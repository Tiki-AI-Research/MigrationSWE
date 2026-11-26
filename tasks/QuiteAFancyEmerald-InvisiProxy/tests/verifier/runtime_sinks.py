"""Semantic runtime-sink detection for Node/TypeScript candidates.

The textual residue scanner is deliberately language-neutral: it can only see
literal bytes.  A hidden fallback to the source runtime (Fastify) can be
expressed without any literal ``fastify`` appearing in a contiguous form, for
example::

    import {createRequire as makeRequire} from 'node:module';
    const legacyRequire = makeRequire(import.meta.url);
    legacyRequire('fa' + 'stify')();

This module adds the semantic half required by the no-deceptive-fallback gate:

* import bindings are resolved (default, namespace, named and aliased imports,
  CommonJS ``require`` and dynamic ``await import``),
* string constants are folded through concatenation, template literals,
  character arrays and ``String.fromCharCode`` so split module names resolve,
* call sites are mapped back to their sink module, and
* five hidden-fallback classes are covered: child process, FFI, RPC/HTTP
  bridge, dynamic loading and an undeclared legacy runtime.

The scanner is intentionally conservative about the *legitimate* production
use of ``child_process``: this repository shells out to ``node``/``pnpm`` for
its build.  A ``child_process`` import or a ``node`` invocation is therefore
never a hit; only a resolved command that names the source runtime is.
"""

from __future__ import annotations

import base64
import bisect
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence


CHILD_PROCESS_MODULES = frozenset({"child_process", "node:child_process"})
CHILD_PROCESS_METHODS = frozenset(
    {"exec", "execSync", "execFile", "execFileSync", "spawn", "spawnSync", "fork"}
)
FFI_MODULES = frozenset(
    {
        "ffi",
        "ffi-napi",
        "node-ffi",
        "node-ffi-napi",
        "koffi",
        "ref",
        "ref-napi",
        "edge",
        "edge-js",
    }
)
RPC_MODULES = frozenset(
    {
        "net",
        "node:net",
        "dgram",
        "node:dgram",
        "tls",
        "node:tls",
        "ws",
        "undici",
        "grpc",
        "@grpc/grpc-js",
    }
)
VM_MODULES = frozenset({"vm", "node:vm"})
LEGACY_RUNTIME_MODULES = frozenset(
    {
        "fastify",
        "@fastify/static",
        "@fastify/helmet",
    }
)
DEFAULT_LEGACY_RUNTIMES = ("fastify",)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})
# File selection is by *content*, never by suffix: a hidden fallback can be
# parked in a ``.txt``/``.dat`` file, an extensionless shim, or a source file
# that a single inserted NUL byte used to mark as binary.  Every eligible file
# (the caller already prunes VCS/virtualenv/dependency trees and symlinks) is
# decoded and scanned, so the scanner's surface cannot be narrowed by a rename
# or a stray control byte.  Non-printable bytes become spaces rather than being
# dropped: a dropped byte could forge a token across a binary boundary
# (``re\xffquire`` -> ``require``) and a NUL-heavy blob stays small.
_TEXT_TRANSLATION = bytes(
    byte if (0x20 <= byte < 0x7F or byte in (0x09, 0x0A, 0x0D)) else 0x20
    for byte in range(256)
)


def _decode_text(raw: bytes) -> str:
    return raw.translate(_TEXT_TRANSLATION).decode("ascii")


# The semantic scanner is invoked once per (rule, file) pair, and the shipped
# policy runs four ``**/*`` runtime-sink rules plus a classification pass; the
# same file would otherwise be tokenised, comment-stripped and import-resolved
# many times over.  The analysis is a pure function of the file bytes, so it is
# memoised on (path, size, mtime) and reused across rules within a run.
_ANALYSIS_CACHE_SIZE = 1024


@lru_cache(maxsize=_ANALYSIS_CACHE_SIZE)
def _file_analysis(
    path_str: str, size: int, mtime_ns: int
) -> tuple | None:
    """Parse one file into the structures every runtime-sink rule shares."""

    path = Path(path_str)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = _decode_text(raw)
    code = strip_comments(text)
    ranges = tuple(_string_ranges(code))
    constants = _collect_constants(code, ranges)
    namespaces, members, module_aliases = _module_aliases(code, ranges)
    builtin_namespaces, builtin_sites = _builtin_module_sites(code, ranges, constants)
    namespaces.update(builtin_namespaces)
    module_aliases.update(builtin_namespaces)
    require_aliases = _require_aliases(code, ranges, namespaces, members)
    return (
        code,
        ranges,
        constants,
        namespaces,
        members,
        module_aliases,
        require_aliases,
        builtin_sites,
    )


def _cached_analysis(path: Path) -> tuple | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _file_analysis(str(path), stat.st_size, stat.st_mtime_ns)


class _StringLiteral:
    __slots__ = ("value", "end", "interpolated")

    def __init__(self, value: str | None, end: int, interpolated: bool = False):
        self.value = value
        self.end = end
        self.interpolated = interpolated


def strip_comments(text: str) -> str:
    """Blank comments while preserving offsets and string contents."""

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
                while index < length:
                    if text[index] == "*" and index + 1 < length and text[index + 1] == "/":
                        characters[index] = " "
                        characters[index + 1] = " "
                        index += 2
                        break
                    if text[index] not in "\r\n":
                        characters[index] = " "
                    index += 1
                continue
            if character in "'\"`":
                state = "string"
                quote = character
                index += 1
                continue
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character == quote:
            state = "normal"
            quote = ""
        index += 1
    return "".join(characters)


def _unescape(raw: str) -> str:
    return (
        raw.replace("\\\\", "\x00")
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\`", "`")
        .replace("\x00", "\\")
    )


def _read_string(text: str, index: int) -> _StringLiteral | None:
    if index >= len(text) or text[index] not in "'\"`":
        return None
    quote = text[index]
    cursor = index + 1
    if quote != "`":
        # Non-template literals have no interpolation: the closing quote is the
        # first unescaped occurrence, found by C-level ``str.find`` instead of a
        # Python character loop (binary assets are full of stray quotes, so the
        # loop made every scan O(text) per unmatched quote).
        end = cursor
        while True:
            end = text.find(quote, end)
            if end < 0:
                return None
            backslashes = 0
            probe = end - 1
            while probe >= cursor and text[probe] == "\\":
                backslashes += 1
                probe -= 1
            if backslashes % 2 == 0:
                break
            end += 1
        return _StringLiteral(_unescape(text[cursor:end]), end + 1, False)
    # Template literal: interpolation switches the value to "not foldable" but
    # the literal still runs to the matching backtick.
    interpolated = False
    parts: list[str] = []
    segment = cursor
    length = len(text)
    while cursor < length:
        character = text[cursor]
        if character == "\\" and cursor + 1 < length:
            cursor += 2
            continue
        if (
            character == "$"
            and cursor + 1 < length
            and text[cursor + 1] == "{"
        ):
            interpolated = True
            parts.append(text[segment:cursor])
            cursor = _skip_template_expression(text, cursor + 2)
            segment = cursor
            continue
        if character == quote:
            parts.append(text[segment:cursor])
            if interpolated:
                return _StringLiteral(None, cursor + 1, True)
            return _StringLiteral(_unescape("".join(parts)), cursor + 1, False)
        cursor += 1
    return None


def _skip_template_expression(text: str, index: int) -> int:
    depth = 1
    cursor = index
    while cursor < len(text):
        character = text[cursor]
        if character in "'\"`":
            nested = _read_string(text, cursor)
            if nested is None:
                return cursor
            cursor = nested.end
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return cursor


def _find_matching(text: str, open_index: int) -> int:
    """Return the index after the bracket matching ``text[open_index]``."""

    pairs = {"(": ")", "[": "]", "{": "}"}
    opening = text[open_index]
    closing = pairs.get(opening)
    if closing is None:
        return -1
    depth = 0
    cursor = open_index
    while cursor < len(text):
        character = text[cursor]
        if character in "'\"`":
            nested = _read_string(text, cursor)
            if nested is None:
                return -1
            cursor = nested.end
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return -1


def _split_top_level(expression: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    cursor = 0
    while cursor < len(expression):
        character = expression[cursor]
        if character in "'\"`":
            nested = _read_string(expression, cursor)
            if nested is None:
                cursor += 1
                continue
            cursor = nested.end
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == delimiter and depth == 0:
            parts.append(expression[start:cursor])
            start = cursor + 1
        cursor += 1
    parts.append(expression[start:])
    return parts


def _first_argument(code: str, open_index: int) -> str:
    end = _find_matching(code, open_index)
    if end < 0:
        return ""
    inner = code[open_index + 1 : end - 1]
    return _split_top_level(inner, ",")[0]


def _arguments(code: str, open_index: int) -> list[str]:
    end = _find_matching(code, open_index)
    if end < 0:
        return []
    inner = code[open_index + 1 : end - 1]
    if not inner.strip():
        return []
    return _split_top_level(inner, ",")


def _folded_argument_values(
    code: str, open_index: int, constants: Mapping[str, str]
) -> list[str]:
    """Fold every argument, including array-form command/argument lists.

    ``spawnSync(process.execPath, ['node_modules/fastify/fastify.js'])`` hides the
    legacy runtime in the *second* argument, and ``spawnSync(['j','e','s','t'])``
    hides it in an array literal.  Both are returned as candidate commands.
    """

    values: list[str] = []
    for argument in _arguments(code, open_index):
        folded = _eval_expr(argument, constants)
        if folded is not None:
            values.append(folded)
            continue
        array = re.fullmatch(r"\[(.*)\]", argument.strip(), re.DOTALL)
        if not array:
            continue
        items = [
            _eval_expr(part, constants)
            for part in _split_top_level(array.group(1), ",")
        ]
        if all(item is not None for item in items):
            values.append(" ".join(item for item in items if item is not None))
            values.append("".join(item for item in items if item is not None))
            values.extend(item for item in items if item is not None)
    return values


def _decode_base64(data: str) -> str | None:
    """Return the decoded text of a base64 payload, or ``None`` when invalid.

    ``atob`` and ``Buffer.from(x, 'base64').toString()`` are the standard way a
    candidate hides a decodable payload; both decode to the same bytes.  The
    payload is a *string* the child process would run, so a permissive utf-8
    decode with replacement is enough for the surrounding sink analysis.
    """

    cleaned = re.sub(r"\s+", "", data)
    if not cleaned:
        return None
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except ValueError:
        return None
    return raw.decode("utf-8", errors="replace")


def _unwrap_single_call(value: str, name: str) -> str | None:
    """Return the single argument text of ``name(<arg>)``, or ``None``.

    ``name`` is matched as a bare identifier (``atob(...)``) or as a member of
    the global object (``globalThis.atob(...)``).
    """

    match = re.fullmatch(r"(?:globalThis\s*\.\s*)?" + name + r"\s*\((.*)\)", value, re.DOTALL)
    return match.group(1) if match is not None else None


def _eval_template(text: str, constants: Mapping[str, str], depth: int) -> str | None:
    """Fold a template literal whose interpolations are all constant.

    ``import(`${JSON.stringify(moduleName)}`)`` is a hidden dynamic load whose
    module name only exists at run time unless the interpolated expression is
    folded, so every ``${...}`` must resolve to a string before the literal can
    be treated as a constant.
    """

    if not text or text[0] != "`":
        return None
    pieces: list[str] = []
    literal: list[str] = []
    cursor = 1
    while cursor < len(text):
        character = text[cursor]
        if character == "\\" and cursor + 1 < len(text):
            literal.append(text[cursor : cursor + 2])
            cursor += 2
            continue
        if character == "`":
            pieces.append(_unescape("".join(literal)))
            return "".join(pieces)
        if character == "$" and cursor + 1 < len(text) and text[cursor + 1] == "{":
            pieces.append(_unescape("".join(literal)))
            literal = []
            end = _skip_template_expression(text, cursor + 2)
            folded = _eval_expr(text[cursor + 2 : end - 1], constants, depth + 1)
            if folded is None:
                return None
            pieces.append(folded)
            cursor = end
            continue
        literal.append(character)
        cursor += 1
    return None


def _reduce_is_concat(callback: str) -> bool:
    """Return whether a ``reduce`` callback concatenates its two parameters.

    ``['f','a','s','t','i','f','y'].reduce((a, c) => a + c, '')`` builds the
    source runtime's name without ever writing it contiguously; the callback is
    recognised whenever it combines its accumulator and item with ``+`` or
    ``.concat(...)``, in arrow or ``function`` form.
    """

    expression = callback.strip()
    match = re.fullmatch(
        r"\(?\s*([A-Za-z_$][\w$]*)\s*,\s*([A-Za-z_$][\w$]*)\s*\)?\s*=>\s*(.+)",
        expression,
        re.DOTALL,
    )
    if match is not None:
        accumulator, item, body = match.group(1), match.group(2), match.group(3)
    else:
        match = re.fullmatch(r"function\s*\(([^)]*)\)\s*\{(.*)\}", expression, re.DOTALL)
        if match is None:
            return False
        params = [part.strip() for part in match.group(1).split(",") if part.strip()]
        if len(params) < 2:
            return False
        accumulator, item, body = params[0], params[1], match.group(2)
    body = body.strip().rstrip(";").strip()
    if body.startswith("return "):
        body = body[len("return ") :].strip().rstrip(";").strip()
    parts = _split_top_level(body, "+")
    if len(parts) == 2 and {part.strip() for part in parts} == {accumulator, item}:
        return True
    return (
        re.fullmatch(
            rf"{re.escape(accumulator)}\s*\.\s*concat\s*\(\s*{re.escape(item)}\s*\)",
            body,
        )
        is not None
    )


def _fold_charcode_sequence(
    text: str, constants: Mapping[str, str], depth: int
) -> str | None:
    """Fold a comma-separated list of integer char codes into a string.

    Shared by ``String.fromCharCode(...)``, ``String.fromCodePoint(...)`` and
    ``String.fromCharCode.apply(null, [...])``.  A runtime-assembled runtime
    name (``String.fromCodePoint(102, 97, ...)``) folds exactly like the
    contiguous literal it replaces, so the sink analysis stays semantic.
    """

    codepoints: list[str] = []
    for part in _split_top_level(text, ","):
        folded = _eval_expr(part, constants, depth + 1)
        raw = folded if folded is not None else part.strip()
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|\d{1,7})", raw):
            return None
        integer = int(raw, 0)
        if integer < 0 or integer > 0x10FFFF:
            return None
        codepoints.append(chr(integer))
    return "".join(codepoints)


def _fold_byte_array(
    text: str, constants: Mapping[str, str], depth: int
) -> str | None:
    """Fold ``Buffer.from([102, 97, ...]).toString()`` into its byte string.

    A byte array is another way to rebuild a runtime name at run time
    (``Buffer.from([102, 97, ...]).toString()`` is ``'fastify'``); decoding it
    here keeps the sink analysis semantic rather than textual.
    """

    array = re.fullmatch(r"\[(.*)\]", text.strip(), re.DOTALL)
    if array is None:
        return None
    parts = _split_top_level(array.group(1), ",")
    if not parts or (len(parts) == 1 and not parts[0].strip()):
        return None
    data = bytearray()
    for part in parts:
        folded = _eval_expr(part, constants, depth + 1)
        raw = folded if folded is not None else part.strip()
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]{1,2}|\d{1,3})", raw):
            return None
        integer = int(raw, 0)
        if not 0 <= integer <= 0xFF:
            return None
        data.append(integer)
    return bytes(data).decode("utf-8", errors="replace")


def _eval_expr(expression: str, constants: Mapping[str, str], depth: int = 0) -> str | None:
    """Fold a string expression, or return ``None`` when it is not constant."""

    if depth > 8:
        return None
    value = expression.strip().rstrip(";").strip()
    if not value:
        return None
    if value.startswith("(") and _find_matching(value, 0) == len(value):
        return _eval_expr(value[1:-1], constants, depth + 1)
    # ``atob('<base64>')`` decodes a payload that may later be handed to a child
    # process; folding it here keeps the analysis semantic instead of textual.
    inner = _unwrap_single_call(value, "atob")
    if inner is not None:
        encoded = _eval_expr(inner, constants, depth + 1)
        return None if encoded is None else _decode_base64(encoded)
    # ``Buffer.from('<base64>', 'base64').toString(...)`` is the Node-side twin.
    buffer_from = re.fullmatch(
        r"Buffer\s*\.\s*from\s*\((.*)\)\s*\.\s*toString\s*\((.*)\)",
        value,
        re.DOTALL,
    )
    if buffer_from is not None:
        buffer_args = _split_top_level(buffer_from.group(1), ",")
        first = buffer_args[0] if buffer_args else ""
        if len(buffer_args) >= 2:
            encoding = _eval_expr(buffer_args[1], constants, depth + 1)
            if encoding is not None and encoding.strip().casefold() in {
                "base64",
                "base64url",
            }:
                encoded = _eval_expr(first, constants, depth + 1)
                return None if encoded is None else _decode_base64(encoded)
        # A byte array is the Node-side twin of ``String.fromCodePoint``:
        # ``Buffer.from([102, 97, ...]).toString()`` rebuilds a runtime name.
        folded_bytes = _fold_byte_array(first, constants, depth)
        return folded_bytes
    if value.startswith("`"):
        return _eval_template(value, constants, depth)
    json_stringify = re.fullmatch(r"JSON\s*\.\s*stringify\s*\((.*)\)", value, re.DOTALL)
    if json_stringify is not None:
        inner = _eval_expr(json_stringify.group(1), constants, depth + 1)
        return None if inner is None else json.dumps(inner, ensure_ascii=False)
    reduce_call = re.fullmatch(r"\[(.*)\]\s*\.\s*reduce\s*\((.*)\)", value, re.DOTALL)
    if reduce_call is not None:
        items = [
            _eval_expr(part, constants, depth + 1)
            for part in _split_top_level(reduce_call.group(1), ",")
        ]
        if not items or not all(item is not None for item in items):
            return None
        reduce_args = _split_top_level(reduce_call.group(2), ",")
        if not reduce_args or not _reduce_is_concat(reduce_args[0]):
            return None
        sequence = [item for item in items if item is not None]
        if len(reduce_args) >= 2:
            initial = _eval_expr(reduce_args[1], constants, depth + 1)
            if initial is None:
                return None
            sequence = [initial, *sequence]
        return "".join(sequence)
    literal = _read_string(value, 0)
    if literal is not None and literal.end == len(value) and not literal.interpolated:
        return literal.value
    # ``['y','f','i','t','s','a','f'].reverse().join('')`` reverses a character
    # array before joining it; folding the array methods the same way as a plain
    # ``join`` keeps a reversed runtime name from escaping the sink analysis.
    match = re.fullmatch(
        r"\[(.*)\]\s*\.\s*(reverse|toReversed|sort|toSorted)\s*\(\s*\)"
        r"\s*\.\s*join\s*\(\s*(['\"`])(.*?)\3\s*\)",
        value,
        re.DOTALL,
    )
    if match:
        items = [
            _eval_expr(part, constants, depth + 1)
            for part in _split_top_level(match.group(1), ",")
        ]
        if not all(item is not None for item in items):
            return None
        resolved = [item for item in items if item is not None]
        if match.group(2) in {"reverse", "toReversed"}:
            resolved = list(reversed(resolved))
        else:
            resolved = sorted(resolved)
        return match.group(4).join(resolved)
    match = re.fullmatch(
        r"\[(.*)\]\s*\.\s*join\s*\(\s*(['\"`])(.*?)\2\s*\)", value, re.DOTALL
    )
    if match:
        items = [
            _eval_expr(part, constants, depth + 1)
            for part in _split_top_level(match.group(1), ",")
        ]
        if all(item is not None for item in items):
            return match.group(3).join(item for item in items if item is not None)
        return None
    match = re.fullmatch(
        r"(?:String\s*\.\s*)?fromCharCode\s*\.\s*apply\s*\(\s*[^,()]*,\s*(.*)\)",
        value,
        re.DOTALL,
    )
    if match:
        array = re.fullmatch(r"\[(.*)\]", match.group(1).strip(), re.DOTALL)
        return None if array is None else _fold_charcode_sequence(
            array.group(1), constants, depth
        )
    match = re.fullmatch(
        r"(?:String\s*\.\s*)?fromCharCode\s*\((.*)\)", value, re.DOTALL
    )
    if match:
        return _fold_charcode_sequence(match.group(1), constants, depth)
    match = re.fullmatch(
        r"(?:String\s*\.\s*)?fromCodePoint\s*\((.*)\)", value, re.DOTALL
    )
    if match:
        return _fold_charcode_sequence(match.group(1), constants, depth)
    match = re.fullmatch(r"\[(.*)\]\s*\[\s*(\d+)\s*\]", value, re.DOTALL)
    if match:
        items = [
            _eval_expr(part, constants, depth + 1)
            for part in _split_top_level(match.group(1), ",")
        ]
        index = int(match.group(2))
        if all(item is not None for item in items) and index < len(items):
            return items[index]
        return None
    if re.fullmatch(r"[A-Za-z_$][\w$]*", value):
        return constants.get(value)
    parts = _split_top_level(value, "+")
    if len(parts) > 1:
        folded_parts = [_eval_expr(part, constants, depth + 1) for part in parts]
        if all(part is not None for part in folded_parts):
            return "".join(part for part in folded_parts if part is not None)
    return None


def _string_ranges(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` ranges occupied by string literals."""

    ranges: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(text):
        character = text[cursor]
        if character in "'\"`":
            literal = _read_string(text, cursor)
            if literal is None:
                cursor += 1
                continue
            ranges.append((cursor, literal.end))
            cursor = literal.end
            continue
        cursor += 1
    return ranges


def _inside(ranges: Sequence[tuple[int, int]], position: int) -> bool:
    if not ranges:
        return False
    # ``ranges`` is sorted by start and ``bisect_right`` with a key avoids
    # rebuilding a starts list on every one of the many membership tests a
    # large file triggers.
    index = bisect.bisect_right(ranges, position, key=lambda item: item[0]) - 1
    return index >= 0 and ranges[index][0] <= position < ranges[index][1]


_ASSIGNMENT = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)"
)


def _collect_constants(
    code: str, ranges: Sequence[tuple[int, int]] | None = None
) -> dict[str, str]:
    constants: dict[str, str] = {}
    ranges = ranges if ranges is not None else _string_ranges(code)
    for _ in range(4):
        changed = False
        for match in _ASSIGNMENT.finditer(code):
            if _inside(ranges, match.start()):
                continue
            name = match.group(1)
            if name in constants:
                continue
            folded = _eval_expr(match.group(2), constants)
            if folded is not None:
                constants[name] = folded
                changed = True
        if not changed:
            break
    return constants


_IMPORT_FROM = re.compile(
    r"\bimport\s+(?!\()([^;]+?)\s+from\s+(['\"])([^'\"]+)\2", re.DOTALL
)
_SIDE_EFFECT_IMPORT = re.compile(r"\bimport\s+(['\"])([^'\"]+)\1")
_REQUIRE_NAMESPACE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:await\s+)?require\s*\(\s*(['\"])([^'\"]+)\2\s*\)"
)
_REQUIRE_DESTRUCTURED = re.compile(
    r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=\s*(?:await\s+)?require\s*\(\s*(['\"])([^'\"]+)\2\s*\)",
    re.DOTALL,
)
_AWAIT_IMPORT_NAMESPACE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*await\s+import\s*\(\s*(['\"])([^'\"]+)\2\s*\)"
)
_AWAIT_IMPORT_DESTRUCTURED = re.compile(
    r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=\s*await\s+import\s*\(\s*(['\"])([^'\"]+)\2\s*\)",
    re.DOTALL,
)
_NAMESPACE_DESTRUCTURED = re.compile(
    r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=\s*([A-Za-z_$][\w$]*)\s*(?:;|\n|$)",
)


def _normalize_module(module: str) -> str:
    return module.strip().casefold()


def _module_aliases(
    code: str, ranges: Sequence[tuple[int, int]] | None = None
) -> tuple[dict[str, str], dict[str, tuple[str, str]], dict[str, str]]:
    """Return ``namespaces``, ``members`` and ``modules`` (all aliases).

    ``namespaces`` maps a local alias to its module.  ``members`` maps a local
    binding to ``(module, imported_member)`` so ``spawnSync as legacySpawn``
    resolves back to ``child_process.spawnSync``.  ``modules`` maps the alias
    that represents the module itself (default/namespace/require) for import
    level checks.
    """

    namespaces: dict[str, str] = {}
    members: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}

    def record_clause(clause: str, module: str) -> None:
        clause = clause.strip()
        if not clause or clause.startswith("type "):
            return
        for part in _split_top_level(clause, ","):
            item = part.strip()
            if not item:
                continue
            if item.startswith("type "):
                item = item[5:].strip()
            namespace = re.fullmatch(r"\*\s*as\s+([A-Za-z_$][\w$]*)", item)
            if namespace:
                namespaces[namespace.group(1)] = module
                modules[namespace.group(1)] = module
                continue
            group = re.fullmatch(r"\{(.*)\}", item, re.DOTALL)
            if group:
                modules.setdefault(module, module)
                for entry in _split_top_level(group.group(1), ","):
                    specifier = entry.strip()
                    if not specifier or specifier.startswith("type "):
                        continue
                    alias = re.fullmatch(
                        r"([A-Za-z_$][\w$]*)\s+as\s+([A-Za-z_$][\w$]*)", specifier
                    )
                    if alias:
                        members[alias.group(2)] = (module, alias.group(1))
                    elif re.fullmatch(r"[A-Za-z_$][\w$]*", specifier):
                        members[specifier] = (module, specifier)
                continue
            if re.fullmatch(r"[A-Za-z_$][\w$]*", item):
                namespaces[item] = module
                modules[item] = module

    ranges = ranges if ranges is not None else _string_ranges(code)

    for match in _IMPORT_FROM.finditer(code):
        if _inside(ranges, match.start()):
            continue
        record_clause(match.group(1), match.group(3))
    for match in _REQUIRE_NAMESPACE.finditer(code):
        if _inside(ranges, match.start()):
            continue
        namespaces[match.group(1)] = match.group(3)
        modules[match.group(1)] = match.group(3)
    for pattern in (_AWAIT_IMPORT_NAMESPACE,):
        for match in pattern.finditer(code):
            if _inside(ranges, match.start()):
                continue
            namespaces[match.group(1)] = match.group(3)
            modules[match.group(1)] = match.group(3)
    for pattern in (_REQUIRE_DESTRUCTURED, _AWAIT_IMPORT_DESTRUCTURED):
        for match in pattern.finditer(code):
            if _inside(ranges, match.start()):
                continue
            module = match.group(3)
            for entry in _split_top_level(match.group(1), ","):
                specifier = entry.strip()
                if not specifier:
                    continue
                alias = re.fullmatch(
                    r"([A-Za-z_$][\w$]*)\s*:\s*([A-Za-z_$][\w$]*)", specifier
                )
                if alias:
                    members[alias.group(2)] = (module, alias.group(1))
                elif re.fullmatch(r"[A-Za-z_$][\w$]*", specifier):
                    members[specifier] = (module, specifier)
    # ``const {spawnSync: legacy} = cp`` after ``import cp from ...``.
    for match in _NAMESPACE_DESTRUCTURED.finditer(code):
        if _inside(ranges, match.start()):
            continue
        module = namespaces.get(match.group(2))
        if not module:
            continue
        for entry in _split_top_level(match.group(1), ","):
            specifier = entry.strip()
            if not specifier:
                continue
            alias = re.fullmatch(
                r"([A-Za-z_$][\w$]*)\s*:\s*([A-Za-z_$][\w$]*)", specifier
            )
            if alias:
                members[alias.group(2)] = (module, alias.group(1))
            elif re.fullmatch(r"[A-Za-z_$][\w$]*", specifier):
                members[specifier] = (module, specifier)
    return namespaces, members, modules


_CREATE_REQUIRE_MODULES = frozenset({"module", "node:module"})
_INLINE_EVAL_FLAGS = frozenset({"-e", "--eval", "-p", "--print"})
_INLINE_MODULE_CALL = re.compile(r"\b(?:require|import)\s*\(\s*([^()]*)\)")
_ASSIGNMENT_STATEMENT = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)"
)


def _is_create_require_call(
    expression: str,
    factories: set[str],
    namespaces: Mapping[str, str],
) -> bool:
    """Return whether an expression evaluates to a ``createRequire`` callable."""

    value = expression.strip()
    if value.startswith("(") and _find_matching(value, 0) == len(value):
        value = value[1:-1].strip()
    local = re.match(r"([A-Za-z_$][\w$]*)\s*\(", value)
    if local is not None and local.group(1) in factories:
        return True
    member = re.match(r"([A-Za-z_$][\w$]*)\s*\.\s*createRequire\s*\(", value)
    if member is not None:
        module = _normalize_module(namespaces.get(member.group(1), ""))
        if module in _CREATE_REQUIRE_MODULES:
            return True
    if re.match(
        r"\(?\s*require\s*\(\s*(['\"])(?:node:)?module\1\s*\)\s*\)?"
        r"\s*\.\s*createRequire\s*\(",
        value,
    ):
        return True
    return False


def _require_aliases(
    code: str,
    ranges: Sequence[tuple[int, int]],
    namespaces: Mapping[str, str],
    members: Mapping[str, tuple[str, str]],
) -> set[str]:
    """Resolve local require callables created through ``createRequire``.

    ``import {createRequire as makeRequire} from 'node:module'; const r =
    makeRequire(import.meta.url); r('fastify')`` hides a legacy runtime load behind
    a callable the raw ``require(...)`` regex cannot see.
    """

    factories = {
        name
        for name, (module, member) in members.items()
        if member == "createRequire"
        and _normalize_module(module) in _CREATE_REQUIRE_MODULES
    }
    aliases: set[str] = set()
    for _ in range(4):
        changed = False
        for match in _ASSIGNMENT_STATEMENT.finditer(code):
            if _inside(ranges, match.start()):
                continue
            name = match.group(1)
            if name in aliases:
                continue
            rhs = match.group(2).strip()
            if _is_create_require_call(rhs, factories, namespaces):
                aliases.add(name)
                changed = True
            elif re.fullmatch(r"[A-Za-z_$][\w$]*", rhs) and rhs in aliases:
                aliases.add(name)
                changed = True
        if not changed:
            break
    return aliases


def _inline_payload_mentions_legacy(
    payload: str,
    constants: Mapping[str, str],
    legacy_runtimes: Sequence[str],
) -> bool:
    """Analyse a Node ``-e``/``--eval`` payload for a legacy runtime load."""

    for match in _INLINE_MODULE_CALL.finditer(payload):
        argument = match.group(1)
        folded = _eval_expr(argument, constants)
        target = folded if folded is not None else argument
        if _mentions_legacy_runtime(target, constants, legacy_runtimes):
            return True
    return False


def _child_process_commands(
    code: str,
    open_index: int,
    constants: Mapping[str, str],
    legacy_runtimes: Sequence[str],
) -> list[str]:
    """Return every argument form that names a legacy runtime.

    Handles plain strings, array-form argument lists, folded constants and the
    inline script passed to ``node -e``/``--eval``.  The latter nests the load
    inside a string, so the payload is analysed as JavaScript rather than
    matched textually (``'requ' + "ire('je' + 'st')"``).
    """

    matches: list[str] = []
    for argument in _arguments(code, open_index):
        folded = _eval_expr(argument, constants)
        if folded is not None:
            if _is_legacy_command(folded, legacy_runtimes):
                matches.append(folded)
            continue
        array = re.fullmatch(r"\[(.*)\]", argument.strip(), re.DOTALL)
        if not array:
            continue
        items = [
            _eval_expr(part, constants)
            for part in _split_top_level(array.group(1), ",")
        ]
        resolved = [item for item in items if item is not None]
        inline_flag = any(
            item is not None and item.strip() in _INLINE_EVAL_FLAGS
            for item in items
        )
        # When the argument list carries an inline script, the payload is
        # analysed as JavaScript below; matching its raw text would treat a
        # harmless literal such as ``console.log('fastify')`` as a command.
        if not inline_flag:
            for item in resolved:
                if _is_legacy_command(item, legacy_runtimes):
                    matches.append(item)
            if all(item is not None for item in items):
                resolved_items = [item for item in items if item is not None]
                joined = " ".join(resolved_items)
                if _is_legacy_command(joined, legacy_runtimes):
                    matches.append(joined)
                concatenated = "".join(resolved_items)
                if concatenated != joined and _is_legacy_command(
                    concatenated, legacy_runtimes
                ):
                    matches.append(concatenated)
        for index, item in enumerate(items):
            if item is None or item.strip() not in _INLINE_EVAL_FLAGS:
                continue
            payload = " ".join(
                part for part in items[index + 1 :] if part is not None
            )
            if _inline_payload_mentions_legacy(payload, constants, legacy_runtimes):
                matches.append(f"{item} {payload}")
    return matches


_MEMBER_CALL = re.compile(
    r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?:\?\.|\.)\s*([A-Za-z_$][\w$]*)\s*(?:\?\.)?\("
)
# ``ns[key](...)``: the property key is folded with the same constants as the
# command names, so ``cp['spawn' + 'Sync'](...)`` and ``cp[method](...)``
# resolve to the very same call site as ``cp.spawnSync(...)``.  A non-constant
# key folds to ``None`` and is skipped rather than treated as text.  Only the
# ``ident[`` head is matched here; the closing bracket is found by a bounded
# linear ``str.find`` (a ``\[[^\]]*?\]`` body would backtrack catastrophically
# over binary data, and an unbounded balance scan over an unmatched ``[`` costs
# O(file) per occurrence).
_BRACKET_HEAD = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?:\?\.)?\[")
# A computed property key that can fold to an identifier is always a short
# expression; bounding the search keeps an unmatched ``[`` from scanning the
# rest of a multi-megabyte binary blob.
_MAX_BRACKET_KEY = 4096
_BARE_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?:\?\.)?\(")
# ``(0, ns.member)(...)`` strips the receiver while keeping the binding.
_INDIRECT_MEMBER_CALL = re.compile(
    r"\(\s*0\s*,\s*([A-Za-z_$][\w$]*)\s*\.\s*([A-Za-z_$][\w$]*)\s*\)\s*\("
)
# ``process.getBuiltinModule(<module expression>)`` (Node >=22) and its
# notation-equivalent forms.  Receiver, loader property and trailing member are
# resolved by folding the expression, so a computed loader key
# (``process['get' + 'BuiltinModule']``), a loader kept in a constant
# (``process[loader]``), an array subscript (``['node:' + 'child_process'][0]``)
# and an inline member chain all take the same path.
_BUILTIN_RECEIVER = r"(?:\(\s*process\s+as\s+[^)]*\)|\bprocess)"
# A module loaded inline by ``require('m')`` / ``(await import('m'))``; there is
# no namespace binding to resolve, so the trailing member is folded directly.
_MODULE_LOAD_HEAD = re.compile(
    r"(?:require|await\s+import)\s*\(\s*(['\"])([^'\"]+)\1\s*\)"
)


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _read_member_key(
    code: str, index: int, constants: Mapping[str, str]
) -> tuple[str, int] | None:
    """Resolve ``.key`` / ``?.key`` / ``['key']`` / ``[expr]`` to a property.

    The bracket form is folded with the same constants as command names, so a
    computed member name (``obj['spawn' + 'Sync']``, ``obj[method]``) resolves
    exactly like the dot form.  Anything that is not a constant identifier is
    not a hit: an opaque key cannot be shown to reach a sink.
    """

    index = _skip_whitespace(code, index)
    if code.startswith("?.", index):
        index = _skip_whitespace(code, index + 2)
    elif index < len(code) and code[index] == ".":
        index = _skip_whitespace(code, index + 1)
    elif index < len(code) and code[index] == "[":
        end = _find_matching(code, index)
        if end < 0:
            return None
        folded = _eval_expr(code[index + 1 : end - 1], constants)
        if folded is None or not re.fullmatch(r"[A-Za-z_$][\w$]*", folded):
            return None
        return folded, end
    else:
        return None
    match = re.match(r"[A-Za-z_$][\w$]*", code[index:])
    if match is None:
        return None
    return match.group(0), index + match.end()


def _builtin_module_sites(
    code: str, ranges: Sequence[tuple[int, int]], constants: Mapping[str, str]
) -> tuple[dict[str, str], list[tuple[int, str, str, str]]]:
    """Resolve ``process.getBuiltinModule`` bindings and inline call sites.

    Returns ``namespaces`` (local alias -> module) for the assignment form and
    ``sites`` (``(open_paren, callee, member, module)``) for the inline chain
    ``process.getBuiltinModule('m').member(...)``.
    """

    namespaces: dict[str, str] = {}
    sites: list[tuple[int, str, str, str]] = []
    for match in re.finditer(_BUILTIN_RECEIVER, code):
        if _inside(ranges, match.start()):
            continue
        loader = _read_member_key(code, match.end(), constants)
        if loader is None or loader[0] != "getBuiltinModule":
            continue
        open_index = _skip_whitespace(code, loader[1])
        if open_index >= len(code) or code[open_index] != "(":
            continue
        end = _find_matching(code, open_index)
        if end < 0:
            continue
        module = _eval_expr(code[open_index + 1 : end - 1], constants)
        if module is None:
            continue
        normalized = _normalize_module(module)
        assignment = re.search(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*$", code[: match.start()]
        )
        if assignment is not None:
            namespaces[assignment.group(1)] = normalized
        member = _read_member_key(code, end, constants)
        if member is None:
            continue
        paren = _skip_whitespace(code, member[1])
        if paren < len(code) and code[paren] == "(":
            sites.append((paren, "process", member[0], normalized))
    return namespaces, sites


def _direct_module_sites(
    code: str,
    ranges: Sequence[tuple[int, int]],
    constants: Mapping[str, str],
) -> list[tuple[int, str, str, str]]:
    """Resolve ``require('m').member(...)`` (and ``['member']``) call sites."""

    sites: list[tuple[int, str, str, str]] = []
    for match in _MODULE_LOAD_HEAD.finditer(code):
        if _inside(ranges, match.start()):
            continue
        module = _normalize_module(match.group(2))
        cursor = _skip_whitespace(code, match.end())
        if cursor < len(code) and code[cursor] == ")":
            cursor = _skip_whitespace(code, cursor + 1)
        member = _read_member_key(code, cursor, constants)
        if member is None:
            continue
        paren = _skip_whitespace(code, member[1])
        if paren < len(code) and code[paren] == "(":
            sites.append((paren, module, member[0], module))
    return sites


def _data_url_payloads(value: str) -> list[str]:
    """Decode the payload of a ``data:`` URL, base64 or percent-encoded.

    ``import('data:text/javascript;base64,<payload>')`` loads a module whose
    source never appears literally in the candidate; decoding the URL keeps the
    dynamic-load analysis semantic instead of textual.
    """

    match = re.match(r"\s*data:([^,]*),(.*)$", value, re.DOTALL)
    if match is None:
        return []
    metadata = match.group(1).casefold()
    payload = match.group(2)
    if "base64" in metadata:
        decoded = _decode_base64(payload)
        return [decoded] if decoded is not None else []
    from urllib.parse import unquote

    return [unquote(payload)]


def _mentions_legacy_runtime(
    expression: str,
    constants: Mapping[str, str],
    legacy_runtimes: Sequence[str],
) -> bool:
    """Return whether a dynamic target can resolve to a legacy runtime.

    The argument often embeds the runtime in a larger path or a conditional
    expression, so every string constant and every folded local identifier is
    considered.  An opaque value such as ``process.env.RUNNER`` is not a hit:
    the scanner cannot show that it loads the source runtime.  A ``data:`` URL
    is decoded (base64 or percent-encoded) because its payload is the module
    source, not an opaque string.
    """

    candidates = [expression]
    for match in re.finditer(r"[A-Za-z_$][\w$]*", expression):
        value = constants.get(match.group(0))
        if value is not None:
            candidates.append(value)
    for candidate in candidates:
        folded = _eval_expr(candidate, constants)
        if folded is not None:
            candidate = folded
        if _is_legacy_command(candidate, legacy_runtimes):
            return True
        for payload in _data_url_payloads(candidate):
            if _is_legacy_command(payload, legacy_runtimes) or (
                _inline_payload_mentions_legacy(payload, constants, legacy_runtimes)
            ):
                return True
    return False


def _dynamic_code_is_fallback(
    code: str,
    open_index: int,
    constants: Mapping[str, str],
    legacy_runtimes: Sequence[str],
) -> bool:
    """Whether a dynamically evaluated program is a hidden runtime fallback.

    ``eval``, the ``Function`` constructor and the ``vm`` module execute code
    that does not appear in the surrounding source, so they are dynamic-load
    sinks.  Bundled third-party code (for example wasm-bindgen glue emitted into
    a build-output directory) also uses them with no relation to the migrated
    framework; treating every such call as a deceptive fallback turned a normal
    build into a cheating verdict.  The sink therefore only counts when the code
    being evaluated references the source runtime — directly, through a local
    constant, or as an obfuscated module specifier.
    """

    end = _find_matching(code, open_index)
    span = code[open_index + 1 : end - 1] if end > 0 else code[open_index + 1 :]
    if not span.strip():
        return False
    return _mentions_legacy_runtime(span, constants, legacy_runtimes) or (
        _is_obfuscated_module_specifier(span)
    )


def _is_legacy_command(value: str, legacy_runtimes: Sequence[str]) -> bool:
    folded = value.strip().replace("\\", "/").casefold()
    if not folded:
        return False
    first = re.split(r"\s+", folded, maxsplit=1)[0].strip("'\"")
    for part in filter(None, first.split("/")):
        base = part[:-3] if part.endswith(".js") else part
        for name in legacy_runtimes:
            if base == name or base.startswith(name + "-"):
                return True
    return any(
        re.search(rf"(?<![\w-]){re.escape(name.casefold())}(?![\w-])", folded)
        for name in legacy_runtimes
    )


def _loopback_url(value: str | None) -> bool:
    if not value:
        return False
    match = re.search(r"^[a-z]+://([^/:?#]+)", value.strip(), re.IGNORECASE)
    host = match.group(1).casefold() if match else ""
    return host in _LOOPBACK_HOSTS


def _identifier_positions(
    code: str, ranges: Sequence[tuple[int, int]], name: str
) -> list[int]:
    """Return offsets where ``name`` appears as a real identifier (not prose)."""

    if not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
        return []
    pattern = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    return [
        match.start()
        for match in pattern.finditer(code)
        if not _inside(ranges, match.start())
    ]


def _void_discard_offsets(
    code: str, ranges: Sequence[tuple[int, int]], name: str
) -> set[int]:
    """Offsets of identifiers consumed by the deliberate ``void x`` idiom."""

    if not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
        return set()
    pattern = re.compile(rf"\bvoid\s+({re.escape(name)})\b")
    return {
        match.start(1)
        for match in pattern.finditer(code)
        if not _inside(ranges, match.start())
    }


def _rpc_binding_used(
    code: str,
    ranges: Sequence[tuple[int, int]],
    alias: str,
    module: str,
    members: Mapping[str, tuple[str, str]],
) -> bool:
    """Whether an imported RPC transport binding is actually referenced.

    The import-level RPC rule must not fire on an *unused* import: a benign
    ``import net from 'node:net'; void net;`` (added only to silence a linter)
    must not be scored as a hidden bridge.  A default/namespace binding counts
    as used when it is referenced outside its own declaration, ignoring the
    ``void x`` discard idiom; a named/destructured import counts when any of its
    local member aliases is referenced.
    """

    positions = _identifier_positions(code, ranges, alias)
    if positions:
        discards = _void_discard_offsets(code, ranges, alias)
        # One occurrence is the binding declaration itself.
        if len([position for position in positions if position not in discards]) - 1 > 0:
            return True
    normalized = _normalize_module(module)
    for member_alias, (member_module, _member) in members.items():
        if _normalize_module(member_module) != normalized:
            continue
        if _identifier_positions(code, ranges, member_alias):
            return True
    return False


_OBFUSCATED_ARRAY_MODULE = re.compile(
    r"^\s*\[(.*)\]\s*\.\s*"
    r"(?:reverse|toReversed|sort|toSorted|join|reduce|flat|flatMap|concat|slice)\s*\(",
    re.DOTALL,
)


def _is_obfuscated_module_specifier(expression: str) -> bool:
    """Whether a dynamic target is an inline string-array module name.

    A module specifier written as a literal array of string fragments combined
    by array methods (``['y','f','i','t','s','a','f'].reverse().join('')``) is
    obfuscation, never legitimate production code; the folder resolves the name
    when it can, and this catches the forms whose result still cannot be shown
    to be the source runtime.
    """

    match = _OBFUSCATED_ARRAY_MODULE.match(expression.strip().lstrip("("))
    if match is None:
        return False
    parts = [part.strip() for part in _split_top_level(match.group(1), ",")]
    if not parts:
        return False
    return all(
        re.fullmatch(r"(['\"])(.*?)\1", part, re.DOTALL) for part in parts if part
    ) and any(parts)


def _hit(
    *,
    path: Path,
    rule_id: str,
    category: str,
    module: str,
    method: str,
    detail: Mapping[str, object],
) -> dict[str, object]:
    token = category if not method else f"{category}:{module}.{method}"
    return {
        "path": path.as_posix(),
        "token": token,
        "kind": "runtime_sink",
        "rule_id": rule_id,
        "category": category,
        "module": module,
        "method": method,
        "spans": [],
        "detail": dict(detail),
    }


def scan_runtime_sinks(
    path: Path,
    *,
    rule_id: str,
    category: str,
    modules: Sequence[str],
    legacy_runtimes: Sequence[str] = DEFAULT_LEGACY_RUNTIMES,
    text: str | None = None,
) -> list[dict[str, object]]:
    """Return hidden-fallback hits for one file and one runtime-sink rule."""

    if text is None:
        analysis = _cached_analysis(path)
        if analysis is None:
            return []
        (
            code,
            ranges,
            constants,
            namespaces,
            members,
            module_aliases,
            require_aliases,
            builtin_sites,
        ) = analysis
    else:
        code = strip_comments(text)
        ranges = tuple(_string_ranges(code))
        constants = _collect_constants(code, ranges)
        namespaces, members, module_aliases = _module_aliases(code, ranges)
        builtin_namespaces, builtin_sites = _builtin_module_sites(
            code, ranges, constants
        )
        namespaces.update(builtin_namespaces)
        module_aliases.update(builtin_namespaces)
        require_aliases = _require_aliases(code, ranges, namespaces, members)
    declared = {_normalize_module(str(module)) for module in modules}
    legacy = tuple(str(name).casefold() for name in legacy_runtimes) or DEFAULT_LEGACY_RUNTIMES
    hits: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()

    def emit(module: str, method: str, detail: Mapping[str, object]) -> None:
        key = (module, method)
        if key in seen:
            return
        seen.add(key)
        hits.append(
            _hit(
                path=path,
                rule_id=rule_id,
                category=category,
                module=module,
                method=method,
                detail=detail,
            )
        )

    # Import-level sinks: FFI, raw RPC transports and an undeclared legacy
    # runtime are never legitimate in the migrated production code.
    if category in {"ffi", "rpc_http", "dynamic_load", "legacy_runtime"}:
        for alias, module in module_aliases.items():
            normalized = _normalize_module(module)
            if not normalized:
                continue
            matched = normalized in declared
            if category == "legacy_runtime":
                matched = matched or normalized.startswith("@fastify/")
            elif category == "dynamic_load":
                matched = matched or normalized in VM_MODULES
            if matched:
                if category == "rpc_http" and not _rpc_binding_used(
                    code, ranges, alias, module, members
                ):
                    continue
                emit(normalized, f"import:{alias}", {"alias": alias})

    # Function-level sinks.  Each entry carries the index of the call's ``(``
    # and an optional module resolved without a local binding (inline
    # ``require('m')`` / ``process.getBuiltinModule('m')``).
    call_sites: list[tuple[int, str, str | None, str | None]] = []
    for match in _MEMBER_CALL.finditer(code):
        if _inside(ranges, match.start()):
            continue
        call_sites.append((match.end() - 1, match.group(1), match.group(2), None))
    for match in _BRACKET_HEAD.finditer(code):
        if _inside(ranges, match.start()):
            continue
        bracket = match.end() - 1
        close = code.find("]", bracket + 1, bracket + 1 + _MAX_BRACKET_KEY)
        if close < 0:
            continue
        inner = code[bracket + 1 : close]
        # A nested ``[`` means the first ``]`` did not close this key; a
        # foldable identifier key never contains one.
        if "[" in inner:
            continue
        key = _eval_expr(inner, constants)
        if key is None or not re.fullmatch(r"[A-Za-z_$][\w$]*", key):
            continue
        cursor = _skip_whitespace(code, close + 1)
        if code.startswith("?.", cursor):
            cursor = _skip_whitespace(code, cursor + 2)
        if cursor < len(code) and code[cursor] == "(":
            call_sites.append((cursor, match.group(1), key, None))
    for match in _BARE_CALL.finditer(code):
        if _inside(ranges, match.start()):
            continue
        call_sites.append((match.end() - 1, match.group(1), None, None))
    call_sites.extend(_direct_module_sites(code, ranges, constants))
    for match in _INDIRECT_MEMBER_CALL.finditer(code):
        if _inside(ranges, match.start()):
            continue
        call_sites.append((match.end() - 1, match.group(1), match.group(2), None))
    call_sites.extend(builtin_sites)
    call_sites.sort(key=lambda item: item[0])

    for open_index, callee, member, forced_module in call_sites:
        module = forced_module or ""
        method = member or callee
        if forced_module is None and member is not None:
            module = _normalize_module(namespaces.get(callee, ""))
        elif forced_module is None and callee in members:
            module, method = members[callee]
            module = _normalize_module(module)

        # The argument and its folded form are only needed once a call site has
        # been recognised as a sink; extracting them for every call in a large
        # file is both wasted work and, for the bracket/arg scanner, potentially
        # O(file) per unmatched call.
        if category == "child_process":
            if module in CHILD_PROCESS_MODULES and method in CHILD_PROCESS_METHODS:
                argument = _first_argument(code, open_index)
                commands = _child_process_commands(
                    code, open_index, constants, legacy
                )
                if commands:
                    emit(
                        module,
                        method,
                        {
                            "command": commands[0],
                            "commands": commands,
                            "argument": argument.strip(),
                        },
                    )
                continue
            # ``process.binding('spawn_sync')`` style hand-rolled spawning.
            if callee == "process" and method == "binding":
                folded = _eval_expr(_first_argument(code, open_index), constants)
                if folded is not None and (
                    "spawn" in folded.casefold() or "exec" in folded.casefold()
                ):
                    emit("process", "binding", {"binding": folded})
            continue

        if category == "ffi":
            if module in FFI_MODULES or method in {"dlopen", "Library"}:
                argument = _first_argument(code, open_index)
                emit(module or "ffi", method, {"argument": argument.strip()})
            continue

        if category == "rpc_http":
            if module in declared:
                argument = _first_argument(code, open_index)
                emit(module, method, {"argument": argument.strip()})
                continue
            if module in {"http", "https", "node:http", "node:https"} or callee in {"fetch"}:
                argument = _first_argument(code, open_index)
                folded = _eval_expr(argument, constants)
                target = folded if folded is not None else argument.strip()
                if _loopback_url(target):
                    emit(module or "http", method, {"url": target})
            continue

        if category == "dynamic_load":
            require_callee = callee in require_aliases and forced_module is None
            if callee in {"require", "import"} or require_callee:
                # ``require.resolve('<pkg>/package.json')`` is a metadata
                # lookup used to locate a local tool bin, not a runtime load.
                if method == "resolve":
                    continue
                argument = _first_argument(code, open_index)
                folded = _eval_expr(argument, constants)
                target = folded if folded is not None else argument
                if not _mentions_legacy_runtime(
                    target, constants, legacy
                ) and not _is_obfuscated_module_specifier(argument):
                    continue
                emit(
                    "dynamic-require",
                    method if require_callee else callee,
                    {"argument": argument.strip(), "target": target.strip()},
                )
                continue
            if callee == "process" and method == "dlopen":
                argument = _first_argument(code, open_index)
                emit("process", "dlopen", {"argument": argument.strip()})
                continue
            if callee == "vm" or module in VM_MODULES:
                if method.startswith("runIn") or method in {"compileFunction", "Script"}:
                    if not _dynamic_code_is_fallback(
                        code, open_index, constants, legacy
                    ):
                        continue
                    argument = _first_argument(code, open_index)
                    emit(module or "vm", method, {"argument": argument.strip()})
                continue
            if callee == "eval" and method == "eval":
                if not _dynamic_code_is_fallback(
                    code, open_index, constants, legacy
                ):
                    continue
                argument = _first_argument(code, open_index)
                emit("eval", "eval", {"argument": argument.strip()})
                continue
            continue

    # ``new Function(...)`` is a dynamic-code sink with no call-chain callee.
    # It is only a fallback when the code it constructs references the migrated
    # runtime; bundled glue that merely uses the constructor is not deceptive.
    if category == "dynamic_load":
        for match in re.finditer(r"\bnew\s+Function\s*\(", code):
            if _inside(ranges, match.start()):
                continue
            if not _dynamic_code_is_fallback(
                code, match.end() - 1, constants, legacy
            ):
                continue
            emit("Function", "constructor", {"construct": True})
            break

    return hits


__all__ = ["scan_runtime_sinks", "strip_comments"]
