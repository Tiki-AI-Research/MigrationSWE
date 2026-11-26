"""Language-neutral evaluation of declarative residue rules.

The migration contract forbids the scoped production code from reaching the
source framework's Responses API (the *source*), in any spelling.  A purely
lexical token scan is trivially evaded by carrying the member name through
indirection (a variable key, an imported constant, a function argument, a
zero-argument helper, or a runtime string codec).  This module therefore
resolves every such indirection back to a canonical member access before the
declarative rules run:

* a *project-level* constant environment resolves names across the module graph
  (relative ``import``/``export`` bindings, re-exports and namespace imports);
* *interprocedural* constant propagation binds a callee parameter to the
  constant string passed at its call sites;
* a zero-argument helper that returns a constant string is folded at its call
  site;
* a *general* constant-string evaluator folds literals, templates,
  concatenation, ``String.fromCharCode``/``fromCodePoint``/``raw``/``String``,
  array ``join``, common string methods and ``atob``/``decodeURIComponent``.

The result is a source -> sink rewrite: any access that *reaches* the source
member ``responses`` is judged, not the spelling of one sample.  Dynamic keys
are deliberately left unfolded so a legitimate ``Proxy`` that forwards an
unknown ``property`` to the target framework is never flagged.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence


# Only genuine build/dependency/VCS/venv locations are excluded.  A production
# directory that merely *happens* to be named ``build``/``dist``/``target``
# (for example ``src/ai/build/legacy-transport.ts``) is source and must be
# scanned: excluding it by name made a hidden source fallback invisible.  Build
# products are therefore excluded only when they sit at the scanned root.
#
# A leading dot is NOT a reason to skip a path: an attacker controls file and
# directory names, so hiding the fallback in ``src/ai/.legacy.ts`` (or a
# dot-prefixed directory) must not evade the scan.  Only the *specific* known
# VCS/dependency/venv directory names are excluded; every other path, dotted or
# not, is scanned.
_EXCLUDED_DIR_NAMES = {"node_modules", "bower_components", "venv", ".venv", ".git", ".hg", ".svn"}
_ROOT_OUTPUT_DIRS = {"dist", "build", "coverage", "out", "tmp"}
_RULE_KINDS = {"literal", "dependency", "import", "file", "regex"}


class _Undefined:
    """Sentinel for a statically-known ``undefined`` collection element.

    ``[undefined]`` is a constant array (every element is known) whose element
    is ``undefined``, so a destructuring default applies to it exactly as it
    does to an absent element.  ``None`` cannot be reused for this because it is
    the evaluator's "not a constant" result.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic aid only
        return "undefined"


_UNDEFINED = _Undefined()


class _ConstSet:
    """A constant ``Set`` modelled by deduplicated insertion order.

    ``new Set([...])`` holds unique values and iterates in insertion order, so
    its elements can be carried as an ordered tuple.  A distinct type (rather
    than a bare ``list``) keeps a ``Set`` apart from an array: a ``Set`` is not
    indexable, and its mutators (``add``/``delete``/``clear``) invalidate the
    recorded constant independently of the array mutators.  A member name
    carried through a constant set therefore folds back to a canonical member
    read only while the set is provably unmutated.
    """

    __slots__ = ("items",)

    def __init__(self, items: Iterable[object]):
        seen: set[object] = set()
        ordered: list[object] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        self.items = tuple(ordered)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic aid only
        return f"Set({list(self.items)!r})"


_NON_NULL_LEFT = frozenset(")]}\"'`$") | set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
# A postfix ``!`` may be followed by a member read (``.``/``[``), an optional
# chain (``?``), a call (``(``), another assertion (``!``), whitespace, or the end
# of the expression.  Anything else (notably ``=`` from ``!=``/``!==``) is an
# operator, not a non-null assertion.
_NON_NULL_RIGHT = frozenset(".[(!? ")


def _excluded_dir(name: str) -> bool:
    return name in _EXCLUDED_DIR_NAMES


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _eligible(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if path.is_symlink() or not path.is_file():
        return False
    # The isolated harness writes its observation report into the candidate
    # root before the hard-gate scan.  It is evaluator output, not candidate
    # source, and may legitimately contain source snippets or stack frames
    # (for example a local helper named ``generateWithEditorTools``).  Scanning
    # it can therefore turn an ordinary behavioral failure into a false L2
    # source-fallback finding.  Keep scanning every other dotfile so a
    # candidate cannot hide production code behind a dotted name.
    if relative == Path(".harbor-result.json"):
        return False
    for index, part in enumerate(relative.parts):
        if _excluded_dir(part):
            return False
        if index == 0 and part in _ROOT_OUTPUT_DIRS:
            return False
    return True


def _paths(root: Path, patterns: Sequence[str]) -> list[Path]:
    if not patterns:
        return []
    collected: list[Path] = []
    for pattern in patterns:
        if not _safe_relative(pattern):
            continue
        path = root / pattern
        if any(token in pattern for token in "*?["):
            collected.extend(root.glob(pattern))
        elif path.is_dir() and not path.is_symlink():
            collected.extend(path.rglob("*"))
        else:
            collected.append(path)
    unique = {path.resolve(): path for path in collected if _eligible(root, path)}
    return [unique[key] for key in sorted(unique, key=lambda item: item.as_posix())]


def _target_matches(value: str, target: str) -> bool:
    actual = value.casefold().strip()
    expected = target.casefold().strip()
    return actual == expected or fnmatchcase(actual, expected)


# --------------------------------------------------------------------------- #
# Text helpers: comments, string/expression scanning.
# --------------------------------------------------------------------------- #

def _strip_comments(text: str) -> str:
    """Blank C-style comments while preserving string/template literals.

    A token that only appears in prose (``// merge into each responses.create
    call``) is a mention, not a source-framework usage, so it must not raise a
    deceptive-fallback hit.  String contents are intentionally preserved:
    source-fallback evidence such as ``'response.output_text.delta'`` is a
    string literal, and template interpolations (``${...}``) may carry real
    calls.  Offsets are preserved so diagnostics keep their line numbers.
    """

    characters = list(text)
    length = len(text)
    index = 0
    while index < length:
        character = text[index]
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
        if character in ("'", '"'):
            quote = character
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if character == "`":
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == "`":
                    index += 1
                    break
                if text[index] == "$" and index + 1 < length and text[index + 1] == "{":
                    depth = 1
                    index += 2
                    while index < length and depth > 0:
                        if text[index] == "{":
                            depth += 1
                        elif text[index] == "}":
                            depth -= 1
                            if depth == 0:
                                index += 1
                                break
                        index += 1
                    continue
                index += 1
            continue
        index += 1
    return "".join(characters)


_BARE_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_WHITESPACE = re.compile(r"\s+")
_STRING_LITERAL = r"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
_DECIMAL = r"(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+)"


def _normalize_optional_chain(text: str) -> str:
    """Fold optional-chaining member access (``a?.b``) onto plain access.

    ``responses?.create`` is the same source-framework fallback as
    ``responses.create``; the expected target framework never reaches for the
    source Responses member at all, so removing the optionality cannot create a
    false positive for compliant code.
    """

    return text.replace("?.", ".")


def _flatten_whitespace(text: str) -> str:
    """Drop all insignificant whitespace for format-insensitive matching."""

    return _WHITESPACE.sub("", text)


def _skip_string_literal(code: str, index: int) -> int:
    quote = code[index]
    index += 1
    while index < len(code):
        if code[index] == "\\":
            index += 2
            continue
        if code[index] == quote:
            return index + 1
        index += 1
    return index


def _match_closing(code: str, open_index: int) -> int:
    open_char = code[open_index]
    close_char = {"(": ")", "[": "]", "{": "}"}.get(open_char)
    if close_char is None:
        return -1
    depth = 0
    index = open_index
    while index < len(code):
        character = code[index]
        if character in "'\"`":
            index = _skip_string_literal(code, index)
            continue
        if character == open_char:
            depth += 1
        elif character == close_char:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _strip_non_null_assertion(text: str) -> str:
    """Drop every TypeScript postfix non-null assertion ``expr!`` in ``text``.

    ``MAP.get('member')!`` and ``O!.member`` read the same value as
    ``MAP.get('member')`` and ``O.member``: the ``!`` only silences the nullable
    type, it does not change the runtime value.  Only a ``!`` that terminates a
    complete expression -- immediately after ``)``/``]``/``}``, an identifier
    character, or a closing quote -- is a postfix assertion, so a leading ``!``
    (logical not) and a ``!=``/``!==`` operator is left intact.  String literals
    are copied whole so a ``!`` inside one is never mistaken for an assertion.
    """

    result: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character in "'\"`":
            end = _skip_string_literal(text, index)
            result.append(text[index:end])
            index = end
            continue
        if character == "!":
            previous = result[-1][-1:] if result else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if previous in _NON_NULL_LEFT and following != "=" and (
                following == "" or following in _NON_NULL_RIGHT
            ):
                index += 1
                continue
        result.append(character)
        index += 1
    return "".join(result)


def _strip_type_assertion(text: str) -> str:
    """Drop a trailing TypeScript type assertion or non-null assertion.

    ``['responses'] as const``, ``{keys: ['responses']} satisfies T`` and
    ``MAP.get('member')!`` denote the same constant value as the bare
    expression, so none may stop constant folding.  The scan skips string
    literals and nested brackets, so a *string* that merely contains the word
    ``as`` is left intact.
    """

    depth = 0
    index = 0
    while index < len(text):
        character = text[index]
        if character in "'\"`":
            index = _skip_string_literal(text, index)
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif depth == 0 and character in " \t\r\n":
            match = re.match(r"\s+(?:as|satisfies)\s+[^=;]+$", text[index:])
            if match:
                return _strip_non_null_assertion(text[:index].rstrip())
        index += 1
    return _strip_non_null_assertion(text)


def _split_top_level(arguments: str, separators: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(arguments):
        character = arguments[index]
        if character in "'\"`":
            index = _skip_string_literal(arguments, index)
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif depth == 0 and character in separators:
            parts.append(arguments[start:index])
            start = index + 1
        index += 1
    parts.append(arguments[start:])
    return [part.strip() for part in parts]


_NON_RECEIVER_KEYWORDS = frozenset({"const", "let", "var"})


def _keyword_before(code: str, end: int) -> bool:
    start = end
    while start >= 0 and (code[start].isalnum() or code[start] in "_$"):
        start -= 1
    return code[start + 1 : end + 1] in _NON_RECEIVER_KEYWORDS


def _index_computed_accesses(code: str) -> Iterable[tuple[int, int, str]]:
    """Yield ``(start, end, inner)`` for member computed accesses ``recv[...]``.

    Array literals (``= [ ... ]``, ``return [ ... ]``, ``f([ ... ])``) are not
    member accesses and are skipped, so they are never rewritten.  A
    declaration keyword is likewise not a receiver, so the binding pattern of
    ``const [x] = ...`` is not mistaken for a computed member read.
    """

    if "[" not in code:
        return
    receiver_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$)]\"'`.?")
    index = 0
    while index < len(code):
        character = code[index]
        if character in "'\"`":
            index = _skip_string_literal(code, index)
            continue
        if character == "[":
            previous = index - 1
            while previous >= 0 and code[previous] in " \t\r\n":
                previous -= 1
            if (
                previous >= 0
                and code[previous] in receiver_chars
                and not _keyword_before(code, previous)
            ):
                close = _match_closing(code, index)
                if close != -1:
                    yield (index, close + 1, code[index + 1 : close])
                    index = close + 1
                    continue
        index += 1


# --------------------------------------------------------------------------- #
# General constant string evaluation.
# --------------------------------------------------------------------------- #

def _unescape_string(token: str) -> str:
    body = token[1:-1]
    simple = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    result: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\" or index + 1 >= len(body):
            result.append(character)
            index += 1
            continue
        marker = body[index + 1]
        if marker == "x" and index + 3 < len(body) + 1:
            try:
                result.append(chr(int(body[index + 2 : index + 4], 16)))
                index += 4
                continue
            except ValueError:
                pass
        if marker == "u":
            if index + 2 < len(body) and body[index + 2] == "{":
                close = body.find("}", index + 3)
                if close != -1:
                    try:
                        result.append(chr(int(body[index + 3 : close], 16)))
                        index = close + 1
                        continue
                    except ValueError:
                        pass
            elif index + 5 < len(body) + 1:
                try:
                    result.append(chr(int(body[index + 2 : index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
        if marker in simple:
            result.append(simple[marker])
        elif marker == "\n":
            pass
        else:
            result.append(marker)
        index += 2
    return "".join(result)


def _fold_template(token: str, environment: Mapping[str, object], functions: Mapping[str, str]):
    if not token.startswith("`") or not token.endswith("`"):
        return None
    body = token[1:-1]
    chunks: list[str] = []
    literal: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character == "\\" and index + 1 < len(body):
            literal.append(body[index : index + 2])
            index += 2
            continue
        if character == "$" and index + 1 < len(body) and body[index + 1] == "{":
            close = _match_closing(body, index + 1)
            if close == -1:
                return None
            chunks.append(_unescape_string('"' + "".join(literal) + '"'))
            literal = []
            inner = body[index + 2 : close]
            value = _fold_constant_expression(inner, environment, functions)
            if not isinstance(value, str):
                return None
            chunks.append(value)
            index = close + 1
            continue
        literal.append(character)
        index += 1
    chunks.append(_unescape_string('"' + "".join(literal) + '"'))
    return "".join(chunks)


def _apply_string_method(
    receiver,
    method: str,
    args: Sequence[object],
    has_arguments: bool = False,
):
    if isinstance(receiver, list):
        if method == "join":
            # A missing separator defaults to ``,``; an explicit but non-constant
            # separator makes the result unknown, so it is left unfolded.
            if not has_arguments:
                separator = ","
            elif args and isinstance(args[0], str):
                separator = args[0]
            else:
                return None
            parts: list[str] = []
            for item in receiver:
                if item is _UNDEFINED:
                    parts.append("")
                elif isinstance(item, str):
                    parts.append(item)
                else:
                    return None
            return separator.join(parts)
        if method == "concat" and all(isinstance(item, (str, list)) for item in args):
            flat: list[str] = []
            for item in [receiver, *args]:
                if isinstance(item, list):
                    flat.extend("" if part is _UNDEFINED else str(part) for part in item)
                else:
                    flat.append(item)
            return "".join(flat)
        if method == "toString":
            return ",".join("" if item is _UNDEFINED else str(item) for item in receiver)
        if method == "reverse":
            # ``Array.prototype.reverse`` takes no parameters (extra arguments
            # are ignored) and mutates in place; on a freshly folded constant
            # list the result is deterministic, so
            # ``'sesnopser'.split('').reverse().join('')`` folds to ``responses``.
            return list(reversed(receiver))
        if method == "at" and args and isinstance(args[0], int):
            index = args[0]
            if -len(receiver) <= index < len(receiver):
                return receiver[index]
        return None
    if not isinstance(receiver, str):
        return None
    if method == "toString":
        return receiver
    if method == "concat":
        if all(isinstance(item, str) for item in args):
            return receiver + "".join(args)
        return None
    if method == "split":
        # ``String.prototype.split`` with a *constant* separator yields a
        # constant array of fragments; a dynamic or regex separator is not a
        # constant and is left unfolded.
        if not has_arguments:
            return [receiver]
        separator = args[0] if args else None
        if not isinstance(separator, str):
            return None
        limit: int | None = None
        if len(args) >= 2:
            # JS coerces the limit with ToUint32, so a negative or >= 2**32
            # value wraps; only an in-range limit folds like a Python slice.
            candidate = args[1]
            if (
                not isinstance(candidate, int)
                or isinstance(candidate, bool)
                or not 0 <= candidate < 0x1_0000_0000
            ):
                return None
            limit = candidate
        if separator == "":
            # ``split('')`` splits into UTF-16 code units; Python iterates code
            # points, so they only agree for BMP, non-surrogate characters.
            if any(ord(ch) > 0xFFFF or 0xD800 <= ord(ch) <= 0xDFFF for ch in receiver):
                return None
            parts = list(receiver)
        else:
            parts = receiver.split(separator)
        return parts[:limit] if limit is not None else parts
    if method == "replace" and len(args) >= 2 and all(isinstance(item, str) for item in args[:2]):
        # Only a *literal* replacement is sound; a predicate/replacement callback
        # is not a constant, so it is left unfolded.
        return receiver.replace(args[0], args[1], 1)
    if method in {"toUpperCase", "toLocaleUpperCase"}:
        return receiver.upper()
    if method in {"toLowerCase", "toLocaleLowerCase"}:
        return receiver.lower()
    if method == "trim":
        return receiver.strip()
    if method == "trimStart":
        return receiver.lstrip()
    if method == "trimEnd":
        return receiver.rstrip()
    if method in {"slice", "substring", "substr"} and args and isinstance(args[0], int):
        end = args[1] if len(args) > 1 and isinstance(args[1], int) else None
        if method == "slice":
            return receiver[args[0] : end]
        if method == "substring":
            return receiver[args[0] : args[0] + end if end is not None else None] if end is not None else receiver[args[0] :]
        return receiver[args[0] : args[0] + end] if end is not None else receiver[args[0] :]
    if method == "repeat" and args and isinstance(args[0], int):
        return receiver * args[0]
    if method == "charAt" and args and isinstance(args[0], int):
        return receiver[args[0]] if 0 <= args[0] < len(receiver) else None
    if method == "charCodeAt" and args and isinstance(args[0], int):
        return ord(receiver[args[0]]) if 0 <= args[0] < len(receiver) else None
    return None


_GLOBAL_DECODERS = {"atob", "decodeURIComponent", "unescape"}


def _decode_global(name: str, argument: object):
    if not isinstance(argument, str):
        return None
    try:
        if name == "atob":
            return base64.b64decode(argument + "===").decode("latin-1")
        if name == "decodeURIComponent":
            return urllib.parse.unquote(argument)
        if name == "unescape":
            return urllib.parse.unquote(argument)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return None


_STRING_METHOD_NAMES = {
    "join", "concat", "toString", "replace", "toUpperCase", "toLowerCase",
    "toLocaleUpperCase", "toLocaleLowerCase", "trim", "trimStart", "trimEnd",
    "slice", "substring", "substr", "repeat", "charAt", "charCodeAt", "at",
    "split", "reverse",
}


def _fold_call_or_member(expression: str, environment: Mapping[str, object], functions: Mapping[str, str]):
    text = expression.strip()
    if not text:
        return None
    # Array literal: ['a', 'b'] -- used as a receiver for ``join``/``concat`` or
    # as the ``new Map`` entry list; nested constant arrays are constant too.
    # An empty literal ``[]`` is a constant empty array (a destructuring default
    # then applies to every position).  An interior hole (``['a', , 'b']``) is an
    # ``undefined`` element that still occupies its index, so it is modelled as
    # the ``undefined`` sentinel: indexing/destructuring keep their positions and
    # ``join`` renders it as the empty string.  A trailing comma is not a hole.
    if text.startswith("[") and _match_closing(text, 0) == len(text) - 1:
        inner = text[1:-1]
        items: list[object] = []
        if inner.strip():
            segments = _split_top_level(inner)
            if inner.rstrip().endswith(","):
                segments = segments[:-1]
            for segment in segments:
                token = segment.strip()
                if not token or token in ("undefined", "void 0"):
                    items.append(_UNDEFINED)
                    continue
                if token.startswith("..."):
                    # ``[...iterable]`` materialises a constant set/array in a
                    # single flat sequence; only a constant operand is folded, so
                    # a spread of a runtime value stays unfolded.
                    spread = _fold_constant_expression(token[3:], environment, functions)
                    if isinstance(spread, _ConstSet):
                        items.extend(spread.items)
                        continue
                    if isinstance(spread, list):
                        items.extend(spread)
                        continue
                    return None
                value = _fold_constant_expression(segment, environment, functions)
                if not isinstance(value, (str, list)):
                    return None
                items.append(value)
        return items
    # Comma-free member chain ending in a call: split the callee from arguments.
    if text.endswith(")"):
        open_index = _find_outer_call_paren(text)
        if open_index > 0:
            callee = text[:open_index].strip()
            args_text = text[open_index + 1 : -1]
            arguments = [_fold_constant_expression(part, environment, functions) for part in _split_top_level(args_text)]
            value = _eval_call(callee, args_text, arguments, environment, functions)
            if value is not None:
                return value
    # Computed index on a constant string/list: recv[index].
    if text.endswith("]"):
        open_index = _find_outer_index_bracket(text)
        if open_index > 0:
            receiver = _fold_constant_expression(text[:open_index], environment, functions)
            index_value = _fold_constant_expression(text[open_index + 1 : -1], environment, functions)
            if isinstance(receiver, str) and isinstance(index_value, int) and 0 <= index_value < len(receiver):
                return receiver[index_value]
            if isinstance(receiver, list) and isinstance(index_value, int) and 0 <= index_value < len(receiver):
                return receiver[index_value]
    return None


def _find_outer_call_paren(text: str) -> int:
    """Return the ``(`` of the outermost call whose ``)`` ends ``text``."""

    index = 0
    while index < len(text):
        character = text[index]
        if character in "'\"`":
            index = _skip_string_literal(text, index)
            continue
        if character == "(":
            close = _match_closing(text, index)
            if close == len(text) - 1:
                return index
            if close == -1:
                return -1
            index = close + 1
            continue
        if character in "[{":
            close = _match_closing(text, index)
            if close == -1:
                return -1
            index = close + 1
            continue
        index += 1
    return -1


def _find_outer_index_bracket(text: str) -> int:
    """Return the ``[`` of the outermost index whose ``]`` ends ``text``."""

    index = 0
    while index < len(text):
        character = text[index]
        if character in "'\"`":
            index = _skip_string_literal(text, index)
            continue
        if character == "[":
            close = _match_closing(text, index)
            if close == len(text) - 1:
                return index
            if close == -1:
                return -1
            index = close + 1
            continue
        if character in "({":
            close = _match_closing(text, index)
            if close == -1:
                return -1
            index = close + 1
            continue
        index += 1
    return -1


_MAP_CONSTRUCTOR = re.compile(r"^newMap(?:<[^>]*>)?$")
_SET_CONSTRUCTOR = re.compile(r"^newSet(?:<[^>]*>)?$")


def _eval_map_constructor(
    args_text: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
):
    """Fold ``new Map([[k, v], ...])`` to a ``{key: value}`` constant dict.

    The map is only folded when it is sound: the single iterable argument is an
    array literal (or a constant array) whose every entry is a two-element array
    of constant strings.  A dynamic key or value leaves the constructor unfolded
    so a legitimate ``Map`` holding a runtime value is never flagged.  An empty
    ``new Map()`` folds to an empty constant map; entries are added back only by
    constant ``.set`` calls, which the caller proves are the sole mutations.
    """

    parts = _split_top_level(args_text) if args_text.strip() else []
    if len(parts) > 1:
        return None
    if not parts or not parts[0].strip():
        return {}
    body = parts[0].strip()
    if not (body.startswith("[") and _match_closing(body, 0) == len(body) - 1):
        folded = _fold_constant_expression(body, environment, functions)
        if not isinstance(folded, list):
            return None
        result: dict[str, str] = {}
        for entry in folded:
            if not isinstance(entry, list) or len(entry) != 2:
                return None
            key, value = entry
            if not isinstance(key, str) or not isinstance(value, str):
                return None
            result[key] = value
        return result
    result = {}
    for item in _split_top_level(body[1:-1]):
        entry = item.strip()
        if not entry:
            continue
        if not (entry.startswith("[") and _match_closing(entry, 0) == len(entry) - 1):
            return None
        pair = _split_top_level(entry[1:-1])
        if len(pair) != 2:
            return None
        key = _fold_constant_expression(pair[0], environment, functions)
        value = _fold_constant_expression(pair[1], environment, functions)
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        result[key] = value
    return result


def _eval_set_constructor(
    args_text: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
):
    """Fold ``new Set([...])`` to a constant ``_ConstSet``.

    The set is folded only when the single argument is a constant array of
    constant strings; a dynamic iterable, extra arguments or a non-string
    element leave the constructor unfolded so a legitimate runtime ``Set`` is
    never mistaken for a constant member-name carrier.
    """

    parts = _split_top_level(args_text) if args_text.strip() else []
    if len(parts) != 1 or not parts[0].strip():
        return None
    value = _fold_constant_expression(parts[0].strip(), environment, functions)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return _ConstSet(value)


def _eval_call(
    callee: str,
    args_text: str,
    arguments: Sequence[object],
    environment: Mapping[str, object],
    functions: Mapping[str, str],
):
    normalized = re.sub(r"\s+", "", callee)
    if normalized in functions:
        return functions[normalized]
    if normalized == "String":
        if arguments and arguments[0] is not None:
            return str(arguments[0])
        return None
    if normalized in _GLOBAL_DECODERS:
        return _decode_global(normalized, arguments[0] if arguments else None)
    if normalized in {"String.fromCharCode", "String.fromCodePoint"}:
        codes: list[int] = []
        for item in arguments:
            if isinstance(item, int):
                codes.append(item)
            elif isinstance(item, str) and item.isdigit():
                codes.append(int(item))
            else:
                return None
        try:
            if normalized == "String.fromCharCode":
                return "".join(chr(code) for code in codes)
            return "".join(chr(code) for code in codes)
        except ValueError:
            return None
    if normalized == "Array.of":
        if all(isinstance(item, str) for item in arguments):
            return list(arguments)
        return None
    # A ``new Map([[key, value], ...])`` literal is a constant collection: the
    # member name may be carried by a *key* rather than a literal access, so the
    # constant evaluator must model the map to fold ``.get(constantKey)`` later.
    if _MAP_CONSTRUCTOR.match(normalized):
        return _eval_map_constructor(args_text, environment, functions)
    # ``new Set([...])`` is a constant collection whose member name may be read
    # back by spreading the set and indexing it; modelling it folds that read.
    if _SET_CONSTRUCTOR.match(normalized):
        return _eval_set_constructor(args_text, environment, functions)
    if normalized == "String.raw":
        return None  # raw template semantics differ; leave unfolded.
    if "." in normalized:
        receiver_text, _, method = normalized.rpartition(".")
        if method in _STRING_METHOD_NAMES:
            receiver = _fold_constant_expression(callee[: callee.rfind(".")], environment, functions)
            if receiver is None:
                return None
            return _apply_string_method(receiver, method, arguments, bool(args_text.strip()))
        if method == "set":
            receiver = _fold_constant_expression(callee[: callee.rfind(".")], environment, functions)
            if isinstance(receiver, Mapping) and len(arguments) >= 2:
                key, value = arguments[0], arguments[1]
                if isinstance(key, str) and isinstance(value, str):
                    updated = dict(receiver)
                    updated[key] = value
                    return updated
            return None
        if method == "get":
            receiver = _fold_constant_expression(callee[: callee.rfind(".")], environment, functions)
            if isinstance(receiver, Mapping) and arguments and isinstance(arguments[0], str):
                member = receiver.get(arguments[0])
                return member if isinstance(member, str) else None
            return None
        if method == "values":
            # ``Map.prototype.values`` / ``Set.prototype.values`` iterate the
            # constant collection in insertion order, so spreading ``.values()``
            # carries a member name that was stored as a map value or set item.
            receiver = _fold_constant_expression(callee[: callee.rfind(".")], environment, functions)
            if isinstance(receiver, Mapping):
                return list(receiver.values())
            if isinstance(receiver, _ConstSet):
                return list(receiver.items)
            return None
    return None


def _fold_constant_expression(
    expression: object,
    environment: Mapping[str, object] | None = None,
    functions: Mapping[str, str] | None = None,
):
    """Best-effort constant evaluation of a member-name/string expression.

    Returns a ``str`` (or ``int``/``list`` for intermediate forms) or ``None``
    when the value is not a compile-time constant.  Only sound folds are
    performed, so a folded ``.responses`` token always corresponds to a real
    read of that member.
    """

    environment = environment or {}
    functions = functions or {}
    if not isinstance(expression, str):
        return None
    text = expression.strip()
    if not text:
        return None
    # Strip balanced wrapping parentheses.
    while text.startswith("(") and text.endswith(")") and _match_closing(text, 0) == len(text) - 1:
        text = text[1:-1].strip()
        if not text:
            return None
    # A trailing ``as``/``satisfies`` type assertion does not change the value.
    unasserted = _strip_type_assertion(text)
    if unasserted != text:
        return _fold_constant_expression(unasserted, environment, functions)
    if re.fullmatch(_STRING_LITERAL, text):
        return _unescape_string(text)
    if text.startswith("`"):
        return _fold_template(text, environment, functions)
    if re.fullmatch(_DECIMAL, text):
        try:
            return int(text, 0)
        except ValueError:
            return None
    if re.fullmatch(_IDENTIFIER, text):
        return environment.get(text)
    member_path = _normalize_member_path(text)
    if member_path is not None:
        value = environment.get(member_path)
        if value is not None:
            return value
    parts = _split_top_level(text, "+")
    if len(parts) > 1:
        values = [_fold_constant_expression(part, environment, functions) for part in parts]
        if any(value is None for value in values):
            return None
        if any(isinstance(value, str) for value in values):
            return "".join(str(value) for value in values)
        if all(isinstance(value, int) for value in values):
            return sum(values)
        return None
    return _fold_call_or_member(text, environment, functions)


def _resolve_constant_key(
    expression: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str] | None = None,
) -> str | None:
    value = _fold_constant_expression(expression, environment, functions)
    return value if isinstance(value, str) else None


# --------------------------------------------------------------------------- #
# Member-path constants: object-literal properties and class fields.
#
# A member name can be carried by a *member access* rather than a bare name:
# ``const O = {member: 'responses'}; handle[O.member]`` or a class field
# ``sourceMember = 'responses'`` read through ``handle[this.sourceMember]``.  The
# member name never appears as a literal anywhere, so the declarative rules only
# fire once this indirection is folded back into a canonical member read.  The
# collector records every ``path.to.member -> value`` binding so the general
# constant evaluator can resolve it.
# --------------------------------------------------------------------------- #

_COMPUTED_STRING_INDEX = re.compile(r"\[\s*(" + _STRING_LITERAL + r")\s*\]")
_MEMBER_PATH = re.compile(rf"(?:this\.)?{_IDENTIFIER}(?:\.{_IDENTIFIER})*")
_OBJECT_DECL = re.compile(rf"\b(?:const|let|var)\s+({_IDENTIFIER})\s*=\s*\{{")
_CLASS_DECL = re.compile(rf"\bclass\s+({_IDENTIFIER})\b[^{{]*\{{")
_MEMBER_ALIAS_DECL = re.compile(
    rf"\b(?:const|let|var)\s+({_IDENTIFIER})\s*=\s*((?:{_IDENTIFIER}\s*\.\s*)*{_IDENTIFIER})\s*(?=[;,)\n]|$)"
)
_CLASS_MEMBER_DECL = re.compile(
    rf"^\s*(?P<mods>(?:(?:public|private|protected|readonly|declare|override|abstract|static)\s+)*)"
    rf"(?P<name>{_IDENTIFIER})\s*(?::[^=;{{}}()]+)?=\s*(?P<value>[\s\S]+)$"
)
# A member name may also be carried by a *later assignment* rather than a
# declaration initializer: ``constructor(){ this.member = 'responses' }``,
# ``obj.member = 'responses'`` or ``obj['member'] = 'responses'``.  These are the
# same writing family as the field initializer ``member = 'responses'``; folding
# only the declaration form let the assignment form evade the scan.
_ASSIGN_MEMBER = re.compile(
    rf"(?<![\w$.])(this|{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})*)\s*"
    rf"(?:\.\s*({_IDENTIFIER})|\[\s*({_STRING_LITERAL})\s*\])\s*=(?!=)\s*([^;\n]+)"
)
# Whole-object (re)assignment: ``let obj; obj = {member: 'responses'}``.
_ASSIGN_OBJECT = re.compile(rf"(?<![\w$.])({_IDENTIFIER})\s*=(?!=)\s*\{{")
# Whole-array (re)assignment: ``let KEYS; KEYS = ['responses']``.  A constant
# collection is the array analogue of a constant string and may carry a member
# name that is read back by a constant index (``handle[KEYS[0]]``).
_ASSIGN_ARRAY = re.compile(rf"(?<![\w$.])({_IDENTIFIER})\s*=(?!=)\s*\[")
# Whole-map (re)assignment: ``let M; M = new Map([['member','responses']])``.  A
# constant ``Map`` carries a member name by *key*, read back with ``.get``.  The
# optional type arguments must be followed immediately by the constructor ``(``,
# so a bare ``new Map`` cannot borrow an unrelated later call's parentheses; an
# optional wrapping ``(`` before ``new Map`` is permitted.
_MAP_ASSIGN = re.compile(
    rf"(?<![\w$.])({_IDENTIFIER})\s*(?::[^=;]*?)?=(?!=)\s*\(?\s*(new\s+Map)\b\s*(?:<[^<>]*>)?\s*\("
)
# ``(obj as SomeType).member = ...`` carries the member through a cast receiver.
# Only a trailing member access is unwrapped; ``f(x as T)`` is left untouched.
_CAST_AS = re.compile(
    rf"\(\s*({_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})*)\s+as\s+[^()]*\)\s*(?=[.\[])"
)


def _trim_assignment_value(text: str) -> str:
    """Return the assignment right-hand side without a trailing inline ``}``.

    ``run(){ this.m = 'responses' }`` captures ``'responses' }`` because the
    assignment runs to the end of the line; the trailing brace belongs to the
    enclosing block, not the value.
    """

    value = text.strip()
    if value.startswith("{"):
        return value
    return value.split("}", 1)[0].strip()


def _record_member_binding(
    path: str,
    value_text: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
    out: dict[str, object],
    aliases: list[tuple[str, str]],
) -> None:
    """Record a ``member.path -> constant`` binding from a value expression."""

    value = _trim_assignment_value(value_text)
    if not value:
        return
    if value.startswith("{"):
        close = _match_closing(value, 0)
        if close == -1:
            return
        _collect_object_members(value[1:close], path, environment, functions, out, aliases)
        return
    folded = _fold_constant_expression(value, environment, functions)
    if isinstance(folded, (str, list)):
        out[path] = folded
        return
    target = _normalize_member_path(value)
    if target is not None:
        aliases.append((path, target))


def _normalize_member_path(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if "'" in compact or '"' in compact:
        def _expand(match: re.Match[str]) -> str:
            value = _unescape_string(match.group(1))
            return f".{value}" if re.fullmatch(_IDENTIFIER, value) else match.group(0)

        compact = _COMPUTED_STRING_INDEX.sub(_expand, compact)
    return compact if _MEMBER_PATH.fullmatch(compact) else None


def _resolve_member_key(
    text: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
) -> str | None:
    key = text.strip()
    if not key:
        return None
    if key.startswith("[") and key.endswith("]"):
        value = _fold_constant_expression(key[1:-1], environment, functions)
        return value if isinstance(value, str) else None
    if re.fullmatch(_STRING_LITERAL, key):
        return _unescape_string(key)
    if re.fullmatch(_IDENTIFIER, key):
        return key
    return None


def _split_member_entry(entry: str) -> tuple[str, str | None]:
    depth = 0
    index = 0
    while index < len(entry):
        character = entry[index]
        if character in "'\"`":
            index = _skip_string_literal(entry, index)
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == ":" and depth == 0:
            return entry[:index], entry[index + 1 :]
        index += 1
    return entry, None


def _collect_object_members(
    body: str,
    prefix: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
    out: dict[str, object],
    aliases: list[tuple[str, str]],
) -> None:
    for entry in _split_top_level(body):
        entry = entry.strip()
        if not entry or entry.startswith("..."):
            continue
        key_text, value_text = _split_member_entry(entry)
        if value_text is None:
            key_text, value_text = entry, entry
        key = _resolve_member_key(key_text, environment, functions)
        if key is None:
            continue
        path = f"{prefix}.{key}"
        value = value_text.strip()
        if value.startswith("{"):
            close = _match_closing(value, 0)
            if close == len(value) - 1:
                _collect_object_members(value[1:-1], path, environment, functions, out, aliases)
                continue
        folded = _fold_constant_expression(value, environment, functions)
        if isinstance(folded, (str, list)):
            out[path] = folded
            continue
        target = _normalize_member_path(value)
        if target is not None:
            aliases.append((path, target))


def _iter_class_members(body: str) -> Iterable[str]:
    start = 0
    index = 0
    while index < len(body):
        character = body[index]
        if character in "'\"`":
            index = _skip_string_literal(body, index)
            continue
        if character == "{":
            close = _match_closing(body, index)
            if close == -1:
                break
            yield body[start : close + 1]
            index = close + 1
            start = index
            continue
        if character in ";\n":
            chunk = body[start:index]
            if chunk.strip():
                yield chunk
            index += 1
            start = index
            continue
        index += 1
    tail = body[start:]
    if tail.strip():
        yield tail


def _collect_class_members(
    body: str,
    class_name: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
    out: dict[str, object],
    aliases: list[tuple[str, str]],
) -> None:
    for member in _iter_class_members(body):
        match = _CLASS_MEMBER_DECL.match(member)
        if not match:
            continue
        modifiers = (match.group("mods") or "").split()
        name = match.group("name")
        prefix = f"{class_name}.{name}" if "static" in modifiers else f"this.{name}"
        value = match.group("value").strip()
        if value.startswith("{"):
            close = _match_closing(value, 0)
            if close == len(value) - 1:
                _collect_object_members(value[1:-1], prefix, environment, functions, out, aliases)
                continue
        folded = _fold_constant_expression(value, environment, functions)
        if isinstance(folded, (str, list)):
            out[prefix] = folded
            continue
        target = _normalize_member_path(value)
        if target is not None:
            aliases.append((prefix, target))


def _map_is_mutated(code: str, name: str) -> bool:
    """Return True when a constant ``Map`` binding is mutated after its literal.

    ``M.set(...)`` / ``M.delete(...)`` / ``M.clear(...)`` (and the bracket
    spelling ``M['set'](...)``) change the map after its constructor is
    recorded, so the recorded constant no longer proves a later ``.get`` read.
    Only these named mutators of this exact binding are treated as mutation, so
    unrelated fields on other receivers do not invalidate a map.
    """

    escaped = re.escape(name)
    for method in ("set", "delete", "clear"):
        if re.search(rf"(?<![\w$.]){escaped}\s*\.\s*{method}\s*\(", code):
            return True
        bracketed = rf"(?<![\w$.]){escaped}\s*\[\s*(?P<quote>['\"]){method}(?P=quote)\s*\]\s*\("
        if re.search(bracketed, code):
            return True
    return False


def _alias_component(code: str, root: str) -> set[str]:
    """Return ``root`` and every name bound to the same value by aliasing.

    ``const A = M`` does not copy a map -- it references it -- so a mutation
    through any name in the alias component mutates the same map and invalidates
    a constant recorded under the original binding.  A plain later assignment
    (``let A; A = M``) aliases the same value, so both declaration and assignment
    edges are collected.  The graph is undirected: ``const B = A`` extends the
    component transitively.
    """

    edges: dict[str, set[str]] = {}
    for pattern in (_CONST_ALIAS_DECL, _ASSIGN_ALIAS):
        for match in pattern.finditer(code):
            left, right = match.group(1), match.group(2)
            edges.setdefault(left, set()).add(right)
            edges.setdefault(right, set()).add(left)
    component = {root}
    pending = [root]
    while pending:
        for neighbour in edges.get(pending.pop(), ()):
            if neighbour not in component:
                component.add(neighbour)
                pending.append(neighbour)
    return component


def _binding_is_mutated(code: str, name: str) -> bool:
    return any(_map_is_mutated(code, alias) for alias in _alias_component(code, name))


def _member_path_constants(
    code: str,
    base: Mapping[str, object],
    functions: Mapping[str, str],
) -> dict[str, object]:
    result: dict[str, object] = {}
    aliases: list[tuple[str, str]] = []
    environment = dict(base)
    code = _CAST_AS.sub(lambda match: match.group(1), code)
    for _ in range(6):
        previous = len(result)
        for match in _OBJECT_DECL.finditer(code):
            brace = match.end() - 1
            close = _match_closing(code, brace)
            if close != -1:
                _collect_object_members(
                    code[brace + 1 : close], match.group(1), environment, functions, result, aliases
                )
        for match in _CLASS_DECL.finditer(code):
            brace = match.end() - 1
            close = _match_closing(code, brace)
            if close != -1:
                _collect_class_members(
                    code[brace + 1 : close], match.group(1), environment, functions, result, aliases
                )
        for match in _MEMBER_ALIAS_DECL.finditer(code):
            aliases.append((match.group(1), re.sub(r"\s+", "", match.group(2))))
        for match in _ASSIGN_MEMBER.finditer(code):
            receiver = re.sub(r"\s+", "", match.group(1))
            if match.group(2) is not None:
                key = match.group(2)
            else:
                key = _unescape_string(match.group(3))
            _record_member_binding(
                f"{receiver}.{key}", match.group(4), environment, functions, result, aliases
            )
        for match in _ASSIGN_OBJECT.finditer(code):
            brace = match.end() - 1
            close = _match_closing(code, brace)
            if close != -1:
                _collect_object_members(
                    code[brace + 1 : close], match.group(1), environment, functions, result, aliases
                )
        for match in _ASSIGN_ARRAY.finditer(code):
            brace = match.end() - 1
            close = _match_closing(code, brace)
            if close == -1:
                continue
            tail = code[close + 1 :]
            if tail.lstrip(" \t")[:1] not in ("", ";", ",", ")", "}", "]", "\n", "\r"):
                continue
            items: list[str] = []
            for part in _split_top_level(code[brace + 1 : close]):
                item = _fold_constant_expression(part, environment, functions)
                if not isinstance(item, str):
                    items = []
                    break
                items.append(item)
            if items:
                result[match.group(1)] = items
        for match in _MAP_ASSIGN.finditer(code):
            close = _match_closing(code, match.end() - 1)
            if close == -1:
                continue
            tail = code[close + 1 :].lstrip(" \t)")
            boundary = tail[:1]
            if boundary and boundary in "\n\r":
                if tail.lstrip("\r\n \t")[:1] in (".", "+"):
                    continue
            elif boundary and boundary not in ";,}]":
                if not re.match(r"(?:as|satisfies)\b", tail):
                    continue
            folded = _fold_constant_expression(
                code[match.start(2) : close + 1], environment, functions
            )
            if (
                isinstance(folded, Mapping)
                and folded
                and not _binding_is_mutated(code, match.group(1))
            ):
                result[match.group(1)] = dict(folded)
        for alias, source in aliases:
            for key, value in list(result.items()):
                if key.startswith(source + "."):
                    result.setdefault(f"{alias}.{key[len(source) + 1:]}", value)
        environment.update(result)
        if len(result) == previous:
            break
    return result


# --------------------------------------------------------------------------- #
# Project index: cross-file constants, zero-arg helpers, parameter constants.
# --------------------------------------------------------------------------- #

_IMPORT_NAMED = re.compile(
    rf"\bimport\s+(?!type\b)(?:({_IDENTIFIER})\s*,?\s*)?(?:\{{([^}}]*)\}})?\s*from\s*['\"]([^'\"]+)['\"]"
)
_IMPORT_NAMESPACE = re.compile(rf"\bimport\s+(?!type\b)\*\s+as\s+({_IDENTIFIER})\s+from\s*['\"]([^'\"]+)['\"]")
_EXPORT_FROM = re.compile(r"\bexport\s*\{([^{}]*)\}\s*from\s*['\"]([^'\"]+)['\"]")
_EXPORT_LOCAL = re.compile(r"\bexport\s*\{([^{}]*)\}\s*(?=[;\n])")
_EXPORT_DECL = re.compile(rf"\bexport\s+(?:const|let|var|async\s+function|function)\s+({_IDENTIFIER})\b")
_CONST_DECL = re.compile(rf"\b(?:const|let|var)\s+({_IDENTIFIER})\s*(?::[^=;]*?)?=\s*(?!>)([^;\n]+)")
_CONST_ALIAS_DECL = re.compile(rf"\b(?:const|let|var)\s+({_IDENTIFIER})\s*=\s*({_IDENTIFIER})\s*(?=[;,\)\n]|$)")
# A plain later assignment ``A = B`` aliases the same value without a declaration
# keyword; a mutation through ``A`` therefore mutates ``B``.
_ASSIGN_ALIAS = re.compile(rf"(?<![\w$.])({_IDENTIFIER})\s*=(?!=)\s*({_IDENTIFIER})\s*(?=[;,\)\n]|$)")
_FUNCTION_DECL = re.compile(rf"\bfunction\s+({_IDENTIFIER})\s*\(([^)]*)\)")
_ARROW_CONST = re.compile(rf"\b(?:const|let|var)\s+({_IDENTIFIER})\s*=\s*(?:async\s*)?\(([^)]*)\)\s*(?::[^=]*?)?=>")
_RETURN_EXPR = re.compile(r"\breturn\b\s*(.+?)(?=\s*(?:;|\n|\}|$))", re.S)
_CALL_SITE = re.compile(rf"\b({_IDENTIFIER})\s*\(")

_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _module_key(root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    for extension in _EXTENSIONS:
        if relative.endswith(extension):
            return relative[: -len(extension)]
    return relative


def _resolve_module(root: Path, source: Path, specifier: str) -> Path | None:
    if not specifier.startswith("."):
        return None
    base = (source.parent / specifier).resolve()
    candidates: list[Path] = []
    if base.suffix:
        candidates.append(base)
    for extension in _EXTENSIONS:
        candidates.append(base.with_suffix(extension))
        candidates.append(base / ("index" + extension))
        candidates.append(base / ("index" + extension))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _param_name(declaration: str) -> str | None:
    """Extract the bound identifier from a parameter declaration.

    ``member: string`` / ``...rest: T[]`` / ``value = 1`` all bind the leading
    identifier; type annotations, defaults and rest markers are stripped.
    """

    text = declaration.strip()
    if text.startswith("..."):
        text = text[3:]
    text = text.split(":", 1)[0]
    text = text.split("=", 1)[0]
    text = text.strip().rstrip("?")
    return text if re.fullmatch(_IDENTIFIER, text) else None


def _param_default_value(declaration: str) -> str | None:
    """Return the ``= default`` initializer text of a binding declaration.

    ``ORIGIN_MEMBER = 'responses'`` yields ``'responses'``; a binding without a
    default yields ``None``.  Only the first top-level ``=`` is the default
    separator, so comparison/membership operators (``==``/``>=``/``=>``) inside
    a default expression are not mistaken for it.
    """

    depth = 0
    index = 0
    while index < len(declaration):
        character = declaration[index]
        if character in "'\"`":
            index = _skip_string_literal(declaration, index)
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "=" and depth == 0:
            before = declaration[index - 1] if index > 0 else ""
            after = declaration[index + 1] if index + 1 < len(declaration) else ""
            if before in "=!<>" or after == "=":
                index += 1
                continue
            value = declaration[index + 1 :].strip()
            return value or None
        index += 1
    return None


def _parse_named_list(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in _split_top_level(text):
        item = item.strip()
        if not item:
            continue
        if " as " in item:
            original, _, alias = item.partition(" as ")
            pairs.append((original.strip(), alias.strip()))
        else:
            pairs.append((item, item))
    return [(original, alias) for original, alias in pairs if re.fullmatch(_IDENTIFIER, original) and re.fullmatch(_IDENTIFIER, alias)]


def _fold_function_return(body: str, environment: Mapping[str, object], functions: Mapping[str, str]) -> str | None:
    values: set[str] = set()
    found = False
    for match in _RETURN_EXPR.finditer(body):
        found = True
        value = _fold_constant_expression(match.group(1), environment, functions)
        if isinstance(value, str):
            values.add(value)
    if not found:
        value = _fold_constant_expression(body.strip(), environment, functions)
        return value if isinstance(value, str) else None
    if len(values) == 1:
        return next(iter(values))
    return None


class _ProjectIndex:
    """Resolves constant strings across the module graph and call sites."""

    def __init__(self, root: Path, files: Sequence[Path]):
        self.root = root
        self.files = list(files)
        self.sources: dict[Path, str] = {}
        self.module_of: dict[Path, str] = {}
        for path in self.files:
            try:
                self.sources[path] = _strip_comments(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                self.sources[path] = ""
            self.module_of[path] = _module_key(root, path)
        self.imports: dict[Path, list[tuple[str, str, str]]] = {}
        self.namespace_imports: dict[Path, list[tuple[str, str]]] = {}
        self.local_consts: dict[Path, list[tuple[str, str]]] = {}
        self.local_functions: dict[Path, list[tuple[str, str, str]]] = {}
        self.exports: dict[Path, list[tuple[str, str]]] = {}
        self.re_exports: dict[Path, list[tuple[str, str, str]]] = {}
        self._parse()
        self.environment: dict[Path, dict[str, object]] = {path: {} for path in self.files}
        self.functions: dict[Path, dict[str, str]] = {path: {} for path in self.files}
        self._resolve()
        self.parameter_constants = self._collect_parameter_constants()
        self.variant_cache: dict[Path, tuple[list[str], str]] = {}

    def _parse(self) -> None:
        for path in self.files:
            code = self.sources[path]
            self.imports[path] = []
            for match in _IMPORT_NAMED.finditer(code):
                default, named, specifier = match.group(1), match.group(2), match.group(3)
                resolved = _resolve_module(self.root, path, specifier)
                if resolved is None:
                    continue
                if named:
                    for original, alias in _parse_named_list(named):
                        self.imports[path].append((alias, original, resolved))
                if default:
                    self.imports[path].append((default, "default", resolved))
            self.namespace_imports[path] = []
            for match in _IMPORT_NAMESPACE.finditer(code):
                resolved = _resolve_module(self.root, path, match.group(2))
                if resolved is not None:
                    self.namespace_imports[path].append((match.group(1), resolved))
            self.local_consts[path] = [
                (match.group(1), match.group(2)) for match in _CONST_DECL.finditer(code)
            ]
            self.local_functions[path] = []
            for match in _FUNCTION_DECL.finditer(code):
                self.local_functions[path].append((match.group(1), match.group(2), match.start()))
            for match in _ARROW_CONST.finditer(code):
                self.local_functions[path].append((match.group(1), match.group(2), match.start()))
            self.exports[path] = []
            for match in _EXPORT_DECL.finditer(code):
                self.exports[path].append((match.group(1), match.group(1)))
            for match in _EXPORT_LOCAL.finditer(code):
                for original, alias in _parse_named_list(match.group(1)):
                    self.exports[path].append((alias, original))
            self.re_exports[path] = []
            for match in _EXPORT_FROM.finditer(code):
                resolved = _resolve_module(self.root, path, match.group(2))
                if resolved is None:
                    continue
                for original, alias in _parse_named_list(match.group(1)):
                    self.re_exports[path].append((alias, original, resolved))

    def _resolve(self) -> None:
        for _ in range(12):
            changed = False
            for path in self.files:
                base = dict(self.environment[path])
                functions = dict(self.functions[path])
                for local, original, module in self.imports[path]:
                    value = self.environment.get(module, {}).get(original)
                    if value is not None and base.get(local) != value:
                        base[local] = value
                    function_value = self.functions.get(module, {}).get(original)
                    if function_value is not None and functions.get(local) != function_value:
                        functions[local] = function_value
                    prefix = original + "."
                    for key, member_value in self.environment.get(module, {}).items():
                        if key.startswith(prefix):
                            aliased = f"{local}.{key[len(prefix):]}"
                            if base.get(aliased) != member_value:
                                base[aliased] = member_value
                for namespace, module in self.namespace_imports[path]:
                    for name, value in self.environment.get(module, {}).items():
                        key = f"{namespace}.{name}"
                        if base.get(key) != value:
                            base[key] = value
                for alias, original, module in self.re_exports[path]:
                    value = self.environment.get(module, {}).get(original)
                    if value is not None and base.get(alias) != value:
                        base[alias] = value
                # Fold local constants with the (cross-file) import environment.
                for name, value in self._resolve_local_consts(path, base):
                    if base.get(name) != value:
                        base[name] = value
                for name, value in self._resolve_local_functions(path, base):
                    if functions.get(name) != value:
                        functions[name] = value
                    if base.get(name) != value:
                        base[name] = value
                if base != self.environment[path] or functions != self.functions[path]:
                    self.environment[path] = base
                    self.functions[path] = functions
                    changed = True
            if not changed:
                break

    def _resolve_local_consts(self, path: Path, base: Mapping[str, object]) -> list[tuple[str, object]]:
        found: list[tuple[str, object]] = []
        functions = self.functions[path]
        source_code = self.sources[path]
        # An imported collection mutated in this module no longer proves its
        # content, so it is removed before folding any local constant from it.
        effective: dict[str, object] = {}
        for key, value in base.items():
            if isinstance(value, _ConstSet) and _rebuild_set_constant(source_code, key, base) is None:
                continue
            if isinstance(value, Mapping) and value and _binding_is_mutated(source_code, key):
                continue
            effective[key] = value
        for name, expression in self.local_consts[path]:
            value = _fold_constant_expression(expression, effective, functions)
            if isinstance(value, (str, list)):
                found.append((name, value))
            elif isinstance(value, _ConstSet):
                resolved = _rebuild_set_constant(source_code, name, {**effective, name: value})
                if resolved is not None:
                    found.append((name, resolved))
            elif isinstance(value, Mapping):
                resolved = _rebuild_map_constant(
                    source_code, name, {**effective, name: value}, functions
                )
                if resolved:
                    found.append((name, resolved))
        # Simple alias chains among names that fold to identifiers.  A chain to
        # a collection must be revalidated against a local mutation, so an alias
        # invalidated in this module is not reintroduced from another module.
        for _ in range(4):
            for name, expression in self.local_consts[path]:
                if any(existing == name for existing, _ in found):
                    continue
                source = expression.strip()
                if not re.fullmatch(_IDENTIFIER, source):
                    continue
                value = effective.get(source)
                if value is None:
                    continue
                if isinstance(value, _ConstSet):
                    value = _rebuild_set_constant(source_code, name, {**effective, name: value})
                elif isinstance(value, Mapping):
                    value = _rebuild_map_constant(
                        source_code, name, {**effective, name: value}, functions
                    )
                if value is not None:
                    found.append((name, value))
        for name, value in _member_path_constants(source_code, effective, functions).items():
            found.append((name, value))
        return found

    def _resolve_local_functions(self, path: Path, base: Mapping[str, str]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        functions = self.functions[path]
        for name, params, offset in self.local_functions[path]:
            body = self._function_body(path, offset)
            if body is None:
                continue
            value = _fold_function_return(body, base, functions)
            if value is not None:
                found.append((name, value))
        return found

    def _function_body(self, path: Path, offset: int) -> str | None:
        code = self.sources[path]
        brace = code.find("{", offset)
        arrow = code.find("=>", offset)
        if arrow != -1 and (brace == -1 or arrow < brace):
            start = arrow + 2
            end = len(code)
            for index in range(start, len(code)):
                if code[index] in ";\n":
                    end = index
                    break
            return code[start:end]
        if brace == -1:
            return None
        close_index = _match_closing(code, brace)
        if close_index == -1:
            return None
        return code[brace + 1 : close_index]

    def _collect_parameter_constants(self) -> dict[str, dict[int, set[str]]]:
        result: dict[str, dict[int, set[str]]] = {}
        # Gather call-site constant arguments project-wide.
        call_args: dict[str, list[list[str | None]]] = {}
        for path in self.files:
            code = self.sources[path]
            for match in _CALL_SITE.finditer(code):
                name = match.group(1)
                open_index = match.end() - 1
                close_index = _match_closing(code, open_index)
                if close_index == -1:
                    continue
                args = _split_top_level(code[open_index + 1 : close_index])
                folded = [
                    _resolve_constant_key(arg, self.environment[path], self.functions[path]) for arg in args
                ]
                if any(value is not None for value in folded):
                    call_args.setdefault(name, []).append(folded)
        for path in self.files:
            for name, params, _ in self.local_functions[path]:
                parameter_names = [_param_name(item) for item in _split_top_level(params)]
                if not any(item is not None for item in parameter_names):
                    continue
                observed = call_args.get(name)
                if not observed:
                    continue
                mapping: dict[int, set[str]] = {}
                for values in observed:
                    for index, value in enumerate(values):
                        if value is not None and index < len(parameter_names):
                            mapping.setdefault(index, set()).add(value)
                if mapping:
                    result[name] = mapping
        return result


_INDEX_CACHE: dict[object, _ProjectIndex] = {}


def _project_index(root: Path, files: Sequence[Path]) -> _ProjectIndex:
    signature = tuple((path.as_posix(), path.stat().st_mtime_ns, path.stat().st_size) for path in files)
    key = (str(root.resolve()), signature)
    cached = _INDEX_CACHE.get(key)
    if cached is None:
        cached = _ProjectIndex(root, files)
        if len(_INDEX_CACHE) > 32:
            _INDEX_CACHE.clear()
        _INDEX_CACHE[key] = cached
    return cached


# --------------------------------------------------------------------------- #
# Canonicalization: resolve indirect member access to a canonical member read.
# --------------------------------------------------------------------------- #

_KEY_CALL = re.compile(
    r"\b(?:Reflect|Object)\s*\.\s*(?:get|set|has|deleteProperty|getOwnPropertyDescriptor)\s*\("
)
_DESTRUCTURE = re.compile(r"(\b(?:const|let|var)\s*)\{([^{}]*)\}\s*=\s*([^;\n]+)")


def _canonicalize_computed_keys(
    code: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
) -> str:
    result: list[str] = []
    last = 0
    for start, end, inner in _index_computed_accesses(code):
        value = _resolve_constant_key(inner, environment, functions)
        if value is None:
            continue
        replacement = f".{value}" if re.fullmatch(_IDENTIFIER, value) else "['" + value.replace("\\", "\\\\").replace("'", "\\'") + "']"
        result.append(code[last:start])
        result.append(replacement)
        last = end
    result.append(code[last:])
    return "".join(result)


def _canonicalize_member_calls(
    code: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
) -> str:
    """Resolve ``Reflect``/``Object`` member reads to a plain member access.

    ``Reflect.get(handle, 'responses')`` and
    ``Object.getOwnPropertyDescriptor(handle, 'responses')`` read the same
    source-framework member as ``handle.responses``; only a constant key is
    folded, so a proxy trap that forwards a dynamic ``property`` is untouched.
    """

    result: list[str] = []
    index = 0
    while True:
        match = _KEY_CALL.search(code, index)
        if not match:
            result.append(code[index:])
            break
        open_index = match.end() - 1
        close_index = _match_closing(code, open_index)
        if close_index == -1:
            result.append(code[index:])
            break
        parts = _split_top_level(code[open_index + 1 : close_index])
        replacement = code[match.start() : close_index + 1]
        if len(parts) >= 2:
            value = _resolve_constant_key(parts[1], environment, functions)
            if value is not None and re.fullmatch(_IDENTIFIER, value):
                replacement = f"({parts[0]}).{value}"
        result.append(code[index : match.start()])
        result.append(replacement)
        index = close_index + 1
    return "".join(result)


def _resolve_destructure_key(
    key: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
) -> str | None:
    key = key.strip().lstrip(".")
    if not key:
        return None
    if key.startswith("[") and key.endswith("]"):
        return _resolve_constant_key(key[1:-1], environment, functions)
    if re.fullmatch(_STRING_LITERAL, key):
        return _unescape_string(key)
    if re.fullmatch(_IDENTIFIER, key):
        return key
    return None


def _canonicalize_destructuring(
    code: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
) -> str:
    """Surface destructured member reads as an explicit member access.

    ``const {responses: transport} = handle`` reads ``handle.responses`` even
    though no ``.responses`` text appears; the pattern's property is resolved
    (including renamed bindings) and the equivalent access is made explicit.
    """

    def replace(match: re.Match[str]) -> str:
        pattern = match.group(2)
        receiver = match.group(3).strip()
        accesses: list[str] = []
        for prop in _split_top_level(pattern):
            if not prop:
                continue
            key = prop.split(":", 1)[0].strip()
            value = _resolve_destructure_key(key, environment, functions)
            if value is not None and re.fullmatch(_IDENTIFIER, value):
                accesses.append(f"({receiver}).{value}")
        if not accesses:
            return match.group(0)
        return f"{match.group(1)}{{{pattern}}} = {receiver}; " + " ".join(accesses)

    return _DESTRUCTURE.sub(replace, code)


def _normalize_base(text: str) -> str:
    return _normalize_optional_chain(_strip_comments(text))


_ARRAY_DESTRUCTURE = re.compile(
    rf"\b(?:const|let|var)\s*\[([^\]]*)\]\s*(?::[^=;]*?)?=\s*([^;\n]+)"
)
_ARRAY_MUTATORS = frozenset(
    {"push", "pop", "shift", "unshift", "splice", "sort", "reverse", "fill", "copyWithin"}
)


def _constant_array_is_stable(code: str, name: str) -> bool:
    """Return False when a constant array name is reassigned or mutated.

    Array destructuring copies the element at destructure time, but the scanner
    models a *single* constant binding: once ``name`` is assigned again or its
    elements are mutated, the recorded constant no longer proves the read, so
    the binding must not be folded (avoiding a false source-fallback hit).
    """

    escaped = re.escape(name)
    if len(re.findall(rf"(?<![\w$.]){escaped}\s*=(?!=)", code)) > 1:
        return False
    if re.search(rf"(?<![\w$.]){escaped}\s*\[[^\]]*\]\s*=(?!=)", code):
        return False
    methods = "|".join(sorted(_ARRAY_MUTATORS))
    if re.search(rf"(?<![\w$.]){escaped}\s*\.\s*(?:{methods})\s*\(", code):
        return False
    return not re.search(rf"(?<![\w$.]){escaped}\s*\.\s*{_IDENTIFIER}\s*=(?!=)", code)


def _array_destructure_bindings(
    code: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Fold ``const [a, , b] = ARR`` element bindings from a constant array.

    Array destructuring binds each identifier to the array element at its
    position (holes shift later bindings).  A bound element that is a constant
    string carries a member name exactly as a literal does, so it is fed back
    into the environment the computed-key resolver reads.  When the element is
    absent (or explicitly ``undefined``) JavaScript applies the binding's
    ``= default`` initializer, so a *constant* default is folded too; a dynamic
    default, a dynamic array, or an array later reassigned/mutated stays
    unresolved.
    """

    bindings: list[tuple[str, str]] = []
    for match in _ARRAY_DESTRUCTURE.finditer(code):
        pattern = match.group(1)
        source = _strip_type_assertion(match.group(2).strip())
        if re.fullmatch(_IDENTIFIER, source) and not _constant_array_is_stable(code, source):
            continue
        value = _fold_constant_expression(source, environment, functions)
        if not isinstance(value, list):
            continue
        for position, item in enumerate(_split_top_level(pattern)):
            text = item.strip()
            if not text or text.startswith("..."):
                continue
            name = _param_name(text)
            if name is None:
                continue
            element = value[position] if position < len(value) else _UNDEFINED
            if isinstance(element, str):
                bindings.append((name, element))
                continue
            if element is _UNDEFINED:
                default = _param_default_value(text)
                if default is None:
                    continue
                folded = _fold_constant_expression(default, environment, functions)
                if isinstance(folded, str):
                    bindings.append((name, folded))
    return bindings


def _constant_set_calls(
    code: str,
    name: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
) -> list[tuple[str, str]] | None:
    """Return ``(key, value)`` for every constant ``name.set(k, v)`` call.

    Returns ``None`` (unsound to fold) when a ``set`` argument is not a constant
    or when the call chains into a further method, since that chained call reads
    the map back and cannot be modelled as one plain write.
    """

    escaped = re.escape(name)
    calls: list[tuple[str, str]] = []
    for match in re.finditer(rf"(?<![\w$.]){escaped}\s*\.\s*set\s*\(", code):
        open_index = match.end() - 1
        close = _match_closing(code, open_index)
        if close == -1:
            return None
        if code[close + 1 :].lstrip()[:1] == ".":
            return None
        args = _split_top_level(code[open_index + 1 : close])
        if len(args) != 2:
            return None
        key = _fold_constant_expression(args[0], environment, functions)
        value = _fold_constant_expression(args[1], environment, functions)
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        calls.append((key, value))
    return calls


def _map_has_other_mutation(code: str, name: str) -> bool:
    """Return True when a map binding is written by anything but a plain ``set``.

    ``delete``/``clear`` remove or wipe entries, and a bracket-spelled method
    call (``M['set'](...)``) hides the method name, so neither can be folded
    soundly.
    """

    escaped = re.escape(name)
    if re.search(rf"(?<![\w$.]){escaped}\s*\.\s*(?:delete|clear)\s*\(", code):
        return True
    if re.search(rf"(?<![\w$.]){escaped}\s*\[\s*['\"](?:delete|clear)['\"]\s*\]\s*\(", code):
        return True
    return bool(re.search(rf"(?<![\w$.]){escaped}\s*\[\s*['\"][^'\"]*['\"]\s*\]\s*\(", code))


def _rebuild_map_constant(
    code: str,
    name: str,
    environment: Mapping[str, object],
    functions: Mapping[str, str],
) -> dict[str, str] | None:
    """Return the provable constant ``{key: value}`` of a map binding, else None.

    An unmutated binding keeps its constructor entries.  A binding whose
    constructor entries are later mutated cannot be proven, so it is dropped (a
    ``set`` may have overwritten any key).  An *empty* map populated only by
    constant ``.set(constKey, constValue)`` calls is folded to those pairs; it is
    dropped when a key is written two different values, when a ``set`` argument
    is non-constant, or when ``delete``/``clear``/a bracket-spelled call appears.
    """

    entries = environment.get(name)
    if not isinstance(entries, Mapping):
        return None
    if not _binding_is_mutated(code, name):
        return dict(entries)
    if entries:
        return None
    values: dict[str, set[str]] = {}
    for alias in _alias_component(code, name):
        if _map_has_other_mutation(code, alias):
            return None
        calls = _constant_set_calls(code, alias, environment, functions)
        if calls is None:
            return None
        for key, value in calls:
            values.setdefault(key, set()).add(value)
    result: dict[str, str] = {}
    for key, written in values.items():
        if len(written) != 1:
            return None
        result[key] = next(iter(written))
    return result


def _set_is_mutated(code: str, name: str) -> bool:
    """Return True when a constant ``Set`` binding is mutated after its literal.

    ``add``/``delete``/``clear`` (and the bracket spelling ``S['delete'](...)``)
    change the set after its constructor is recorded, so the recorded constant
    no longer proves a later spread read.  Any mutation drops the binding rather
    than tracking the new content, matching the conservative ``Map`` policy and
    keeping a mutated collection from being reported.
    """

    escaped = re.escape(name)
    for method in ("add", "delete", "clear"):
        if re.search(rf"(?<![\w$.]){escaped}\s*\.\s*{method}\s*\(", code):
            return True
        bracketed = rf"(?<![\w$.]){escaped}\s*\[\s*(?P<quote>['\"]){method}(?P=quote)\s*\]\s*\("
        if re.search(bracketed, code):
            return True
    return False


def _set_binding_is_mutated(code: str, name: str) -> bool:
    """Return True when the set ``name`` or any of its aliases is mutated."""

    return any(_set_is_mutated(code, alias) for alias in _alias_component(code, name))


def _rebuild_set_constant(
    code: str,
    name: str,
    environment: Mapping[str, object],
) -> _ConstSet | None:
    """Return the provable constant ``Set`` of a binding, else None.

    An unmutated binding keeps its constructor elements; a binding mutated after
    construction cannot be proven, so it is dropped.
    """

    entries = environment.get(name)
    if not isinstance(entries, _ConstSet):
        return None
    if _set_binding_is_mutated(code, name):
        return None
    return entries


def _local_environment(
    code: str,
    base: Mapping[str, object],
    functions: Mapping[str, str],
) -> dict[str, object]:
    environment = dict(base)
    for _ in range(3):
        changed = False
        # Drop a collection invalidated by a mutation before it -- or an alias
        # seeded from another module -- can fold a dependent constant.
        for name in [
            key
            for key, value in environment.items()
            if (
                isinstance(value, _ConstSet)
                and _rebuild_set_constant(code, key, environment) is None
            )
            or (isinstance(value, Mapping) and value and _binding_is_mutated(code, key))
        ]:
            del environment[name]
            changed = True
        for match in _CONST_DECL.finditer(code):
            name = match.group(1)
            value = _fold_constant_expression(match.group(2), environment, functions)
            if isinstance(value, _ConstSet) and _set_binding_is_mutated(code, name):
                # A set mutated after its literal no longer proves its elements,
                # so it must not fold a dependent constant either.
                if environment.pop(name, None) is not None:
                    changed = True
                continue
            if isinstance(value, Mapping) and value and _binding_is_mutated(code, name):
                # A non-empty map mutated after its literal may have overwritten
                # any key, so the literal content is no longer provable.
                if environment.pop(name, None) is not None:
                    changed = True
                continue
            if isinstance(value, (str, list, Mapping, _ConstSet)) and environment.get(name) != value:
                environment[name] = value
                changed = True
        for match in _CONST_ALIAS_DECL.finditer(code):
            name, source = match.group(1), match.group(2)
            if source not in environment:
                continue
            value = environment[source]
            if isinstance(value, _ConstSet):
                value = _rebuild_set_constant(code, name, {**environment, name: value})
            elif isinstance(value, Mapping):
                value = _rebuild_map_constant(
                    code, name, {**environment, name: value}, functions
                )
            if value is not None and environment.get(name) != value:
                environment[name] = value
                changed = True
        for name, value in _member_path_constants(code, environment, functions).items():
            if environment.get(name) != value:
                environment[name] = value
                changed = True
        for name, value in _array_destructure_bindings(code, environment, functions):
            if environment.get(name) != value:
                environment[name] = value
                changed = True
        if not changed:
            break
    # A map binding is resolved to its final provable constant content: an
    # unmutated constructor literal is kept, a mutated constructor literal is
    # dropped (a later write may have overwritten any key), and an empty map
    # built purely by constant ``.set`` calls is folded to those pairs.
    for name in [key for key, value in environment.items() if isinstance(value, Mapping)]:
        resolved = _rebuild_map_constant(code, name, environment, functions)
        if resolved is None:
            del environment[name]
        else:
            environment[name] = resolved
    # A set binding is kept only while provably unmutated; any ``add``/``delete``/
    # ``clear`` on the binding or an alias drops it.
    for name in [key for key, value in environment.items() if isinstance(value, _ConstSet)]:
        resolved = _rebuild_set_constant(code, name, environment)
        if resolved is None:
            del environment[name]
        else:
            environment[name] = resolved
    return environment


def _canonical_variants(text: str, path: Path, index: _ProjectIndex) -> list[str]:
    base_code = _normalize_base(text)
    seed_environment = index.environment.get(path, {})
    functions = index.functions.get(path, {})
    environment = _local_environment(base_code, seed_environment, functions)
    variants: list[dict[str, str]] = [environment]
    # Interprocedural parameter constants: the function body uses a parameter as
    # a computed key and a call site passes a constant string for that position.
    for name, mapping in index.parameter_constants.items():
        for function_name, params, _ in index.local_functions.get(path, []):
            if function_name != name:
                continue
            parameter_names = [_param_name(item) for item in _split_top_level(params)]
            for position, values in mapping.items():
                if position >= len(parameter_names):
                    continue
                parameter = parameter_names[position]
                if parameter is None:
                    continue
                if not re.search(rf"\[\s*{re.escape(parameter)}\s*\]", base_code):
                    continue
                for value in sorted(values):
                    variant = dict(environment)
                    variant[parameter] = value
                    variants.append(variant)
    canonical: list[str] = []
    for environment_variant in variants:
        code = _canonicalize_computed_keys(base_code, environment_variant, functions)
        code = _canonicalize_member_calls(code, environment_variant, functions)
        code = _canonicalize_destructuring(code, environment_variant, functions)
        canonical.append(code)
    return canonical


def _locally_defined(code: str, name: str) -> bool:
    """Return True when ``name`` is declared in this file.

    A rule target that names a symbol the candidate declared locally (for
    example the retained ``generateWithEditorTools`` helper) is a local
    implementation name, not a source-framework API, so naming is not a
    deceptive-source fallback.  The real fallback call inside such a helper is
    still caught by the direct-call rules.
    """

    escaped = re.escape(name)
    patterns = (
        re.compile(rf"\b(?:function|class|const|let|var)\s+{escaped}\b"),
        re.compile(rf"\b(?:async\s+)?{escaped}\s*\([^)]*\)\s*(?::[^;{{=]*)?\{{"),
        re.compile(rf"\b(?:private|public|protected|static|readonly|get|set)\b[^\n]*\b{escaped}\s*\("),
    )
    return any(pattern.search(code) for pattern in patterns)


def _variants_for(
    path: Path,
    index: _ProjectIndex | None,
    cache: dict[Path, tuple[list[str], str]],
) -> tuple[list[str], str]:
    cached = cache.get(path)
    if cached is not None:
        return cached
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        result: tuple[list[str], str] = ([], "")
    else:
        base = _normalize_base(text)
        variants = _canonical_variants(text, path, index) if index is not None else [base]
        result = ([_mask_response_inspections(code) for code in variants], base)
    cache[path] = result
    return result


def _mask_response_inspections(code: str) -> str:
    """Do not equate inspecting a client's method with issuing a request.

    A const read used only by typeof/identity checks cannot invoke or forward
    the method. Keep all other reads in the scan, including a callable alias
    or an alias that escapes to another function. This narrow exemption also
    leaves the existing computed-member and cross-module detection intact.
    """

    declaration = re.compile(
        rf"\bconst\s+({_IDENTIFIER})\s*=\s*([^;\n]*\.responses\s*\.create)\s*(?=;|\n|$)"
    )
    spans: list[tuple[int, int]] = []
    for match in declaration.finditer(code):
        name = match.group(1)
        uses = list(re.finditer(rf"\b{re.escape(name)}\b", code))
        safe = True
        for use in uses:
            if match.start(1) == use.start():
                continue
            before = code[:use.start()]
            after = code[use.end():]
            if re.search(r"\btypeof\s*$", before) and re.match(
                r"\s*(?:===|!==|==|!=|[),;}])", after
            ):
                continue
            if re.match(r"\s*(?:===|!==|==|!=)", after):
                continue
            safe = False
            break
        if safe:
            spans.append((match.start(2), match.end(2)))
    for start, end in reversed(spans):
        code = code[:start] + " " * (end - start) + code[end:]
    return code


def _literal_contains(target: str, variants: Sequence[str], base_code: str) -> bool:
    if _BARE_IDENTIFIER.match(target) and _locally_defined(base_code, target):
        return False
    flat_target = _flatten_whitespace(target)
    if not flat_target:
        return False
    folded = flat_target.casefold()
    return any(folded in _flatten_whitespace(variant).casefold() for variant in variants)


def _regex_contains(target: str, variants: Sequence[str]) -> bool:
    """Match a regular expression against the access-resolved source form.

    Whitespace, optional chaining, computed and concatenated member access are
    already folded, so a pattern such as ``\\.\\s*responses\\b`` catches the
    whole equivalent writing family of a source-framework member dereference
    without depending on the exact spelling.
    """

    try:
        pattern = re.compile(target, re.IGNORECASE)
    except re.error:
        return False
    return any(pattern.search(_WHITESPACE.sub(" ", variant)) for variant in variants)


def _dependency_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        for field in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
            "require",
            "require-dev",
        ):
            entries = payload.get(field)
            if isinstance(entries, Mapping) and any(
                _target_matches(str(name), target) for name in entries
            ):
                return True
    dependency_lines = (
        re.compile(r"^\s*([A-Za-z0-9@._+/-]+)\s*(?:[<>=~!].*)?$"),
        re.compile(r"^\s*(?:gem|implementation|api|compileOnly|runtimeOnly)\s*[( ]\s*['\"]([^'\"]+)"),
        re.compile(r"^\s*([A-Za-z0-9_.+-]+)\s*=\s*['\"]?[0-9*^~<>=]"),
    )
    for line in text.splitlines():
        for pattern in dependency_lines:
            match = pattern.search(line)
            if match and _target_matches(match.group(1), target):
                return True
    return False


def _import_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    candidates: list[str] = []
    patterns = (
        re.compile(r"\bfrom\s+([A-Za-z0-9_./@+-]+)\s+import\b"),
        re.compile(r"\bimport\s+([A-Za-z0-9_./@+-]+)"),
        re.compile(r"\bfrom\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\brequire\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"\b(?:use|using)\s+([A-Za-z0-9_.\\]+)"),
    )
    for pattern in patterns:
        candidates.extend(match.group(1) for match in pattern.finditer(text))
    return any(
        _target_matches(candidate, target)
        or _target_matches(re.split(r"[./\\]", candidate, maxsplit=1)[0], target)
        for candidate in candidates
    )


def _is_safe_lazy_client_facade(path: Path) -> bool:
    """Recognize the public-client getter without exempting API invocations.

    ``OpenAiService.client`` must stay readable.  A lazy proxy may therefore
    expose the underlying client's ``responses`` resource, while model calls
    still run exclusively through ``@openai/agents``.  The broad source-member
    rule remains useful for cross-file aliases, so exempt only this exact
    getter shape when every direct access is the guarded return and no
    ``create`` call appears in the file.
    """

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    accesses = list(re.finditer(r"\.\s*responses\b", text))
    if not accesses or "new Proxy" not in text:
        return False
    if re.search(r"\bcreate\s*\(", text):
        return False
    guarded_return = re.compile(
        r"^\s*if\s*\([^\n]*\bproperty\s*===\s*['\"]responses['\"][^\n]*\)"
        r"\s*return\s+[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*|\(\))*\.responses\s*;?\s*$"
    )
    lines = text.splitlines()
    return all(
        guarded_return.match(lines[text.count("\n", 0, match.start())]) is not None
        for match in accesses
    )


def _is_safe_response_spy_only(path: Path) -> bool:
    """Treat a test spy's method lookup as inspection, not invocation."""

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if not re.search(r"\.\s*responses\b", text):
        return False
    without_spies = re.sub(
        r"\b(?:[A-Za-z_$][\w$]*\.)?spyOn\s*\(\s*[^,\n]*\.\s*responses\s*,\s*['\"]create['\"]\s*\)",
        "SAFE_RESPONSE_METHOD_INSPECTION",
        text,
    )
    return re.search(r"\.\s*responses\b", without_spies) is None


def _matches(
    root: Path,
    path: Path,
    *,
    kind: str,
    target: str,
    index: _ProjectIndex | None,
    cache: dict[Path, tuple[list[str], str]],
) -> bool:
    if kind == "literal":
        variants, base_code = _variants_for(path, index, cache)
        return _literal_contains(target, variants, base_code)
    if kind == "regex":
        if target == r"\.\s*responses\b" and (
            _is_safe_lazy_client_facade(path) or _is_safe_response_spy_only(path)
        ):
            return False
        variants, _ = _variants_for(path, index, cache)
        return _regex_contains(target, variants)
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target)
    relative = path.relative_to(root).as_posix()
    return fnmatchcase(relative.casefold(), target.casefold()) or fnmatchcase(
        path.name.casefold(), target.casefold()
    )


def scan_residue_rules(
    root: Path, rules: object
) -> list[dict[str, str]]:
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    rules_list = [row for row in rules if isinstance(row, Mapping)]
    all_paths: list[Path] = []
    seen: set[str] = set()
    for row in rules_list:
        patterns = row.get("paths", [])
        if not isinstance(patterns, Sequence) or isinstance(patterns, (str, bytes)):
            continue
        for path in _paths(root, tuple(str(item) for item in patterns)):
            key = path.as_posix()
            if key not in seen:
                seen.add(key)
                all_paths.append(path)
    index = _project_index(root, all_paths) if all_paths else None
    cache = index.variant_cache if index is not None else {}
    hits: list[dict[str, str]] = []
    for row in rules_list:
        identifier = row.get("id")
        kind = row.get("kind")
        target = row.get("target")
        patterns = row.get("paths", [])
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(kind, str)
            or not isinstance(target, str)
            or not target
            or not isinstance(patterns, Sequence)
            or isinstance(patterns, (str, bytes))
            or not patterns
            or any(
                not isinstance(item, str) or not _safe_relative(item)
                for item in patterns
            )
        ):
            raise ValueError(f"invalid residue rule: {identifier or '<unknown>'}")
        if kind not in _RULE_KINDS:
            continue
        for path in _paths(root, tuple(patterns)):
            matched = _matches(root, path, kind=str(kind), target=target, index=index, cache=cache)
            if matched:
                hits.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "token": target,
                        "kind": str(kind),
                        "rule_id": identifier,
                    }
                )
    return hits


def classify_residue_rules(
    root: Path, rules: object
) -> list[dict[str, object]]:
    """Classify residue rules without treating unsupported syntax as a failure."""

    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    supported = [row for row in rules if isinstance(row, Mapping) and row.get("kind") in _RULE_KINDS]
    hits_by_rule: dict[str, list[dict[str, str]]] = {}
    for hit in scan_residue_rules(root, supported):
        hits_by_rule.setdefault(str(hit["rule_id"]), []).append(hit)
    diagnostics: list[dict[str, object]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        if kind not in _RULE_KINDS:
            diagnostics.append(
                {
                    "rule_id": str(identifier or "<unknown>"),
                    "kind": str(kind or ""),
                    "status": "UNSUPPORTED",
                    "hits": [],
                    "reason": "residue rule syntax is not supported by the generic scanner",
                }
            )
            continue
        hits = hits_by_rule.get(str(identifier), [])
        diagnostics.append(
            {
                "rule_id": str(identifier),
                "kind": str(kind),
                "status": "FAIL" if hits else "PASS",
                "hits": hits,
            }
        )
    return diagnostics


__all__ = ["classify_residue_rules", "scan_residue_rules"]
