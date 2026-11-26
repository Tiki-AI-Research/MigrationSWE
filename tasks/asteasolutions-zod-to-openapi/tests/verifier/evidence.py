"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .residue import classify_residue_rules, scan_deceptive_rules, scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from residue import classify_residue_rules, scan_deceptive_rules, scan_residue_rules


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*"
    r"|[^/]*_(?:test|spec)\.[^/]+|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)
# A shared test-support module (for example ``spec/lib/helpers.ts``, which owns
# the assertion helper every spec routes through) lives under a test directory
# but carries no ``.spec``/``.test`` suffix.  Treating such modules as test
# files keeps them inside the frozen test baseline, so gutting a shared
# assertion implementation is detected as tampering instead of passing
# vacuously.  This covers the whole family of test-support directories.
_TEST_DIR_NAMES = frozenset(
    {"spec", "specs", "test", "tests", "__tests__", "__test__"}
)
# Marker detection must see through every equivalent spelling of a disabled
# test, not just the literal ``describe.skip`` chain.  A disabled test reaches
# the same runtime effect through:
#
#   * dot access            ``it.skip(...)``
#   * literal bracket        ``it['skip'](...)``
#   * computed bracket       ``(describe as any)['sk' + 'ip'](...)``
#   * template bracket       ``it[`sk${'ip'}`](...)``
#   * variable key           ``const k = 'skip'; it[k](...)``
#   * alias binding          ``const s = it.skip; s(...)``
#   * member chains          ``describe.skip.each(...)`` / ``it.each(...) .skip()``
#   * x/f aliases            ``xdescribe`` / ``fit``
#   * unittest decorators    ``@pytest.mark.skip``
#   * flag switches          ``skip = true``
#
# Rather than enumerate spellings, the detector constant-folds every bracket
# member key whose expression evaluates to a compile-time string, turning the
# whole computed-key family into ordinary ``.skip`` access; a single semantic
# marker scan then recognises the result.  Comment/string masking keeps prose
# out of the count while the fold runs first so bracket keys (which are string
# literals) stay observable.
_PYTEST_SKIP = re.compile(r"pytest\.mark\.skip", re.IGNORECASE)
_PYTEST_XFAIL = re.compile(r"pytest\.mark\.xfail", re.IGNORECASE)
_DOT_MARKER = re.compile(r"\.\s*(?:skip|only|todo)\b", re.IGNORECASE)
_TEST_GLOBAL_NAMES = ("describe", "it", "test", "context", "specify")
# Mocha disables a suite/spec with x-prefixed aliases (``xdescribe``,
# ``xcontext``, ``xit``, ``xspecify``).  They are semantically identical to
# ``.skip`` and a migration translates them one-for-one, so they must be counted
# symmetrically; otherwise the mechanical ``xdescribe -> describe.skip`` rewrite
# looks like a newly introduced skip.
_DISABLED_ALIAS = re.compile(r"(?<![\w$])x(?:describe|context|it|specify)\b", re.IGNORECASE)
# Jasmine focus aliases (``fdescribe``/``fit``) silently disable every sibling
# suite/spec, so they are the same strength regression as ``.only``.
_FOCUS_ALIAS = re.compile(
    r"(?<![\w$])f(?:describe|context|it|specify)\b\s*[\(\.\[]", re.IGNORECASE
)
_UNITTEST_SKIP = re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE)
_SKIP_FLAG_TRUE = re.compile(
    r"(?<![\w$])(?:skip|xfail|disabled)\s*=\s*true\b", re.IGNORECASE
)
_JS_STRING_LITERAL = re.compile(r"""^(['"])((?:\\.|(?!\1).)*)\1$""", re.DOTALL)
_JS_IDENTIFIER = re.compile(r"^[A-Za-z_$][\w$]*$")
# Exact-equality matchers that carry a concrete expected value.  They are
# normalized to a single canonical ``expect(<subject>).toBe(<argument>)`` shape
# so an equivalent ``toEqual``/``toStrictEqual`` rewrite is not punished, while
# a concrete expectation replaced by a range, truthiness or non-null check loses
# its fingerprint and is therefore detectable as a weakened assertion.
_EXACT_ASSERTION = re.compile(
    r"(?<![.\w$])expect\s*\((?P<subject>.*?)\)\s*\.\s*(?P<chain>(?:not\s*\.\s*)?)"
    r"(?P<matcher>[A-Za-z_$][\w$]*)\s*\((?P<argument>.*?)\)",
    re.DOTALL,
)
_STRONG_MATCHERS = frozenset({"toBe", "toEqual", "toStrictEqual"})


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_test(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if any(part.casefold() in _TEST_DIR_NAMES for part in parts[:-1]):
        return True
    return bool(_TEST_NAME.search(normalized))


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


def _mask_source(text: str, *, mask_strings: bool) -> str:
    """Blank comment (and optionally string) content while preserving offsets.

    Marker counting must not treat prose in comments or string literals as a
    disabled test.  Assertion fingerprinting, by contrast, must keep string
    literals (the expected values) and mask only comments.  Replacing the
    skipped characters with spaces keeps line numbers and column offsets stable
    for diagnostics.
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
                if mask_strings and text[index] != "\n":
                    characters[index] = " "
                index += 1
            continue
        if character == quote:
            state = "normal"
            index += 1
            continue
        if mask_strings and character != "\n":
            characters[index] = " "
        index += 1
    return "".join(characters)


def _strip_comments(text: str) -> str:
    return _mask_source(text, mask_strings=True)


def _fingerprint_is_span(value: str) -> bool:
    return "test(" in value or len(value) > 180


def _strong_assertion_fingerprints_code(code: str) -> list[str]:
    fingerprints: list[str] = []
    for match in _EXACT_ASSERTION.finditer(code):
        matcher = match.group("matcher")
        chain = re.sub(r"\s+", "", match.group("chain") or "")
        if matcher in _STRONG_MATCHERS and not chain:
            subject = re.sub(r"\s+", "", match.group("subject"))
            argument = match.group("argument").strip()
            fingerprints.append(f"expect({subject}).toBe({argument})")
    return sorted(fingerprints)


def _missing_strong_assertions(baseline: Sequence[object], current: Sequence[str]) -> list[str]:
    expected = Counter(str(item) for item in baseline)
    actual = Counter(current)
    missing: list[str] = []
    for value, count in sorted(expected.items()):
        if _fingerprint_is_span(value):
            continue
        if actual.get(value, 0) < count:
            missing.append(value)
    return missing


_TSD_ASSERTIONS = (
    "expectType",
    "expectAssignable",
    "expectNotAssignable",
    "expectError",
    "expectDeprecated",
)


def _balanced_group(
    code: str, open_index: int, open_char: str = "(", close_char: str = ")"
) -> tuple[str, int]:
    depth = 0
    index = open_index
    length = len(code)
    quote = ""
    while index < length:
        character = code[index]
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
        elif character == open_char:
            depth += 1
        elif character == close_char:
            depth -= 1
            if depth == 0:
                return code[open_index + 1:index], index + 1
        index += 1
    return code[open_index + 1:], length


def _split_top_level_arguments(arguments: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    index = 0
    length = len(arguments)
    quote = ""
    start = 0
    while index < length:
        character = arguments[index]
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
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(arguments[start:index])
            start = index + 1
        index += 1
    tail = arguments[start:]
    if tail.strip() or parts:
        parts.append(tail)
    return parts


def _skip_whitespace(code: str, index: int) -> int:
    length = len(code)
    while index < length and code[index] in " \t\r\n":
        index += 1
    return index


def _split_top_level_operator(expression: str, operator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    index = 0
    length = len(expression)
    quote = ""
    start = 0
    while index < length:
        character = expression[index]
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
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif depth == 0 and expression.startswith(operator, index):
            parts.append(expression[start:index])
            index += len(operator)
            start = index
            continue
        index += 1
    parts.append(expression[start:])
    return parts


# Every spelling a candidate can use to hide a string constant behind an escape:
# ``\uXXXX``, ``\u{XXXXX}`` (code point), ``\xHH`` and the legacy octal forms.
# A mis-decoded key would fold to a value that differs from the runtime value,
# so the whole family must decode exactly, not just the single-character escapes.
_JS_ESCAPE = re.compile(
    r"\\(?:u\{([0-9A-Fa-f]{1,6})\}|u([0-9A-Fa-f]{4})|x([0-9A-Fa-f]{2})|([0-7]{1,3})|(.))",
    re.DOTALL,
)
_JS_SIMPLE_ESCAPES = {
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


def _combine_surrogate_pairs(value: str) -> str:
    result: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        code = ord(value[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < length:
            low = ord(value[index + 1])
            if 0xDC00 <= low <= 0xDFFF:
                result.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                index += 2
                continue
        result.append(value[index])
        index += 1
    return "".join(result)


def _unescape_js_string(body: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        if match.group(1) is not None:
            return chr(int(match.group(1), 16))
        if match.group(2) is not None:
            return chr(int(match.group(2), 16))
        if match.group(3) is not None:
            return chr(int(match.group(3), 16))
        if match.group(4) is not None:
            return chr(int(match.group(4), 8))
        return _JS_SIMPLE_ESCAPES.get(match.group(5), match.group(5))

    return _combine_surrogate_pairs(_JS_ESCAPE.sub(replace, body))


def _eval_template_string(body: str, known: Mapping[str, str]) -> str | None:
    result: list[str] = []
    literal: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        character = body[index]
        if character == "\\" and index + 1 < length:
            literal.append(body[index:index + 2])
            index += 2
            continue
        if character == "$" and index + 1 < length and body[index + 1] == "{":
            if literal:
                result.append(_unescape_js_string("".join(literal)))
                literal = []
            inner, end = _balanced_group(body, index + 1, "{", "}")
            value = _eval_const_string(inner, known)
            if value is None:
                return None
            result.append(value)
            index = end
            continue
        literal.append(character)
        index += 1
    if literal:
        result.append(_unescape_js_string("".join(literal)))
    return "".join(result)


def _eval_const_string(expression: str, known: Mapping[str, str]) -> str | None:
    expression = expression.strip()
    if not expression:
        return None
    while expression.startswith("(") and expression.endswith(")"):
        inner, end = _balanced_group(expression, 0)
        if end != len(expression):
            break
        expression = inner.strip()
    literal = _JS_STRING_LITERAL.match(expression)
    if literal:
        return _unescape_js_string(literal.group(2))
    if expression.startswith("`") and expression.endswith("`"):
        return _eval_template_string(expression[1:-1], known)
    parts = _split_top_level_operator(expression, "+")
    if len(parts) > 1:
        values = [_eval_const_string(part, known) for part in parts]
        if any(value is None for value in values):
            return None
        return "".join(value for value in values if value is not None)
    array_join = re.match(r"^\[(.*)\]\s*\.\s*join\s*\((.*)\)$", expression, re.DOTALL)
    if array_join:
        elements = _split_top_level_arguments(array_join.group(1))
        values = [_eval_const_string(element, known) for element in elements]
        separator = _eval_const_string(array_join.group(2), known)
        if separator is not None and all(value is not None for value in values):
            return separator.join(value for value in values if value is not None)
        return None
    if _JS_IDENTIFIER.match(expression):
        return known.get(expression)
    return None


_DECLARATION = re.compile(r"(?<![\w$])(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)")


def _build_const_string_map(code: str) -> dict[str, str]:
    known: dict[str, str] = {}
    for _ in range(3):
        changed = False
        for match in _DECLARATION.finditer(code):
            name, expression = match.group(1), match.group(2)
            if name in known:
                continue
            value = _eval_const_string(expression, known)
            if value is not None:
                known[name] = value
                changed = True
        if not changed:
            break
    return known


_TS_CAST = re.compile(r"\bas\s+(?:const\s+)?[A-Za-z_$][\w$]*(?:\s*<[^;{}()]*>)?")


def _strip_ts_casts(code: str) -> str:
    previous = None
    while previous != code:
        previous = code
        code = _TS_CAST.sub(" ", code)
    return code


def _build_global_alias_map(code: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for _ in range(3):
        changed = False
        for match in _DECLARATION.finditer(code):
            name, expression = match.group(1), match.group(2)
            if name in aliases:
                continue
            candidate = re.sub(
                r"\bas\s+(?:const\s+)?[A-Za-z_$][\w$]*(?:\s*<[^;{}()]*?>)?",
                " ",
                expression,
            ).strip()
            candidate = candidate.strip("() \t")
            if candidate in _TEST_GLOBAL_NAMES:
                aliases[name] = candidate
                changed = True
            elif candidate in aliases:
                aliases[name] = aliases[candidate]
                changed = True
        if not changed:
            break
    return aliases


def _fold_bracket_properties(code: str, known: Mapping[str, str]) -> str:
    result: list[str] = []
    index = 0
    length = len(code)
    while index < length:
        character = code[index]
        if character == "[":
            probe = index - 1
            while probe >= 0 and code[probe] in " \t":
                probe -= 1
            previous = code[probe] if probe >= 0 else ""
            if previous and (previous.isalnum() or previous in "_$])."):
                inner, end = _balanced_group(code, index, "[", "]")
                value = _eval_const_string(inner, known)
                if value is not None and _JS_IDENTIFIER.match(value):
                    result.append("." + value)
                    index = end
                    continue
        result.append(character)
        index += 1
    return "".join(result)


def _dynamic_key_marker_count(
    code: str, known: Mapping[str, str], aliases: Mapping[str, str]
) -> int:
    count = 0
    for base in {*_TEST_GLOBAL_NAMES, *aliases.keys()}:
        pattern = re.compile(r"(?<![\w$])" + re.escape(base) + r"\s*\)*\s*\[")
        for match in pattern.finditer(code):
            open_index = code.index("[", match.start())
            inner, _ = _balanced_group(code, open_index, "[", "]")
            if _eval_const_string(inner, known) is None:
                count += 1
    return count


_TRUE_LITERALS = frozenset({"true"})
_FALSE_LITERALS = frozenset({"false", "0", "null", "undefined", "NaN"})


def _const_operand(
    token: str, scalars: Mapping[str, object]
) -> tuple[bool, object]:
    token = token.strip()
    if not token:
        return False, None
    if token in _TRUE_LITERALS:
        return True, True
    if token == "false":
        return True, False
    if token in ("null", "undefined"):
        return True, None
    if token == "NaN":
        return True, float("nan")
    literal = _JS_STRING_LITERAL.match(token)
    if literal:
        return True, _unescape_js_string(literal.group(2))
    if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
        return True, (float(token) if "." in token else int(token))
    if _JS_IDENTIFIER.match(token) and token in scalars:
        return True, scalars[token]
    return False, None


def _truthy(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value == value and value != 0
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value != ""
    return True


def _eval_const_scalar(
    expression: str, scalars: Mapping[str, object]
) -> tuple[bool, object]:
    expression = expression.strip()
    if not expression:
        return False, None
    while expression.startswith("(") and expression.endswith(")"):
        inner, end = _balanced_group(expression, 0)
        if end != len(expression):
            break
        expression = inner.strip()
    if expression.startswith("!") and not expression.startswith("!="):
        known, value = _eval_const_scalar(expression[1:], scalars)
        return (True, not _truthy(value)) if known else (False, None)
    for operator in ("===", "!==", "==", "!=", ">=", "<=", ">", "<"):
        parts = _split_top_level_operator(expression, operator)
        if len(parts) != 2:
            continue
        left_known, left = _eval_const_scalar(parts[0], scalars)
        right_known, right = _eval_const_scalar(parts[1], scalars)
        if not (left_known and right_known):
            return False, None
        if operator in ("===", "!=="):
            equal = type(left) is type(right) and left == right
        else:
            try:
                equal = bool(left == right)
            except (TypeError, ValueError):
                equal = False
        if operator in ("===", "=="):
            return True, equal
        if operator in ("!==", "!="):
            return True, not equal
        if not isinstance(left, (int, float, str)) or not isinstance(right, (int, float, str)):
            return False, None
        if operator == ">":
            return True, left > right
        if operator == "<":
            return True, left < right
        if operator == ">=":
            return True, left >= right
        if operator == "<=":
            return True, left <= right
    return _const_operand(expression, scalars)


def _build_const_scalar_map(code: str) -> dict[str, object]:
    scalars: dict[str, object] = {}
    for _ in range(3):
        changed = False
        for match in _DECLARATION.finditer(code):
            name, expression = match.group(1), match.group(2)
            if name in scalars:
                continue
            known, value = _eval_const_scalar(expression, scalars)
            if known:
                scalars[name] = value
                changed = True
        if not changed:
            break
    return scalars


def _statement_end(code: str, index: int) -> int:
    length = len(code)
    depth = 0
    quote = ""
    while index < length:
        character = code[index]
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
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            if depth == 0:
                return index
            depth -= 1
        elif character == ";" and depth == 0:
            return index + 1
        index += 1
    return length


def _expression_end(code: str, index: int) -> int:
    length = len(code)
    depth = 0
    quote = ""
    while index < length:
        character = code[index]
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
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            if depth == 0:
                return index
            depth -= 1
        elif character in ",;" and depth == 0:
            return index
        elif character == "\n" and depth == 0:
            return index
        index += 1
    return length


def _statement_span(code: str, index: int) -> tuple[int, int]:
    start = _skip_whitespace(code, index)
    if start >= len(code):
        return start, start
    if code[start] == "{":
        _, end = _balanced_group(code, start, "{", "}")
        return start, end
    return start, _statement_end(code, start)


def _find_top_level(code: str, index: int, delimiter: str) -> int:
    depth = 0
    quote = ""
    length = len(code)
    while index < length:
        character = code[index]
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
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif depth == 0 and character == delimiter:
            return index
        index += 1
    return -1


def _blank_span(characters: list[str], start: int, end: int) -> None:
    for position in range(max(start, 0), min(end, len(characters))):
        if characters[position] != "\n":
            characters[position] = " "


def _blank_dead_regions(code: str, scalars: Mapping[str, object]) -> str:
    characters = list(code)
    for match in re.finditer(r"(?<![\w$])(?:if|while)\s*\(", code):
        open_index = code.index("(", match.start())
        condition, after = _balanced_group(code, open_index)
        known, value = _eval_const_scalar(condition, scalars)
        if not known:
            continue
        if not _truthy(value):
            start, end = _statement_span(code, after)
            _blank_span(characters, start, end)
            continue
        cursor = _skip_whitespace(code, after)
        if code[cursor:cursor + 4] == "else":
            start, end = _statement_span(code, cursor + 4)
            _blank_span(characters, start, end)
    for match in re.finditer(r"(?<![\w$])for\s*\(", code):
        open_index = code.index("(", match.start())
        header, after = _balanced_group(code, open_index)
        parts = _split_top_level_operator(header, ";")
        if len(parts) < 2:
            continue
        known, value = _eval_const_scalar(parts[1], scalars)
        if known and not _truthy(value):
            start, end = _statement_span(code, after)
            _blank_span(characters, start, end)
    for match in re.finditer(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*(&&|\|\|)", code):
        known, value = _eval_const_scalar(match.group(1), scalars)
        if not known:
            continue
        operator = match.group(2)
        reachable = _truthy(value) if operator == "&&" else not _truthy(value)
        if not reachable:
            start = _skip_whitespace(code, match.end())
            _blank_span(characters, start, _expression_end(code, start))
    for match in re.finditer(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\?", code):
        known, value = _eval_const_scalar(match.group(1), scalars)
        if not known:
            continue
        consequent = _skip_whitespace(code, match.end())
        colon = _find_top_level(code, consequent, ":")
        if colon < 0:
            continue
        if _truthy(value):
            _blank_span(characters, colon + 1, _expression_end(code, colon + 1))
        else:
            _blank_span(characters, consequent, colon)
    return "".join(characters)


def _normalized_fragment(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _expect_schema_fingerprints(code: str) -> list[str]:
    fingerprints: list[str] = []
    pattern = re.compile(r"(?<![\w$.])expectSchema\s*\(")
    for match in pattern.finditer(code):
        open_index = code.index("(", match.start())
        arguments, _ = _balanced_group(code, open_index)
        parts = _split_top_level_arguments(arguments)
        # ``expectSchema(input, expectedOutput[, options])`` asserts that the
        # generated components equal ``expectedOutput``; fingerprint that
        # expected value so replacing it with ``{}`` loses its expectation.
        target = parts[1] if len(parts) >= 2 else arguments
        fingerprints.append("expectSchema:" + _normalized_fragment(target))
    return fingerprints


def _tsd_assertion_fingerprints(code: str) -> list[str]:
    fingerprints: list[str] = []
    for name in _TSD_ASSERTIONS:
        pattern = re.compile(r"(?<![\w$.])" + re.escape(name) + r"\b")
        for match in pattern.finditer(code):
            index = match.end()
            length = len(code)
            while index < length and code[index].isspace():
                index += 1
            if index < length and code[index] == "<":
                _, index = _balanced_group(code, index, "<", ">")
                while index < length and code[index].isspace():
                    index += 1
            if index < length and code[index] == "(":
                _, end = _balanced_group(code, index)
                fingerprints.append("tsd:" + _normalized_fragment(code[match.start():end]))
    return fingerprints


def _assertion_inventory_text(text: str) -> list[str]:
    code = _mask_source(text, mask_strings=False)
    live = _blank_dead_regions(code, _build_const_scalar_map(code))
    fingerprints = [f"direct:{value}" for value in _strong_assertion_fingerprints_code(live)]
    fingerprints.extend(_expect_schema_fingerprints(live))
    fingerprints.extend(_tsd_assertion_fingerprints(live))
    return sorted(fingerprints)


def _assertion_inventory(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return _assertion_inventory_text(text)


def _missing_assertions(baseline: Sequence[str], current: Sequence[str]) -> list[str]:
    expected = Counter(baseline)
    actual = Counter(current)
    missing: list[str] = []
    for value, count in sorted(expected.items()):
        if value.startswith("direct:") and _fingerprint_is_span(value):
            continue
        if actual.get(value, 0) < count:
            missing.append(value)
    return missing


def _disabled_marker_count(code: str) -> int:
    known = _build_const_string_map(code)
    aliases = _build_global_alias_map(code)
    stripped = _strip_ts_casts(code)
    folded = _fold_bracket_properties(stripped, known)
    masked = _mask_source(folded, mask_strings=True)
    count = (
        len(_DOT_MARKER.findall(masked))
        + len(_DISABLED_ALIAS.findall(masked))
        + len(_FOCUS_ALIAS.findall(masked))
        + len(_UNITTEST_SKIP.findall(masked))
        + len(_SKIP_FLAG_TRUE.findall(masked))
    )
    count += _dynamic_key_marker_count(folded, known, aliases)
    return count


def _marker_counts(root: Path) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        code = _mask_source(text, mask_strings=False)
        counts["skip"] += len(_PYTEST_SKIP.findall(code))
        counts["xfail"] += len(_PYTEST_XFAIL.findall(code))
        counts["disabled"] += _disabled_marker_count(code)
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
    baseline_root: Path | None = None,
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
    equivalent: list[str] = []
    for relative, record in baseline_files.items():
        if relative not in current or not isinstance(record, Mapping):
            continue
        if record.get("sha256") == _digest(current[relative]):
            continue
        if relative in allowed:
            baseline_path = baseline_root / relative if baseline_root is not None else None
            if baseline_path is not None and baseline_path.is_file() and not baseline_path.is_symlink():
                missing_assertions = _missing_assertions(
                    _assertion_inventory(baseline_path),
                    _assertion_inventory(current[relative]),
                )
                if not missing_assertions:
                    equivalent.append(relative)
                    continue
            changed.append(relative)
        else:
            changed.append(relative)
    changed = sorted(changed)
    equivalent = sorted(equivalent)
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
        evidence={"missing": missing, "changed": changed, "equivalent": equivalent},
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
    baseline_root: Path | None = None,
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
    deceptive_mechanisms = policy.get("deceptive_fallback_mechanisms", [])
    exemption = policy.get("deceptive_fallback_not_applicable")
    migration = config.get("migration", {})
    category = migration.get("category") if isinstance(migration, Mapping) else None
    missing_l2 = (
        category != "version_upgrade"
        and not deceptive_rules
        and not deceptive_mechanisms
        and not (
            isinstance(exemption, Mapping)
            and str(exemption.get("reason", "")).strip()
            and str(exemption.get("evidence", "")).strip()
        )
    )
    deceptive_diagnostics = classify_residue_rules(candidate_root, deceptive_rules)
    deceptive_hits = scan_deceptive_rules(
        candidate_root,
        deceptive_rules,
        baseline_root=baseline_root,
        mechanisms=deceptive_mechanisms,
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
        baseline_root=baseline_root,
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
            "FAIL" if deceptive_hits else "INCONCLUSIVE" if missing_l2 else "NA" if not (deceptive_rules or deceptive_mechanisms) else "PASS",
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
