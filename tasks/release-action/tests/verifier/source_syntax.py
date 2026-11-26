"""Small non-executing lexer for source evidence, preserving literal identity.

Comments, quoted diagnostics and executable tokens are separate. This is not a
compiler or a proof of arbitrary program equivalence. In particular, retained
tests may gain independent cases, but changes to an existing case's setup,
control flow, helper context or matcher require replacement-suite evidence.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int
    end: int


def tokens(text: str, *, python: bool = False, go: bool = False, include_comments: bool = False) -> list[Token]:
    out = []
    i = 0
    operators = ("===", "!==", "...", "=>", "==", "!=", "<=", ">=", "&&", "||", "?.", "??", ":=", "++", "--", "+=", "-=", "**", "<<", ">>")
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        start = i
        if text.startswith("//", i) or python and text[i] == "#":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            if include_comments:
                out.append(Token("comment", text[start:i], start, i))
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            if include_comments:
                out.append(Token("comment", text[start:i], start, i))
            continue
        quote = text[i]
        if quote in "\"'`":
            delimiter = quote * 3 if python and text.startswith(quote * 3, i) else quote
            i += len(delimiter)
            while i < len(text):
                if (quote != "`" or not go) and text[i] == "\\":
                    i += 2
                elif text.startswith(delimiter, i):
                    i += len(delimiter)
                    break
                else:
                    i += 1
            literal = text[start:i]
            try:
                value = ast.literal_eval(literal) if quote != "`" else literal[1:-1]
            except (ValueError, SyntaxError):
                value = literal[1:-1]
            # Template interpolation remains an opaque expression, not a
            # constant string; changing its contents changes the evidence.
            kind = "template" if quote == "`" and "${" in value else "string"
            out.append(Token(kind, str(value), start, i))
            continue
        if text[i] == "/" and (not out or out[-1].value in {"(", "[", "=", ",", ":", "return", "=>", "!"}):
            j, in_class = i + 1, False
            while j < len(text) and text[j] not in "\r\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    j += 1
                    while j < len(text) and text[j].isalpha():
                        j += 1
                    out.append(Token("regexp", text[i:j], i, j))
                    i = j
                    break
                j += 1
            if i != start:
                continue
        match = re.match(r"[\w$]+", text[i:], re.UNICODE)
        if match:
            i += len(match[0])
            out.append(Token("word", match[0], start, i))
            continue
        operator = next((op for op in operators if text.startswith(op, i)), text[i])
        i += len(operator)
        out.append(Token("punct", operator, start, i))
    return out


def pairs(items: list[Token]) -> dict[int, int]:
    result, stack = {}, []
    closers = {")": "(", "]": "[", "}": "{"}
    for index, token in enumerate(items):
        if token.kind != "punct":
            continue
        if token.value in {"(", "[", "{"}:
            stack.append(index)
        elif token.value in closers and stack and items[stack[-1]].value == closers[token.value]:
            opening = stack.pop()
            result[opening] = index
            result[index] = opening
    return result


def canonical(items: list[Token], text: str = "") -> list[list[str]]:
    result = []
    matching = pairs(items)
    restricted = {"return", "throw", "break", "continue", "yield"}
    for index, token in enumerate(items):
        previous = items[index - 1] if index else None
        following = items[index + 1] if index + 1 < len(items) else None
        if token.value == ";" and token.kind == "punct":
            # A for-header separator, empty conditional body, restricted
            # return, or separator before a continuing expression is semantic.
            openings = [k for k, end in matching.items() if k < index < end]
            in_parentheses = bool(openings) and items[max(openings)].value == "("
            control_end = previous and previous.value == ")" and index - 1 in matching and matching[index - 1] > 0 and items[matching[index - 1] - 1].value in {"if", "while", "for", "with"}
            if previous and previous.value in restricted:
                result.append(["asi", "boundary"])
            elif (in_parentheses or control_end or not previous or previous.value in {";", "{"}
                  or following and following.value in {"(", "[", "+", "-", "/", "*", "?.", "."}
                  or following and following.kind == "template"):
                result.append([token.kind, token.value])
            continue
        if token.value == "," and token.kind == "punct" and following and following.value in {")", "]", "}"}:
            continue
        result.append([token.kind, token.value])
        if text and token.value in restricted and (following is None or following.value != ";"):
            if following is None or following.value == "}" or "\n" in text[token.end:following.start]:
                result.append(["asi", "boundary"])
    return result


_TESTS = {"it", "test", "specify", "xit", "xtest", "xspecify"}
_SUITES = {"describe", "context", "suite", "xdescribe", "xcontext"}


def test_flow(text: str) -> list[dict]:
    """Protect full existing cases and their enclosing setup/helper contexts.

    A matcher on a following line, a preceding return, or a change to a fixed
    expected value remains part of its case. Comments/quotes/line wrapping and
    optional semicolons do not affect the representation. Additional cases are
    compared separately instead of changing a whole-file assertion count.
    """
    items = tokens(text)
    matching = pairs(items)
    if not any(t.value in {"expect", "assert", "require", "should"} and t.kind == "word"
               and index + 1 < len(items) and items[index + 1].value in {"(", "."}
               for index, t in enumerate(items)):
        return []
    records = []

    def walk(start, stop, scope):
        context = []
        i = start
        while i < stop:
            token = items[i]
            if token.kind == "word" and token.value in _TESTS | _SUITES and (i == 0 or items[i - 1].value not in {".", "?."}):
                j = i + 1
                # Modifiers and parameterized cases remain in the protected
                # prefix, including the data passed to .each(...).
                while j < stop:
                    if items[j].value == "." and j + 1 < stop:
                        modifier = items[j + 1].value
                        j += 2
                        if modifier in {"each", "for"} and j < stop and items[j].value == "(" and j in matching:
                            j = matching[j] + 1
                    else:
                        break
                if j < stop and items[j].value == "(" and j in matching:
                    end = matching[j]
                    title = items[j + 1].value if j + 1 < end else ""
                    arrow = next((k for k in range(j + 1, end) if items[k].value == "=>"), None)
                    body = arrow + 1 if arrow is not None else None
                    if body is not None and body < end and items[body].value == "{" and body in matching:
                        path = [*scope, title]
                        if token.value in _SUITES:
                            records.append({"kind": "suite", "scope": path,
                                            "tokens": canonical(items[i:body], text)})
                            walk(body + 1, matching[body], path)
                        else:
                            records.append({"kind": "test", "scope": path,
                                            "tokens": canonical(items[i:end + 1], text)})
                        i = end + 1
                        if i < stop and items[i].value == ";":
                            i += 1
                        continue
            context.append(token)
            i += 1
        records.append({"kind": "context", "scope": scope, "tokens": canonical(context, text)})

    walk(0, len(items), [])
    return sorted(records, key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))


def preserves_flow(before: list[dict], after: list[dict]) -> bool:
    encode = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False)
    old, new = Counter(map(encode, before)), Counter(map(encode, after))
    return all(new[key] >= count for key, count in old.items())
