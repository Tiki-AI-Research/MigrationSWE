"""Conservative constant propagation for JavaScript module specifiers.

This never executes candidate JavaScript. Only a resolved string passed to a
known Node loader is evidence; decoding upload data or naming a loader is not.
Unknown expressions are left to the existing runtime audit.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, NamedTuple, Sequence
from urllib.parse import unquote


UNKNOWN = object()


class Token(NamedTuple):
    text: str
    kind: str
    offset: int


class Name(NamedTuple):
    value: str


class Loader(NamedTuple):
    name: str
    bound: tuple = ()


class Method(NamedTuple):
    owner: Any
    name: str


def follows_control_header(result: Sequence[Token]) -> bool:
    """A statement may start with a regex after a control header or else/do.

    A call's closing parenthesis still ends an expression: treating every ')'
    as a regex prefix would hide real loads in ``f() / require('express') / 2``.
    Property names such as ``object.if(...)`` are calls, too.
    """
    if result and result[-1].text in ('else', 'do'):
        return len(result) == 1 or result[-2].text not in ('.', '?.', '#')
    if not result or result[-1].text != ')':
        return False
    depth = 1
    for index in range(len(result) - 2, -1, -1):
        text = result[index].text
        if text == ')':
            depth += 1
        elif text == '(':
            depth -= 1
            if depth == 0:
                keyword = index - 1
                if keyword >= 1 and result[keyword].text == 'await' and result[keyword - 1].text == 'for':
                    keyword -= 1
                return (keyword >= 0 and result[keyword].text in ('if', 'while', 'for', 'with')
                        and (keyword == 0 or result[keyword - 1].text not in ('.', '?.', '#')))
    return False


def tokens(source: str) -> List[Token]:
    result = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith('//', index):
            end = source.find('\n', index)
            index = len(source) if end < 0 else end
            continue
        if source.startswith('/*', index):
            end = source.find('*/', index + 2)
            index = len(source) if end < 0 else end + 2
            continue
        start = index
        if char in "'\"`":
            quote = char
            index += 1
            while index < len(source):
                if source[index] == '\\':
                    index += 2
                elif source[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            result.append(Token(source[start:index], 'template' if quote == '`' else 'string', start))
            continue
        # A regex is data too. Do not discover loader-looking text inside it.
        if char == '/' and (not result or result[-1].text in (
            '=', '(', '[', '{', ',', ':', ';', 'return', '=>', '!', '&&', '||', '?',
        ) or follows_control_header(result)):
            match = re.match(r'/((?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\r\n\\])*)/[a-z]*', source[index:])
            if match:
                index += len(match.group())
                result.append(Token(match.group(), 'regex', start))
                continue
        match = re.match(r'[A-Za-z_$][\w$]*|0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|\d+', source[index:])
        if match:
            value = match.group()
            index += len(value)
            result.append(Token(value, 'number' if value[0].isdigit() else 'word', start))
            continue
        operator = next((value for value in ('...', '=>', '===', '!==', '==', '!=', '&&', '||', '?.')
                         if source.startswith(value, index)), char)
        index += len(operator)
        result.append(Token(operator, 'punctuation', start))
    return result


def string_value(text: str):
    if len(text) < 2 or text[-1] != text[0]:
        return UNKNOWN
    body = text[1:-1]

    def escape(match):
        value = match.group(1)
        if value.startswith('u{'):
            return chr(int(value[2:-1], 16))
        if value.startswith(('u', 'x')) and len(value) > 1:
            return chr(int(value[1:], 16))
        return {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
                'v': '\v', '0': '\0', '\n': '', '\r\n': ''}.get(value, value)
    try:
        return re.sub(r'\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|[\s\S])', escape, body)
    except (ValueError, OverflowError):
        return UNKNOWN


class Scanner:
    def __init__(self, source: str, modules: Sequence[str]):
        self.source = source
        self.tokens = tokens(source)
        self.modules = tuple(modules)
        self.findings: List[Dict[str, Any]] = []
        self.environment = {name: Name(name) for name in (
            'String', 'Buffer', 'module', 'process', 'globalThis', 'global', 'Reflect',
            'NativeModule', 'atob', 'unescape',
        )}
        self.environment.update(require=Loader('require'), **{'import': Loader('import')})
        self.parameters = self.function_parameters()

    def function_parameters(self):
        """Locate parameter scopes without treating a locally named loader as Node.

        Unsupported binding patterns are deliberately unknown. This is evidence
        collection, not a JavaScript interpreter; runtime auditing remains the
        authority for values that cannot be established statically.
        """
        pairs, stack = {}, []
        for index, token in enumerate(self.tokens):
            if token.text in ('(', '[', '{'):
                stack.append(index)
            elif token.text in (')', ']', '}') and stack:
                opening = stack.pop()
                if self.tokens[opening].text == {')': '(', ']': '[', '}': '{'}[token.text]:
                    pairs[opening] = index
                    pairs[index] = opening
        parameters = {}
        for index, token in enumerate(self.tokens):
            if token.text != '=>' and token.text != '{':
                continue
            if token.text == '{' and index and self.tokens[index - 1].text == '=>':
                continue
            end = index - 1
            if end < 0:
                continue
            if self.at(end, ')') and end in pairs:
                opening = pairs[end]
                previous = self.tokens[opening - 1].text if opening else ''
                if previous in ('if', 'for', 'while', 'switch', 'with'):
                    continue
                names = {item.text for pos, item in enumerate(self.tokens[opening + 1:end], opening + 1)
                         if item.kind == 'word' and not self.at(pos + 1, ':')}
            elif token.text == '=>' and self.tokens[end].kind == 'word':
                names = {self.tokens[end].text}
            else:
                continue
            body = index + 1 if token.text == '=>' else index
            if self.at(body, '{'):
                parameters[body] = names
            else:
                # Expression arrows have no brace at which to install a scope.
                # Mask their parameters only while scanning that expression.
                parameters[index] = names
        return parameters

    def at(self, index: int, text: str) -> bool:
        return index < len(self.tokens) and self.tokens[index].text == text

    def property(self, owner, key):
        if not isinstance(key, str):
            return UNKNOWN
        if isinstance(owner, Name):
            name = owner.value + '.' + key
            if name == 'node:module.createRequire':
                return Name('createRequire')
            if name in ('module.require', 'process.mainModule.require', 'globalThis.require',
                        'global.require', 'globalThis.module.require', 'global.module.require',
                        'NativeModule.require', 'process.binding'):
                return Loader(name)
            return Name(name)
        return Method(owner, key)

    def call(self, function, arguments, start, direct=False):
        if isinstance(function, Loader):
            arguments = list(function.bound) + arguments
            value = arguments[0] if arguments else UNKNOWN
            if isinstance(value, str):
                module = value.removeprefix('node:')
                if any(module == name or module.startswith(name + '/') for name in self.modules):
                    offset = self.tokens[start].offset
                    label = ('requires child_process' if module == 'child_process' else
                             'requires the source framework express' if module.split('/')[0] in ('express', 'body-parser', 'multer') else
                             'requires a bridging runtime module')
                    if not direct:
                        label = 'loads a forbidden module through a constructed specifier'
                    self.findings.append({'value': value, 'module': module, 'kind': 'ordinary' if direct else 'hidden',
                                          'line': self.source.count('\n', 0, offset) + 1,
                                          'offset': offset, 'label': label})
            if value in ('module', 'node:module'):
                return Name('node:module')
            return UNKNOWN
        if isinstance(function, Method):
            owner, method = function
            if isinstance(owner, Loader):
                if method == 'bind':
                    return Loader(owner.name, owner.bound + tuple(arguments[1:]))
                if method == 'call':
                    return self.call(owner, arguments[1:], start)
                if method == 'apply' and len(arguments) > 1 and isinstance(arguments[1], tuple):
                    return self.call(owner, list(arguments[1]), start)
            if method == 'join' and isinstance(owner, tuple) and all(isinstance(value, (str, int)) for value in owner):
                separator = arguments[0] if arguments else ','
                if isinstance(separator, str):
                    return separator.join(str(value) for value in owner)
            if method == 'toString' and isinstance(owner, bytes):
                encoding = arguments[0] if arguments else 'utf8'
                if encoding in ('utf8', 'utf-8', 'ascii', 'latin1', 'binary'):
                    return owner.decode('latin1' if encoding in ('latin1', 'binary') else 'utf-8', errors='replace')
            return UNKNOWN
        if not isinstance(function, Name):
            return UNKNOWN
        name = function.value
        try:
            if name == 'createRequire':
                return Loader('createRequire')
            if name in ('String.fromCharCode', 'String.fromCodePoint') and all(isinstance(value, int) for value in arguments):
                if name.endswith('fromCharCode'):
                    return ''.join(chr(value & 0xffff) for value in arguments)
                return ''.join(chr(value) for value in arguments)
            if name == 'Buffer.from' and arguments and isinstance(arguments[0], str):
                value = arguments[0]
                encoding = arguments[1] if len(arguments) > 1 else 'utf8'
                if encoding in ('base64', 'base64url'):
                    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=False)
                if encoding == 'hex':
                    return bytes.fromhex(value)
                if encoding in ('utf8', 'utf-8', 'ascii', 'latin1'):
                    return value.encode('latin1' if encoding == 'latin1' else 'utf-8')
            if name == 'atob' and arguments and isinstance(arguments[0], str):
                value = arguments[0]
                return base64.b64decode(value + '=' * (-len(value) % 4)).decode('latin1')
            if name == 'unescape' and arguments and isinstance(arguments[0], str):
                return re.sub(r'%u([0-9a-fA-F]{4})', lambda match: chr(int(match.group(1), 16)),
                              unquote(arguments[0], encoding='latin1'))
            if name == 'Reflect.apply' and len(arguments) == 3 and isinstance(arguments[2], tuple):
                return self.call(arguments[0], list(arguments[2]), start)
        except (ValueError, TypeError, UnicodeError, OverflowError):
            pass
        return UNKNOWN

    def expression(self, index, depth=0):
        if index >= len(self.tokens) or depth > 32:
            return UNKNOWN, min(index + 1, len(self.tokens))
        if self.at(index, '{') or self.at(index, '}'):
            return UNKNOWN, index
        start = index
        token = self.tokens[index]
        index += 1
        if token.kind == 'string':
            value = string_value(token.text)
        elif token.kind == 'number':
            try:
                value = (int(token.text, 0) if token.text.startswith(('0x', '0X', '0o', '0O', '0b', '0B'))
                         else int(token.text)) if len(token.text) <= 256 else UNKNOWN
            except ValueError:
                value = UNKNOWN
        elif token.kind == 'template':
            body = token.text[1:-1]
            value = string_value(token.text)
            if '${' in body:
                def interpolate(match):
                    nested = Scanner(match.group(1), self.modules)
                    nested.environment = self.environment.copy()
                    result, end = nested.expression(0, depth + 1)
                    for finding in nested.findings:
                        offset = token.offset + 1 + match.start(1) + finding['offset']
                        self.findings.append({**finding, 'offset': offset,
                                              'line': self.source.count('\n', 0, offset) + 1})
                    if end != len(nested.tokens) or not isinstance(result, (str, int)):
                        raise ValueError('unknown template value')
                    return str(result)
                try:
                    # Escaped interpolation is literal upload/diagnostic data.
                    # Leave ambiguous escape patterns to runtime auditing.
                    if '\\${' in body:
                        raise ValueError('escaped template interpolation')
                    value = re.sub(r'\$\{([^{}]*)\}', interpolate, body)
                    if '${' in value:
                        value = UNKNOWN
                    else:
                        value = string_value('`' + value + '`')
                except ValueError:
                    value = UNKNOWN
        elif token.text == '[':
            values = []
            while index < len(self.tokens) and not self.at(index, ']'):
                item, end = self.expression(index, depth + 1)
                values.append(item)
                index = max(index + 1, end)
                if not self.at(index, ','):
                    break
                index += 1
            value = tuple(values) if self.at(index, ']') else UNKNOWN
            if self.at(index, ']'):
                index += 1
        elif token.text == '(':
            value, index = self.expression(index, depth + 1)
            if self.at(index, ')'):
                index += 1
            else:
                value = UNKNOWN
        else:
            value = self.environment.get(token.text, UNKNOWN)
        while index < len(self.tokens):
            if self.at(index, '.') or self.at(index, '?.'):
                if index + 1 >= len(self.tokens):
                    break
                value = self.property(value, self.tokens[index + 1].text)
                index += 2
            elif self.at(index, '['):
                key, end = self.expression(index + 1, depth + 1)
                if not self.at(end, ']'):
                    break
                value = self.property(value, key)
                index = end + 1
            elif self.at(index, '('):
                opening = index
                index += 1
                arguments = []
                while index < len(self.tokens) and not self.at(index, ')'):
                    spread = self.at(index, '...')
                    argument, end = self.expression(index + (1 if spread else 0), depth + 1)
                    if spread and isinstance(argument, tuple):
                        arguments.extend(argument)
                    else:
                        arguments.append(UNKNOWN if spread else argument)
                    index = max(index + 1, end)
                    if not self.at(index, ','):
                        break
                    index += 1
                if not self.at(index, ')'):
                    return UNKNOWN, index
                direct = (opening == start + 1 and self.tokens[start].text in ('require', 'import')
                          and index == opening + 2 and self.tokens[opening + 1].kind == 'string')
                value = self.call(value, arguments, start, direct)
                index += 1
            elif self.at(index, '+'):
                right, end = self.expression(index + 1, depth + 1)
                value = value + right if isinstance(value, str) and isinstance(right, str) else UNKNOWN
                index = end
            else:
                break
        return value, index

    def scan(self):
        index = 0
        scopes = []
        while index < len(self.tokens):
            token = self.tokens[index]
            if token.text == '{':
                names = self.parameters.get(index, set())
                scopes.append((self.environment.copy(), set(names)))
                self.environment.update({name: UNKNOWN for name in names})
                index += 1
            elif token.text == '}':
                if scopes:
                    self.environment, _ = scopes.pop()
                index += 1
            elif token.text == '=>':
                names = self.parameters.get(index)
                if names is None:
                    index += 1
                else:
                    saved = self.environment.copy()
                    self.environment.update({name: UNKNOWN for name in names})
                    _, end = self.expression(index + 1)
                    self.environment = saved
                    index = max(index + 1, end)
            elif token.text == 'function':
                name_index = index + (2 if self.at(index + 1, '*') else 1)
                if name_index < len(self.tokens) and self.tokens[name_index].kind == 'word':
                    self.environment[self.tokens[name_index].text] = UNKNOWN
                index += 1
            elif token.text in ('const', 'let', 'var') and self.at(index + 1, '{'):
                # Common Node spelling: const { createRequire } = require('module').
                closing = index + 2
                while closing < len(self.tokens) and self.tokens[closing].text not in ('}', '{'):
                    closing += 1
                if self.at(closing, '}') and self.at(closing + 1, '='):
                    owner, end = self.expression(closing + 2)
                    position = index + 2
                    while position < closing:
                        key = self.tokens[position].text
                        name = key
                        position += 1
                        if self.at(position, ':') and position + 1 < closing:
                            name = self.tokens[position + 1].text
                            position += 2
                        if self.at(position, ',') or position == closing:
                            self.environment[name] = self.property(owner, key)
                            if scopes:
                                scopes[-1][1].add(name)
                        else:
                            # Defaults/nested patterns are not established values.
                            self.environment[name] = UNKNOWN
                            while position < closing and not self.at(position, ','):
                                position += 1
                        position += 1
                    index = max(closing + 2, end)
                else:
                    index += 1
            elif (token.text in ('const', 'let', 'var') and index + 1 < len(self.tokens)
                  and self.tokens[index + 1].kind == 'word' and not self.at(index + 2, '=')):
                name = self.tokens[index + 1].text
                self.environment[name] = UNKNOWN
                if scopes:
                    scopes[-1][1].add(name)
                index += 2
            elif token.text in ('from', 'import') and index + 1 < len(self.tokens) and self.tokens[index + 1].kind == 'string':
                self.call(Loader('import'), [string_value(self.tokens[index + 1].text)], index, direct=True)
                index += 2
            elif token.kind == 'word' and self.at(index + 1, '='):
                # A property write does not rebind a variable of the same name.
                if index and self.tokens[index - 1].text in ('.', '?.'):
                    index += 2
                    continue
                value, end = self.expression(index + 2)
                declared = index and self.tokens[index - 1].text in ('const', 'let', 'var')
                if scopes:
                    parent, locals_ = scopes[-1]
                    if declared:
                        locals_.add(token.text)
                    elif token.text not in locals_ and token.text in parent:
                        # Branches and functions may or may not execute. A
                        # changed outer binding is no longer a known constant.
                        parent[token.text] = UNKNOWN
                self.environment[token.text] = value
                index = max(index + 2, end)
            else:
                _, end = self.expression(index)
                index = max(index + 1, end)
        return self.findings


def module_load_findings(source: str, modules: Sequence[str]) -> List[Dict[str, Any]]:
    return Scanner(source, modules).scan()
