"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .detectors import (
        detect_source_runtime_asset_fallback,
        detect_source_runtime_dependency,
        detect_source_runtime_dynamic_load,
        detect_source_runtime_ffi,
        detect_source_runtime_subprocess,
    )
    from .residue import classify_residue_rules, scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from detectors import (
        detect_source_runtime_asset_fallback,
        detect_source_runtime_dependency,
        detect_source_runtime_dynamic_load,
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

# A skipped Go test is not always written as the literal ``t.Skip(``.  The
# testing handle can be rebound to an alias (``skipAlias := t; skipAlias.Skip``),
# the method borrowed as a value (``f := t.Skip; f("...")``), or the *package*
# renamed (``import tst "testing"`` then ``func TestX(tb *tst.T)``).  A regex
# that only matches ``t.Skip(`` and ``testing.T`` misses the whole family;
# resolve the ``testing`` package's local qualifier from the file's own imports
# (default, explicit alias or dot import) and then resolve the ``*testing.T/B/F``
# receivers and their local aliases, counting ``Skip`` / ``Skipf`` / ``SkipNow``
# calls *and* method-value borrows on them.
_GO_ALIAS_ASSIGN = re.compile(
    r"\b([A-Za-z_$][\w$]*)\s*:?=\s*([A-Za-z_$][\w$]*)\b"
)
# The canonical ``t.Skip(`` receiver is already counted by ``_SKIP_MARKERS``;
# excluding only that literal call form keeps the totals free of double counting.
# ``b``/``f`` are *not* excluded: the literal marker only recognises ``t.``, so a
# ``b.Skip``/``f.Skip`` call (or an alias that happens to be named ``b``) must be
# counted here.
_LITERAL_SKIP_RECEIVER = "t"
_GO_IMPORT_LITERAL = r'("(?:[^"\\]|\\.)*"|`[^`]*`)'


def _go_import_specs(text: str) -> list[tuple[str, str]]:
    """``(local_name, import_path)`` for every Go import spec.

    Mirrors the detector helper: explicit alias, ``"."`` for a dot import,
    ``"_"`` for a blank import or ``""`` when the package's declared name is
    used.  Both interpreted and raw string literals are recognised.
    """

    specs: list[tuple[str, str]] = []
    single = re.compile(
        r"(?m)^\s*import\s+(?:([A-Za-z_][A-Za-z0-9_]*|\.|_)\s+)?"
        + _GO_IMPORT_LITERAL
    )
    for match in single.finditer(text):
        specs.append((match.group(1) or "", _go_unquote(match.group(2))))
    block = re.compile(r"(?ms)^\s*import\s*\((.*?)\)")
    spec = re.compile(
        r"^(?:([A-Za-z_][A-Za-z0-9_]*|\.|_)\s+)?" + _GO_IMPORT_LITERAL + r"$"
    )
    for group in block.finditer(text):
        for raw in re.split(r"[\n;]", group.group(1)):
            candidate = re.sub(r"//.*$", "", raw).strip()
            if not candidate:
                continue
            match = spec.match(candidate)
            if match:
                specs.append((match.group(1) or "", _go_unquote(match.group(2))))
    return specs


def _go_unquote(literal: str) -> str:
    if len(literal) >= 2 and literal[0] in "\"`":
        return literal[1:-1]
    return literal


def _go_testing_receiver_regex(text: str) -> re.Pattern[str] | None:
    """Regex matching ``*testing.T/B/F/TB`` receivers under every import spelling.

    Returns ``None`` when the file does not import ``testing`` at all, so a
    locally defined ``T`` in a file that merely *mentions* testing-like names is
    never mistaken for a testing handle.  A dot import is the only case in which
    a bare ``T``/``B``/``F``/``TB`` receiver is accepted.
    """

    qualifiers: set[str] = set()
    dotted = False
    imported = False
    for name, path in _go_import_specs(text):
        if path != "testing":
            continue
        imported = True
        if name == ".":
            dotted = True
        elif name == "_":
            continue
        elif name:
            qualifiers.add(name)
        else:
            qualifiers.add("testing")
    if not imported:
        return None
    alternatives = [rf"{re.escape(name)}\.(?:T|B|F|TB)" for name in sorted(qualifiers)]
    if dotted:
        alternatives.append(r"(?:T|B|F|TB)")
    if not alternatives:
        return None
    return re.compile(
        r"\b([A-Za-z_$][\w$]*)\s+\*?(?:" + "|".join(alternatives) + r")\b"
    )


def _go_alias_skip_markers(text: str, import_text: str | None = None) -> int:
    """Count ``Skip``/``Skipf``/``SkipNow`` uses through a testing-handle alias.

    *text* is comment- and string-stripped, so it keeps the receiver/method code
    but loses the import *paths*; *import_text* (comments stripped, strings kept)
    supplies the ``testing`` import so the receiver qualifier can be resolved.
    """

    receiver = _go_testing_receiver_regex(import_text if import_text is not None else text)
    if receiver is None:
        return 0
    aliases = {match.group(1) for match in receiver.finditer(text)}
    if not aliases:
        return 0
    for _ in range(4):
        added = False
        for match in _GO_ALIAS_ASSIGN.finditer(text):
            if match.group(2) in aliases and match.group(1) not in aliases:
                aliases.add(match.group(1))
                added = True
        if not added:
            break
    count = 0
    for name in aliases:
        escaped = re.escape(name)
        call = re.compile(rf"(?<![A-Za-z0-9_]){escaped}\.Skip(?:Now|f)?\s*\(")
        value = re.compile(rf"(?<![A-Za-z0-9_]){escaped}\.Skip(?:Now|f)?\b(?!\s*\()")
        if name != _LITERAL_SKIP_RECEIVER:
            count += len(call.findall(text))
        count += len(value.findall(text))
    return count


# A skip/xfail marker is a *bypass* of a migrated behavioural test only when it
# is not guarded by a conditional.  A faithful port may legitimately add a
# platform/environment guard (``if runtime.GOOS == "windows" { t.Skip(...) }``,
# ``if os.Getenv("AWS_CA_BUNDLE") != "" { t.Skip(...) }``): that test still runs
# on the evaluation platform, so it does not disable anything.  An
# *unconditional* ``t.Skip`` -- the literal form, a testing-handle alias, or a
# package-alias import -- disables the test on every platform and is exactly the
# ``no_new_skip_or_xfail`` bypass the atom exists to catch.  Go requires braces
# around every ``if``/``for``/``switch``/``select``/``else`` body, so a marker is
# guarded precisely when one of its enclosing braces was opened by such a
# keyword.
_CONDITIONAL_KEYWORD = re.compile(r"\b(?:if|for|switch|select|else)\b")


def _conditional_offsets(code: str) -> bytearray:
    """Mark each offset that sits inside a conditional block.

    *code* must be comment- and string-stripped so brace classification sees the
    real control flow.  ``1`` marks an offset enclosed by at least one brace
    frame opened by ``if``/``for``/``switch``/``select``/``else``; ``0`` marks
    function bodies and package scope.
    """

    inside = bytearray(len(code))
    stack: list[bool] = []
    statement_start = 0
    index = 0
    length = len(code)
    while index < length:
        character = code[index]
        if character == "{":
            prefix = code[statement_start:index]
            stack.append(bool(_CONDITIONAL_KEYWORD.search(prefix)))
            statement_start = index + 1
        elif character == "}":
            if stack:
                stack.pop()
            statement_start = index + 1
        elif character == ";" and not _CONDITIONAL_KEYWORD.search(
            code[statement_start:index]
        ):
            # Go permits an initializer before the condition (if init; cond),
            # and for clauses contain semicolons too. Keep the keyword until
            # its opening brace so the body retains its conditional context.
            statement_start = index + 1
        inside[index] = 1 if any(stack) else 0
        index += 1
    return inside


def _go_alias_skip_unguarded(
    text: str, import_text: str | None, guarded: bytearray
) -> int:
    """Count *unguarded* ``Skip``/``Skipf``/``SkipNow`` uses through a handle alias.

    Same receiver/alias resolution as :func:`_go_alias_skip_markers`, but a
    marker that lives inside a conditional guard (``guarded[offset] == 1``) is a
    legitimate platform/environment guard and is not counted.
    """

    receiver = _go_testing_receiver_regex(import_text if import_text is not None else text)
    if receiver is None:
        return 0
    aliases = {match.group(1) for match in receiver.finditer(text)}
    if not aliases:
        return 0
    for _ in range(4):
        added = False
        for match in _GO_ALIAS_ASSIGN.finditer(text):
            if match.group(2) in aliases and match.group(1) not in aliases:
                aliases.add(match.group(1))
                added = True
        if not added:
            break
    count = 0
    for name in aliases:
        escaped = re.escape(name)
        call = re.compile(rf"(?<![A-Za-z0-9_]){escaped}\.Skip(?:Now|f)?\s*\(")
        value = re.compile(rf"(?<![A-Za-z0-9_]){escaped}\.Skip(?:Now|f)?\b(?!\s*\()")
        if name != _LITERAL_SKIP_RECEIVER:
            for match in call.finditer(text):
                if not guarded[match.start()]:
                    count += 1
        for match in value.finditer(text):
            if not guarded[match.start()]:
                count += 1
    return count


def _bypass_marker_counts(root: Path) -> dict[str, int]:
    """Count only *unguarded* skip/xfail/disabled markers in a candidate tree.

    Mirrors :func:`_marker_counts` (same marker families and alias resolution)
    but drops every marker enclosed by a conditional block, so a faithful port's
    platform/environment guard is not mistaken for a newly introduced bypass.
    """

    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        if not _is_test(relative):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        import_text = _strip_comments_keep_strings(raw)
        text = _strip_comments(raw)
        guarded = _conditional_offsets(text)

        def unguarded(pattern: re.Pattern[str]) -> int:
            return sum(1 for match in pattern.finditer(text) if not guarded[match.start()])

        counts["skip"] += unguarded(_SKIP_MARKERS[0])
        counts["xfail"] += unguarded(_SKIP_MARKERS[1])
        for pattern in _SKIP_MARKERS[2:]:
            counts["disabled"] += unguarded(pattern)
        counts["disabled"] += unguarded(_DISABLED_ALIAS)
        counts["disabled"] += _go_alias_skip_unguarded(text, import_text, guarded)
    return counts


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
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        import_text = _strip_comments_keep_strings(raw)
        text = _strip_comments(raw)
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
        counts["disabled"] += _go_alias_skip_markers(text, import_text)
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


def _differential_oracle_status(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected: int | None,
    source: str,
) -> dict[str, object]:
    """Group the DIFFERENTIAL observations as a declared, gating atom.

    ``group_applicability.DIFFERENTIAL`` is ``required``: the differential
    behavioural oracle compares the migrated implementation against the source
    behaviour, so an all-PASS group is the migration-equivalence bottom line
    and any ``FAIL`` must fail the hard gate (reward ``0``, never a silent
    pass).  A probe that cannot apply to the candidate's (undeclared) internal
    structure reports every observation as ``NA``; that is "not applicable",
    not a failure, so it must not be turned into an ``INCONCLUSIVE`` gate
    failure -- doing so would restore the reference-layout hidden contract that
    the applicability fix removed.  Any other non-PASS/FAIL mix keeps the gate
    ``INCONCLUSIVE`` so missing evidence is never silently accepted.
    """

    if expected == 0 or (expected is None and not rows):
        return _atom("NA", source=source, evidence={"total": 0, "checks": []})
    if not rows:
        return _atom("INCONCLUSIVE", source=source, evidence={"total": 0, "checks": []})
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in rows]
    evidence = {
        "passed": sum(item == "PASS" for item in statuses),
        "total": len(statuses),
        "checks": [dict(row) for row in rows],
    }
    if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses):
        return _atom("INFRA_ERROR", source=source, evidence=evidence)
    if all(item == "NA" for item in statuses):
        return _atom("NA", source=source, evidence=evidence)
    if any(item == "FAIL" for item in statuses):
        return _atom("FAIL", source=source, evidence=evidence)
    if any(item in {"INCONCLUSIVE", "NA"} for item in statuses):
        return _atom("INCONCLUSIVE", source=source, evidence=evidence)
    return _atom("PASS", source=source, evidence=evidence)


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


def go_test_json_skips(stdout: str) -> list[str]:
    """Return the test IDs a ``go test -json`` stream reported as skipped.

    A static text scan cannot decide whether a *conditional* skip actually
    fires: ``if true { t.Skip() }`` and ``if runtime.GOOS == "windows"
    { t.Skip() }`` are textually indistinguishable but behaviourally opposed.
    Observing the testing event stream settles it: a test is skipped precisely
    when it disables itself on the evaluation platform.  ``go test -json``
    emits one ``{"Action":"skip","Test":...}`` event per skipped test (and
    sub-test); package-level events carry no ``Test`` field and are ignored.
    """

    skipped: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, Mapping):
            continue
        if str(event.get("Action")) != "skip":
            continue
        test = event.get("Test")
        if isinstance(test, str) and test:
            skipped.add(test)
    return sorted(skipped)


def _test_integrity(
    root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    source: str = "source-test-baseline",
    runtime_skips: Sequence[str] | None = None,
    runtime_skip_audit: Mapping[str, Any] | None = None,
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
    # A ``present_only`` baseline entry is a test file that exists only in the
    # *reference solution* (see ``harbor_entry._merge_test_integrity_baselines``):
    # it is absent from the starting repository and the instruction never
    # declares it protected, so a candidate can only ever *author* a file at
    # that path.  Absence is therefore allowed and an independently authored
    # same-named file is not tamper evidence -- a candidate cannot tamper with a
    # test it never received.  Only the instruction-protected source baseline
    # (the tests present in the starting repository) is enforced for presence
    # AND content below.  Content cannot be used to police reference-only paths
    # here: a faithful port of the *same* source test legitimately resembles the
    # reference port, so a provenance/content check on those paths re-creates
    # the very false positive this rule removes.
    # Constraint 8 preserves a test's behavioural assertions, not its source
    # path.  A port/rename that keeps the content (or the assertion signature)
    # is a legitimate relocation, so a baseline path that is absent is only
    # tampering when no equivalent copy survives elsewhere in the tree.  Index
    # the non-baseline test files by content identity: a byte-identical rename
    # matches by digest, a reformatted port by normalised digest, and a port
    # that only renames the file by assertion signature, while deleting or
    # weakening a test still leaves it unmatched.
    digests: dict[str, list[str]] = {}
    normalized: dict[str, list[str]] = {}
    signatures: dict[str, list[str]] = {}
    for relative, path in current.items():
        if relative in baseline_files or not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        digests.setdefault(_digest(path), []).append(relative)
        normalized.setdefault(_normalized_digest(text), []).append(relative)
        signature = _assertion_signature(text)
        if signature:
            signatures.setdefault(signature, []).append(relative)

    def _relocation(record: object) -> str | None:
        if not isinstance(record, Mapping):
            return None
        for index, key in (
            (digests, "sha256"),
            (normalized, "normalized_sha256"),
            (signatures, "assertions"),
        ):
            value = record.get(key)
            if value:
                matches = index.get(str(value))
                if matches:
                    return sorted(matches)[0]
        return None

    relocated: dict[str, str] = {}
    missing: list[str] = []
    for relative in sorted(baseline_files):
        record = baseline_files[relative]
        if relative in current:
            continue
        if isinstance(record, Mapping) and record.get("present_only"):
            # Reference-solution-only path: absence is always allowed.
            continue
        target = _relocation(record)
        if target is not None:
            relocated[relative] = target
        else:
            missing.append(relative)
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
        if record.get("present_only"):
            # Reference-solution-only path: the candidate authored this file
            # (it never received it), so a content difference is not tamper
            # evidence.  See the block above.
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
        if record.get("assertions") and record.get("assertions") == _assertion_signature(text):
            continue
        changed.append(relative)
    changed = sorted(changed)
    # The skip/xfail check is enforced against the whole candidate tree for
    # *every* case, not only when the source baseline protects tests the
    # candidate received.  Adding an unconditional ``t.Skip`` (or an
    # ``xfail``/``exclude``/``ignore`` equivalent) disables a migrated
    # behavioural test on the evaluation platform, which is the bypass the
    # ``no_new_skip_or_xfail`` atom exists to catch -- independent of whether
    # the test was authored by the reference or ported by the candidate.
    # Markers guarded by a conditional (``if runtime.GOOS == "windows"
    # { t.Skip(...) }``, ``if os.Getenv("AWS_CA_BUNDLE") != "" { t.Skip(...) }``)
    # are legitimate platform/environment guards: the test still runs on the
    # evaluation platform, so they are an equivalent conversion and are not
    # counted (see ``_bypass_marker_counts``).  The baseline's raw marker counts
    # are the "before" side, so a pre-existing skip (guarded or not) is never
    # reported as newly introduced.
    marker_after = _bypass_marker_counts(root)
    tamper = _atom(
        "FAIL" if missing or changed else "PASS",
        source=source,
        evidence={"missing": missing, "changed": changed, "relocated": relocated},
    )
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    # Runtime skips remain useful diagnostics, but they are not independently
    # a test-integrity failure.  A migration necessarily authors a new target
    # test suite, and a conditional skip in an additional candidate-owned test
    # does not remove or weaken a baseline assertion.  Baseline test changes are
    # handled above by the content/assertion comparison, while unconditional
    # bypass markers remain covered by ``marker_delta``.  Treating every newly
    # observed runtime skip as a hard failure would reject otherwise scoreable
    # candidates merely for adding an environment-conditional extra test.
    baseline_runtime_skips = baseline.get("runtime_skips")
    baseline_runtime_skips = (
        {str(item) for item in baseline_runtime_skips}
        if isinstance(baseline_runtime_skips, Sequence)
        and not isinstance(baseline_runtime_skips, (str, bytes))
        else set()
    )
    runtime_new_skips = sorted(
        {
            str(item)
            for item in (runtime_skips or ())
            if str(item) and str(item) not in baseline_runtime_skips
        }
    )
    skip = _atom(
        "FAIL"
        if any(value > 0 for value in marker_delta.values())
        else "PASS",
        source=source,
        evidence={
            "before": dict(marker_before),
            "after": marker_after,
            "delta": marker_delta,
            "bypass_only": True,
            "guarded_markers_excluded": True,
            "runtime_skipped_tests": runtime_new_skips,
            "runtime_skip_audit": (
                dict(runtime_skip_audit)
                if isinstance(runtime_skip_audit, Mapping)
                else None
            ),
        },
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
    runtime_test_skips: Sequence[str] | None = None,
    runtime_skip_audit: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Derive hardgate atoms from Case evidence instead of policy booleans."""

    policy = config.get("policy", {})
    policy = policy if isinstance(policy, Mapping) else {}
    rows = _rows(observations)
    expected_tests = config.get("tests", {})
    expected_tests = expected_tests if isinstance(expected_tests, Mapping) else {}
    p2p = [row for row in rows if row.get("kind") == "P2P"]
    differential = [row for row in rows if row.get("kind") == "DIFFERENTIAL"]
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
        *detect_source_runtime_asset_fallback(candidate_root, config),
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
        candidate_root,
        source_test_baseline,
        config,
        source=test_baseline_source,
        runtime_skips=runtime_test_skips,
        runtime_skip_audit=runtime_skip_audit,
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
    # The differential behavioural oracle is a declared ``required`` group (see
    # ``group_applicability.DIFFERENTIAL``).  Unlike the P2P/F2P groups it is a
    # hard-gate bottom line: a candidate that drifts from the source's
    # observable behaviour must not be admitted by an inflated scope/F2P
    # average.  ``NA`` (probe not applicable to the candidate's internal
    # structure) is not a failure and leaves the gate non-blocking.
    differential_oracle = _differential_oracle_status(
        differential,
        expected=len(expected_tests.get("differential", []))
        if isinstance(expected_tests.get("differential", []), Sequence)
        else None,
        source="candidate-differential-observations",
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
        "differential_oracle_preserved": differential_oracle,
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


__all__ = [
    "build_hardgate_evidence",
    "build_source_test_baseline",
    "go_test_json_skips",
]
