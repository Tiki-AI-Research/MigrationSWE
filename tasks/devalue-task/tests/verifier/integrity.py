"""Protected test-integrity checks for framework conversions.

Allowed framework conversions are not blank cheques.  The protected source
test inventory records the original assertion/test counts and the scenario
literals that the accepted conversion retained.  A converted file must keep
that surface, and trivially-true or empty tests are rejected outright.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


_ASSERTION = re.compile(
    r"\bassert\.(?:not\.)?(?:deepStrictEqual|strictEqual|equal|is|ok|throws|instance|type|unreachable)\s*\("
)
_EXPECT = re.compile(r"\bexpect\s*\(")
_TEST = re.compile(r"\b(?:uvu\.test|test|it)\s*(?:\.each\s*\([^)]*\))?\s*\(")
_TRIVIAL = (
    re.compile(r"\bassert\.ok\s*\(\s*true\s*\)"),
    re.compile(r"\bassert\.equal\s*\(\s*(true|false|-?\d+)\s*,\s*\1\s*\)"),
    re.compile(
        r"\bexpect\s*\(\s*(true|false|-?\d+)\s*\)\s*\.(?:toBe|toEqual|toStrictEqual)\s*\(\s*\1\s*\)"
    ),
    re.compile(r"\bexpect\s*\(\s*true\s*\)\s*\.toBeTruthy\s*\(\s*\)"),
)
_EMPTY_TEST = re.compile(
    r"\b(?:test|it)\s*\([^,]+,\s*(?:async\s*)?\(\s*\)\s*=>\s*\{\s*\}\s*\)"
)
_LITERAL = r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|-?\d+(?:\.\d+)?|true|false|null|undefined)"
_TRIVIAL_LITERAL = re.compile(
    r"\bexpect\s*\(\s*(?P<literal>" + _LITERAL + r")\s*\)\s*\.(?:toBe|toEqual|toStrictEqual)\s*\(\s*(?P=literal)\s*\)"
)
# An assertion helper may not be rebound or shadowed: a local no-op named
# ``expect`` turns every retained assertion into a silently passing one while
# keeping the file text, test names and call count intact.
_ASSERTION_SHADOW = re.compile(
    r"(?:\b(?:const|let|var|function|class)\s*(?:expect|assert)\b"
    r"|\bimport\s*\{[^}]*\b(?:expect|assert)\s*as\b"
    r"|(?<![\w$.])(?:expect|assert)\s*=(?!=))"
)
_IF_FALSE = re.compile(r"\bif\s*\(\s*(?:false|0)\s*\)")
_TEST_MODIFIERS = {"each", "skip", "only", "concurrent", "fails", "todo", "sequential"}
_MATCH_TYPES = {"exact", "ordered", "alias", "manual", "merged"}
_ASSERTION_WORDS = {"assert", "expect"}
_DIRECT_ASSERTION_EXPORTS = {
    "deepStrictEqual": "deepStrictEqual",
    "equal": "equal",
    "instance": "instance",
    "is": "is",
    "ok": "ok",
    "strictEqual": "strictEqual",
    "throws": "throws",
    "type": "type",
    "unreachable": "unreachable",
}
# Comparing an expression with itself is unconditionally true and therefore
# carries no regression signal even though the assertion call is preserved.
_SELF_COMPARE = re.compile(
    r"\bexpect\s*\(\s*(?P<expect_expr>[^,()\n]+?)\s*\)\s*"
    r"\.(?:toBe|toEqual|toStrictEqual)\s*\(\s*(?P=expect_expr)\s*\)"
)
_SELF_EQUAL = re.compile(
    r"\bassert\.(?:strictEqual|equal|is)\s*\(\s*(?P<assert_expr>[^,()\n]+?)\s*,"
    r"\s*(?P=assert_expr)\s*\)"
)
# Wall-clock assertions are not deterministic contract assertions.  Their
# exact numeric bound depends on the grader host: the canonical 100 ms bound on
# a one-million-hole array has been observed at 112-124 ms on a loaded host,
# while the same implementation completes in a few milliseconds when idle.
# Keep the assertion site, operation, comparison direction and message shape
# protected, and treat the numeric bound as host calibration rather than as a
# contract.  Calibration is accepted across the whole plausible host-jitter
# envelope, but a removed assertion, a changed operation/comparator, or an
# effectively disabled bound still fails.  Real sparse-array regressions are
# guarded by the deterministic property tests in the reference migration and by
# the native-test retry in the verifier (see harbor_entry.py); the anti-tamper
# gate must not turn a host-load artefact into a cheating verdict.
_TIMING_ASSERTION = re.compile(
    r"(?P<prefix>(?:assert\.ok|expect)\((?P<operand>elapsed2?)<)"
    r"(?P<threshold>\d+)"
    r"(?P<middle>,`(?P<label>stringify|uneval) took \$\{(?P=operand)\}ms, "
    r"expected < )"
    r"(?P=threshold)"
    r"(?P<suffix>ms`\).*)"
)
# Absolute upper bound for a calibrated wall-clock assertion.  Values above this
# are treated as effectively disabled: no plausible scheduler stall explains a
# ten-second serialization of a one-million-hole array.
_MAX_TIMING_THRESHOLD_MS = 10_000


def _text(root: Path, relative: str) -> str | None:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _canonical(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _protected_token_atoms(token: str) -> list[str]:
    """Return the protected call and literal atoms inside a retained token.

    ``required_literal_tokens`` are canonical text windows taken from the
    accepted conversion.  Requiring the whole window to stay contiguous
    rejects a legitimate assertion that the candidate adds between two
    protected assertions.  Match the protected atoms of the window instead:
    complete assertion/helper calls and complete string or template literals.
    Deleting, changing, or commenting out an atom still fails, while inserting
    a new assertion around the protected ones does not.  Tokens without any
    such atom fall back to the exact contiguous check at the call site.

    The token is a raw canonical slice, so it can begin in the middle of a
    string literal.  That is why atoms are located with dedicated call and
    literal patterns instead of a single lexical pass from offset zero.
    """

    atoms: list[str] = []
    for match in re.finditer(
        r"(?<![\w$.])(assert_[A-Za-z0-9_$]*|assert\.[A-Za-z0-9_$]+|expect)\s*\(",
        token,
    ):
        opened = token.index("(", match.start())
        depth = 0
        end = None
        index = opened
        while index < len(token):
            character = token[index]
            if character in "'\"`":
                quote = character
                index += 1
                while index < len(token):
                    if token[index] == "\\":
                        index += 2
                        continue
                    if token[index] == quote:
                        break
                    index += 1
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
            index += 1
        if end is None:
            continue
        atom = token[match.start() : end]
        if len(atom) >= 8:
            atoms.append(atom)
    for match in re.finditer(
        r"""'[^'\\\n]*(?:\\.[^'\\\n]*)*'|"[^"\\\n]*(?:\\.[^"\\\n]*)*"|`[^`\\\n]*(?:\\.[^`\\\n]*)*`""",
        token,
    ):
        literal = match.group(0)
        # A quote that merely delimit a window fragment can span the whole
        # slice.  Only treat compact, self-contained literals as atoms.
        if (
            len(literal) >= 4
            and len(literal) <= 160
            and not any(mark in literal for mark in ";(){}")
        ):
            atoms.append(literal)
    return list(dict.fromkeys(atoms))


def _protected_token_segments(needle: str) -> list[str]:
    """Split a protected window at object-member boundaries.

    A retained window can span several members of the protected operations
    map.  Adding a member there is additive: it neither deletes nor weakens a
    protected assertion, so it must not read as tampering.  Every segment must
    still reappear verbatim and in the original order, so removing a member or
    rewriting a member's body still fails.

    A window may begin or end inside a string literal, so a single boundary
    quote is trimmed from a segment.  Any other quote disqualifies the window:
    splitting inside a literal would make the segments meaningless.
    """

    segments: list[str] = []
    for raw in needle.split("},"):
        if not raw:
            continue
        segment = raw
        if segment[:1] in ("'", '"', "`"):
            segment = segment[1:]
        if segment[-1:] in ("'", '"', "`"):
            segment = segment[:-1]
        if any(quote in segment for quote in ("'", '"', "`")):
            return []
        if len(segment) < 10:
            return []
        segments.append(segment)
    if len(segments) < 2 or len(segments) > 8:
        return []
    return segments


def _executed_test_count(
    relative: str, executed_tests: Mapping[str, int] | None
) -> int:
    if not executed_tests:
        return 0
    best = 0
    for name, count in executed_tests.items():
        normalized = str(name).replace("\\", "/")
        if normalized == relative or normalized.endswith("/" + relative):
            try:
                value = int(count)
            except (TypeError, ValueError):
                continue
            best = max(best, value)
    return best


def _regex_allowed(previous: str, last_word: str) -> bool:
    if not previous:
        return True
    if previous in "([{:;,=!?&|+-*%<>~^":
        return True
    if previous in ")]}":
        return False
    return last_word in {
        "await",
        "case",
        "delete",
        "do",
        "else",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }


def _scan_javascript(text: str) -> tuple[str, list[bool], str]:
    """Return canonical code, a per-character comment mask and code-only text.

    The mask lets the equivalence check reject source that was moved into a
    comment while still accepting comment directives embedded in an otherwise
    executable snippet.
    """

    canonical: list[str] = []
    mask: list[bool] = []
    code: list[str] = []
    index = 0
    mode = "code"
    previous = ""
    last_word = ""
    template_expressions: list[int] = []

    def emit(character: str, *, comment: bool = False) -> None:
        if character.isspace():
            # Keep lexical separation in executable code.  The canonical form
            # intentionally removes whitespace for literal matching, but the
            # control-flow tokenizer must still see ``throw new Error`` as
            # three tokens instead of the single identifier ``thrownewError``.
            if not comment and code and not code[-1].isspace():
                code.append(" ")
            return
        canonical.append(character)
        mask.append(comment)
        if not comment:
            code.append(character)
        elif code and not code[-1].isspace():
            # Comments also separate tokens.  Without this, ``return/*x*/value``
            # would collapse into ``returnvalue`` and evade the early-exit
            # check.
            code.append(" ")

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
                emit(character)
                index += 1
                continue
            if character == "`":
                mode = "template"
                emit(character)
                index += 1
                continue
            if character == "/" and _regex_allowed(previous, last_word):
                mode = "regex"
                emit(character)
                index += 1
                continue
            if template_expressions and character == "{":
                template_expressions[-1] += 1
            elif template_expressions and character == "}":
                if template_expressions[-1] == 1:
                    template_expressions.pop()
                    emit(character)
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
            emit(character, comment=True)
            index += 1
            continue

        if mode == "block-comment":
            if character == "*" and following == "/":
                emit(character, comment=True)
                emit(following, comment=True)
                index += 2
                mode = "code"
                continue
            emit(character, comment=True)
            index += 1
            continue

        if mode in {"single-quote", "double-quote"}:
            emit(character)
            if character == "\\" and following:
                emit(following)
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
            emit(character)
            if character == "\\" and following:
                emit(following)
                index += 2
                continue
            if character == "`":
                mode = "code"
                previous = character
                last_word = ""
                index += 1
                continue
            if character == "$" and following == "{":
                emit(following)
                index += 2
                template_expressions.append(1)
                mode = "code"
                previous = "{"
                last_word = ""
                continue
            index += 1
            continue

        # Regular-expression literal.
        emit(character)
        if character == "\\" and following:
            emit(following)
            index += 2
            continue
        if character == "/":
            mode = "code"
            flags = index + 1
            while flags < len(text) and text[flags].isalpha():
                emit(text[flags])
                flags += 1
            index = flags
            previous = "/"
            last_word = ""
            continue
        index += 1

    return "".join(canonical), mask, "".join(code)


def _direct_assertion_bindings(text: str) -> dict[str, str]:
    """Return local names imported from a relative assertion helper.

    The migration contract requires one local helper module, but does not
    require namespace imports.  Treat named imports such as ``equal`` and
    ``throws`` as the same protected assertion primitives as ``assert.equal``
    and ``assert.throws``.  The evaluator-owned helper probe separately checks
    that the imported implementations preserve the required semantics.
    """

    bindings: dict[str, str] = {}
    executable = _scan_javascript(text)[2]
    for match in re.finditer(
        r"\bimport\s*\{([^}]*)\}\s*from\s*['\"](\.[^'\"]+)['\"]",
        executable,
    ):
        for item in match.group(1).split(","):
            words = item.strip().split()
            if not words:
                continue
            if words[0] == "type":
                words = words[1:]
            if not words:
                continue
            exported = words[0]
            local = words[2] if len(words) >= 3 and words[1] == "as" else exported
            canonical = _DIRECT_ASSERTION_EXPORTS.get(exported)
            if canonical is not None:
                bindings[local] = canonical
    return bindings


def _counts(text: str) -> tuple[int, int]:
    assertions = len(_ASSERTION.findall(text)) + len(_EXPECT.findall(text))
    direct = _direct_assertion_bindings(text)
    if direct:
        names = "|".join(re.escape(name) for name in sorted(direct, key=len, reverse=True))
        assertions += len(re.findall(rf"(?<![\w$.])(?:{names})\s*\(", text))
    tests = len(_TEST.findall(text))
    return assertions, tests


def _trivial_hits(text: str) -> list[str]:
    hits = [
        match.group(0)
        for pattern in _TRIVIAL
        for match in pattern.finditer(text)
    ]
    hits.extend(match.group(0) for match in _EMPTY_TEST.finditer(text))
    hits.extend(match.group(0) for match in _TRIVIAL_LITERAL.finditer(text))
    hits.extend(match.group(0) for match in _SELF_COMPARE.finditer(text))
    hits.extend(match.group(0) for match in _SELF_EQUAL.finditer(text))
    return hits


def _assertion_shadow_hits(text: str) -> list[str]:
    return [match.group(0) for match in _ASSERTION_SHADOW.finditer(text)]


def _scan_quoted(text: str, start: int, delimiter: str) -> int:
    """Return the end offset of a quoted string starting at ``start``."""

    end = start + 1
    while end < len(text):
        if text[end] == "\\":
            end += 2
            continue
        if text[end] == delimiter:
            return end + 1
        end += 1
    return len(text)


def _scan_template(text: str, start: int) -> int:
    """Return the end offset of a template literal, including ``${...}``.

    A plain ``scan_quoted`` closes the token at the first backtick, which is
    wrong for nested templates such as ``js`...${js`...`}...` ``.  When that
    happens the remaining file is interpreted as code in the wrong lexical
    state, and static checks can silently stop inspecting half of a converted
    test file.  Track interpolation depth and recursively skip nested
    templates instead.
    """

    index = start + 1
    interpolation_depth: list[int] = []
    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if character == "\\":
            index += 2
            continue
        if not interpolation_depth:
            if character == "`":
                return index + 1
            if character == "$" and following == "{":
                interpolation_depth.append(1)
                index += 2
                continue
            index += 1
            continue
        # Inside ``${ ... }``.  Braces in strings, comments and nested
        # templates do not affect interpolation depth.
        if character in "'\"":
            index = _scan_quoted(text, index, character)
            continue
        if character == "`":
            index = _scan_template(text, index)
            continue
        if character == "/" and following == "/":
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if character == "/" and following == "*":
            close = text.find("*/", index + 2)
            index = len(text) if close < 0 else close + 2
            continue
        if character == "{":
            interpolation_depth[-1] += 1
        elif character == "}":
            interpolation_depth[-1] -= 1
            if interpolation_depth[-1] == 0:
                interpolation_depth.pop()
        index += 1
    return index


def _tokenize(text: str) -> list[tuple[str, str, int, int]]:
    """Tokenize JavaScript just enough to inspect test callback control flow.

    Comments and regular-expression bodies are skipped.  Strings and templates
    are single tokens; assertion text can never hide inside them because the
    regular-expression fallbacks already rejected literal-only conversions.
    """

    tokens: list[tuple[str, str, int, int]] = []
    index = 0
    previous = ""
    last_word = ""

    def scan_quoted(start: int, delimiter: str) -> int:
        return _scan_quoted(text, start, delimiter)

    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if character == "/" and following == "/":
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if character == "/" and following == "*":
            close = text.find("*/", index + 2)
            index = len(text) if close < 0 else close + 2
            continue
        if character in "'\"":
            end = scan_quoted(index, character)
            tokens.append(("string", text[index:end], index, end))
            previous = character
            last_word = ""
            index = end
            continue
        if character == "`":
            # Template expressions are tokenized as part of the template.  A
            # test bypass hidden inside a template interpolation still leaves
            # the executable assertion text in the template token.  Nested
            # templates must be skipped as a whole; closing at the first
            # backtick would leave the rest of the file in the wrong lexical
            # state and silently stop control-flow checks.
            end = _scan_template(text, index)
            tokens.append(("template", text[index:end], index, end))
            previous = "`"
            last_word = ""
            index = end
            continue
        if character == "/" and _regex_allowed(previous, last_word):
            end = index + 1
            in_class = False
            while end < len(text):
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == "[":
                    in_class = True
                elif text[end] == "]":
                    in_class = False
                elif text[end] == "/" and not in_class:
                    end += 1
                    while end < len(text) and text[end].isalpha():
                        end += 1
                    break
                elif text[end] == "\n":
                    break
                end += 1
            tokens.append(("regex", text[index:end], index, end))
            previous = "/"
            last_word = ""
            index = end
            continue
        if character.isspace():
            index += 1
            continue
        if character.isalnum() or character in "_$":
            end = index + 1
            while end < len(text) and (text[end].isalnum() or text[end] in "_$"):
                end += 1
            value = text[index:end]
            kind = "number" if value[0].isdigit() else "word"
            tokens.append((kind, value, index, end))
            previous = character
            last_word = value
            index = end
            continue
        tokens.append(("punct", character, index, index + 1))
        previous = character
        last_word = ""
        index += 1
    return tokens


def _bracket_map(tokens: list[tuple[str, str, int, int]]) -> dict[int, int]:
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack: list[tuple[str, int]] = []
    result: dict[int, int] = {}
    for index, (_, value, _, _) in enumerate(tokens):
        if value in pairs:
            stack.append((value, index))
        elif value in {")", "}", "]"}:
            if not stack:
                continue
            opened, position = stack[-1]
            if pairs[opened] != value:
                continue
            stack.pop()
            result[position] = index
    return result


_ASSERTION_CHAIN_WORDS = {"not", "resolves", "rejects"}
_ASSERTION_EQUIVALENT_MATCHERS = {"toBe", "toEqual", "toStrictEqual"}


def _render_assertion_tokens(
    tokens: list[tuple[str, str, int, int]], start: int, end: int
) -> str:
    while end > start and tokens[end - 1][1] == ",":
        end -= 1
    return "".join(value for _, value, _, _ in tokens[start:end])


def _literal_matcher_argument(argument: str) -> bool:
    """Return true for a primitive literal matcher operand.

    ``toBe('b')`` and ``toEqual('b')`` are interchangeable for primitives, and
    the accepted control conversion uses exactly that rewrite.  For object or
    identifier operands the two matchers differ in identity semantics, so the
    fingerprint must keep the original matcher.
    """

    value = argument.strip()
    if not value:
        return False
    if value in {"true", "false", "null", "undefined"}:
        return True
    if re.fullmatch(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?n?", value, re.IGNORECASE):
        return True
    if value.startswith(("'", '"')) and value.endswith(value[0]):
        return True
    if value.startswith("`") and value.endswith("`") and "${" not in value:
        return True
    return False


def _proven_primitive_bindings(root: Path, relative: str, text: str) -> set[str]:
    """Prove imported primitive results against evaluator-owned source assets.

    Candidate annotations alone are not type evidence. Require the test's
    entire executable binding context to match the frozen reference (apart
    from equality matcher spelling), and require the imported implementation
    to match the frozen implementation. Unknown or changed modules fail closed.
    """
    assets = Path(__file__).resolve().parent.parent / "assets"
    reference = _text(assets / "reference-tests", relative)
    if reference is None:
        return set()

    def context(value: str) -> list[tuple[str, str]]:
        return [
            (kind, "toEqual" if kind == "word" and word in
             _ASSERTION_EQUIVALENT_MATCHERS else word)
            for kind, word, _, _ in _tokenize(_scan_javascript(value)[2])
        ]

    if context(text) != context(reference):
        return set()

    def implementation(value: str) -> list[tuple[str, str]]:
        # JSDoc casts around an identifier are runtime identity operations.
        # The official conversion adds nested casts to a typed-array argument.
        cast = re.compile(
            r"/\*\*\s*@type\s*\{[^}]*\}\s*\*/\s*\(\s*([\w$]+)\s*\)"
        )
        while cast.search(value):
            value = cast.sub(r"\1", value)
        return [(kind, word) for kind, word, _, _ in
                _tokenize(_scan_javascript(value)[2])]

    proven: set[str] = set()
    for imported in re.finditer(
        r"\bimport\s+\*\s+as\s+(\w+)\s+from\s+['\"](\.[^'\"]+)['\"]",
        reference,
    ):
        namespace, specifier = imported.groups()
        module = str(Path(relative).parent / specifier)
        frozen = _text(assets / "source-runtime", module)
        candidate = _text(root, module)
        if frozen is None or candidate is None:
            continue
        if implementation(frozen) != implementation(candidate):
            continue
        primitive_exports = set(re.findall(
            r"/\*\*\s*@type\s*\{\([^\n]*?\)\s*=>\s*"
            r"(?:string|number|boolean|bigint)\}\s*\*/\s*"
            r"export\s+function\s+(\w+)\s*\(", frozen,
        ))
        for binding in re.finditer(
            r"\bconst\s+(\w+)\s*=\s*" + re.escape(namespace)
            + r"\.(\w+)\s*\(", _scan_javascript(reference)[2],
        ):
            name, function = binding.groups()
            if function in primitive_exports:
                proven.add(name)
    return proven


def _assertion_fingerprints(
    text: str, primitive_bindings: set[str] | None = None
) -> Counter[str]:
    """Return normalized executable assertion fingerprints.

    The protected reference conversion is the source of truth for assertion
    strength.  Comparing only counts or matcher names lets a candidate replace
    ``expect(value).toBe('b')`` with ``expect(value).toBeDefined()`` without
    changing the total.  Fingerprints retain the operand expression, matcher
    and duplicate count; only whitespace and the equivalent primitive matcher
    family are normalized.
    """

    tokens = _tokenize(text)
    brackets = _bracket_map(tokens)
    direct_bindings = _direct_assertion_bindings(text)
    fingerprints: Counter[str] = Counter()
    index = 0
    while index < len(tokens):
        kind, value, _, _ = tokens[index]
        if kind == "word" and value == "expect":
            # ``expect.unreachable(...)`` has no initial argument list.
            if (
                index + 3 < len(tokens)
                and tokens[index + 1][1] == "."
                and tokens[index + 2][0] == "word"
                and tokens[index + 3][1] == "("
            ):
                close = brackets.get(index + 3)
                if close is not None:
                    matcher = tokens[index + 2][1]
                    arguments = _render_assertion_tokens(
                        tokens, index + 4, close
                    )
                    fingerprints[
                        f"expect.{matcher}({arguments})"
                    ] += 1
                    index = close + 1
                    continue
            if index + 1 < len(tokens) and tokens[index + 1][1] == "(":
                close = brackets.get(index + 1)
                if close is not None:
                    cursor = close + 1
                    chain: list[str] = []
                    matched = False
                    while (
                        cursor + 1 < len(tokens)
                        and tokens[cursor][1] == "."
                        and tokens[cursor + 1][0] == "word"
                    ):
                        property_name = tokens[cursor + 1][1]
                        if property_name in _ASSERTION_CHAIN_WORDS:
                            chain.extend([".", property_name])
                            cursor += 2
                            continue
                        if (
                            cursor + 2 < len(tokens)
                            and tokens[cursor + 2][1] == "("
                        ):
                            matcher_close = brackets.get(cursor + 2)
                            if matcher_close is not None:
                                matcher = property_name
                                arguments = _render_assertion_tokens(
                                    tokens, index + 2, close
                                )
                                matcher_arguments = _render_assertion_tokens(
                                    tokens, cursor + 3, matcher_close
                                )
                                if (
                                    matcher in _ASSERTION_EQUIVALENT_MATCHERS
                                    and (
                                        _literal_matcher_argument(matcher_arguments)
                                        or (
                                            arguments in (primitive_bindings or set())
                                            and matcher_arguments in (primitive_bindings or set())
                                        )
                                    )
                                ):
                                    matcher = "toEqual"
                                fingerprints[
                                    "expect("
                                    + arguments
                                    + ")"
                                    + "".join(chain)
                                    + "."
                                    + matcher
                                    + "("
                                    + matcher_arguments
                                    + ")"
                                ] += 1
                                index = matcher_close + 1
                                matched = True
                        break
                    if matched:
                        continue
        direct_name = direct_bindings.get(value) if kind == "word" else None
        if (
            kind == "word"
            and value == "assert"
            and index + 2 < len(tokens)
            and tokens[index + 1][1] == "."
            and tokens[index + 2][0] == "word"
        ):
            cursor = index + 2
            name = tokens[cursor][1]
            if (
                name == "not"
                and cursor + 2 < len(tokens)
                and tokens[cursor + 1][1] == "."
                and tokens[cursor + 2][0] == "word"
            ):
                name = "not." + tokens[cursor + 2][1]
                cursor += 2
            if cursor + 1 < len(tokens) and tokens[cursor + 1][1] == "(":
                close = brackets.get(cursor + 1)
                if close is not None:
                    ranges = _split_arguments(tokens, cursor + 2, close)
                    if (
                        name in {"deepStrictEqual", "equal", "is", "strictEqual"}
                        and len(ranges) >= 2
                    ):
                        expected = _render_assertion_tokens(tokens, *ranges[1])
                        if _literal_matcher_argument(expected):
                            name = "equal"
                    arguments = _render_assertion_tokens(tokens, cursor + 2, close)
                    fingerprints[f"assert.{name}({arguments})"] += 1
                    index = close + 1
                    continue
        elif direct_name is not None and index + 1 < len(tokens) and tokens[index + 1][1] == "(":
            close = brackets.get(index + 1)
            if close is not None:
                name = direct_name
                ranges = _split_arguments(tokens, index + 2, close)
                if name in {"deepStrictEqual", "equal", "is", "strictEqual"} and len(ranges) >= 2:
                    expected = _render_assertion_tokens(tokens, *ranges[1])
                    if _literal_matcher_argument(expected):
                        name = "equal"
                arguments = _render_assertion_tokens(tokens, index + 2, close)
                fingerprints[f"assert.{name}({arguments})"] += 1
                index = close + 1
                continue
        index += 1
    return fingerprints


def _timing_assertion(fingerprint: str) -> tuple[str, int] | None:
    """Return a threshold-independent key and bound for a wall-clock assertion.

    The key deliberately keeps the operands, comparison and message wording;
    only the two identical numeric bounds are normalized.  This closes the
    common environment-calibration rewrite without accepting deletion or a
    changed matcher.  The caller applies the bounded multiplier.
    """

    match = _TIMING_ASSERTION.fullmatch(fingerprint)
    if match is None:
        return None
    threshold = int(match.group("threshold"))
    key = (
        match.group("prefix")
        + "#"
        + match.group("middle")
        + "#"
        + match.group("suffix")
    )
    return key, threshold


def _split_arguments(
    tokens: list[tuple[str, str, int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    arguments: list[tuple[int, int]] = []
    depth = 0
    item = start
    for index in range(start, end):
        value = tokens[index][1]
        if value in {"(", "{", "["}:
            depth += 1
        elif value in {")", "}", "]"}:
            depth -= 1
        elif value == "," and depth == 0:
            arguments.append((item, index))
            item = index + 1
    if item < end:
        arguments.append((item, end))
    return arguments


def _callback_body(
    tokens: list[tuple[str, str, int, int]],
    brackets: Mapping[int, int],
    start: int,
    end: int,
) -> tuple[int, int] | None:
    arrow = None
    depth = 0
    for index in range(start, end):
        value = tokens[index][1]
        if value in {"(", "{", "["}:
            depth += 1
        elif value in {")", "}", "]"}:
            depth -= 1
        elif value == "=" and index + 1 < end and tokens[index + 1][1] == ">":
            arrow = index + 2
            break
    if arrow is None:
        # function (...) { ... }
        for index in range(start, end):
            if tokens[index][1] == "{":
                close = brackets.get(index)
                if close is not None and close < end:
                    return index + 1, close
        return None
    if arrow >= end:
        return None
    if tokens[arrow][1] == "{":
        close = brackets.get(arrow)
        if close is None or close > end:
            return None
        return arrow + 1, close
    return arrow, end


def _test_callbacks(
    tokens: list[tuple[str, str, int, int]],
) -> list[tuple[int, int]]:
    brackets = _bracket_map(tokens)
    callbacks: list[tuple[int, int]] = []
    for index, (kind, value, _, _) in enumerate(tokens):
        if kind != "word" or value not in {"test", "it"}:
            continue
        if index > 0 and tokens[index - 1][1] in {".", "?"}:
            continue
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor][1] == ".":
            if cursor + 1 >= len(tokens) or tokens[cursor + 1][1] not in _TEST_MODIFIERS:
                continue
            cursor += 2
            if cursor < len(tokens) and tokens[cursor][1] == "(":
                close = brackets.get(cursor)
                if close is None:
                    continue
                cursor = close + 1
        if cursor >= len(tokens) or tokens[cursor][1] != "(":
            continue
        close = brackets.get(cursor)
        if close is None:
            continue
        for argument_start, argument_end in _split_arguments(tokens, cursor + 1, close):
            if argument_start >= argument_end:
                continue
            body = _callback_body(tokens, brackets, argument_start, argument_end)
            if body is not None:
                callbacks.append(body)
    return callbacks


def _nested_function_ranges(
    tokens: list[tuple[str, str, int, int]],
    start: int,
    end: int,
    brackets: Mapping[int, int],
) -> list[tuple[int, int]]:
    """Return brace ranges belonging to functions defined inside a callback.

    A ``return`` inside ``assert.throws(() => { ... })`` is part of the
    assertion fixture, not an early exit from the test callback.  Only the
    callback's own statements are relevant to the control-flow bypass check.
    """

    ranges: list[tuple[int, int]] = []
    method_prefixes = {"get", "set", "async", "static"}
    method_boundaries = {"{", ",", ";", "}", "*", "#"}
    control_words = {
        "catch",
        "do",
        "else",
        "finally",
        "for",
        "function",
        "if",
        "switch",
        "try",
        "while",
        "with",
    }

    def method_body(cursor: int) -> int | None:
        """Return the opening brace of a class/object method at ``cursor``."""

        first = cursor
        while cursor < end and tokens[cursor][1] in method_prefixes:
            cursor += 1
        if cursor < end and tokens[cursor][1] == "(":
            # ``get(...)`` / ``set(...)`` are ordinary method names; only a
            # modifier followed by another name is a getter/setter/async
            # method declaration.
            cursor = first
        if cursor < end and tokens[cursor][1] == "*":
            cursor += 1
        if cursor < end and tokens[cursor][1] == "#":
            cursor += 1
        if cursor >= end or tokens[cursor][0] != "word":
            return None
        if tokens[cursor][1] in control_words:
            return None
        previous = tokens[first - 1][1] if first > start else None
        if (
            previous is not None
            and previous not in method_boundaries
            and tokens[first][1] not in method_prefixes
        ):
            return None
        cursor += 1
        if cursor >= end or tokens[cursor][1] != "(":
            return None
        params_close = brackets.get(cursor)
        if params_close is None or params_close + 1 >= end:
            return None
        if tokens[params_close + 1][1] != "{":
            return None
        return params_close + 1

    def computed_method_body(cursor: int) -> int | None:
        """Return the opening brace of a computed method name ``[expr]()``."""

        previous = tokens[cursor - 1][1] if cursor > start else None
        if previous is not None and previous not in method_boundaries:
            return None
        name_close = brackets.get(cursor)
        if name_close is None or name_close + 1 >= end:
            return None
        cursor = name_close + 1
        if tokens[cursor][1] != "(":
            return None
        params_close = brackets.get(cursor)
        if params_close is None or params_close + 1 >= end:
            return None
        if tokens[params_close + 1][1] != "{":
            return None
        return params_close + 1

    index = start
    while index < end:
        kind, value, _, _ = tokens[index]
        if kind == "word" and value == "function":
            cursor = index + 1
            if cursor < end and tokens[cursor][0] == "word":
                cursor += 1
            if cursor < end and tokens[cursor][1] == "(":
                params_close = brackets.get(cursor)
                if params_close is not None and params_close < end:
                    cursor = params_close + 1
            if cursor < end and tokens[cursor][1] == "{":
                close = brackets.get(cursor)
                if close is not None and close < end:
                    ranges.append((cursor, close))
                    index = close + 1
                    continue
        if kind == "word" or value == "[":
            opening = (
                method_body(index)
                if kind == "word"
                else computed_method_body(index)
            )
            if opening is not None:
                close = brackets.get(opening)
                if close is not None and close < end:
                    ranges.append((opening, close))
                    index = close + 1
                    continue
        if value == "=" and index + 1 < end and tokens[index + 1][1] == ">":
            cursor = index + 2
            if cursor < end and tokens[cursor][1] == "{":
                close = brackets.get(cursor)
                if close is not None and close < end:
                    ranges.append((cursor, close))
                    index = close + 1
                    continue
        index += 1
    return ranges


def _callback_violations(
    tokens: list[tuple[str, str, int, int]],
    start: int,
    end: int,
    assertion_words: set[str],
) -> list[str]:
    """Reject assertion-bypassing control flow at any callback nesting depth.

    The previous implementation only looked at brace depth zero.  Wrapping the
    early exit in an otherwise redundant block (``{ return; }``) therefore kept
    the assertion text, declaration counts and required literals intact while
    making every assertion unreachable.  The walker below deliberately ignores
    nested function bodies (their returns belong to the helper, not to the
    test) but inspects every statement block that is part of the callback
    itself.
    """

    violations: list[str] = []
    if end <= start:
        return ["empty test callback"]
    brackets = _bracket_map(tokens)
    reverse_brackets = {close: opened for opened, close in brackets.items()}
    nested = _nested_function_ranges(tokens, start, end, brackets)
    skipped = {opened: close for opened, close in nested}

    def block_is_conditional(open_index: int) -> bool:
        cursor = open_index - 1
        while cursor >= start and tokens[cursor][1] == ";":
            cursor -= 1
        if cursor < start:
            return False
        if tokens[cursor][1] == ")":
            opened = reverse_brackets.get(cursor)
            if opened is not None and opened > start:
                keyword = tokens[opened - 1][1]
                return keyword in {"if", "for", "while", "switch", "catch", "with"}
            return False
        return tokens[cursor][1] in {"else", "try", "finally", "do"}

    def termination_is_conditional(index: int, enclosing: tuple[int, ...]) -> bool:
        for opened in enclosing:
            if block_is_conditional(opened):
                return True
        # Brace-less branches such as ``if (condition) return;`` do not create
        # an enclosing block.  Look back over the current statement for the
        # closing parenthesis of a conditional head.
        cursor = index - 1
        while cursor >= start and tokens[cursor][1] not in {";", "{", "}"}:
            if tokens[cursor][1] == ")":
                opened = reverse_brackets.get(cursor)
                if opened is not None and opened > start:
                    keyword = tokens[opened - 1][1]
                    if keyword in {"if", "for", "while"}:
                        return True
            cursor -= 1
        return False

    def safe_expected_throw_catch_return(
        index: int, enclosing: tuple[int, ...]
    ) -> bool:
        """Accept the inverse expected-throw idiom used by valid conversions.

        A converted test may call the operation in ``try``, assert on the caught
        error, return from that catch, and put ``unreachable()`` after the catch.
        The return is what keeps the failure marker from running on the success
        path, so treating every conditional return as an assertion bypass gives
        this strengthening of the source test an anti-cheating penalty.

        Keep the exception deliberately narrow: the return must be the final
        catch statement, an assertion before it must inspect the caught value,
        and the callback tail must consist solely of a failure marker.
        """

        catch_open = None
        catch_parameter = None
        for opened in reversed(enclosing):
            cursor = opened - 1
            if cursor < start:
                continue
            if tokens[cursor][1] == ")":
                params_open = reverse_brackets.get(cursor)
                if (
                    params_open is None
                    or params_open <= start
                    or tokens[params_open - 1][1] != "catch"
                ):
                    continue
                if (
                    params_open + 1 < cursor
                    and tokens[params_open + 1][0] == "word"
                ):
                    catch_parameter = tokens[params_open + 1][1]
                catch_open = opened
                break
            if tokens[cursor][1] == "catch":
                catch_open = opened
                break
        if catch_open is None or catch_parameter is None:
            return False
        catch_close = brackets.get(catch_open)
        if catch_close is None or not (catch_open < index < catch_close):
            return False
        if any(tokens[cursor][1] != ";" for cursor in range(index + 1, catch_close)):
            return False

        catch_reasserts_caught = False
        for offset in range(catch_open + 1, index):
            if tokens[offset][0] != "word" or tokens[offset][1] not in assertion_words:
                continue
            if any(
                tokens[probe][0] == "word"
                and tokens[probe][1] == catch_parameter
                for probe in range(offset + 1, min(offset + 16, index))
            ):
                catch_reasserts_caught = True
                break
        if not catch_reasserts_caught:
            return False

        tail = catch_close + 1
        while tail < end and tokens[tail][1] == ";":
            tail += 1
        if tail >= end or tokens[tail][0] != "word":
            return False
        marker = tokens[tail][1]
        cursor = tail + 1
        if marker == "assert" and cursor + 1 < end and tokens[cursor][1] == ".":
            if tokens[cursor + 1][1] not in {"fail", "unreachable"}:
                return False
            cursor += 2
        elif marker == "expect" and cursor + 1 < end and tokens[cursor][1] == ".":
            if tokens[cursor + 1][1] != "unreachable":
                return False
            cursor += 2
        elif _DIRECT_ASSERTION_EXPORTS.get(marker) not in {"fail", "unreachable"}:
            return False
        if cursor >= end or tokens[cursor][1] != "(":
            return False
        call_close = brackets.get(cursor)
        if call_close is None:
            return False
        return all(
            tokens[probe][1] == ";" for probe in range(call_close + 1, end)
        )

    assertions: list[int] = []
    terminations: list[tuple[int, str, bool, tuple[int, ...]]] = []
    stack: list[int] = []
    index = start
    while index < end:
        kind, value, _, _ = tokens[index]
        if index in skipped:
            index = skipped[index] + 1
            continue
        if kind == "word" and value in assertion_words:
            assertions.append(index)
        if kind == "word" and value in {"return", "throw"}:
            terminations.append(
                (
                    index,
                    value,
                    termination_is_conditional(index, tuple(stack)),
                    tuple(stack),
                )
            )
        if kind == "word" and value == "if":
            scope = index + 1
            if scope < end and tokens[scope][1] == "(":
                condition = scope + 1
                if condition < end and tokens[condition][1] in {"false", "0"}:
                    violations.append(
                        "test callback contains a constant-false branch"
                    )
        if value == ";":
            index += 1
            continue
        if value == "{":
            stack.append(index)
        elif value == "}" and stack:
            stack.pop()
        index += 1

    for index, value, conditional, enclosing in terminations:
        before_assertion = not any(item < index for item in assertions)
        after_assertion = any(item > index for item in assertions)
        # A return that is not the final statement can skip a protected
        # assertion.  Returns inside conditional branches are always rejected,
        # including the ``if (...) { assert(...) } else { return; }`` shape
        # where the assertion appears before the exit in source order.
        if (
            value == "return"
            and conditional
            and safe_expected_throw_catch_return(index, enclosing)
        ):
            continue
        if not (before_assertion or after_assertion or conditional):
            continue
        if before_assertion and index == start:
            violations.append(
                "test callback begins with an unconditional "
                f"{value} before any assertion"
            )
        elif before_assertion:
            violations.append(
                f"test callback reaches {value} before any assertion"
            )
        elif conditional:
            violations.append(
                f"test callback has a conditional {value} that can bypass "
                "protected assertions"
            )
        else:
            violations.append(
                f"test callback reaches {value} before later protected assertions"
            )
        break

    # A callback may legitimately delegate to a named assertion helper (for
    # example ``assert_parity(value)``).  Empty callbacks are rejected by the
    # structural checks and helper definitions are audited separately, so a
    # bare ``no assertion`` heuristic here would reject valid conversions.
    # A catch block that swallows an assertion failure makes the callback pass
    # even though the protected assertion was reached and failed.
    for index in range(start, end):
        if index in skipped:
            continue
        if tokens[index][1] != "try" or index + 1 >= end or tokens[index + 1][1] != "{":
            continue
        try_close = brackets.get(index + 1)
        if try_close is None or try_close + 2 >= end:
            continue
        if tokens[try_close + 1][1] != "catch":
            continue
        cursor = try_close + 2
        if cursor < end and tokens[cursor][1] == "(":
            cursor = brackets.get(cursor, cursor) + 1
        if cursor >= end or tokens[cursor][1] != "{":
            continue
        catch_close = brackets.get(cursor)
        if catch_close is None:
            continue
        try_tokens = tokens[index + 2 : try_close]
        catch_tokens = tokens[cursor + 1 : catch_close]
        try_asserts = any(
            item[0] == "word" and item[1] in assertion_words for item in try_tokens
        )
        catch_rethrows = any(item[1] == "throw" for item in catch_tokens)
        catch_fails = any(
            (catch_tokens[offset][1] == "assert"
             and offset + 2 < len(catch_tokens)
             and catch_tokens[offset + 1][1] == "."
             and catch_tokens[offset + 2][1] == "fail")
            or (catch_tokens[offset][1] == "expect"
                and offset + 2 < len(catch_tokens)
                and catch_tokens[offset + 1][1] == "."
                and catch_tokens[offset + 2][1] == "unreachable")
            for offset in range(len(catch_tokens))
        )
        # The protected source itself uses the expected-throw idiom: call the
        # operation inside ``try``, assert ``unreachable`` when it returns, and
        # re-assert on the caught error in ``catch``.  That shape cannot swallow
        # a protected failure -- if the operation returns, ``unreachable``
        # throws an error whose fields do not satisfy the catch assertions, so
        # the test still fails.  Accept it only when (a) every assertion in the
        # ``try`` block is an expected-throw marker and (b) the ``catch`` block
        # asserts on the caught value.  An empty catch, a catch that only
        # contains inert literals, or a ``try`` holding ordinary assertions
        # still counts as swallowing.
        catch_parameter = None
        if (
            try_close + 3 < end
            and tokens[try_close + 2][1] == "("
            and tokens[try_close + 3][0] == "word"
        ):
            catch_parameter = tokens[try_close + 3][1]

        def _expected_throw_marker(offset: int) -> bool:
            token = try_tokens[offset][1]
            direct = _DIRECT_ASSERTION_EXPORTS.get(token)
            if direct is not None:
                return direct in {"throws", "unreachable"}
            if token == "assert":
                return (
                    offset + 2 < len(try_tokens)
                    and try_tokens[offset + 1][1] == "."
                    and try_tokens[offset + 2][1] in {"fail", "throws", "unreachable"}
                )
            if token == "expect":
                probe = offset + 1
                while (
                    probe + 1 < len(try_tokens)
                    and try_tokens[probe][1] == "."
                    and try_tokens[probe + 1][0] == "word"
                ):
                    if try_tokens[probe + 1][1] in {"toThrow", "unreachable"}:
                        return True
                    probe += 2
            return False

        try_offsets = [
            offset
            for offset, item in enumerate(try_tokens)
            if item[0] == "word" and item[1] in assertion_words
        ]
        try_has_ordinary_assertion = any(
            not _expected_throw_marker(offset) for offset in try_offsets
        )
        catch_reasserts_caught = False
        if catch_parameter is not None:
            for offset in range(len(catch_tokens)):
                if (
                    catch_tokens[offset][0] == "word"
                    and catch_tokens[offset][1] in assertion_words
                ):
                    if any(
                        catch_tokens[probe][0] == "word"
                        and catch_tokens[probe][1] == catch_parameter
                        for probe in range(
                            offset + 1, min(offset + 16, len(catch_tokens))
                        )
                    ):
                        catch_reasserts_caught = True
                        break
        expected_throw_idiom = (
            catch_reasserts_caught
            and bool(try_offsets)
            and not try_has_ordinary_assertion
        )
        if (
            try_asserts
            and not catch_rethrows
            and not catch_fails
            and not expected_throw_idiom
        ):
            violations.append("test callback swallows an assertion failure in catch")
    return violations


def _control_flow_hits(text: str) -> list[str]:
    tokens = _tokenize(text)
    assertion_words = _ASSERTION_WORDS | set(_direct_assertion_bindings(text))
    hits: list[str] = []
    for start, end in _test_callbacks(tokens):
        hits.extend(_callback_violations(tokens, start, end, assertion_words))
    if _IF_FALSE.search(text):
        hits.append("constant-false branch outside an analysable test callback")
    return list(dict.fromkeys(hits))


def _string_literal_units(literal: str) -> bytes | None:
    """Return a JavaScript string literal as UTF-16 code units when possible.

    Source spelling is not a scenario value.  For example, ``'𝌆'`` and
    ``'\uD834\uDF06'`` are the same JavaScript string, and changing between
    those spellings is explicitly relevant when migrating surrogate handling.
    Comparing UTF-16 units also makes a raw astral character equal to its
    surrogate-pair escape without treating a genuinely changed value as equal.
    """

    value = literal.strip()
    if len(value) < 2 or value[0] not in {"'", '"'} or value[-1] != value[0]:
        return None
    try:
        decoded = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(decoded, str):
        return None
    return decoded.encode("utf-16-le", errors="surrogatepass")


def load_equivalence(
    path: Path,
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"source test equivalence asset is invalid: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("source test equivalence asset must be an object")
    schema = payload.get("schema_version")
    if schema not in {
        "migration-source-test-equivalence:v1",
        "migration-source-test-equivalence:v2",
        "migration-source-test-equivalence:v3",
    }:
        raise ValueError("source test equivalence asset has an unsupported schema")
    files = payload.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("source test equivalence asset must declare files")
    if baseline is not None:
        if not isinstance(baseline, Mapping):
            raise ValueError("protected source test baseline must be an object")
        baseline_files = baseline.get("files")
        if not isinstance(baseline_files, Mapping):
            raise ValueError("protected source test baseline must declare files")
        for relative, record in sorted(files.items()):
            if not isinstance(relative, str) or not relative:
                raise ValueError(
                    "source test equivalence asset contains an invalid path"
                )
            if not isinstance(record, Mapping):
                raise ValueError(
                    f"source test equivalence record is invalid: {relative}"
                )
            expected = baseline_files.get(relative)
            if not isinstance(expected, Mapping) or not isinstance(
                expected.get("sha256"), str
            ):
                raise ValueError(
                    f"source test equivalence path is absent from the protected "
                    f"baseline: {relative}"
                )
            if record.get("source_sha256") != expected["sha256"]:
                raise ValueError(
                    f"source test equivalence digest does not match the protected "
                    f"baseline: {relative}"
                )
            _validate_record(relative, record, schema)
    return dict(files)


def _validate_record(relative: str, record: Mapping[str, Any], schema: str) -> None:
    tokens = record.get("required_literal_tokens")
    comment_only = record.get("comment_only_tokens", [])
    if not isinstance(tokens, list) or not all(
        isinstance(token, str) for token in tokens
    ):
        raise ValueError(
            f"source test equivalence literals are invalid: {relative}"
        )
    if not isinstance(comment_only, list) or not all(
        isinstance(token, str) for token in comment_only
    ):
        raise ValueError(
            f"source test equivalence comment-only literals are invalid: {relative}"
        )
    if not set(comment_only).issubset(tokens):
        raise ValueError(
            f"source test equivalence comment-only literals are not a "
            f"subset of the retained literals: {relative}"
        )
    if schema not in {
        "migration-source-test-equivalence:v2",
        "migration-source-test-equivalence:v3",
    }:
        return
    cases = record.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(
            f"source test equivalence case inventory is missing: {relative}"
        )
    reference_tests = record.get("reference_test_count")
    if not isinstance(reference_tests, int) or reference_tests < 1:
        raise ValueError(
            f"source test equivalence reference test count is invalid: {relative}"
        )
    # ``reference_test_count`` protects the reference conversion's visible
    # test declarations.  The one-to-one source inventory is a separate floor:
    # every original source case must appear in ``cases`` even when a framework
    # conversion legitimately splits or parameterises it.
    source_tests = record.get("source_test_count", record.get("test_count"))
    if not isinstance(source_tests, int) or source_tests < 1:
        raise ValueError(
            f"source test equivalence source test count is invalid: {relative}"
        )
    if len(cases) < source_tests:
        raise ValueError(
            f"source test equivalence case inventory is missing: {relative} "
            f"({len(cases)} < {source_tests})"
        )
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ValueError(
                f"source test equivalence case {index} is invalid: {relative}"
            )
        stable_id = case.get("stable_id")
        if not isinstance(stable_id, str) or not stable_id:
            raise ValueError(
                f"source test equivalence case {index} lacks a stable id: {relative}"
            )
        if stable_id in seen:
            raise ValueError(
                f"source test equivalence contains a duplicate stable id: {stable_id}"
            )
        seen.add(stable_id)
        suite = case.get("suite")
        if not isinstance(suite, list) or not all(
            isinstance(item, str) for item in suite
        ):
            raise ValueError(
                f"source test equivalence case {stable_id} has an invalid suite"
            )
        name = case.get("name")
        # Parameterised test titles are allowed to be empty (the empty-string
        # case in the base64 matrix is a protected source case).  Identity is
        # carried by ``stable_id`` and ``target_titles``, not by the display
        # title alone.
        if not isinstance(name, str):
            raise ValueError(
                f"source test equivalence case {stable_id} has an invalid name"
            )
        if case.get("match") not in _MATCH_TYPES:
            raise ValueError(
                f"source test equivalence case {stable_id} has an invalid match type"
            )
        target_titles = case.get("target_titles")
        if not isinstance(target_titles, list) or not target_titles:
            raise ValueError(
                f"source test equivalence case {stable_id} has no executable target"
            )
        for title in target_titles:
            if (
                not isinstance(title, list)
                or not title
                or not all(isinstance(item, str) for item in title)
                or not title[-1]
            ):
                raise ValueError(
                    f"source test equivalence case {stable_id} has an invalid target title"
                )
        assertions = case.get("assertion_tokens")
        if (
            not isinstance(assertions, list)
            or not assertions
            or not all(isinstance(item, str) and item for item in assertions)
        ):
            raise ValueError(
                f"source test equivalence case {stable_id} has no assertion mapping"
            )
        expected = case.get("expected_tokens")
        if not isinstance(expected, list) or not all(
            isinstance(item, str) for item in expected
        ):
            raise ValueError(
                f"source test equivalence case {stable_id} has invalid expectations"
            )
        assertion_count = case.get("assertion_count")
        if (
            not isinstance(assertion_count, int)
            or isinstance(assertion_count, bool)
            or assertion_count < 0
        ):
            raise ValueError(
                f"source test equivalence case {stable_id} has an invalid "
                f"assertion count"
            )
        target_file = case.get("target_file")
        if not isinstance(target_file, str) or not target_file:
            raise ValueError(
                f"source test equivalence case {stable_id} has no target file"
            )
    if schema == "migration-source-test-equivalence:v3":
        fingerprints = record.get("reference_assertions")
        if not isinstance(fingerprints, Mapping) or not fingerprints:
            raise ValueError(
                f"source test equivalence reference assertions are missing: {relative}"
            )
        for fingerprint, count in fingerprints.items():
            if not isinstance(fingerprint, str) or not fingerprint:
                raise ValueError(
                    f"source test equivalence reference assertion is invalid: {relative}"
                )
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 1
            ):
                raise ValueError(
                    f"source test equivalence reference assertion count is invalid: "
                    f"{relative}"
                )


def _baseline_kind(
    record: Mapping[str, Any], executable: str
) -> tuple[str, int, int]:
    """Return the matching test-era baseline and its fingerprint hit counts."""

    observed_assertions = _assertion_fingerprints(executable)
    assertions, _ = _counts(executable)
    source_count = int(record.get("assertion_count", 0) or 0)
    reference_count = int(record.get("reference_assertion_count", 0) or 0)
    source_fingerprints = record.get("source_assertions")
    reference_fingerprints = record.get("reference_assertions")

    def fingerprint_hits(expected: object) -> int:
        if not isinstance(expected, Mapping):
            return 0
        total = 0
        for fingerprint, required in expected.items():
            if not isinstance(required, int) or isinstance(required, bool):
                continue
            total += min(observed_assertions.get(str(fingerprint), 0), required)
        return total

    source_hits = fingerprint_hits(source_fingerprints)
    reference_hits = fingerprint_hits(reference_fingerprints)
    source_total = sum(
        int(value)
        for value in (
            source_fingerprints.values()
            if isinstance(source_fingerprints, Mapping)
            else []
        )
        if isinstance(value, int) and not isinstance(value, bool)
    )
    reference_total = sum(
        int(value)
        for value in (
            reference_fingerprints.values()
            if isinstance(reference_fingerprints, Mapping)
            else []
        )
        if isinstance(value, int) and not isinstance(value, bool)
    )
    # A candidate may keep the source-era uvu assertions or use the target-era
    # converted suite.  Compare against the matching baseline: requiring
    # target-only assertions from a source-era candidate would penalise a valid
    # migration for tests it never had, while applying the source floor to a
    # target-era file would miss a deleted target assertion.  Fingerprint
    # matches identify the family unambiguously; the count midpoint is the
    # fallback for an expect-only source rewrite.
    if reference_total and reference_hits >= max(1, (reference_total + 1) // 2):
        return "reference", source_hits, reference_hits
    if source_total and source_hits >= max(1, (source_total + 1) // 2):
        return "source", source_hits, reference_hits
    baseline = (
        "reference"
        if reference_count and assertions >= max(reference_count, source_count)
        else "source"
    )
    return baseline, source_hits, reference_hits


def detect_test_baselines(
    root: Path,
    *,
    allowed_paths: set[str],
    equivalence: Mapping[str, Any],
) -> dict[str, str]:
    """Identify source-era versus target-era test files for execution checks."""

    modes: dict[str, str] = {}
    for relative in sorted(allowed_paths):
        text = _text(root, relative)
        record = equivalence.get(relative)
        if text is None or not isinstance(record, Mapping):
            continue
        _, _, executable = _scan_javascript(text)
        if not executable.strip():
            continue
        baseline, _, _ = _baseline_kind(record, executable)
        modes[relative] = baseline
    return modes


def check_converted_tests(
    root: Path,
    *,
    allowed_paths: set[str],
    equivalence: Mapping[str, Any],
    enforce_counts: bool = True,
    executed_tests: Mapping[str, int] | None = None,
    enforce_test_floor: bool = True,
) -> tuple[list[str], list[str]]:
    """Return (structural failures, notes) for allowed converted test files.

    ``enforce_counts`` additionally checks the reference assertion/test floors
    and the normalized reference-assertion fingerprints.  Callers that need the
    full hard-gate signal must leave it enabled; structural and anti-bypass
    checks are always mandatory.

    ``enforce_test_floor`` controls only the static test-declaration floor.  A
    suite adapter may legitimately register dynamic cases from a single static
    declaration, so callers that classify evaluator-executed cases separately
    can disable this signal without weakening the assertion, literal or
    control-flow checks.  The default remains enabled.

    ``executed_tests`` maps a converted test path to the number of cases the
    evaluator-controlled run actually registered.  A suite adapter (for
    example ``create_suite``/``register_test``) can expand one static
    declaration into many executed cases, so the registered count corroborates
    the protected test floor without trusting the candidate's static shape.
    """

    failures: list[str] = []
    notes: list[str] = []
    for relative in sorted(allowed_paths):
        text = _text(root, relative)
        if text is None:
            failures.append(f"converted test file is missing: {relative}")
            continue
        if not text.strip():
            failures.append(f"converted test file is empty: {relative}")
            continue
        record = equivalence.get(relative)
        if not isinstance(record, Mapping):
            failures.append(
                f"no protected equivalence record exists for converted test: {relative}"
            )
            continue
        canonical, comment_mask, executable = _scan_javascript(text)
        if not executable.strip():
            failures.append(f"converted test file has no executable code: {relative}")
            continue
        assertions, tests = _counts(executable)
        primitive_bindings = _proven_primitive_bindings(root, relative, text)
        observed_assertions = _assertion_fingerprints(executable, primitive_bindings)
        observed_exact: Counter[str] = Counter()
        observed_timing: dict[str, list[tuple[int, int]]] = {}
        for fingerprint, count in observed_assertions.items():
            timing = _timing_assertion(str(fingerprint))
            if timing is None:
                observed_exact[str(fingerprint)] += count
            else:
                key, threshold = timing
                observed_timing.setdefault(key, []).append((threshold, count))
        baseline, source_hits, reference_hits = _baseline_kind(record, executable)
        source_count = int(record.get("assertion_count", 0) or 0)
        source_test_count = int(record.get("test_count", 0) or 0)
        reference_count = int(record.get("reference_assertion_count", 0) or 0)
        reference_test_count = int(record.get("reference_test_count", 0) or 0)
        source_fingerprints = record.get("source_assertions")
        reference_fingerprints = record.get("reference_assertions")
        if baseline == "reference":
            protected_count = reference_count or source_count
            protected_tests = reference_test_count or source_test_count
            protected_fingerprints = (
                reference_fingerprints
                if reference_hits
                else (source_fingerprints if source_hits else None)
            )
        else:
            protected_count = source_count
            protected_tests = source_test_count
            protected_fingerprints = (
                source_fingerprints if source_hits else None
            )
        if enforce_counts and assertions < protected_count:
            failures.append(
                f"{relative}: {assertions} assertions < protected {baseline} count "
                f"{protected_count}"
            )
        if enforce_counts and enforce_test_floor and tests < protected_tests:
            executed = _executed_test_count(relative, executed_tests)
            if executed >= protected_tests:
                notes.append(
                    f"{relative}: {tests} static test declarations below the "
                    f"protected {baseline} count {protected_tests}, but the "
                    f"evaluator run registered {executed} executed cases"
                )
            else:
                failures.append(
                    f"{relative}: {tests} test declarations < protected {baseline} count "
                    f"{protected_tests}"
                )
        if enforce_counts and isinstance(protected_fingerprints, Mapping):
            missing_assertions: list[str] = []
            for fingerprint, required in protected_fingerprints.items():
                if not isinstance(required, int) or isinstance(required, bool):
                    continue
                fingerprint_text = str(fingerprint)
                normalized = _assertion_fingerprints(
                    fingerprint_text, primitive_bindings
                )
                if len(normalized) == 1:
                    fingerprint_text = next(iter(normalized))
                timing = _timing_assertion(fingerprint_text)
                if timing is None:
                    missing = required - observed_exact.get(fingerprint_text, 0)
                else:
                    key, baseline_threshold = timing
                    maximum = _MAX_TIMING_THRESHOLD_MS
                    candidates = observed_timing.get(key, [])
                    matched = sum(
                        count
                        for threshold, count in candidates
                        if 1 <= threshold <= maximum
                    )
                    missing = required - matched
                    recalibrated = sorted(
                        {
                            threshold
                            for threshold, _ in candidates
                            if 1 <= threshold <= maximum
                            and threshold != baseline_threshold
                        }
                    )
                    if matched and recalibrated:
                        notes.append(
                            f"{relative}: wall-clock assertion retained with "
                            f"host-calibrated bound {baseline_threshold} -> "
                            f"{recalibrated}ms (host-jitter envelope "
                            f"{maximum}ms): {fingerprint_text[:160]}"
                        )
                if missing > 0:
                    missing_assertions.append(
                        f"{fingerprint} (missing {missing})"
                    )
            if missing_assertions:
                summary = "; ".join(
                    item[:240] for item in missing_assertions[:8]
                )
                if len(missing_assertions) > 8:
                    summary += (
                        f"; ... ({len(missing_assertions) - 8} more missing "
                        "fingerprints)"
                    )
                failures.append(
                    f"{relative}: protected assertion content was deleted or "
                    f"weakened ({len(missing_assertions)} fingerprint(s)): "
                    f"{summary}"
                )
        tricky = _trivial_hits(executable)
        if tricky:
            failures.append(
                f"{relative}: trivial or empty test construct(s): {tricky[0][:160]}"
            )
        control_flow = _control_flow_hits(executable)
        if control_flow:
            failures.append(
                f"{relative}: test callback control flow bypasses assertions: "
                f"{control_flow[0][:200]}"
            )
        shadowed = _assertion_shadow_hits(executable)
        if shadowed:
            failures.append(
                f"{relative}: assertion helper was redefined or aliased: {shadowed[0][:160]}"
            )
        comment_only = {
            str(token) for token in record.get("comment_only_tokens", [])
        }
        executable_canonical = _canonical(executable)
        executable_string_units = {
            units
            for kind, value, _, _ in _tokenize(executable)
            if kind == "string"
            for units in [_string_literal_units(value)]
            if units is not None
        }
        missing_tokens: list[str] = []
        for token in record.get("required_literal_tokens", []):
            needle = _canonical(str(token))
            if not needle:
                continue
            found = False
            start = canonical.find(needle)
            while start != -1:
                end = start + len(needle)
                # A token that only survives inside a comment was removed from
                # the executable test surface even if its text is still in the
                # file.  Require a substantial executable portion.
                executable_chars = sum(
                    1 for in_comment in comment_mask[start:end] if not in_comment
                )
                # Literals that were part of a comment in the protected source
                # do not prove executable coverage; every other literal must
                # survive in executable code, not merely in a comment.
                if (
                    str(token) in comment_only
                    or executable_chars >= 1
                ):
                    found = True
                    break
                start = canonical.find(needle, start + 1)
            if not found:
                literal_units = _string_literal_units(str(token))
                if (
                    literal_units is not None
                    and literal_units in executable_string_units
                ):
                    found = True
            if not found:
                # A window can span several protected assertions.  Match its
                # individual assertion/helper calls and string literals so a
                # legitimate new assertion inserted between them does not make
                # the whole window disappear.  Every atom must still appear in
                # executable (non-comment) code.
                atoms = _protected_token_atoms(needle)
                if atoms and all(
                    atom in executable_canonical for atom in atoms
                ):
                    found = True
                if not found:
                    # A window that spans several operations-map members may
                    # gain a member (for example a new ``isBuffer`` hook).
                    # Require every remaining segment verbatim and in order so
                    # additions are accepted while deletions and rewrites are
                    # still rejected.
                    segments = _protected_token_segments(needle)
                    if segments:
                        position = 0
                        for segment in segments:
                            offset = executable_canonical.find(
                                segment, position
                            )
                            if offset == -1:
                                break
                            position = offset + len(segment)
                        else:
                            found = True
            if not found:
                missing_tokens.append(str(token))
        if missing_tokens:
            failures.append(
                f"{relative}: retained scenario literal missing: {missing_tokens[0]}"
            )
        notes.append(
            f"{relative}: assertions {assertions}/{protected_count} ({baseline}), "
            f"tests {tests}/{protected_tests}, "
            f"cases {len(record.get('cases', []))}"
        )
    return failures, notes


def executable_javascript(text: str) -> str:
    """Return JavaScript executable token text without inert literals.

    The protected-test marker scan must not treat a comment or a string as
    evidence that the candidate forged a verdict.  The tokenizer already skips
    comments; drop string, template and regular-expression tokens as well so
    only code identifiers and punctuation remain searchable.
    """

    return "".join(
        value
        for kind, value, _, _ in _tokenize(text)
        if kind in {"word", "number", "punct"}
    )


def _test_results(path: Path) -> list[Mapping[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("test execution report must be an object")
    results = payload.get("testResults")
    if not isinstance(results, list):
        raise ValueError("test execution report must contain testResults")
    rows = [row for row in results if isinstance(row, Mapping)]
    if not rows:
        raise ValueError("test execution report must contain at least one test result")
    return rows


def registered_test_counts(report: Path | None) -> dict[str, int]:
    """Return the number of cases each test file registered in the report."""

    if report is None:
        return {}
    try:
        rows = _test_results(report)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row.get("name", "")).replace("\\", "/")
        assertions = row.get("assertionResults")
        if not isinstance(assertions, list):
            continue
        counts[name] = max(counts.get(name, 0), len(assertions))
    return counts


def execution_report_failure(report: Path | None) -> str | None:
    """Classify a missing or unreadable execution report as ordinary evidence.

    A report that does not exist, cannot be decoded, or has no usable
    ``testResults`` container proves only that the evaluator's test run did not
    produce trustworthy execution evidence.  It is not evidence that the
    candidate modified a protected test.  A well-formed report is still checked
    strictly by :func:`check_executed_cases`.
    """

    if report is None:
        return "protected test execution report is missing"
    try:
        _test_results(report)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return f"protected test execution report is invalid: {exc}"
    return None


def check_executed_cases(
    equivalence: Mapping[str, Any],
    *,
    report: Path | None,
    baseline_modes: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Verify every mapped source case was actually registered and passed.

    The report is produced by an evaluator-controlled Vitest invocation.  A
    title that never ran cannot count as an equivalent conversion, even when
    the file text still contains the original assertions.
    """

    failures: list[str] = []
    notes: list[str] = []
    report_failure = execution_report_failure(report)
    if report_failure is not None:
        return [report_failure], notes
    assert report is not None
    results = _test_results(report)
    # Multiple source cases may legitimately map to the same converted title
    # (for example an alias case and the canonical case).  Keep every result
    # rather than overwriting duplicates: the title only proves identity, the
    # assertion results prove that the protected assertions actually ran.
    executed: dict[str, dict[tuple[tuple[str, ...], str], list[dict[str, Any]]]] = {}
    files_without_results: set[str] = set()
    for row in results:
        name = str(row.get("name", "")).replace("\\", "/")
        assertions = row.get("assertionResults")
        if not isinstance(assertions, list):
            files_without_results.add(name)
            continue
        for assertion in assertions:
            if not isinstance(assertion, Mapping):
                continue
            suite = assertion.get("ancestorTitles")
            suite = tuple(str(item) for item in suite) if isinstance(suite, list) else ()
            title = str(assertion.get("title", ""))
            status = str(assertion.get("status", "unknown"))
            count = assertion.get("assertionCount")
            executed.setdefault(name, {}).setdefault((suite, title), []).append(
                {"status": status, "assertionCount": count}
            )
    matched = 0
    for relative, record in sorted(equivalence.items()):
        if not isinstance(record, Mapping):
            continue
        cases = record.get("cases")
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, Mapping):
                continue
            target_file = str(case.get("target_file", ""))
            stable_id = str(case.get("stable_id", case.get("name", "")))
            candidates = [
                name
                for name in executed
                if name == target_file or name.endswith("/" + target_file)
            ]
            if not candidates:
                if any(
                    name == target_file
                    or name.endswith("/" + target_file)
                    for name in files_without_results
                ):
                    failures.append(
                        f"{relative}: mapped target file has no assertionResults: "
                        f"{target_file}"
                    )
                    continue
                failures.append(
                    f"{relative}: mapped target file did not run: {target_file}"
                )
                continue
            available: dict[
                tuple[tuple[str, ...], str], list[dict[str, Any]]
            ] = {}
            for name in candidates:
                for key, rows in executed[name].items():
                    available.setdefault(key, []).extend(rows)
            source_mode = (
                isinstance(baseline_modes, Mapping)
                and baseline_modes.get(relative) == "source"
            )
            for raw_title in case.get("target_titles", []):
                remaining = [str(item) for item in raw_title]
                key = (
                    tuple(str(suite) for suite in remaining[:-1]),
                    remaining[-1],
                )
                source_suite = case.get("suite")
                source_name = case.get("name")
                source_title = (
                    " > ".join(
                        [str(item) for item in source_suite] + [str(source_name)]
                    )
                    if isinstance(source_suite, list) and isinstance(source_name, str)
                    else ""
                )
                source_title_colon = (
                    ": ".join(
                        [" > ".join(str(item) for item in source_suite), str(source_name)]
                    )
                    if isinstance(source_suite, list) and isinstance(source_name, str)
                    else ""
                )
                source_title_chevron = (
                    " › ".join(
                        [str(item) for item in source_suite] + [str(source_name)]
                    )
                    if isinstance(source_suite, list) and isinstance(source_name, str)
                    else ""
                )
                source_tuple = (
                    (
                        tuple(str(item) for item in source_suite),
                        str(source_name),
                    )
                    if isinstance(source_suite, list) and isinstance(source_name, str)
                    else None
                )
                source_key = None
                if key not in available and source_mode:
                    # A source-era harness may report the original suite/name
                    # tuple, a bare top-level name, or flatten it into a single
                    # Vitest title.  The target-era suite remains the primary
                    # identity.  Only the source baseline may use these
                    # fallbacks: a target-era file that renames its tests to
                    # source titles must still prove the target suite ran.
                    for candidate_key in (
                        source_tuple,
                        ((), str(source_name)) if isinstance(source_name, str) else None,
                        ((), source_title),
                        ((), source_title_colon),
                        ((), source_title_chevron),
                    ):
                        if candidate_key is not None and candidate_key in available:
                            key = candidate_key
                            source_key = candidate_key
                            break
                if key not in available:
                    failures.append(
                        f"{relative}: protected case {stable_id} did not execute "
                        f"target {' > '.join(remaining)}"
                    )
                    continue
                observations = available[key]
                statuses = [
                    str(row.get("status", "unknown")) for row in observations
                ]
                if any(status != "passed" for status in statuses):
                    failures.append(
                        f"{relative}: protected case {stable_id} target "
                        f"{' > '.join(remaining)} finished "
                        f"{next(status for status in statuses if status != 'passed')}"
                    )
                    continue
                counts: list[int] = []
                invalid_count = False
                for observation in observations:
                    value = observation.get("assertionCount")
                    if (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value < 0
                    ):
                        invalid_count = True
                        break
                    counts.append(value)
                if invalid_count or not counts:
                    failures.append(
                        f"{relative}: protected case {stable_id} has no evaluator "
                        f"assertion count for target {' > '.join(remaining)}"
                    )
                    continue
                required = case.get("assertion_count")
                if source_mode or key == source_key:
                    # Source-era uvu harnesses report the containing test's
                    # executed assertion count, which need not match the
                    # target-era split case count.  The source file-level
                    # fingerprint floor still protects every source assertion;
                    # execution coverage only needs the case to have run.  Some
                    # source cases intentionally execute zero assertions (for
                    # example a smoke test of generated source), so do not
                    # impose the target per-case count here.
                    required = 0
                if not isinstance(required, int) or isinstance(required, bool):
                    required = 0
                if max(counts) < required:
                    failures.append(
                        f"{relative}: protected case {stable_id} executed "
                        f"{max(counts)} assertions, below the protected "
                        f"{required} for target {' > '.join(remaining)}"
                    )
                    continue
                matched += 1
    notes.append(f"mapped target cases executed and passed: {matched}")
    return failures, notes


__all__ = [
    "check_converted_tests",
    "check_executed_cases",
    "executable_javascript",
    "execution_report_failure",
    "load_equivalence",
    "registered_test_counts",
]
