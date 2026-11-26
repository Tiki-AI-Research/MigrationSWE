"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .residue import classify_residue_rules, scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from residue import classify_residue_rules, scan_residue_rules


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*"
    r"|[^/]*_(?:test|spec)\.[^/]+|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)
_SKIP_MARKERS = (
    re.compile(r"pytest\.mark\.skip", re.IGNORECASE),
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.skip\s*\(", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.todo\s*\(", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.failing\s*\(", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    re.compile(r"\.concurrent\.skip\s*\(", re.IGNORECASE),
    re.compile(r"\.skipIf\s*\(", re.IGNORECASE),
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


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return "test" in {part.casefold() for part in parts[:-1]} or bool(
        _TEST_NAME.search(path)
    )


def _regular_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        return {}
    values: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if ".git" in Path(relative).parts or "node_modules" in Path(relative).parts:
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


_ASSERTION_CALL = re.compile(r"\bexpect\s*\(")
_TRIVIAL_ASSERTION = re.compile(
    r"\bexpect\s*\(\s*(?:true|false|null|undefined|-?\d+(?:\.\d+)?|['\"][^'\"]*['\"])\s*\)"
)
_WEAK_MATCHER = re.compile(r"\.toBeDefined\s*\(")
_VALUE_MATCHER = re.compile(r"\.(?:toEqual|toStrictEqual|toBe|toMatchObject)\s*\(")
_OBJECT_MATCHER = re.compile(r"\.toMatchObject\s*\(")
_LOOSE_MATCHER = re.compile(
    r"\.toBe(?:GreaterThan|GreaterThanOrEqual|LessThan|LessThanOrEqual|CloseTo)\s*\("
)
_UNCONDITIONAL_RETURN = re.compile(r"(?m)^\s*return\s*;\s*$")
_SHORT_CIRCUIT_ASSERTION = re.compile(r"&&\s*expect\s*\(")
_DEAD_BRANCH = re.compile(r"if\s*\(\s*false\s*\)")


def _unreachable_return_count(code: str) -> int:
    returns = [match.start() for match in _UNCONDITIONAL_RETURN.finditer(code)]
    assertions = [match.start() for match in _ASSERTION_CALL.finditer(code)]
    return sum(any(position < assertion for assertion in assertions) for position in returns)


_ASSERTION_MATCHER = re.compile(
    r"\.(toBe|toEqual|toStrictEqual|toMatchObject|toContain|toHaveLength|"
    r"toHaveProperty|toHaveBeenCalledWith|toHaveBeenCalled|toBeDefined|"
    r"toBeTruthy|not\.toBeNull)\s*\("
)


def _assertion_category(actual: str) -> str:
    folded = actual.casefold()
    if "status" in folded:
        return "status"
    if (
        "json" in folded
        or "body" in folded
        or "data" in folded
        or re.search(r"\.(?:text|arraybuffer|blob)\s*\(", folded)
    ):
        return "body"
    if "header" in folded:
        return "headers"
    if "calls" in folded:
        return "calls"
    if "dependenc" in folded or "package" in folded:
        return "package"
    return "expr"


def _signature_value(value: str) -> str:
    return re.sub(r"\s+", "", value).replace('"', "'")


def _assertion_signature_counts(text: str) -> dict[str, int]:
    # Assertions are expressions, not lines. Harness adaptation frequently
    # wraps expect(...) or its matcher arguments across several lines.
    # Balance calls on a comment/string-masked copy, retaining the original
    # argument text for signatures and counting each assertion separately.
    code = _strip_comments(text)

    def call_end(opening: int) -> int | None:
        depth = 0
        for position in range(opening, len(code)):
            if code[position] == "(" and (position == 0 or code[position - 1] != "\\"):
                depth += 1
            elif code[position] == ")" and (position == 0 or code[position - 1] != "\\"):
                depth -= 1
                if depth == 0:
                    return position
        return None

    counts: dict[str, int] = {}
    for assertion in _ASSERTION_CALL.finditer(code):
        opening = code.find("(", assertion.start(), assertion.end())
        closing = call_end(opening)
        if closing is None:
            continue
        tail = closing + 1
        while tail < len(code) and code[tail].isspace():
            tail += 1
        match = _ASSERTION_MATCHER.match(code, tail)
        if match is None:
            continue
        expected_end = call_end(match.end() - 1)
        if expected_end is None:
            continue
        actual = text[opening + 1:closing].strip().rstrip(",").strip()
        matcher = match.group(1)
        if matcher in {"toBe", "toEqual", "toStrictEqual"}:
            matcher = "eq"
        elif matcher == "toMatchObject":
            matcher = "match-object"
        expected = text[match.end():expected_end].strip().rstrip(",").strip()
        signature = f"{_assertion_category(actual)}:{matcher}:{_signature_value(expected)}"
        counts[signature] = counts.get(signature, 0) + 1
    return counts


def _assertion_signatures(text: str) -> list[str]:
    return sorted(_assertion_signature_counts(text))


def _assertion_metrics(text: str) -> dict[str, int]:
    code = _strip_comments(text)
    return {
        "total": len(_ASSERTION_CALL.findall(code)),
        "trivial": len(_TRIVIAL_ASSERTION.findall(code)),
        "weak_matcher": len(_WEAK_MATCHER.findall(code)),
        "value_matcher": len(_VALUE_MATCHER.findall(code)),
        "object_matcher": len(_OBJECT_MATCHER.findall(code)),
        "loose_matcher": len(_LOOSE_MATCHER.findall(code)),
        "unreachable_return": _unreachable_return_count(code),
        "short_circuit": len(_SHORT_CIRCUIT_ASSERTION.findall(code))
        + len(_DEAD_BRANCH.findall(code)),
    }


def _assertion_weakening(
    before: Mapping[str, Any] | None, after: Mapping[str, int]
) -> list[str]:
    if not isinstance(before, Mapping):
        return []
    issues: list[str] = []
    base_total = int(before.get("total", 0))
    base_trivial = int(before.get("trivial", 0))
    base_weak = int(before.get("weak_matcher", 0))
    base_value = int(before.get("value_matcher", 0))
    if after["trivial"] > base_trivial and (
        after["total"] <= base_total or after["value_matcher"] < base_value
    ):
        issues.append("trivial-assertion")
    if after["weak_matcher"] > base_weak and after["value_matcher"] < base_value:
        issues.append("weakened-matcher")
    if after["loose_matcher"] > int(before.get("loose_matcher", 0)) and (
        after["value_matcher"] < base_value or after["total"] < base_total
    ):
        issues.append("weakened-range-matcher")
    if after["object_matcher"] > int(before.get("object_matcher", 0)):
        issues.append("weakened-object-matcher")
    if after["unreachable_return"] > int(before.get("unreachable_return", 0)):
        issues.append("unreachable-assertion")
    if after["short_circuit"] > int(before.get("short_circuit", 0)):
        issues.append("short-circuit-assertion")
    return issues


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

    files = {
        relative: {"sha256": _digest(path)}
        for relative, path in sorted(_regular_files(root).items())
        if _is_test(relative)
    }
    assertions = {}
    signatures = {}
    signature_counts = {}
    for relative, path in sorted(_regular_files(root).items()):
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        assertions[relative] = _assertion_metrics(text)
        counts = _assertion_signature_counts(text)
        signature_counts[relative] = counts
        signatures[relative] = sorted(counts)
    return {
        "schema_version": "migration-source-test-baseline:v1",
        "files": files,
        "marker_counts": _marker_counts(root),
        "assertion_counts": assertions,
        "assertion_signatures": signatures,
        "assertion_signature_counts": signature_counts,
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


def _exact_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", value) else None


def _dependency_scan(
    root: Path,
    *,
    residual_dependencies: Sequence[str] = (),
) -> dict[str, object]:
    package_path = root / "package.json"
    lock_path = root / "package-lock.json"
    if package_path.is_symlink() or not package_path.is_file():
        return {"status": "NA", "failures": []}
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "INCONCLUSIVE", "failures": [{"reason": "invalid-package-json"}]}
    dependencies = package.get("dependencies") if isinstance(package, Mapping) else None
    if not isinstance(dependencies, Mapping):
        dependencies = {}
    failures: list[dict[str, str]] = []
    wanted = tuple(str(name).casefold() for name in residual_dependencies if str(name).strip())
    if wanted and isinstance(package, Mapping):
        for group in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        ):
            entries = package.get(group)
            if not isinstance(entries, Mapping):
                continue
            for name, spec in entries.items():
                folded = str(name).casefold()
                spec_text = str(spec)
                if any(
                    folded == item
                    or folded.startswith(item + "/")
                    or f"npm:{item}@" in spec_text
                    for item in wanted
                ):
                    failures.append(
                        {
                            "dependency": str(name),
                            "group": group,
                            "reason": "residual-source-dependency",
                        }
                    )
    if lock_path.is_symlink() or not lock_path.is_file():
        return {"status": "INCONCLUSIVE", "failures": [{"reason": "package-lock-missing"}]}
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "INCONCLUSIVE", "failures": [{"reason": "invalid-package-lock"}]}
    packages = lock.get("packages") if isinstance(lock, Mapping) else None
    packages = packages if isinstance(packages, Mapping) else {}
    checked = 0
    for name, declared in dependencies.items():
        exact = _exact_version(declared)
        if exact is None:
            continue
        checked += 1
        record = packages.get(f"node_modules/{name}")
        if not isinstance(record, Mapping):
            failures.append({"dependency": str(name), "reason": "missing-lock-entry"})
            continue
        version = record.get("version")
        if version != exact:
            failures.append(
                {
                    "dependency": str(name),
                    "declared": exact,
                    "locked": str(version),
                    "reason": "lock-version-mismatch",
                }
            )
    return {
        "status": "FAIL" if failures else "PASS",
        "checked": checked,
        "failures": failures,
    }


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
    changed = sorted(
        relative for relative, record in baseline_files.items()
        if relative in current
        and isinstance(record, Mapping)
        and record.get("sha256") != _digest(current[relative])
        and relative not in allowed
    )
    baseline_assertions = baseline.get("assertion_counts", {})
    baseline_assertions = (
        baseline_assertions if isinstance(baseline_assertions, Mapping) else {}
    )
    baseline_signatures = baseline.get("assertion_signatures", {})
    baseline_signatures = (
        baseline_signatures if isinstance(baseline_signatures, Mapping) else {}
    )
    baseline_signature_counts = baseline.get("assertion_signature_counts", {})
    baseline_signature_counts = (
        baseline_signature_counts
        if isinstance(baseline_signature_counts, Mapping)
        else {}
    )
    weakened: list[dict[str, object]] = []
    for relative in sorted(allowed):
        record = baseline_files.get(relative)
        if relative not in current or not isinstance(record, Mapping):
            continue
        if record.get("sha256") == _digest(current[relative]):
            continue
        try:
            text = current[relative].read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        after = _assertion_metrics(text)
        issues = _assertion_weakening(baseline_assertions.get(relative), after)
        expected_signature_counts = baseline_signature_counts.get(relative)
        expected_signatures = baseline_signatures.get(relative)
        missing_signatures: list[str] = []
        if isinstance(expected_signature_counts, Mapping):
            # Position-level comparison: a signature may appear several times in
            # the same file, so the candidate must keep at least the declared
            # multiplicity instead of merely any one occurrence.
            candidate_counts = _assertion_signature_counts(text)
            # Some permitted harness changes replace an internal success
            # return assertion with its HTTP-client equivalent. Consume only
            # surplus alternative assertions, so one assertion cannot satisfy
            # two baseline obligations. Other signatures remain mandatory.
            policy = config.get("policy", {})
            alternatives = policy.get("equivalent_assertion_signatures", {}).get(relative, {})
            surplus = {
                signature: max(0, count - int(expected_signature_counts.get(signature, 0)))
                for signature, count in candidate_counts.items()
            }
            for signature, equivalents in alternatives.items():
                deficit = max(0, int(expected_signature_counts.get(signature, 0)) - candidate_counts.get(signature, 0))
                for equivalent in equivalents:
                    used = min(deficit, surplus.get(equivalent, 0))
                    candidate_counts[signature] = candidate_counts.get(signature, 0) + used
                    surplus[equivalent] = surplus.get(equivalent, 0) - used
                    deficit -= used
            missing_signatures = sorted(
                str(signature)
                for signature, required in expected_signature_counts.items()
                if int(candidate_counts.get(str(signature), 0)) < int(required)
            )
            if missing_signatures:
                issues.append("missing-key-assertion")
        elif isinstance(expected_signatures, Sequence) and not isinstance(
            expected_signatures, (str, bytes)
        ):
            missing_signatures = sorted(
                set(str(item) for item in expected_signatures)
                - set(_assertion_signatures(text))
            )
            if missing_signatures:
                issues.append("missing-key-assertion")
        elif isinstance(baseline_assertions.get(relative), Mapping):
            expected_count = int(baseline_assertions[relative].get("total", 0))
            if expected_count > 0 and after["total"] < expected_count:
                issues.append("assertion-count-drop")
        if issues:
            weakened.append(
                {
                    "path": relative,
                    "issues": issues,
                    "missing": missing_signatures,
                    "before": baseline_assertions.get(relative),
                    "after": after,
                }
            )
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = _marker_counts(root)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if missing or changed or weakened else "PASS",
        source=source,
        evidence={
            "missing": missing,
            "changed": changed,
            "weakened": weakened,
        },
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
    differential_rows = [row for row in rows if row.get("kind") == "DIFFERENTIAL"]
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
    residual_names: list[str] = []
    technology = config.get("source_technology")
    if isinstance(technology, Mapping):
        source_name = technology.get("name")
        if isinstance(source_name, str) and source_name.strip():
            residual_names.append(source_name.strip())
    declared_residual = policy.get(
        "residual_source_dependencies", config.get("residual_source_dependencies", [])
    )
    if isinstance(declared_residual, Sequence) and not isinstance(
        declared_residual, (str, bytes)
    ):
        residual_names.extend(str(item) for item in declared_residual if str(item).strip())
    dependency_scan = _dependency_scan(
        candidate_root,
        residual_dependencies=tuple(dict.fromkeys(residual_names)),
    )
    if dependency_scan.get("status") == "FAIL":
        dependency_evidence = _atom(
            "FAIL", source="dependency-lock", evidence=dependency_scan
        )
    elif dependency_scan.get("status") == "PASS" and dependency_evidence["status"] in {
        "INCONCLUSIVE",
        "NA",
    }:
        dependency_evidence = _atom(
            "PASS", source="dependency-lock", evidence=dependency_scan
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
    differential_preserved = _group_status(
        differential_rows,
        expected=len(expected_tests.get("differential", []))
        if isinstance(expected_tests.get("differential", []), Sequence)
        else None,
        source="candidate-differential-observations",
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
        "differential_preserved": differential_preserved,
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
        "dependency_scan": dependency_scan,
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
