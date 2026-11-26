"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .residue import classify_residue_rules, scan_residue_rules, go_test_skip_count
except ImportError:  # Standalone Runtime Draft layout.
    from residue import classify_residue_rules, scan_residue_rules, go_test_skip_count


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*"
    r"|[^/]*_(?:test|spec)\.[^/]+|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)
_SKIP_MARKERS = (
    re.compile(r"pytest\.mark\.skip", re.IGNORECASE),
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
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
# Actual testing receiver calls are counted by go_test_skip_count. Merely
# querying testing.Short(), or calling an imported iterator Skip operation,
# does not disable a test.


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
        if character == "\\":
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


# Whole-invocation patterns used only for neutralisation: they consume the call
# and its arguments so an appended ``t.Skip("...")`` line becomes empty instead
# of leaving its argument list behind.
_NEUTRALIZE_PATTERNS = (
    re.compile(r"pytest\s*\.\s*mark\s*\.\s*(?:skip|xfail)(?:\s*\([^\n]*\))?"),
    re.compile(r"(?:describe|it|test)\s*\.\s*(?:skip|only)\s*\([^\n]*\)"),
    re.compile(r"@(?:unittest\s*\.\s*)?skip(?:If|Unless)?\b(?:\s*\([^\n]*\))?"),
    re.compile(r"\b(?:skip|xfail|disabled)\s*=\s*true\b"),
    re.compile(r"\b[A-Za-z_]\w*\s*\.\s*Skip(?:Now|f)?\s*\([^\n]*\)"),
    re.compile(r"\btesting\s*\.\s*Short\s*\(\s*\)"),
    re.compile(r"\bx(?:describe|context|it|specify)\s*\([^\n]*\)"),
)
_LINE_COMMENT = re.compile(r"//.*$")


def _neutralize_skip_lines(text: str) -> str:
    """Drop lines whose only content is a ``skip``/``xfail`` invocation.

    ``tests_not_tampered`` must catch deleted/weakened assertions, while a
    candidate that *only* adds a skip is a non-cheating ``test_integrity``
    failure (``no_new_skip_or_xfail``, reward 0).  Removing marker-only lines
    before hashing keeps those two facts separate: a pure skip addition leaves
    the digest unchanged, but removing or weakening an assertion still changes
    it.  Whole marker-only lines are dropped (rather than the marker text being
    blanked) so an appended ``t.Skip(...)`` statement does not leave an extra
    empty line behind and trip the tampering atom.
    """

    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line
        for pattern in _NEUTRALIZE_PATTERNS:
            body = pattern.sub("", body)
        body = _LINE_COMMENT.sub("", body)
        if line.strip() and not body.strip(" \t;\r\n"):
            continue
        kept.append(line)
    return "".join(kept)


def _integrity_digest(path: Path) -> str:
    """Skip-marker neutralised test-file digest used by ``tests_not_tampered``."""

    text = path.read_text(encoding="utf-8", errors="ignore")
    encoded = _neutralize_skip_lines(text).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


# Assertion primitives that make a Go test *fail*.  ``t.Log``/``t.Logf`` and
# ``fmt.Print*`` only record output, so converting an assertion into one of them
# (the ``adv-test-assert-weak`` bypass) silently disables the regression.
_FAIL_CAPABLE_ASSERTION = re.compile(
    r"\b[tb]\s*\.\s*(?:Fatal|Fatalf|FailNow|Fail|Failf|Error|Errorf)\s*\("
    r"|\brequire\s*\.\s*\w+\s*\("
    r"|\bassert\s*\.\s*\w+\s*\("
    r"|\bpanic\s*\("
)
_TARGET_WEAKENING_SIMILARITY = 0.85


def _fail_capable_assertions(text: str) -> int:
    """Count assertions that actually fail a Go test (comments/strings excluded)."""

    return len(_FAIL_CAPABLE_ASSERTION.findall(_strip_comments(text)))


def _normalized_test_lines(text: str) -> list[str]:
    """Comment/whitespace-normalised, string-blanked lines used for similarity."""

    lines: list[str] = []
    for line in _strip_comments(text).splitlines():
        body = re.sub(r"\s+", " ", line).strip()
        if body:
            lines.append(body)
    return lines


def _unreachable_fail_capable_assertions(text: str) -> int:
    """Count fail-capable assertions a top-level ``return`` has already ended.

    Go terminates the enclosing block at a statement-level ``return``; any
    statement written after it in the same block can never execute.  A target
    test that keeps every assertion textually but places an unconditional
    ``return`` in front of them has neutralised the regression while leaving the
    assertion *count* unchanged (the ``adv-test-early-return`` bypass).  Braces,
    returns and assertions are processed in offset order so single-line blocks
    (``func() int { return 1 }``) and conditional returns
    (``if !ok { return }``) only kill their own block.
    """

    stripped = _strip_comments(text)
    events: list[tuple[int, str]] = []
    events.extend((match.start(), "open") for match in re.finditer(r"\{", stripped))
    events.extend((match.start(), "close") for match in re.finditer(r"\}", stripped))
    events.extend(
        (match.start(), "return") for match in re.finditer(r"\breturn\b", stripped)
    )
    events.extend(
        (match.start(), "assert") for match in _FAIL_CAPABLE_ASSERTION.finditer(stripped)
    )
    events.sort(key=lambda item: item[0])
    parents: list[bool] = []
    dead = False
    unreachable = 0
    for _, kind in events:
        if kind == "open":
            parents.append(dead)
        elif kind == "close":
            if parents:
                dead = parents.pop()
        elif kind == "return":
            dead = True
        elif dead:
            unreachable += 1
    return unreachable


def _reachable_fail_capable_assertions(text: str) -> int:
    """Fail-capable assertions that are not dead code after a ``return``."""

    return _fail_capable_assertions(text) - _unreachable_fail_capable_assertions(text)


def _target_test_weakened(record: object, candidate_text: str) -> bool:
    """True when a target-origin test is the certified test with weaker asserts.

    The certified target test is stored verbatim in the baseline as
    ``reference``.  A candidate that merely rewrites its migration tests is
    allowed to use its own structure; a candidate whose file is a near-verbatim
    copy of the certified test but with fewer *reachable* fail-capable
    assertions has removed, downgraded or neutralised assertions and is
    tampering.  Reachability (not the raw text count) is what is compared, so an
    early ``return`` that strands the assertions is caught even though every
    assertion is still present in the source.
    """

    if not isinstance(record, Mapping):
        return False
    reference = record.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        return False
    if _reachable_fail_capable_assertions(candidate_text) >= _reachable_fail_capable_assertions(reference):
        return False
    reference_lines = _normalized_test_lines(reference)
    candidate_lines = _normalized_test_lines(candidate_text)
    if not reference_lines or not candidate_lines:
        return False
    similarity = difflib.SequenceMatcher(
        None, reference_lines, candidate_lines, autojunk=False
    ).ratio()
    return similarity >= _TARGET_WEAKENING_SIMILARITY


def _marker_counts(root: Path) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if relative.endswith(".go"):
            counts["skip"] += go_test_skip_count(text)
            # Frontend patterns are case-insensitive and would otherwise count
            # a legitimate imported alias such as it.Skip as a disabled spec.
            continue
        text = _strip_comments(text)
        # Go disables a test with ``t.Skip``/``t.Skipf``/``t.SkipNow`` (and the
        # benchmark variants); ``testing.Short()`` guards a conditional skip.
        # Unlike the frontend markers these are counted in *every* source file,
        # not only files named like tests: a candidate can otherwise delegate the
        # skip to a helper in an ordinary ``.go`` file and call it from the test,
        # which is exactly the ``adv-skip-delegate-helper`` bypass (the marker
        # never appears in a scanned ``*_test.go`` file, so ``delta.skip`` stayed
        # zero).  The helper is still a disabled test at run time.
        if not _is_test(relative):
            continue
        counts["skip"] += sum(
            1 for pattern in _SKIP_MARKERS[:1] for _ in pattern.finditer(text)
        )
        counts["xfail"] += sum(
            1 for pattern in (_SKIP_MARKERS[1],) for _ in pattern.finditer(text)
        )
        counts["disabled"] += sum(
            1
            for pattern in (*_SKIP_MARKERS[2:], _DISABLED_ALIAS)
            for _ in pattern.finditer(text)
        )
    return counts


def build_source_test_baseline(root: Path) -> dict[str, object]:
    """Freeze source-visible test identities and marker counts for a Case."""

    files = {
        relative: {"sha256": _digest(path)}
        for relative, path in sorted(_regular_files(root).items())
        if _is_test(relative)
    }
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
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in rows]
    status = (
        "INFRA_ERROR" if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses)
        else "INCONCLUSIVE" if any(item in {"INCONCLUSIVE", "NA"} for item in statuses)
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


def _test_integrity(
    root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    source: str = "source-test-baseline",
    receipts: Sequence[Mapping[str, Any]] = (),
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

    def _is_target_origin(record: object) -> bool:
        return isinstance(record, Mapping) and record.get("origin") == "target"

    missing_source = sorted(
        str(relative) for relative, record in baseline_files.items()
        if str(relative) not in current and not _is_target_origin(record)
    )
    # A migration-target test the candidate was asked to author is created by
    # the migration, not shipped by the source repo.  Deleting it is an
    # incomplete migration, which ``migration_scope_complete`` already scores;
    # only *modifying* its content is test tampering.  Source-owned tests must
    # survive verbatim, so their deletion stays a tampering signal.
    missing_target = sorted(
        str(relative) for relative, record in baseline_files.items()
        if str(relative) not in current and _is_target_origin(record)
    )
    allowed = {
        str(item).replace("\\", "/").strip("/")
        for item in config.get("policy", {}).get("allowed_equivalent_test_paths", [])
    } if isinstance(config.get("policy"), Mapping) else set()

    def _weakened_target(relative: str, record: object) -> bool:
        if relative not in current or not isinstance(record, Mapping):
            return False
        if record.get("sha256") == _integrity_digest(current[relative]):
            return False
        if relative in allowed:
            return False
        try:
            text = current[relative].read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return _target_test_weakened(record, text)

    changed_source = sorted(
        relative for relative, record in baseline_files.items()
        if relative in current
        and not _is_target_origin(record)
        and isinstance(record, Mapping)
        and record.get("sha256") != _integrity_digest(current[relative])
        and relative not in allowed
    )
    # A target test is authored by the migration, so it may legitimately differ
    # from the certified translation; only a near-verbatim copy with fewer
    # fail-capable assertions is provable tampering (assertion weakening).
    changed_target = sorted(
        relative for relative, record in baseline_files.items()
        if _is_target_origin(record) and _weakened_target(relative, record)
    )
    changed = changed_source + changed_target
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = _marker_counts(root)
    static_go_skips = sum(
        go_test_skip_count(path.read_text(encoding="utf-8", errors="ignore"))
        for relative, path in current.items() if relative.endswith(".go")
    )
    runtime_go_skips = None
    for receipt in receipts:
        command = receipt.get("command")
        summary = receipt.get("go_test_summary")
        # Only the verifier's full default Go test run can resolve a static
        # Go marker. A filtered/tagged run or incomplete report cannot do so.
        if (receipt.get("kind") == "target-native-test"
                and receipt.get("returncode") == 0 and not receipt.get("timed_out")
                and isinstance(command, list) and len(command) == 4
                and Path(str(command[0])).name == "go"
                and command[1:] == ["test", "-json", "./..."]
                and isinstance(summary, Mapping) and summary.get("complete") is True
                and isinstance(summary.get("skipped"), list)):
            runtime_go_skips = len(summary["skipped"])
            break
    if runtime_go_skips is not None:
        # A guard for an unavailable build-tag API does not skip any default
        # behavior assertion. Keep other languages' static markers and use
        # actual Go skip events; helper/alias Skip calls remain observable.
        marker_after["skip"] += runtime_go_skips - static_go_skips
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if missing_source or changed else "PASS",
        source=source,
        evidence={
            "missing": missing_source,
            "missing_target": missing_target,
            "changed": changed,
            "changed_source": changed_source,
            "changed_target_weakened": changed_target,
        },
    )
    skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={"before": dict(marker_before), "after": marker_after, "delta": marker_delta,
                  "static_go_skips": static_go_skips, "runtime_go_skips": runtime_go_skips},
    )
    return tamper, skip


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
    structural_ids = policy.get("structural_differential_ids", [])
    structural_ids = (
        {str(item) for item in structural_ids}
        if isinstance(structural_ids, Sequence) and not isinstance(structural_ids, (str, bytes))
        else set()
    )
    # Behavioral differentials prove "the same base swap did not change
    # observable behavior"; per the migration verifier contract they are a
    # sub-item of the ``p2p_regression_preserved`` hard gate, not a stand-alone
    # verifier composition.  Structural differentials (e.g. delivered test-file
    # presence, build-tag matrix) score through ``migration_scope_complete``.
    behavioral_differential = [
        row
        for row in rows
        if row.get("kind") == "DIFFERENTIAL" and str(row.get("id")) not in structural_ids
    ]
    semantic_rules = policy.get("semantic_rules", [])
    residue_rules = policy.get("ordinary_residue_rules", policy.get("residue_rules", semantic_rules))
    residue_diagnostics = classify_residue_rules(candidate_root, residue_rules)
    residue_hits = scan_residue_rules(candidate_root, residue_rules)
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
    deceptive_diagnostics = classify_residue_rules(candidate_root, deceptive_rules)
    deceptive_hits = scan_residue_rules(candidate_root, deceptive_rules)
    protected = policy.get("protected_paths", config.get("protected_paths", []))
    protected = protected if isinstance(protected, Sequence) and not isinstance(protected, (str, bytes)) else []
    marker_names = policy.get("forbidden_report_markers", [])
    marker_names = marker_names if isinstance(marker_names, Sequence) and not isinstance(marker_names, (str, bytes)) else []
    protected_hits = _protected_hits(candidate_root, [str(item) for item in protected])
    marker_hits = _report_marker_hits(candidate_root, [str(item) for item in marker_names])
    tests_not_tampered, no_new_skip = _test_integrity(
        candidate_root, source_test_baseline, config, source=test_baseline_source,
        receipts=receipts,
    )
    build_evidence, dependency_evidence, valid_receipts = _derive_execution_atoms(
        receipts
    )
    public = _group_status(
        p2p,
        expected=len(expected_tests.get("p2p", [])) if isinstance(expected_tests.get("p2p", []), Sequence) else None,
        source="candidate-observations",
        empty="NA",
    )
    p2p_regression = _group_status(
        [*p2p, *behavioral_differential],
        expected=(
            (len(expected_tests.get("p2p", [])) + len(behavioral_differential))
            if isinstance(expected_tests.get("p2p", []), Sequence)
            else None
        ),
        source="candidate-p2p-and-behavioral-differential-observations",
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
            "FAIL" if deceptive_hits else "INCONCLUSIVE" if missing_l2 else "NA" if not deceptive_rules else "PASS",
            source="deceptive-fallback-scan",
            evidence=deceptive_hits,
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
        "structural_differential_ids": sorted(structural_ids),
        "behavioral_differential_observation_ids": [
            str(row.get("id")) for row in behavioral_differential
        ],
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
