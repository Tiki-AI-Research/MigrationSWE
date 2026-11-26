"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .residue import (
        _UNKNOWN,
        _parse_concat,
        _tokenize,
        classify_residue_rules,
        scan_residue_rules,
    )
except ImportError:  # Standalone Runtime Draft layout.
    from residue import (  # type: ignore[no-redef]
        _UNKNOWN,
        _parse_concat,
        _tokenize,
        classify_residue_rules,
        scan_residue_rules,
    )


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


def _strip_comments_keep_strings(text: str) -> str:
    """Blank comment content while preserving string literals and offsets."""

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
            if character == "/" and index + 1 < length and text[index + 1] == "*":
                characters[index] = " "
                characters[index + 1] = " "
                index += 2
                while index < length and not (
                    text[index] == "*" and index + 1 < length and text[index + 1] == "/"
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
        if character == "\\":
            index += 2
            continue
        if character == quote:
            state = "normal"
        index += 1
    return "".join(characters)


_SPAN_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>//[^\n]*|/\*.*?\*/)
    | (?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`)
    | (?P<number>0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)
    | (?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)
    | (?P<punct>[\[\]\(\)\{\}\.,;:+\-*/%=<>!&|?])
    """,
    re.DOTALL | re.VERBOSE,
)
_MULTI_OPERATORS = frozenset(
    {"===", "!==", "==", "!=", "<=", ">=", "&&", "||", "=>", "++", "--", "**"}
)
_UNDEFINED = object()


def _span_tokens(text: str) -> list[tuple[str, str, int, int]]:
    spans: list[tuple[str, str, int, int]] = []
    for match in _SPAN_TOKEN.finditer(text):
        kind = match.lastgroup
        if kind in {"ws", "comment"}:
            continue
        value = match.group()
        if kind == "string":
            body = value[1:-1]
            if value[0] == "`" and "${" in body:
                spans.append(("opaque", "", match.start(), match.end()))
                continue
            spans.append(("string", body, match.start(), match.end()))
            continue
        spans.append((kind, value, match.start(), match.end()))
    return spans


def _merge_operators(
    spans: Sequence[tuple[str, str, int, int]],
) -> list[tuple[str, str, int, int]]:
    merged: list[tuple[str, str, int, int]] = []
    index = 0
    total = len(spans)
    while index < total:
        kind, value, start, end = spans[index]
        if kind == "punct":
            operator = value
            cursor = index
            while (
                cursor + 1 < total
                and spans[cursor + 1][0] == "punct"
                and spans[cursor + 1][2] == end
                and operator + spans[cursor + 1][1] in _MULTI_OPERATORS
            ):
                operator += spans[cursor + 1][1]
                cursor += 1
                end = spans[cursor][3]
            merged.append(("punct", operator, start, end))
            index = cursor + 1
            continue
        merged.append(spans[index])
        index += 1
    return merged


def _is_truthy(value: object) -> bool:
    if value is _UNDEFINED or value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0 and value == value
    if isinstance(value, str):
        return len(value) > 0
    return True


def _strict_equal(left: object, right: object) -> bool:
    if left is _UNDEFINED or right is _UNDEFINED:
        return left is _UNDEFINED and right is _UNDEFINED
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return False


def _loose_equal(left: object, right: object) -> bool:
    if (left is None or left is _UNDEFINED) and (right is None or right is _UNDEFINED):
        return True
    if left is None or left is _UNDEFINED or right is None or right is _UNDEFINED:
        return False
    if isinstance(left, bool):
        left = 1 if left else 0
    if isinstance(right, bool):
        right = 1 if right else 0
    if isinstance(left, str) and isinstance(right, (int, float)):
        try:
            left = float(left)
        except ValueError:
            return False
    if isinstance(right, str) and isinstance(left, (int, float)):
        try:
            right = float(right)
        except ValueError:
            return False
    return left == right


def _compare(op: str, left: object, right: object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        pass
    elif (
        isinstance(left, (int, float))
        and isinstance(right, (int, float))
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    ):
        left, right = float(left), float(right)
    else:
        raise TypeError("incomparable constants")
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    return left >= right


_ConstEnv = Mapping[str, object]


def _eval_primary(
    tokens: Sequence[tuple[str, str, int, int]],
    index: int,
    end: int,
    env: _ConstEnv,
):
    if index >= end:
        return (False, None), index
    kind, value = tokens[index][0], tokens[index][1]
    if kind == "number":
        try:
            number = (
                int(value, 16)
                if value.lower().startswith("0x")
                else (float(value) if "." in value else int(value))
            )
        except ValueError:
            return (False, None), index + 1
        return (True, number), index + 1
    if kind == "string":
        return (True, value), index + 1
    if kind == "ident":
        if value == "true":
            return (True, True), index + 1
        if value == "false":
            return (True, False), index + 1
        if value == "null":
            return (True, None), index + 1
        if value == "undefined":
            return (True, _UNDEFINED), index + 1
        if value in env:
            return (True, env[value]), index + 1
        return (False, None), index + 1
    if kind == "punct" and value == "(":
        result, cursor = _eval_or(tokens, index + 1, end, env)
        if cursor < end and tokens[cursor][1] == ")":
            cursor += 1
        return result, cursor
    return (False, None), index + 1


def _eval_unary(
    tokens: Sequence[tuple[str, str, int, int]],
    index: int,
    end: int,
    env: _ConstEnv,
):
    if index < end and tokens[index][0] == "punct" and tokens[index][1] == "!":
        result, cursor = _eval_unary(tokens, index + 1, end, env)
        if result[0]:
            return (True, not _is_truthy(result[1])), cursor
        return (False, None), cursor
    if index < end and tokens[index][0] == "punct" and tokens[index][1] == "-":
        result, cursor = _eval_unary(tokens, index + 1, end, env)
        if result[0] and isinstance(result[1], (int, float)) and not isinstance(
            result[1], bool
        ):
            return (True, -result[1]), cursor
        return (False, None), cursor
    return _eval_primary(tokens, index, end, env)


def _eval_relational(
    tokens: Sequence[tuple[str, str, int, int]],
    index: int,
    end: int,
    env: _ConstEnv,
):
    left, cursor = _eval_unary(tokens, index, end, env)
    while (
        cursor < end
        and tokens[cursor][0] == "punct"
        and tokens[cursor][1] in {"<", ">", "<=", ">="}
    ):
        op = tokens[cursor][1]
        right, cursor = _eval_unary(tokens, cursor + 1, end, env)
        if left[0] and right[0]:
            try:
                left = (True, _compare(op, left[1], right[1]))
            except TypeError:
                left = (False, None)
        else:
            left = (False, None)
    return left, cursor


def _eval_equality(
    tokens: Sequence[tuple[str, str, int, int]],
    index: int,
    end: int,
    env: _ConstEnv,
):
    left, cursor = _eval_relational(tokens, index, end, env)
    while (
        cursor < end
        and tokens[cursor][0] == "punct"
        and tokens[cursor][1] in {"===", "!==", "==", "!="}
    ):
        op = tokens[cursor][1]
        right, cursor = _eval_relational(tokens, cursor + 1, end, env)
        if left[0] and right[0]:
            equal = (
                _strict_equal(left[1], right[1])
                if op in {"===", "!=="}
                else _loose_equal(left[1], right[1])
            )
            left = (True, equal if op in {"===", "=="} else not equal)
        else:
            left = (False, None)
    return left, cursor


def _eval_and(
    tokens: Sequence[tuple[str, str, int, int]],
    index: int,
    end: int,
    env: _ConstEnv,
):
    left, cursor = _eval_equality(tokens, index, end, env)
    while cursor < end and tokens[cursor][0] == "punct" and tokens[cursor][1] == "&&":
        right, cursor = _eval_equality(tokens, cursor + 1, end, env)
        if left[0] and not _is_truthy(left[1]):
            left = (True, False)
            continue
        if left[0] and right[0]:
            left = (True, right[1])
        else:
            left = (False, None)
    return left, cursor


def _eval_or(
    tokens: Sequence[tuple[str, str, int, int]],
    index: int,
    end: int,
    env: _ConstEnv,
):
    left, cursor = _eval_and(tokens, index, end, env)
    while cursor < end and tokens[cursor][0] == "punct" and tokens[cursor][1] == "||":
        right, cursor = _eval_and(tokens, cursor + 1, end, env)
        if left[0] and _is_truthy(left[1]):
            continue
        if left[0] and right[0]:
            left = (True, right[1])
        else:
            left = (False, None)
    return left, cursor


def _expression_extent(
    tokens: Sequence[tuple[str, str, int, int]], start: int
) -> int:
    depth = 0
    index = start
    while index < len(tokens):
        kind, value = tokens[index][0], tokens[index][1]
        if kind == "punct":
            if value in {"(", "[", "{"}:
                depth += 1
            elif value in {")", "]", "}"}:
                if depth == 0:
                    return index
                depth -= 1
            elif value in {";", ","} and depth == 0:
                return index
        index += 1
    return len(tokens)


def _constant_bindings(
    tokens: Sequence[tuple[str, str, int, int]]
) -> dict[str, object]:
    """Fixpoint of ``const/let/var`` bindings whose initializer is constant."""

    env: dict[str, object] = {}
    for _ in range(8):
        changed = False
        for index in range(len(tokens) - 3):
            kind, value = tokens[index][0], tokens[index][1]
            if kind != "ident" or value not in {"const", "let", "var"}:
                continue
            if (
                tokens[index + 1][0] != "ident"
                or tokens[index + 2][0] != "punct"
                or tokens[index + 2][1] != "="
            ):
                continue
            name = tokens[index + 1][1]
            extent = _expression_extent(tokens, index + 3)
            (known, resolved), _ = _eval_or(tokens, index + 3, extent, env)
            if not known:
                continue
            if name not in env or env[name] != resolved:
                env[name] = resolved
                changed = True
        if not changed:
            break
    return env


def _matching_token(
    tokens: Sequence[tuple[str, str, int, int]], open_index: int, opener: str, closer: str
) -> int | None:
    depth = 0
    for index in range(open_index, len(tokens)):
        kind, value = tokens[index][0], tokens[index][1]
        if kind != "punct":
            continue
        if value == opener:
            depth += 1
        elif value == closer:
            depth -= 1
            if depth == 0:
                return index
    return None


def _statement_extent(
    tokens: Sequence[tuple[str, str, int, int]], start: int, limit: int
) -> int | None:
    if start >= limit:
        return None
    if tokens[start][0] == "punct" and tokens[start][1] == "{":
        return _matching_token(tokens, start, "{", "}")
    depth = 0
    index = start
    while index < limit:
        kind, value = tokens[index][0], tokens[index][1]
        if kind == "punct":
            if value in {"(", "[", "{"}:
                depth += 1
            elif value in {")", "]", "}"}:
                if depth == 0:
                    return index - 1
                depth -= 1
            elif value == ";" and depth == 0:
                return index
        index += 1
    return limit - 1


def _strip_dead_branches(text: str) -> str:
    """Blank branches whose guard folds to a compile-time constant.

    The allow-listed test file may be adapted, but a source assertion moved into
    ``if (false) { ... }`` must not count as "still present".  Constant
    conditions are folded with a small JS expression evaluator and the untaken
    branch is masked with spaces (offsets preserved) so the assertion-signature
    extraction sees only reachable assertions.
    """

    tokens = _merge_operators(_span_tokens(text))
    env = _constant_bindings(tokens)
    masked = list(text)
    total = len(tokens)

    def blank(start: int, end: int) -> None:
        for position in range(start, min(end + 1, len(masked))):
            if masked[position] != "\n":
                masked[position] = " "

    index = 0
    while index < total:
        kind, value = tokens[index][0], tokens[index][1]
        if kind != "ident" or value not in {"if", "while"}:
            index += 1
            continue
        if index + 1 >= total or tokens[index + 1][1] != "(":
            index += 1
            continue
        close_index = _matching_token(tokens, index + 1, "(", ")")
        if close_index is None:
            index += 1
            continue
        (known, condition), _ = _eval_or(tokens, index + 2, close_index, env)
        truthy = _is_truthy(condition) if known else None
        body_start = close_index + 1
        body_end = _statement_extent(tokens, body_start, total)
        if body_end is None:
            index = close_index + 1
            continue
        else_start = None
        if (
            value == "if"
            and body_end + 1 < total
            and tokens[body_end + 1][0] == "ident"
            and tokens[body_end + 1][1] == "else"
        ):
            else_start = body_end + 2
        if truthy is True and else_start is not None:
            else_end = _statement_extent(tokens, else_start, total)
            if else_end is not None:
                blank(tokens[else_start][2], tokens[else_end][3])
        elif truthy is False:
            blank(tokens[body_start][2], tokens[body_end][3])
        index = body_end + 1
    return "".join(masked)


_TEST_FRAMEWORK_CALLS = frozenset(
    {
        "describe", "it", "test", "context", "specify",
        "beforeEach", "afterEach", "beforeAll", "afterAll", "before", "after",
        "xit", "xdescribe", "fit", "fdescribe",
    }
)


def _blank_offsets(masked: list[str], start: int, end: int) -> None:
    for position in range(start, min(end + 1, len(masked))):
        if masked[position] != "\n":
            masked[position] = " "


def _function_block_span(
    tokens: Sequence[tuple[str, str, int, int]], paren_open: int
) -> tuple[int, int] | None:
    """``(params) { body }`` -> the ``{ body }`` token span, or ``None``."""

    if paren_open >= len(tokens) or tokens[paren_open][:2] != ("punct", "("):
        return None
    close = _matching_token(tokens, paren_open, "(", ")")
    if close is None or close + 1 >= len(tokens):
        return None
    if tokens[close + 1][:2] != ("punct", "{"):
        return None
    body_end = _matching_token(tokens, close + 1, "{", "}")
    if body_end is None:
        return None
    return (close + 1, body_end)


def _arrow_or_function_block(
    tokens: Sequence[tuple[str, str, int, int]], start: int
) -> tuple[int, int] | None:
    """Block body of ``function ...`` / ``(...) => { }`` / ``x => { }``."""

    total = len(tokens)
    cursor = start
    if cursor < total and tokens[cursor][:2] == ("ident", "async"):
        cursor += 1
    if cursor < total and tokens[cursor][:2] == ("ident", "function"):
        cursor += 1
        while cursor < total and tokens[cursor][:2] != ("punct", "("):
            cursor += 1
        return _function_block_span(tokens, cursor) if cursor < total else None
    if cursor < total and tokens[cursor][:2] == ("punct", "("):
        close = _matching_token(tokens, cursor, "(", ")")
        if close is not None and close + 1 < total and tokens[close + 1][:2] == ("punct", "=>"):
            if close + 2 < total and tokens[close + 2][:2] == ("punct", "{"):
                body_end = _matching_token(tokens, close + 2, "{", "}")
                if body_end is not None:
                    return (close + 2, body_end)
        return None
    if (
        cursor + 2 < total
        and tokens[cursor][0] == "ident"
        and tokens[cursor + 1][:2] == ("punct", "=>")
        and tokens[cursor + 2][:2] == ("punct", "{")
    ):
        body_end = _matching_token(tokens, cursor + 2, "{", "}")
        if body_end is not None:
            return (cursor + 2, body_end)
    return None


def _named_function_like_bodies(
    tokens: Sequence[tuple[str, str, int, int]],
) -> list[tuple[str, int, int]]:
    """``(name, body_start, body_end)`` for named function declarations and
    ``const/let/var NAME = function|arrow`` bindings with a block body."""

    total = len(tokens)
    found: list[tuple[str, int, int]] = []
    index = 0
    while index < total:
        kind, value = tokens[index][0], tokens[index][1]
        if kind == "ident" and value == "function":
            cursor = index + 1
            if cursor < total and tokens[cursor][:2] == ("punct", "*"):
                cursor += 1
            if cursor < total and tokens[cursor][0] == "ident":
                name = tokens[cursor][1]
                cursor += 1
                while cursor < total and tokens[cursor][:2] != ("punct", "("):
                    cursor += 1
                span = _function_block_span(tokens, cursor) if cursor < total else None
                if span is not None:
                    found.append((name, span[0], span[1]))
            index += 1
            continue
        if kind == "ident" and value in {"const", "let", "var"}:
            if (
                index + 2 < total
                and tokens[index + 1][0] == "ident"
                and tokens[index + 2][:2] == ("punct", "=")
            ):
                span = _arrow_or_function_block(tokens, index + 3)
                if span is not None:
                    found.append((tokens[index + 1][1], span[0], span[1]))
            index += 1
            continue
        index += 1
    return found


def _direct_argument_names(
    tokens: Sequence[tuple[str, str, int, int]],
    start: int,
    end: int,
    declared: set[str],
    reachable: set[str],
) -> None:
    """Names passed as *direct* arguments of a call (depth 0 within its args).

    A name nested inside a callback body (``describe("x", () => { void helper; })``)
    is not a direct argument, so a discarded reference cannot masquerade as a
    live test callback.
    """

    depth = 0
    for index in range(start, end):
        kind, value = tokens[index][0], tokens[index][1]
        if kind == "punct" and value in {"(", "[", "{"}:
            depth += 1
        elif kind == "punct" and value in {")", "]", "}"}:
            depth -= 1
        elif depth == 0 and kind == "ident" and value in declared:
            reachable.add(value)


def _reachable_function_names(
    tokens: Sequence[tuple[str, str, int, int]], declared: set[str]
) -> set[str]:
    total = len(tokens)
    reachable: set[str] = set()
    for index in range(total):
        if tokens[index][0] != "ident" or tokens[index][1] not in declared:
            continue
        following = tokens[index + 1][:2] if index + 1 < total else ("", "")
        if following == ("punct", "("):
            reachable.add(tokens[index][1])
        elif following == ("punct", "?") and index + 2 < total and tokens[index + 2][:2] == ("punct", "("):
            reachable.add(tokens[index][1])
    for index in range(total):
        if (
            tokens[index][0] == "ident"
            and tokens[index][1] in _TEST_FRAMEWORK_CALLS
            and index + 1 < total
            and tokens[index + 1][:2] == ("punct", "(")
        ):
            close = _matching_token(tokens, index + 1, "(", ")")
            if close is not None:
                _direct_argument_names(tokens, index + 2, close, declared, reachable)
    for index in range(total):
        if tokens[index][:2] != ("ident", "export"):
            continue
        cursor = index + 1
        if cursor < total and tokens[cursor][:2] == ("ident", "default"):
            cursor += 1
        if cursor < total and tokens[cursor][0] == "ident" and tokens[cursor][1] in declared:
            reachable.add(tokens[cursor][1])
    return reachable


def _strip_unreachable_functions(text: str) -> str:
    """Blank the bodies of named functions that are never invoked.

    An assertion moved into ``const retained = () => { expect(...) }`` (or any
    named helper that is never called, never a test-framework callback, and not
    exported) does not run, so it must not count as "the assertion is still
    present".  Only named function-likes are considered; anonymous callbacks and
    object methods are left untouched.  Spans are masked with spaces so offsets
    stay stable for the downstream comment strip.
    """

    tokens = _merge_operators(_span_tokens(text))
    if not tokens:
        return text
    bodies = _named_function_like_bodies(tokens)
    if not bodies:
        return text
    declared = {name for name, _, _ in bodies}
    unreachable = declared - _reachable_function_names(tokens, declared)
    if not unreachable:
        return text
    masked = list(text)
    for name, body_start, body_end in bodies:
        if name in unreachable:
            _blank_offsets(masked, tokens[body_start][2], tokens[body_end][3])
    return "".join(masked)


_EXPECT = re.compile(r"\bexpect\s*\(")
_MATCHER_CHAIN = re.compile(r"\s*\.\s*[A-Za-z_$][\w$]*")
_OPEN_CALL = re.compile(r"\s*\(")
_TY_CAST = re.compile(r"\bas\s+(?:any|never|const|unknown)\b")


def _balanced_end(text: str, open_index: int) -> int | None:
    if text[open_index:open_index + 1] in {"{", "["}:
        opener = text[open_index]
        tokens = _span_tokens(text[open_index:])
        end = _matching_token(tokens, 0, opener, "}" if opener == "{" else "]")
        return open_index + tokens[end][2] if end is not None else None
    depth = 0
    index = open_index
    length = len(text)
    while index < length:
        character = text[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _assertion_signatures(text: str) -> list[str]:
    """Normalized ``expect(...).matcher(...)`` chains, order-preserving.

    Whitespace and TypeScript casts are erased so a formatting-only rewrite is
    not mistaken for a change, while a dropped or downgraded matcher
    (``toEqual`` -> ``toBeDefined``) yields a different signature.
    """

    text = _strip_comments_keep_strings(_strip_unreachable_functions(_strip_dead_branches(text)))
    signatures: list[str] = []
    for match in _EXPECT.finditer(text):
        start = match.start()
        close = _balanced_end(text, match.end() - 1)
        if close is None:
            continue
        cursor = close + 1
        while True:
            chain = _MATCHER_CHAIN.match(text[cursor:])
            if chain is None:
                break
            cursor += chain.end()
            call = _OPEN_CALL.match(text[cursor:])
            if call is None:
                continue
            cursor += call.end() - 1
            call_close = _balanced_end(text, cursor)
            if call_close is None:
                cursor = len(text)
                break
            cursor = call_close + 1
        signature = _TY_CAST.sub("", text[start:cursor])
        signatures.append(re.sub(r"\s+", "", signature))
    return signatures


def _expand_test_aliases(text: str, protected: set[str]) -> str:
    """Inline immutable local call aliases inside assertions, respecting scope.

    Only new identifiers are expanded: names in the baseline signatures retain
    their meaning. `const copy = getInitialData(data)` and a vi.fn-wrapped
    equivalent handler must not look like removal of the original assertion.
    Dead branches/functions are stripped first, as in the ordinary extractor.
    """
    text = _strip_comments_keep_strings(_strip_unreachable_functions(_strip_dead_branches(text)))
    masked = _strip_comments(text)
    scopes: list[tuple[int, ...]] = []
    stack: list[int] = []
    for index, character in enumerate(masked):
        scopes.append(tuple(stack))
        if character == "{":
            stack.append(index)
        elif character == "}" and stack:
            stack.pop()
    declarations: list[tuple[int, tuple[int, ...], str, str]] = []
    pattern = re.compile(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*((?:[A-Za-z_$][\w$]*\.)*[A-Za-z_$][\w$]*(?:<[^;\n]+?>)?\s*\()")
    for match in pattern.finditer(text):
        if match.group(1) in protected:
            continue
        close = _balanced_end(text, match.end() - 1)
        if close is None or text[close + 1:].lstrip()[:1] != ";":
            continue
        expression = text[match.start(2):close + 1]
        if expression.startswith("vi.fn("):
            expression = expression[6:-1]
        declarations.append((match.start(), scopes[match.start()], match.group(1), expression))
    edits = []
    for match in _EXPECT.finditer(text):
        close = _balanced_end(text, match.end() - 1)
        if close is None:
            continue
        scope = scopes[match.start()]
        aliases = {
            name: value for position, declared_scope, name, value in declarations
            if position < match.start() and scope[:len(declared_scope)] == declared_scope
        }
        expression = text[match.end():close]
        for _ in range(4):
            previous = expression
            expression = re.sub(
                r'''(['"`])(?:\\.|(?!\1).)*?\1|\b[A-Za-z_$][\w$]*\b''',
                lambda token: aliases.get(token.group(), token.group()), expression,
            )
            if expression == previous:
                break
        edits.append((match.end(), close, expression))
    for start, end, value in reversed(edits):
        text = text[:start] + value + text[end:]
    return text


def _canonical_assertion(signature: str) -> str:
    # Function parameter annotations do not change the asserted handler result.
    signature = re.sub(r"\((\w+):[A-Za-z_$][\w$]*\)=>", r"(\1)=>", signature)
    # A missing-handler assertion is unchanged when additional non-selected
    # handlers are supplied. Require an explicit object with no spread or
    # computed keys, and prove that the selected literal id is still absent.
    if signature.endswith(".toThrow(/Nomatchhandler/)"):
        match = re.search(r'''matchStep\([^,]+,["']([^"']+)["'],(\{)''', signature)
        if match:
            start = match.start(2)
            close = _balanced_end(signature, start)
            if close is not None:
                body = signature[start + 1:close]
                keys = re.findall(r"(?:^|,)([A-Za-z_$][\w$]*):", body)
                if keys and match.group(1) not in keys and "..." not in body and "[" not in body:
                    signature = signature[:start] + "{missing-selected-handler}" + signature[close + 1:]
    return signature


_TEST_FRAMEWORK_BASES = {"describe", "it", "test"}
_TEST_PROPERTY_MARKERS = {"skip", "only"}
# Tokens that may sit between a test-framework global and the property it
# accesses: parentheses from grouping/arg-lists, TypeScript ``as`` casts and
# their primitive type names, and the pieces of an optional chain.
_SKIP_BACKWARD_PUNCT = {"(", ")", "[", "]", ".", "?", "!", ",", ":"}
_SKIP_BACKWARD_IDENTS = {"as", "any", "unknown", "never", "const"}


def _fold_string_constants(tokens: Sequence[tuple[str, str]]) -> dict[str, object]:
    """Constant ``const`` bindings (to a fixpoint), mirroring the residue fold."""

    env: dict[str, object] = {}
    for _ in range(6):
        changed = False
        for index in range(len(tokens) - 2):
            kind, value = tokens[index]
            if kind != "ident" or value not in {"const", "let", "var"}:
                continue
            if tokens[index + 1][0] != "ident" or tokens[index + 2] != ("punct", "="):
                continue
            name = tokens[index + 1][1]
            resolved, _ = _parse_concat(list(tokens), index + 3, env)
            if resolved is _UNKNOWN:
                continue
            if name not in env or env[name] != resolved:
                env[name] = resolved
                changed = True
        if not changed:
            break
    return env


def _skip_base_identifier(
    tokens: Sequence[tuple[str, str]], index: int
) -> str | None:
    """Nearest identifier to the left of a member access, skipping cast noise."""

    cursor = index - 1
    while cursor >= 0:
        kind, value = tokens[cursor]
        if kind == "punct" and value in _SKIP_BACKWARD_PUNCT:
            cursor -= 1
            continue
        if kind == "ident" and value in _SKIP_BACKWARD_IDENTS:
            cursor -= 1
            continue
        break
    if cursor >= 0 and tokens[cursor][0] == "ident":
        return tokens[cursor][1]
    return None


def _test_base_aliases(tokens: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Resolve local identifiers that alias a test-framework global.

    ``const aliasIt = it; aliasIt["skip"](...)`` reaches exactly the same
    disabling capability as ``it["skip"](...)``, so a respelled-skip scan that
    only recognises the literal ``describe``/``it``/``test`` receiver is blind to
    it.  This folds ``ident = <base|alias>`` bindings (declarations and plain
    assignments) to a fixpoint, so any depth of renaming resolves back to the
    original global.  Property assignments (``obj.a = it``) are excluded so a
    coincidental member name is never promoted to a test receiver.
    """

    aliases: dict[str, str] = {}
    total = len(tokens)
    for _ in range(8):
        changed = False
        for index in range(1, total - 1):
            kind, value = tokens[index]
            if kind != "ident" or tokens[index + 1] != ("punct", "="):
                continue
            if tokens[index - 1] == ("punct", "."):
                continue
            following = tokens[index + 2] if index + 2 < total else ("", "")
            if following[0] != "ident":
                continue
            # The RHS must be the whole binding: ``a = it`` aliases the global,
            # but ``a = it.foo``/``a = it()``/``a = obj.it`` do not (the receiver
            # would then be a member or call result, not the framework global).
            successor = tokens[index + 3] if index + 3 < total else ("", "")
            if successor in (("punct", "."), ("punct", "["), ("punct", "(")):
                continue
            target = aliases.get(following[1], following[1])
            if target in _TEST_FRAMEWORK_BASES and aliases.get(value) != target:
                aliases[value] = target
                changed = True
        if not changed:
            break
    return aliases


def _respelled_disabled_markers(text: str) -> int:
    """Count test-framework ``.skip``/``.only`` *calls* the dot-call regex misses.

    ``(it as any)["skip"](...)``, ``it["skip"](...)``, ``it[bind]`` (a folded
    constant), and ``it?.skip(...)`` all reach the same disabling capability as
    the literal ``it.skip(`` the fixed regexes count.  The scan is token/value
    based so it is unaffected by identifier respelling.
    """

    tokens = _tokenize(text)
    if not tokens:
        return 0
    env = _fold_string_constants(tokens)
    aliases = _test_base_aliases(tokens)
    count = 0
    total = len(tokens)
    index = 0
    while index < total:
        kind, value = tokens[index]
        prop = None
        after = None
        if kind == "punct" and value == ".":
            following = tokens[index + 1] if index + 1 < total else ("", "")
            if following[0] == "ident" and following[1] in _TEST_PROPERTY_MARKERS:
                prop = following[1]
                after = index + 2
        elif kind == "punct" and value == "[":
            resolved, cursor = _parse_concat(list(tokens), index + 1, env)
            if (
                isinstance(resolved, str)
                and resolved in _TEST_PROPERTY_MARKERS
                and cursor < total
                and tokens[cursor] == ("punct", "]")
            ):
                prop = resolved
                after = cursor + 1
        if prop is not None:
            base = _skip_base_identifier(tokens, index)
            resolved = aliases.get(base, base) if base is not None else None
            if (
                after is not None
                and after < total
                and tokens[after] == ("punct", "(")
                and resolved in _TEST_FRAMEWORK_BASES
            ):
                count += 1
            index = after if after is not None else index + 1
            continue
        index += 1
    return count


def _marker_counts(root: Path) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        if not _is_test(relative):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _strip_comments(raw)
        counts["skip"] += sum(
            1 for pattern in _SKIP_MARKERS[:1] for _ in pattern.finditer(text)
        )
        counts["xfail"] += sum(
            1 for pattern in (_SKIP_MARKERS[1],) for _ in pattern.finditer(text)
        )
        # ``.skip(`` / ``.only(`` reached through bracket access, a TypeScript
        # cast or optional chaining escapes the fixed dot-call patterns; the
        # token scan covers those without double counting the literal forms
        # (both detectors require a ``describe|it|test`` receiver).
        counts["disabled"] += sum(
            1
            for pattern in (_SKIP_MARKERS[4], _SKIP_MARKERS[5], _DISABLED_ALIAS)
            for _ in pattern.finditer(text)
        )
        counts["disabled"] += _respelled_disabled_markers(raw)
    return counts


def build_source_test_baseline(root: Path) -> dict[str, object]:
    """Freeze source-visible test identities and marker counts for a Case."""

    files = {
        relative: {"sha256": _digest(path)}
        for relative, path in sorted(_regular_files(root).items())
        if _is_test(relative)
    }
    minimums = {
        relative: sorted(
            _assertion_signatures(path.read_text(encoding="utf-8", errors="ignore"))
        )
        for relative, path in sorted(_regular_files(root).items())
        if _is_test(relative)
    }
    return {
        "schema_version": "migration-source-test-baseline:v1",
        "files": files,
        "marker_counts": _marker_counts(root),
        "source_assertion_minimums": minimums,
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
    # ``allowed_equivalent_test_paths`` exists so a candidate may adapt imports
    # and bootstrap lines, not so it may weaken assertions.  For an allowlisted
    # file whose bytes changed we still require every assertion the migration
    # started from to remain present (a superset is allowed).
    minimums = baseline.get("source_assertion_minimums", {})
    minimums = minimums if isinstance(minimums, Mapping) else {}
    weakened: list[dict[str, object]] = []
    for relative, record in sorted(baseline_files.items()):
        if (
            relative not in current
            or relative not in allowed
            or not isinstance(record, Mapping)
            or record.get("sha256") == _digest(current[relative])
        ):
            continue
        expected = minimums.get(relative)
        if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)) or not expected:
            continue
        expected_counts = Counter(_canonical_assertion(str(item)) for item in expected)
        protected_names = set(re.findall(r"\b[A-Za-z_$][\w$]*\b", " ".join(str(item) for item in expected)))
        candidate_text = current[relative].read_text(encoding="utf-8", errors="ignore")
        actual_counts = Counter(
            _canonical_assertion(item) for item in _assertion_signatures(
                _expand_test_aliases(candidate_text, protected_names)
            )
        )
        absent = sorted(
            signature
            for signature, count in expected_counts.items()
            if actual_counts[signature] < count
        )
        if absent:
            weakened.append({"path": relative, "missing_assertions": absent})
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
        evidence={"missing": missing, "changed": changed, "weakened": weakened},
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
    raw_deceptive_hits = scan_residue_rules(candidate_root, deceptive_rules)
    rule_meta = {
        str(rule.get("id")): rule
        for rule in deceptive_rules
        if isinstance(rule, Mapping)
    }

    def _contract_obliged(hit: Mapping[str, Any]) -> bool:
        rule = rule_meta.get(str(hit.get("rule_id")))
        return bool(rule and rule.get("contract_obliged") is True)

    # `contract_obliged` rules match text the public contract itself mandates,
    # so they only count as a fallback when an independent implementation
    # residue corroborates them; a lone hit is a non-cheating diagnostic.
    contract_obliged_hits = [hit for hit in raw_deceptive_hits if _contract_obliged(hit)]
    independent_hits = [hit for hit in raw_deceptive_hits if not _contract_obliged(hit)]
    deceptive_hits = raw_deceptive_hits if independent_hits else []
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
        "contract_obliged_hits": contract_obliged_hits,
        "deceptive_raw_hits": raw_deceptive_hits,
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
