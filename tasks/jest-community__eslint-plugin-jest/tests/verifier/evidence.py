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
    re.compile(r"(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    # ``it.todo(...)`` / ``test.todo(...)`` register a *disabled* (pending) spec:
    # it never executes, so it disables a test just like ``.skip`` and must be
    # counted symmetrically.  A quoted fixture snippet is blanked by
    # ``_strip_comments`` before this runs, so prose is not counted.
    re.compile(r"(?:describe|it|test)\.todo\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE),
    re.compile(r"\b(?:skip|xfail|disabled)\s*=\s*true\b", re.IGNORECASE),
)
# A disabled test can be registered through a computed property instead of the
# literal ``it.skip`` / ``test.only`` form (for example ``it['skip']`` or
# ``test["only"]``).  The property name is a string literal, so a naive
# comment-and-string stripper erases the evidence.  ``_computed_disabled_count``
# instead requires the *receiver* (``it`` / ``describe`` / ``test``) to sit at
# code level while allowing its computed key to be a string; fixture code that
# merely mentions ``it["skip"]`` inside a quoted snippet is ignored.
_RECEIVER_HEAD = re.compile(r"describe|it|test", re.IGNORECASE)
_COMPUTED_KEY = re.compile(r"\s*\[\s*['\"`](?:skip|only|todo)['\"`]\s*\]", re.IGNORECASE)
# Mocha disables a suite/spec with x-prefixed aliases (``xdescribe``,
# ``xcontext``, ``xit``, ``xspecify``).  They are semantically identical to
# ``.skip`` and a migration translates them one-for-one, so they must be counted
# symmetrically; otherwise the mechanical ``xdescribe -> describe.skip`` rewrite
# looks like a newly introduced skip.  Only invocations count: a compatibility
# shim that *defines* ``globals.xit = it.skip`` (or references ``xit``) is not a
# disabled test.
_DISABLED_ALIAS = re.compile(r"\bx(?:describe|context|it|specify)\s*\(", re.IGNORECASE)
# A skipped/only spec can also be registered without calling the receiver
# directly: the candidate may alias or indirect the property (``const h =
# (it as any).skip; h(...)``), index it dynamically (``it[variable]``) or take a
# wrapped-member path (``(it as any)['skip']``).  These forms never appear as the
# literal ``it.skip(`` token, so the literal markers above miss them.  Matching
# runs on comment/string-stripped source and only recognises program-level
# accesses, so prose inside a comment or fixture string is not counted.
# ``.todo`` is included because a pending spec is a disabled spec.
_ALIASED_DISABLED = re.compile(r"\.(?:skip|only|xfail|todo)\b", re.IGNORECASE)
_RECEIVER_INDEX = re.compile(
    r"(?<![A-Za-z0-9_$])(?:describe|it|test)\s*\[", re.IGNORECASE
)
# Identifier immediately followed by an index bracket, used by the alias-aware
# computed-key scan (``spec['skip']`` after ``const spec = it``).
_RECEIVER_IDENT = re.compile(r"([A-Za-z_$][\w$]*)\s*\[")
_WRAPPED_INDEX = re.compile(r"\bas\s+any\s*\)\s*\[", re.IGNORECASE)
# A migration can keep a reference strong assertion "in place" but make it
# unreachable by guarding it with a *literal* falsy condition.  The text still
# contains the assertion, so a pure presence check is fooled; the assertion is
# dead code.  The detector covers the whole family of literal-false guard
# constructs -- the parenthesised statement conditions (``if`` / ``while`` /
# ``switch`` / ``for``) plus the ``&&`` short-circuit and ``?:`` true-branch --
# so re-spelling the literal or swapping the guarding statement does not evade
# it.  A bare parenthesised condition can only be a guard (comparing is written
# ``x === false``), while the operator forms are restricted to ``false`` /
# ``!true`` so an ordinary ``x === 0 && ...`` comparison is never mistaken for a
# dead guard.  Legitimate test code does not guard assertions with literal
# constants, so the detector stays free of false positives.
_FALSY_LITERAL = r"(?:false|!\s*true|!\s*\d+|0n|0|''|\"\"|null|undefined|void\s+0)"
_DEAD_GUARD = re.compile(
    r"(?:\bif\s*\(\s*" + _FALSY_LITERAL + r"\s*\)"
    r"|\bwhile\s*\(\s*" + _FALSY_LITERAL + r"\s*\)"
    r"|\bswitch\s*\(\s*" + _FALSY_LITERAL + r"\s*\)"
    r"|\bfor\s*\([^;)]*;\s*" + _FALSY_LITERAL + r"\s*;[^)]*\)"
    r"|(?:false|!\s*true)\s*&&"
    r"|(?:false|!\s*true)\s*\?)",
    re.IGNORECASE,
)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
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


_LINE_COMMENT_PREFIXES = ("//", "/*", "*", "*/")


def _significant_line_hashes(text: str) -> list[str]:
    """Return the sorted digests of a file's non-trivial source lines.

    Blank lines and lines that only carry a comment are dropped; every other
    line is stripped and hashed.  Two files with the same digest set carry the
    same executable statements regardless of blank-line or comment churn, which
    lets an audit distinguish an equivalent adaptation from a deletion-only
    weakening without embedding the reference source itself.
    """

    digests = {
        "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest()
        for raw in text.splitlines()
        if (line := raw.strip())
        and not line.startswith(_LINE_COMMENT_PREFIXES)
    }
    return sorted(digests)


# Matchers that actually compare a subject against an expected value.  A
# candidate that keeps the subject but replaces one of these with an
# existence-only matcher (``toBeDefined`` / ``toBeTruthy`` ...) has removed a
# behavioural assertion even though it added a line, which the line-subset
# comparison alone cannot see.
_STRONG_ASSERTION_MATCHERS = frozenset(
    {
        "tobe",
        "tobecloseto",
        "tocontain",
        "tocontainequal",
        "toequal",
        "tobegreaterthan",
        "tobegreaterthanorequal",
        "tobeinstanceof",
        "tobelessthan",
        "tobelessthanorequal",
        "tomatchinlinesnapshot",
        "tomatchobject",
        "tomatchsnapshot",
        "tostrictequal",
        "tohavelength",
        "tothrow",
        "tothrowerror",
    }
)
# Structural matchers whose exact operand/subject legitimately varies under an
# equivalent adaptation (a candidate may snapshot a normalised view of the
# configs).  Their obligation is covered by the snapshot file and the behaviour
# probes, so the source-preservation check does not require the same subject.
_STRUCTURAL_ASSERTION_MATCHERS = frozenset({"tomatchsnapshot", "tomatchinlinesnapshot"})
# Equality-class matchers compare the subject against a fully concrete expected
# value.  Such a comparison subsumes every weaker strong matcher on the same
# subject (``toHaveLength(n)`` / ``toContain(x)`` / ``toBe(v)``), so a candidate
# that *strengthens* a source assertion into an exact equality must not be read
# as dropping it.  A wildcard operand (``expect.any(...)`` and friends) is not a
# concrete value and is never treated as a strengthening.
_EQUALITY_ASSERTION_MATCHERS = frozenset(
    {"toequal", "tostrictequal", "tomatchobject"}
)
_WILDCARD_ARGUMENT = re.compile(
    r"expect\s*\.\s*"
    r"(?:any|anything|stringcontaining|stringmatching|arraycontaining"
    r"|objectcontaining|closeto)\s*\(",
    re.IGNORECASE,
)
_EXPECT_CALL = re.compile(r"\bexpect\s*\(", re.IGNORECASE)


def _assertion_code_mask(text: str) -> bytearray:
    """Per-offset classification: 0=comment, 1=string/template, 2=code.

    Assertion extraction must only accept ``expect(...)`` calls that are live
    code.  A candidate can otherwise keep a reference strong assertion from
    counting by parking it inside a ``//``/``/* */`` comment or a string literal
    while deleting the behavioural comparison.
    """

    mask = bytearray(len(text))
    length = len(text)
    state = "code"
    quote = ""
    index = 0
    while index < length:
        character = text[index]
        if state == "code":
            if character == "/" and index + 1 < length and text[index + 1] == "/":
                mask[index] = 0
                mask[index + 1] = 0
                index += 2
                while index < length and text[index] != "\n":
                    mask[index] = 0
                    index += 1
                continue
            if character == "/" and index + 1 < length and text[index + 1] == "*":
                mask[index] = 0
                mask[index + 1] = 0
                index += 2
                while index < length and not (
                    text[index] == "*"
                    and index + 1 < length
                    and text[index + 1] == "/"
                ):
                    mask[index] = 2 if text[index] == "\n" else 0
                    index += 1
                if index < length:
                    mask[index] = 0
                    index += 1
                if index < length:
                    mask[index] = 0
                    index += 1
                continue
            if character in ("'", '"', "`"):
                mask[index] = 1
                state = "string"
                quote = character
                index += 1
                continue
            mask[index] = 2
            index += 1
            continue
        if character == "\\":
            mask[index] = 1
            index += 1
            if index < length:
                mask[index] = 1
                index += 1
            continue
        mask[index] = 1
        if character == quote:
            state = "code"
        index += 1
    return mask


def _canon_subject(subject: str) -> str:
    """Canonicalise an assertion subject against common equivalent churn.

    Legitimate migrations rename local variables, add TypeScript non-null
    assertions (``x!``), optional chaining (``x?.y``) and nullish fallbacks
    (``x ?? {}``).  Those are equivalent test adaptations, not weakenings, so the
    subject comparison ignores them.  A trailing comma (from prettier wrapping a
    long ``expect(`` argument over multiple lines) carries no meaning and is
    dropped too, otherwise the same subject compares unequal just because it was
    reflowed.  An order-normalising ``.sort()`` before a set comparison
    (``Object.keys(x).sort()`` == ``Object.keys(x)`` for preservation purposes)
    is folded away as well: sorting reorders the compared elements but neither
    adds nor drops any, so it is the canonical way to strengthen an
    order-insensitive expectation into an exact set comparison.
    """

    text = re.sub(r"\s+", "", subject)
    text = text.replace("?.", ".")
    text = text.replace("!", "")
    text = text.replace("??{}", "").replace("??[]", "")
    text = re.sub(r"\.sort\(\s*\)", "", text)  # order-normalising copy
    text = re.sub(r",(?=[\]\}])", "", text)  # drop trailing commas
    return text[:-1] if text.endswith(",") else text


def _canon_argument(argument: str) -> str:
    """Canonicalise a matcher argument for value-preservation comparison."""

    text = re.sub(r"\s+", "", argument)
    text = text.replace('"', "'")
    text = re.sub(r",(?=[\]\}])", "", text)  # drop trailing commas
    return text[:-1] if text.endswith(",") else text


# A candidate legitimately hoists an inline expected value into a local constant
# (``toEqual([...names])`` -> ``const configNames = [...names]; ...toEqual(configNames)``)
# and inlines it back.  Both spellings denote the same compared value, so the
# argument-preservation check must resolve local bindings before comparing;
# otherwise an equivalent adaptation is misread as a dropped value comparison.
# The binding family also covers a renamed holder reachable through object
# destructuring (``const { configs } = plugin`` == ``const configs = plugin.configs``),
# which the reflow below folds back onto the same subject identity.
_BINDING_DECL = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*")
_OBJECT_DESTRUCTURE = re.compile(r"\b(?:const|let|var)\s*\{\s*([^{}]*?)\s*\}\s*=\s*")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_TRAILING_AS_CONST = re.compile(r"(?:asconst|asreadonly)$")


def _scan_expression(text: str, start: int) -> str:
    """Return the initialiser expression beginning at ``start``.

    The scan tracks bracket depth and skips quoted strings so a multi-line
    array/object/call argument is captured whole, while a statement terminates
    at the first depth-zero ``;`` or newline.
    """

    depth = 0
    index = start
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
        if character in "([{":
            depth += 1
        elif character in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif character == ";" and depth == 0:
            break
        elif character == "\n" and depth == 0:
            break
        index += 1
    return text[start:index]


def _local_bindings(text: str) -> dict[str, str]:
    """Map locally declared ``const``/``let``/``var`` names to their initialiser.

    Only code-level declarations count (a ``const`` inside a comment or string
    literal is not a binding).  Both a plain ``const x = expr`` and an object
    destructuring ``const { a, b: c } = expr`` are resolved (the latter to
    ``expr.a`` / ``expr.b``) so a legitimate rename through destructuring is
    recognised as the same value.  Nested patterns are skipped rather than
    guessed.
    """

    bindings: dict[str, str] = {}
    mask = _assertion_code_mask(text)
    for match in _BINDING_DECL.finditer(text):
        if mask[match.start()] != 2:
            continue
        name = match.group(1)
        expression = _scan_expression(text, match.end()).strip()
        if expression:
            bindings[name] = expression
    for match in _OBJECT_DESTRUCTURE.finditer(text):
        if mask[match.start()] != 2:
            continue
        expression = _scan_expression(text, match.end()).strip()
        if not expression:
            continue
        for entry in match.group(1).split(","):
            entry = entry.strip()
            if not entry or "=" in entry:
                continue
            if ":" in entry:
                key, _, alias = entry.partition(":")
                key, alias = key.strip(), alias.strip()
            else:
                key = alias = entry
            if re.fullmatch(r"[A-Za-z_$][\w$]*", key) and re.fullmatch(
                r"[A-Za-z_$][\w$]*", alias
            ):
                bindings[alias] = f"{expression}.{key}"
    return bindings


def _resolve_expression(
    expression: str,
    bindings: Mapping[str, str],
    depth: int = 0,
) -> str:
    """Substitute code-level local identifiers with their declared initialiser.

    Property names (``a.b``) are never substituted, so ``Object.keys(x)`` keeps
    its shape and only the bound value of ``x`` is expanded.  The depth bound
    stops a cyclic binding (``a = b; b = a``) from recursing forever.
    """

    if depth > 6 or not expression:
        return expression
    mask = _assertion_code_mask(expression)
    pieces: list[str] = []
    last = 0
    for match in _IDENTIFIER.finditer(expression):
        start = match.start()
        if mask[start] != 2:
            continue
        if start > 0:
            preceding = expression[start - 1]
            if preceding == "." and expression[max(0, start - 3):start] != "...":
                # Property name (``a.b``): keep it verbatim.  A spread operand
                # (``...b``) is not a property access and must resolve.
                continue
            if preceding == "$" or preceding == "_" or preceding.isalnum():
                # Tail of a larger identifier token: not a standalone name.
                continue
        name = match.group(0)
        if name not in bindings:
            continue
        pieces.append(expression[last:start])
        pieces.append(_resolve_expression(bindings[name], bindings, depth + 1))
        last = match.end()
    pieces.append(expression[last:])
    return "".join(pieces)


def _subject_signature(subject: str, bindings: Mapping[str, str]) -> str:
    """Canonical subject identity with local variable renaming resolved away."""

    bindings = {name: _strip_value_copies(value) for name, value in bindings.items()}
    canonical = _canon_subject(_resolve_expression(subject, bindings))
    return _TRAILING_AS_CONST.sub("", canonical)


def _argument_signature(argument: str, bindings: Mapping[str, str]) -> str:
    """Canonical expected value with local bindings inlined.

    ``as const`` / ``as readonly`` type-only wrappers are dropped: they constrain
    the TypeScript type but leave the compared value unchanged.  Equal-value copy
    wrappers (``[...x]`` / ``{...x}`` / ``x.slice()``) are folded to their core
    value both before and after binding resolution, so a legitimate
    ``toEqual([...configNames])`` (== ``toEqual(configNames)``) rewrite is not
    misread as a dropped value comparison.
    """

    bindings = {name: _strip_value_copies(value) for name, value in bindings.items()}
    resolved = _resolve_expression(_strip_value_copies(argument), bindings)
    canonical = _strip_value_copies(_canon_argument(resolved))
    return _TRAILING_AS_CONST.sub("", canonical)


_COPY_MEMBER_CALL = re.compile(r"^(.*)\.(?:slice|concat)\(\s*\)$")


def _top_level_commas(text: str) -> int:
    """Count commas not nested inside (), [] , {} or a quoted string."""

    depth = 0
    quote = ""
    count = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
        index += 1
    return count


def _strip_value_copies(expression: str) -> str:
    """Fold equal-value copy wrappers down to the value they copy.

    ``x``, ``[...x]``, ``{...x}``, ``x.slice()`` and ``x.concat()`` all denote an
    equal value for preservation purposes; a candidate may legitimately respell
    one as the other.  Type-only ``as const`` / ``as readonly`` wrappers are
    dropped first so a hoisted literal stays comparable.  Only a *sole* spread or
    an argument-less member copy is folded: ``[a, ...b]`` (which drops or reorders
    elements) is left intact, so a genuine value change is still visible.
    """

    text = expression.replace("asconst", "").replace("asreadonly", "")
    for _ in range(8):
        stripped = text.strip()
        if not stripped:
            break
        inner: str | None = None
        if stripped.startswith("[") and stripped.endswith("]"):
            inner = stripped[1:-1].strip()
        elif stripped.startswith("{") and stripped.endswith("}"):
            inner = stripped[1:-1].strip()
        if (
            inner is not None
            and inner.startswith("...")
            and _top_level_commas(inner[3:]) == 0
        ):
            text = inner[3:].strip()
            continue
        match = _COPY_MEMBER_CALL.match(stripped)
        if match:
            text = match.group(1).strip()
            continue
        break
    return text


def _copy_core(expression: str) -> str:
    """Strip array/object-copy wrappers to expose a value's core expression.

    ``x``, ``[...x]``, ``{...x}``, ``x.slice()`` and ``x.concat()`` all denote the
    same value for the purpose of spotting a vacuous self-comparison
    (``expect(x).toEqual([...x])`` or ``expect(x.slice()).toEqual(x)``).  Type-only
    ``as const`` wrappers are dropped first so a hoisted literal stays comparable.
    """

    return _strip_value_copies(expression)


def _matching_paren(text: str, open_index: int) -> int:
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
    return -1


def _assertion_spans(text: str) -> list[tuple[int, str, str, str]]:
    """Extract ``(offset, subject, matcher, argument)`` from live assertions.

    Only assertions that are live code count: an ``expect`` parked inside a
    comment or a string literal is not a behavioural assertion.  ``.not`` chains
    are skipped so the underlying matcher is read.  ``offset`` is the index of the
    ``expect`` token, used to look up the assertion's guard reachability.
    """

    mask = _assertion_code_mask(text)
    records: list[tuple[int, str, str, str]] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        match = _EXPECT_CALL.search(text, cursor)
        if match is None:
            break
        if mask[match.start()] != 2:
            # The ``expect`` token itself is inside a comment or a string
            # literal: not a live assertion.
            cursor = match.end()
            continue
        open_index = match.end() - 1
        index = _matching_paren(text, open_index)
        if index < 0:
            break
        subject = re.sub(r"\s+", "", text[open_index + 1:index])
        tail = text[index + 1:index + 81]
        method = re.match(
            r"\s*(?:\.\s*not\s*)?\.\s*([A-Za-z_$][\w$]*)\s*\(", tail
        )
        if method is None:
            cursor = index + 1
            continue
        argument_open = index + method.end()
        argument_close = _matching_paren(text, argument_open)
        if argument_close < 0:
            cursor = index + 1
            continue
        argument = text[argument_open + 1:argument_close]
        records.append(
            (match.start(), subject, method.group(1).casefold(), argument)
        )
        cursor = argument_close + 1
    return records


def _assertion_records(text: str) -> list[tuple[str, str, str]]:
    """Extract ``(subject, matcher, argument)`` triples from live assertions."""

    return [(subject, matcher, argument) for _, subject, matcher, argument in _assertion_spans(text)]


def _assertion_signatures(text: str) -> list[tuple[str, str]]:
    """Extract ``(subject, matcher)`` pairs from ``expect(...).matcher(...)``."""

    return [(subject, matcher) for subject, matcher, _ in _assertion_records(text)]


def _assertion_map(text: str) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for subject, matcher in _assertion_signatures(text):
        mapping.setdefault(subject, set()).add(matcher)
    return mapping


def _assertion_replaced_by_weaker(
    reference: Mapping[str, Any] | None,
    candidate_text: str,
) -> bool:
    """True when a reference strong assertion is kept but weakened in place."""

    if not isinstance(reference, Sequence) or isinstance(reference, (str, bytes)):
        return False
    reference_map: dict[str, set[str]] = {}
    for item in reference:
        subject: object = None
        matcher: object = None
        if isinstance(item, Mapping):
            subject = item.get("subject")
            matcher = item.get("matcher")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            subject, matcher = item
        if not isinstance(subject, str) or not isinstance(matcher, str):
            continue
        reference_map.setdefault(subject, set()).add(matcher.casefold())
    if not reference_map:
        return False
    candidate_map = _assertion_map(candidate_text)
    for subject, matchers in reference_map.items():
        if not (matchers & _STRONG_ASSERTION_MATCHERS):
            continue
        present = candidate_map.get(subject)
        if present is None:
            # The subject was dropped entirely; the line-subset comparison owns
            # pure deletion and reports it separately.
            continue
        if not (present & _STRONG_ASSERTION_MATCHERS):
            return True
    return False


def _disabled_receiver_aliases(text: str) -> set[str]:
    """Code-level names bound to a test receiver (``it`` / ``test`` / ``describe``).

    A candidate can dodge the literal ``it['skip']`` scan by aliasing the
    receiver first (``const spec = it; spec['skip'](...)``).  Resolving local
    ``const``/``let``/``var`` bindings (including chained aliases) to their core
    receiver lets the computed-key scan recognise ``spec['skip']`` too, instead
    of enumerating receiver spellings.
    """

    aliases: set[str] = set()
    bindings = _local_bindings(text)
    for name, expression in bindings.items():
        resolved = re.sub(r"\s+", "", _resolve_expression(expression, bindings))
        if resolved in {"describe", "it", "test"}:
            aliases.add(name)
    return aliases


def _computed_disabled_count(text: str) -> int:
    """Count code-level computed ``.skip`` / ``.only`` / ``.todo`` registrations.

    The receiver may be a literal ``it`` / ``test`` / ``describe`` or a
    code-level alias of one, so a ``const spec = it; spec['skip'](...)`` rewrite
    is counted like ``it['skip'](...)``.
    """

    stripped = _strip_comments(text)
    aliases = _disabled_receiver_aliases(text)
    count = 0
    for match in _RECEIVER_IDENT.finditer(text):
        name = match.group(1)
        if name not in aliases and name.lower() not in {"describe", "it", "test"}:
            continue
        start, end = match.start(1), match.end(1)
        if stripped[start:end] != text[start:end]:
            # The receiver sits inside a string or comment: not a real skip.
            continue
        if _COMPUTED_KEY.match(text[end:end + 24]):
            count += 1
    return count


def _aliased_disabled_count(text: str) -> int:
    """Count aliased / indexed / wrapped disabled-test registrations.

    These are the program-level forms that a literal ``it.skip(`` scan cannot
    see: ``(it as any).skip``, ``it[variable]``, ``(it as any)['skip']`` and
    ``xdescribe``-style aliases.  The match runs on comment/string-stripped text
    so a quoted snippet such as ``"it.skip(...)"`` is not treated as a real
    registration.
    """

    stripped = _strip_comments(text)
    return (
        len(_ALIASED_DISABLED.findall(stripped))
        + len(_RECEIVER_INDEX.findall(stripped))
        + len(_WRAPPED_INDEX.findall(stripped))
    )


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
        counts["disabled"] += sum(
            1
            for pattern in (*_SKIP_MARKERS[2:], _DISABLED_ALIAS)
            for _ in pattern.finditer(text)
        )
        counts["disabled"] += _computed_disabled_count(raw)
        counts["disabled"] += _aliased_disabled_count(raw)
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


def _matching_open_paren(text: str, close_index: int) -> int:
    depth = 0
    index = close_index
    while index >= 0:
        character = text[index]
        if character == ")":
            depth += 1
        elif character == "(":
            depth -= 1
            if depth == 0:
                return index
        index -= 1
    return -1


# Keywords whose parenthesised head controls a *conditional or loop* block: an
# ``expect`` inside such a block may never execute.  ``try`` / ``finally`` blocks
# always execute and are intentionally not listed; ``else`` / ``do`` / ``catch``
# are handled by their own branch below.
_GUARD_BLOCK_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch"})
# A statement that *leads* (before the expect on the same statement) with one of
# these guards is conditional: ``if (c) expect`` / ``c && expect`` / ``c ? expect``
# / ``case x: expect``.  ``?`` excludes optional chaining (``?.``), nullish
# (``??``) and assignment (``?=``); ``&&`` / ``||`` are the short-circuit forms.
_LEADER_GUARD = re.compile(
    r"\b(?:if|for|while|switch|case|default|catch|else|do)\b"
    r"|&&|\|\|"
    r"|(?<![?.])\?(?![.?=])"
)


def _block_guard_own(text: str, open_index: int) -> bool:
    """Whether the ``{`` at ``open_index`` opens a conditional/loop/catch block."""

    index = open_index - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    if index < 0:
        return False
    if text[index] == ">":
        return False  # arrow function body: ``() => {``
    if text[index] == ")":
        head = _matching_open_paren(text, index)
        if head < 0:
            return False
        match = re.search(r"([A-Za-z_$][\w$]*)\s*$", text[:head])
        return bool(match) and match.group(1).casefold() in _GUARD_BLOCK_KEYWORDS
    match = re.search(r"([A-Za-z_$][\w$]*)\s*$", text[: index + 1])
    if match:
        word = match.group(1).casefold()
        if word in {"else", "do", "catch"}:
            return True
        if word in {"try", "finally"}:
            return False
    if text[index] == ":":
        # A ``case X: {`` / ``default: {`` label block only runs for that case.
        boundary = max(
            text.rfind(";", 0, index),
            text.rfind("{", 0, index),
            text.rfind("}", 0, index),
        )
        return bool(re.search(r"\b(?:case|default)\b", text[boundary + 1:index]))
    return False


def _statement_leader_guard(text: str, mask: bytearray, position: int) -> bool:
    """Whether the statement leading up to ``position`` opens a guard."""

    index = position - 1
    while index >= 0:
        if mask[index] == 2 and text[index] in ";{}":
            break
        index -= 1
    leader = "".join(
        character if mask[offset] == 2 else " "
        for offset, character in enumerate(text[index + 1:position], start=index + 1)
    )
    return bool(_LEADER_GUARD.search(leader))


def _guarded_expect_offsets(text: str) -> set[int]:
    """Offsets of ``expect(...)`` calls that sit inside a conditional/loop guard.

    A source comparison must survive *reachable*: wrapping it in a guard the
    source did not have (``if (process.env.X === 'enabled') { … }``, ``c && …``,
    ``c ? … : _``, a ``case`` branch) turns a behavioural assertion into dead or
    conditional code while leaving the literal text in place.  The scan tracks a
    stack of brace blocks (each ``{`` classified by the construct that owns it)
    and, per expect, also inspects the statement leader.  It is applied to both
    the source and the candidate, so an assertion that was *already* inside a
    guard in the source stays acceptable.
    """

    mask = _assertion_code_mask(text)
    brace_guard = [False]
    guarded: set[int] = set()
    index = 0
    length = len(text)
    while index < length:
        if mask[index] != 2:
            index += 1
            continue
        character = text[index]
        if character == "{":
            own = _block_guard_own(text, index)
            brace_guard.append(own or brace_guard[-1])
        elif character == "}":
            if len(brace_guard) > 1:
                brace_guard.pop()
        elif character == "e":
            if _EXPECT_CALL.match(text, index):
                if brace_guard[-1] or _statement_leader_guard(text, mask, index):
                    guarded.add(index)
        index += 1
    return guarded


def _dead_guard_strong_assertion(text: str) -> bool:
    """True when a strong assertion is statically unreachable.

    Detects a literal false guard (``if (false)`` / ``!true`` / ``0``) that
    directly wraps or short-circuits an ``expect(...)`` carrying a comparison
    matcher.  Comments and string literals are blanked first so prose cannot
    trigger the guard.
    """

    code = _strip_comments(text)
    for guard in _DEAD_GUARD.finditer(code):
        segment = code[guard.end(): guard.end() + 240]
        call = _EXPECT_CALL.search(segment)
        if call is None:
            continue
        if "}" in segment[: call.start()]:
            # The guard block closed before this expect: the assertion is not
            # guarded by the dead branch (for example an ``else`` branch).
            continue
        signatures = _assertion_signatures(segment[call.start():])
        if signatures and signatures[0][1] in _STRONG_ASSERTION_MATCHERS:
            return True
    return False


def _source_strong_assertions_dropped(
    source_root: Path | None,
    relative: str,
    candidate_text: str,
) -> bool:
    """True when the candidate drops a *source* strong assertion.

    ``allowed-test-integrity.json`` carries the reference (target) signatures,
    which include assertions the target solution *adds*.  A candidate is free to
    leave the source tests untouched, so absence of a target-only assertion is
    not a weakening.  The durable obligation (instruction Constraint 10) is that
    the assertions present in the *source* tests survive: a candidate that
    replaces a source comparison with an existence-only assertion -- or rewrites
    the subject so the reference strong comparison disappears, or hides it in a
    comment / string / dead branch -- has removed a behavioural assertion even
    though it added lines.
    """

    if not isinstance(source_root, Path) or not source_root.is_dir():
        return False
    source_path = source_root.joinpath(*relative.split("/"))
    if source_path.is_symlink() or not source_path.is_file():
        return False
    try:
        source_text = source_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if source_text == candidate_text:
        return False
    candidate_bindings = _local_bindings(candidate_text)
    candidate_guarded = _guarded_expect_offsets(candidate_text)
    by_subject: dict[str, set[tuple[str, str]]] = {}
    by_subject_guarded: dict[str, set[tuple[str, str]]] = {}
    relocatable: set[tuple[str, str]] = set()
    for offset, subject, matcher, argument in _assertion_spans(candidate_text):
        subject_sig = _subject_signature(subject, candidate_bindings)
        argument_sig = _argument_signature(argument, candidate_bindings)
        if offset in candidate_guarded:
            # The assertion may never execute; it cannot certify that the source
            # obligation survived.  It is only acceptable when the source
            # assertion was itself guarded (checked below).
            by_subject_guarded.setdefault(subject_sig, set()).add(
                (matcher, argument_sig)
            )
            continue
        by_subject.setdefault(subject_sig, set()).add((matcher, argument_sig))
        # A comparison of a value against a copy of itself is vacuous: it proves
        # nothing and must not be counted as "preserving" a relocated obligation.
        if _copy_core(subject_sig) != _copy_core(argument_sig):
            relocatable.add((matcher, argument_sig))
    source_bindings = _local_bindings(source_text)
    source_guarded = _guarded_expect_offsets(source_text)
    for offset, subject, matcher, argument in _assertion_spans(source_text):
        if matcher not in _STRONG_ASSERTION_MATCHERS:
            continue
        if matcher in _STRUCTURAL_ASSERTION_MATCHERS:
            # Snapshot structure is legitimately reorganised by an equivalent
            # adaptation; it is validated by the snapshot + behaviour probes.
            continue
        # The durable obligation is the *whole* source comparison, not just its
        # shape.  The same (canonical) subject must still carry the same matcher
        # with a semantically retained expected value -- requiring the argument
        # to match closes the "keep the subject but swap in a wider
        # ``expect.any(...)``" weakening.  The surviving assertion must also be
        # *reachable*: a candidate that keeps the text but guards it with a
        # condition the source did not have (``if (env.X) { … }``) has disabled a
        # behavioural assertion.  A comparison legitimately regrouped onto a
        # *different* subject is still accepted when the exact expected value
        # reappears in a non-vacuous, unguarded comparison, so refactors such as
        # ``expect(configs.all.rules).toEqual(configs['flat/all'].rules)`` are
        # not misread as a drop.  Local-binding resolution keeps a legitimate
        # hoist/inline/rename of the value or subject (``toEqual([...])`` <->
        # ``toEqual(constNames)``, destructuring renames) from being a false hit.
        obligation = (matcher, _argument_signature(argument, source_bindings))
        subject_sig = _subject_signature(subject, source_bindings)
        if obligation in by_subject.get(subject_sig, set()):
            continue
        if offset in source_guarded and obligation in by_subject_guarded.get(
            subject_sig, set()
        ):
            continue
        if obligation in relocatable:
            continue
        if matcher == "tohavelength" and _collection_length_strengthened(
            subject_sig, candidate_text, by_subject
        ):
            continue
        # The obligation also survives when the *same* subject is now compared
        # for exact equality against a concrete expected value.  ``toHaveLength``
        # -> ``toEqual(exactList)``, ``toContain(x)`` -> ``toEqual(fullArray)``
        # and ``toBe(v)`` -> ``toEqual(v)`` all assert strictly more than the
        # source did, so an equivalent adaptation that strengthens a comparison
        # must not be misread as a removal.  The relaxation is narrow: the new
        # matcher must be equality-class, its operand must be a concrete value
        # (a wildcard ``expect.any(...)`` proves nothing and stays a drop), and
        # the comparison must be non-vacuous (subject != operand).
        for candidate_matcher, candidate_argument in by_subject.get(subject_sig, set()):
            if candidate_matcher not in _EQUALITY_ASSERTION_MATCHERS:
                continue
            if _WILDCARD_ARGUMENT.search(candidate_argument):
                continue
            if _copy_core(subject_sig) == _copy_core(candidate_argument):
                continue
            break
        else:
            return True
        continue
    if _dead_guard_strong_assertion(candidate_text) and not _dead_guard_strong_assertion(
        source_text
    ):
        return True
    return False


def _collection_length_strengthened(
    source_subject: str,
    candidate_text: str,
    candidate_assertions: Mapping[str, set[tuple[str, str]]],
) -> bool:
    """Recognize an exact collection comparison that strengthens a length check.

    Flat-config migrations often replace ``Object.keys(config.rules).toHaveLength``
    with an equality assertion over either the complete rules object or a small
    helper returning its keys.  Both prove strictly more than the source length
    assertion; changing the subject expression must not turn that strengthening
    into a test-tampering hit.
    """

    match = re.fullmatch(r"Object\.keys\((.+)\)", source_subject)
    if match is None:
        return False
    collection = match.group(1)
    owner = collection[:-len(".rules")] if collection.endswith(".rules") else None
    for candidate_subject, assertions in candidate_assertions.items():
        equivalent_subject = candidate_subject == collection
        if not equivalent_subject and owner is not None:
            # ``_subject_signature`` expands simple local arrow helpers before
            # this check.  A helper such as ``getRuleNames(config) =>
            # Object.keys(config.rules)`` therefore retains its resolved body
            # and appends the canonical config argument.
            equivalent_subject = (
                candidate_subject.endswith(f"({owner})")
                and "Object.keys(" in candidate_subject
                and ".rules)" in candidate_subject
            )
        if not equivalent_subject and owner is not None:
            helper_call = re.fullmatch(
                rf"([A-Za-z_$][\w$]*)\({re.escape(owner)}\)(?:\.sort\(\))?",
                candidate_subject,
            )
            if helper_call is not None:
                helper = re.escape(helper_call.group(1))
                helper_definition = re.search(
                    rf"const\s+{helper}\s*=\s*\(\s*([A-Za-z_$][\w$]*)"
                    rf"(?:\s*:[^)]*)?\)\s*=>\s*Object\.keys\(\s*\1\.rules\b",
                    _strip_comments(candidate_text),
                    re.DOTALL,
                )
                equivalent_subject = helper_definition is not None
        if not equivalent_subject:
            continue
        for matcher, argument in assertions:
            if matcher not in _EQUALITY_ASSERTION_MATCHERS:
                continue
            if _WILDCARD_ARGUMENT.search(argument):
                continue
            if _copy_core(candidate_subject) == _copy_core(argument):
                continue
            return True
    return False


def _weakened_allowed_paths(
    root: Path,
    allowed: Sequence[str] | set[str],
    allowed_integrity: Mapping[str, Any] | None,
    source_root: Path | None = None,
) -> list[str]:
    """Flag allowed-equivalent tests that weaken reference assertions.

    ``allowed_equivalent_test_paths`` lets a candidate adapt the in-scope tests
    to flat config, but instruction Constraint 10 still forbids removing
    behavioural assertions.  Comparing a candidate file's significant lines
    against the reference version embedded in ``allowed-test-integrity.json``
    catches a deletion-only edit (the candidate becomes a strict subset).

    A candidate can also delete a strong comparison and add a weaker assertion
    on the same subject (for example ``toEqual`` -> ``toBeDefined``), which is
    neither a subset nor equal.  The reference assertion signatures in the same
    asset expose that substitution: a subject that carried a comparison matcher
    but now only carries existence-only matchers is treated as weakened.

    The same substitution is used to *hide* the removal: the reference subject
    is rewritten to a different subject and a weaker assertion is added there.
    The reference (target) signatures cannot see that on their own because the
    target solution also adds subjects the source never had.  The source-derived
    obligation closes the gap: a strong assertion that the *source* test carried
    must still be present with a comparison matcher in the candidate.
    """

    if not isinstance(allowed_integrity, Mapping):
        return []
    records = allowed_integrity.get("files")
    if not isinstance(records, Mapping):
        return []
    current = _regular_files(root)
    weakened: list[str] = []
    for relative, record in records.items():
        path = str(relative).replace("\\", "/").strip("/")
        if path not in allowed or path not in current or not isinstance(record, Mapping):
            continue
        target_lines = record.get("target_lines")
        if not isinstance(target_lines, Sequence) or isinstance(target_lines, (str, bytes)):
            continue
        target = {str(item) for item in target_lines}
        if not target:
            continue
        try:
            text = current[path].read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        candidate = set(_significant_line_hashes(text))
        if candidate != target and candidate <= target:
            weakened.append(path)
            continue
        if _assertion_replaced_by_weaker(record.get("assertions"), text):
            weakened.append(path)
            continue
        if _source_strong_assertions_dropped(source_root, path, text):
            weakened.append(path)
    return sorted(weakened)


# Test-runner configuration files decide *which* tests execute.  The
# ``target-native-test`` hard gate trusts the runner exit code, so a candidate
# that narrows the selection (``testMatch`` / ``testRegex`` /
# ``testPathIgnorePatterns`` / ``projects`` / ``runner``) can silence a failing
# test while still returning 0.  The conventional Jest entry points are compared
# against the pristine source tree so that any rewrite -- narrowing the globs,
# dropping a project, swapping in a no-op runner, or adding a shadowing config --
# is surfaced instead of only being charged a scope deduction.
_TEST_RUNNER_CONFIG_NAMES = (
    "jest.config.js",
    "jest.config.cjs",
    "jest.config.mjs",
    "jest.config.ts",
    "jest.config.cts",
    "jest.config.mts",
    "jest.config.tsx",
    "jest.config.jsx",
    "jest.config.json",
    ".jestrc",
    ".jestrc.json",
    ".jestrc.js",
    ".jestrc.cjs",
    ".jestrc.mjs",
)


def _runner_config_code(text: str) -> str:
    """Remove comments from a runner configuration, keeping string literals.

    ``_strip_comments`` blanks string *contents* because marker counting must not
    read prose; the runner fingerprint is the opposite case -- the compared
    selection globs (``testMatch`` / ``testPathIgnorePatterns`` operands) live
    inside string literals and MUST survive, otherwise two different selections
    would fingerprint identically.
    """

    pieces: list[str] = []
    index = 0
    length = len(text)
    state = "code"
    quote = ""
    while index < length:
        character = text[index]
        if state == "code":
            if character == "/" and index + 1 < length and text[index + 1] == "/":
                while index < length and text[index] != "\n":
                    index += 1
                continue
            if character == "/" and index + 1 < length and text[index + 1] == "*":
                index += 2
                while index < length and not (
                    text[index] == "*"
                    and index + 1 < length
                    and text[index + 1] == "/"
                ):
                    index += 1
                index += 2
                continue
            if character in ("'", '"', "`"):
                state = "string"
                quote = character
            pieces.append(character)
            index += 1
            continue
        pieces.append(character)
        if character == "\\":
            if index + 1 < length:
                pieces.append(text[index + 1])
                index += 2
                continue
            index += 1
            continue
        if character == quote:
            state = "code"
        index += 1
    return "".join(pieces)


def _runner_config_fingerprint(text: str) -> str:
    """Comment/whitespace-insensitive fingerprint of a runner configuration.

    Comments and layout churn must not read as a change; the compared tokens
    (including string literals such as ``testMatch`` globs) must.
    """

    normalised = re.sub(r"\s+", " ", _runner_config_code(text)).strip()
    return hashlib.sha256(normalised.encode("utf-8", "ignore")).hexdigest()


def _test_runner_config_fingerprints(
    root: Path,
    *,
    neutralise: Sequence[Path] = (),
) -> dict[str, str]:
    """Map each present test-runner configuration to its fingerprint.

    ``package.json`` contributes only its ``jest`` field: the manifest itself is
    expected to change (dependency/engine migration), but a ``jest`` block is a
    test-selection directive and must stay comparable to the source.
    """

    fingerprints: dict[str, str] = {}

    def neutralise_paths(text: str) -> str:
        for path in neutralise:
            if path:
                text = text.replace(str(path), "<ROOT>")
        return text

    for name in _TEST_RUNNER_CONFIG_NAMES:
        path = root / name
        if path.is_file() and not path.is_symlink():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            fingerprints[name] = _runner_config_fingerprint(neutralise_paths(text))
    manifest = root / "package.json"
    if manifest.is_file() and not manifest.is_symlink():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, Mapping) and isinstance(payload.get("jest"), Mapping):
            fingerprints["package.json#jest"] = _runner_config_fingerprint(
                json.dumps(payload["jest"], sort_keys=True, default=str)
            )
    return fingerprints


def _test_runner_config_changes(
    source_root: Path | None,
    candidate_root: Path,
) -> list[dict[str, str]]:
    """Report test-runner configuration that drifted from the pristine source."""

    if not isinstance(source_root, Path) or not source_root.is_dir():
        return []
    source = _test_runner_config_fingerprints(
        source_root, neutralise=(source_root,)
    )
    candidate = _test_runner_config_fingerprints(
        candidate_root, neutralise=(candidate_root,)
    )
    changes: list[dict[str, str]] = []
    for name in sorted(set(source) | set(candidate)):
        if source.get(name) == candidate.get(name):
            continue
        if name in source and name in candidate:
            kind = "modified"
        elif name in candidate:
            kind = "new"
        else:
            kind = "deleted"
        changes.append(
            {
                "path": name,
                "kind": kind,
                "rule_id": "TEST-RUNNER-CONFIG",
                "reason": "test-runner configuration defines which tests execute",
            }
        )
    return changes


def _test_integrity(
    root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    allowed_integrity: Mapping[str, Any] | None = None,
    source_root: Path | None = None,
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
    weakened = _weakened_allowed_paths(root, allowed, allowed_integrity, source_root)
    runner_changes = _test_runner_config_changes(source_root, root)
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = _marker_counts(root)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if missing or changed or weakened or runner_changes else "PASS",
        source=source,
        evidence={
            "missing": missing,
            "changed": changed,
            "weakened": weakened,
            "runner_config_changes": runner_changes,
        },
    )
    skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={"before": dict(marker_before), "after": marker_after, "delta": marker_delta},
    )
    return tamper, skip


_CHANGE_SCOPE_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
    }
)
# Generated build output and tool caches (``lib``, ``dist``, ``build``,
# ``coverage``, ``.yarn``, ``.cache``, ``.turbo``) only live at the repository
# root.  A directory with the same name nested inside ``src`` is ordinary source
# and MUST still be range-checked: otherwise a candidate can hide an out-of-scope
# file in ``src/.cache/`` or ``src/build/`` and the migration-scope gate reports
# 100%.  Only the root-level instance is exempt.
_CHANGE_SCOPE_ROOT_ONLY_EXCLUDED_PARTS = frozenset(
    {"lib", "dist", "build", "coverage", ".yarn", ".cache", ".turbo"}
)
_CHANGE_SCOPE_EXCLUDED_NAMES = frozenset(
    {".harbor-result.json", ".eslintcache", ".pnp.cjs", ".pnp.loader.mjs"}
)
# Transient-looking suffixes (``.tmp``, ``.log``, ``.orig``, ``.rej``, ``.swp``,
# ``.tsbuildinfo``, ``.pyc``, ``.gz``) are deliberately NOT exempt from the scope
# inventory.  The candidate patch is collected with ``git add -A``, which honours
# ``.gitignore``: a git-ignored transient file therefore never reaches the
# candidate tree through the patch, while a *candidate-authored* file that merely
# wears a lazy suffix does.  Blanket-exempting those suffixes let a candidate
# hide an out-of-scope file from ``migration_scope_complete``.  The verifier's own
# build outputs live under the excluded build directories below, so no generated
# artifact depends on this exemption.


def _inventory_digests(root: Path) -> dict[str, str]:
    """Hash every regular in-scope file of a repository tree."""

    if not isinstance(root, Path) or not root.is_dir() or root.is_symlink():
        return {}
    values: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        parts = Path(relative).parts
        if any(part in _CHANGE_SCOPE_EXCLUDED_PARTS for part in parts):
            continue
        if parts and parts[0] in _CHANGE_SCOPE_ROOT_ONLY_EXCLUDED_PARTS:
            continue
        if path.name in _CHANGE_SCOPE_EXCLUDED_NAMES:
            continue
        try:
            values[relative] = _digest(path)
        except OSError:
            continue
    return values


def _out_of_scope_changes(
    source_root: Path | None,
    candidate_root: Path,
    allowed_paths: Sequence[str] | set[str],
) -> list[dict[str, str]]:
    """Report files changed outside the declared allowed change-set.

    Instruction Constraint 8 restricts edits to an explicit file list, yet the
    residue scanner only inspects the paths its rules name.  Comparing the
    candidate tree against the pristine source tree closes that gap: a new,
    modified or deleted file that is not in ``allowed_change_paths`` is surfaced
    as a scope hit so ``migration_scope_complete`` cannot return 100% for an
    out-of-scope edit.
    """

    if not isinstance(source_root, Path) or not source_root.is_dir():
        return []
    allowed = {
        str(item).replace("\\", "/").strip("/")
        for item in allowed_paths
    }
    source = _inventory_digests(source_root)
    candidate = _inventory_digests(candidate_root)
    hits: list[dict[str, str]] = []

    def record(path: str, kind: str) -> None:
        hits.append(
            {
                "path": path,
                "token": "<out-of-scope-change>",
                "kind": "scope",
                "rule_id": "ALLOWED-CHANGE-PATHS",
                "change": kind,
            }
        )

    for relative in sorted(set(candidate) - set(source)):
        if relative not in allowed:
            record(relative, "new")
    for relative in sorted(set(source) & set(candidate)):
        if relative not in allowed and source[relative] != candidate[relative]:
            record(relative, "modified")
    for relative in sorted(set(source) - set(candidate)):
        if relative not in allowed:
            record(relative, "deleted")
    return hits


def build_hardgate_evidence(
    candidate_root: Path,
    *,
    config: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    observation_errors: Sequence[str],
    receipts: Sequence[Mapping[str, Any]],
    patch_applicable: bool | None,
    source_test_baseline: Mapping[str, Any] | None,
    allowed_integrity: Mapping[str, Any] | None = None,
    source_root: Path | None = None,
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
    allowed_change_paths = policy.get("allowed_change_paths", [])
    allowed_change_paths = (
        allowed_change_paths
        if isinstance(allowed_change_paths, Sequence)
        and not isinstance(allowed_change_paths, (str, bytes))
        else []
    )
    residue_hits = [
        *residue_hits,
        *_out_of_scope_changes(source_root, candidate_root, allowed_change_paths),
    ]
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
        candidate_root,
        source_test_baseline,
        config,
        allowed_integrity=allowed_integrity,
        source_root=source_root,
        source=test_baseline_source,
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
