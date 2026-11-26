"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .detectors import (
        detect_source_runtime_dependency,
        detect_source_runtime_dynamic_load,
        detect_source_runtime_ffi,
        detect_source_runtime_subprocess,
    )
    from .detectors.subprocess import constant_strings, reachable_resource_paths
    from .residue import classify_residue_rules, scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from detectors import (
        detect_source_runtime_dependency,
        detect_source_runtime_ffi,
        detect_source_runtime_subprocess,
    )
    from detectors.subprocess import constant_strings, reachable_resource_paths
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
# Marker patterns are tagged with the framework/language whose syntax they
# describe and only applied to files with a compatible suffix.  Without this a
# JavaScript-intended ``test.skip(`` rule also fires on Go ``test.Skip(...)``
# where ``test`` is simply the ``*testing.T`` parameter name of
# ``func TestX(test *testing.T)``.  The Go rule below matches the receiver form
# regardless of the parameter name, so a genuine Go skip is still counted -- a
# skip is only exempted when it is guarded by a concrete platform predicate
# (see ``_platform_guarded``).
_JS_SUFFIXES = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"})
_GO_SUFFIXES = frozenset({".go"})
_PY_SUFFIXES = frozenset({".py"})

_SKIP_MARKERS = (
    re.compile(r"pytest\.mark\.skip(?:if)?", re.IGNORECASE),
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.skip\s*\(", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE),
    re.compile(r"\b(?:skip|xfail|disabled)\s*=\s*true\b", re.IGNORECASE),
    re.compile(r"#\[ignore\]", re.IGNORECASE),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.Skip(?:Now|f)?\s*\(", re.IGNORECASE),
    re.compile(r"@(?:Disabled|Ignore)\b", re.IGNORECASE),
    re.compile(r"\b(?:test|it|describe)\.todo\s*\(", re.IGNORECASE),
    re.compile(r"\bpassWithNoTests\b", re.IGNORECASE),
    # A bare ``exclude`` / ``exclude =`` is ordinary code (``const exclude =
    # new Set()``), not a skipped test, so no generic exclude marker is counted.
    # Test-runner exclude flags live in config files that are not counted here.
)
# (category, pattern, allowed suffixes or None for every language)
_MARKER_RULES: tuple[tuple[str, re.Pattern[str], frozenset[str] | None], ...] = (
    ("skip", _SKIP_MARKERS[0], _PY_SUFFIXES),
    ("xfail", _SKIP_MARKERS[1], _PY_SUFFIXES),
    ("disabled", _SKIP_MARKERS[2], _JS_SUFFIXES),
    ("disabled", _SKIP_MARKERS[3], _JS_SUFFIXES),
    ("disabled", _SKIP_MARKERS[4], _PY_SUFFIXES),
    ("disabled", _SKIP_MARKERS[5], None),
    ("disabled", _SKIP_MARKERS[6], None),
    ("disabled", _SKIP_MARKERS[7], _GO_SUFFIXES),
    ("disabled", _SKIP_MARKERS[8], None),
    ("disabled", _SKIP_MARKERS[9], _JS_SUFFIXES),
    ("disabled", _SKIP_MARKERS[10], None),
)

# A skip guarded by a concrete platform predicate is ordinary conditional-skip
# logic ("run the POSIX fixture only on POSIX"), not a skip introduced to hide a
# failure.  Recognising the guard requires the platform literal, so the check
# runs on the raw text (string contents intact) rather than the
# comment/string-blanked text the marker scanner uses.
_PLATFORM_SIGNAL = re.compile(
    r"(?:runtime\.GOOS|runtime\.GOARCH|\bGOOS\b|\bGOARCH\b"
    r"|process\.platform|os\.platform|sys\.platform|platform\.system"
    r"|target_os|target_family|navigator\.platform|userAgent|user_agent)",
    re.IGNORECASE,
)
_PLATFORM_VALUE = re.compile(
    r"[\"'`](?:windows|win32|win64|darwin|macos|macintosh|mac|linux|gnu"
    r"|freebsd|openbsd|netbsd|dragonfly|sunos|solaris|illumos|aix|android"
    r"|ios|emscripten|js|wasm)[\"'`]",
    re.IGNORECASE,
)
_GUARD_IF = re.compile(r"\b(?:if|elif)\b[^\n{;]*\{")
_GUARD_LOOKBACK = 400


def _platform_guarded(text: str, index: int) -> bool:
    """True when the marker at ``index`` sits inside a platform-predicate block."""

    start = max(0, index - _GUARD_LOOKBACK)
    window = text[start:index]
    for match in _GUARD_IF.finditer(window):
        condition = match.group(0)
        if not (_PLATFORM_SIGNAL.search(condition) and _PLATFORM_VALUE.search(condition)):
            continue
        # Still inside the guarded block: no block close between the guard's
        # opening brace and the marker.
        if "}" not in text[start + match.end() : index]:
            return True
    return False


def _deceptive_scope_patterns(rules: object) -> tuple[str, ...]:
    """The declared production file set that the fallback rules target.

    ``deceptive_fallback_rules`` scope their literal/dependency targets to the
    migrated (target-language) production files.  The semantic detectors are a
    complement to those rules and must honour the same scope: a test runner or
    a retained source asset outside the production set is not a delegation of a
    migrated responsibility.
    """

    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return ()
    patterns: list[str] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        paths = row.get("paths", [])
        if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)):
            continue
        for item in paths:
            if isinstance(item, str) and item.strip():
                patterns.append(item.strip())
    return tuple(dict.fromkeys(patterns))


def _in_deceptive_scope(relative: str, patterns: Sequence[str]) -> bool:
    """True when ``relative`` is part of the target production surface.

    The scan surface is the union of the declared rule paths and every Go
    source file.  The Go clause keeps layout-independent coverage of the
    migrated implementation (a candidate may place packages anywhere), while
    the rule paths carry the build manifests (``Makefile`` / ``go.mod``).
    """

    normalized = relative.replace("\\", "/").casefold().lstrip("./")
    if not normalized:
        return False
    if normalized.endswith(".go"):
        return True
    for pattern in patterns:
        candidate = pattern.replace("\\", "/").casefold().strip().lstrip("./")
        if not candidate:
            continue
        if fnmatchcase(normalized, candidate):
            return True
        if normalized == candidate or normalized.startswith(candidate.rstrip("/") + "/"):
            return True
        if fnmatchcase(normalized, candidate.rstrip("/") + "/*"):
            return True
    return False


# A Go string literal: interpreted (double quotes, honouring escapes) or raw
# (backticks).  Used to find resource paths named by target-language source.
_GO_STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|`[^`]*`')
# Documentation/data files are never executed as programs; a README code block
# or manifest referenced by name must not join the executable scan surface.
_NON_EXECUTABLE_SUFFIXES = frozenset(
    {
        ".md", ".markdown", ".rst", ".txt", ".adoc",
        ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".csv", ".tsv", ".lock", ".sum", ".mod", ".svg", ".png", ".jpg", ".jpeg",
        ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".whl",
    }
)
# A target-language file is a possible launcher only when it can start a
# process; the reference check then binds the named resource to production.
_PROCESS_LAUNCH_SINKS = (
    "exec.command",
    "exec.cmd",
    "os.startprocess",
    "syscall.exec",
    "syscall.forkexec",
    "std::process::command",
    "processbuilder(",
    "subprocess.",
    "child_process",
)


def _referenced_resource_paths(root: Path) -> tuple[str, ...]:
    """Non-source files whose path is named by target-language process code.

    A shell script, Node runner or embedded asset is only a deceptive fallback
    when the migrated (target-language) code can reach it.  A build/test asset
    that no target-language launcher references is tooling, not a delegation of
    a migrated responsibility, so it stays out of the deceptive scan surface.
    The check is suffix-agnostic so a bridge hidden in any resource language
    (``.sh``, ``.mjs``, ``.py``, an extensionless helper) is covered alike.
    """

    if not root.is_dir() or root.is_symlink():
        return ()
    literals: list[str] = []
    resources: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in _EXCLUDED_PARTS for part in Path(relative).parts):
            continue
        suffix = path.suffix.casefold()
        if suffix == ".go":
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not any(sink in text.casefold() for sink in _PROCESS_LAUNCH_SINKS):
                continue
            # Reachability is decided on the *value* a name can build, not on
            # its literal spelling: ``base + ext`` / ``filepath.Join`` /
            # ``fmt.Sprintf`` all name the same resource the direct literal
            # would, and a spelling-only match lets the assembled form through.
            literals.extend(constant_strings(text))
        elif suffix in _NON_EXECUTABLE_SUFFIXES:
            continue
        else:
            resources.append(relative)
    if not literals:
        return ()
    referenced: list[str] = []
    for relative in resources:
        name = Path(relative).name
        if any(
            (name and name in literal) or relative in literal
            for literal in literals
        ):
            referenced.append(relative)
    return tuple(dict.fromkeys(referenced))

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
    """Blank comment content while preserving string-literal contents.

    The scan is string-literal aware: a comment introducer (``//``, ``/* */``
    or ``#``) that appears *inside* a quoted literal (``"#!/bin/sh"``,
    ``"https://example.com"``) is literal data, not a comment.  Regex-only
    stripping corrupted Go/JS source whenever a literal contained ``#`` or
    ``//`` (it deleted to end-of-line, dropping the closing quote), which broke
    the quote pairing the dead-guard and assertion scanners rely on.  Offsets
    are preserved so callers can map matches back onto the original text.
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
        # Inside a string literal: keep the content, but do not let an escaped
        # quote close the literal early (raw backtick strings have no escapes).
        if quote != "`" and character == "\\":
            index += 2
            continue
        if character == quote:
            state = "normal"
        index += 1
    return "".join(characters)


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


# A guard that provably never executes its body.  Neutralising an assertion by
# wrapping it in one of these blocks leaves the assertion call *textually*
# present while removing the check, so an assertion-signature comparison that
# only looks at the call itself cannot see the regression.  Recognising only
# the literal ``if false``/``for 0`` spellings is insufficient: any equivalent
# constant-false predicate (``1 == 2``, ``len(x) < 0``, ``true && false``)
# neutralises the guarded assertions just as well.  The guard condition is
# therefore constant-folded with a bounded evaluator instead of pattern-matched.
_UNKNOWN = object()
_NONNEG = object()

_GUARD_KEYWORD = re.compile(r"\b(if|for|while)\b")
_CONDITION_TOKEN = re.compile(
    r"(?:"
    r"(?P<number>0[xX][0-9a-fA-F]+|\d+\.\d+|\d+)"
    r"|(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`[^`]*`)"
    r"|(?P<identifier>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<operator>===|!==|==|!=|<=|>=|&&|\|\||[<>!+\-*/%()])"
    r")"
)
_BINARY_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "==": 3,
    "===": 3,
    "!=": 3,
    "!==": 3,
    "<": 4,
    "<=": 4,
    ">": 4,
    ">=": 4,
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
    "%": 6,
}


def _condition_tokens(expression: str) -> list[tuple[str, str]] | None:
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(expression)
    while index < length:
        if expression[index].isspace():
            index += 1
            continue
        match = _CONDITION_TOKEN.match(expression, index)
        if match is None:
            return None
        kind = match.lastgroup
        tokens.append((str(kind), match.group(kind)))
        index = match.end()
    return tokens


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _truthiness(value: object) -> object:
    if value is _UNKNOWN or value is _NONNEG:
        return _UNKNOWN
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if _is_number(value):
        return value != 0
    if isinstance(value, str):
        return len(value) > 0
    return _UNKNOWN


def _primary_value(token: tuple[str, str] | None, constants: Mapping[str, object] | None = None) -> object:
    if token is None:
        return _UNKNOWN
    kind, text = token
    if kind == "number":
        try:
            if text.lower().startswith("0x"):
                return int(text, 16)
            return float(text) if "." in text else int(text)
        except ValueError:
            return _UNKNOWN
    if kind == "string":
        return text[1:-1]
    if kind == "identifier":
        lowered = text.casefold()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"null", "undefined", "nil"}:
            return None
        if constants is not None and text in constants:
            return constants[text]
        return _UNKNOWN
    return _UNKNOWN


def _equal_values(left: object, right: object) -> object:
    if left is _UNKNOWN or right is _UNKNOWN or left is _NONNEG or right is _NONNEG:
        return _UNKNOWN
    return left == right


def _compare_values(operator: str, left: object, right: object) -> object:
    for value, other, flip in ((left, right, False), (right, left, True)):
        if value is _NONNEG and _is_number(other):
            operator = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}[operator] if flip else operator
            if operator == "<":
                return False if other <= 0 else _UNKNOWN
            if operator == "<=":
                return False if other < 0 else _UNKNOWN
            if operator == ">":
                return True if other < 0 else _UNKNOWN
            if operator == ">=":
                return True if other <= 0 else _UNKNOWN
            return _UNKNOWN
    if _is_number(left) and _is_number(right):
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
    return _UNKNOWN


def _apply_binary(operator: str, left: object, right: object) -> object:
    if operator == "&&":
        left_truth = _truthiness(left)
        right_truth = _truthiness(right)
        if left_truth is False or right_truth is False:
            return False
        if left_truth is True and right_truth is True:
            return True
        return _UNKNOWN
    if operator == "||":
        left_truth = _truthiness(left)
        right_truth = _truthiness(right)
        if left_truth is True or right_truth is True:
            return True
        if left_truth is False and right_truth is False:
            return False
        return _UNKNOWN
    if operator in {"==", "===", "!=", "!=="}:
        equal = _equal_values(left, right)
        if equal is _UNKNOWN:
            return _UNKNOWN
        return (not equal) if operator in {"!=", "!=="} else equal
    if operator in {"<", "<=", ">", ">="}:
        return _compare_values(operator, left, right)
    if _is_number(left) and _is_number(right):
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        if operator == "/" and right != 0:
            return left / right
        if operator == "%" and right != 0:
            return left % right
    return _UNKNOWN


def _parse_primary(
    tokens: list[tuple[str, str]], position: int, constants: Mapping[str, object] | None = None
) -> tuple[object, int]:
    if position >= len(tokens):
        return _UNKNOWN, position
    kind, text = tokens[position]
    if kind == "identifier" and text.casefold() == "len":
        position += 1
        if position < len(tokens) and tokens[position] == ("operator", "("):
            depth = 0
            while position < len(tokens):
                piece = tokens[position]
                if piece == ("operator", "("):
                    depth += 1
                elif piece == ("operator", ")"):
                    depth -= 1
                    if depth == 0:
                        position += 1
                        break
                position += 1
            return _NONNEG, position
        return _UNKNOWN, position
    if text == "(" and kind == "operator":
        value, position = _parse_binary(tokens, position + 1, 0, constants)
        if position < len(tokens) and tokens[position] == ("operator", ")"):
            position += 1
        return value, position
    return _primary_value(tokens[position], constants), position + 1


def _parse_binary(
    tokens: list[tuple[str, str]],
    position: int,
    minimum: int,
    constants: Mapping[str, object] | None = None,
) -> tuple[object, int]:
    if position < len(tokens):
        kind, text = tokens[position]
        if kind == "operator" and text in {"!", "-", "+"}:
            value, position = _parse_primary(tokens, position + 1, constants)
            if text == "!":
                truth = _truthiness(value)
                return (_UNKNOWN if truth is _UNKNOWN else not truth), position
            if text == "-":
                return (-value if _is_number(value) else _UNKNOWN), position
            return (value if _is_number(value) else _UNKNOWN), position
    left, position = _parse_primary(tokens, position, constants)
    while position < len(tokens):
        kind, text = tokens[position]
        if kind != "operator" or text not in _BINARY_PRECEDENCE:
            break
        precedence = _BINARY_PRECEDENCE[text]
        if precedence < minimum:
            break
        right, position = _parse_binary(tokens, position + 1, precedence + 1, constants)
        left = _apply_binary(text, left, right)
    return left, position


# ``const`` bindings are immutable, so resolving one to its value is sound: a
# guard ``if never { … }`` with ``const never = false`` provably never runs.
# Only ``const`` declarations are folded (a mutable ``var``/``:=`` may be
# reassigned before the guard, so resolving it could mark a live guard dead).
_CONST_BLOCK = re.compile(r"(?m)^[ \t]*const[ \t]*\(([^)]*)\)")
_CONST_ENTRY = re.compile(
    r"(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:[ \t]+[A-Za-z_][A-Za-z0-9_.\[\]*]*)?"  # optional type annotation
    r"[ \t]*=[ \t]*([^\n;]+)"
)
_CONST_SINGLE = re.compile(
    r"(?m)^[ \t]*const[ \t]+([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:[ \t]+[A-Za-z_][A-Za-z0-9_.\[\]*]*)?"
    r"[ \t]*=[ \t]*([^\n;]+)"
)


def _fold_expression(expression: str, constants: Mapping[str, object]) -> object:
    tokens = _condition_tokens(expression)
    if not tokens:
        return _UNKNOWN
    value, position = _parse_binary(tokens, 0, 0, constants)
    if position != len(tokens):
        return _UNKNOWN
    return value


def _const_values(text: str) -> dict[str, object]:
    """Named ``const`` bindings folded to their immutable value.

    A bounded evaluator resolves the literal and constant-expression spellings
    (``const never = false``, ``const never = 1 == 2``, a ``const ( … )`` block,
    a typed declaration) so a dead guard cannot be hidden behind a name.  A few
    passes let a const reference another const without needing a real linker.
    """

    declarations: list[tuple[str, str]] = []
    for block in _CONST_BLOCK.finditer(text):
        for entry in _CONST_ENTRY.finditer(block.group(1)):
            declarations.append((entry.group(1), entry.group(2)))
    for single in _CONST_SINGLE.finditer(text):
        declarations.append((single.group(1), single.group(2)))
    values: dict[str, object] = {}
    for _ in range(4):
        progressed = False
        for name, expression in declarations:
            folded = _fold_expression(expression, values)
            if folded is _UNKNOWN or folded is _NONNEG:
                continue
            if name not in values or values[name] != folded:
                values[name] = folded
                progressed = True
        if not progressed:
            break
    return values


def _condition_is_dead(condition: str, constants: Mapping[str, object] | None = None) -> bool:
    tokens = _condition_tokens(condition)
    if not tokens:
        return False
    value, position = _parse_binary(tokens, 0, 0, constants)
    if position != len(tokens):
        return False
    return _truthiness(value) is False


def _guard_block_open(text: str, start: int) -> int | None:
    """Return the index of the ``{`` opening the guard's body, if any."""

    depth = 0
    index = start
    limit = min(len(text), start + 400)
    quote = ""
    while index < limit:
        character = text[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
        elif character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
            if depth < 0:
                return None
        elif character == "{":
            if depth == 0:
                return index
            depth += 1
        elif character == "}":
            return None
        index += 1
    return None


def _dead_guard_openings(text: str) -> list[int]:
    """Find ``{`` of guards whose condition is provably constant-false.

    The scan runs over a copy whose string literals are blanked so a guard
    keyword inside a quoted string can never masquerade as control flow.  The
    returned offsets stay valid for the original text.  Named ``const`` bindings
    are folded into the condition evaluator so a guard spelled ``if never {}``
    is recognised just like the literal ``if false {}`` the name stands for.
    """

    scan = _strip_comments(text)
    constants = _const_values(scan)
    openings: list[int] = []
    for match in _GUARD_KEYWORD.finditer(scan):
        open_index = _guard_block_open(scan, match.end())
        if open_index is None:
            continue
        segment = scan[match.end() : open_index]
        condition = segment.rsplit(";", 1)[-1]
        if _condition_is_dead(condition, constants):
            openings.append(open_index)
    return openings


def _matching_brace(text: str, open_index: int) -> int | None:
    """Return the index of the ``}`` matching the ``{`` at ``open_index``."""

    depth = 0
    index = open_index
    length = len(text)
    quote = ""
    while index < length:
        character = text[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
            index += 1
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _mask_dead_blocks(text: str) -> str:
    """Blank the bodies of statically-dead guards while preserving offsets."""

    characters = list(text)
    for open_index in _dead_guard_openings(text):
        close_index = _matching_brace(text, open_index)
        if close_index is None:
            continue
        for position in range(open_index, close_index + 1):
            if characters[position] != "\n":
                characters[position] = " "
    return "".join(characters)


def _effective_assertion_signature(text: str) -> str:
    """Assertion signature with assertions inside dead guards removed.

    A test can be weakened by leaving every assertion call in place but making
    it unreachable (``if false { t.Fatal(...) }``); the raw signature is
    unchanged, so the effective signature is what distinguishes a genuine check
    from a neutralised one.

    Dead-guard masking runs on the original text: the guard scanner is already
    string-literal aware, so it locates the guard reliably regardless of what
    the surrounding literals contain.
    """

    return _assertion_signature(_mask_dead_blocks(text))


def merge_test_baselines(
    source_baseline: Mapping[str, Any] | None,
    target_baseline: Mapping[str, Any] | None,
) -> tuple[dict[str, object], set[str]]:
    """Combine the source and target test baselines.

    Only tests present in the source repository are protected.  Reference-only
    Go tests are optional wiring; a candidate may independently author a test
    at the same path, with different assertions and implementation APIs.
    """

    source = source_baseline if isinstance(source_baseline, Mapping) else {}
    target = target_baseline if isinstance(target_baseline, Mapping) else {}
    source_files = source.get("files", {})
    target_files = target.get("files", {})
    source_files = source_files if isinstance(source_files, Mapping) else {}
    target_files = target_files if isinstance(target_files, Mapping) else {}
    files: dict[str, object] = {str(key): value for key, value in source_files.items()}
    marker_counts = source.get("marker_counts")
    if not isinstance(marker_counts, Mapping):
        marker_counts = target.get("marker_counts", {})
    merged = {
        "schema_version": "migration-merged-test-baseline:v1",
        "files": files,
        "marker_counts": dict(marker_counts) if isinstance(marker_counts, Mapping) else {},
    }
    return merged, {str(key) for key in source_files}


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


def _marker_counts(root: Path, paths: set[str] | None = None) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        if paths is not None and relative not in paths:
            continue
        if not _is_test(relative):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _strip_comments(raw)
        suffix = Path(relative).suffix.casefold()
        rules = (*_MARKER_RULES, ("disabled", _DISABLED_ALIAS, _JS_SUFFIXES))
        for category, pattern, suffixes in rules:
            if suffixes is not None and suffix not in suffixes:
                continue
            counts[category] += sum(
                1
                for match in pattern.finditer(text)
                if not _platform_guarded(raw, match.start())
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
        and row.get("diagnostic_only") is not True
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
    required: object = None,
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
    required_paths = (
        {str(item) for item in required}
        if isinstance(required, (set, frozenset, list, tuple))
        else None
    )
    missing = sorted(
        str(relative) for relative in baseline_files
        if str(relative) not in current
        and (required_paths is None or str(relative) in required_paths)
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
        if record.get("assertions") and record.get("assertions") == _effective_assertion_signature(text):
            continue
        changed.append(relative)
    changed = sorted(changed)
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    # Optional new tests (profiling, missing tools, platform fixtures) cannot
    # weaken a pre-existing public assertion. Check skip deltas on protected
    # tests only; their removal/modification remains covered above.
    marker_after = _marker_counts(root, {str(path) for path in baseline_files})
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
    required_test_paths: object = None,
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
    # The scan surface is the declared production set plus every resource the
    # target-language code can actually reach.  Reachability is delegated to the
    # detector's one contract (``//go:embed`` ∪ process-launch constant values,
    # suffix-agnostic and alias-aware) so the scope filter cannot be narrower
    # than the detector that produces the hits it filters.
    deceptive_patterns = (
        *_deceptive_scope_patterns(deceptive_rules),
        *_referenced_resource_paths(candidate_root),
        *reachable_resource_paths(candidate_root),
    )
    detector_hits = [
        hit
        for hit in (
            *detect_source_runtime_subprocess(candidate_root, config),
            *detect_source_runtime_ffi(candidate_root, config),
            *detect_source_runtime_dynamic_load(candidate_root, config),
        )
        if _in_deceptive_scope(str(hit.get("path", "")), deceptive_patterns)
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
        candidate_root, source_test_baseline, config, source=test_baseline_source,
        required=required_test_paths,
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


__all__ = ["build_hardgate_evidence", "build_source_test_baseline", "merge_test_baselines"]
