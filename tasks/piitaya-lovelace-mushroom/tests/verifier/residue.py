"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence


# Directory names that are dependency/install scaffolding wherever they appear.
_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "coverage",
}
# Build-output names are only scaffolding when they sit at the scanned repository
# root.  A directory such as ``src/target`` is ordinary candidate source and a
# deceptive fallback parked there must stay visible; pruning the name at any
# depth hid it from the residue scan.  Root-level ``dist``/``build``/``target``
# remain pruned so a generated bundle tree is never re-scanned.
_EXCLUDED_ROOT_PARTS = {"target", "dist", "build"}
_RULE_KINDS = {"literal", "dependency", "import", "file", "invoked_import"}
# Candidate source suffixes whose declarative ``literal``/``import`` rules are
# matched against the comment-stripped, constant-folded source.  Structured
# manifests (``package.json`` / lock files) keep exact-text matching.
_JS_SOURCE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _eligible(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part in _EXCLUDED_PARTS for part in relative.parts)
        and (not relative.parts or relative.parts[0] not in _EXCLUDED_ROOT_PARTS)
    )


# --- Comment stripping --------------------------------------------------------
#
# A deceptive bridge is executable code, so the declarative rules must not match
# prose.  ``// Previously imported from "superstruct"`` or a comment that merely
# names ``child_process`` is documentation, not delegation.  We strip comments
# while preserving string/template/regex contents (import specifiers live in
# strings and must survive), using a dependency-free lexical scan.

_REGEX_ALLOWED_KEYWORDS = {
    "await", "case", "delete", "do", "else", "in", "instanceof", "new", "of",
    "return", "throw", "typeof", "void", "yield",
}


def _regex_allowed(previous: str, last_word: str) -> bool:
    if not previous:
        return True
    if previous in "([{:;,=!?&|+-*%<>~^":
        return True
    if previous in ")]}":
        return False
    return last_word in _REGEX_ALLOWED_KEYWORDS


def _strip_js_comments(text: str) -> str:
    """Return ``text`` with block/line comments replaced by whitespace.

    String, template and regular-expression literals are preserved verbatim so
    import specifiers survive; only comments are blanked.  Comments still act as
    token separators so ``return/*x*/value`` cannot fuse into one identifier.
    """

    code: list[str] = []
    index = 0
    mode = "code"
    previous = ""
    last_word = ""
    template_expressions: list[int] = []

    def emit(character: str, *, comment: bool = False) -> None:
        if character.isspace():
            if not comment and code and not code[-1].isspace():
                code.append(" ")
            return
        if comment:
            if code and not code[-1].isspace():
                code.append(" ")
            return
        code.append(character)

    def note(character: str) -> None:
        nonlocal previous, last_word
        if character.isalnum() or character in "_$":
            last_word = (last_word + character) if last_word else character
            previous = character
        else:
            previous = character
            last_word = ""

    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""

        if mode == "code":
            if character == "/" and following == "/":
                mode = "line-comment"
                emit(character, comment=True)
                emit(following, comment=True)
                index += 2
                continue
            if character == "/" and following == "*":
                mode = "block-comment"
                emit(character, comment=True)
                emit(following, comment=True)
                index += 2
                continue
            if character in "'\"":
                mode = "single-quote" if character == "'" else "double-quote"
                code.append(character)
                index += 1
                continue
            if character == "`":
                mode = "template"
                code.append(character)
                index += 1
                continue
            if character == "/" and _regex_allowed(previous, last_word):
                mode = "regex"
                code.append(character)
                index += 1
                continue
            if template_expressions and character == "{":
                template_expressions[-1] += 1
            elif template_expressions and character == "}":
                if template_expressions[-1] == 1:
                    template_expressions.pop()
                    code.append(character)
                    index += 1
                    mode = "template"
                    previous = ""
                    last_word = ""
                    continue
                template_expressions[-1] -= 1
            emit(character)
            note(character)
            index += 1
            continue

        if mode == "line-comment":
            if character == "\n":
                mode = "code"
                index += 1
                continue
            index += 1
            continue

        if mode == "block-comment":
            if character == "*" and following == "/":
                emit(character, comment=True)
                emit(following, comment=True)
                index += 2
                mode = "code"
                continue
            index += 1
            continue

        if mode in {"single-quote", "double-quote"}:
            code.append(character)
            if character == "\\" and following:
                code.append(following)
                index += 2
                continue
            if (mode == "single-quote" and character == "'") or (
                mode == "double-quote" and character == '"'
            ):
                mode = "code"
                previous = character
                last_word = ""
            index += 1
            continue

        if mode == "template":
            code.append(character)
            if character == "\\" and following:
                code.append(following)
                index += 2
                continue
            if character == "`":
                mode = "code"
                previous = character
                last_word = ""
                index += 1
                continue
            if character == "$" and following == "{":
                code.append(following)
                index += 2
                template_expressions.append(1)
                mode = "code"
                previous = "{"
                last_word = ""
                continue
            index += 1
            continue

        # Regular-expression literal.
        code.append(character)
        if character == "\\" and following:
            code.append(following)
            index += 2
            continue
        if character == "/":
            mode = "code"
            flags = index + 1
            while flags < len(text) and text[flags].isalpha():
                code.append(text[flags])
                flags += 1
            index = flags
            previous = "/"
            last_word = ""
            continue
        index += 1

    return "".join(code)


# --- Lightweight constant folding --------------------------------------------
#
# A bridge does not have to spell its sink as one contiguous literal.
# ``import("super" + "struct")``, ``["super", "struct"].join("")`` and a
# forwarded ``const __pkg = "superstruct"`` all build the same runtime value
# while defeating a plain substring search.  We fold exactly the constant string
# expressions JavaScript evaluates eagerly (literal concatenation, templates with
# literal substitutions, array ``join('')``, ``String.fromCharCode`` and base64
# decoding), decode string escapes, normalize computed member access, and
# propagate simple ``const`` aliases, then match the dangerous targets on the
# reconstructed form.  This reasons about evaluated constants instead of raw
# text and stays dependency-free.

_JS_STRING_LITERAL = r"""(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")"""
_JS_LITERAL_TOKEN = re.compile(_JS_STRING_LITERAL, re.DOTALL)
_JS_STRING_PARTS = re.compile(
    r"""'(?:\\.|[^'\\])*'""" + r'''|"(?:\\.|[^"\\])*"''', re.DOTALL
)
_ONE_LITERAL = re.compile(
    r"""^(?:'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)")$""", re.DOTALL
)
_FOLD_CONCAT = re.compile(
    r"""(?P<head>"""
    + _JS_STRING_LITERAL
    + r""")(?P<tail>(?:\s*\+\s*"""
    + _JS_STRING_LITERAL
    + r""")+)""",
    re.DOTALL,
)
_FOLD_JOIN = re.compile(
    r"""\[\s*(?P<items>"""
    + _JS_STRING_LITERAL
    + r"""(?:\s*,\s*"""
    + _JS_STRING_LITERAL
    + r""")*)\s*\]\s*\.\s*join\s*\(\s*(?P<quote>['"])\s*(?P=quote)\s*\)""",
    re.DOTALL,
)
_FOLD_TEMPLATE = re.compile(r"""`(?P<body>(?:[^`\\]|\\.)*)`""", re.DOTALL)
_TEMPLATE_SUBSTITUTION = re.compile(r"\$\{([^{}]*)\}")
_FOLD_CHAR_CODES = re.compile(
    r"""String\s*\.\s*from(?:CharCode|CodePoint)\s*\(\s*(?P<args>[0-9a-fA-FxX,\s]+?)\s*\)"""
)
# ``[115, 117, ...].map((c) => String.fromCharCode(c)).join("")`` reconstructs a
# module name from numeric character codes, so the source package never appears
# as a contiguous token.  ``codes`` is frequently hoisted into a ``const`` array
# first, hence the two incoming shapes: an inline numeric array and a named one
# resolved through ``_FOLD_NUMERIC_ARRAY``.  The callback may also be a bare
# reference (``.map(String.fromCharCode)``), and the codes may be spread
# (``String.fromCharCode(...codes)``) or forwarded through ``apply``; all are the
# same evaluated string.
_FOLD_CHARCODE_ARRAY = re.compile(
    r"(?:\[(?P<items>[0-9a-fA-FxX,\s]*)\]"
    r"|(?P<arr>[A-Za-z_$][A-Za-z0-9_$]*))"
    r"\s*\.\s*map\s*\(\s*(?:"
    r"\(?\s*(?P<var>[A-Za-z_$][A-Za-z0-9_$]*)\s*(?::\s*[^=)]+)?\)?\s*=>\s*"
    r"String\s*\.\s*from(?:CharCode|CodePoint)\s*\(\s*(?P=var)\s*\)"
    r"|String\s*\.\s*from(?:CharCode|CodePoint)"
    r")\s*\)\s*\.\s*join\s*\(\s*['\"]\s*['\"]\s*\)"
)
_FOLD_CHAR_CODES_SPREAD = re.compile(
    r"String\s*\.\s*from(?:CharCode|CodePoint)\s*\(\s*\.\.\.\s*"
    r"(?:\[(?P<items>[0-9a-fA-FxX,\s]*)\]|(?P<arr>[A-Za-z_$][A-Za-z0-9_$]*))\s*\)"
)
_FOLD_CHAR_CODES_APPLY = re.compile(
    r"String\s*\.\s*from(?:CharCode|CodePoint)\s*\.\s*apply\s*\(\s*"
    r"(?:null|undefined|this)\s*,\s*"
    r"(?:\[(?P<items>[0-9a-fA-FxX,\s]*)\]|(?P<arr>[A-Za-z_$][A-Za-z0-9_$]*))\s*\)"
)
_FOLD_NUMERIC_ARRAY = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"\s*(?::\s*[^=;]+)?=\s*\[(?P<items>[0-9a-fA-FxX,\s]*)\]\s*"
    r"(?:as\s+const\s*)?(?![\w$])"
)
_FOLD_BASE64 = re.compile(
    r"""(?:Buffer\s*\.\s*from\s*\(\s*(?P<buffer>"""
    + _JS_STRING_LITERAL
    + r""")\s*,\s*(?P<encoding>['"])base64(?P=encoding)\s*\)"""
    r"""(?!\s*\.\s*toString\s*\(\s*['"](?!(?:utf-?8|utf8|ascii|latin1|binary)['"]))"""
    r"""|atob\s*\(\s*(?P<atob>"""
    + _JS_STRING_LITERAL
    + r""")\s*\))""",
    re.IGNORECASE | re.DOTALL,
)
_FOLD_COMPUTED_MEMBER = re.compile(
    r"""\[\s*(?P<quote>['"])(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)(?P=quote)\s*\]"""
)
_ESCAPE_SEQUENCES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "`": "`",
}

_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
_STRING_DECL = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>"
    + _IDENT
    + r")\s*=\s*(?P<value>"
    + _JS_STRING_LITERAL
    + r")\s*(?:as\s+const\s*)?(?![\w$])",
)
_IDENT_DECL = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>"
    + _IDENT
    + r")\s*=\s*(?P<value>"
    + _IDENT
    + r")\s*(?![\w$])",
)
# ``const load = host.require`` routes the loader through a local alias just as
# ``const load = require`` does; the member prefix is irrelevant because the
# callee behaviour (``require``/``import``) is what matters.  Matching the whole
# member chain keeps the alias recognisable while leaving the prefix in place so
# the eventual call still reads as ``host.require("superstruct")``.
_CALLABLE_MEMBER = r"""(?:[A-Za-z_$][A-Za-z0-9_$]*\s*\.\s*)*"""
_CALLABLE_DECL = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>"
    + _IDENT
    + r")\s*=\s*(?P<value>"
    + _CALLABLE_MEMBER
    + r"(?:require|import)\b)\s*(?:as\s+const\s*)?(?![\w$])",
)
# ``const alias = binding`` (with an optional ``as`` type assertion) is how a
# statically imported binding is renamed before use.  Tracking these edges lets
# the invoked-import check follow the binding through local aliases instead of a
# fixed set of call spellings.
_ALIAS_ASSIGN = re.compile(
    r"(?:(?<![\w$.])(?:const|let|var)\s+)?(?<![\w$.])(?P<target>"
    + _IDENT
    + r")\s*=\s*(?P<value>"
    + _IDENT
    + r")\s*(?:as\s+[^;,\n]+)?(?![\w$])",
)
_DYNAMIC_CALL = re.compile(
    r"\b(?P<fn>import|require)\s*\(\s*(?P<arg>[^()]*?)\s*\)", re.DOTALL
)
# A fallback does not have to spell ``require("superstruct")`` as one contiguous
# call either.  Borrowing the callee through the comma operator
# (``(0, host.require)("superstruct")``), routing the arguments through
# ``Reflect.apply`` (``Reflect.apply(host.require, null, ["superstruct"])``) or
# the Node-internal ``Module._load`` (``(module as any).constructor._load(...)``)
# all evaluate the same delegation while breaking the literal token.  We
# normalize those call shapes to their canonical ``callee(args)`` form so the
# declarative rules keep matching evaluated behaviour instead of raw spelling.
_COMMA_BORROW_CALL = re.compile(
    r"\(\s*[^()]*?\s*,\s*"
    r"(?P<callee>[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)*)"
    r"\s*\)\s*(?=\()"
)
_REFLECT_APPLY_CALL = re.compile(
    r"Reflect\s*\.\s*apply\s*\(\s*"
    r"(?P<callee>[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)*)"
    r"\s*,\s*[^,()]*?\s*,\s*\[(?P<args>[^\[\]]*)\]\s*\)"
)
_LOAD_CALL = re.compile(r"\b_load\s*\(")


def _unescape_js(body: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\" or index + 1 >= len(body):
            out.append(character)
            index += 1
            continue
        marker = body[index + 1]
        if marker == "x" and index + 3 < len(body):
            try:
                out.append(chr(int(body[index + 2 : index + 4], 16)))
                index += 4
                continue
            except ValueError:
                pass
        if marker == "u":
            if index + 2 < len(body) and body[index + 2] == "{":
                close = body.find("}", index + 3)
                if close != -1:
                    try:
                        out.append(chr(int(body[index + 3 : close], 16)))
                        index = close + 1
                        continue
                    except ValueError:
                        pass
            elif index + 5 < len(body):
                try:
                    out.append(chr(int(body[index + 2 : index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
        out.append(_ESCAPE_SEQUENCES.get(marker, marker))
        index += 2
    return "".join(out)


def _decode_literal(literal: str) -> str | None:
    match = _ONE_LITERAL.match(literal.strip())
    if not match:
        return None
    body = match.group(1) if match.group(1) is not None else match.group(2)
    return _unescape_js(body)


def _quote_js(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _fold_string_parts(text: str) -> str:
    parts = _JS_STRING_PARTS.findall(text)
    decoded = [_decode_literal(part) for part in parts]
    return "".join(part for part in decoded if part)


def _fold_concatenations(code: str) -> str:
    def replace(match: re.Match) -> str:
        return _quote_js(_fold_string_parts(match.group(0)))

    return _FOLD_CONCAT.sub(replace, code)


def _fold_joins(code: str) -> str:
    def replace(match: re.Match) -> str:
        return _quote_js(_fold_string_parts(match.group("items")))

    return _FOLD_JOIN.sub(replace, code)


def _fold_templates(code: str) -> str:
    def replace(match: re.Match) -> str:
        body = match.group("body")
        if "${" not in body:
            return _quote_js(_unescape_js(body))
        pieces: list[str] = []
        position = 0
        for substitution in _TEMPLATE_SUBSTITUTION.finditer(body):
            value = _decode_literal(substitution.group(1).strip())
            if value is None:
                return match.group(0)
            pieces.append(_unescape_js(body[position : substitution.start()]))
            pieces.append(value)
            position = substitution.end()
        pieces.append(_unescape_js(body[position:]))
        return _quote_js("".join(pieces))

    return _FOLD_TEMPLATE.sub(replace, code)


def _fold_char_codes(code: str) -> str:
    def replace(match: re.Match) -> str:
        codes: list[int] = []
        for token in match.group("args").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                codes.append(
                    int(token, 16) if token.lower().startswith("0x") else int(token)
                )
            except ValueError:
                return match.group(0)
        try:
            return _quote_js("".join(chr(code) for code in codes))
        except (ValueError, OverflowError):
            return match.group(0)

    return _FOLD_CHAR_CODES.sub(replace, code)


def _fold_char_code_arrays(code: str) -> str:
    """Fold ``[115, 117, ...].map((c) => String.fromCharCode(c)).join("")``.

    The call form (``String.fromCharCode(115, 117, ...)``) is already handled by
    :func:`_fold_char_codes`, but a module name is just as easily rebuilt from a
    numeric array driven through ``map``/``join`` — with the array frequently
    hoisted into a named ``const`` first, and the callback passed either as an
    arrow (``(c) => String.fromCharCode(c)``) or as a bare reference
    (``String.fromCharCode``).  The same codes may instead be spread
    (``String.fromCharCode(...codes)``) or forwarded through ``apply``.  None of
    these spellings contains the source package token, so each is folded to the
    reconstructed string literal.
    """

    numeric: dict[str, str] = {}
    for match in _FOLD_NUMERIC_ARRAY.finditer(code):
        numeric[match.group("name")] = match.group("items")

    def decode(items: str) -> str | None:
        codes: list[int] = []
        for token in items.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                codes.append(
                    int(token, 16) if token.lower().startswith("0x") else int(token)
                )
            except ValueError:
                return None
        if not codes:
            return None
        try:
            return "".join(chr(code) for code in codes)
        except (ValueError, OverflowError):
            return None

    def replace(match: re.Match) -> str:
        items = match.group("items")
        if items is None:
            items = numeric.get(match.group("arr") or "", "")
        value = decode(items)
        if value is None:
            return match.group(0)
        return _quote_js(value)

    code = _FOLD_CHARCODE_ARRAY.sub(replace, code)
    code = _FOLD_CHAR_CODES_SPREAD.sub(replace, code)
    code = _FOLD_CHAR_CODES_APPLY.sub(replace, code)
    return code


def _fold_base64(code: str) -> str:
    def replace(match: re.Match) -> str:
        literal = match.group("buffer") or match.group("atob")
        value = _decode_literal(literal)
        if value is None:
            return match.group(0)
        try:
            text = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return match.group(0)
        if len(text) > 512 or not all(
            character.isprintable() or character in "\t\n\r" for character in text
        ):
            return match.group(0)
        return _quote_js(text)

    return _FOLD_BASE64.sub(replace, code)


def _fold_js_constants(code: str) -> str:
    for _ in range(8):
        before = code
        code = _fold_joins(code)
        code = _fold_base64(code)
        code = _fold_char_codes(code)
        code = _fold_char_code_arrays(code)
        code = _fold_templates(code)
        code = _fold_concatenations(code)
        if code == before:
            break
    return code


def _normalize_computed_members(code: str) -> str:
    return _FOLD_COMPUTED_MEMBER.sub(r".\g<name>", code)


def _decode_escapes_in_literals(code: str) -> str:
    """Rewrite each string literal to its decoded value.

    An escaped spelling such as ``'super\\u0073truct'`` equals ``superstruct`` at
    runtime but contains no contiguous token in the raw text.  Decoding every
    literal (and re-quoting it canonically) folds those escapes before the
    dangerous-name match without needing an expression.
    """

    def replace(match: re.Match) -> str:
        value = _decode_literal(match.group(0))
        return _quote_js(value) if value is not None else match.group(0)

    return _JS_LITERAL_TOKEN.sub(replace, code)


def _resolve_indirect_references(code: str) -> str:
    """Propagate ``const`` string aliases into dynamic ``import``/``require`` calls.

    ``const __pkg = "superstruct"; import(__pkg)`` and a call forwarded through a
    wrapper (``const load = require; load(__pkg)``) defeat a pure constant
    folder because the sink argument is an identifier.  We resolve simple
    aliases to a fixed point and rewrite the dynamic-call arguments/call names so
    the reconstructed specifier is matchable.
    """

    aliases: dict[str, str] = {}
    callables: dict[str, str] = {}
    for _ in range(8):
        changed = False
        for match in _STRING_DECL.finditer(code):
            name = match.group("name")
            value = _decode_literal(match.group("value"))
            if value is not None and aliases.get(name) != value:
                aliases[name] = value
                changed = True
        for match in _IDENT_DECL.finditer(code):
            name = match.group("name")
            source = match.group("value")
            if source in aliases and aliases.get(name) != aliases[source]:
                aliases[name] = aliases[source]
                changed = True
        for match in _CALLABLE_DECL.finditer(code):
            name = match.group("name")
            value = match.group("value")
            if callables.get(name) != value:
                callables[name] = value
                changed = True
        if not changed:
            break
    if not aliases and not callables:
        return code
    if callables:
        names = "|".join(re.escape(name) for name in sorted(callables, key=len, reverse=True))
        wrapper = re.compile(r"\b(" + names + r")\s*\(")
        code = wrapper.sub(lambda match: callables[match.group(1)] + "(", code)

    def replace(match: re.Match) -> str:
        function = match.group("fn")
        argument = match.group("arg").strip()
        if re.fullmatch(_IDENT, argument) and argument in aliases:
            argument = _quote_js(aliases[argument])
        return f"{function}({argument})"

    return _DYNAMIC_CALL.sub(replace, code)


def _fold_indirect_calls(code: str) -> str:
    """Rewrite indirection around a call into the canonical call form.

    ``(0, host.require)(pkg)`` is the comma-operator callee borrow,
    ``Reflect.apply(host.require, thisArg, [pkg])`` forwards the call through
    ``Reflect``, and ``Module._load(pkg)`` is the internal loader behind
    ``require``.  Each is semantically ``host.require(pkg)``; folding them here
    lets the contiguous ``require("superstruct")`` rule see the evaluated call
    instead of the attacker-chosen spelling.
    """

    def replace_reflect(match: re.Match) -> str:
        return f"{match.group('callee')}({match.group('args').strip()})"

    for _ in range(8):
        before = code
        code = _REFLECT_APPLY_CALL.sub(replace_reflect, code)
        code = _COMMA_BORROW_CALL.sub(lambda match: match.group("callee"), code)
        code = _LOAD_CALL.sub("require(", code)
        if code == before:
            break
    return code


def _normalize_js_code(code: str) -> str:
    folded = _fold_js_constants(_decode_escapes_in_literals(code))
    calls = _fold_indirect_calls(folded)
    normalized = _normalize_computed_members(calls)
    return _resolve_indirect_references(normalized)


# --- Rule evaluation ----------------------------------------------------------

def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _source_for_matching(path: Path) -> str:
    text = _read_text(path)
    if text is None:
        return ""
    if path.suffix.casefold() not in _JS_SOURCE_SUFFIXES:
        return text
    try:
        return _normalize_js_code(_strip_js_comments(text))
    except Exception:  # pragma: no cover - defensive
        return _strip_js_comments(text)


def _paths(root: Path, patterns: Sequence[str]) -> Iterable[Path]:
    if not patterns:
        return ()
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
    candidates = collected
    unique = {path.resolve(): path for path in candidates if _eligible(root, path)}
    return (unique[key] for key in sorted(unique, key=lambda item: item.as_posix()))


def _all_source_paths(root: Path) -> Iterable[Path]:
    """Yield every candidate JS/TS source file, pruning heavy directories.

    ``os.walk`` is used instead of ``rglob`` so excluded directories
    (``node_modules`` and friends) are pruned *before* descending, keeping the
    scan cheap even when the candidate has a full install tree.
    """

    collected: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        at_root = Path(current) == root
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _EXCLUDED_PARTS
            and not (at_root and name in _EXCLUDED_ROOT_PARTS)
            and not (Path(current) / name).is_symlink()
        ]
        for name in filenames:
            if Path(name).suffix.casefold() not in _JS_SOURCE_SUFFIXES:
                continue
            path = Path(current) / name
            if _eligible(root, path):
                collected.append(path)
    unique = {path.resolve(): path for path in collected}
    return (unique[key] for key in sorted(unique, key=lambda item: item.as_posix()))


def _target_matches(value: str, target: str) -> bool:
    actual = value.casefold().strip()
    expected = target.casefold().strip()
    return actual == expected or fnmatchcase(actual, expected)


_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_$")
# Declarative ``literal`` targets that begin with one of these tokens denote a
# call/keyword/module name rather than a candidate-defined binding, so they are
# matched on spelling alone.  Any other leading identifier (e.g. ``dynamic``)
# names a symbol that a legitimate migration may re-provide from the target
# framework, and is only treated as residue when the module still binds it to
# the source framework.
_LITERAL_ORIGIN_EXEMPT = frozenset(
    {
        "require", "import", "from", "instanceof", "typeof", "new", "delete",
        "child_process", "superstruct",
    }
)
_SUPERSTRUCT_BINDING = (
    re.compile(r"""\bfrom\s*['"]superstruct['"]""", re.IGNORECASE),
    re.compile(r"""\brequire\s*\(\s*['"]superstruct['"]\s*\)""", re.IGNORECASE),
    re.compile(r"""\bimport\s*\(\s*['"]superstruct['"]\s*\)""", re.IGNORECASE),
)


def _boundary_match(haystack: str, needle: str) -> bool:
    """Return True when ``needle`` occurs in ``haystack`` on identifier bounds.

    A plain ``in`` substring test made the ``dynamic(`` rule fire on the
    candidate's own ``zDynamic(`` helper.  Matching only where the target is not
    glued to adjacent identifier characters keeps the literal semantic while
    refusing substrings of longer names.
    """

    if not needle:
        return False
    left_is_ident = needle[0] in _IDENT_CHARS
    right_is_ident = needle[-1] in _IDENT_CHARS
    start = 0
    while True:
        position = haystack.find(needle, start)
        if position == -1:
            return False
        end = position + len(needle)
        left_ok = (
            not left_is_ident
            or position == 0
            or haystack[position - 1] not in _IDENT_CHARS
        )
        right_ok = (
            not right_is_ident
            or end >= len(haystack)
            or haystack[end] not in _IDENT_CHARS
        )
        if left_ok and right_ok:
            return True
        start = position + 1


def _leading_symbol(target: str) -> str | None:
    match = re.match(_IDENT, target)
    return match.group(0) if match else None


def _binds_superstruct(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SUPERSTRUCT_BINDING)


# A residue target written as ``loader("module")`` names the *module load*, not
# the callee spelling.  Enumerating how the loader value was obtained (comma
# borrow, ``Reflect.apply``, ``Module._load``, ``eval("require")``,
# ``new Function("return require")``, member alias, destructure, ``Proxy``,
# variable computed key, ...) is a losing game: each new spelling defeats a
# fixed rewriter.  We instead match the evaluated invariant — the source module
# name is passed as the argument of a call — so any callee provenance is caught.
_DYNAMIC_MODULE_TARGET = re.compile(
    r"""^\s*[A-Za-z_$][A-Za-z0-9_$]*\s*\(\s*['"](?P<module>[^'"]+)['"]\s*\)\s*$"""
)


def _module_load_name(target: str) -> str | None:
    match = _DYNAMIC_MODULE_TARGET.match(target)
    return match.group("module") if match else None


def _module_call_argument(text: str, module: str) -> bool:
    """True when ``module`` appears as the first argument of a call.

    ``loader("superstruct")``, ``host.require("superstruct")``,
    ``host[key]("superstruct")``, ``(new Function("return require"))("superstruct")``
    and ``eval("require")("superstruct")`` all reduce to "invoke a callable with
    the module name"; the callee token before ``(`` is deliberately not
    constrained.
    """

    if not module:
        return False
    literal = r"""['"]""" + re.escape(module) + r"""['"]"""
    pattern = re.compile(
        r"(?:[A-Za-z0-9_$]|\)|\])\s*\(\s*" + literal + r"\s*[,)]",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


# A static ESM import is the remaining way to reach the source framework at
# runtime without ever spelling ``require("superstruct")`` or
# ``import("superstruct")``.  ``no_deceptive_source_fallback`` is defined as a
# *runtime* check — the target code must not execute the source implementation —
# so what matters is not the import spelling but whether the imported binding is
# actually invoked.  ``bridge.ts`` importing ``{ assign, object, ... }`` and then
# calling ``assign(...)``/``object(...)`` delegates validation back to source; a
# module that merely re-exports the binding (``export const x = object``) leaves
# the source dormant and is ordinary residue, not a hidden fallback.  We therefore
# evaluate the invariant "the module name is statically imported and one of its
# imported bindings is invoked" instead of a fixed set of import spellings.
_STATIC_IMPORT = re.compile(
    r"""(?<![\w$."'`])\bimport\b\s+(?P<clause>[^;'"]*?)\s*from\s*['"](?P<module>[^'"]+)['"]""",
    re.DOTALL,
)
_STATIC_IMPORT_DEFAULT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_STATIC_IMPORT_NAMESPACE = re.compile(r"\*\s*as\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)")
_STATIC_IMPORT_NAMED = re.compile(
    r"""(?:['"](?P<quoted>[^'"]+)['"]|(?P<name>[A-Za-z_$][A-Za-z0-9_$]*))"""
    r"""(?:\s+as\s+(?P<alias>[A-Za-z_$][A-Za-z0-9_$]*))?"""
)


def _static_import_bindings(clause: str) -> list[str]:
    """Return the local names bound by one ESM ``import`` clause.

    Handles ``import d from``, ``import * as ns from``, ``import { a, b as c }``
    and the default+named combination.  Type-only imports (``import type ...``
    and inline ``type`` specifiers) are erased before the program runs and so are
    not runtime delegation.
    """

    clause = clause.strip()
    if re.match(r"type(?:\s|\{)", clause):
        return []
    bindings: list[str] = []
    default = _STATIC_IMPORT_DEFAULT.match(clause)
    if default:
        bindings.append(default.group(0))
        clause = clause[default.end() :].lstrip()
        if clause.startswith(","):
            clause = clause[1:].lstrip()
    if clause.startswith("*"):
        namespace = _STATIC_IMPORT_NAMESPACE.match(clause)
        if namespace:
            bindings.append(namespace.group("name"))
        return [name for name in bindings if name]
    if clause.startswith("{"):
        body = clause[1 : clause.rfind("}")] if "}" in clause else clause[1:]
        for part in body.split(","):
            part = re.sub(r"^type\s+", "", part.strip())
            if not part:
                continue
            match = _STATIC_IMPORT_NAMED.match(part)
            if match:
                name = match.group("alias") or match.group("name") or match.group("quoted")
                if name:
                    bindings.append(name)
    return [name for name in bindings if name]


def _binding_invoked(text: str, name: str) -> bool:
    """True when the imported binding names a runtime call.

    A bare reference or re-export is dormant; ``name(...)``, ``name?.()``,
    ``name`...```, and ``name.member(...)`` all execute the imported value.
    """

    escaped = re.escape(name)
    guard = r"(?<![\w$.])"
    patterns = (
        guard + escaped + r"\s*(?:\?\.)?\s*\(",
        guard + escaped + r"\s*`",
        guard + escaped + r"\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*\s*(?:\?\.)?\s*\(",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _tainted_binding_closure(text: str, seeds: Iterable[str]) -> set[str]:
    """Follow ``X = Y`` aliases outward from the imported binding names.

    An attacker can hide the delegation by renaming the imported value once
    (``const alias = imported as ...; alias(...)``).  Propagating the seed set
    through simple assignments keeps the invoked-import check keyed on evaluated
    behaviour rather than the exact local name.
    """

    tainted = {name for name in seeds if name}
    if not tainted:
        return tainted
    for _ in range(8):
        changed = False
        for match in _ALIAS_ASSIGN.finditer(text):
            target = match.group("target")
            value = match.group("value")
            if value in tainted and target not in tainted:
                tainted.add(target)
                changed = True
        if not changed:
            break
    return tainted


def _invoked_import_contains(path: Path, module: str, *, source: str | None = None) -> bool:
    text = source if source is not None else _read_text(path)
    if text is None:
        return False
    bindings: list[str] = []
    for match in _STATIC_IMPORT.finditer(text):
        if match.group("module") != module:
            continue
        bindings.extend(_static_import_bindings(match.group("clause")))
    tainted = _tainted_binding_closure(text, set(bindings))
    return any(_binding_invoked(text, name) for name in tainted)


def _literal_contains(path: Path, target: str, *, source: str | None = None) -> bool:
    text = source if source is not None else _read_text(path)
    if text is None:
        return False
    symbol = _leading_symbol(target)
    if (
        symbol is not None
        and symbol not in _LITERAL_ORIGIN_EXEMPT
        and not _binds_superstruct(text)
    ):
        # The target names a symbol (``dynamic``) that this module re-provides
        # from the target framework; it is not source-framework residue.
        return False
    if _boundary_match(text.casefold(), target.casefold().strip()):
        return True
    module = _module_load_name(target)
    if module is not None:
        return _module_call_argument(text, module)
    return False


def _dependency_contains(path: Path, target: str) -> bool:
    text = _read_text(path)
    if text is None:
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


def _import_contains(path: Path, target: str, *, source: str | None = None) -> bool:
    text = source if source is not None else _read_text(path)
    if text is None:
        return False
    candidates: list[str] = []
    patterns = (
        re.compile(r"\bfrom\s+([A-Za-z0-9_./@+-]+)\s+import\b"),
        re.compile(r"\bimport\s+([A-Za-z0-9_./@+-]+)"),
        re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\bimport\s*['\"]([^'\"]+)['\"]"),
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


def _matches(
    root: Path,
    path: Path,
    *,
    kind: str,
    target: str,
    source: str | None = None,
) -> bool:
    if kind == "literal":
        return _literal_contains(path, target, source=source)
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target, source=source)
    if kind == "invoked_import":
        return _invoked_import_contains(path, target, source=source)
    relative = path.relative_to(root).as_posix()
    return fnmatchcase(relative.casefold(), target.casefold()) or fnmatchcase(
        path.name.casefold(), target.casefold()
    )


def scan_residue_rules(
    root: Path,
    rules: object,
    *,
    scan_all: bool = False,
) -> list[dict[str, str]]:
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    cache: dict[str, str] = {}

    def source_for(path: Path) -> str:
        key = str(path)
        cached = cache.get(key)
        if cached is None:
            cached = _source_for_matching(path)
            cache[key] = cached
        return cached

    hits: list[dict[str, str]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
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
        if scan_all and kind in {"literal", "import", "invoked_import"}:
            paths: Iterable[Path] = _all_source_paths(root)
        else:
            paths = _paths(root, tuple(patterns))
        for path in paths:
            matched = _matches(
                root,
                path,
                kind=str(kind),
                target=target,
                source=source_for(path),
            )
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
    root: Path,
    rules: object,
    *,
    scan_all: bool = False,
) -> list[dict[str, object]]:
    """Classify residue rules without treating unsupported syntax as a failure."""

    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
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
        hits = scan_residue_rules(root, [row], scan_all=scan_all)
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
