"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .detectors import (
        detect_source_runtime_dynamic_load,
        detect_source_runtime_ffi,
        detect_source_runtime_subprocess,
    )
    from .residue import classify_residue_rules, scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from detectors import (
        detect_source_runtime_dynamic_load,
        detect_source_runtime_ffi,
        detect_source_runtime_subprocess,
    )
    from residue import classify_residue_rules, scan_residue_rules


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*"
    r"|[^/]*_(?:test|spec)\.[^/]+|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)
# ``skip``/``xfail``/``disabled`` assignment is a test-runner idiom in
# JS/Python config; in Go/Rust source it is ordinary code (``for _, e := range
# ... { skip = true }``), so it is only counted outside Go.
_SKIP_MARKERS = (
    re.compile(r"pytest\.mark\.skip(?:if)?", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.skip\s*\(", re.IGNORECASE),
    re.compile(r"\bt\.Skip(?:Now|f)?\s*\(", re.IGNORECASE),
    re.compile(r"#\[ignore\]", re.IGNORECASE),
    re.compile(r"@(?:Disabled|Ignore)\b", re.IGNORECASE),
)
_XFAIL_MARKERS = (
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
    re.compile(r"\b(?:test|it|describe)\.todo\s*\(", re.IGNORECASE),
)
_ASSIGN_SKIP_MARKERS = (
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
_DISABLED_MARKERS = (
    re.compile(r"(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE),
    _DISABLED_ALIAS,
)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    # Go only ever names a test file ``*_test.go``.  Without this guard a
    # production file such as ``internal/gittest/gittest.go`` matches the
    # generic ``...Test...`` pattern and is treated as a test, so ordinary
    # ``skip = true`` assignments are miscounted as disabled tests.
    if name.casefold().endswith(".go"):
        return name.casefold().endswith("_test.go")
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
            if character == "#" and not text.startswith("#[", index):
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


def _marker_counts(root: Path, roots: Sequence[str] | None = None) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        if roots is not None and not any(
            relative == item.rstrip("/") or relative.startswith(item.rstrip("/") + "/")
            for item in roots
        ):
            continue
        if not _is_test(relative) and roots is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _strip_comments(text)
        is_go = Path(relative).suffix.casefold() == ".go"
        counts["skip"] += sum(
            1 for pattern in _SKIP_MARKERS for _ in pattern.finditer(text)
        )
        counts["xfail"] += sum(
            1 for pattern in _XFAIL_MARKERS for _ in pattern.finditer(text)
        )
        counts["disabled"] += sum(
            1 for pattern in _DISABLED_MARKERS for _ in pattern.finditer(text)
        )
        if not is_go:
            counts["disabled"] += sum(
                1 for pattern in _ASSIGN_SKIP_MARKERS for _ in pattern.finditer(text)
            )
    return counts


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


# The declared equivalence tolerance for protected tests is *alpha-equivalence*:
# two versions are equivalent when their token streams agree after replacing
# every identifier with a placeholder and every literal with a placeholder.
# This lets a candidate rename locals or restructure literals (a legitimate,
# behaviour-preserving rewrite) while still flagging a changed control-flow
# condition, a removed operator, or a weakened comparison -- changes that add,
# drop, or reorder structural tokens.
_STRUCTURE_TOKEN = re.compile(
    r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`[^`]*`"
    r"|0[xX][0-9a-fA-F]+|\d+\.\d+|\d+"
    r"|[A-Za-z_][A-Za-z0-9_]*"
    r"|[^\s]",
    re.S,
)


def _structure_signature(text: str) -> str:
    text = _strip_comments_keep_strings(text)
    tokens: list[str] = []
    for match in _STRUCTURE_TOKEN.finditer(text):
        token = match.group(0)
        head = token[0]
        if head in ("'", '"', "`"):
            tokens.append("STR")
        elif head.isdigit():
            tokens.append("NUM")
        elif head.isalpha() or head == "_":
            tokens.append("ID")
        else:
            tokens.append(token)
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()


_IF_GUARD = re.compile(r"^(?:\}\s*)?(?:else\s+)?if\b(.*)$", re.S)


def _brace_depths(text: str) -> list[int]:
    """Per-character net brace depth, ignoring braces inside strings/comments."""

    depths = [0] * len(text)
    depth = 0
    state = "normal"
    quote = ""
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if state == "normal":
            if character == "/" and index + 1 < length and text[index + 1] == "/":
                state = "line"
                index += 1
            elif character == "/" and index + 1 < length and text[index + 1] == "*":
                state = "block"
                index += 1
            elif character in ("'", '"', "`"):
                state = "string"
                quote = character
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
        elif state == "line":
            if character == "\n":
                state = "normal"
        elif state == "block":
            if character == "*" and index + 1 < length and text[index + 1] == "/":
                state = "normal"
                index += 1
        else:  # string
            if quote != "`" and character == "\\":
                index += 1
            elif character == quote:
                state = "normal"
        depths[index] = depth
        index += 1
    return depths


def _guard_chains(text: str) -> list[tuple[str, ...]]:
    """Return each line's open ``if`` guard chain (normalized conditions).

    An assertion that is guarded by ``if cond`` is weaker than an unguarded one;
    capturing the whole open guard chain makes a *new* enclosing ``if false``
    wrapper (a dead assertion) change the atom, not just a rewritten condition.
    """

    lines = text.splitlines()
    if not lines:
        return []
    depths = _brace_depths(text)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line) + 1
    chains: list[tuple[str, ...]] = []
    stack: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        start = starts[index]
        depth_before = depths[start - 1] if start > 0 else 0
        while stack and stack[-1][0] > depth_before:
            stack.pop()
        stripped = line.strip()
        own = _IF_GUARD.match(stripped)
        own_condition = ""
        if own is not None:
            condition = own.group(1).strip().rstrip("{").strip()
            own_condition = re.sub(r"\s+", "", condition)
        chain = [guard for _, guard in stack]
        if own_condition:
            chain.append(own_condition)
        chains.append(tuple(chain))
        end = start + len(line)
        depth_after = depths[end - 1] if end > start else depth_before
        if own is not None and depth_after > depth_before and own_condition:
            stack.append((depth_after, own_condition))
    return chains


def _assertion_atoms(text: str) -> list[str]:
    """Return the file's assertion atoms: ``<guard chain>\\x01<assertion line>``."""

    clean = _strip_comments_keep_strings(text)
    lines = clean.splitlines()
    chains = _guard_chains(clean)
    atoms: list[str] = []
    for index, line in enumerate(lines):
        if not _ASSERTION_TOKENS.search(line):
            continue
        guard = "&&".join(chains[index]) if index < len(chains) else ""
        atoms.append(guard + "\x01" + re.sub(r"\s+", "", line))
    return sorted(atoms)


def _line_digests(text: str) -> list[str]:
    """Return the ordered digests of a file's normalized code lines."""

    digests: list[str] = []
    for line in _strip_comments_keep_strings(text).splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            digests.append(hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16])
    return digests


def build_source_test_baseline(root: Path) -> dict[str, object]:
    """Freeze source-visible test identities and marker counts for a Case."""

    files: dict[str, object] = {}
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
            "structure": _structure_signature(text),
            "assertion_atoms": _assertion_atoms(text),
            "line_digests": _line_digests(text),
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
        if row.get("kind") == "target-build"
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


def _assertions_preserved(baseline_atoms: Sequence[str], current_atoms: Sequence[str]) -> bool:
    """True when every reference assertion atom survives in the candidate."""

    if not baseline_atoms:
        return True
    available = list(current_atoms)
    for atom in baseline_atoms:
        try:
            available.remove(atom)
        except ValueError:
            return False
    return True


# A candidate file is treated as a *derivative* of a protected reference test
# when it is a reworked copy of that file rather than an independently authored
# test that merely occupies the same path (``instruction`` explicitly permits a
# candidate to add its own Go tests).  Derivation is measured with two symmetric
# containment ratios of the normalized code lines:
#
#   recall    = |reference ∩ candidate| / |reference|
#   precision = |reference ∩ candidate| / |candidate|
#
# Both directions are required.  A "gutted copy" rewrite -- keep the reference
# scaffolding, delete the assertion blocks -- removes most reference lines, so
# recall collapses, but the few lines that remain are almost the entire (now
# tiny) candidate file, so precision stays high.  A candidate that deletes whole
# test cases from the reference, or keeps reference content and pads it with
# unrelated filler, still *contains* a substantial minority of the reference.
# Testing only the majority-recall direction let the "whole rewrite" family be
# misclassified as an independent candidate file and its deleted assertions go
# unnoticed.
#
# A file is therefore reference-derived when EITHER
#   precision >= 0.5   (the file is mostly a copy of the reference), OR
#   recall    >= 0.25  (it still contains a substantial part of the reference),
# and only then are dropped/weakened reference assertions flagged as tampering.
_DERIVATIVE_PRECISION = 0.5
_DERIVATIVE_RECALL = 0.25


def _derivative_containment(
    baseline_lines: Sequence[str], text: str
) -> tuple[float, float]:
    """Return ``(recall, precision)`` of the reference lines in ``text``."""

    baseline = {str(item) for item in baseline_lines if item}
    if not baseline:
        return 1.0, 1.0
    current = set(_line_digests(text))
    if not current:
        return 0.0, 0.0
    shared = len(baseline & current)
    return shared / len(baseline), shared / len(current)


def _is_derivative(baseline_lines: Sequence[str], text: str) -> bool:
    recall, precision = _derivative_containment(baseline_lines, text)
    return precision >= _DERIVATIVE_PRECISION or recall >= _DERIVATIVE_RECALL


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
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_roots = config.get("policy", {}).get("protected_test_marker_roots")
    if marker_roots is not None:
        # Original protected assets are also compared byte-for-byte by P2P.
        # New candidate-authored tests can have conditional optional probes.
        marker_before = baseline.get("protected_marker_counts", {})
    marker_after = _marker_counts(root, marker_roots)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={"before": dict(marker_before), "after": marker_after, "delta": marker_delta},
    )
    if not baseline_files:
        # A pass here would be vacuous: there is no protected test file to
        # compare against.  Keep the evidence honest without turning a
        # repository with no pre-existing tests into a hard failure, but still
        # report the skip/xfail marker delta computed above.
        empty = _atom(
            "NA",
            source=source,
            evidence={"reason": "baseline-has-no-test-files", "files": 0},
        )
        return empty, skip
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
    derivative_metrics: dict[str, dict[str, object]] = {}
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
        # Comment-only and whitespace-only edits are equivalent; any real code
        # or assertion change (including a weakened condition) counts.  The
        # comment-stripped normalized digest detects exactly that boundary.
        if record.get("normalized_sha256") and record.get("normalized_sha256") == _normalized_digest(text):
            continue
        baseline_atoms = record.get("assertion_atoms")
        current_atoms = _assertion_atoms(text)
        # Behaviour-preserving rewrites that keep the structural token stream
        # (renaming locals, reformatting literals) are not tampering.  The
        # tolerance must NOT hide a reduced assertion strength, though: the
        # structural tokenizer collapses every call identifier to ``ID``, so a
        # downgrade such as ``t.Fatalf`` -> ``t.Logf`` (a failing assertion turned
        # into a passing log) leaves the structure unchanged.  The structural
        # shortcut is therefore only allowed while the candidate still carries at
        # least as many assertion atoms as the reference.
        structure_matches = bool(
            record.get("structure") and record.get("structure") == _structure_signature(text)
        )
        assertions_not_reduced = (
            not isinstance(baseline_atoms, list)
            or len(current_atoms) >= len(baseline_atoms)
        )
        if structure_matches and assertions_not_reduced:
            continue
        # Tolerance for genuine structural refactors (table-driven tests,
        # ``t.Run`` subtests, extracted helpers): the reference's *protected
        # assertion strength* must survive.  A file that keeps every reference
        # assertion atom (guard chain + assertion statement) is equivalent even
        # when its control flow differs.
        if isinstance(baseline_atoms, list):
            if _assertions_preserved(baseline_atoms, current_atoms):
                continue
            # Some reference assertion atom is missing or weakened.  Only treat
            # that as tampering when the candidate file is a *derivative* of the
            # reference (it is a reworked copy, measured by symmetric line
            # containment).  A candidate that lays out its own tests under the
            # same path shares little of the reference and is not tampering.
            baseline_lines = record.get("line_digests")
            if isinstance(baseline_lines, list):
                recall, precision = _derivative_containment(baseline_lines, text)
            else:
                recall, precision = 0.0, 0.0
            derived = isinstance(baseline_lines, list) and _is_derivative(baseline_lines, text)
            derivative_metrics[str(relative)] = {
                "recall": round(recall, 4),
                "precision": round(precision, 4),
                "derived": derived,
            }
            if derived:
                changed.append(str(relative))
            continue
        changed.append(str(relative))
    changed = sorted(changed)
    # A baseline file that is simply absent is not tampering: the candidate is
    # allowed to lay out its own tests.  Only a present file whose assertions
    # were rewritten or weakened is a test-integrity failure.
    tamper = _atom(
        "FAIL" if changed else "PASS",
        source=source,
        evidence={"missing": missing, "changed": changed, "derivative": derivative_metrics},
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
    # A declared literal rule is path-bound and easy to evade by moving or
    # obfuscating the call.  Add a path-independent scan for the migrated
    # responsibilities being delegated back to the source runtime (subprocess,
    # FFI, dynamic load), matching the fail-closed L2 contract.
    detector_hits = [
        *detect_source_runtime_subprocess(candidate_root, config),
        *detect_source_runtime_ffi(candidate_root, config),
        *detect_source_runtime_dynamic_load(candidate_root, config),
    ]
    if detector_hits:
        deceptive_hits = [*deceptive_hits, *detector_hits]
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
