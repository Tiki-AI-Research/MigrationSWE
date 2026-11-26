"""Small, non-executing Go source checks for imported API calls.

This is a lexical check, not a Go type checker or proof of a complete migration.
Behavior probes remain authoritative. Imports are resolved per file, and prose,
string data, type references and blank imports cannot impersonate API calls.
"""

from __future__ import annotations

import re
from typing import Iterable


_LEX = re.compile(
    r'(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)'
    r'|(?P<string>"(?:\\[\s\S]|[^"\\])*"|`[^`]*`)'
    r"|(?P<rune>'(?:\\[\s\S]|[^'\\])*')"
    r'|(?P<ident>[^\W\d]\w*)|(?P<number>\d[\w.]*)'
    r'|(?P<operator>:=|\+\+|--|==|!=|<=|>=|&&|\|\||\.\.\.|[^\s])',
    re.UNICODE,
)
_KEYWORDS = frozenset(
    'break default func interface select case defer go map struct chan else goto '
    'package switch const fallthrough if range type continue for import return var'.split()
)


def tokens(text: str) -> list[str]:
    result: list[str] = []
    can_end = False
    for match in _LEX.finditer(text):
        kind, value = match.lastgroup, match.group()
        if kind in {'space', 'comment'}:
            if '\n' in value and can_end:
                result.append(';')
                can_end = False
            continue
        result.append(value)
        can_end = (
            kind in {'string', 'rune', 'number'}
            or kind == 'ident' and (value not in _KEYWORDS or value in {
                'break', 'continue', 'fallthrough', 'return'
            })
            or value in {')', ']', '}', '++', '--'}
        )
    return result


def _unquote(value: str) -> str:
    if value.startswith('`'):
        return value[1:-1].replace('\r', '')
    body = value[1:-1]
    escapes = {'a': '\a', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r',
               't': '\t', 'v': '\v', '\\': '\\', '"': '"', "'": "'"}

    def replace(match: re.Match[str]) -> str:
        text = match.group()[1:]
        if text in escapes:
            return escapes[text]
        if text[0] in 'xuU':
            return chr(int(text[1:], 16))
        return chr(int(text, 8))

    return re.sub(r'\\(?:[abfnrtv\\"\']|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}'
                  r'|U[0-9a-fA-F]{8}|[0-7]{3})', replace, body)


def imports(items: list[str]) -> list[tuple[str, str]]:
    result = []
    index = 0
    while index < len(items):
        if items[index] != 'import':
            index += 1
            continue
        index += 1
        block = index < len(items) and items[index] == '('
        if block:
            index += 1
        while index < len(items):
            if items[index] == ';':
                index += 1
                if block:
                    continue
                break
            if items[index] == ')':
                index += 1
                break
            alias = ''
            if not items[index].startswith(('"', '`')):
                alias = items[index]
                index += 1
            if index >= len(items) or not items[index].startswith(('"', '`')):
                break
            try:
                result.append((alias, _unquote(items[index])))
            except (ValueError, OverflowError):
                pass  # Invalid import paths are rejected by the build gate.
            index += 1
            if not block:
                break
    return result


def module_requirements(text: str) -> dict[str, str]:
    """Read require directives, excluding comments and replace/exclude blocks."""
    result = {}
    in_require = False
    for line in text.splitlines():
        line = line.split('//', 1)[0].strip()
        if line.startswith('require') and re.match(r'require(?:\s|\()', line):
            line = line[len('require'):].strip()
            if line == '(':
                in_require = True
                continue
        elif not in_require:
            continue
        if line == ')':
            in_require = False
            continue
        match = re.fullmatch(r'("[^"\n]+"|[^\s()]+)\s+(v[^\s()]+)', line)
        if match:
            path = match[1]
            result[_unquote(path) if path.startswith('"') else path] = match[2]
    return result


class GoSource:
    def __init__(self, text: str):
        self.tokens = tokens(text)
        self.imports = imports(self.tokens)

    def expression_span(self, start: int, end: int) -> tuple[int, int]:
        """Include parentheses around a function value, not a caller's args."""
        items = self.tokens
        while start and end + 1 < len(items) and items[start - 1] == '(' and items[end + 1] == ')':
            opening = start - 1
            previous = items[opening - 1] if opening else ''
            if (previous and (previous.isidentifier() and previous not in _KEYWORDS
                              or previous in {')', ']', '.'}
                              or previous.startswith(('"', '`', "'")))):
                break
            if self.closing(opening) != end + 1:
                break
            start, end = opening, end + 1
        return start, end

    def references(self, package: str, symbols: Iterable[str]) -> list[tuple[int, int]]:
        """Return inclusive token spans for references bound to an import."""
        names = set(symbols)
        aliases = set()
        dotted = False
        for alias, path in self.imports:
            if path != package or alias == '_':
                continue
            if alias == '.':
                dotted = True
            else:
                aliases.add(alias or ('message' if path.endswith('/go-message')
                                      else path.rsplit('/', 1)[-1]))
        items = self.tokens
        result = []
        for index, item in enumerate(items):
            if index and items[index - 1] == '.':
                continue
            if (item in aliases and index + 2 < len(items)
                    and items[index + 1] == '.' and items[index + 2] in names):
                result.append((index, index + 2))
            elif dotted and item in names:
                result.append((index, index))
        return result

    def calls(self, package: str, symbols: Iterable[str]) -> list[tuple[int, int]]:
        """Find direct calls and simple function aliases, ignoring marker padding."""
        items = self.tokens
        references = [self.expression_span(*span) for span in self.references(package, symbols)]
        aliases = set()
        for start, end in references:
            if start >= 2 and items[start - 1] in {'=', ':='}:
                name = items[start - 2]
                if name.isidentifier() and name != '_' and (end + 1 == len(items)
                                                           or items[end + 1] in {';', '}'}):
                    aliases.add(name)
        # Follow aliases such as readAgain := read, without interpreting data.
        for _ in range(len(items)):
            before = len(aliases)
            for index, item in enumerate(items):
                if item not in aliases:
                    continue
                start, end = self.expression_span(index, index)
                if (start >= 2 and items[start - 1] in {'=', ':='}
                        and (end + 1 == len(items) or items[end + 1] in {';', '}'})
                        and items[start - 2].isidentifier() and items[start - 2] != '_'):
                    aliases.add(items[start - 2])
            if len(aliases) == before:
                break
        spans = references + [self.expression_span(index, index) for index, item in enumerate(items)
                              if item in aliases and (not index or items[index - 1] != '.')]
        return sorted({(start, end + 1) for start, end in spans
                       if end + 1 < len(items) and items[end + 1] == '('
                       and (not start or items[start - 1] != 'func')})

    def closing(self, opening: int) -> int:
        depth = 0
        for index in range(opening, len(self.tokens)):
            if self.tokens[index] == '(':
                depth += 1
            elif self.tokens[index] == ')':
                depth -= 1
                if not depth:
                    return index
        return len(self.tokens) - 1

    def discards_first_result(self, start: int, opening: int) -> bool:
        """Recognize an explicit blank result or a standalone preflight call.

        In `if _, err := mail.ReadMessage(...); err != nil`, only the error is
        consumed. That does not establish MIME traversal by the old parser.
        Returning/passing the parsed value or assigning it to a named result
        remains evidence; this deliberately does not infer arbitrary data flow.
        """
        items = self.tokens
        end = self.closing(opening)
        if end + 1 < len(items) and items[end + 1] in {'.', '['}:
            return False
        boundary = start - 1
        while boundary >= 0 and items[boundary] not in {';', '{', '}'}:
            boundary -= 1
        before = items[boundary + 1:start]
        assignment = max((index for index, item in enumerate(before)
                          if item in {'=', ':='}), default=-1)
        if assignment >= 0:
            # The blank must discard this call's result, not the result of an
            # outer consumer such as `_, err := decode(mail.ReadMessage(r))`.
            if any(item != '(' for item in before[assignment + 1:]):
                return False
            left = before[:assignment]
            while left and left[0] in {'if', 'for', 'var'}:
                left = left[1:]
            if not left:
                return False
            if left[0] == '_' and (len(left) == 1 or left[1] == ','):
                return True
            if left[0].isidentifier() and (len(left) == 1 or left[1] == ','):
                # A named value immediately discarded with `_ = parsed` is
                # equivalent to a blank result. Any other use stays visible.
                name = left[0]
                uses = [index for index in range(end + 1, len(items)) if items[index] == name]
                return all(index >= 2 and items[index - 2:index] == ['_', '=']
                           and (index + 1 == len(items) or items[index + 1] in {';', '}'})
                           for index in uses)
            return False
        return before in ([], ['go'], ['defer'])
