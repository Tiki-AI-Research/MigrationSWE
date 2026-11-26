"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .detectors import (
        detect_raw_process_launch,
        detect_source_runtime_dependency,
        detect_source_runtime_dynamic_load,
        detect_source_runtime_ffi,
        detect_source_runtime_subprocess,
    )
    from .residue import classify_residue_rules, scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from detectors import (
        detect_raw_process_launch,
        detect_source_runtime_dependency,
        detect_source_runtime_ffi,
        detect_source_runtime_subprocess,
    )
    from residue import classify_residue_rules, scan_residue_rules


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test_[^/]+\.py|conftest\.py"
    r"|[^/]*(?:\.test|\.spec)\.[^/]+"
    r"|[^/]*_(?:test|spec)\.[^/]+"
    r"|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "e2e"})
_NON_TEST_DIRS = frozenset({"docs", "doc", "assets", "examples", ".github"})
_NON_TEST_SUFFIXES = frozenset(
    {
        ".md", ".markdown", ".txt", ".rst",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
        ".yml", ".yaml", ".lock",
    }
)
_SKIP_MARKERS = (
    re.compile(r"pytest\.mark\.skip(?:if)?", re.IGNORECASE),
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.skip\s*\(", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE),
    re.compile(r"\b(?:skip|xfail|disabled)\s*=\s*true\b", re.IGNORECASE),
    re.compile(r"#\[ignore\]", re.IGNORECASE),
    re.compile(r"\bt\.Skip(?:Now|f)?\s*\(", re.IGNORECASE),
    re.compile(r"@(?:Disabled|Ignore)\b", re.IGNORECASE),
    re.compile(r"\b(?:test|it|describe)\.todo\s*\(", re.IGNORECASE),
    re.compile(r"\bpassWithNoTests\b", re.IGNORECASE),
    # A bare ``exclude`` / ``exclude =`` is ordinary code (``const exclude =
    # new Set()``), not a skipped test, so no generic exclude marker is counted.
    # Test-runner exclude flags live in config files that are not counted here.
)
# Mocha disables a suite/spec with x-prefixed aliases (``xdescribe``,
# ``xcontext``, ``xit``, ``xspecify``).  They are semantically identical to
# ``.skip`` and a migration translates them one-for-one, so they must be counted
# symmetrically; otherwise the mechanical ``xdescribe -> describe.skip`` rewrite
# looks like a newly introduced skip.  Only invocations count: a compatibility
# shim that *defines* ``globals.xit = it.skip`` (or references ``xit``) is not a
# disabled test.
_DISABLED_ALIAS = re.compile(r"\bx(?:describe|context|it|specify)\s*\(", re.IGNORECASE)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


_ASSERTION_TOKENS = re.compile(
    r"\b(?:"
    r"assert(?:_eq|_ne|_matches|_contains|_that)?!?"
    r"|assertEquals|assertNotEquals|assertTrue|assertFalse|assertNull|assertNotNull"
    r"|assertThat|assertRaises|assertIs|assertIn"
    r"|expect|should|require|t\.(?:Error|Fatal|Fatalf|Errorf)"
    r")\b",
    re.IGNORECASE,
)


def _strip_comments_keep_strings(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)//.*$", " ", text)
    text = re.sub(r"(?m)#(?!\[).*$", " ", text)
    return text


def _normalized_text(text: str) -> str:
    text = _strip_comments_keep_strings(text)
    return re.sub(r"\s+", " ", text).strip()


def _assertion_signature(text: str) -> str:
    text = _strip_comments_keep_strings(text)
    rows: list[str] = []
    for line in text.splitlines():
        if not _ASSERTION_TOKENS.search(line):
            continue
        normalized = _ASSERTION_TOKENS.sub("ASSERT", line)
        normalized = re.sub(r"[\s'\"!]+", "", normalized)
        if normalized:
            rows.append(normalized)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest() if rows else ""


def _normalized_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(_normalized_text(text).encode("utf-8")).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    directories = {part.casefold() for part in parts[:-1]}
    if directories & _NON_TEST_DIRS:
        return False
    if Path(path).suffix.casefold() in _NON_TEST_SUFFIXES:
        return False
    return bool(directories & _TEST_DIRS) or bool(_TEST_NAME.search(path))


_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        "node_modules",
        ".gomodcache",
        ".gocache",
        "__pycache__",
        ".pytest_cache",
        ".harbor-result.json",
    }
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
            if character == "#" and not (
                index + 1 < length and text[index + 1] == "["
            ):
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

    files = {}
    for relative, path in sorted(_regular_files(root).items()):
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        files[relative] = {
            "sha256": _digest(path),
            "normalized_sha256": _normalized_digest(text),
            "assertions": _assertion_signature(text),
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


def _public_contract_evidence(
    observations: Sequence[Mapping[str, Any]],
    surfaces: object,
) -> dict[str, object]:
    """Map declared public surfaces to independent CONTRACT observations.

    The public contract is audited only through its own ``CONTRACT``
    observations.  A declared surface that binds anything else (legacy P2P/F2P
    aliases) or a missing CONTRACT row is ``INCONCLUSIVE``: the evaluator cannot
    prove the contract without the independent probe.
    """

    if not isinstance(surfaces, Sequence) or isinstance(surfaces, (str, bytes)):
        return _atom(
            "NA",
            source="public-contract-surfaces",
            evidence={"reason": "no declared public surface"},
        )
    if not [row for row in surfaces if isinstance(row, Mapping)]:
        return _atom(
            "NA",
            source="public-contract-surfaces",
            evidence={"reason": "no declared public surface"},
        )
    by_id = {
        str(row.get("id")): row
        for row in observations
        if isinstance(row, Mapping) and row.get("id")
    }
    contract_ids = {
        identifier
        for identifier, row in by_id.items()
        if str(row.get("kind")) == "CONTRACT"
    }
    selected: list[Mapping[str, Any]] = []
    missing: list[str] = []
    bound_ids: list[str] = []
    for surface in surfaces:
        if not isinstance(surface, Mapping):
            continue
        for identifier in surface.get("observation_ids", []) or []:
            text = str(identifier)
            if text not in bound_ids:
                bound_ids.append(text)
    for identifier in bound_ids:
        if identifier not in contract_ids:
            missing.append(identifier)
            continue
        if all(str(row.get("id")) != identifier for row in selected):
            selected.append(by_id[identifier])
    if not contract_ids:
        return _atom(
            "INCONCLUSIVE",
            source="public-contract-surfaces",
            evidence={
                "reason": "no independent CONTRACT observation for declared public surfaces",
                "missing_observation_ids": sorted(by_id),
            },
        )
    if not selected and not missing:
        return _atom(
            "NA",
            source="public-contract-surfaces",
            evidence={"reason": "declared public surfaces have no observations"},
        )
    if missing:
        return _atom(
            "INCONCLUSIVE",
            source="public-contract-surfaces",
            evidence={
                "binding": "contract",
                "missing_observation_ids": sorted(missing),
            },
        )
    result = _group_status(
        selected,
        expected=len(selected),
        source="public-contract-observations",
        empty="NA",
    )
    evidence = dict(result.get("evidence", {})) if isinstance(result.get("evidence"), Mapping) else {}
    evidence["binding"] = "contract"
    result["evidence"] = evidence
    return result


_CRITICAL_INVARIANTS = (
    "record_count",
    "stable_ids",
    "foreign_key_relations",
    "timestamp_precision",
    "permission_boundaries",
    "error_contract",
)


def _invariant_status(value: object) -> str:
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    if isinstance(value, Mapping):
        status = value.get("status")
        if isinstance(status, str) and status.upper() in {"PASS", "FAIL", "NA", "INCONCLUSIVE"}:
            return status.upper()
        for key in ("pass", "passed", "ok"):
            if key in value:
                return "PASS" if value[key] is True else "FAIL"
    if isinstance(value, str) and value.upper() in {"PASS", "FAIL", "NA"}:
        return value.upper()
    return "INCONCLUSIVE"


def _critical_data_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected: int,
    applicable: bool,
) -> dict[str, object]:
    if not applicable:
        return _atom(
            "NA",
            source="critical-data-invariants",
            evidence={"reason": "persistent state is not applicable"},
        )
    if not rows:
        return _atom(
            "INCONCLUSIVE",
            source="critical-data-invariants",
            evidence={"reason": "no critical-data observations"},
        )
    statuses: list[str] = []
    details: dict[str, list[str]] = {key: [] for key in _CRITICAL_INVARIANTS}
    structured = 0
    legacy = 0
    for row in rows:
        value = row.get("value")
        payload = value.get("value") if isinstance(value, Mapping) and value.get("type") == "json" else None
        invariants = payload.get("invariants") if isinstance(payload, Mapping) else None
        if not isinstance(invariants, Mapping):
            if isinstance(payload, Mapping) and any(key in payload for key in _CRITICAL_INVARIANTS):
                invariants = payload
            else:
                legacy += 1
                statuses.append(str(row.get("status", "INCONCLUSIVE")))
                continue
        structured += 1
        for key in _CRITICAL_INVARIANTS:
            if key not in invariants:
                status = "INCONCLUSIVE"
            else:
                status = _invariant_status(invariants[key])
            details[key].append(status)
            statuses.append(status)
    overall = (
        "INFRA_ERROR" if "INFRA_ERROR" in statuses
        else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses
        else "FAIL" if "FAIL" in statuses
        else "NA" if statuses and all(item == "NA" for item in statuses)
        else "PASS"
    )
    return _atom(
        overall,
        source="critical-data-invariants",
        evidence={
            "expected_observations": expected,
            "observed_observations": len(rows),
            "structured_observations": structured,
            "legacy_observations": legacy,
            "invariants": details,
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
    allowed = {
        str(item).replace("\\", "/").strip("/")
        for item in config.get("policy", {}).get("allowed_equivalent_test_paths", [])
    } if isinstance(config.get("policy"), Mapping) else set()
    changed: list[str] = []
    for relative, record in baseline_files.items():
        if relative not in current or not isinstance(record, Mapping):
            continue
        if relative in allowed:
            continue
        path = current[relative]
        if record.get("sha256") == _digest(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        if record.get("normalized_sha256") and record.get("normalized_sha256") == _normalized_digest(text):
            continue
        # A matching assertion signature is NOT sufficient for "unchanged":
        # inserting an early ``return``/short-circuit leaves every assertion
        # line byte-identical while removing their execution (Constraint 6/7).
        # Only comment/whitespace-equivalent rewrites stay accepted above.
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
    detector_hits = [
        *detect_source_runtime_subprocess(candidate_root, config),
        *detect_source_runtime_ffi(candidate_root, config),
        *detect_source_runtime_dynamic_load(candidate_root, config),
        *detect_raw_process_launch(candidate_root, config),
    ]
    if detector_hits:
        deceptive_hits = [*deceptive_hits, *detector_hits]
    public_contract = _public_contract_evidence(
        rows, policy.get("public_surface_evidence", [])
    )
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
    source_dependency_hits = detect_source_runtime_dependency(candidate_root, config)
    if source_dependency_hits:
        dependency_evidence = _atom(
            "FAIL",
            source="source-runtime-dependency",
            evidence=source_dependency_hits,
        )
    public = _group_status(
        p2p,
        expected=len(expected_tests.get("p2p", [])) if isinstance(expected_tests.get("p2p", []), Sequence) else None,
        source="candidate-observations",
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
        else _critical_data_evidence(
            critical_rows,
            expected=expected_critical,
            applicable=state_applicable,
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
        "public_contract_preserved": public_contract,
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
