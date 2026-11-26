"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .residue import classify_residue_rules, scan_residue_rules, strip_comments_text
    from .residue import _paths as _residue_paths
    from .go_source import GoSource
    from .go_test_flow import assertion_flow, canonical_tokens, go_assertion_counts, go_tautology_count, preserves_flow
except ImportError:  # Standalone Runtime Draft layout.
    from residue import classify_residue_rules, scan_residue_rules, strip_comments_text
    from residue import _paths as _residue_paths
    from go_source import GoSource
    from go_test_flow import assertion_flow, canonical_tokens, go_assertion_counts, go_tautology_count, preserves_flow


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*"
    r"|[^/]*_(?:test|spec)\.[^/]+|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)
# Disabled-test markers, bucketed by severity so a newly introduced skip is
# distinguishable from an existing one.  Go's ``t.Skip``/``t.SkipNow``/``t.Skipf``
# disable the current test and MUST be counted like ``pytest.mark.skip``.
_SKIP_PATTERNS = (
    re.compile(r"pytest\.mark\.skip", re.IGNORECASE),
    re.compile(r"\bt\.Skip(?:Now|f)?\s*\(", re.IGNORECASE),
)
_XFAIL_PATTERNS = (
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
)
_DISABLED_PATTERNS = (
    re.compile(r"(?:describe|it|test)\.skip\s*\(", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE),
    re.compile(r"\b(?:skip|xfail|disabled)\s*=\s*true\b", re.IGNORECASE),
)
# Mocha disables a suite/spec with x-prefixed aliases (``xdescribe``,
# ``xcontext``, ``xit``, ``xspecify``).  They are semantically identical to
# ``.skip`` and a migration translates them one-for-one, so they must be counted
# symmetrically; otherwise the mechanical ``xdescribe -> describe.skip`` rewrite
# looks like a newly introduced skip.  Only invocations count: a compatibility
# shim that *defines* ``globals.xit = it.skip`` (or references ``xit``) is not a
# disabled test.
_DISABLED_ALIAS = re.compile(r"\bx(?:describe|context|it|specify)\s*\(", re.IGNORECASE)

# Assertion-strength signature (RG-A1): a changed test file is tolerated only
# when it keeps every baseline test identity and does not reduce assertion
# strength.  Comments/formatting/branch reshapes are therefore allowed, while
# deleting or weakening assertions is still caught as tampering.
_TEST_NAME_PATTERNS = (
    re.compile(r"\bfunc\s+((?:Test|Example|Benchmark)[A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\s*(?:async\s+)?def\s+(test[A-Za-z0-9_]*)\s*\(", re.MULTILINE),
    re.compile(r"(?:^|\n)\s*(?:it|test|describe)\s*\(\s*['\"]([^'\"]+)['\"]"),
)
_ASSERTION_PATTERNS = (
    re.compile(r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\b"),
    re.compile(r"\b(?:require|assert)\.[A-Za-z]"),
    re.compile(r"\bexpect\s*\("),
    re.compile(r"\bself\.assert\w*\s*\("),
    re.compile(
        r"\.(?:assertEqual|assertTrue|assertFalse|assertIsNone|assertRaises|assertIn|assertIs)\s*\("
    ),
)
# "Strong" (value/equality) assertions.  Weakening a strong assertion into a
# mere presence check (``assert.Exactly`` -> ``assert.NotNil``) leaves the token
# count unchanged, so strength must be tracked by *kind*, not count (RG-A1 r2).
_STRONG_ASSERTION_PATTERNS = (
    re.compile(r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\b"),
    re.compile(
        r"\b(?:require|assert)\.(?:Equal|Exactly|DeepEqual|Equals|Error|Errorf|ErrorIs|ErrorAs"
        r"|Contains|NotContains|Len|Greater|GreaterOrEqual|Less|LessOrEqual|NotEqual|Zero|Regexp)\b"
    ),
    re.compile(r"\bexpect\s*\("),
    re.compile(
        r"\.(?:assertEqual|assertNotEqual|assertIs|assertIsNot|assertIn|assertNotIn|assertAlmostEqual)\s*\("
    ),
    re.compile(
        r"self\.(?:assertEqual|assertNotEqual|assertIs|assertIn|assertNotIn|assertAlmostEqual)\s*\("
    ),
)
# A value assertion whose two compared operands are textually identical is a
# tautology (e.g. ``assert.Exactly(t, body, body)``): it preserves the assertion
# count and identifier but checks nothing, so it counts as weakening (RG-A1 r3).
_TAUTOLOGY_RE = re.compile(
    r"\b(?:assert|require)\.\w+\s*\(\s*[^,()]*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*\1\s*\)"
)


def _assertion_signature(text: str) -> dict[str, object]:
    is_go = GoSource(text).tokens[:1] == ['package']
    stripped = _strip_comments(' '.join(canonical_tokens(text)).replace(' . ', '.') if is_go else text)
    names: set[str] = set()
    for pattern in _TEST_NAME_PATTERNS:
        names.update(str(item) for item in pattern.findall(stripped))
    count = sum(len(pattern.findall(stripped)) for pattern in _ASSERTION_PATTERNS)
    strong = sum(len(pattern.findall(stripped)) for pattern in _STRONG_ASSERTION_PATTERNS)
    if is_go:
        count, strong, _ = go_assertion_counts(text)
    tautology = go_tautology_count(text) if is_go else len(_TAUTOLOGY_RE.findall(stripped))
    signature = {
        "test_names": sorted(names),
        "assertion_count": count,
        "strong_assertion_count": strong,
        "tautology_count": tautology,
    }
    if is_go:
        signature['go_assertion_flow'] = assertion_flow(text)
    return signature


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return "test" in {part.casefold() for part in parts[:-1]} or bool(
        _TEST_NAME.search(path)
    )


_EXCLUDED_PARTS = frozenset(
    {".git", "node_modules", ".gomodcache", ".gocache", "__pycache__", ".pytest_cache"}
)


def _regular_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        return {}
    values: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in _EXCLUDED_PARTS for part in Path(relative).parts):
            continue
        values[relative] = path
    return values


def _strip_comments(text: str) -> str:
    """Blank comment and string content while preserving offsets.

    Marker counting must not treat prose in comments or string literals as a
    disabled test.  Replacing the skipped characters with spaces keeps line
    numbers and column offsets stable for diagnostics.
    """

    characters = list(text)
    length = len(text)
    state = "normal"
    quote = ""
    index = 0
    while index < length:
        character = text[index]
        if state == "normal":
            if character == "/" and index + 1 < length and text[index + 1] == "/":
                while index < length and text[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            if character == "#":
                while index < length and text[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            if character == "/" and index + 1 < length and text[index + 1] == "*":
                characters[index] = " "
                characters[index + 1] = " "
                index += 2
                while index < length and not (
                    text[index] == "*"
                    and index + 1 < length
                    and text[index + 1] == "/"
                ):
                    if text[index] != "\n":
                        characters[index] = " "
                    index += 1
                if index < length:
                    characters[index] = " "
                    index += 1
                if index < length:
                    characters[index] = " "
                    index += 1
                continue
            if character in ("'", '"', "`"):
                state = "string"
                quote = character
                index += 1
                continue
            index += 1
            continue
        if character == "\\" and quote != "`":
            index += 1
            if index < length:
                if text[index] != "\n":
                    characters[index] = " "
                index += 1
            continue
        if character == quote:
            state = "normal"
            index += 1
            continue
        if character != "\n":
            characters[index] = " "
        index += 1
    return "".join(characters)


def _marker_counts(root: Path) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _strip_comments(text)
        if GoSource(text).tokens[:1] == ['package']:
            counts["skip"] += go_assertion_counts(text)[2]
        else:
            counts["skip"] += sum(len(pattern.findall(text)) for pattern in _SKIP_PATTERNS)
        counts["xfail"] += sum(len(pattern.findall(text)) for pattern in _XFAIL_PATTERNS)
        counts["disabled"] += sum(
            len(pattern.findall(text))
            for pattern in (*_DISABLED_PATTERNS, _DISABLED_ALIAS)
        )
    return counts


def build_source_test_baseline(root: Path) -> dict[str, object]:
    """Freeze source-visible test identities, signatures and marker counts."""

    files: dict[str, dict[str, object]] = {}
    for relative, path in sorted(_regular_files(root).items()):
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        files[relative] = {"sha256": _digest(path), **_assertion_signature(text)}
    return {
        "schema_version": "migration-source-test-baseline:v1",
        "files": files,
        "marker_counts": _marker_counts(root),
    }


def _atom(
    status: str,
    *,
    source: str,
    evidence: object = None,
) -> dict[str, object]:
    return {
        "status": status,
        "source": source,
        "evidence": [] if evidence is None else evidence,
    }


def _status(value: object, *, source: str, default: str = "INCONCLUSIVE") -> dict[str, object]:
    if isinstance(value, Mapping) and isinstance(value.get("status"), str):
        return {
            "status": str(value["status"]),
            "source": str(value.get("source", source)),
            "evidence": value.get("evidence", []),
        }
    if isinstance(value, bool):
        return _atom("PASS" if value else "FAIL", source=source)
    if value is None:
        return _atom(default, source=source)
    return _atom(str(value), source=source)


def _rows(observations: object) -> list[Mapping[str, Any]]:
    return [
        row for row in observations
        if isinstance(row, Mapping)
    ] if isinstance(observations, Sequence) and not isinstance(observations, (str, bytes)) else []


def _receipt_identity(receipt: Mapping[str, Any]) -> str:
    command = receipt.get("command", [])
    return json.dumps(
        {
            "kind": receipt.get("kind"),
            "phase": receipt.get("phase"),
            "index": receipt.get("index"),
            "command": command,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _derive_execution_atoms(
    receipts: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    accepted_kinds = {
        "environment-build",
        "target-build",
        "target-native-test",
        "dependency-resolution",
        "import-probe",
        "version-probe",
        "target-execution",
        "report",
    }
    valid: list[dict[str, object]] = []
    malformed: list[dict[str, object]] = []
    seen: dict[str, tuple[object, object, object]] = {}
    contradictions: list[dict[str, object]] = []
    for offset, raw in enumerate(receipts):
        receipt = dict(raw)
        kind = receipt.get("kind")
        if (
            receipt.get("schema_version") != "migration-execution-receipt:v1"
            or kind not in accepted_kinds
            or not isinstance(receipt.get("timed_out"), bool)
        ):
            malformed.append({"index": offset, "receipt": receipt})
            continue
        returncode = receipt.get("returncode")
        if returncode is not None and (
            not isinstance(returncode, int) or isinstance(returncode, bool)
        ):
            malformed.append({"index": offset, "receipt": receipt})
            continue
        identity = _receipt_identity(receipt)
        facts = (
            returncode,
            receipt.get("timed_out"),
            receipt.get("report_present"),
        )
        if identity in seen and seen[identity] != facts:
            contradictions.append(
                {"identity": identity, "first": seen[identity], "second": facts}
            )
        else:
            seen[identity] = facts
        valid.append(receipt)
    evidence = {
        "receipts": valid,
        "malformed": malformed,
        "contradictions": contradictions,
    }
    if contradictions:
        atom = _atom("INFRA_ERROR", source="execution-receipts", evidence=evidence)
        return atom, atom, valid
    if any(bool(row.get("timed_out")) for row in valid):
        atom = _atom("INFRA_ERROR", source="execution-receipts", evidence=evidence)
        return atom, atom, valid

    environment = [row for row in valid if row.get("kind") == "environment-build"]
    target_build = [
        row
        for row in valid
        if row.get("kind") in {"target-build", "target-native-test"}
    ]
    dependency = [
        row
        for row in valid
        if row.get("kind") in {"dependency-resolution", "import-probe", "version-probe"}
    ]
    executions = [row for row in valid if row.get("kind") == "target-execution"]
    reports = [row for row in valid if row.get("kind") == "report"]
    report_present = any(row.get("report_present") is True for row in executions)
    report_valid = not reports or all(row.get("valid") is True for row in reports)

    if any(row.get("returncode") not in {0, None} for row in environment):
        build = _atom("INFRA_ERROR", source="environment-build-receipt", evidence=evidence)
    elif any(row.get("returncode") not in {0, None} for row in target_build):
        build = _atom("FAIL", source="target-build-receipt", evidence=evidence)
    elif target_build and all(row.get("returncode") == 0 for row in target_build):
        build = _atom("PASS", source="target-build-receipt", evidence=evidence)
    elif any(row.get("returncode") not in {0, None} for row in executions):
        build = _atom("FAIL", source="target-execution-receipt", evidence=evidence)
    elif report_present and report_valid:
        build = _atom("PASS", source="target-execution-receipt", evidence=evidence)
    else:
        build = _atom("INCONCLUSIVE", source="execution-receipts", evidence=evidence)

    if any(row.get("returncode") not in {0, None} for row in dependency):
        dependencies = _atom(
            "FAIL", source="dependency-probe-receipts", evidence=evidence
        )
    elif dependency and all(row.get("returncode") == 0 for row in dependency):
        dependencies = _atom(
            "PASS", source="dependency-probe-receipts", evidence=evidence
        )
    elif any(row.get("returncode") not in {0, None} for row in executions):
        dependencies = _atom(
            "FAIL", source="target-execution-receipt", evidence=evidence
        )
    elif report_present and report_valid:
        dependencies = _atom(
            "PASS", source="target-execution-receipt", evidence=evidence
        )
    else:
        dependencies = _atom(
            "INCONCLUSIVE", source="execution-receipts", evidence=evidence
        )
    return build, dependencies, valid


def _group_status(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected: int | None,
    source: str,
    empty: str = "NA",
) -> dict[str, object]:
    if expected == 0 or (expected is None and not rows):
        return _atom(empty, source=source, evidence={"total": 0, "checks": []})
    if not rows:
        return _atom("INCONCLUSIVE", source=source, evidence={"total": 0, "checks": []})
    applicable = [
        row for row in rows if str(row.get("status", "INCONCLUSIVE")) != "NA"
    ]
    if not applicable:
        # Every row is not-applicable: the check does not participate (na must
        # never be treated as pass or fail).
        return _atom(
            empty,
            source=source,
            evidence={"passed": 0, "total": 0, "checks": [dict(row) for row in rows]},
        )
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in applicable]
    status = (
        "INFRA_ERROR" if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses)
        else "INCONCLUSIVE" if any(item == "INCONCLUSIVE" for item in statuses)
        else "FAIL" if any(item == "FAIL" for item in statuses)
        else "PASS"
    )
    return _atom(
        status,
        source=source,
        evidence={
            "passed": sum(item == "PASS" for item in statuses),
            "total": len(statuses),
            "checks": [dict(row) for row in rows],
        },
    )


def _protected_hits(root: Path, protected: Sequence[str]) -> list[str]:
    hits: list[str] = []
    for relative in _regular_files(root):
        normalized = relative.replace("\\", "/")
        for raw in protected:
            path = str(raw).replace("\\", "/").strip("/")
            if path and (normalized == path or normalized.startswith(path + "/")):
                hits.append(normalized)
                break
    for raw in protected:
        path = root / Path(str(raw).replace("\\", "/"))
        if path.exists() and str(raw).replace("\\", "/") not in hits:
            hits.append(str(raw).replace("\\", "/").strip("/"))
    return sorted(set(hits))


def _report_marker_hits(root: Path, markers: Sequence[str]) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    if not markers:
        return hits
    for relative, path in _regular_files(root).items():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for marker in markers:
            if str(marker) and str(marker).casefold() in text.casefold():
                hits.append({"path": relative, "marker": str(marker)})
    return hits


def _equivalent_test_rewrite(record: Mapping[str, Any], path: Path) -> bool:
    """Tolerate a changed test file only when assertion strength is preserved.

    A migration may legitimately adapt a test file (comments, formatting,
    branch reshapes) as long as every baseline test identity survives and no
    assertion strength is lost.  Deleting or weakening assertions still fails.
    """

    before_count = record.get("assertion_count")
    if not isinstance(before_count, int) or isinstance(before_count, bool):
        return False
    before_names = record.get("test_names")
    before_names = (
        {str(item) for item in before_names} if isinstance(before_names, list) else set()
    )
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    after = _assertion_signature(text)
    after_names = {str(item) for item in after["test_names"]}
    before_strong = record.get("strong_assertion_count")
    strong_ok = (
        not isinstance(before_strong, int)
        or isinstance(before_strong, bool)
        or int(after["strong_assertion_count"]) >= before_strong
    )
    before_taut = record.get("tautology_count")
    taut_ok = (
        not isinstance(before_taut, int)
        or isinstance(before_taut, bool)
        or int(after["tautology_count"]) <= before_taut
    )
    return (
        before_names.issubset(after_names)
        and int(after["assertion_count"]) >= before_count
        and strong_ok
        and taut_ok
        and (not isinstance(record.get('go_assertion_flow'), Mapping)
             or preserves_flow(record['go_assertion_flow'], after.get('go_assertion_flow', {})))
    )


def _test_integrity(
    root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    source: str = "source-test-baseline",
) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(baseline, Mapping):
        inconclusive = _atom("INCONCLUSIVE", source=source)
        return inconclusive, inconclusive
    baseline_files = baseline.get("files", {})
    baseline_files = baseline_files if isinstance(baseline_files, Mapping) else {}
    if not baseline_files:
        # A pass here would be vacuous: there is no test file to protect.  Keep
        # the evidence honest without turning a repository with no target tests
        # into a hard failure.
        empty = _atom(
            "NA",
            source=source,
            evidence={"reason": "baseline-has-no-test-files", "files": 0},
        )
        return empty, empty
    current = _regular_files(root)
    missing = sorted(
        str(relative) for relative in baseline_files
        if str(relative) not in current
    )
    policy = config.get("policy", {})
    policy = policy if isinstance(policy, Mapping) else {}
    allowed = {
        str(item).replace("\\", "/").strip("/")
        for item in policy.get("allowed_equivalent_test_paths", [])
    }
    tolerance = policy.get("test_integrity", {})
    tolerance = tolerance if isinstance(tolerance, Mapping) else {}
    assertion_strength = tolerance.get("assertion_strength", True) is not False
    changed = []
    for relative, record in baseline_files.items():
        if relative not in current or not isinstance(record, Mapping) or relative in allowed:
            continue
        if record.get("sha256") == _digest(current[relative]):
            continue
        if assertion_strength and _equivalent_test_rewrite(record, current[relative]):
            continue
        changed.append(relative)
    changed = sorted(changed)
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = _marker_counts(root)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if missing or changed else "PASS",
        source=source,
        evidence={"missing": missing, "changed": changed},
    )
    skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={"before": dict(marker_before), "after": marker_after, "delta": marker_delta},
    )
    return tamper, skip


def _mime_go_sources(candidate_root: Path, spec: Mapping[str, Any]) -> list[GoSource]:
    paths = spec.get("paths", [])
    paths = tuple(paths) if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)) else ()
    exclude = spec.get("exclude", [])
    exclude = tuple(exclude) if isinstance(exclude, Sequence) and not isinstance(exclude, (str, bytes)) else ()
    if not paths:
        return []
    sources = []
    for path in _residue_paths(candidate_root, paths, exclude):
        if path.suffix != ".go" or path.name.endswith("_test.go"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        sources.append(GoSource(text))
    return sources


def _target_delegated(candidate_root: Path, policy: Mapping[str, Any]) -> bool | None:
    """Require a real, import-bound parser call, independent of its local alias.

    This is a necessary mechanism check. The behavior probes still determine
    whether traversal, decoding, collection and signature extraction work.
    """

    spec = policy.get("target_delegation")
    if not isinstance(spec, Mapping):
        return None
    calls = spec.get("api_calls")
    if not isinstance(calls, Mapping) or not calls:
        return None
    sources = _mime_go_sources(candidate_root, spec)
    if not sources:
        return None
    return any(source.calls(package, symbols)
               for source in sources for package, symbols in calls.items())


def _target_reimplemented(candidate_root: Path, policy: Mapping[str, Any]) -> bool | None:
    """Detect replacement of migrated MIME traversal, not envelope reading.

    The baseline already uses net/mail.ReadMessage for the top-level envelope;
    that operation was not supplied by go-mime and is outside the migrated
    responsibilities. Keeping it before go-message.New is allowed. A real
    go-message mechanism and the public behavior probes remain mandatory.
    Old go-mime imports and runtime bridges are independently forbidden.
    """

    spec = policy.get("target_reimplementation")
    if not isinstance(spec, Mapping):
        return None
    calls = spec.get("api_calls")
    if not isinstance(calls, Mapping) or not calls:
        return None
    sources = _mime_go_sources(candidate_root, spec)
    if not sources:
        return None
    return any(not source.discards_first_result(start, opening)
               for source in sources for package, symbols in calls.items()
               if package != "net/mail"
               for start, opening in source.calls(package, symbols))


def build_hardgate_evidence(
    candidate_root: Path,
    *,
    config: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    observation_errors: Sequence[str],
    receipts: Sequence[Mapping[str, Any]],
    patch_applicable: bool | None,
    source_test_baseline: Mapping[str, Any] | None,
    test_baseline_source: str = "source-test-baseline",
) -> dict[str, object]:
    """Derive hardgate atoms from Case evidence instead of policy booleans."""

    policy = config.get("policy", {})
    policy = policy if isinstance(policy, Mapping) else {}
    rows = _rows(observations)
    expected_tests = config.get("tests", {})
    expected_tests = expected_tests if isinstance(expected_tests, Mapping) else {}
    p2p = [row for row in rows if row.get("kind") == "P2P"]
    state = [row for row in rows if row.get("kind") == "STATE"]
    semantic_rules = policy.get("semantic_rules", [])
    residue_rules = policy.get("ordinary_residue_rules", policy.get("residue_rules", semantic_rules))
    residue_diagnostics = classify_residue_rules(candidate_root, residue_rules, strip_strings=True)
    residue_hits = scan_residue_rules(candidate_root, residue_rules, strip_strings=True)
    deceptive_rules = policy.get("deceptive_fallback_rules", [])
    exemption = policy.get("deceptive_fallback_not_applicable")
    migration = config.get("migration", {})
    category = migration.get("category") if isinstance(migration, Mapping) else None
    missing_l2 = (
        category != "version_upgrade"
        and not deceptive_rules
        and not (
            isinstance(exemption, Mapping)
            and str(exemption.get("reason", "")).strip()
            and str(exemption.get("evidence", "")).strip()
        )
    )
    deceptive_diagnostics = classify_residue_rules(candidate_root, deceptive_rules, strip_strings=False)
    deceptive_hits = scan_residue_rules(candidate_root, deceptive_rules, strip_strings=False)
    delegated = _target_delegated(candidate_root, policy)
    reimplemented = _target_reimplemented(candidate_root, policy)
    protected = policy.get("protected_paths", config.get("protected_paths", []))
    protected = protected if isinstance(protected, Sequence) and not isinstance(protected, (str, bytes)) else []
    marker_names = policy.get("forbidden_report_markers", [])
    marker_names = marker_names if isinstance(marker_names, Sequence) and not isinstance(marker_names, (str, bytes)) else []
    protected_hits = _protected_hits(candidate_root, [str(item) for item in protected])
    marker_hits = _report_marker_hits(candidate_root, [str(item) for item in marker_names])
    tests_not_tampered, no_new_skip = _test_integrity(
        candidate_root, source_test_baseline, config, source=test_baseline_source
    )
    build_evidence, dependency_evidence, valid_receipts = _derive_execution_atoms(
        receipts
    )
    differential = [row for row in rows if row.get("kind") == "DIFFERENTIAL"]
    public = _group_status(
        differential,
        expected=len(expected_tests.get("differential", []))
        if isinstance(expected_tests.get("differential", []), Sequence)
        else None,
        source="candidate-contract-observations",
        empty="NA",
    )
    p2p_regression = _group_status(
        p2p,
        expected=len(expected_tests.get("p2p", []))
        if isinstance(expected_tests.get("p2p", []), Sequence)
        else None,
        source="candidate-p2p-observations",
        empty="NA",
    )
    state_applicable = bool(policy.get("state_applicable"))
    expected_state = expected_tests.get("state", [])
    expected_state = expected_state if isinstance(expected_state, list) else []
    state_roles = {
        str(item.get("id")): str(item.get("state_role", ""))
        for item in expected_state if isinstance(item, Mapping)
    }
    invalid_state_roles = state_applicable and (
        not state_roles
        or any(role not in {"critical_data", "required_state"} for role in state_roles.values())
    )
    critical_rows = [row for row in state if state_roles.get(str(row.get("id"))) == "critical_data"]
    expected_critical = sum(role == "critical_data" for role in state_roles.values())
    critical = (
        _atom("INCONCLUSIVE", source="state-role-validation", evidence={"missing_or_invalid_roles": sorted(state_roles)})
        if invalid_state_roles
        else _group_status(
            critical_rows,
            expected=expected_critical,
            source="candidate-critical-data-observations",
            empty="NA" if not state_applicable else "INCONCLUSIVE",
        )
    )
    anti_evidence = {
        "observation_errors": list(observation_errors),
        "protected_hits": protected_hits,
        "report_marker_hits": marker_hits,
        "receipt_count": len(receipts),
        "valid_receipt_count": len(valid_receipts),
    }
    cheating_errors = [
        error for error in observation_errors
        if any(
            marker in str(error).casefold()
            for marker in ("receipt", "duplicate", "unexpected", "forged")
        )
    ]
    return {
        "patch_applicable": _status(patch_applicable, source="candidate-patch"),
        "build_pass": build_evidence,
        "dependency_resolved": dependency_evidence,
        "no_deceptive_source_fallback": _atom(
            (
                "FAIL" if deceptive_hits
                else "INCONCLUSIVE" if missing_l2
                else "NA" if not deceptive_rules
                else "FAIL" if delegated is False or reimplemented is True
                else "PASS"
            ),
            source="deceptive-fallback-scan",
            evidence=(
                deceptive_hits
                if deceptive_hits
                else {"delegation_failed": True} if delegated is False
                else {"reimplementation_present": True} if reimplemented is True
                else []
            ),
        ),
        "public_contract_preserved": public,
        "p2p_regression_preserved": p2p_regression,
        "critical_data_preserved": critical,
        "tests_not_tampered": tests_not_tampered,
        "no_new_skip_or_xfail": no_new_skip,
        "protected_paths_unchanged": _atom(
            "FAIL" if protected_hits else "PASS",
            source="protected-path-inventory",
            evidence=protected_hits,
        ),
        "anti_cheating_pass": _atom(
            "FAIL" if cheating_errors or protected_hits or marker_hits else "PASS",
            source="observation-and-report-integrity",
            evidence=anti_evidence,
        ),
        "source_hits": [*residue_hits, *deceptive_hits],
        "ordinary_residue_hits": residue_hits,
        "deceptive_fallback_hits": deceptive_hits,
        "residue_diagnostics": residue_diagnostics,
        "deceptive_residue_diagnostics": deceptive_diagnostics,
        "cheating": bool(
            deceptive_hits
            or cheating_errors
            or protected_hits
            or marker_hits
        ),
        "evidence_mode": "derived",
    }


__all__ = ["build_hardgate_evidence", "build_source_test_baseline"]
