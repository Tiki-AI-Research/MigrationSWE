"""Non-executing shell lexer/parser for action launcher evidence.

Words retain quoting and expansion structure. Shell functions, groups and
substitutions are syntax rather than command-name fragments. Unsupported or
malformed syntax is explicit, not evidence that a Go launcher ran.
"""
from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=1)
def _pattern_functions():
    # POSIX fnmatch supplies bracket classes, collating elements and byte vs
    # multibyte matching. Use a thread-local locale; never change Python's
    # process locale or execute the inspected shell.
    import ctypes
    library = ctypes.CDLL(None)
    for name, arguments, result in [
        ('fnmatch', [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
        ('newlocale', [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p], ctypes.c_void_p),
        ('uselocale', [ctypes.c_void_p], ctypes.c_void_p),
        ('freelocale', [ctypes.c_void_p], None),
        ('__ctype_get_mb_cur_max', [], ctypes.c_size_t),
        ('nl_langinfo_l', [ctypes.c_int, ctypes.c_void_p], ctypes.c_char_p),
    ]:
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result
    return library


def match_pattern(subject, pattern, *, ctype='C', collate='C', caret_negates=True,
                  collating_symbols=True):
    """Match shell wildcards and POSIX bracket classes without running a shell."""
    import locale
    if any('\0' in value for value in [subject, pattern, ctype, collate]):
        raise ValueError('NUL in shell pattern or locale')
    try:
        library = _pattern_functions()
        ctype_name, collate_name = ctype.encode(), collate.encode()
        selected = library.newlocale(1 << locale.LC_CTYPE, ctype_name, None)
        if not selected:
            raise ValueError('Unavailable shell character locale')
        updated = library.newlocale(1 << locale.LC_COLLATE, collate_name, selected)
        if not updated:
            library.freelocale(selected)
            raise ValueError('Unavailable shell collation locale')
        selected = updated
        previous = library.uselocale(selected)
        if not previous:
            library.freelocale(selected)
            raise ValueError('Cannot select shell pattern locale')
        try:
            # fnmatch can retry a failed multibyte match as bytes. Bash does
            # not: e.g. ?? must not match a single UTF-8 character. Evaluate
            # wildcard positions here and use fnmatch only for one bracket.
            multibyte = library.__ctype_get_mb_cur_max() > 1
            if multibyte:
                encoding = library.nl_langinfo_l(locale.CODESET, selected).decode('ascii')
                if encoding.upper().replace('-', '') != 'UTF8':
                    raise ValueError('Unsupported multibyte shell pattern locale')
            units = [char.encode() for char in subject] if multibyte else [bytes([char]) for char in subject.encode()]
            tokens, index = [], 0
            while index < len(pattern):
                char = pattern[index]
                if char in '*?':
                    tokens.append((char, None))
                    index += 1
                    continue
                if char == '[':
                    end = index + 1
                    if end < len(pattern) and (pattern[end] == '!' or pattern[end] == '^' and caret_negates):
                        end += 1
                    if end < len(pattern) and pattern[end] == ']':
                        end += 1
                    while end < len(pattern) and pattern[end] != ']':
                        if pattern[end] == '\\':
                            end += 2
                        elif (pattern[end] == '[' and end + 1 < len(pattern)
                              and pattern[end + 1] in (':=.' if collating_symbols else ':')):
                            closing = pattern.find(pattern[end + 1] + ']', end + 2)
                            end = closing + 2 if closing >= 0 else end + 1
                        else:
                            end += 1
                    if end < len(pattern):
                        bracket = pattern[index:end + 1]
                        if bracket.startswith('[^'):
                            bracket = ('[!' if caret_negates else '[\\^') + bracket[2:]
                        if not collating_symbols:
                            # Dash recognises [:class:] but treats [.x.] and
                            # [=x=] as ordinary bracket contents.
                            pieces, cursor = [bracket[0]], 1
                            while cursor < len(bracket) - 1:
                                if bracket[cursor] == '\\':
                                    pieces.append(bracket[cursor:cursor + 2])
                                    cursor += 2
                                elif bracket[cursor:cursor + 2] in {'[.', '[='}:
                                    pieces.append('\\[')
                                    cursor += 1
                                else:
                                    pieces.append(bracket[cursor])
                                    cursor += 1
                            bracket = ''.join(pieces) + ']'
                        tokens.append(('bracket', bracket.encode()))
                        index = end + 1
                        continue
                if char == '\\' and index + 1 < len(pattern):
                    index += 1
                    char = pattern[index]
                encoded = char.encode()
                tokens.extend(('literal', value) for value in
                              ([encoded] if multibyte else [bytes([byte]) for byte in encoded]))
                index += 1
            reachable = [True] + [False] * len(units)
            for kind, value in tokens:
                following = [False] * (len(units) + 1)
                if kind == '*':
                    active = False
                    for position in range(len(following)):
                        active = active or reachable[position]
                        following[position] = active
                else:
                    for position, unit in enumerate(units):
                        if not reachable[position]:
                            continue
                        if kind == 'bracket':
                            result = library.fnmatch(value, unit, 0)
                            if result not in {0, 1}:
                                raise ValueError('Unsupported shell bracket pattern')
                            matches = result == 0
                        else:
                            matches = kind == '?' or value == unit
                        following[position + 1] = matches
                reachable = following
            return reachable[-1]
        finally:
            library.uselocale(previous)
            library.freelocale(selected)
    except (AttributeError, OSError, UnicodeError) as error:
        raise ValueError('Unavailable POSIX shell pattern matcher') from error


class ShellSyntaxError(ValueError):
    """Syntax known to be invalid, rather than valid syntax we cannot model."""


class IncompleteShellSyntax(ShellSyntaxError):
    """A command which may become complete after another input line."""


class Word(str):
    def __new__(cls, parts=(), *, quoted=False, protected=(), quoted_parts=()):
        value = ''.join(value if kind == 'literal' else '${' + value + '}' if kind == 'param'
                        else '$(' + value + ')' if kind == 'sub' else '$((' + value + '))'
                        for kind, value in parts)
        obj = super().__new__(cls, value)
        obj.parts, obj.quoted, obj.protected = tuple(parts), quoted, frozenset(protected)
        obj.quoted_parts = frozenset(quoted_parts)
        return obj


_OPERATORS = (';;&', '<<-', '&&', '||', ';;', ';&', '<<', '>>', '>&', '<&', '|&', ';', '&', '|', '(', ')', '<', '>')


def lex(text, *, parameter=False, double_quoted=False, dialect='sh', expand_only=False):
    result, pending, i = [], [], 0

    def end_quote(start, quote, depth):
        if depth > 48:
            raise ValueError('Shell nesting limit reached')
        index = start
        while index < len(text):
            if text[index] == quote:
                return index + 1
            if quote != "'" and text[index] == '\\':
                index += 2
            elif quote == '"' and text.startswith('$(', index):
                index = end_command(index + 2, depth + 1)
            elif quote == '"' and text.startswith('${', index):
                index = end_parameter(index + 2, True, depth + 1)
            elif quote == '"' and text[index] == '`':
                index = end_quote(index + 1, '`', depth + 1)
            else:
                index += 1
        raise IncompleteShellSyntax('Unterminated shell quote')

    def end_command(start, depth=0):
        if depth > 48:
            raise ValueError('Shell nesting limit reached')
        level, index = 1, start
        while index < len(text):
            char = text[index]
            if char in "'\"`":
                index = end_quote(index + 1, char, depth + 1)
            elif char == '\\':
                index += 2
            elif text.startswith('${', index):
                index = end_parameter(index + 2, False, depth + 1)
            elif char == '#' and (index == start or text[index - 1].isspace()):
                end = text.find('\n', index)
                index = len(text) if end < 0 else end
            elif char == '(':
                level += 1
                index += 1
            elif char == ')':
                level -= 1
                index += 1
                if not level:
                    return index
            else:
                index += 1
        raise IncompleteShellSyntax('Unterminated command substitution')

    def end_parameter(start, quoted, depth=0):
        if depth > 48:
            raise ValueError('Shell nesting limit reached')
        index = start
        while index < len(text):
            char = text[index]
            if char == '\\':
                index += 2
            elif text.startswith('$(', index):
                index = end_command(index + 2, depth + 1)
            elif text.startswith('${', index):
                index = end_parameter(index + 2, quoted, depth + 1)
            elif char == '"' or char == "'" and (not quoted or dialect == 'bash') or char == '`':
                index = end_quote(index + 1, char, depth + 1)
            elif char == '}':
                return index + 1
            else:
                index += 1
        raise IncompleteShellSyntax('Unterminated shell parameter')

    def expansion(index, quoted=False):
        if text.startswith('$((', index):
            end = end_command(index + 2)
            if not text[end - 2:end] == '))':
                raise IncompleteShellSyntax('Unterminated shell arithmetic')
            return ('arith', text[index + 3:end - 2]), end
        if text.startswith('$(', index):
            end = end_command(index + 2)
            return ('sub', text[index + 2:end - 1]), end
        if text.startswith('${', index):
            end = end_parameter(index + 2, quoted)
            return ('param', text[index + 2:end - 1]), end
        match = re.match(r'\$([A-Za-z_]\w*|[0-9]|[@*?#$!-])', text[index:])
        if match:
            return ('param', match[1]), index + len(match[0])
        return ('literal', '$'), index + 1

    def word(index, end=None, expand_only=False, parameter=False, in_double=False):
        parts, quoted, protected, quoted_parts = [], False, [], []
        stop = len(text) if end is None else end
        while index < stop:
            char = text[index]
            if not expand_only and not parameter and (char.isspace() or any(text.startswith(op, index) for op in _OPERATORS)):
                break
            part_start = len(parts)
            if char == "'" and not expand_only and not in_double:
                finish = end_quote(index + 1, "'", 0)
                protected.append(len(parts))
                quoted_parts.append(len(parts))
                parts.append(('literal', text[index + 1:finish - 1]))
                quoted, index = True, finish
            elif char == '"' and not expand_only:
                quoted, index = True, index + 1
                quote_start = len(parts)
                while index < stop and text[index] != '"':
                    protected.append(len(parts))
                    quoted_parts.append(len(parts))
                    if text[index] == '$':
                        part, index = expansion(index, quoted=True)
                        parts.append(part)
                    elif text[index] == '`':
                        finish = end_quote(index + 1, '`', 0)
                        parts.append(('sub', text[index + 1:finish - 1]))
                        index = finish
                    elif text[index] == '\\' and index + 1 < stop and text[index + 1] in ('$`"\\\n' + ('}' if parameter and (in_double or dialect != 'bash') else '')):
                        if text[index + 1] != '\n':
                            parts.append(('literal', text[index + 1]))
                        index += 2
                    else:
                        parts.append(('literal', text[index]))
                        index += 1
                if index >= stop:
                    raise IncompleteShellSyntax('Unterminated shell quote')
                if len(parts) == quote_start:
                    protected.append(len(parts))
                    parts.append(('literal', ''))
                index += 1
            elif char == '$':
                part, index = expansion(index, quoted=in_double)
                parts.append(part)
            elif char == '`':
                finish = end_quote(index + 1, '`', 0)
                parts.append(('sub', text[index + 1:finish - 1]))
                index = finish
            elif char == '\\' and index + 1 < stop and (not in_double or text[index + 1] in '$`"\\\n}'):
                if text[index + 1] != '\n':
                    protected.append(len(parts))
                    parts.append(('literal', text[index + 1]))
                index += 2
            else:
                parts.append(('literal', char))
                index += 1
            if in_double:
                protected.extend(range(part_start, len(parts)))
                quoted_parts.extend(range(part_start, len(parts)))
        if in_double and not parts:
            parts.append(('literal', ''))
            protected.append(0)
        quoted = quoted or in_double
        return Word(parts, quoted=quoted, protected=protected, quoted_parts=quoted_parts), index

    if expand_only:
        # Startup filenames are expanded as strings: quotes and whitespace
        # are filename data, while nested substitutions retain shell syntax.
        return [word(0, expand_only=True, in_double=True)[0]]

    if parameter:
        # The word in ${name:-word} is expansion syntax, not a command.
        # Its spaces, comments and operators are data; quotes inherit the
        # surrounding parameter expansion's double-quote context.
        operand = word(0, parameter=True, in_double=double_quoted)[0]
        operand.parameter_quoted = double_quoted
        return [operand]

    while i < len(text):
        if text[i] in ' \t\r':
            i += 1
            continue
        if text[i] == '#':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
            continue
        if text[i] == '\n':
            i += 1
            for delimiter, strip_tabs, quoted in pending:
                start, lines = i, []
                while i < len(text):
                    end = text.find('\n', i)
                    end = len(text) if end < 0 else end + 1
                    line = text[i:end]
                    i = end
                    if (line.lstrip('\t') if strip_tabs else line).rstrip('\r\n') == delimiter:
                        break
                    lines.append(line.lstrip('\t') if strip_tabs else line)
                else:
                    raise ValueError('Unterminated shell heredoc')
                body = ''.join(lines)
                if quoted:
                    result.append(Word((('literal', body),), quoted=True))
                else:
                    # Reuse expansion-aware word lexing over just the body;
                    # quote characters in a heredoc are literal characters.
                    saved = text
                    text = body
                    expanded, _ = word(0, expand_only=True)
                    text = saved
                    result.append(expanded)
            pending.clear()
            result.append('\n')
            continue
        operator = next((op for op in _OPERATORS if text.startswith(op, i)), None)
        if operator:
            result.append(operator)
            i += len(operator)
            continue
        if text[i] in '{}' and (i + 1 == len(text) or text[i + 1].isspace() or text[i + 1] in ';()'):
            result.append(text[i])
            i += 1
            continue
        item, end = word(i)
        if end == i:
            raise ValueError('Unsupported shell token')
        if result and result[-1] in {'<<', '<<-'} and not isinstance(result[-1], Word):
            pending.append((str(item), result[-1] == '<<-', item.quoted))
        result.append(item)
        i = end
    if pending:
        raise ValueError('Missing shell heredoc body')
    return result


def parse(text, *, dialect='sh', _words=None):
    words, cursor, nesting = lex(text, dialect=dialect) if _words is None else _words, 0, 0
    unmodelled_compound = False

    def at(value):
        return cursor < len(words) and words[cursor] == value and (not isinstance(words[cursor], Word) or not words[cursor].quoted and not words[cursor].protected)

    def consume(value, *, required=False):
        nonlocal cursor
        if not at(value):
            if cursor == len(words):
                raise IncompleteShellSyntax('Expected shell token ' + value)
            if required:
                raise ShellSyntaxError('Expected shell token ' + value)
            raise ValueError('Expected shell token ' + value)
        cursor += 1

    def separators():
        nonlocal cursor
        if at(';') or at('&'):
            cursor += 1
        while at('\n'):
            cursor += 1
        if at(';') or at('&'):
            raise ShellSyntaxError('Unexpected shell separator')

    def sequence(stops=(), *, required=False):
        nonlocal cursor
        nodes = []
        while at('\n'):
            cursor += 1
        if at(';') or at('&'):
            raise ShellSyntaxError('Unexpected shell separator')
        while cursor < len(words) and not any(at(stop) for stop in stops):
            before = cursor
            node = pipeline()
            while at('&&') or at('||'):
                operator = words[cursor]
                cursor += 1
                while at('\n'):
                    cursor += 1
                node = (operator, node, pipeline())
            nodes.append(node)
            if cursor == before:
                raise ValueError('Shell parser made no progress')
            separators()
        if required and not nodes:
            if cursor == len(words):
                raise IncompleteShellSyntax('Missing shell command list')
            raise ShellSyntaxError('Empty shell command list')
        return ('seq', nodes)

    def pipeline():
        nonlocal cursor
        if dialect == 'bash' and at('time'):
            cursor += 1
            if at('-p'):
                cursor += 1
            if at('--'):
                cursor += 1
            if cursor == len(words) or any(at(token) for token in {';', '\n', '&', '&&', '||', ')', '}'}):
                return ('time', ('seq', []))
            timed = pipeline()
            return ('time', timed)
        # ! is reserved syntax only at the start of a pipeline. Quoted or
        # escaped words, and a later expansion to !, remain command names.
        negate = at('!') and not words[cursor].protected
        if negate:
            cursor += 1
            if dialect == 'bash' and at('time'):
                return ('not', pipeline())
        nodes = [statement()]
        while at('|') or at('|&'):
            cursor += 1
            while at('\n'):
                cursor += 1
            nodes.append(statement())
        node = nodes[0] if len(nodes) == 1 else ('pipe', nodes)
        return ('not', node) if negate else node

    def group():
        nonlocal nesting
        nesting += 1
        if nesting > 48:
            raise ValueError('Shell parser nesting limit reached')
        opening = words[cursor]
        consume(opening)
        body = sequence({')' if opening == '(' else '}'}, required=True)
        consume(')' if opening == '(' else '}')
        nesting -= 1
        return ('subshell' if opening == '(' else 'group', body)

    def branch():
        nonlocal cursor
        condition = sequence({'then'}, required=True)
        consume('then')
        yes = sequence({'elif', 'else', 'fi'}, required=True)
        no = ('seq', [])
        if at('elif'):
            cursor += 1
            no = branch()
        elif at('else'):
            cursor += 1
            no = sequence({'fi'}, required=True)
            consume('fi')
        else:
            consume('fi')
        return ('if', condition, yes, no)

    def statement():
        nonlocal cursor, unmodelled_compound
        if cursor == len(words):
            raise IncompleteShellSyntax('Missing shell statement')
        if any(at(token) for token in {'then', 'elif', 'else', 'fi', 'do', 'done', 'esac'}):
            raise ShellSyntaxError('Unexpected shell reserved word')
        if dialect == 'bash' and any(at(token) for token in {'select', '[['}):
            # These prefixes can change the grammar of later tokens. A
            # failed parse in that context is not proof of a syntax error.
            unmodelled_compound = True
        if dialect == 'bash' and at('(') and cursor + 1 < len(words) and words[cursor + 1] == '(':
            raise ValueError('Unsupported shell arithmetic command')
        if dialect == 'bash' and at('coproc'):
            cursor += 1
            if cursor == len(words):
                raise IncompleteShellSyntax('Missing coprocess command')
            if at('\n'):
                raise ShellSyntaxError('Missing coprocess command')
            name = 'COPROC'
            compound_starts = {'{', '(', 'if', 'for', 'while', 'until', 'case'}
            following = words[cursor + 1] if cursor + 1 < len(words) else None
            if (isinstance(words[cursor], Word) and following in compound_starts
                    and (not isinstance(following, Word) or not following.quoted and not following.protected)):
                name = str(words[cursor])
                if words[cursor].quoted or not re.fullmatch(r'[A-Za-z_]\w*', name):
                    raise ValueError('Unsupported coprocess name')
                cursor += 1
            child = statement()
            return ('coproc', child, name)
        if at('(') or at('{'):
            return group()
        if at('if'):
            cursor += 1
            return branch()
        if at('case'):
            cursor += 1
            if cursor >= len(words):
                raise IncompleteShellSyntax('Missing case subject')
            if not isinstance(words[cursor], Word):
                raise ShellSyntaxError('Invalid case subject')
            subject = words[cursor]
            cursor += 1
            while at('\n'):
                cursor += 1
            consume('in', required=True)
            while at('\n'):
                cursor += 1
            arms = []
            while not at('esac'):
                if at('('):
                    cursor += 1
                patterns = []
                while cursor < len(words) and not at(')'):
                    if not at('|'):
                        patterns.append(words[cursor])
                    cursor += 1
                consume(')')
                body = sequence({';;', ';&', ';;&', 'esac'})
                arms.append((patterns, body))
                if not at('esac'):
                    # Fall-through requires a separate execution model.
                    consume(';;')
                while at('\n'):
                    cursor += 1
            consume('esac')
            return ('case', subject, arms)
        if at('for'):
            cursor += 1
            if cursor >= len(words):
                raise IncompleteShellSyntax('Missing for loop variable')
            if at('('):
                raise ValueError('Unsupported for loop')
            if not isinstance(words[cursor], Word) or words[cursor].quoted or not re.fullmatch(r'[A-Za-z_]\w*', words[cursor]):
                raise ShellSyntaxError('Invalid for loop variable')
            name = str(words[cursor])
            cursor += 1
            name_linebreak = at('\n')
            while at('\n'):
                cursor += 1
            values = None
            if at('in'):
                cursor += 1
                values = []
                while cursor < len(words) and not at(';') and not at('\n'):
                    values.append(words[cursor])
                    cursor += 1
            elif name_linebreak and at(';'):
                raise ShellSyntaxError('Unexpected separator after for variable')
            separators()
            consume('do', required=True)
            body = sequence({'done'}, required=True)
            consume('done')
            return ('for', name, values, body)
        if at('while') or at('until'):
            kind = str(words[cursor])
            cursor += 1
            condition = sequence({'do'}, required=True)
            consume('do')
            body = sequence({'done'}, required=True)
            consume('done')
            return (kind, condition, body)
        function_keyword = at('function')
        if function_keyword:
            cursor += 1
        if cursor < len(words) and isinstance(words[cursor], Word) and not words[cursor].quoted and re.fullmatch(r'[A-Za-z_]\w*', words[cursor]):
            if function_keyword or cursor + 2 < len(words) and words[cursor + 1:cursor + 3] == ['(', ')']:
                name = str(words[cursor])
                cursor += 1
                if at('('):
                    cursor += 1
                    consume(')')
                while at('\n'):
                    cursor += 1
                if not at('{') and not at('('):
                    if cursor == len(words):
                        raise IncompleteShellSyntax('Missing function body')
                    raise ValueError('Missing function body')
                return ('function', name, group())
        start = cursor
        while cursor < len(words) and not any(at(stop) for stop in {';', '\n', '&', '&&', '||', '|', '|&', ')', '}', ';;', ';&', ';;&'}):
            cursor += 1
        if cursor == start:
            if any(at(token) for token in {';', ';;', ';&', ';;&', '&', '&&', '||', '|', '|&'}):
                raise ShellSyntaxError('Unexpected shell token')
            raise ValueError('Unexpected or unsupported shell token')
        return ('cmd', words[start:cursor])

    try:
        program = sequence()
    except ShellSyntaxError as error:
        if unmodelled_compound:
            raise ValueError('Unsupported shell compound syntax') from error
        raise
    if cursor != len(words):
        raise ValueError('Unconsumed shell syntax')
    return program


def programs(text, *, dialect='sh'):
    """Read complete input lines, retaining commands before a later error.

    A semicolon does not end a read unit: Bash checks the whole logical line
    before executing it. Multiline groups, quotes, substitutions, heredocs and
    backslash continuations must all finish before a unit can be evaluated.
    Unsupported valid syntax remains ValueError, never a recoverable error.
    """
    pending = ''
    for line in re.finditer(r'[^\n]*\n|[^\n]+$', text):
        pending += line[0]
        try:
            words = lex(pending, dialect=dialect)
            program = parse(pending, dialect=dialect, _words=words)
        except IncompleteShellSyntax:
            continue
        except ValueError as error:
            if str(error) in {'Missing shell heredoc body', 'Unterminated shell heredoc'}:
                continue
            raise
        if pending.endswith('\n') and words and words[-1] != '\n':
            continue
        if program[1]:
            yield program
        pending = ''
    if pending:
        program = parse(pending, dialect=dialect)
        if program[1]:
            yield program
