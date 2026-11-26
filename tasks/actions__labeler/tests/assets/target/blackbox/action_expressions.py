"""Bounded, non-executing evaluation of GitHub Actions expressions."""
from __future__ import annotations

import glob
import hashlib
import json
import math
from pathlib import Path
import re


class ExpressionError(ValueError):
    pass


def truthy(value):
    return not (value is None or value is False or isinstance(value, (int, float)) and
                (value == 0 or math.isnan(value)) or isinstance(value, str) and value == '')


def string(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        raise ExpressionError('an object or array cannot be interpolated as a string')
    return str(value)


def number(value):
    if value is None or value is False or value == '':
        return 0
    if value is True:
        return 1
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, (int, float)) and not isinstance(result, bool) else math.nan
        except ValueError:
            pass
    return math.nan


TOKEN = re.compile(r'''\s*(?:(?P<string>'(?:[^']|'')*'|"(?:[^"\\]|\\.)*")|(?P<number>-?(?:0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?))|(?P<name>[A-Za-z_][A-Za-z_0-9-]*)|(?P<op>\|\||&&|==|!=|<=|>=|[!<>().,\[\]*]))''')


class Parser:
    def __init__(self, text):
        if len(text) > 65536:
            raise ExpressionError('action expression is too long')
        self.tokens, self.index = [], 0
        offset = 0
        while offset < len(text.rstrip()):
            match = TOKEN.match(text, offset)
            if not match:
                raise ExpressionError('invalid action expression near: ' + text[offset:offset + 40])
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            offset = match.end()
        self.tokens.append(('end', ''))

    def take(self, value=None):
        token = self.tokens[self.index]
        if value is not None and token[1] != value:
            raise ExpressionError('expected %r in action expression' % value)
        self.index += 1
        return token

    def parse(self, minimum=0, depth=0):
        if depth > 80:
            raise ExpressionError('action expression nesting is too deep')
        kind, value = self.take()
        if value == '!':
            node = ('not', self.parse(5, depth + 1))
        elif value == '(':
            node = self.parse(0, depth + 1)
            self.take(')')
        elif kind == 'string':
            node = ('literal', value[1:-1].replace("''", "'") if value[0] == "'" else json.loads(value))
        elif kind == 'number':
            node = ('literal', int(value, 16) if 'x' in value.lower() else json.loads(value))
        elif kind == 'name':
            if value.lower() in {'true', 'false', 'null'}:
                node = ('literal', {'true': True, 'false': False, 'null': None}[value.lower()])
            elif self.tokens[self.index][1] == '(':
                self.take('(')
                arguments = []
                while self.tokens[self.index][1] != ')':
                    arguments.append(self.parse(0, depth + 1))
                    if self.tokens[self.index][1] != ',':
                        break
                    self.take(',')
                self.take(')')
                node = ('call', value.lower(), arguments)
            else:
                node = ('get', ('context',), ('literal', value))
        else:
            raise ExpressionError('expected a value in action expression')
        precedence = {'||': 1, '&&': 2, '==': 3, '!=': 3, '<': 4, '>': 4, '<=': 4, '>=': 4}
        while True:
            operator = self.tokens[self.index][1]
            if operator in {'.', '['}:
                self.take()
                if operator == '.':
                    key_kind, key = self.take()
                    if key_kind != 'name' and key != '*':
                        raise ExpressionError('invalid property in action expression')
                    index = ('literal', key)
                else:
                    index = self.parse(0, depth + 1)
                    self.take(']')
                node = ('get', node, index)
            elif operator in precedence and precedence[operator] >= minimum:
                self.take()
                node = ('binary', operator, node, self.parse(precedence[operator] + 1, depth + 1))
            else:
                return node


class Selection(list):
    """A wildcard result projects later properties over its members."""


def lookup(value, key):
    if isinstance(value, Selection):
        return Selection(lookup(item, key) for item in value)
    if key == '*':
        return Selection(value.values() if isinstance(value, dict) else value if isinstance(value, list) else [])
    if isinstance(value, dict):
        return next((item for name, item in value.items() if str(name).casefold() == str(key).casefold()), '')
    if isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
        return value[key]
    return ''  # Missing context properties evaluate to the empty string.


def equal(left, right):
    if type(left) is not type(right):
        left, right = number(left), number(right)
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return left is right
    return left == right


def evaluate(text, context):
    parser = Parser(text.strip())
    tree = parser.parse()
    if parser.tokens[parser.index][0] != 'end':
        raise ExpressionError('unexpected trailing action expression')

    def visit(node, depth=0):
        if depth > 100:
            raise ExpressionError('action expression nesting is too deep')
        kind = node[0]
        if kind == 'literal':
            return node[1]
        if kind == 'context':
            return context
        if kind == 'get':
            return lookup(visit(node[1], depth + 1), visit(node[2], depth + 1))
        if kind == 'not':
            return not truthy(visit(node[1], depth + 1))
        if kind == 'binary':
            operator, left = node[1], visit(node[2], depth + 1)
            if operator == '&&':
                return visit(node[3], depth + 1) if truthy(left) else left
            if operator == '||':
                return left if truthy(left) else visit(node[3], depth + 1)
            right = visit(node[3], depth + 1)
            if operator in {'==', '!='}:
                return equal(left, right) == (operator == '==')
            if type(left) is not type(right) or not isinstance(left, str):
                left, right = number(left), number(right)
            else:
                left, right = left.casefold(), right.casefold()
            return {'<': lambda: left < right, '>': lambda: left > right,
                    '<=': lambda: left <= right, '>=': lambda: left >= right}[operator]()
        name, arguments = node[1], [visit(item, depth + 1) for item in node[2]]
        if name in {'success', 'failure', 'cancelled', 'always'} and not arguments:
            return {'success': context.get('_status', 'success') == 'success',
                    'failure': context.get('_status') == 'failure', 'cancelled': False, 'always': True}[name]
        if name in {'contains', 'startswith', 'endswith'} and len(arguments) == 2:
            left, right = arguments
            if name == 'contains' and isinstance(left, list):
                return any(equal(item, right) for item in left)
            left, right = string(left).casefold(), string(right).casefold()
            return right in left if name == 'contains' else left.startswith(right) if name == 'startswith' else left.endswith(right)
        if name == 'fromjson' and len(arguments) == 1:
            return json.loads(string(arguments[0]))
        if name == 'tojson' and len(arguments) == 1:
            return json.dumps(arguments[0], ensure_ascii=False, indent=2)
        if name == 'join' and len(arguments) in {1, 2}:
            separator = string(arguments[1]) if len(arguments) == 2 else ','
            return separator.join(map(string, arguments[0])) if isinstance(arguments[0], list) else string(arguments[0])
        if name == 'format' and arguments:
            template = string(arguments[0])
            def substitute(match):
                if match[0] == '{{': return '{'
                if match[0] == '}}': return '}'
                index = int(match[1]) + 1
                if index >= len(arguments): raise ExpressionError('format argument index is out of range')
                return string(arguments[index])
            return re.sub(r'\{\{|\}\}|\{(\d+)\}', substitute, template)
        if name == 'hashfiles' and arguments:
            workspace = Path(context.get('github', {}).get('workspace', '.')).resolve()
            files = set()
            for pattern in arguments:
                pattern = string(pattern)
                negate = pattern.startswith('!')
                matches = {Path(path).resolve() for path in glob.glob(str(workspace / pattern.lstrip('!')), recursive=True)
                           if Path(path).is_file() and Path(path).resolve().is_relative_to(workspace)}
                files = files - matches if negate else files | matches
            return hashlib.sha256(b''.join(hashlib.sha256(path.read_bytes()).digest() for path in sorted(files))).hexdigest() if files else ''
        raise ExpressionError('unsupported action function or argument count: ' + name)

    return visit(tree)
