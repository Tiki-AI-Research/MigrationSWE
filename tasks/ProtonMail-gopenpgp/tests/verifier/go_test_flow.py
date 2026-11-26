"""Conservative, non-executing reachability checks for Go test assertions.

Keep the existing assertion-strength checks, and additionally preserve the
paths on which baseline test functions and their checking helpers run checks.
This is a structural safeguard, not a proof of arbitrary Go test equivalence.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

try:
    from .go_source import GoSource, _unquote
except ImportError:
    from go_source import GoSource, _unquote

_TESTIFY_FUNCTIONS = set(('Equal EqualValues EqualExportedValues Exactly NotEqual NotEqualValues '
                         'True False Nil NotNil Zero NotZero Empty NotEmpty Len Contains NotContains '
                         'Subset NotSubset ElementsMatch NotElementsMatch Same NotSame IsType '
                         'Implements Error NoError ErrorIs NotErrorIs ErrorAs NotErrorAs EqualError '
                         'ErrorContains Regexp NotRegexp Greater GreaterOrEqual Less LessOrEqual '
                         'Positive Negative InDelta InDeltaSlice InDeltaMapValues InEpsilon '
                         'InEpsilonSlice JSONEq YAMLEq Panics NotPanics PanicsWithValue PanicsWithError '
                         'Eventually EventuallyWithT Never WithinDuration WithinRange Condition '
                         'FileExists NoFileExists DirExists NoDirExists HTTPBodyContains '
                         'HTTPBodyNotContains HTTPError HTTPRedirect HTTPSuccess Fail FailNow').split())
_TESTIFY_FUNCTIONS |= {name + 'f' for name in _TESTIFY_FUNCTIONS}


# Message/format arguments do not alter the value being checked.
_UNARY_ASSERTIONS = set(('True False Nil NotNil Zero NotZero Empty NotEmpty Error NoError '
                         'Positive Negative Panics NotPanics FileExists NoFileExists '
                         'DirExists NoDirExists Condition').split())
_ASSERTION_ARITY = {name: 1 for name in _UNARY_ASSERTIONS}
_ASSERTION_ARITY.update(Fail=0, FailNow=0, HTTPError=4, HTTPRedirect=4, HTTPSuccess=4,
                        HTTPBodyContains=5, HTTPBodyNotContains=5)
_ASSERTION_ARITY.update({name: 3 for name in (
    'InDelta', 'InDeltaSlice', 'InDeltaMapValues', 'InEpsilon', 'InEpsilonSlice',
    'WithinDuration', 'WithinRange', 'Eventually', 'EventuallyWithT', 'Never')})


def _assertion_arity(kind):
    if kind.endswith('f'):
        kind = kind[:-1]
    return _ASSERTION_ARITY.get(kind, 2)


def canonical_tokens(text):
    source = GoSource(text)
    items = list(source.tokens)
    # Resolve assertion aliases per file; diagnostic strings are single tokens.
    for alias, package in source.imports:
        if package not in {"github.com/stretchr/testify/assert", "github.com/stretchr/testify/require"}:
            continue
        canonical = package.rsplit("/", 1)[-1]
        symbols = _TESTIFY_FUNCTIONS if alias == '.' else {item for item in items if item.isidentifier()}
        for start, end in source.references(package, symbols):
            if alias == '.':
                items[start] = canonical + '.' + items[start]
            else:
                items[start] = canonical
    # Parameter types must be recognized through testing import aliases too.
    for alias, package in source.imports:
        if package == 'testing':
            for start, _ in source.references(package, {'T', 'B', 'TB'}):
                items[start] = 'testing.' + items[start] if alias == '.' else 'testing'
    return items


def _pairs(items):
    pairs, stack = {}, []
    closing = {')': '(', ']': '[', '}': '{'}
    for index, item in enumerate(items):
        if item in {'(', '[', '{'}:
            stack.append((item, index))
        elif item in closing and stack and stack[-1][0] == closing[item]:
            _, opening = stack.pop()
            pairs[opening] = index
    return pairs


def _definitions(items, pairs):
    functions, index = {}, 0
    while index < len(items):
        if items[index] != 'func':
            index += 1
            continue
        cursor, receiver = index + 1, ''
        if cursor < len(items) and items[cursor] == '(':
            end = pairs.get(cursor, cursor)
            names = [t for t in items[cursor + 1:end] if t.isidentifier()]
            receiver = (names[-1] + '.') if names else ''
            cursor = end + 1
        if cursor + 1 >= len(items) or not items[cursor].isidentifier() or items[cursor + 1] != '(':
            index += 1
            continue
        name = receiver + items[cursor]
        parameters = cursor + 1
        cursor = pairs.get(parameters, parameters) + 1
        signature_end = cursor
        cursor = _signature_body(items, pairs, cursor, len(items))
        if cursor not in pairs:
            break
        functions[name] = (index, parameters, signature_end, cursor, pairs[cursor])
        index = pairs[cursor] + 1
    return functions


def _functions(items, pairs):
    return {name: (definition[3] + 1, definition[4])
            for name, definition in _definitions(items, pairs).items()}


def _segments(items, start, end, pairs, delimiter=','):
    result, first, cursor = [], start, start
    while cursor < end:
        if items[cursor] == delimiter:
            result.append((first, cursor))
            first = cursor + 1
        elif items[cursor] in {'(', '[', '{'} and cursor in pairs:
            cursor = pairs[cursor]
        cursor += 1
    return result + [(first, end)]


def _parameters(items, start, end, pairs):
    pending, result = [], []
    for first, last in _segments(items, start, end, pairs):
        if first >= last or not items[first].isidentifier():
            continue
        if first + 1 < last and items[first + 1] == '.':  # An unnamed qualified type.
            continue
        pending.append(first)
        if first + 1 < last:
            result.extend((position, items[first + 1:last]) for position in pending)
            pending = []
    return result


def _type_tables(items, pairs, functions):
    structs, globals_ = {}, {}
    # Keep definitions and aliases separate: both have an underlying storage
    # kind, but a defined type still matters to an assertion such as Exactly.
    for index, token in enumerate(items):
        if token != 'type' or index + 2 >= len(items):
            continue
        specs = (_segments(items, index + 2, pairs[index + 1], pairs, ';')
                 if items[index + 1] == '(' and index + 1 in pairs else
                 [(index + 1, next((n for n in range(index + 2, len(items))
                                   if items[n] == ';'), len(items)))])
        for first, last in specs:
            if first + 1 >= last or not items[first].isidentifier():
                continue
            alias = items[first + 1] == '='
            kind = items[first + 2 if alias else first + 1:last]
            if kind and kind[0] not in {'struct', 'interface'}:
                globals_['type:' + items[first]] = kind
                globals_['alias:' + items[first]] = alias
    for index in range(len(items) - 3):
        if items[index] != 'type' or items[index + 2:index + 4] != ['struct', '{']:
            continue
        opening = index + 3
        if opening in pairs:
            fields = {}
            for first, last in _segments(items, opening + 1, pairs[opening], pairs, ';'):
                for position, kind in _parameters(items, first, last, pairs):
                    fields[items[position]] = kind
            structs[items[index + 1]] = fields
    # Anonymous holders can also carry method receivers. Key their structural
    # type by its complete spelling, including its field declaration braces.
    for index in range(len(items) - 1):
        if items[index:index + 2] == ['struct', '{'] and index + 1 in pairs:
            closing = pairs[index + 1]
            fields = {items[position]: kind for first, last in _segments(items, index + 2, closing, pairs, ';')
                      for position, kind in _parameters(items, first, last, pairs)}
            structs[''.join(items[index:closing + 1])] = fields
    for index, token in enumerate(items):
        if token != 'var' or any(first <= index < last for first, last in functions.values()):
            continue
        first = index + 1
        if first < len(items) and items[first] == '(' and first in pairs:
            specs = _segments(items, first + 1, pairs[first], pairs, ';')
        else:
            last = first
            while last < len(items) and items[last] != ';':
                if last in pairs:
                    last = pairs[last]
                last += 1
            specs = [(first, last)]
        for first, last in specs:
            assignment = next((n for n in range(first, last) if items[n] == '='), last)
            for position, kind in _parameters(items, first, assignment, pairs):
                globals_[items[position]] = kind
    return structs, globals_


def _underlying_type(kind, globals_, seen=frozenset()):
    if len(kind) == 1 and kind[0] not in seen and 'type:' + kind[0] in globals_:
        return _underlying_type(globals_['type:' + kind[0]], globals_, seen | {kind[0]})
    if kind[:1] == ['*']:
        return ['*'] + _underlying_type(kind[1:], globals_, seen)
    return kind


def _element_type(kind):
    opening = 1 if kind[:1] == ['map'] else 0
    closing = _pairs(kind).get(opening)
    return kind[closing + 1:] if closing is not None else []


def _key_path(value):
    # Map keys may contain '/' and whitespace; neither is a path delimiter.
    return 'key:' + json.dumps(value, separators=(',', ':')).encode().hex()


def _result_fields(tokens):
    if not tokens:
        return []
    if tokens[0] != '(':
        return [(None, tokens)]
    pairs, result, pending = _pairs(tokens), [], []
    for a, b in _segments(tokens, 1, len(tokens) - 1, pairs):
        part = tokens[a:b]
        if not part:
            continue
        if (len(part) > 1 and part[0].isidentifier() and part[0] not in {'map', 'chan', 'func', 'struct', 'interface'}
                and part[1] != '.'):
            pending.append(part[0])
            result.extend((name, part[1:]) for name in pending)
            pending = []
        else:
            if len(part) == 1 and part[0].isidentifier():
                pending.append(part[0])
                continue
            result.extend((None, [name]) for name in pending)
            pending = []
            result.append((None, part))
    result.extend((None, [name]) for name in pending)
    return result


def _expression_tokens(items):
    """Remove redundant primary-expression groups without changing precedence."""
    items = _unparenthesized(list(items))
    pairs = _pairs(items)

    def primary(tokens):
        if not tokens or not (tokens[0].isidentifier() or ':' in tokens[0]
                              or tokens[0].startswith(('"', "'", '`')) or tokens[0][0].isdigit()):
            return False
        nested, cursor = _pairs(tokens), 1
        while cursor < len(tokens):
            if tokens[cursor] == '.' and cursor + 1 < len(tokens) and tokens[cursor + 1].isidentifier():
                cursor += 2
            elif tokens[cursor] in {'(', '['} and cursor in nested:
                cursor = nested[cursor] + 1
            else:
                return False
        return True

    result, cursor = [], 0
    while cursor < len(items):
        token = items[cursor]
        if token not in {'(', '['} or cursor not in pairs:
            result.append(token)
            cursor += 1
            continue
        closing = pairs[cursor]
        # Argument/index delimiters are not grouping parentheses.
        previous = items[cursor - 1] if cursor else ''
        call = previous and (previous.isidentifier() or ':' in previous or previous in {')', ']'})
        inner = items[cursor + 1:closing]
        if token == '(' and not call:
            normalized = _expression_tokens(inner)
            result.extend(normalized if primary(normalized) else ['('] + normalized + [')'])
        else:
            parts = _segments(items, cursor + 1, closing, pairs)
            result.append(token)
            for number, (first, last) in enumerate(parts):
                if number:
                    result.append(',')
                result.extend(_expression_tokens(items[first:last]))
            result.append(')' if token == '(' else ']')
        cursor = closing + 1
    return result


def _value_type(value, parameters, structs, globals_, sources, seen=frozenset()):
    return _underlying_type(_value_type_raw(value, parameters, structs, globals_, sources, seen), globals_)


def _value_type_raw(value, parameters, structs, globals_, sources, seen=frozenset()):
    value = _expression_tokens(value)
    if not value:
        return []
    if len(value) == 1:
        token = value[0]
        if token in parameters:
            return parameters[token]
        if token in globals_:
            return globals_[token]
        if 'type:' + token in globals_:
            return [token]
        if token in sources and token not in seen:
            return _value_type(sources[token], parameters, structs, globals_, sources, seen | {token})
        return [token] if token in structs else []
    if value[0] == 'store:':
        return _value_type(value[1:2], parameters, structs, globals_, sources, seen)
    if value[0] == 'zero:':
        return value[1:]
    if value[0] == '&':
        kind = _value_type(value[1:], parameters, structs, globals_, sources, seen)
        return ['*'] + kind if kind else []
    if value[0] == '*':
        kind = _value_type(value[1:], parameters, structs, globals_, sources, seen)
        return kind[1:] if kind[:1] == ['*'] else []
    if value[-1:] == [']']:
        pairs = _pairs(value)
        opening = next((a for a, b in pairs.items() if value[a] == '[' and b == len(value) - 1), None)
        if opening is not None and opening > 0:
            kind = _value_type(value[:opening], parameters, structs, globals_, sources, seen)
            if ':' in value[opening + 1:-1]:
                closing = _pairs(kind).get(0) if kind[:1] == ['['] else None
                return ['[', ']'] + kind[closing + 1:] if closing is not None and closing > 1 else kind
            if kind[:1] in (['['], ['map']):
                return _element_type(kind)
    if value[:2] == ['new', '('] and _pairs(value).get(1) == len(value) - 1:
        return ['*'] + value[2:-1]
    if value[:2] == ['make', '('] and _pairs(value).get(1) == len(value) - 1:
        first = _segments(value, 2, len(value) - 1, _pairs(value))[0]
        return value[slice(*first)]
    if value[:2] == ['append', '('] and _pairs(value).get(1) == len(value) - 1:
        first = _segments(value, 2, len(value) - 1, _pairs(value))[0]
        return _value_type_raw(value[slice(*first)], parameters, structs, globals_, sources, seen)
    if value[-1:] == [')']:
        opening = next((a for a, b in _pairs(value).items() if a > 0 and value[a] == '('
                        and b == len(value) - 1), None)
        if opening is not None:
            target = _expand_value(value[:opening], sources)
            if len(target) == 1 and 'type:' + target[0] in globals_:
                return [target[0]]
            if target[:1] in (['['], ['map']):
                return target
            returned = globals_.get('return:' + ''.join(target))
            if returned:
                return returned
    if '{' in value:
        if value[:2] == ['struct', '{'] and 1 in _pairs(value):
            return value[:_pairs(value)[1] + 1]
        return value[:value.index('{')]
    if len(value) % 2 == 1 and all(value[index] == '.' for index in range(1, len(value), 2)):
        kind = _value_type(value[:1], parameters, structs, globals_, sources, seen)
        for index in range(2, len(value), 2):
            kind = structs.get(''.join(kind).lstrip('*'), {}).get(value[index], [])
        return kind
    return []


# Keywords cannot be the primary expression immediately preceding call args.
_NON_CALLS = set(('func if for switch select return go defer range case type '
                  'var const struct interface map chan else').split())


def _primary_name(token):
    return all(part.isidentifier() for part in token.split('.'))


def _type_end(items, pairs, first, last):
    """Consume one Go type, including braces belonging to a result type."""
    if first >= last:
        return first
    token = items[first]
    if items[first:first + 2] == ['<', '-']:
        return _type_end(items, pairs, first + 2, last)
    if token in {'*', '<-'}:
        return _type_end(items, pairs, first + 1, last)
    if token == 'chan':
        following = first + 1
        following += 2 if items[following:following + 2] == ['<', '-'] else 1 if items[following:following + 1] == ['<-'] else 0
        return _type_end(items, pairs, following, last)
    if token in {'[', '('} and first in pairs:
        return (_type_end(items, pairs, pairs[first] + 1, last) if token == '[' else pairs[first] + 1)
    if token == 'map' and first + 1 in pairs:
        return _type_end(items, pairs, pairs[first + 1] + 1, last)
    if token in {'struct', 'interface'} and items[first + 1:first + 2] == ['{'] and first + 1 in pairs:
        return pairs[first + 1] + 1
    if token == 'func' and items[first + 1:first + 2] == ['('] and first + 1 in pairs:
        following = pairs[first + 1] + 1
        return (_type_end(items, pairs, following, last)
                if following < last and items[following] not in {'{', ',', ';', ')', ']', '}'} else following)
    if token.isidentifier() and token not in _NON_CALLS:
        following = first + 1
        if items[following:following + 1] == ['.']:
            following += 2
        if items[following:following + 1] == ['['] and following in pairs:
            following = pairs[following] + 1
        return following
    return first


def _signature_body(items, pairs, first, last):
    body = first if items[first:first + 1] == ['{'] else _type_end(items, pairs, first, last)
    return body if body < last and items[body] == '{' and body in pairs else None


def _function_literal_body(items, pairs, first, last):
    parameters = first + 1
    if items[parameters:parameters + 1] != ['('] or parameters not in pairs:
        return None
    return _signature_body(items, pairs, pairs[parameters] + 1, last)


def _control_body(items, pairs, first, last):
    """Skip complete header expressions, including literal types and bodies."""
    cursor = first
    while cursor < last:
        token = items[cursor]
        if token == 'func':
            body = _function_literal_body(items, pairs, cursor, last)
            if body is not None:
                cursor = pairs[body] + 1
                continue
        type_start = (cursor == first or items[cursor - 1] in {
            'range', '=', ':=', ',', '(', '&', 'return', '==', '!=', '&&', '||'})
        if token in {'map', 'struct'} or token == '[' and type_start:
            following = _type_end(items, pairs, cursor, last)
            if following in pairs and items[following] == '{':
                cursor = pairs[following] + 1
                continue
        if token == '{':
            return cursor
        cursor = pairs.get(cursor, cursor) + 1
    return last


def _literal_invocation(items, pairs, first, last):
    """A literal primary followed by call arguments; groups wrap only the literal."""
    cursor = first
    while cursor < last and items[cursor] == '(':
        cursor += 1
    if items[cursor:cursor + 1] != ['func']:
        return None
    body = _function_literal_body(items, pairs, cursor, last)
    if body is None:
        return None
    call = pairs[body] + 1
    for group in range(cursor - 1, first - 1, -1):
        if pairs.get(group) != call:
            return None
        call += 1
    if call < last and items[call] == '(' and call in pairs:
        return cursor + 1, body, pairs[body], call
    return None


def _literal_start(items, pairs, cursor, first, body):
    callee, following = cursor, pairs.get(body, -2) + 1
    while callee > first and items[callee - 1] == '(' and pairs.get(callee - 1) == following:
        callee, following = callee - 1, following + 1
    return callee


def _primary_calls(items, pairs, first, last, *, skip_literals=True):
    """Read primary-expression callees, including grouped receivers/selectors."""
    reverse = {closing: opening for opening, closing in pairs.items()}

    def start(end):
        if end <= first:
            return end
        token = items[end - 1]
        if token in {')', ']'} and end - 1 in reverse:
            opening = reverse[end - 1]
            previous = items[opening - 1] if opening > first else ''
            if previous and previous not in _NON_CALLS and (
                    _primary_name(previous) or previous in {')', ']'}):
                return start(opening)
            return opening
        if _primary_name(token):
            if end >= first + 3 and items[end - 2] == '.':
                return start(end - 2)
            if end >= first + 2 and items[end - 2] == ']':
                types = [n for n in range(first, end - 1) if items[n] in {'[', 'map'} and
                         _type_end(items, pairs, n, end) == end]
                if types:
                    return min(types)
            return end - 1
        return end

    cursor, emitted = first, set()
    while cursor < last:
        if skip_literals and items[cursor] == 'func':
            body = _function_literal_body(items, pairs, cursor, last)
            callee = _literal_start(items, pairs, cursor, first, body)
            literal = _literal_invocation(items, pairs, callee, last)
            if literal is not None:
                emitted.add(literal[3])
                yield callee, literal[3]
            cursor = pairs[body] + 1 if body is not None else cursor + 1
            continue
        if items[cursor] == '(' and cursor in pairs and cursor > first and cursor not in emitted:
            previous = items[cursor - 1]
            if previous not in _NON_CALLS and (_primary_name(previous) or previous in {')', ']'}):
                opening = start(cursor)
                if opening < cursor:
                    yield opening, cursor
        cursor += 1


def _call_guards(items, pairs, first, last, position):
    """Read short-circuit guards enclosing a call, without entering literals."""
    if not any(token in {'&&', '||'} for token in items[first:last]):
        return []
    cursor = first
    while cursor < last:
        if items[cursor] in {'=', ':='}:
            first = cursor + 1
        cursor = pairs.get(cursor, cursor) + 1
    if items[first:first + 1] == ['return']:
        first += 1
    operators = []
    cursor = first
    while cursor < last:
        if items[cursor] in {'&&', '||'}:
            operators.append(cursor)
        cursor = pairs.get(cursor, cursor) + 1
    if operators:
        split = next((n for n in reversed(operators) if items[n] == '||'), operators[-1])
        if position > split:
            return [(first, split, items[split] == '&&')] + _call_guards(items, pairs, split + 1, last, position)
        return _call_guards(items, pairs, first, split, position)
    for opening, closing in sorted(pairs.items()):
        if first <= opening < position < closing < last and items[opening] in {'(', '['}:
            for a, b in _segments(items, opening + 1, closing, pairs):
                if a <= position < b:
                    return _call_guards(items, pairs, a, b, position)
    return []


def _reference(value, sources, origins, seen=frozenset()):
    value = _expression_tokens(value)
    if len(value) == 1 and value[0] in sources and value[0] not in seen:
        return _reference(origins.get(value[0], sources[value[0]]), sources, origins, seen | {value[0]})
    return value


def _expand_value(value, sources, seen=frozenset()):
    value = _expression_tokens(value)
    if len(value) == 1 and value[0] in sources and value[0] not in seen:
        return _expand_value(sources[value[0]], sources, seen | {value[0]})
    return value


def _constant_value(value, sources, seen=frozenset()):
    expanded = []
    for token in value:
        if token in sources and token not in seen:
            source = sources[token]
            constant = _constant_value(source, sources, seen | {token})
            expanded += ['('] + source + [')'] if constant is not None else [token]
        else:
            expanded.append(token)
    # Resolve nested arithmetic bindings as well as a direct scalar alias.
    if expanded != value:
        return _constant_value(expanded, sources, seen | set(value))
    return _constant_operand(expanded)


def _panic_helpers(items, pairs):
    stopped = {'panic'}
    functions = _functions(items, pairs)
    for _ in range(len(functions) + 1):
        previous = set(stopped)
        for name, (first, last) in functions.items():
            if 'recover' in items[first:last]:
                continue
            cursor = first
            while cursor < last:
                if items[cursor] == ';':
                    cursor += 1
                    continue
                if items[cursor] in {'if', 'for', 'switch', 'select', 'return', 'goto'}:
                    break
                end = cursor
                while end < last and items[end] != ';':
                    end = pairs.get(end, end) + 1
                if items[cursor] not in {'defer', 'go'} and any(
                        ''.join(items[a:b]) in stopped and not _call_guards(items, pairs, cursor, end, a)
                        for a, b in _primary_calls(items, pairs, cursor, end)):
                    stopped.add(name)
                    break
                cursor = end + 1
        if previous == stopped:
            break
    return stopped


def _recovers(items, pairs, first, last):
    """Recognize an unconditionally registered, directly recovering defer."""
    cursor = first
    while cursor < last:
        if items[cursor] == ';':
            cursor += 1
            continue
        if items[cursor] in {'if', 'for', 'switch', 'select', 'return', 'goto'}:
            return False
        end = cursor
        while end < last and items[end] != ';':
            end = pairs.get(end, end) + 1
        literal = _deferred_literal(items, pairs, cursor, end) if items[cursor] == 'defer' else None
        if literal is not None:
            body, closing = literal[1:3]
            for a, b in _segments(items, body + 1, closing, pairs, ';'):
                if items[a:a + 1] in (['if'], ['for'], ['switch'], ['return']):
                    break
                if any(items[c:d] == ['recover'] for c, d in _primary_calls(items, pairs, a, b)):
                    return True
        cursor = end + 1
    return False


def _literal_elements(value, sources):
    value = _expand_value(value, sources)
    if value[:1] == ['zero:'] and value[1:2] in (['['], ['map']):
        return _literal_elements(value[1:] + ['{', '}'], sources) if value[1:2] == ['['] else []
    pairs = _pairs(value)
    opening = next((n for n, token in enumerate(value) if token == '{'), None)
    if opening is None or pairs.get(opening) != len(value) - 1 or value[:1] not in (['['], ['map']):
        return None
    elements = [value[a:b] for a, b in _segments(value, opening + 1, len(value) - 1, pairs) if a < b]
    if any(len(_segments(element, 0, len(element), _pairs(element), ':')) > 1 for element in elements):
        return None
    if value[:1] == ['['] and pairs.get(0, 0) > 1:
        closing = pairs[0]
        bound = _constant_operand(value[1:closing])
        size = len(elements) if value[1:closing] == ['...'] else bound[1] if bound and bound[0] == 'integer' else None
        if size is None or not len(elements) <= size <= 64:
            return None
        kind = value[closing + 1:opening]
        zero = ['false'] if kind == ['bool'] else ['""'] if kind == ['string'] else ['0'] if len(kind) == 1 and kind[0] in {
            'int', 'int8', 'int16', 'int32', 'int64', 'uint', 'uint8', 'uint16', 'uint32', 'uint64',
            'uintptr', 'byte', 'rune', 'float32', 'float64', 'complex64', 'complex128'} else ['zero-element:'] + kind
        elements += [zero] * (size - len(elements))
    return elements


def _known_empty(value, sources):
    value = _expand_value(value, sources)
    return value == ['nil'] or _literal_elements(value, sources) == [] or _constant_operand(value) == ('string', '')


def _empty_slice(value):
    pairs = _pairs(value)
    opening = next((a for a, b in pairs.items() if value[a] == '[' and b == len(value) - 1), None)
    if opening is None or ':' not in value[opening + 1:-1]:
        return False
    spans = _segments(value, opening + 1, len(value) - 1, pairs, ':')
    lower = _constant_operand(value[slice(*spans[0])] or ['0'])
    upper = _constant_operand(value[slice(*spans[1])])
    return lower is not None and lower[0] == 'integer' and lower == upper


def _project_value(value, path, sources, structs, references=frozenset()):
    """Resolve constructor/zero fields without inventing a testing context."""
    if not path and len(value) == 1 and value[0] in references:
        return value
    value = _expand_value(value, sources)
    while value and value[0] in {'&', '*'}:
        value = _expand_value(value[1:], sources)
    if not path:
        return value
    field, rest = path[0], path[1:]
    if value[:1] == ['store:']:
        pairs = _pairs(value)
        entries = _segments(value, 3, len(value) - 1, pairs, ';')
        for a, b in sorted(entries,
                           key=lambda span: len(value[span[0]].split('/')) if span[0] < span[1] else 0, reverse=True):
            if a >= b or value[a + 1:a + 2] != [':']:
                continue
            written = value[a].split('/')
            if path[:len(written)] == written:
                return _project_value(value[a + 2:b], path[len(written):], sources, structs, references)
        # A whole-container write also changes every element below it. Falling
        # back to the constructor here would resurrect pre-mutation values.
        for a, b in entries:
            if a < b and value[a + 1:a + 2] == [':']:
                written = value[a].split('/')
                if written[-1:] == ['contents'] and path[:len(written) - 1] == written[:-1]:
                    return _project_value(value[a + 2:b], path[len(written) - 1:], sources, structs, references)
        return _project_value([value[1]], path, sources, structs, references)
    if value[:1] == ['zero:'] or value[:2] == ['new', '(']:
        return _project_value(['nil'], rest, sources, structs, references)
    if len(value) >= 3 and value[1] == '.' and all(value[n] == '.' for n in range(1, len(value), 2)):
        return _project_value(value[:1], value[2::2] + path, sources, structs, references)
    pairs = _pairs(value)
    opening = next((n for n, token in enumerate(value) if token == '{' and pairs.get(n) == len(value) - 1), None)
    if opening is not None and pairs.get(opening) == len(value) - 1:
        parts = [(a, b) for a, b in _segments(value, opening + 1, len(value) - 1, pairs) if a < b]
        named = {value[a]: value[a + 2:b] for a, b in parts if value[a + 1:a + 2] == [':']}
        if field in named:
            return _project_value(named[field], rest, sources, structs, references)
        kind = ''.join(value[:opening])
        names = list(structs.get(kind, {}))
        if field in names:
            index = names.index(field)
            part = value[slice(*parts[index])] if not named and index < len(parts) else ['nil']
            return _project_value(part, rest, sources, structs, references)
        if field.startswith('index:') and field[6:].isdigit():
            index = int(field[6:])
            elements = _literal_elements(value, sources)
            if elements is not None and index < len(elements):
                return _project_value(elements[index], rest, sources, structs, references)
        if value[:1] == ['map'] and field.startswith('key:'):
            key = json.loads(bytes.fromhex(field[4:]).decode())
            for a, b in reversed(parts):
                split = _segments(value, a, b, pairs, ':')
                if len(split) != 2:
                    continue
                current = _expand_value(value[slice(*split[0])], sources)
                left, right = _constant_operand(current), _constant_operand(key)
                if current == key or left is not None and left == right:
                    return _project_value(value[slice(*split[1])], rest, sources, structs, references)
            return _project_value(['nil'], rest, sources, structs, references)
    result = list(value)
    for part in path:
        if part.startswith('key:'):
            result += ['['] + json.loads(bytes.fromhex(part[4:]).decode()) + [']']
        elif part.startswith('index:'):
            result += ['['] + part[6:].split() + [']']
        elif part.startswith('slice:'):
            result.append('[')
            for slot, bound in enumerate(json.loads(bytes.fromhex(part[6:]).decode())):
                if slot:
                    result.append(':')
                result += bound.split()
            result.append(']')
        else:
            result += ['.', part]
    return result


def _deferred_literal(items, pairs, first, last):
    """Recognize a directly deferred func literal, including grouped literals."""
    literal = _literal_invocation(items, pairs, first + 1, last)
    return literal if literal is not None and pairs[literal[3]] == last - 1 else None


def _slice_path(bounds):
    # Store paths use '/' as a separator; expressions inside a bound may
    # themselves contain '/', ':' or quoted strings.
    return 'slice:' + json.dumps(bounds, separators=(',', ':')).encode().hex()


class _Bindings:
    """Forward value provenance with lexical scopes and control-flow joins."""
    def __init__(self, items, pairs, *, effects=None, returns=None, nonreturning=None):
        self.items, self.pairs = items, pairs
        self.effects = effects or {}
        self.returns = returns or {}
        self.call_values, self.expression_overrides = {}, {}
        self.replacements, self.sources, self.choices, self.origins = {}, {}, {}, {}
        self.occurrences, self.controls = {}, []
        self.structs, self.globals = _type_tables(items, pairs, _functions(items, pairs))
        self.parameter_types = {}
        self.roots, self.storage, self.allocations, self.backings = {}, {}, {}, {}
        # A descriptor is a captured reference value, not the field/variable
        # that happened to contain it. Aggregate values retain these handles;
        # assertion snapshots project their backing storage at the read time.
        self.descriptors = set()
        self.nominal_types = {}
        self.allocation_values, self.slice_capacities = {}, {}
        self.literal_symbols = {}
        self.closures, self.closure_calls, self.call_frames = {}, {}, {}
        self.literal_invocations = {}
        self.closure_variants = {}
        self.capture_defaults = {}
        self.active_closures = set()
        self.cleanup_calls, self.joined_go = set(), set()
        self.cleanup_key = 'metadata:cleanups'
        self.testing_owner_key = 'metadata:testing-owner'
        self.testing_handles = {}
        self.testing_method_owners = {}
        self.terminating_sites = set()
        self.nonreturning = set(nonreturning) if nonreturning is not None else _panic_helpers(items, pairs)
        self.abrupt_literals = set()
        self.loop_frames = {}
        self.alias_choices = {}
        self.initial_references, self.external_backings = {}, {}
        self.pure_values = set()
        self.return_states, self.method_receivers, self.deferred_bindings = [], {}, {}
        self.addressed_roots = set()
        self.deferred_expressions = {}
        self.deferred_key = 'metadata:deferred'
        self.scope_key = 'metadata:scope'
        self.callback_paths_key = 'metadata:callback-paths'
        self.literal_results_key = 'metadata:literal-results'
        self.call_results_key = 'metadata:call-results'
        self.callback_path_splits = 0
        self.helpers = {}
        self.receiver_types = {}
        self.result_fields = []
        self.definitions = _definitions(items, pairs)
        for name, (declaration, opening, following, body, _) in _definitions(items, pairs).items():
            signature = [kind for _, kind in _parameters(items, opening + 1, following - 1, pairs)], items[following:body]
            self.helpers[name] = 'helper:' + hashlib.sha256(repr(signature).encode()).hexdigest()[:16]
            results = _result_fields(items[following:body])
            if results:
                self.globals['return:' + name] = results[0][1]
                self.globals['return:' + self.helpers[name]] = results[0][1]
            if items[declaration + 1] == '(':
                receiver = _parameters(items, declaration + 2, pairs[declaration + 1], pairs)
                if receiver:
                    self.receiver_types[name] = receiver[0][1]

    @staticmethod
    def copy(state):
        return [dict(scope) for scope in state]

    @staticmethod
    def lookup(state, name):
        return next((scope[name] for scope in reversed(state) if name in scope), None)

    def expression(self, first, last, state, *, helpers=False, identity=False, slot=0):
        result = []
        position = first
        while position < last:
            span = max((span for span in self.call_values if span[0] == position and span[1] <= last),
                       key=lambda span: span[1], default=None)
            if span is not None:
                values = state[0].get(self.call_results_key, {}).get(span, self.call_values[span])
                role = values[min(slot, len(values) - 1)]
                current = role if identity else self.snapshot(role, state)
                if not identity:
                    self.expression_overrides[span] = [current]
                result.append(current)
                position = span[1]
                continue
            token = self.items[position]
            # A literal is a function value, not an evaluation of its body.
            # Keep its lexical cells when it appears in a return or aggregate,
            # just as when it is assigned directly to a local variable.
            type_start = position == first or self.items[position - 1] in {
                ':=', '=', ',', '(', '{', ':', '&', 'return'}
            if type_start and token in {'[', 'map', 'struct', 'interface'}:
                following = _type_end(self.items, self.pairs, position, last)
                if following > position and self.items[following:following + 1] == ['{']:
                    result.extend(self.items[position:following])
                    position = following
                    continue
            if token == 'func':
                body = _function_literal_body(self.items, self.pairs, position, last)
                if body is not None and self.pairs[body] < last:
                    result.append(self.literal_value(position, body, state))
                    position = self.pairs[body] + 1
                    continue
            if position and self.items[position - 1] == '.':
                result.append(token)
                position += 1
                continue
            role = self.lookup(state, token)
            if role is not None:
                current = self.snapshot(role, state)
                if not identity:
                    self.replacements[position] = current
                    role = current
            if role is not None and helpers:
                target = _reference([role], self.sources, self.origins)
                if len(target) == 1 and target[0] in self.helpers:
                    role = self.helpers[target[0]]
            result.append(role if role is not None else self.helpers.get(token, token) if helpers else token)
            position += 1
        return _expression_tokens(result)

    def literal_value(self, first, body, state):
        scopes = tuple(scope.get(self.scope_key) for scope in state)
        role = self.role(['closure-reference:', str(body), repr(scopes)])
        self.closures[role] = {'literal': (first + 1, body, self.pairs[body], body),
                               'captures': self.literal_captures(state)}
        self.origins[role] = [role]
        self.parameter_types[role] = self.items[first:body]
        return role

    def named_literal(self, name, receiver=None):
        definition = self.definitions.get(name)
        if definition is None:
            return None
        declaration, opening, _, body, end = definition
        method = self.items[declaration + 1] == '('
        if method and receiver is None:
            return None
        # A declared function cannot capture its caller's lexical variables.
        action = {'literal': (opening, body, end, body), 'captures': [('package', {})]}
        if method:
            parameters = _parameters(self.items, declaration + 2, self.pairs[declaration + 1], self.pairs)
            if parameters:
                action['receiver_binding'] = parameters[0], receiver
        return action

    def returns_function(self, name):
        definition = self.definitions.get(name)
        return definition is not None and any('func' in _underlying_type(kind, self.globals)
            for _, kind in _result_fields(self.items[definition[2]:definition[3]]))

    def retain_receiver_storage(self, location):
        if location is not None and ''.join(self.parameter_types.get(location[0], [])) in self.structs:
            self.addressed_roots.add(location[0])

    def factory_literal(self, target, raw, state):
        if len(target) == 1:
            return self.named_literal(target[0]) if self.returns_function(target[0]) else None
        if len(target) < 3 or target[-2] != '.':
            return None
        type_receiver = _unparenthesized(target[:-2])
        method_expression = ''.join(type_receiver).lstrip('*') in self.structs
        kind = (type_receiver if method_expression else
                _value_type(target[:-2], self.parameter_types, self.structs, self.globals, self.sources))
        method = ''.join(kind).lstrip('*') + '.' + target[-1]
        if not self.returns_function(method):
            return None
        receiver_kind = self.receiver_types.get(method)
        if receiver_kind is None:
            return None
        if method_expression:
            action = self.named_literal(method, ([], None))
            action['receiver_argument'] = True
            return action
        bound = self.method_receivers.get(raw[0]) if len(raw) == 1 else None
        location = bound if bound is not None else self.location(raw[:-2], state, follow=True)
        if location is not None:
            root, path = location
            if receiver_kind[:1] == ['*']:
                self.retain_receiver_storage(location)
            value = _project_value(self.raw_snapshot(root, state), list(path), self.sources,
                                   self.structs, self.descriptors)
        else:
            value = self.capture_value(target[:-2], raw[:-2], receiver_kind, state)
        # Value receivers copy fields while keeping slice/map/pointer fields
        # shared; pointer receivers retain the selected object's identity.
        return self.named_literal(method, (value, location if receiver_kind[:1] == ['*'] else None))

    def closure_values(self, value, state, *, named=False, seen=frozenset()):
        if tuple(value) in seen:
            return []
        seen = seen | {tuple(value)}
        if len(value) == 1:
            role = value[0]
            if role in self.closures:
                return [self.closures[role]]
            if role in self.choices:
                return [action for choice in sorted(self.choices[role])
                        for action in (self.closure_values([choice], state, named=named, seen=seen) or [None])]
        projected = self.read(value)
        role = self.role(projected)
        if role in self.closures:
            return [self.closures[role]]
        if role in self.choices:
            return self.closure_values([role], state, named=named, seen=seen)
        reference = _reference(value, self.sources, self.origins)
        if reference != value:
            actions = self.closure_values(reference, state, named=named, seen=seen)
            if actions:
                return actions
        if named and len(reference) == 1:
            action = self.named_literal(reference[0])
            if action is not None:
                return [action]
        # An unknown index can select any element. Keep every candidate rather
        # than ignoring the call, including an explicitly unresolved element.
        selected = _expression_tokens(value)
        pairs = _pairs(selected)
        opening = next((a for a, b in pairs.items() if selected[a] == '[' and a > 0
                        and b == len(selected) - 1), None)
        if opening is not None and ':' not in selected[opening + 1:-1] and _constant_operand(
                self.read(selected[opening + 1:-1])) is None:
            container = self.read(selected[:opening])
            elements = self.elements(container)
            if elements is None and container[:1] == ['map']:
                braces = _pairs(container)
                body = next((a for a, b in braces.items() if container[a] == '{'
                             and b == len(container) - 1), None)
                if body is not None:
                    entries = [_segments(container, a, b, braces, ':') for a, b in
                               _segments(container, body + 1, len(container) - 1, braces)]
                    elements = [container[slice(*entry[1])] for entry in entries if len(entry) == 2]
            if elements is not None:
                return [action for element in elements
                        for action in (self.closure_values(element, state, named=named, seen=seen) or [None])]
        return []

    def role(self, value, slot=0, *, pure=False):
        if len(value) == 1 and value[0].startswith(('parameter:', 'local:', 'receiver')):
            return value[0]
        key = (' '.join(value), slot)
        # Repeated pure values have the same provenance; distinct evaluations
        # of a call retain their occurrence, including multi-result slots.
        call = any(token == '(' and index and (
            value[index - 1].isidentifier() or ':' in value[index - 1] or value[index - 1] in {')', ']'})
            for index, token in enumerate(value))
        if call and value[-1:] == [')'] and self.conversion(value) is not None and self.underlying_value(value) == ['nil']:
            call = False
        ordinal = self.occurrences.get(key, 0) if call and not pure else 0
        if call and not pure:
            self.occurrences[key] = ordinal + 1
        role = 'local:' + hashlib.sha256(repr((key, ordinal)).encode()).hexdigest()[:16]
        self.sources[role] = value
        return role

    def location(self, value, state=None, *, follow=False):
        value = _expression_tokens(value)
        conversion = self.conversion(value)
        if conversion is not None:
            kind, operand = conversion
            underlying = _underlying_type(kind, self.globals)
            if underlying[:1] in (['*'], ['map']) or underlying[:2] == ['[', ']']:
                return self.location(operand, state, follow=follow)
        while value and value[0] in {'&', '*'}:
            value = _expression_tokens(value[1:])
        if not value:
            return None
        token = value[0]
        if not token.startswith(('local:', 'parameter:', 'receiver')):
            return None
        root, prefix = self.roots.get(token, (token, ()))
        path, cursor, pairs = list(prefix), 1, _pairs(value)
        while cursor < len(value):
            if state is not None:
                root, path = self.indirect((root, tuple(path)), state)
                path = list(path)
            if value[cursor] == '.' and cursor + 1 < len(value):
                path.append(value[cursor + 1])
                cursor += 2
            elif value[cursor] == '[' and cursor in pairs:
                closing = pairs[cursor]
                if ':' in value[cursor + 1:closing]:
                    spans = _segments(value, cursor + 1, closing, pairs, ':')
                    path.append(_slice_path([' '.join(value[a:b]) for a, b in spans]))
                else:
                    kind = _value_type(value[:cursor], self.parameter_types, self.structs, self.globals, self.sources)
                    index = self.read(value[cursor + 1:closing])
                    path.append(_key_path(index) if kind[:1] == ['map'] else 'index:' + ' '.join(index))
                cursor = closing + 1
            else:
                return None
        location = root, tuple(path)
        return self.indirect(location, state) if follow and state is not None else location

    def conversion(self, value):
        pairs = _pairs(value)
        opening = next((a for a, b in pairs.items() if a > 0 and value[a] == '('
                        and b == len(value) - 1), None)
        if opening is None or len(_segments(value, opening + 1, len(value) - 1, pairs)) != 1:
            return None
        kind = _expression_tokens(value[:opening])
        if (len(kind) == 1 and 'type:' + kind[0] in self.globals or
                kind[:1] in (['['], ['map'])):
            return kind, value[opening + 1:-1]
        return None

    def nominal_type(self, kind, seen=frozenset()):
        if len(kind) != 1 or kind[0] in seen or 'type:' + kind[0] not in self.globals:
            return None
        if not self.globals.get('alias:' + kind[0]):
            return kind
        return self.nominal_type(self.globals['type:' + kind[0]], seen | {kind[0]})

    def underlying_value(self, value):
        conversion = self.conversion(value)
        return self.underlying_value(self.read(conversion[1])) if conversion is not None else value

    def indirect(self, location, state):
        """Read a reference stored in a field without rebinding its backing."""
        seen = set()
        while location not in seen:
            seen.add(location)
            root, path = location
            value = _project_value(self.raw_snapshot(root, state), list(path), self.sources, self.structs, self.descriptors)
            if len(value) != 1 or value[0] not in self.descriptors:
                kind = _value_type(self.location_tokens(location), self.parameter_types,
                                   self.structs, self.globals, self.sources)
                reference = kind[:1] in (['*'], ['map']) or kind[:2] == ['[', ']']
                if not path or path[-1].startswith('slice:') or not reference:
                    break
                key = root, path
                if key not in self.initial_references:
                    backing = 'local:external-reference:' + hashlib.sha256(repr((
                        state[0].get(self.scope_key), key)).encode()).hexdigest()[:16]
                    initial = self.location_tokens((self.snapshot(root, state), path))
                    self.sources[backing] = initial
                    self.storage[backing] = self.role(initial)
                    self.roots[backing] = backing, ()
                    self.parameter_types[backing] = kind
                    self.descriptors.add(backing)
                    self.initial_references[key] = backing
                    self.external_backings[backing] = location
                value = [self.initial_references[key]]
            location = self.roots[value[0]]
        return location

    def resolve_location(self, location, state, *, follow=True):
        if location is None:
            return None
        root, parts = location
        path = ()
        for part in parts:
            root, path = self.indirect((root, path), state)
            path += (part,)
        return self.indirect((root, path), state) if follow else (root, path)

    def join_references(self, roles):
        locations = set()
        for role in roles:
            if role not in self.descriptors:
                return None
            locations.update(self.alias_choices.get(role, (self.roots[role],)))
        role = 'local:references:' + hashlib.sha256(repr(sorted(locations)).encode()).hexdigest()[:16]
        self.descriptors.add(role)
        self.alias_choices[role] = frozenset(locations)
        self.roots[role] = role, ()
        self.sources[role] = ['reference-choice:'] + sorted(roles)
        return role

    @staticmethod
    def location_tokens(location):
        root, path = location
        value = [root]
        for part in path:
            if part.startswith('index:'):
                value += ['['] + part[6:].split() + [']']
            elif part.startswith('key:'):
                value += ['['] + json.loads(bytes.fromhex(part[4:]).decode()) + [']']
            elif part.startswith('slice:'):
                bounds = json.loads(bytes.fromhex(part[6:]).decode())
                value.append('[')
                for slot, bound in enumerate(bounds):
                    if slot:
                        value.append(':')
                    value += bound.split()
                value.append(']')
            else:
                value += ['.', part]
        return value

    def reference_effect(self, role, state=None, seen=frozenset()):
        references = []
        for root, path in sorted(self.alias_choices.get(role, (self.roots[role],))):
            if root in self.external_backings:
                original, prefix = self.external_backings[root]
                root, path = original, prefix + path
            if root == 'receiver' or root.startswith('parameter:'):
                references.append(self.location_tokens((root, path)))
            elif root in seen:
                references.append({'alias': root, 'path': '/'.join(path)})
            else:
                initial = self.allocation_values.get(root, _expand_value([root], self.sources))
                writes = [] if state is None else [
                    (key[2], self.value_effect([value], state, seen | {root})) for key, value in state[0].items()
                    if isinstance(key, tuple) and key[:2] == ('store', root)]
                references.append({'identity': root, 'allocation': self.value_effect(initial, state, seen | {root}),
                                   'writes': sorted(writes, key=repr), 'path': '/'.join(path),
                                   'capacity': self.slice_capacities.get(root)})
        return {'references': references, 'nominal': self.nominal_types.get(role)}

    def value_effect(self, value, state=None, references=frozenset()):
        captures = {}

        def expand(tokens, seen=frozenset()):
            result = []
            for token in tokens:
                if token in seen:
                    result.append('unknown-value:' + hashlib.sha256(token.encode()).hexdigest()[:16])
                elif token in self.descriptors:
                    key = 'captured-reference:' + str(len(captures))
                    captures[key] = self.reference_effect(token, state, references)
                    result.append(key)
                elif token in self.sources:
                    result += expand(self.sources[token], seen | {token})
                else:
                    result.append(token)
            return result

        return {'value': expand(value), 'captures': captures}

    def instantiate_value(self, effect, actual, locations, state, memo=None):
        memo = {} if memo is None else memo
        captures = {key: self.instantiate_reference(value, actual, locations, state, memo)
                    for key, value in effect['captures'].items()}
        value = []
        for token in effect['value']:
            if token in captures:
                value.append(captures[token])
            else:
                value += ['('] + actual[token] + [')'] if token in actual else [token]
        return _expression_tokens(value)

    def instantiate_reference(self, effect, actual, locations, state, memo=None):
        # Allocation identities belong to one invocation. In particular two
        # result slots, fields and output effects may all refer to one object.
        memo = {} if memo is None else memo
        captured = []
        for reference in effect['references']:
            if isinstance(reference, dict) and ('allocation' in reference or 'alias' in reference):
                identity = reference.get('identity', reference.get('alias'))
                if identity in memo:
                    role = memo[identity]
                    kind = self.parameter_types.get(role, [])
                    if 'allocation' in reference:
                        definition = repr((reference['allocation'], reference['writes']))
                        definitions = memo.setdefault(('definitions', identity), set())
                        if definition not in definitions:
                            definitions.add(definition)
                            # The same allocation can reach different returns
                            # with different contents. Preserve every exit,
                            # rather than letting the first return win.
                            previous, alternative = self.copy(state), self.copy(state)
                            # Each exit starts from the allocation's initial
                            # contents, including exits with no final writes.
                            for key in list(alternative[0]):
                                if isinstance(key, tuple) and key[:2] == ('store', role):
                                    del alternative[0][key]
                            if memo.get(('initial', identity)) != repr(reference['allocation']):
                                value = self.instantiate_value(reference['allocation'], actual, locations, alternative, memo)
                                self.write_reference((role, ()), value, alternative)
                            self.reference_writes(role, reference, actual, locations, alternative, memo)
                            state[:] = self.merge([previous, alternative], previous)[0]
                else:
                    # Reserve the node before traversing fields and writes so
                    # self references retain a real edge rather than unknown.
                    pending = self.allocate(self.role(['pending-allocation']))
                    self.roots[pending] = pending, ()
                    self.descriptors.add(pending)
                    memo[identity] = pending
                    memo['definitions', identity] = {repr((reference['allocation'], reference['writes']))}
                    memo['initial', identity] = repr(reference['allocation'])
                    value = self.instantiate_value(reference['allocation'], actual, locations, state, memo)
                    kind = _value_type(value, self.parameter_types, self.structs, self.globals, self.sources)
                    role = self.role(self.capture_value(self.read(value), value, kind, state))
                    if role not in self.descriptors:
                        role = self.allocate(role)
                        self.roots[role] = role, ()
                        self.descriptors.add(role)
                        self.parameter_types[role] = kind
                    self.roots[pending] = role, ()
                    self.parameter_types[pending] = kind
                    memo[identity] = role
                    if reference.get('capacity') is not None:
                        self.slice_capacities[role] = reference['capacity']
                    self.reference_writes(role, reference, actual, locations, state, memo)
                if reference['path']:
                    location = role, self.instantiate_path(reference['path'], actual)
                    view = self.allocate(self.role(self.reference_value(location, state)))
                    self.roots[view] = location
                    self.descriptors.add(view)
                    self.parameter_types[view] = kind
                    role = view
                captured.append(role)
                continue
            if isinstance(reference, dict):
                identity = value = self.instantiate_value(reference, actual, locations, state, memo)
            else:
                identity, value = [], []
                for token in reference:
                    identity += (['('] + self.location_tokens(locations[token]) + [')']
                                 if token in locations and locations[token] is not None else
                                 ['('] + actual[token] + [')'] if token in actual else [token])
                    value += ['('] + actual[token] + [')'] if token in actual else [token]
                identity, value = _expression_tokens(identity), _expression_tokens(value)
            kind = _value_type(identity, self.parameter_types, self.structs, self.globals, self.sources)
            role = self.role(self.capture_value(value, identity, kind, state))
            if role not in self.descriptors:
                role = self.allocate(role)
                self.roots[role] = self.location(identity, state, follow=True) or (role, ())
                self.descriptors.add(role)
            captured.append(role)
        role = captured[0] if len(captured) == 1 else self.join_references(captured)
        if effect.get('nominal'):
            self.nominal_types[role] = effect['nominal']
        elif 'nominal' in effect:
            self.nominal_types.pop(role, None)
        return role

    def reference_writes(self, role, reference, actual, locations, state, memo):
        for path, written in reference['writes']:
            location = role, self.instantiate_path(path, actual)
            current = self.instantiate_value(written, actual, locations, state, memo)
            if len(current) == 1 and current[0] in self.descriptors:
                self.write_reference(location, current, state)
            else:
                self.write(location, current, state)

    def capture_value(self, value, identity, kind, state, *, declared=False):
        """Copy aggregate fields by value and reference descriptors by identity."""
        declared_kind = kind
        kind = _underlying_type(kind, self.globals)
        reference = (kind[:1] in (['*'], ['map']) or kind[:2] == ['[', ']'] or
                     kind == ['testing', '.', 'TB'])
        location = self.location(identity, state, follow=True)
        conversion = self.conversion(identity)
        made = identity[:2] == ['make', '('] and _pairs(identity).get(1) == len(identity) - 1
        appended = identity[:2] == ['append', '('] and _pairs(identity).get(1) == len(identity) - 1
        if made or appended:
            value = self.underlying_value(self.read(value))
        if kind[:2] == ['[', ']'] and self.underlying_value(value) == ['nil']:
            value = kind + ['(', 'nil', ')']
        nominal = (self.nominal_type(declared_kind) if declared else
                   self.nominal_type(_value_type_raw(identity, self.parameter_types, self.structs,
                                                    self.globals, self.sources)) if made or appended else
                   self.nominal_type(conversion[0]) if conversion is not None else
                   self.nominal_types.get(identity[0]) if len(identity) == 1 else None)
        if reference and location is not None:
            value = self.reference_value(location, state)
            role = self.allocate(self.role(value))
            self.roots[role] = location
            self.descriptors.add(role)
            self.parameter_types[role] = kind
            if nominal:
                self.nominal_types[role] = nominal
            return [role]
        aggregate = ''.join(kind).lstrip('*') in self.structs or kind[:1] == ['['] or reference
        if aggregate and location is not None:
            root, path = location
            return _project_value(self.raw_snapshot(root, state), list(path), self.sources, self.structs, self.descriptors)
        identity = _expression_tokens(identity)
        pairs = _pairs(identity)
        opening = next((a for a, b in pairs.items() if identity[a] == '{' and b == len(identity) - 1), None)
        if aggregate and opening is not None:
            fields = self.structs.get(''.join(kind).lstrip('*'), {})
            element_kind = _element_type(kind) if kind[:1] in (['['], ['map']) else []
            declared = identity[:opening]
            nominal = self.nominal_type(declared)
            result = (kind + ['{'] if not declared or len(declared) == 1 and 'type:' + declared[0] in self.globals
                      else identity[:opening + 1])
            for slot, (a, b) in enumerate(_segments(identity, opening + 1, len(identity) - 1, pairs)):
                if a >= b:
                    continue
                if result[-1] != '{':
                    result.append(',')
                entries = _segments(identity, a, b, pairs, ':')
                keyed = len(entries) == 2
                field = identity[a] if keyed else next(iter(list(fields)[slot:]), '')
                first = entries[1][0] if keyed else a
                key = self.read(identity[slice(*entries[0])]) if kind[:1] == ['map'] and keyed else identity[a:first - 1]
                result += key + [':'] if keyed else []
                actual = identity[first:b]
                current = self.read([self.snapshot(token, state) if token.startswith(
                    ('local:', 'parameter:', 'receiver')) else token for token in actual])
                result += self.capture_value(current, actual, fields.get(field, element_kind), state, declared=True)
            value = result + ['}']
        if reference:
            role = self.allocate(self.role(value))
            self.descriptors.add(role)
            self.roots[role] = role, ()
            self.parameter_types[role] = kind
            if made or appended:
                self.allocation_values[role] = identity
            if appended:
                # Go may reserve more cells than the appended result needs.
                # The returned elements do not establish its new capacity.
                self.slice_capacities[role] = 0 if self.underlying_value(value) == ['nil'] else None
            if made:
                arguments = _segments(identity, 2, len(identity) - 1, _pairs(identity))
                if len(arguments) in {2, 3} and kind[:2] == ['[', ']']:
                    capacity = self.integer(identity[slice(*arguments[-1])])
                    if capacity is not None:
                        self.slice_capacities[role] = capacity
            if nominal:
                self.nominal_types[role] = nominal
            return [role]
        return value

    def materialize(self, value, state, seen=frozenset()):
        """Read captured references while keeping store records well formed."""
        if value[:1] == ['store:']:
            root = value[1]
            original = self.materialize([self.storage.get(root, root)], state, seen | {root})
            return ['store:', self.role(original), '{'] + self.materialize(value[3:-1], state, seen) + ['}']
        result = []
        for token in value:
            if token in seen:
                result.append(token)
            elif token in self.descriptors:
                result.append(self.snapshot(token, state, seen | {token}))
            elif token in self.pure_values:
                result.append(token)
            elif token in self.sources:
                source = self.sources[token]
                current = self.materialize(source, state, seen | {token})
                result.append(self.role(current) if current != source else token)
                if all(part not in self.descriptors and (part not in self.sources or part in self.pure_values)
                       for part in source):
                    self.pure_values.add(token)
            else:
                result.append(token)
        return result

    def normalize_view(self, value, types):
        """Canonicalize a complete slice view without erasing type or capacity."""
        value = _expression_tokens(value)
        expanded = _expand_value(value, self.sources)
        conversion = self.conversion(expanded)
        if conversion is not None and self.nominal_type(conversion[0]) is not None:
            # A descriptor's underlying backing has no defined slice name.
            # Normalize its view inside the conversion, retaining that name.
            operand = self.normalize_view(conversion[1], types)
            return [self.role(conversion[0] + ['('] + operand + [')'])]
        if len(value) >= 3 and value[-2] == '.':
            kind = _value_type(value, types, self.structs, self.globals, self.sources)
            if kind[:1] in (['*'], ['map']) or kind[:2] == ['[', ']']:
                return [self.role(value)]
        location = self.location(value)
        if location is not None:
            root, path = location
            kind = _value_type([root], types, self.structs, self.globals, self.sources)
            if kind[:1] == ['['] and path and all(part.startswith('slice:') for part in path):
                region, original = self.region(location), self.region((root, ()))
                backing = self.backings.get(value[0], [root])
                if kind[:2] == ['[', ']'] and region == original and original[3] is not None:
                    return [self.role(backing)]
                _, _, offset, length, capacity = region
                if None not in (offset, length, capacity):
                    return [self.role([self.role(backing), '[', str(offset), ':', str(offset + length),
                                      ':', str(offset + capacity), ']'])]
                full = True
                for part in path:
                    bounds = [bound.split() for bound in json.loads(bytes.fromhex(part[6:]).decode())]
                    full = full and (not bounds[0] or self.integer(bounds[0]) == 0) and (
                        not bounds[1] or _expression_tokens(bounds[1]) == ['len', '(', root, ')']) and (
                        len(bounds) < 3 or _expression_tokens(bounds[2]) == ['cap', '(', root, ')'])
                if full and kind[:2] == ['[', ']']:
                    return [self.role(backing)]
        pairs = _pairs(value)
        opening = next((a for a, b in pairs.items() if a > 0 and value[a] == '['
                        and b == len(value) - 1 and ':' in value[a + 1:b]), None)
        if opening is None:
            return value
        base = self.normalize_view(value[:opening], types)
        kind = _value_type(base, types, self.structs, self.globals, self.sources)
        bounds = [value[a:b] for a, b in _segments(value, opening + 1, len(value) - 1, pairs, ':')]
        low = not bounds[0] or self.integer(bounds[0]) == 0
        high = not bounds[1] or _expression_tokens(bounds[1]) == ['len', '('] + base + [')']
        cap = len(bounds) < 3 or _expression_tokens(bounds[2]) == ['cap', '('] + base + [')']
        return base if kind[:2] == ['[', ']'] and low and high and cap else value

    def fold_sizes(self, value):
        value = list(value)
        for _ in range(len(value)):
            pairs = _pairs(value)
            for opening in sorted((a for a in pairs if a > 0 and value[a] == '(' and
                                   value[a - 1] in {'len', 'cap'}), reverse=True):
                current = self.read(value[opening - 1:pairs[opening] + 1])
                constant = _constant_operand(current)
                if constant and constant[0] == 'integer':
                    value = value[:opening - 1] + [str(constant[1])] + value[pairs[opening] + 1:]
                    break
            else:
                break
        return value

    def integer(self, value):
        value = self.fold_sizes(self.read(value))
        constant = _constant_operand(value)
        return constant[1] if constant and constant[0] in {'integer', 'rune'} else None

    def extended_backing(self, value, capacity):
        """Expose allocated zeroed cells only when a slice view can reach them."""
        if capacity is None or not 0 <= capacity <= 64:
            return value
        current = self.underlying_value(_expand_value(value, self.sources))
        stored = current[:1] == ['store:']
        initial = self.underlying_value(_expand_value(current[1:2], self.sources)) if stored else current
        elements = _literal_elements(initial, self.sources)
        if capacity is None or elements is None or not len(elements) < capacity <= 64 or initial[:2] != ['[', ']']:
            return value
        opening = initial.index('{')
        zeros = _literal_elements(['[', str(capacity), ']'] + initial[2:opening] + ['{', '}'], self.sources)
        if zeros is None:
            return value
        backing = self.literal(elements + zeros[len(elements):], initial)
        return ['store:', self.role(backing)] + current[2:] if stored else backing

    def read(self, value, seen=frozenset()):
        """Project a pure element/view read from its current value, not spelling."""
        value = _expression_tokens(value)
        location = self.location(value)
        if location is not None and any(part.startswith('slice:') for part in location[1]):
            root, path, offset, length, _ = self.region(location)
            token = next((token for token in value if token.startswith(('local:', 'parameter:', 'receiver'))), '')
            backing = self.backings.get(token, [root])
            backing = self.extended_backing(backing, self.slice_capacities.get(root))
            projected = _project_value(backing, list(path), self.sources, self.structs)
            if location[1][-1].startswith('index:'):
                return self.read(projected, seen)
            elements = self.elements(projected)
            if elements is not None and offset is not None and length is not None and (
                    0 <= offset <= offset + length <= len(elements)):
                return self.literal(elements[offset:offset + length], self.read(projected, seen))
        value = _expand_value(value, self.sources, seen)
        if not value or tuple(value) in seen:
            return value
        seen = seen | {tuple(value)}
        if value[:1] == ['store:']:
            for a, b in _segments(value, 3, len(value) - 1, _pairs(value), ';'):
                if value[a:a + 2] == ['contents', ':']:
                    replacement = self.read(value[a + 2:b], seen)
                    if replacement and not replacement[0].startswith(('copy:', 'mutation:', 'mutation:append')):
                        return replacement
            original = self.read(value[1:2], seen)
            elements = self.elements(value, seen) if original[:2] == ['[', ']'] else None
            if elements is not None and len(elements) <= 64:
                return self.literal([self.read(element, seen) for element in elements], original)
        if value[:1] == ['*']:
            # Pointer bindings retain their backing location separately. Their
            # snapshot is the current pointee value.
            return self.read(value[1:], seen)
        pairs = _pairs(value)
        # Only peel a suffix of a primary expression. A literal's leading []
        # is a type, not an index operation.
        opening = next((a for a, b in pairs.items()
                        if a > 0 and value[a] == '[' and b == len(value) - 1), None)
        if opening is not None:
            base = self.read(value[:opening], seen)
            elements = self.elements(base, seen)
            body = value[opening + 1:-1]
            if ':' not in body:
                index = self.integer(body)
                if elements is not None and index is not None and 0 <= index < len(elements):
                    return self.read(elements[index], seen)
                kind = _value_type(base, self.parameter_types, self.structs, self.globals, self.sources)
                if base[:1] == ['map'] or kind[:1] == ['map']:
                    return self.read(_project_value(base, [_key_path(self.read(body))], self.sources, self.structs), seen)
            elif elements is not None:
                spans = _segments(value, opening + 1, len(value) - 1, pairs, ':')
                low = self.integer(value[slice(*spans[0])]) if spans[0][0] < spans[0][1] else 0
                high = self.integer(value[slice(*spans[1])]) if spans[1][0] < spans[1][1] else len(elements)
                if low is not None and high is not None and 0 <= low <= high <= len(elements):
                    return self.literal(elements[low:high], base)
            return base + ['['] + body + [']']
        if value[:2] == ['make', '('] and pairs.get(1) == len(value) - 1:
            arguments = _segments(value, 2, len(value) - 1, pairs)
            if len(arguments) in {2, 3}:
                declared = value[slice(*arguments[0])]
                kind = _underlying_type(declared, self.globals)
                size = self.integer(value[slice(*arguments[1])])
                capacity = self.integer(value[slice(*arguments[-1])])
                if kind[:2] == ['[', ']'] and size is not None and capacity is not None and 0 <= size <= capacity <= 64:
                    elements = _literal_elements(['[', str(size), ']'] + kind[2:] + ['{', '}'], self.sources)
                    if elements is not None:
                        literal = self.literal(elements, kind + ['{', '}'])
                        nominal = self.nominal_type(declared)
                        return nominal + ['('] + literal + [')'] if nominal else literal
        if value[:2] == ['append', '('] and pairs.get(1) == len(value) - 1:
            arguments = [value[a:b] for a, b in _segments(value, 2, len(value) - 1, pairs)]
            if len(arguments) > 1:
                base = self.underlying_value(self.read(arguments[0], seen))
                elements = [] if base == ['nil'] else self.elements(base, seen)
                if len(arguments) == 2 and arguments[1][-1:] == ['...']:
                    source = self.underlying_value(self.read(arguments[1][:-1], seen))
                    added = [] if source == ['nil'] else self.elements(source, seen)
                else:
                    added = arguments[1:]
                if elements is not None and added is not None and len(elements) + len(added) <= 64:
                    if not added:
                        return self.read(arguments[0], seen)
                    kind = _value_type(arguments[0], self.parameter_types, self.structs, self.globals, self.sources)
                    return self.literal(elements + added, kind + ['{', '}'])
        if value[:2] in (['len', '('], ['cap', '(']) and pairs.get(1) == len(value) - 1:
            if value[0] == 'cap':
                region = self.region(self.location(value[2:-1]))
                if region is not None and region[4] is not None:
                    return [str(region[4])]
            elements = self.elements(value[2:-1], seen)
            if elements is not None and value[0] == 'len':
                return [str(len(elements))]
        # Field projection is useful on both constructor and store snapshots.
        if len(value) >= 3 and value[-2] == '.' and value[-1].isidentifier():
            base = self.read(value[:-2], seen)
            if base[:1] == ['store:'] or '{' in base or base[:1] == ['zero:']:
                return self.read(_project_value(base, [value[-1]], self.sources, self.structs), seen)
        return value

    @staticmethod
    def literal(elements, template=None):
        template = template or ['[', ']', 'int', '{', '}']
        opening = next((n for n, token in enumerate(template) if token == '{'), None)
        kind = template[:opening] if opening is not None and template[:1] == ['['] else ['[', ']', 'int']
        closing = _pairs(kind).get(0)
        if closing is not None and closing > 1:
            kind = ['[', ']'] + kind[closing + 1:]  # A view of an array is a slice.
        result = kind + ['{']
        for number, element in enumerate(elements):
            if number:
                result.append(',')
            result += element
        return result + ['}']

    def elements(self, value, seen=frozenset()):
        value = _expand_value(value, self.sources)
        value = self.underlying_value(value)
        elements = _literal_elements(value, self.sources)
        if elements is not None:
            return elements
        if value[:1] == ['store:']:
            original = _expand_value(value[1:2], self.sources)
            initial = _literal_elements(original, self.sources)
            if initial is not None and len(initial) <= 64:
                return [_project_value(value, ['index:' + str(n)], self.sources, self.structs)
                        for n in range(len(initial))]
        return None

    def region(self, location):
        """Return a backing location and bounded slice interval/capacity."""
        if location is None:
            return None
        root, parts = location
        path, offset, length, capacity = [], 0, None, None
        for part in (*parts, None):
            if length is None:
                initial = _project_value([root], path, self.sources, self.structs)
                elements = self.elements(self.read(initial))
                if elements is not None:
                    length = len(elements)
                    capacity = self.slice_capacities.get(root, length) if not path else length
            if part is None:
                break
            if part.startswith('slice:'):
                bounds = json.loads(bytes.fromhex(part[6:]).decode())
                low = self.integer(bounds[0].split()) if bounds[0] else 0
                high = self.integer(bounds[1].split()) if len(bounds) > 1 and bounds[1] else length
                cap = self.integer(bounds[2].split()) if len(bounds) > 2 and bounds[2] else capacity
                if low is None or high is None:
                    return root, tuple(path), None, None, None
                offset = offset + low if offset is not None else None
                length = high - low if 0 <= low <= high else None
                capacity = cap - low if cap is not None and 0 <= low <= cap else None
            elif part.startswith('index:'):
                index = self.integer(part[6:].split())
                path.append('index:' + str(offset + index) if offset is not None and index is not None else part)
                offset, length, capacity = 0, None, None
            else:
                path.append(part)
                offset, length, capacity = 0, None, None
        return root, tuple(path), offset, length, capacity

    def current(self, root, path, state):
        return self.read(_project_value([self.snapshot(root, state)], list(path), self.sources, self.structs))

    def copy_contents(self, destination, source, state):
        destination = self.resolve_location(destination, state)
        if destination is not None and destination[0] in self.alias_choices:
            root, path = destination
            for backing, prefix in sorted(self.alias_choices[root]):
                self.copy_contents((backing, prefix + path), source, state)
            return
        region = self.region(destination)
        if region is None:
            return
        root, path, offset, length, _ = region
        source = self.underlying_value(self.read(source))
        elements = self.elements(source)
        if length == 0 or _known_empty(source, self.sources) or _empty_slice(source):
            return
        source_region = self.region(self.location(source))
        if source_region is not None and source_region[:4] == region[:4]:
            return  # Includes self-copies of already modified storage.
        if offset is not None and length is not None and 0 <= length <= 64:
            count = min(length, len(elements)) if elements is not None else length
            # Read every source element first: Go copy permits overlap.
            values = [self.read(elements[n]) if elements is not None else
                      ['copy-element:'] + source + ['[', str(n), ']'] for n in range(count)]
            for n, value in enumerate(values):
                self.write((root, path + ('index:' + str(offset + n),)), value, state)
            return
        # Unknown intervals retain their range in summaries. They cannot prove
        # that a later partial write restored the complete container.
        self.write((root, tuple(destination[1]) + ('contents',)), ['copy:'] + source, state, raw=True)

    def reference_value(self, location, state):
        root, parts = location
        if parts and self.normalize_view(self.location_tokens(location), self.parameter_types) == [root]:
            return self.reference_value((root, ()), state)
        if root in self.external_backings and not parts:
            return [self.snapshot(root, state)]
        _, path, offset, length, _ = self.region(location)
        backing = [self.snapshot(root, state)]
        if any(part.startswith('slice:') for part in parts):
            backing = self.extended_backing(backing, self.slice_capacities.get(root))
        value = _project_value(backing, list(path), self.sources, self.structs)
        if any(part.startswith('slice:') for part in parts):
            elements = self.elements(self.read(value))
            if elements is not None and offset is not None and length is not None:
                return self.literal(elements[offset:offset + length], self.read(value))
            value += ['[', str(offset) if offset is not None else 'unknown:offset', ':',
                      str(offset + length) if offset is not None and length is not None else '', ']']
        return self.read(value)

    def clear_contents(self, destination, state):
        destination = self.resolve_location(destination, state)
        if destination is not None and destination[0] in self.alias_choices:
            root, path = destination
            for backing, prefix in sorted(self.alias_choices[root]):
                self.clear_contents((backing, prefix + path), state)
            return
        region = self.region(destination)
        if region is None:
            return
        root, path, offset, length, _ = region
        if length == 0:
            return
        if offset is not None and length is not None and 0 <= length <= 64:
            for index in range(offset, offset + length):
                cell = path + ('index:' + str(index),)
                initial = self.read(_project_value([root], list(cell), self.sources, self.structs))
                constant = _constant_operand(initial)
                zero = {'bool': ['false'], 'string': ['""'], 'nil': ['nil']}.get(
                    constant[0] if constant else '', ['0'])
                self.write((root, cell), zero, state)
            return
        self.write(destination, ['mutation:', 'clear'], state, raw=True)

    def raw_snapshot(self, role, state):
        """A value copy retains reference handles even after a field rebind."""
        root, prefix = self.roots.get(role, (role, ()))
        updates = {key[2]: value for key, value in state[0].items()
                   if isinstance(key, tuple) and key[:2] == ('store', root)}
        for (owner, path), reference in self.initial_references.items():
            if owner == root and not any(path[:len(parts)] == parts or parts == ('contents',)
                                         for parts in (tuple(key.split('/')) for key in updates)):
                updates['/'.join(path)] = reference
        updates = sorted(updates.items())
        if not updates and not prefix:
            return [self.storage.get(root, root)]
        if updates:
            value = ['store:', root, '{']
            for path, current in updates:
                value += [path, ':', current, ';']
            value += ['}']
        else:
            value = [self.storage.get(root, root)]
        if prefix:
            region = self.region((root, prefix))
            _, path, offset, length, _ = region
            if any(part.startswith('slice:') for part in prefix):
                value = self.extended_backing(value, self.slice_capacities.get(root))
            value = _project_value(value, list(path), self.sources, self.structs)
            if any(part.startswith('slice:') for part in prefix):
                elements = self.elements(value)
                if elements is not None and offset is not None and length is not None:
                    value = self.literal(elements[offset:offset + length], self.read(value))
                else:
                    value += ['[', str(offset) if offset is not None else 'unknown:offset', ':',
                              str(offset + length) if offset is not None and length is not None else '', ']']
        return value

    def snapshot(self, role, state, seen=frozenset()):
        testing_root, testing_path = self.roots.get(role, (role, ()))
        if not testing_path and testing_root in self.testing_handles:
            # A folded failure context must not rebind the parent's storage
            # identity to the child's lifetime (including nested t.Run calls).
            return self.testing_handles[testing_root]
        if role in self.literal_symbols and not any(isinstance(key, tuple) and key[:2] == ('store', role)
                                                   for key in state[0]):
            return self.literal_symbols[role]
        root, prefix = self.roots.get(role, (role, ()))
        if root in self.alias_choices:
            values = {self.role(self.reference_value((backing, path + prefix), state))
                      for backing, path in self.alias_choices[root]}
            return next(iter(values)) if len(values) == 1 else self.role(['reference-choice:'] + sorted(values))
        value = self.materialize(self.raw_snapshot(role, state), state, seen)
        # Reference-cell identity can change while its visible value stays the
        # same. Retain the raw cell write, but omit equal writes in a read.
        if value[:1] == ['store:']:
            pairs, kept = _pairs(value), []
            for a, b in _segments(value, 3, len(value) - 1, pairs, ';'):
                if a >= b:
                    continue
                path = value[a].split('/')
                reference = self.initial_references.get((root, tuple(path)))
                initial = ([self.storage[reference]] if reference is not None else
                           _project_value([root], path, self.sources, self.structs))
                original = self.materialize(initial, state, seen)
                if self.read(value[a + 2:b]) != self.read(original):
                    kept += value[a:b] + [';']
            value = ['store:', value[1], '{'] + kept + ['}'] if kept else [self.storage.get(root, root)]
            if not kept:
                value = self.materialize(value, state, seen)
        backing = self.materialize(self.raw_snapshot(root, state), state, seen)
        nominal = self.nominal_types.get(role)
        if nominal:
            value = nominal + ['('] + value + [')']
        if prefix:
            version = 'local:projection:' + hashlib.sha256(repr((root, prefix, value)).encode()).hexdigest()[:16]
            self.sources[version] = value
        else:
            version = self.role(value)
        self.roots[version] = root, prefix
        self.backings[version] = backing
        return version

    def allocate(self, value):
        number = self.allocations.get(value, 0)
        self.allocations[value] = number + 1
        root = 'local:allocation:' + hashlib.sha256(repr((value, number)).encode()).hexdigest()[:16]
        self.sources[root] = [value]
        self.storage[root] = value
        return root

    def default(self, root, path):
        if (root, path) in self.initial_references:
            return self.initial_references[root, path]
        value = _project_value([root], list(path[:-1] if path[-1:] == ('contents',) else path),
                               self.sources, self.structs, self.descriptors)
        return self.role(value)

    def write_reference(self, location, value, state):
        if location is None:
            return
        root, path = location
        for key in list(state[0]):
            if isinstance(key, tuple) and key[:2] == ('store', root) and tuple(
                    key[2].split('/'))[:len(path)] == path:
                del state[0][key]
        state[0][('store', root, '/'.join(path) if path else 'contents')] = self.role(value)

    def write(self, location, value, state, *, raw=False):
        location = self.resolve_location(location, state, follow=False)
        if location is None:
            return
        root, path = location
        if root in self.alias_choices:
            for backing, prefix in sorted(self.alias_choices[root]):
                self.write((backing, prefix + path), value, state, raw=raw)
            return
        expanded = self.read(value)
        if expanded[:1] == ['copy:'] and not raw:
            self.copy_contents((root, path[:-1] if path[-1:] == ('contents',) else path), expanded[1:], state)
            return
        if expanded == ['mutation:', 'clear'] and not raw:
            self.clear_contents(location, state)
            return
        resolved = self.region((root, path))
        if resolved is not None and (not any(part.startswith('slice:') for part in path) or
                                     path[-1:] != ('contents',)):
            root, path = resolved[:2]
        if not path:
            path = ('contents',)
        key = 'store', root, '/'.join(path)
        original = self.default(root, path)
        left, right = self.read([original]), expanded
        if self.current(root, path[:-1] if path[-1:] == ('contents',) else path, state) == right:
            return
        if right[:1] == ['copy:'] and not any(part.startswith('slice:') for part in path):
            copied = _expand_value(right[1:], self.sources)
            if _known_empty(copied, self.sources):
                return
            if left == copied:
                right = left
        if right == ['mutation:', 'clear']:
            elements = _literal_elements(left, self.sources)
            if elements is not None and all(_constant_operand(element) in {
                    ('integer', 0), ('rune', 0), ('bool', False), ('string', ''), ('nil', None)}
                    for element in elements):
                right = left
        prefix = path[:-1] if path[-1] == 'contents' else path
        for existing in list(state[0]):
            if not isinstance(existing, tuple) or existing[:2] != ('store', root):
                continue
            parts = tuple(existing[2].split('/'))
            if parts[:len(prefix)] == prefix and (path[-1] == 'contents' or len(parts) > len(prefix)):
                del state[0][existing]
        constant = _constant_operand(left)
        equal = left == right or constant is not None and constant == _constant_operand(right)
        covering = ('store', root, '/'.join(path[:-1] + ('contents',)))
        if equal and covering not in state[0]:
            state[0].pop(key, None)
        else:
            state[0][key] = self.role(right)
        # A complete set of element restorations also restores the slice after
        # an earlier copy whose source contents were unknown.
        if path[-1].startswith('index:') and covering in state[0]:
            initial = _project_value([root], list(path[:-1]), self.sources, self.structs)
            elements = _literal_elements(initial, self.sources)
            if elements is not None and len(elements) <= 32:
                restored = True
                for index, expected in enumerate(elements):
                    element_key = 'store', root, '/'.join(path[:-1] + ('index:' + str(index),))
                    actual = state[0].get(element_key)
                    actual = self.current(root, path[:-1] + ('index:' + str(index),), state)
                    if self.read(actual) != self.read(expected):
                        restored = False
                        break
                if restored:
                    for existing in list(state[0]):
                        if isinstance(existing, tuple) and existing[:2] == ('store', root) and tuple(
                                existing[2].split('/'))[:len(path) - 1] == path[:-1]:
                            del state[0][existing]

    def testing_callback(self, target, raw, arguments, values, state, opening):
        if not (len(target) >= 3 and target[-2] == '.' and target[-1] in {'Run', 'Cleanup'} and arguments):
            return None
        receiver = _value_type(target[:-2], self.parameter_types, self.structs, self.globals, self.sources)
        if ''.join(receiver).lstrip('*') not in {'testing.T', 'testing.B', 'testing.TB'}:
            return None
        a, b = arguments[-1]
        cursor = a
        while self.items[cursor:cursor + 1] == ['(']:
            cursor += 1
        body = _function_literal_body(self.items, self.pairs, cursor, b)
        saved = self.closure_values(values[-1], state, named=True) or [None]
        if self.items[cursor:cursor + 1] == ['func'] and body is not None:
            saved = [{'literal': (cursor + 1, body, self.pairs[body], opening),
                      'captures': self.literal_captures(state)}]
        owner = self.testing_method_owners.get(raw[0]) if len(raw) == 1 else None
        if owner is None:
            owner = self.testing_owner(raw[:-2], state)
        # Select the function value at registration, retaining the closure's
        # defining scopes for execution later, including deferred registration.
        context = {'test_boundary': target[-1] == 'Run', 'owner': owner, 'values': []}
        if target[-1] == 'Run':
            # Keep the parent's failure context for matching assertions,
            # but give the subtest its own handle and cleanup lifetime.
            testing_context = self.role(target[:-2])
            child = self.allocate(self.role(['testing-handle:',
                state[0].get(self.scope_key, ''), str(opening)]))
            self.sources[child], self.storage[child] = [testing_context], testing_context
            self.roots[child] = child, ()
            self.descriptors.add(child)
            self.parameter_types[child] = receiver
            self.testing_handles[child] = testing_context
            context['owner'] = self.testing_owner([child], state)
            context['values'] = [([child], None)]
        callbacks = [{**action, **context, 'literal': (*action['literal'][:3], opening), 'site': opening}
                     if action is not None else {'unresolved_callback': True, **context, 'site': opening}
                     for action in saved]
        return callbacks[0] if len(callbacks) == 1 else {'alternatives': callbacks, **context, 'site': opening}

    def mutations(self, first, last, state, *, skip=frozenset()):
        calls = sorted(_primary_calls(self.items, self.pairs, first, last), key=lambda call: self.pairs[call[1]])
        completed = set(skip)
        for callee, opening in calls:
            if opening in skip:
                continue
            if self.callback_paths_key in state[0]:
                def remaining(path):
                    self.mutations(first, last, path, skip=completed)
                    return path
                paths = self.run_paths(state[0][self.callback_paths_key], remaining)
                state[:] = self.merge(paths, state)[0]
                return
            completed.add(opening)
            guards = [(_constant_operand(self.fold_sizes(self.expression(a, b, state))), positive)
                      for a, b, positive in _call_guards(self.items, self.pairs, first, last, callee)]
            if any(value is not None and value[0] == 'bool' and value[1] != positive for value, positive in guards):
                continue
            optional = any(value is None or value[0] != 'bool' for value, _ in guards)
            previous = self.copy(state) if optional else None
            function_value = self.expression(callee, opening, state)
            target = _reference(function_value, self.sources, self.origins)
            arguments = _segments(self.items, opening + 1, self.pairs[opening], self.pairs)
            values = [self.expression(a, b, state) for a, b in arguments]
            locations = [self.location(self.expression(a, b, state, identity=True), state, follow=True) for a, b in arguments]
            raw = self.expression(callee, opening, state, identity=True)
            literal = _literal_invocation(self.items, self.pairs, callee, self.pairs[opening] + 1)
            stored_values = self.closure_values(function_value, state)
            if not stored_values:
                action = self.factory_literal(target, raw, state)
                if action is not None:
                    stored_values = [action]
            stored = stored_values[0] if len(stored_values) == 1 else None
            callback = self.testing_callback(target, raw, arguments, values, state, opening)
            if callback and target[-1] == 'Cleanup':
                self.cleanup_calls.add(opening)
                action = {**callback, 'site': opening}
                stacks = state[0].get(self.cleanup_key, [()])
                state[0][self.cleanup_key] = [stack + (action,) for stack in stacks]
                returned = []
            elif callback is not None and ('alternatives' in callback or callback.get('unresolved_callback')):
                state[:] = self.finish_action(callback, state)
                returned = []
            elif len(stored_values) > 1:
                actions = iter(stored_values)
                def execute(path):
                    action = next(actions)
                    if action is None:
                        self.closure_variants[opening] = [(None, None)]
                        return path, []
                    return self.invoke_literal({**action, 'values': list(zip(values, locations))}, path, opening)
                outcomes = self.run_paths([state] * len(stored_values), execute)
                state[:] = self.merge([path for path, _ in outcomes], state)[0]
                returned = [self.join_values({result[slot] for _, result in outcomes})
                            for slot in range(min(len(result) for _, result in outcomes))]
            elif stored is not None or callback is not None:
                action = callback or {**stored, 'values': list(zip(values, locations))}
                following, returned = self.invoke_literal(action, state, opening)
                state[:] = following
            elif literal is not None and literal[3] == opening:
                action = {'literal': literal, 'values': list(zip(values, locations)),
                          'captures': self.literal_captures(state)}
                following, returned = self.finish_literal(action, state, immediate=True)
                state[:] = following
                if literal[1] in self.abrupt_literals:
                    self.terminating_sites.add(opening)
                self.call_frames[opening] = self.frame(literal[1] + 1, literal[2])
            else:
                returned = self.invoke(target, values, locations, raw, state)
            if returned:
                span = callee, self.pairs[opening] + 1
                self.call_values[span] = returned
                for path in [state] + state[0].get(self.callback_paths_key, []):
                    results = path[0].pop(self.literal_results_key, returned)
                    path[0][self.call_results_key] = {**path[0].get(self.call_results_key, {}), span: results}
            if previous is not None:
                state[:] = self.merge([previous, state], previous)[0]

    def invoke_literal(self, action, state, opening):
        action = {**action, 'literal': (*action['literal'][:3], opening)}
        body = action['literal'][1]
        self.closure_calls[opening] = action['literal']
        if body in self.active_closures:
            return state, []
        self.active_closures.add(body)
        try:
            following, returned = self.finish_literal(action, state, immediate=True)
        finally:
            self.active_closures.remove(body)
        if body in self.abrupt_literals:
            self.terminating_sites.add(opening)
        self.call_frames[opening] = self.frame(body + 1, action['literal'][2])
        return following, returned

    def frame(self, first, last):
        return ({n: role for n, role in self.replacements.items() if first <= n < last},
                {span: value for span, value in self.expression_overrides.items() if first <= span[0] < last})

    def testing_owner(self, value, state):
        # Testing values may share an assertion context while having different
        # lifetimes. Resolve aliases by storage identity, before value folding.
        location = self.location(value, state, follow=True)
        return state[0].get(self.scope_key), location or tuple(value)

    def invoke(self, target, values, destinations, raw, state, *, captured_receiver=None):
        if target == ['copy'] and len(values) == 2:
            self.copy_contents(destinations[0], values[1], state)
        elif target == ['clear'] and values:
            self.clear_contents(destinations[0], state)
        elif target == ['append'] and len(values) > 1:
            region = self.region(destinations[0])
            if region is not None:
                root, path, offset, length, capacity = region
                source = values[1][:-1] if len(values) == 2 and values[1][-1:] == ['...'] else self.literal(values[1:])
                elements = self.elements(self.read(source))
                count = len(elements) if elements is not None else None
                if count == 0:
                    role = self.allocate(self.role(values[0]))
                    self.roots[role] = destinations[0]
                    self.descriptors.add(role)
                    kind = _value_type_raw(_expand_value(values[0], self.sources), self.parameter_types,
                                           self.structs, self.globals, self.sources)
                    self.parameter_types[role] = _underlying_type(kind, self.globals)
                    nominal = self.nominal_type(kind)
                    if nominal:
                        self.nominal_types[role] = nominal
                    return [role]
                if count != 0 and capacity is not None and length is not None and (
                        capacity == length or count is not None and count > capacity - length):
                    return  # append allocates, or appends no elements.
                if offset is not None and length is not None and capacity is not None:
                    if count != 0:
                        tail = path + (_slice_path([str(offset + length), str(offset + capacity)]),)
                        self.copy_contents((root, tail), source, state)
                    if count is not None:
                        view = root, path + (_slice_path([str(offset), str(offset + length + count), str(offset + capacity)]),)
                        role = self.allocate(self.role(self.reference_value(view, state)))
                        self.roots[role] = view
                        self.descriptors.add(role)
                        self.parameter_types[role] = _value_type(values[0], self.parameter_types,
                                                               self.structs, self.globals, self.sources)
                        nominal = self.nominal_type(_value_type_raw(_expand_value(values[0], self.sources), self.parameter_types,
                                                                   self.structs, self.globals, self.sources))
                        if nominal:
                            self.nominal_types[role] = nominal
                        return [role]
                else:
                    if offset is not None and length is not None and count is not None:
                        # An append may allocate, but it never overwrites the
                        # existing prefix. Keep possible tail writes visible
                        # to longer aliases without corrupting the old view.
                        for index, element in enumerate(elements):
                            self.write((root, path + ('index:' + str(offset + length + index),)),
                                       ['possible-append:'] + element, state)
                    else:
                        self.write(destinations[0], ['mutation:append'] + values[0], state)
        elif target == ['delete'] and values:
            key = self.read(values[1]) if len(values) > 1 else []
            if destinations[0] is not None and _constant_operand(key) is not None:
                root, path = destinations[0]
                self.write((root, path + (_key_path(key),)), ['nil'], state)
            else:
                self.write(destinations[0], ['mutation:'] + target, state)
        else:
            name = ''.join(target)
            receiver = None
            if len(target) >= 3 and target[-2] == '.':
                kind = _value_type(target[:-2], self.parameter_types, self.structs, self.globals, self.sources)
                method = ''.join(kind).lstrip('*') + '.' + target[-1]
                if method in self.effects or method in self.returns:
                    name, receiver = method, target[:-2]
            actual = {'parameter:' + str(slot): value for slot, value in enumerate(values)}
            locations = {'parameter:' + str(slot): value for slot, value in enumerate(destinations)}
            if receiver is not None:
                # Resolve the lexical receiver to its backing allocation.
                bound = raw[0] if len(raw) == 1 else None
                locations['receiver'] = (captured_receiver if captured_receiver is not None else
                    self.method_receivers.get(bound, self.location(raw[:-2], state, follow=True)))
                actual['receiver'] = (self.reference_value(locations['receiver'], state)
                                      if locations['receiver'] is not None else receiver)
            returned = []
            memo = {}
            for kind, effect in self.returns.get(name, ()):
                if 'references' in effect:
                    role = self.instantiate_reference(effect, actual, locations, state, memo)
                else:
                    value = self.instantiate_value(effect, actual, locations, state, memo)
                    role = self.role(self.capture_value(value, value, kind, state))
                self.parameter_types[role] = kind
                returned.append(role)
            writes = []
            for parameter, path, effect in self.effects.get(name, ()):
                location = locations.get(parameter)
                if location is None:
                    continue
                destination = self.resolve_location((location[0], location[1] + self.instantiate_path(path, actual)),
                                                    state, follow=False)
                if isinstance(effect, dict):
                    value = ([self.instantiate_reference(effect, actual, locations, state, memo)] if 'references' in effect
                             else self.instantiate_value(effect, actual, locations, state, memo))
                    writes.append((destination, value, True))
                    continue
                value = []
                for token in effect:
                    value += ['('] + actual[token] + [')'] if token in actual else [token]
                writes.append((destination, _expression_tokens(value), False))
            for location, value, reference in writes:
                if reference:
                    self.write_reference(location, value, state)
                else:
                    self.write(location, value, state)
            return returned

    def instantiate_path(self, path, actual):
        def replace(tokens):
            result = []
            for token in tokens:
                result += ['('] + actual[token] + [')'] if token in actual else [token]
            return self.read(_expression_tokens(result))
        result = []
        for part in path.split('/'):
            if part.startswith('key:'):
                result.append(_key_path(replace(json.loads(bytes.fromhex(part[4:]).decode()))))
            elif part.startswith('slice:'):
                bounds = json.loads(bytes.fromhex(part[6:]).decode())
                result.append(_slice_path([' '.join(replace(bound.split())) for bound in bounds]))
            elif part.startswith('index:'):
                result.append('index:' + ' '.join(replace(part[6:].split())))
            else:
                result.append(part)
        return tuple(result)

    def schedule(self, first, last, state):
        literal = _deferred_literal(self.items, self.pairs, first, last)
        if literal is not None:
            parameters, body, end, opening = literal
            self.mutations(opening + 1, self.pairs[opening], state)
            arguments = _segments(self.items, opening + 1, self.pairs[opening], self.pairs)
            types = _parameters(self.items, parameters + 1, self.pairs[parameters], self.pairs)
            values = []
            for slot, (a, b) in enumerate(arguments):
                kind = types[slot][1] if slot < len(types) else []
                reference = kind[:1] in (['*'], ['map']) or kind[:2] == ['[', ']']
                values.append((self.expression(a, b, state),
                               self.location(self.expression(a, b, state, identity=True), state, follow=True) if reference else None))
            action = {'literal': literal, 'values': values,
                      'captures': self.literal_captures(state)}
            stacks = state[0].get(self.deferred_key, [()])
            state[0][self.deferred_key] = [stack + (action,) for stack in stacks]
            return
        calls = list(_primary_calls(self.items, self.pairs, first + 1, last))
        outer = next(((callee, opening) for callee, opening in calls if self.pairs[opening] == last - 1), None)
        if outer is None:
            return
        callee, opening = outer
        self.mutations(first + 1, last, state, skip={opening})
        target = _reference(self.expression(callee, opening, state), self.sources, self.origins)
        arguments = _segments(self.items, opening + 1, self.pairs[opening], self.pairs)
        values, locations, refresh = [], [], []
        for a, b in arguments:
            value = self.expression(a, b, state)
            location = self.location(self.expression(a, b, state, identity=True), state, follow=True)
            identity = self.expression(a, b, state, identity=True)
            kind = (_value_type(identity, self.parameter_types, self.structs, self.globals, self.sources) or
                    _value_type(value, self.parameter_types, self.structs, self.globals, self.sources))
            reference = location if not kind or kind[:1] in (['*'], ['map']) or kind[:2] == ['[', ']'] else None
            values.append((value, reference))
            locations.append(location)
            if reference is not None:
                refresh += [(position, self.lookup(state, self.items[position])) for position in range(a, b)
                            if self.lookup(state, self.items[position]) is not None and
                            (position == a or self.items[position - 1] != '.')]
        action = {'target': target, 'values': values, 'locations': locations,
                  'raw': self.expression(callee, opening, state, identity=True), 'refresh': refresh,
                  'arguments': arguments}
        callback = self.testing_callback(target, action['raw'], arguments,
                                         [value for value, _ in values], state, opening)
        if callback is not None:
            action = {**callback, 'site': opening}
            if target[-1] == 'Cleanup':
                action['register_cleanup'] = True
            stacks = state[0].get(self.deferred_key, [()])
            state[0][self.deferred_key] = [stack + (action,) for stack in stacks]
            return
        stored = self.closure_values(self.expression(callee, opening, state), state)
        if not stored:
            factory = self.factory_literal(target, action['raw'], state)
            if factory is not None:
                stored = [factory]
        if stored:
            alternatives = [{**choice, 'values': ([(values[0][0], locations[0])] + values[1:]
                                                   if choice.get('receiver_argument') and values else values), 'site': opening,
                             'literal': (*choice['literal'][:3], opening)} if choice is not None else
                            {'unresolved_callback': True, 'site': opening} for choice in stored]
            alternatives = [self.bind_receiver_argument(choice, state) for choice in alternatives]
            action = alternatives[0] if len(alternatives) == 1 else {'alternatives': alternatives, 'site': opening}
            stacks = state[0].get(self.deferred_key, [()])
            state[0][self.deferred_key] = [stack + (action,) for stack in stacks]
            return
        if len(target) >= 3 and target[-2] == '.':
            kind = _value_type(target[:-2], self.parameter_types, self.structs, self.globals, self.sources)
            method = ''.join(kind).lstrip('*') + '.' + target[-1]
            receiver_kind = self.receiver_types.get(method)
            if receiver_kind is not None:
                raw = action['raw']
                receiver = self.method_receivers.get(raw[0]) if len(raw) == 1 else self.location(raw[:-2], state, follow=True)
                if receiver is not None and receiver_kind[:1] != ['*']:
                    # Value receivers copy their aggregate at registration;
                    # its slice fields still share the captured backing arrays.
                    root, path = receiver
                    value = _project_value(self.raw_snapshot(root, state), list(path), self.sources,
                                           self.structs, self.descriptors)
                    receiver = self.allocate(self.role(value)), ()
                action['receiver'] = receiver
        stacks = state[0].get(self.deferred_key, [()])
        state[0][self.deferred_key] = [stack + (action,) for stack in stacks]

    def finish_action(self, action, current):
        if action.get('register_cleanup'):
            callback = {key: value for key, value in action.items() if key != 'register_cleanup'}
            stacks = current[0].get(self.cleanup_key, [()])
            current[0][self.cleanup_key] = [stack + (callback,) for stack in stacks]
            return current
        if 'alternatives' in action:
            opening = action['site']
            variants = list(self.closure_variants.get(opening, ()))
            states = []
            for alternative in action['alternatives']:
                self.closure_calls.pop(opening, None)
                self.call_frames.pop(opening, None)
                states.append(self.finish_action(alternative, self.copy(current)))
                literal = self.closure_calls.get(opening)
                frame = self.call_frames.get(opening)
                variant = literal, frame
                if variant not in variants:
                    variants.append(variant)
            self.closure_calls.pop(opening, None)
            self.call_frames.pop(opening, None)
            self.closure_variants[opening] = variants
            return self.merge(states, current)[0]
        if action.get('unresolved_callback'):
            self.closure_variants[action['site']] = [(None, None)]
            return current
        if 'literal' in action:
            body = action['literal'][1]
            if body in self.active_closures:
                return current
            self.active_closures.add(body)
            try:
                current = self.finish_literal(action, current)
            finally:
                self.active_closures.remove(body)
            if 'site' in action:
                opening = action['site']
                self.closure_calls[opening] = action['literal']
                frame = self.frame(action['literal'][1] + 1, action['literal'][2])
                previous = self.call_frames.get(opening)
                if previous is not None:
                    merged = []
                    for slot, (left, right) in enumerate(zip(previous, frame)):
                        values = dict(left)
                        for key, value in right.items():
                            if key not in values or values[key] == value:
                                values[key] = value
                                continue
                            choices = sorted({values[key], value} if slot == 0 else
                                             {self.role(values[key]), self.role(value)})
                            joined = self.role(['cleanup-choice:'] + choices)
                            values[key] = joined if slot == 0 else [joined]
                        merged.append(values)
                    frame = tuple(merged)
                self.call_frames[opening] = frame
            return current
        for position, role in action['refresh']:
            choices = self.deferred_bindings.setdefault(position, set())
            choices.add(self.snapshot(role, current))
        values = [self.reference_value(location, current) if location is not None else value
                  for value, location in action['values']]
        for span, value, (_, reference) in zip(action['arguments'], values, action['values']):
            captured = [self.role(value)] if reference is not None else value
            self.deferred_expressions.setdefault(span, set()).add(tuple(captured))
        self.invoke(action['target'], values, action['locations'], action['raw'], current,
                    captured_receiver=action.get('receiver'))
        return current

    def finish(self, state, *, run_cleanups=True):
        if self.callback_paths_key in state[0]:
            paths = self.run_paths(state[0][self.callback_paths_key],
                                   lambda path: self.finish(path, run_cleanups=run_cleanups))
            return self.merge(paths, state)[0]
        stacks = state[0].get(self.deferred_key, [()])
        owner = state[0].get(self.testing_owner_key)
        states = []
        for stack in stacks:
            current = self.copy(state)
            current[0].pop(self.deferred_key, None)
            for action in reversed(stack):
                current = self.finish_action(action, current)
            if not run_cleanups:
                states.append(current)
                continue
            # Alternative cleanup stacks represent separate exits. Keep them
            # separate, and run callbacks only at the testing boundary.
            pending = [current]
            while pending:
                current = pending.pop()
                cleanups = current[0].pop(self.cleanup_key, [()])
                for cleanup in cleanups:
                    following = self.copy(current)
                    selected = next((index for index in range(len(cleanup) - 1, -1, -1)
                                     if owner is None or cleanup[index].get('owner') == owner), None)
                    if selected is None:
                        if cleanup:
                            following[0][self.cleanup_key] = [cleanup]
                        states.append(following)
                        continue
                    following[0][self.cleanup_key] = [cleanup[:selected] + cleanup[selected + 1:]]
                    following = self.finish_action(cleanup[selected], following)
                    pending.append(following)
        template = self.copy(state)
        template[0].pop(self.deferred_key, None)
        if run_cleanups:
            template[0].pop(self.cleanup_key, None)
        return self.merge(states, template)[0]

    def literal_captures(self, state):
        return [(scope.get(self.scope_key), {name: value for name, value in scope.items()
                 if isinstance(name, str) and not name.startswith('metadata:')}) for scope in state]

    def bind_receiver_argument(self, action, state):
        if action.get('receiver_argument') and action['values']:
            (parameter, kind), _ = action['receiver_binding']
            value, location = action['values'][0]
            if kind[:1] == ['*']:
                self.retain_receiver_storage(location)
            if location is not None:
                root, path = location
                value = _project_value(self.raw_snapshot(root, state), list(path), self.sources,
                                       self.structs, self.descriptors)
            else:
                value = self.capture_value(value, value, kind, state)
            action = {**action, 'values': action['values'][1:],
                      'receiver_binding': ((parameter, kind), (value, location if kind[:1] == ['*'] else None))}
            action.pop('receiver_argument')
        return action

    def finish_literal(self, action, state, *, immediate=False):
        action = self.bind_receiver_argument(action, state)
        parameters, body, end, _ = action['literal']
        captures = []
        for scope_id, names in action['captures']:
            live = next((scope for scope in state if scope.get(self.scope_key) == scope_id), {})
            captures.append({self.scope_key: scope_id, **{
                name: live.get(name, state[0].get(('capture', scope_id, name), value))
                for name, value in names.items()}})
        captures[0].update({key: value for key, value in state[0].items() if isinstance(key, tuple)})
        boundary = action.get('test_boundary', False)
        captures[0][self.testing_owner_key] = (action['owner'] if boundary else
                                              state[0].get(self.testing_owner_key))
        if self.cleanup_key in state[0]:
            captures[0][self.cleanup_key] = state[0][self.cleanup_key]
        invocation = self.literal_invocations.get(body, 0)
        self.literal_invocations[body] = invocation + 1
        local = {self.scope_key: 'closure:' + str(body) + ':' + str(invocation)}
        bindings = list(zip(_parameters(self.items, parameters + 1, self.pairs[parameters], self.pairs), action['values']))
        if 'receiver_binding' in action:
            bindings.insert(0, action['receiver_binding'])
        for (position, kind), (value, location) in bindings:
            reference = (kind[:1] in (['*'], ['map']) or kind[:2] == ['[', ']'] or
                         _underlying_type(kind, self.globals) == ['testing', '.', 'TB'])
            if location is not None and reference:
                role = self.allocate(self.role(value))
                self.roots[role] = location
            else:
                role = self.role(value)
                if 'receiver_binding' in action and position == action['receiver_binding'][0][0]:
                    role = self.allocate(role)
                if len(value) == 1 and value[0].isidentifier() and value[0] not in {'nil', 'true', 'false'}:
                    self.literal_symbols[role] = value[0]
            self.parameter_types[role] = kind
            local[self.items[position]] = role
        old_returns, old_controls, old_results = self.return_states, self.controls, self.result_fields
        self.return_states, self.controls = [], []
        self.result_fields = _result_fields(self.items[self.pairs[parameters] + 1:body])
        for name, kind in self.result_fields:
            if name:
                local[name] = self.role(['zero:'] + kind)
        following, live = self.block(body + 1, end, captures + [local], scoped=False)
        exits = ([following] if live else []) + self.return_states
        for exit in exits:
            self.close_scope(exit)
        exits = [self.finish(exit, run_cleanups=boundary) for exit in exits]
        results = self.result_fields
        self.return_states, self.controls, self.result_fields = old_returns, old_controls, old_results
        if not exits:
            if not _recovers(self.items, self.pairs, body + 1, end):
                self.abrupt_literals.add(body)
            return (state, []) if immediate else state
        exits = [path for exit in exits for path in exit[0].get(self.callback_paths_key, [exit])]
        for exit in exits:
            values = [self.lookup(exit, name) if name else exit[0].get('metadata:result:' + str(slot))
                      for slot, (name, _) in enumerate(results)]
            if values and None not in values:
                exit[0][self.literal_results_key] = values
        merged, _ = self.merge(exits, captures + [local])

        def project_exit(source_state):
            result = self.copy(state)
            result[0].pop(self.callback_paths_key, None)
            for key in list(result[0]):
                if isinstance(key, tuple):
                    del result[0][key]
            result[0].update({key: value for key, value in source_state[0].items() if isinstance(key, tuple)})
            for key in (self.cleanup_key, self.literal_results_key):
                result[0].pop(key, None)
                if key in source_state[0]:
                    result[0][key] = source_state[0][key]
            for scope_id, names in action['captures']:
                destination = next((scope for scope in result if scope.get(self.scope_key) == scope_id), None)
                source = next((scope for scope in source_state if scope.get(self.scope_key) == scope_id), {})
                if destination is not None:
                    destination.update({name: source[name] for name in names if name in source})
                else:
                    for name, value in names.items():
                        cell = ('capture', scope_id, name)
                        self.capture_defaults.setdefault(cell, value)
                        result[0][cell] = source.get(name, value)
            return result

        result = project_exit(merged)
        if self.callback_paths_key in merged[0]:
            result[0][self.callback_paths_key] = [project_exit(path) for path in merged[0][self.callback_paths_key]]
        for position in range(body + 1, end):
            if position in self.replacements:
                self.deferred_bindings.setdefault(position, set()).add(self.replacements[position])
        returned = []
        for slot, (name, kind) in enumerate(results):
            roles = {self.lookup(exit, name) if name else exit[0].get('metadata:result:' + str(slot)) for exit in exits}
            if None in roles:
                break
            role = self.join_values(roles)
            self.parameter_types[role] = kind
            returned.append(role)
        return (result, returned) if immediate else result

    def close_scope(self, state):
        for path in state[0].get(self.callback_paths_key, ()):
            self.close_scope(path)
        scope_id = state[-1].get(self.scope_key)
        # A stored closure can escape before it is registered as a callback.
        # Preserve its captured cells in path-local state when their lexical
        # scope ends, rather than reverting to the values at definition time.
        for action in self.closures.values():
            for key, names in action['captures']:
                if key == scope_id:
                    for name, value in names.items():
                        cell = ('capture', scope_id, name)
                        self.capture_defaults.setdefault(cell, value)
                        state[0][cell] = state[-1].get(name, value)
        def refresh(action):
            if 'alternatives' in action:
                action = {**action, 'alternatives': [refresh(choice) for choice in action['alternatives']]}
            if 'captures' in action:
                captures = [(key, {name: state[-1].get(name, value) for name, value in names.items()}
                             if key == scope_id else names) for key, names in action['captures']]
                action = {**action, 'captures': captures}
            return action

        for queue in (self.deferred_key, self.cleanup_key):
            stacks = []
            for stack in state[0].get(queue, []):
                updated = []
                for action in stack:
                    updated.append(refresh(action))
                stacks.append(tuple(updated))
            if stacks:
                state[0][queue] = stacks

    def run_paths(self, states, operation):
        """Analyze correlated states without letting the last path hide checks."""
        names = ('replacements', 'expression_overrides', 'call_values',
                 'closure_calls', 'call_frames', 'closure_variants', 'loop_frames')
        original = {name: dict(getattr(self, name)) for name in names}
        records, results = [], []
        for state in states:
            for name in names:
                setattr(self, name, dict(original[name]))
            results.append(operation(self.copy(state)))
            records.append({name: dict(getattr(self, name)) for name in names})
        for name in names:
            setattr(self, name, dict(original[name]))
        for name in names[:3]:
            destination = getattr(self, name)
            for key in set().union(*(record[name] for record in records)):
                values = [record[name][key] for record in records if key in record[name]]
                if all(value == values[0] for value in values):
                    destination[key] = values[0]
                elif name == 'replacements':
                    destination[key] = self.role(['path-choice:'] + sorted(set(values)))
                elif name == 'expression_overrides':
                    destination[key] = [self.role(['path-choice:'] + sorted({self.role(value) for value in values}))]
                else:
                    destination[key] = [self.join_values({value[slot] for value in values})
                                        for slot in range(min(map(len, values)))]
        sites = set().union(*(set(record['closure_calls']) | set(record['closure_variants']) for record in records))
        for opening in sites:
            variants = []
            for record in records:
                if opening not in record['closure_calls'] and opening not in record['closure_variants']:
                    # This may be a call inside another, mutually exclusive
                    # literal. Its enclosing callback records that selection.
                    # An executed but unresolved callback has an explicit
                    # (None, None) entry and must still remain an alternative.
                    continue
                for variant in record['closure_variants'].get(opening, [(
                        record['closure_calls'].get(opening), record['call_frames'].get(opening))]):
                    if variant not in variants:
                        variants.append(variant)
            self.closure_calls.pop(opening, None)
            self.call_frames.pop(opening, None)
            self.closure_variants.pop(opening, None)
            if len(variants) == 1 and variants[0][0] is not None:
                self.closure_calls[opening], frame = variants[0]
                if frame is not None:
                    self.call_frames[opening] = frame
            else:
                self.closure_variants[opening] = variants
        for record in records:
            for opening, frames in record['loop_frames'].items():
                destination = self.loop_frames.setdefault(opening, [])
                self.loop_frames[opening] = destination + [frame for frame in frames if frame not in destination]
        return results

    def join_values(self, roles):
        if len(roles) == 1:
            return next(iter(roles))
        joined = self.join_references(roles)
        if joined is not None:
            return joined
        choices = frozenset(choice for role in roles for choice in self.choices.get(role, {role}))
        role = 'local:' + hashlib.sha256(repr(('join', sorted(choices))).encode()).hexdigest()[:16]
        self.choices[role], self.sources[role] = choices, sorted(choices)
        return role

    def contains_closure(self, role, seen=frozenset()):
        return role in self.closures or role not in seen and any(
            self.contains_closure(token, seen | {role}) for token in self.sources.get(role, ()))

    def merge(self, states, template):
        if not states:
            return self.copy(template), False
        states = [self.copy(path[:len(template)]) for state in states
                  for path in state[0].get(self.callback_paths_key, [state])]
        result = self.copy(template)
        result[0].pop(self.callback_paths_key, None)
        callback_change, value_change = False, False
        for depth, scope in enumerate(template):
            names = set(scope)
            if depth == 0:
                names.update(key for state in states for key in state[0] if isinstance(key, tuple))
                if any(self.deferred_key in state[0] for state in states):
                    names.add(self.deferred_key)
                if any(self.cleanup_key in state[0] for state in states):
                    names.add(self.cleanup_key)
                if any(self.literal_results_key in state[0] for state in states):
                    names.add(self.literal_results_key)
            for name in sorted(names, key=repr):
                if name == self.callback_paths_key:
                    continue
                if name == self.call_results_key:
                    result[0].pop(name, None)
                    continue
                if name == self.literal_results_key:
                    values = [state[0].get(name, []) for state in states]
                    roles = [{value[slot] for value in values} for slot in range(min(map(len, values)))]
                    result[0][name] = [self.join_values(choices) for choices in roles]
                    callback_change = callback_change or any(len(choices) > 1 and any(
                        self.contains_closure(role) for role in choices) for choices in roles)
                    continue
                if name == self.testing_owner_key:
                    # The active testing boundary is execution metadata, not
                    # a Go value to snapshot or join with variable bindings.
                    result[depth][name] = scope[name]
                    continue
                if name in {self.deferred_key, self.cleanup_key}:
                    stacks = {repr(stack): stack for state in states
                              for stack in state[0].get(name, [()])}
                    result[0][name] = list(stacks.values())
                    callback_change = callback_change or len({repr(state[0].get(name, [()])) for state in states}) > 1
                    continue
                original = scope.get(name)
                if isinstance(name, tuple):
                    original = (self.capture_defaults[name] if name[0] == 'capture' else
                                self.default(name[1], tuple(name[2].split('/'))))
                if original is None:
                    original = self.default(name[1], tuple(name[2].split('/')))
                roles = {state[depth].get(name, original) for state in states}
                if len(roles) > 1:
                    callback_change = callback_change or any(self.contains_closure(role) for role in roles)
                    value_change = value_change or not all(role in self.closures or role in self.choices and all(
                        choice in self.closures for choice in self.choices[role]) for role in roles)
                joined = self.join_references(roles) if len(roles) > 1 else None
                if joined is not None:
                    result[depth][name] = joined
                    continue
                if not isinstance(name, tuple):
                    values = {self.snapshot(state[depth].get(name, original), state) for state in states}
                    if len(values) == 1 and len(roles) > 1:
                        result[depth][name] = self.allocate(next(iter(values)))
                        continue
                if len(roles) == 1:
                    result[depth][name] = next(iter(roles))
                    continue
                choices = frozenset(choice for role in roles for choice in self.choices.get(role, {role}))
                if len(choices) == 1:
                    result[depth][name] = next(iter(choices))
                    continue
                role = 'local:' + hashlib.sha256(repr(('join', sorted(choices))).encode()).hexdigest()[:16]
                self.choices[role] = choices
                self.sources[role] = sorted(choices)
                result[depth][name] = role
        # Keep the whole state associated with a selected callback. Separately
        # joining its function value and captured stores invents combinations
        # that no Go execution can reach. Beyond the bounded disjunction use
        # the existing conservative joined state, never choose a single path.
        if callback_change and value_change and 1 < len(states) <= 64 and self.callback_path_splits + len(states) <= 64:
            self.callback_path_splits += len(states) - 1
            result[0][self.callback_paths_key] = states
        return result, True

    def simple(self, first, last, state):
        items, pairs = self.items, self.pairs
        self.expression(first, last, state)
        if first >= last:
            return state
        if items[first] == 'defer':
            self.schedule(first, last, state)
            return state
        if items[first] == 'go':
            outer = next(((a, b) for a, b in _primary_calls(items, pairs, first + 1, last)
                          if pairs[b] == last - 1), None)
            literal = _literal_invocation(items, pairs, first + 1, last)
            joined = False
            if literal is not None:
                parameters, body, closing, _ = literal
                parts = [(a, b) for a, b in _segments(items, body + 1, closing, pairs, ';') if a < b]
                tail = items[slice(*parts[-1])] if parts else []
                following = last
                while items[following:following + 1] == [';']:
                    following += 1
                if len(tail) == 4 and tail[:2] == ['close', '('] and tail[-1] == ')':
                    channel = tail[2]
                    role = self.lookup(state, channel)
                    source = _expand_value([role], self.sources) if role else []
                    arguments = (_segments(source, 2, len(source) - 1, _pairs(source))
                                 if source[:2] == ['make', '('] else [])
                    unbuffered = (len(arguments) == 1 or len(arguments) == 2 and
                                  self.integer(source[slice(*arguments[1])]) == 0)
                    joined = (unbuffered and items[following:following + 3] == ['<', '-', channel]
                              and pairs[parameters] == parameters + 1
                              and items[body + 1:closing].count('close') == 1)
            if joined:
                self.joined_go.add(first)
            else:
                # The call arguments are evaluated by the launching goroutine;
                # its asynchronous body cannot prove a completed assertion.
                self.mutations(first + 1, last, state, skip={outer[1]} if outer else set())
                return state
        if items[first] in {'var', 'const'}:
            start = first + 1
            specs = _segments(items, start + 1, pairs[start], pairs, ';') if start < last and items[start] == '(' and start in pairs else [(start, last)]
            for start, stop in specs:
                if start >= stop or not items[start].isidentifier():
                    continue
                names = [start]
                while names[-1] + 2 < stop and items[names[-1] + 1] == ',':
                    names.append(names[-1] + 2)
                assignment = next((n for n in range(names[-1] + 1, stop) if items[n] == '='), None)
                right = assignment + 1 if assignment is not None else names[-1] + 1
                self.mutations(right, stop, state)
                self.assign(names, right, stop, state, declare=True, uninitialized=assignment is None,
                            declared_kind=items[names[-1] + 1:assignment] if assignment is not None else None)
            return state
        self.mutations(first, last, state)
        cursor = first
        while cursor < last and items[cursor] not in {':=', '='}:
            if cursor in pairs:
                cursor = pairs[cursor]
            cursor += 1
        if cursor < last:
            if items[cursor] == '=' and cursor > first and items[cursor - 1] in {'+', '-', '*', '/', '%', '|', '&', '^'}:
                operator = items[cursor - 1]
                location = self.location(self.expression(first, cursor - 1, state, identity=True), state)
                value = self.expression(cursor + 1, last, state)
                if location is not None:
                    root, path = location
                    old = _project_value([self.snapshot(root, state)], list(path), self.sources, self.structs)
                    changed = ['('] + old + [')', operator, '('] + value + [')']
                    if path:
                        self.write(location, changed, state)
                    elif cursor == first + 2:
                        for scope in reversed(state):
                            if items[first] in scope:
                                scope[items[first]] = self.role(changed)
                                break
                return state
            lhs = items[first:cursor]
            names = list(range(first, cursor, 2))
            if lhs and all(items[n].isidentifier() for n in names) and all(
                    items[n] == ',' for n in range(first + 1, cursor, 2)):
                self.assign(names, cursor + 1, last, state, declare=items[cursor] == ':=')
            else:
                left = _segments(items, first, cursor, pairs)
                right = _segments(items, cursor + 1, last, pairs)
                writes = []
                for slot, (a, b) in enumerate(left):
                    if len(right) not in {1, len(left)}:
                        break
                    c, d = right[slot] if len(right) == len(left) else right[0]
                    result_slot = slot if len(right) == 1 else 0
                    writes.append((self.location(self.expression(a, b, state, identity=True), state),
                        self.expression(c, d, state, slot=result_slot),
                        self.expression(c, d, state, identity=True, slot=result_slot),
                        _value_type_raw(self.expression(a, b, state, identity=True), self.parameter_types,
                                        self.structs, self.globals, self.sources)))
                for location, value, identity, kind in writes:
                    captured = self.capture_value(value, identity, kind, state, declared=True)
                    if captured != value:
                        if location is not None:
                            self.indirect(location, state)
                        self.write_reference(location, captured, state)
                    else:
                        self.write(location, value, state)
        elif items[last - 1] in {'++', '--'}:
            location = self.location(self.expression(first, last - 1, state, identity=True), state)
            if location is not None:
                root, path = location
                old = _project_value([self.snapshot(root, state)], list(path), self.sources, self.structs)
                changed = ['('] + old + [')', '+' if items[last - 1] == '++' else '-', '1']
                if path:
                    self.write(location, changed, state)
                elif last == first + 2:
                    for scope in reversed(state):
                        if items[first] in scope:
                            scope[items[first]] = self.role(changed)
                            break
        return state

    def assign(self, names, right, last, state, *, declare=False, uninitialized=False, declared_kind=None):
        if self.callback_paths_key in state[0]:
            def bind(path):
                self.assign(names, right, last, path, declare=declare, uninitialized=uninitialized,
                            declared_kind=declared_kind)
                return path
            paths = self.run_paths(state[0][self.callback_paths_key], bind)
            template = self.copy(state)
            if declare:
                template[-1].update({self.items[position]: self.lookup(paths[0], self.items[position])
                                     for position in names if self.items[position] != '_'})
            state[:] = self.merge(paths, template)[0]
            return
        segments = _segments(self.items, right, last, self.pairs)
        values = []
        for slot, _ in enumerate(names):
            first, stop = segments[slot] if len(segments) == len(names) else (right, last)
            result_slot = slot if len(segments) != len(names) else 0
            value = self.expression(first, stop, state, helpers=True, slot=result_slot)
            if uninitialized:
                value = ['zero:'] + value
            role = self.role(value, 0 if len(segments) == len(names) else slot)
            original = self.expression(first, stop, state, slot=result_slot)
            target = _reference(original, self.sources, self.origins)
            if len(target) == 1 and target[0] in self.helpers:
                # Signatures normalize return-value provenance across renames;
                # function values still identify the exact lexical declaration.
                role = self.role(['function-reference:', target[0]])
                self.origins[role] = target
            identity = self.expression(first, stop, state, identity=True, slot=result_slot)
            kind = (declared_kind or _value_type(identity, self.parameter_types, self.structs, self.globals, self.sources) or
                    _value_type(value, self.parameter_types, self.structs, self.globals, self.sources))
            location = self.location(identity, state, follow=True)
            captured = self.capture_value(value, identity, kind, state, declared=bool(declared_kind))
            if captured != value:
                role = self.role(captured)
            if len(target) >= 3 and target[-2] == '.':
                receiver_type = _value_type(target[:-2], self.parameter_types, self.structs, self.globals, self.sources)
                method = ''.join(receiver_type).lstrip('*') + '.' + target[-1]
                if (target[-1] in {'Run', 'Cleanup'} and ''.join(receiver_type).lstrip('*') in
                        {'testing.T', 'testing.B', 'testing.TB'}):
                    owner = self.testing_method_owners.get(identity[0]) if len(identity) == 1 else None
                    if owner is None:
                        owner = self.testing_owner(identity[:-2], state)
                    role = self.role(['testing-method:', target[-1], repr(owner)])
                    self.origins[role] = target
                    self.testing_method_owners[role] = owner
                if method in self.effects or method in self.returns or self.returns_function(method):
                    bound = self.method_receivers.get(identity[0]) if len(identity) == 1 else None
                    receiver = bound if bound is not None else self.location(identity[:-2], state, follow=True)
                    if receiver is not None:
                        receiver_kind = self.receiver_types.get(method)
                        if receiver_kind is not None and receiver_kind[:1] == ['*']:
                            self.retain_receiver_storage(receiver)
                        if bound is None and receiver_kind is not None and receiver_kind[:1] != ['*']:
                            owner, path = receiver
                            copied = _project_value(self.raw_snapshot(owner, state), list(path), self.sources,
                                                    self.structs, self.descriptors)
                            receiver = self.allocate(self.role(copied)), ()
                        role = self.role(['method-reference:', method, repr(receiver)])
                        self.origins[role] = target
                        self.method_receivers[role] = receiver
            reference = kind[:1] in (['*'], ['map']) or kind[:2] == ['[', ']']
            if role in self.descriptors:
                pass
            elif reference and location is not None:
                # Slices, maps and pointers retain backing storage through aliases.
                alias = self.allocate(role)
                self.roots[alias] = location
                role = alias
            elif ''.join(kind) in self.structs or kind[:1] in (['*'], ['map'], ['[']):
                role = self.allocate(role)
            self.origins.setdefault(role, original)
            if kind:
                self.parameter_types[role] = kind
            values.append(role)
        for position, role in zip(names, values):
            name = self.items[position]
            if name == '_':
                continue
            if declare:
                state[-1][name] = role
            else:
                for scope in reversed(state):
                    if name in scope:
                        previous = scope[name]
                        root, path = self.roots.get(previous, (previous, ()))
                        if root in self.addressed_roots and not path and ''.join(self.parameter_types.get(previous, [])) in self.structs:
                            # Implicit &value receiver selection points at the
                            # variable's storage, including whole-value writes.
                            self.write_reference((root, ()), [role], state)
                        else:
                            scope[name] = role
                        break

    def failed(self, first, last, state):
        tokens = self.expression(first, last, state)
        pairs, cursor = _pairs(tokens), 0
        while cursor < len(tokens):
            if tokens[cursor] == 'func':
                body = _function_literal_body(tokens, pairs, cursor, len(tokens))
                cursor = pairs.get(body, len(tokens)) + 1
                continue
            if tokens[cursor] != '(' or not cursor:
                cursor += 1
                continue
            start = cursor - 1
            while start >= 2 and tokens[start - 1] == '.':
                start -= 2
            target = tokens[start:cursor]
            seen = set()
            while len(target) == 1 and target[0] in self.sources and target[0] not in seen:
                seen.add(target[0])
                target = self.sources[target[0]]
            if len(target) >= 3 and target[-2] == '.' and target[-1] in {
                    'Error', 'Errorf', 'Fatal', 'Fatalf', 'Fail', 'FailNow'}:
                kind = _value_type(target[:-2], self.parameter_types, self.structs, self.globals, self.sources)
                if ''.join(kind).lstrip('*') in {'testing.T', 'testing.B', 'testing.TB'}:
                    return True
            cursor += 1
        return False

    def stopped(self, first, last, state):
        if self.items[first:first + 1] in (['defer'], ['go']):
            return False
        for callee, opening in _primary_calls(self.items, self.pairs, first, last):
            target = _reference(self.expression(callee, opening, state), self.sources, self.origins)
            if ''.join(target) not in self.nonreturning:
                continue
            guards = _call_guards(self.items, self.pairs, first, last, callee)
            if all(_constant_value(self.expression(a, b, state), self.sources) == ('bool', positive)
                   for a, b, positive in guards):
                self.terminating_sites.add(opening)
                return True
        return False

    def body_opening(self, first, last):
        return _control_body(self.items, self.pairs, first, last)

    def counted_loop(self, header, opening, closing, control, template, active, label):
        """Execute small proven integer loops; keep the generic join otherwise."""
        if len(header) != 3:
            return None
        items = self.items
        a, b = header[0]
        c, d = header[1]
        e, f = header[2]
        if (b - a < 3 or items[a + 1] not in {':=', '='} or not items[a].isidentifier()
                or d - c < 3 or items[c] != items[a] or items[c + 1] not in {'<', '<=', '>', '>='}
                or items[e:f] not in ([items[a], '++'], [items[a], '--'])):
            return None
        name = items[a]
        bounds = {token for token in items[c + 2:d] if token.isidentifier()}
        def changed(identifiers, first, last):
            for position in range(first, last):
                if items[position] not in identifiers:
                    continue
                if (items[position - 1] == '&' or
                        items[position + 1] in {'=', ':=', '++', '--'} or
                        items[position + 2:position + 3] == ['=']):
                    return True
                following = position + 1
                while items[following:following + 1] == [','] and following + 1 < last:
                    following += 2
                if following > position + 1 and items[following:following + 1] in (['='], [':=']):
                    return True
            return False
        mutable_bound = changed(bounds, opening + 1, closing)
        # Bounds held by address or a stored closure are mutable even when
        # their assignments occur outside the loop's literal body.
        if any(items[n:n + 2] == ['&', bound] for bound in bounds for n in range(opening)):
            return None
        scopes = {scope.get(self.scope_key) for scope in control}
        for action in self.closures.values():
            captured = {key for scope, names in action['captures'] if scope in scopes for key in names}
            if changed(bounds & captured, action['literal'][1] + 1, action['literal'][2]):
                mutable_bound = True
        for position in range(opening + 1, closing):
            if items[position] == name and (items[position - 1] == '&' or
                    items[position + 1] in {'=', ':=', '++', '--'} or
                    items[position + 2:position + 3] == ['=']):
                return None
        start = _constant_value(self.expression(a + 2, b, control), self.sources)
        bound = _constant_value(self.expression(c + 2, d, control), self.sources)
        if not start or not bound or start[0] not in {'integer', 'rune'} or bound[0] not in {'integer', 'rune'}:
            return None
        index, limit = start[1], bound[1]
        if max(abs(index), abs(limit)) > 1_000_000:
            return None
        compare = {'<': lambda n: n < limit, '<=': lambda n: n <= limit,
                   '>': lambda n: n > limit, '>=': lambda n: n >= limit}[items[c + 1]]
        count, step = 0, 1 if items[f - 1] == '++' else -1
        while compare(index) and count <= 64:
            count, index = count + 1, index + step
        if count > 64:
            return None
        if mutable_bound:
            # Prove the actual iteration count in isolation before updating
            # caller state. A bound can shrink or grow inside the body.
            trial = deepcopy(self)
            incoming = trial.copy(control)
            trial.simple(*header[0], incoming)
            context = {'label': label, 'break': [], 'continue': []}
            trial.controls.append(context)
            edges, count = [incoming], 0
            while edges:
                incoming, _ = trial.merge(edges, incoming)
                condition = _constant_value(trial.expression(*header[1], incoming), trial.sources)
                if condition == ('bool', False):
                    break
                if condition != ('bool', True) or count == 64:
                    return None
                back, live = trial.block(opening + 1, closing, incoming, active=active)
                edges = ([back] if live else []) + context['continue']
                context['continue'] = []
                for edge in edges:
                    trial.simple(*header[2], edge)
                count += 1
        self.simple(*header[0], control)
        context = {'label': label, 'break': [], 'continue': []}
        self.controls.append(context)
        frames, edges = [], [control]
        for _ in range(count):
            if not edges:
                break
            incoming, _ = self.merge(edges, control)
            self.expression(*header[1], incoming)
            back, live = self.block(opening + 1, closing, incoming, active=active)
            frames.append(self.frame(opening + 1, closing))
            edges = ([back] if live else []) + context['continue']
            context['continue'] = []
            for edge in edges:
                self.simple(*header[2], edge)
        self.controls.pop()
        self.loop_frames[opening] = frames
        merged, live = self.merge(context['break'] + edges, template)
        return closing + 1, merged, live

    def end(self, first, last):
        while first < last and self.items[first] != ';':
            if first in self.pairs:
                first = self.pairs[first]
            first += 1
        return first

    def block(self, first, last, state, *, scoped=True, active=True):
        if self.callback_paths_key in state[0]:
            outcomes = self.run_paths(state[0][self.callback_paths_key],
                                     lambda path: self.block(first, last, path, scoped=scoped, active=active))
            return self.merge([path for path, live in outcomes if live], state)
        state = self.copy(state)
        if scoped:
            state.append({self.scope_key: 'block:' + str(first)})
        live = True
        while first < last:
            if self.callback_paths_key in state[0]:
                outcomes = self.run_paths(state[0][self.callback_paths_key],
                    lambda path: self.block(first, last, path, scoped=False, active=active and live))
                state, following = self.merge([path for path, reached in outcomes if reached], state)
                live = live and following
                break
            first, state, following = self.statement(first, last, state, active=active and live)
            live = live and following
        if scoped:
            self.close_scope(state)
            if self.callback_paths_key in state[0]:
                state[0][self.callback_paths_key] = [path[:-1] for path in state[0][self.callback_paths_key]]
        return (state[:-1] if scoped else state), live

    def statement(self, first, last, state, *, active=True, label=None):
        items, pairs, token = self.items, self.pairs, self.items[first]
        if token == ';':
            return first + 1, state, True
        if token.isidentifier() and first + 1 < last and items[first + 1] == ':':
            if first + 2 >= last:
                return last, state, False
            return self.statement(first + 2, last, state, active=active, label=token)
        if token == '{' and first in pairs:
            state, live = self.block(first + 1, pairs[first], state, active=active)
            return pairs[first] + 1, state, live
        if token not in {'if', 'for', 'switch', 'select'}:
            end = self.end(first, last)
            failed = token != 'defer' and (self.failed(first, end, state) or self.stopped(first, end, state))
            self.simple(first, end, state)
            failed = failed or token not in {'defer', 'go'} and any(
                opening in self.terminating_sites for _, opening in _primary_calls(items, pairs, first, end))
            if token == 'return' and active and not failed:
                segments = _segments(items, first + 1, end, pairs)
                for slot, (name, declared) in enumerate(self.result_fields):
                    if first + 1 == end:
                        role = self.lookup(state, name) if name else None
                    else:
                        a, b = segments[slot] if len(segments) == len(self.result_fields) else (first + 1, end)
                        identity = self.expression(a, b, state, identity=True, slot=slot if len(segments) == 1 else 0)
                        value = self.expression(a, b, state, slot=slot if len(segments) == 1 else 0)
                        role = self.role(self.capture_value(value, identity, declared, state, declared=True))
                    if role is not None:
                        if name:
                            for scope in reversed(state):
                                if name in scope:
                                    scope[name] = role
                                    break
                        state[0]['metadata:result:' + str(slot)] = role
                self.return_states.append(self.copy(state))
            if token in {'break', 'continue', 'fallthrough'} and active:
                target = items[first + 1] if first + 1 < end and token != 'fallthrough' else None
                for control in reversed(self.controls):
                    if token in control and (target is None or control['label'] == target):
                        control[token].append(self.copy(state))
                        break
            return min(end + 1, last), state, not failed and token not in {'return', 'goto', 'break', 'continue', 'fallthrough'}
        opening = self.body_opening(first + 1, last)
        if opening not in pairs:
            self.expression(first, last, state)
            return last, state, False
        closing = pairs[opening]
        template, control = self.copy(state), self.copy(state) + [{self.scope_key: 'control:' + str(opening)}]
        header = _segments(items, first + 1, opening, pairs, ';')
        if token == 'if':
            if len(header) > 1:
                self.simple(*header[0], control)
            self.mutations(*header[-1], control)
            condition = self.fold_sizes(self.expression(*header[-1], control))
            constant = _constant_value(condition, self.sources)
            truth = constant[1] if constant and constant[0] == 'bool' else None
            yes, yes_live = self.block(opening + 1, closing, control, active=active and truth is not False)
            following = closing + 1
            no, no_live = self.copy(control), True
            if following < last and items[following] == 'else':
                following, no, no_live = self.statement(following + 1, last, no, active=active and truth is not True)
            states = ([yes] if yes_live and truth is not False else []) + ([no] if no_live and truth is not True else [])
            merged, live = self.merge(states, template)
            return following, merged, live
        if token == 'for':
            counted = self.counted_loop(header, opening, closing, control, template, active, label)
            if counted is not None:
                return counted
            if len(header) == 3:
                self.simple(*header[0], control)
                self.mutations(*header[1], control)
                condition = self.expression(*header[1], control) or ['true']
            elif 'range' in items[first + 1:opening]:
                self.simple(first + 1, opening, control)
                operand = items.index('range', first + 1, opening) + 1
                elements = self.elements(self.read(self.expression(operand, opening, control)))
                condition = ['false'] if elements == [] else ['unknown:range']
            else:
                self.mutations(first + 1, opening, control)
                condition = self.expression(first + 1, opening, control) or ['true']
            constant = _constant_value(condition, self.sources)
            truth = constant[1] if constant and constant[0] == 'bool' else None
            context = {'label': label, 'break': [], 'continue': []}
            self.controls.append(context)
            back, back_live = self.block(opening + 1, closing, control, active=active and truth is not False)
            self.controls.pop()
            states = [control] if truth is not True else []
            if truth is not False:
                states += context['break']
                if truth is not True:
                    edges = ([back] if back_live else []) + context['continue']
                    for edge in edges:
                        if len(header) == 3:
                            self.simple(*header[2], edge)
                    states += edges
            merged, live = self.merge(states, template)
            return closing + 1, merged, live
        # Switch/select branches start with the same incoming state. A default
        # removes the unmatched edge; break/fallthrough retain their writes.
        if len(header) > 1:
            self.simple(*header[0], control)
        self.mutations(*header[-1], control)
        tag = self.expression(*header[-1], control)
        clauses = _clauses(items, opening, pairs)
        context = {'label': label, 'break': [], 'fallthrough': []}
        self.controls.append(context)
        normals = []
        remaining = True
        selected = {}
        for number, (kind, start, colon, _) in enumerate(clauses):
            if kind == 'default':
                continue
            possible = False
            for a, b in _segments(items, start, colon, pairs):
                value = self.expression(a, b, control)
                if tag:
                    value = ['('] + tag + [')', '==', '('] + value + [')']
                constant = _constant_value(value, self.sources)
                truth = constant[1] if constant and constant[0] == 'bool' else None
                possible = possible or remaining and truth is not False
                if truth is True:
                    remaining = False
            selected[number] = possible
        if not any(kind == 'default' for kind, *_ in clauses) and remaining:
            normals.append(control)
        for number, (kind, _, colon, end) in enumerate(clauses):
            possible = remaining if kind == 'default' else selected[number]
            entrances = ([control] if possible else []) + context['fallthrough']
            context['fallthrough'] = []
            entrance, _ = self.merge(entrances, control)
            branch, live = self.block(colon + 1, end, entrance, active=active and bool(entrances))
            if entrances and live:
                normals.append(branch)
        self.controls.pop()
        merged, live = self.merge(normals + context['break'], template)
        return closing + 1, merged, live


def _clauses(items, opening, pairs):
    clauses, cursor, closing = [], opening + 1, pairs[opening]
    while cursor < closing:
        if items[cursor] not in {'case', 'default'}:
            cursor += 1
            continue
        kind, first = items[cursor], cursor + 1
        colon = first
        while colon < closing and items[colon] != ':':
            if colon in pairs:
                colon = pairs[colon]
            colon += 1
        following = colon + 1
        while following < closing and items[following] not in {'case', 'default'}:
            if following in pairs:
                following = pairs[following]
            following += 1
        clauses.append((kind, first, colon, following))
        cursor = following
    return clauses


def _condition_bindings(items, pairs, *, return_sources=False):
    definitions = _definitions(items, pairs)
    effects, returns = {}, {}
    nonreturning = _panic_helpers(items, pairs)
    for _ in range(len(definitions) + 1):
        resolver = _Bindings(items, pairs, effects=effects, returns=returns, nonreturning=nonreturning)
        inferred, inferred_returns = {}, {}
        inferred_nonreturning = set(nonreturning)
        for name, (declaration, opening, following, body, end) in definitions.items():
            resolver.callback_path_splits = 0
            resolver.occurrences, resolver.allocations, resolver.return_states = {}, {}, []
            resolver.deferred_bindings = {}
            resolver.initial_references = {}
            resolver.parameter_types = {'parameter:' + str(slot): _underlying_type(kind, resolver.globals) for slot, (_, kind) in enumerate(
                _parameters(items, opening + 1, following - 1, pairs))}
            state = {items[position]: 'parameter:' + str(slot)
                     for slot, (position, _) in enumerate(_parameters(items, opening + 1, following - 1, pairs))}
            state[resolver.scope_key] = 'function:' + name
            resolver.result_fields = _result_fields(items[following:body])
            for result_name, kind in resolver.result_fields:
                if result_name:
                    state[result_name] = resolver.role(['zero:'] + kind)
            if items[declaration + 1] == '(':
                for position, kind in _parameters(items, declaration + 2, pairs[declaration + 1], pairs):
                    state[items[position]] = 'receiver'
                    resolver.parameter_types['receiver'] = kind
            testing = next((role for role, kind in resolver.parameter_types.items()
                            if ''.join(kind).lstrip('*') in {'testing.T', 'testing.B', 'testing.TB'}), None)
            if testing is not None:
                state[resolver.testing_owner_key] = resolver.testing_owner([testing], [state])
            final, live = resolver.block(body + 1, end, [state], scoped=False)
            exits = [resolver.finish(state) for state in (([final] if live else []) + resolver.return_states)]
            if not exits and not _recovers(items, pairs, body + 1, end):
                inferred_nonreturning.add(name)
            for position, choices in resolver.deferred_bindings.items():
                resolver.replacements[position] = (next(iter(choices)) if len(choices) == 1 else
                    resolver.role(['deferred-choice:'] + sorted(choices)))
            final, live = resolver.merge(exits, [state])
            writes = []
            if live:
                for key, value in final[0].items():
                    if not isinstance(key, tuple) or key[0] != 'store':
                        continue
                    _, root, path = key
                    if root in resolver.external_backings:
                        root, prefix = resolver.external_backings[root]
                        path = '/'.join(prefix + tuple(path.split('/')))
                    kind = _underlying_type(resolver.parameter_types.get(root, []), resolver.globals)
                    shared = False
                    for part in path.split('/'):
                        if kind[:1] in (['*'], ['map']) or kind[:2] == ['[', ']']:
                            shared = True
                            break
                        if part.startswith('index:') and kind[:1] == ['[']:
                            kind = kind[_pairs(kind).get(0, -1) + 1:]
                        else:
                            kind = resolver.structs.get(''.join(kind), {}).get(part, [])
                    if (root == 'receiver' or root.startswith('parameter:')) and shared:
                        stored_kind = _value_type([value], resolver.parameter_types, resolver.structs,
                                                  resolver.globals, resolver.sources)
                        effect = (resolver.reference_effect(value, final) if value in resolver.descriptors else
                                  resolver.value_effect([value], final) if ''.join(stored_kind) in resolver.structs or
                                  stored_kind[:1] == ['['] else _expand_value([value], resolver.sources))
                        if len(effect) > 128:
                            effect = ['unknown-mutation:' + hashlib.sha256(repr(effect).encode()).hexdigest()[:16]]
                        writes.append((root, path, effect))
            if writes:
                inferred[name] = sorted(writes, key=repr)
            # Only summarize bodies whose calls have understood effects. A
            # constructor returned after an opaque API call must retain that
            # call's provenance, rather than becoming a known empty object.
            understood = {'copy', 'clear', 'append', 'delete', 'len', 'cap', 'make', 'new'} | set(returns) | set(effects)
            def supported_call(a, b):
                literal = _literal_invocation(items, pairs, a, pairs[b] + 1)
                if literal is not None and literal[3] == b:
                    return all(supported_call(c, d) for c, d in
                               _primary_calls(items, pairs, literal[1] + 1, literal[2]))
                target = _reference([resolver.replacements.get(n, items[n]) for n in range(a, b)],
                                    resolver.sources, resolver.origins)
                if ''.join(target) in understood or resolver.conversion(items[a:pairs[b] + 1]) is not None:
                    return True
                if len(target) >= 3 and target[-2] == '.':
                    kind = _value_type(target[:-2], resolver.parameter_types, resolver.structs,
                                       resolver.globals, resolver.sources)
                    return ''.join(kind).lstrip('*') + '.' + target[-1] in understood
                return False
            supported = all(supported_call(a, b) for a, b in _primary_calls(items, pairs, body + 1, end))
            values = []
            if supported and resolver.result_fields and exits:
                for slot, (result_name, declared) in enumerate(resolver.result_fields):
                    roles = {exit[0].get(result_name if result_name else 'metadata:result:' + str(slot)) for exit in exits}
                    if None in roles:
                        break
                    kind = _underlying_type(declared, resolver.globals)
                    reference = kind[:1] in (['*'], ['map']) or kind[:2] == ['[', ']']
                    if reference:
                        joined = (next(iter(roles)) if len(roles) == 1 else resolver.join_references(roles))
                        if joined is None or joined not in resolver.descriptors:
                            break
                        variants = [resolver.reference_effect(exit[0].get(result_name if result_name else
                                    'metadata:result:' + str(slot)), exit) for exit in exits]
                        effect = {'references': [reference for variant in variants for reference in variant['references']]}
                        effect['nominal'] = resolver.nominal_type(declared)
                    else:
                        variants = [resolver.value_effect(resolver.raw_snapshot(role, exit), exit)
                                    for role, exit in ((exit[0].get(result_name if result_name else
                                        'metadata:result:' + str(slot)), exit) for exit in exits)]
                        if any(value != variants[0] for value in variants):
                            break
                        effect = variants[0]
                    values.append((kind, effect))
                if len(values) == len(resolver.result_fields):
                    inferred_returns[name] = values
        if inferred == effects and inferred_returns == returns and inferred_nonreturning == nonreturning:
            break
        effects, returns = inferred, inferred_returns
        nonreturning = inferred_nonreturning
    return (resolver.replacements, resolver.sources, resolver.origins, resolver) if return_sources else resolver.replacements


def _simplify(paths):
    paths = set(paths)
    changed = True
    while changed:
        changed = False
        for left in list(paths):
            for right in list(paths):
                if left != right and left.issubset(right):
                    paths.discard(right)
                    changed = True
                elif len(left - right) == len(right - left) == 1:
                    a, b = next(iter(left - right)), next(iter(right - left))
                    if a[0] == b[0] and a[1] != b[1]:
                        paths.discard(left)
                        paths.discard(right)
                        paths.add(left & right)
                        changed = True
    return sorted(paths, key=lambda p: (len(p), sorted(p)))


def _unparenthesized(items):
    while len(items) >= 2 and items[0] == '(' and _pairs(items).get(0) == len(items) - 1:
        items = items[1:-1]
    return items


def _condition(items, positive=True):
    items = _unparenthesized(items)
    constant = _constant_operand(items)
    if constant and constant[0] == 'bool':
        return ('true' if constant[1] else 'false', positive)
    if items and items[0] == '!':
        return _condition(items[1:], not positive)
    pairs, index = _pairs(items), 0
    while index < len(items):
        if items[index] in {'(', '['} and index in pairs:
            index = pairs[index] + 1
            continue
        operator = items[index]
        if operator in {'==', '!=', '<', '>', '<=', '>='}:
            left = ' '.join(_unparenthesized(items[:index]))
            right = ' '.join(_unparenthesized(items[index + 1:]))
            if operator in {'==', '!='}:
                left, right = sorted((left, right))
                return ('== ' + left + ' | ' + right, positive == (operator == '=='))
            if operator in {'>', '<='}:
                left, right = right, left
            return ('< ' + left + ' | ' + right, positive == (operator in {'<', '>'}))
        index += 1
    return (' '.join(items), positive)


def _branch(paths, condition, positive):
    term = _condition(condition, positive)
    if term[0] in {'true', 'false'}:
        return paths if (term[0] == 'true') == term[1] else []
    return _simplify([path | {term} for path in paths if (term[0], not term[1]) not in path])


class _Flow:
    def __init__(self, items, functions, pairs):
        self.items, self.functions, self.pairs = items, functions, pairs
        self.helpers = set()
        self.events = []
        self.active_literals = set()
        self.bindings, self.sources, self.origins, self.reader = _condition_bindings(items, pairs, return_sources=True)
        self.controls = []
        self.deferred_literals = {}
        for first, token in enumerate(items):
            if token != 'defer':
                continue
            end = first + 1
            bound = min((closing for opening, closing in pairs.items()
                         if items[opening] == '{' and opening < first < closing), default=len(items))
            while end < bound and items[end] != ';':
                end = pairs.get(end, end) + 1
            literal = _deferred_literal(items, pairs, first, end)
            if literal is not None:
                self.deferred_literals[first] = literal

        self.definitions = _definitions(items, pairs)
        self.parameter_types = []
        self.struct_fields, self.global_types = _type_tables(items, pairs, functions)
        self.testing_fields = {}
        for kind, fields in self.struct_fields.items():
            selected = [name for name, value in fields.items()
                        if ''.join(value).lstrip('*') in {'testing.T', 'testing.B', 'testing.TB'}]
            self.testing_fields[kind] = {name: 'testing-field:' + str(slot) for slot, name in enumerate(selected)}
        self.methods = {}
        for name, (declaration, opening, following, body, end) in self.definitions.items():
            types = {'parameter:' + str(slot): kind for slot, (_, kind) in enumerate(
                _parameters(items, opening + 1, following - 1, pairs))}
            if items[declaration + 1] == '(':
                receiver = _parameters(items, declaration + 2, pairs[declaration + 1], pairs)
                if receiver:
                    types['receiver'] = receiver[0][1]
                    receiver_type = ''.join(receiver[0][1]).lstrip('*')
                    self.methods.setdefault(receiver_type, []).append(name)
            self.parameter_types.append((body + 1, end, types))

        self.failure_contexts = {}
        # Propagate concrete callback kinds through typed/interface parameters
        # before collecting effects. This is type information, not evidence
        # that a guarded or recursive wrapper actually dispatches a callback.
        self.inferred_types = {}
        for _ in range(len(functions) + 1):
            changed = False
            for _, span in functions.items():
                for name, opening in self.call_sites(*span):
                    if name not in functions or opening not in pairs:
                        continue
                    for slot, argument in enumerate(_segments(items, opening + 1, pairs[opening], pairs)):
                        key = name, 'parameter:' + str(slot)
                        known = self.inferred_types.setdefault(key, set())
                        inferred = self.value_types(*argument)
                        changed = changed or not inferred.issubset(known)
                        known.update(inferred)
            if not changed:
                break

    def value_types(self, start, end):
        return self.value_kinds(self.condition(start, end), start)

    def value_kinds(self, tokens, start):
        """Track callback objects passed through simple locals and aliases."""
        known = set(self.methods) | set(self.struct_fields)
        pending = list(tokens)
        types = next((types for first, last, types in self.parameter_types if first <= start < last), {})
        function = next((name for name, (first, last) in self.functions.items() if first <= start < last), None)
        seen, result = set(), set()
        while pending:
            token = pending.pop()
            if token in seen:
                continue
            seen.add(token)
            if token in known:
                result.add(token)
            pending.extend(self.sources.get(token, ()))
            pending.extend(types.get(token, ()))
            pending.extend(self.inferred_types.get((function, token), ()))
            pending.extend(self.global_types.get(token, ()))
        return result

    def reference(self, tokens):
        return _reference(tokens, self.sources, self.origins)

    def call_sites(self, start, end, *, skip_literals=True, descend_literals=True):
        def collect(first, last, visiting=frozenset()):
            calls = set(_primary_calls(self.items, self.pairs, first, last, skip_literals=skip_literals))
            if not skip_literals:
                return calls
            if descend_literals:
                for _, opening in list(calls):
                    variants = self.reader.closure_variants.get(opening, [
                        (self.reader.closure_calls.get(opening), None)])
                    for stored, _ in variants:
                        if stored is not None and stored[1] not in visiting:
                            calls.update(collect(stored[1] + 1, stored[2], visiting | {stored[1]}))
            cursor = first
            while cursor < last:
                if cursor in self.deferred_literals:
                    _, body, closing, call = self.deferred_literals[cursor]
                    calls.update(collect(body + 1, closing, visiting))
                    cursor = self.pairs[call] + 1
                elif self.items[cursor] == 'func':
                    # A defer inside an uncalled closure is not registered.
                    body = _function_literal_body(self.items, self.pairs, cursor, last)
                    callee = _literal_start(self.items, self.pairs, cursor, first, body)
                    literal = _literal_invocation(self.items, self.pairs, callee, last)
                    if literal is not None and descend_literals:
                        calls.update(collect(body + 1, self.pairs[body], visiting))
                    cursor = self.pairs[body] + 1 if body is not None else cursor + 1
                else:
                    cursor += 1
            return calls

        calls = collect(start, end)
        for first, opening in sorted(calls):
            target = self.reference(self.condition(first, opening))
            types = next((types for a, b, types in self.parameter_types if a <= first < b), {})
            if len(target) >= 3 and target[-2] == '.' and target[-1] in {
                    'Error', 'Errorf', 'Fatal', 'Fatalf', 'Fail', 'FailNow', 'Skip', 'Skipf', 'SkipNow'}:
                kind = _value_type(target[:-2], types, self.struct_fields, self.global_types, self.sources)
                if ''.join(kind).lstrip('*') in {'testing.T', 'testing.B', 'testing.TB'}:
                    self.failure_contexts[opening] = self.context_tokens(target[:-2], first)
                    yield 'testing.' + target[-1], opening
                    continue
            if target and (len(target) == 1 and (target[0].isidentifier() or '.' in target[0]) or
                           len(target) % 2 == 1 and all(target[n] == '.' for n in range(1, len(target), 2))
                           and all(target[n].isidentifier() for n in range(0, len(target), 2))):
                yield ''.join(target), opening
            else:
                # A bound parameter, closure, branch join or constructor is not
                # the original API merely because its variable has that name.
                yield 'bound:' + hashlib.sha256(repr(target).encode()).hexdigest()[:16], opening

    def check_sites(self, start, end, *, descend_literals=True):
        for name, opening in self.call_sites(start, end, descend_literals=descend_literals):
            yield name, opening, None
            if opening not in self.pairs or name in self.functions or name.startswith(('assert.', 'require.', 'testing.')):
                continue
            if name.startswith('bound:') or name in {'len', 'cap', 'append', 'copy', 'new', 'make', 'delete', 'clear',
                        'string', 'print', 'println', 'panic', 'recover'}:
                continue
            # Only an opaque consumer can dispatch a callback. Local helper
            # bodies are analyzed separately, rather than assumed to invoke it.
            arguments = _segments(self.items, opening + 1, self.pairs[opening], self.pairs)
            for slot, span in enumerate(arguments):
                for kind in sorted(self.value_types(*span)):
                    for method in self.methods.get(kind, ()):
                        yield method, opening, {'api': name, 'slot': slot, 'object': self.condition(*span),
                                                'contexts': self.testing_contexts(self.condition(*span), span[0], {kind})}

    def calls(self, start, end):
        return (name for name, _, _ in self.check_sites(start, end))

    def condition(self, start, end):
        choices = self.reader.deferred_expressions.get((start, end))
        if choices:
            value = list(next(iter(choices))) if len(choices) == 1 else ['deferred-choice:'] + [
                self.reader.role(list(choice)) for choice in sorted(choices)]
        else:
            value, position, expanded = [], start, False
            while position < end:
                span = max((span for span in self.reader.expression_overrides
                            if span[0] == position and span[1] <= end), key=lambda span: span[1], default=None)
                if span is not None:
                    value += self.reader.expression_overrides[span]
                    position, expanded = span[1], True
                else:
                    value.append(self.bindings.get(position, self.items[position]))
                    position += 1
            if expanded:
                value = [self.reader.role(self.reader.read(value))]
        indexed = (value[-1:] == [']'] and any(a > 0 and value[a] == '[' and b == len(value) - 1
                    and ':' not in value[a + 1:b] for a, b in _pairs(value).items()))
        if indexed or value[:1] == ['*']:
            projected = self.reader.read(value)
            # Unknown index expressions retain their normalized parameter and
            # loop bindings, instead of expanding a renamed helper spelling.
            if not (projected[-1:] == [']'] and any(a > 0 and projected[a] == '['
                    and b == len(projected) - 1 for a, b in _pairs(projected).items())):
                value = [self.reader.role(projected)]
        if (len(value) >= 3 and value[-2] == '.' or value[:2] == ['make', '(']
                or _expand_value(value, self.sources)[:1] == ['store:']):
            projected = self.reader.read(value)
            # A copied receiver field can resolve to the same opaque symbol
            # as the original operand. Its aggregate wrapper is not a new
            # evaluation or a different assertion value.
            value = (projected if len(projected) == 1 and projected[0].isidentifier()
                     else [self.reader.role(projected)])
        conversion = self.reader.conversion(value)
        if conversion is not None and self.reader.nominal_type(conversion[0]) is None:
            value = [self.reader.role(self.reader.underlying_value(value))]
        location = self.reader.location(value)
        if location is not None and location[0] in self.reader.slice_capacities:
            region, original = self.reader.region(location), self.reader.region((location[0], ()))
            if region[3] is not None and original[3] is not None and region[3] > original[3]:
                value = [self.reader.role(self.reader.read(value))]
        if len(value) == 1:
            projected = self.reader.read(value)
            # Deferred/saved operands can wrap the same symbol without a
            # selector at this site. Normalize them like direct field reads.
            if len(projected) == 1 and projected[0].isidentifier():
                value = projected
        types = next((types for first, last, types in self.parameter_types if first <= start < last), {})
        constant = _constant_value(value, self.sources)
        if constant is not None and constant[0] == 'bool':
            return ['true' if constant[1] else 'false']
        return self.reader.normalize_view(self.reader.fold_sizes(value), types)

    def body_opening(self, start, end):
        return _control_body(self.items, self.pairs, start, end)

    def switch(self, index, opening, end, paths, label=None):
        items, closing = self.items, self.pairs[opening]
        header_start = index + 1
        header_parts = _segments(items, header_start, opening, self.pairs, ';')
        header = self.condition(*header_parts[-1])
        clauses = _clauses(items, opening, self.pairs)
        matched, remaining = {}, paths
        for number, (kind, first, colon, _) in enumerate(clauses):
            if kind == 'default':
                continue
            selected = []
            for start, stop in _segments(items, first, colon, self.pairs):
                condition = self.condition(start, stop)
                if header:
                    condition = ['('] + header + [')', '==', '('] + condition + [')']
                selected += _branch(remaining, condition, True)
                remaining = _branch(remaining, condition, False)
            matched[number] = _simplify(selected)
        context = {'kind': 'switch', 'label': label, 'break': [], 'fallthrough': []}
        self.controls.append(context)
        normal = [] if any(row[0] == 'default' for row in clauses) else remaining
        for number, (kind, _, colon, following) in enumerate(clauses):
            selected = remaining if kind == 'default' else matched[number]
            selected = _simplify(selected + context['fallthrough'])
            context['fallthrough'] = []
            normal += self.block(colon + 1, following, selected)
        self.controls.pop()
        return closing + 1, _simplify(normal + context['break'])

    def loop(self, index, opening, end, paths, label=None):
        items, closing = self.items, self.pairs[opening]
        frames = self.reader.loop_frames.get(opening)
        if frames is not None:
            context = {'kind': 'loop', 'label': label, 'break': [], 'continue': []}
            self.controls.append(context)
            bindings, overrides = self.bindings, self.reader.expression_overrides
            current = paths
            for replacements, expressions in frames:
                if not current:
                    break
                self.bindings = {**bindings, **replacements}
                self.reader.expression_overrides = {**overrides, **expressions}
                current = self.block(opening + 1, closing, current) + context['continue']
                context['continue'] = []
            self.bindings, self.reader.expression_overrides = bindings, overrides
            self.controls.pop()
            return closing + 1, _simplify(current + context['break'])
        header = self.condition(index + 1, opening)
        pieces = _segments(items, index + 1, opening, self.pairs, ';')
        if len(pieces) == 3:
            condition = self.condition(*pieces[1]) or ['true']
        elif 'range' in header:
            operand = header[header.index('range') + 1:]
            elements = self.reader.elements(self.reader.read(operand))
            condition = ['true' if elements else 'false'] if elements is not None else ['loop:range'] + operand
        else:
            condition = header or ['true']
        entered = _branch(paths, condition, True)
        unentered = _branch(paths, condition, False)
        context = {'kind': 'loop', 'label': label, 'break': [], 'continue': []}
        self.controls.append(context)
        backedge = self.block(opening + 1, closing, entered)
        self.controls.pop()
        normal = unentered + context['break']
        # A normal/continue back edge can eventually reach the condition's
        # false exit. A return inside the loop is deliberately absent here.
        if _condition(condition)[0] != 'true' or 'range' in header:
            term = _condition(condition)
            normal += [path - {term} for path in backedge + context['continue']]
        return closing + 1, _simplify(normal)

    def kind(self, name):
        if name in {'testing.Error', 'testing.Errorf', 'testing.Fatal', 'testing.Fatalf', 'testing.Fail', 'testing.FailNow'}:
            return 'failure'
        if name.startswith(('assert.', 'require.')):
            kind = name.rsplit('.', 1)[-1]
            return kind[:-1] if kind.endswith('f') and kind[:-1] in _TESTIFY_FUNCTIONS else kind
        if name in self.helpers:
            return 'checking-helper'
        return None

    def context_expression(self, first, last):
        return self.context_tokens(self.condition(first, last), first)

    def context_tokens(self, tokens, position):
        # A local alias of a testing field has the same receiver. Do not expand
        # arbitrary calls/constructors into an apparent testing context.
        def expand(value, seen=frozenset()):
            if len(value) == 1 and value[0] in self.sources and value[0] not in seen:
                source = self.sources[value[0]]
                if len(source) == 1 and source[0].startswith(('parameter:', 'receiver', 'local:projection:')):
                    return expand(source, seen | {value[0]})
                if len(source) == 3 and source[1] == '.':
                    return expand(source, seen | {value[0]})
            return value
        tokens = list(expand(tokens))
        if len(tokens) >= 3 and tokens[1] == '.' and all(tokens[n] == '.' for n in range(1, len(tokens), 2)):
            tokens = _project_value(tokens[:1], tokens[2::2], self.sources, self.struct_fields)
        classes = self.value_kinds(tokens, position)
        for index in range(1, len(tokens)):
            if tokens[index - 1] != '.':
                continue
            ranks = {self.testing_fields[kind][tokens[index]] for kind in classes
                     if tokens[index] in self.testing_fields.get(kind, {})}
            if len(ranks) == 1:
                tokens[index] = next(iter(ranks))
        return tokens

    def testing_contexts(self, tokens, position, kinds=None):
        kinds = self.value_types(position, position + 1) if kinds is None else kinds
        result = {}
        base = _expand_value(tokens, self.sources)
        for kind in sorted(kinds):
            for field, slot in self.testing_fields.get(kind, {}).items():
                if len(base) == 1 and base[0].startswith('parameter:'):
                    result[slot] = ['context-' + base[0] + ':' + slot]
                else:
                    result[slot] = _project_value(tokens, [field], self.sources, self.struct_fields)
        return result

    def symmetric_arguments(self, arguments):
        if len(arguments) != 2:
            return False
        values = [self.items[first:last] for first, last in arguments]
        pure = [_operand_key(value) is not None for value in values]
        if all(pure):
            return True
        # Reading an unescaped scalar parameter commutes with a named call:
        # that call cannot write to the caller's local parameter. Calls on both
        # sides, address escapes and capturing closures retain evaluation order.
        for number, (first, last) in enumerate(arguments):
            normalized = self.condition(first, last)
            if not pure[number] or len(normalized) != 1:
                continue
            role = normalized[0]
            for start, stop, types in self.parameter_types:
                if not start <= first < stop or types.get(role) not in (
                        ['string'], ['bool'], ['int'], ['int8'], ['int16'], ['int32'], ['int64'],
                        ['uint'], ['uint8'], ['uint16'], ['uint32'], ['uint64'], ['byte'], ['rune']):
                    continue
                if any(self.items[position] == 'func' or self.items[position] == '&' and
                       self.bindings.get(position + 1) == role for position in range(start, stop)):
                    continue
                other_first, other_last = arguments[1 - number]
                if not any(self.bindings.get(position) == role for position in range(other_first, other_last)):
                    return True
        return False

    def record(self, start, end, paths):
        if self.items[start:start + 1] == ['go'] and start not in self.reader.joined_go:
            outer = next(((a, b) for a, b in _primary_calls(self.items, self.pairs, start + 1, end)
                          if self.pairs[b] == end - 1), None)
            if outer is not None:
                self.record(outer[1] + 1, self.pairs[outer[1]], paths)
            return
        def execution_paths(position):
            guarded = paths
            for a, b, positive in _call_guards(self.items, self.pairs, start, end, position):
                guarded = _branch(guarded, self.condition(a, b), positive)
            return guarded

        def record_literal(literal, frame, selected):
            if literal is None or literal[1] in self.active_literals:
                return
            _, body, closing, _ = literal
            controls, self.controls = self.controls, []
            bindings, overrides = self.bindings, self.reader.expression_overrides
            if frame is not None:
                self.bindings = {**bindings, **frame[0]}
                self.reader.expression_overrides = {**overrides, **frame[1]}
            self.active_literals.add(body)
            try:
                self.block(body + 1, closing, selected)
            finally:
                self.active_literals.remove(body)
                self.bindings, self.reader.expression_overrides = bindings, overrides
                self.controls = controls

        for callee, opening in _primary_calls(self.items, self.pairs, start, end):
            variants = self.reader.closure_variants.get(opening)
            if variants is not None:
                events = self.events
                branches = []
                for literal, frame in variants:
                    self.events = []
                    record_literal(literal, frame, execution_paths(callee))
                    if self.events not in branches:
                        branches.append(self.events)
                self.events = events
                if len(branches) == 1:
                    self.events.extend(branches[0])
                elif branches:
                    self.events.append({'kind': 'alternatives', 'branches': branches})
                continue
            literal = _literal_invocation(self.items, self.pairs, callee, self.pairs[opening] + 1)
            literal = self.reader.closure_calls.get(opening, literal)
            if literal is not None and literal[3] == opening:
                record_literal(literal, self.reader.call_frames.get(opening), execution_paths(callee))
        for name, opening, callback in self.check_sites(start, end, descend_literals=False):
            current_paths = execution_paths(opening)
            kind = self.kind(name)
            if not kind or not current_paths:
                continue
            event = {'kind': kind, 'paths': [sorted(path) for path in _simplify(current_paths)]}
            if kind == 'checking-helper':
                event['callee'] = name
            if opening in self.pairs:
                arguments = _segments(self.items, opening + 1, self.pairs[opening], self.pairs)
            else:
                arguments = []
            if kind == 'checking-helper' and not callback:
                event['argument_contexts'] = [self.testing_contexts(self.condition(first, last), first,
                                                                  self.value_types(first, last))
                                              for first, last in arguments]
            if callback:
                event.update(dispatch='callback', api=callback['api'], slot=callback['slot'], object=callback['object'],
                             contexts=callback['contexts'])
                event['arguments'] = [self.condition(first, last) if slot != callback['slot'] else ['@callback']
                                      for slot, (first, last) in enumerate(arguments)]
            elif kind == 'failure':
                event['context'] = self.failure_contexts.get(opening)
            else:
                if kind != 'checking-helper':
                    if arguments:
                        event['context'] = self.context_expression(*arguments[0])
                    arguments = arguments[1:1 + _assertion_arity(kind)]
                values = [self.condition(first, last) for first, last in arguments if first < last]
                if kind == 'checking-helper' and name in self.definitions:
                    definition = self.definitions[name]
                    parameters = _parameters(self.items, definition[1] + 1, definition[2] - 1, self.pairs)
                    for slot, (_, declared) in enumerate(parameters[:len(values)]):
                        underlying = _underlying_type(declared, self.global_types)
                        value = _expand_value(values[slot], self.sources)
                        if underlying[:2] == ['[', ']'] and self.reader.conversion(value) is not None:
                            # Parameter assignment converts a defined slice to
                            # the helper's declared (possibly unnamed) type.
                            value = self.reader.underlying_value(value)
                            nominal = self.reader.nominal_type(declared)
                            values[slot] = [self.reader.role(nominal + ['('] + value + [')'] if nominal else value)]
                if kind == 'Exactly' and self.symmetric_arguments(arguments):
                    values = sorted(values)
                if kind in {'Exactly', 'Equal', 'EqualValues', 'EqualExportedValues', 'NotEqual', 'NotEqualValues'}:
                    # Value comparisons do not observe a slice's capacity.
                    # Keep headers in helper arguments, where cap/reslicing or
                    # append may affect whether and what the helper checks.
                    compared = []
                    for value in values:
                        declared = _value_type_raw(_expand_value(value, self.sources), self.reader.parameter_types,
                                                   self.struct_fields, self.global_types, self.sources)
                        if _underlying_type(declared, self.global_types)[:2] == ['[', ']']:
                            content = self.reader.underlying_value(self.reader.read(value))
                            elements = self.reader.elements(content)
                            if (elements is not None and all(_operand_key(element) is not None for element in elements)
                                    or content == ['nil']):
                                nominal = self.reader.nominal_type(declared)
                                if content == ['nil']:
                                    content = ['[', ']'] + _underlying_type(declared, self.global_types)[2:] + ['(', 'nil', ')']
                                content = nominal + ['('] + content + [')'] if nominal else content
                                value = [self.reader.role(content, pure=True)]
                        compared.append(value)
                    values = sorted(compared) if kind == 'Exactly' and self.symmetric_arguments(arguments) else compared
                event['arguments'] = values
            self.events.append(event)

    def block(self, start, end, paths):
        index = start
        while index < end and paths:
            index, paths = self.statement(index, end, paths)
        return paths

    def statement(self, index, end, paths, label=None):
        items, token = self.items, self.items[index]
        if token == ';':
            return index + 1, paths
        if token.isidentifier() and index + 1 < end and items[index + 1] == ':':
            if index + 2 >= end:
                return end, []
            return self.statement(index + 2, end, paths, label=token)
        if index in self.deferred_literals:
            _, body, closing, call = self.deferred_literals[index]
            self.record(call + 1, self.pairs[call], paths)
            # The binder has evaluated captures at function exit. Analyze the
            # closure's own branches, without letting its return exit the caller.
            controls, self.controls = self.controls, []
            self.block(body + 1, closing, paths)
            self.controls = controls
            return min(self.pairs[call] + 2, end), paths
        if token in {'if', 'for', 'switch', 'select'}:
            cursor = self.body_opening(index + 1, end)
            if cursor not in self.pairs:
                return end, paths
            self.record(index + 1, cursor, paths)
            if token in {'switch', 'select'}:
                return self.switch(index, cursor, end, paths, label=label)
            if token == 'for':
                return self.loop(index, cursor, end, paths, label=label)
            header = self.condition(index + 1, cursor)
            if token == 'if' and ';' in header:
                header = header[1 + max(n for n, t in enumerate(header) if t == ';'):]
            start = len(self.events)
            yes = self.block(cursor + 1, self.pairs[cursor], _branch(paths, header, True))
            middle = len(self.events)
            following = self.pairs[cursor] + 1
            no = _branch(paths, header, False)
            if following < end and items[following] == 'else':
                following, no = self.statement(following + 1, end, no)
                # These arms share one evaluation of the condition. Pair
                # equal checks while retaining their runtime multiplicity.
                # Separate if statements may re-evaluate a changed global;
                # their complementary spelling cannot establish coverage.
                right = list(self.events[middle:])
                combined = []
                for event in self.events[start:middle]:
                    identity = {key: value for key, value in event.items() if key != 'paths'}
                    match = next((n for n, other in enumerate(right)
                                  if event['kind'] != 'alternatives' and identity == {
                                      key: value for key, value in other.items() if key != 'paths'} and
                                  _simplify([frozenset(map(tuple, path)) for path in
                                             event['paths'] + other['paths']]) == _simplify(paths)), None)
                    if match is not None:
                        other = right.pop(match)
                        selected = [frozenset(map(tuple, path)) for path in event['paths'] + other['paths']]
                        event = {**event, 'paths': [sorted(path) for path in _simplify(selected)]}
                    combined.append(event)
                self.events[start:] = combined + right
            return following, _simplify(yes + no)
        if token == '{' and index in self.pairs:
            return self.pairs[index] + 1, self.block(index + 1, self.pairs[index], paths)
        cursor = index
        while cursor < end and items[cursor] != ';':
            if items[cursor] in {'(', '[', '{'} and cursor in self.pairs:
                cursor = self.pairs[cursor]
            cursor += 1
        self.record(index, cursor, paths)
        if token in {'break', 'continue', 'fallthrough'}:
            target = items[index + 1] if index + 1 < cursor and token != 'fallthrough' else None
            for context in reversed(self.controls):
                if token in context and (target is None or context['label'] == target):
                    context[token] += paths
                    break
            return min(cursor + 1, end), []
        terminating = token in {'return', 'break', 'continue', 'goto'}
        terminating = terminating or any(opening in self.reader.terminating_sites
                                        for _, opening in _primary_calls(items, self.pairs, index, cursor))
        # Once a path has already failed the test, returning on that path does
        # not weaken subsequent checks. This also tolerates Error+return and
        # Fatal+return rewrites of existing error branches.
        terminating = terminating or token != 'defer' and any(
            self.kind(name) == 'failure' for name, _, _ in self.check_sites(index, cursor, descend_literals=False))
        return min(cursor + 1, end), [] if terminating else paths


def assertion_flow(text):
    items = canonical_tokens(text)
    pairs = _pairs(items)
    functions = _functions(items, pairs)
    flow = _Flow(items, functions, pairs)
    calls = {name: set(flow.calls(*span)) for name, span in functions.items()}
    for _ in range(len(functions) + 1):
        added = {name for name, callees in calls.items() if any(flow.kind(callee) for callee in callees)}
        if added.issubset(flow.helpers):
            break
        flow.helpers.update(added)
    reachable = {name for name in functions if name.startswith(('Test', 'Example', 'Benchmark'))}
    for _ in range(len(functions) + 1):
        added = {callee for name in reachable for callee in calls.get(name, ()) if callee in functions}
        if added.issubset(reachable):
            break
        reachable.update(added)
    result = {}
    for name in sorted(reachable):
        flow.events = []
        flow.block(*functions[name], [frozenset()])
        if flow.events:
            result[name] = flow.events
    return result


def _constant_operand(items):
    """Evaluate a bounded subset of Go constant expressions, without execution."""
    if not items or len(items) > 128:
        return None
    merged, position = [], 0
    while position < len(items):
        if position + 1 < len(items) and items[position] + items[position + 1] in {'<<', '>>', '&^'}:
            merged.append(items[position] + items[position + 1])
            position += 2
        else:
            merged.append(items[position])
            position += 1
    precedence = {'||': 1, '&&': 2, '==': 3, '!=': 3, '<': 3, '<=': 3, '>': 3, '>=': 3,
                  '+': 4, '-': 4, '|': 4, '^': 4, '*': 5, '/': 5, '%': 5,
                  '<<': 5, '>>': 5, '&': 5, '&^': 5}
    cursor = 0

    def atom():
        nonlocal cursor
        if cursor >= len(merged):
            raise ValueError
        token = merged[cursor]
        cursor += 1
        if token == '(':
            value = expression(1)
            if cursor >= len(merged) or merged[cursor] != ')':
                raise ValueError
            cursor += 1
            return value
        if token in {'+', '-', '^', '!'}:
            kind, value = atom()
            if token == '!' and kind == 'bool':
                return kind, not value
            if kind not in {'integer', 'rune'} or token == '!':
                raise ValueError
            return kind, value if token == '+' else -value if token == '-' else ~value
        if token in {'true', 'false'}:
            return 'bool', token == 'true'
        if token == 'nil':
            return 'nil', None
        if token.startswith(('"', '`', "'")):
            value = _unquote(token)
            return ('rune', ord(value)) if token.startswith("'") else ('string', value)
        numeric = token.replace('_', '')
        base = 0 if numeric.lower().startswith(('0x', '0b', '0o')) else 8 if len(numeric) > 1 and numeric[0] == '0' else 10
        return 'integer', int(numeric, base)

    def operation(left, operator, right):
        a_kind, a = left
        b_kind, b = right
        numeric = a_kind in {'integer', 'rune'} and b_kind in {'integer', 'rune'}
        if operator in {'==', '!=', '<', '<=', '>', '>='} and (numeric or a_kind == b_kind):
            if operator == '==':
                return 'bool', a == b
            if operator == '!=':
                return 'bool', a != b
            if a_kind == 'nil':
                raise ValueError
            return 'bool', {'<': lambda: a < b, '<=': lambda: a <= b,
                            '>': lambda: a > b, '>=': lambda: a >= b}[operator]()
        if operator in {'&&', '||'} and a_kind == b_kind == 'bool':
            return 'bool', a and b if operator == '&&' else a or b
        if operator == '+' and a_kind == b_kind == 'string':
            return 'string', a + b
        if not numeric or operator not in {'+', '-', '*', '/', '%', '|', '^', '&', '&^', '<<', '>>'}:
            raise ValueError
        if operator in {'<<', '>>'} and not 0 <= b <= 4096:
            raise ValueError
        if operator in {'/', '%'}:
            if not b:
                raise ValueError
            quotient = (abs(a) // abs(b)) * (-1 if (a < 0) != (b < 0) else 1)
            value = quotient if operator == '/' else a - quotient * b
        else:
            value = {'+': lambda: a + b, '-': lambda: a - b, '*': lambda: a * b,
                     '|': lambda: a | b, '^': lambda: a ^ b, '&': lambda: a & b,
                     '&^': lambda: a & ~b, '<<': lambda: a << b, '>>': lambda: a >> b}[operator]()
        if abs(value).bit_length() > 4096:
            raise ValueError
        kind = a_kind if operator in {'<<', '>>'} else 'rune' if 'rune' in {a_kind, b_kind} else 'integer'
        return kind, value

    def expression(minimum):
        nonlocal cursor
        value = atom()
        while cursor < len(merged) and precedence.get(merged[cursor], 0) >= minimum:
            operator = merged[cursor]
            cursor += 1
            value = operation(value, operator, expression(precedence[operator] + 1))
        return value

    try:
        value = expression(1)
        return value if cursor == len(merged) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _operand_key(items):
    """Recognize identical side-effect-free operands, not repeated function calls."""
    items = _unparenthesized(items)
    if not items:
        return None
    constant = _constant_operand(items)
    if constant is not None:
        return constant
    if len(items) == 1:
        token = items[0]
        if token.startswith(('"', '`', "'")):
            try:
                return ('rune' if token.startswith("'") else 'string', _unquote(token))
            except (ValueError, OverflowError):
                return None
        if token[0].isdigit():
            return ('number', token.replace('_', ''))
    if any(token in {'func', '++', '--', '<', '>'} for token in items):
        return None
    for position, token in enumerate(items):
        if token == '(' and position and (items[position - 1].isidentifier()
                                           or items[position - 1] in {')', ']'}):
            return None  # next(), conversions and other calls need Go evaluation.
    return ('expression', tuple(items))


def go_assertion_counts(text):
    items = canonical_tokens(text)
    pairs = _pairs(items)
    flow = _Flow(items, _functions(items, pairs), pairs)
    count = strong = skips = 0
    strong_names = set(('Equal Exactly DeepEqual Equals Error Errorf ErrorIs ErrorAs Contains '
                        'NotContains Len Greater GreaterOrEqual Less LessOrEqual NotEqual Zero Regexp').split())
    for name, _ in flow.call_sites(0, len(items), skip_literals=False):
        if name.startswith('testing.'):
            if name.rsplit('.', 1)[-1].startswith('Skip'):
                skips += 1
            else:
                count += 1
                strong += 1
        elif name in {'t.Skip', 't.Skipf', 't.SkipNow'}:
            skips += 1  # Preserve the original guard for untyped/global t values.
        elif name.startswith(('assert.', 'require.')):
            count += 1
            kind = name.rsplit('.', 1)[-1]
            strong += kind in strong_names or kind.endswith('f') and kind[:-1] in strong_names
    return count, strong, skips


def go_tautology_count(text):
    """Inspect call operands throughout the file, including callback methods."""
    items = canonical_tokens(text)
    pairs = _pairs(items)
    flow = _Flow(items, _functions(items, pairs), pairs)
    count = 0
    for name, opening in flow.call_sites(0, len(items), skip_literals=False):
        if not name.startswith(('assert.', 'require.')) or opening not in pairs:
            continue
        kind = name.rsplit('.', 1)[-1]
        if kind.endswith('f'):
            kind = kind[:-1]
        arguments = [items[first:last] for first, last in _segments(items, opening + 1, pairs[opening], pairs)]
        if kind in {'Equal', 'EqualValues', 'EqualExportedValues', 'Exactly'} and len(arguments) >= 3:
            left, right = _operand_key(arguments[1]), _operand_key(arguments[2])
            count += left is not None and left == right
        elif kind in {'True', 'False', 'NoError'} and len(arguments) >= 2:
            expected = {'True': ['true'], 'False': ['false'], 'NoError': ['nil']}[kind]
            count += _constant_operand(arguments[1]) == _constant_operand(expected)
    return count


def _expand_callback_wrappers(graph):
    """Expose real API effects of forwarding helpers without dropping checks.

    Callback effects must be unconditional and forward a callback parameter.
    Independent checks keep their original helper edge. Recursive forwarding
    is not used as proof of reaching an API. Token substitution preserves data
    literals and expression precedence.
    """
    def substitute(tokens, arguments):
        result = []
        for token in tokens:
            if token.startswith('context-parameter:'):
                _, slot, field = token.split(':', 2)
                index = int(slot)
                contexts = arguments.get('contexts', [])
                if index >= len(contexts) or field not in contexts[index]:
                    return None
                result += ['('] + contexts[index][field] + [')']
            elif token.startswith('parameter:') and token[len('parameter:'):].isdigit():
                index = int(token[len('parameter:'):])
                values = arguments['values']
                if index >= len(values):
                    return None
                result += ['('] + values[index] + [')']
            else:
                result.append(token)
        return _expression_tokens(result)

    cache = {}

    def expand_events(events, visiting):
        result, recursive = [], False
        for event in events:
            if event['kind'] == 'alternatives':
                branches = []
                for branch in event['branches']:
                    expanded, cyclic = expand_events(branch, visiting)
                    branches.append(expanded)
                    recursive = recursive or cyclic
                result.append({**event, 'branches': branches})
                continue
            callee = event.get('callee')
            if event['kind'] != 'checking-helper' or event.get('dispatch') or callee not in graph:
                result.append(event)
                continue
            children, cyclic = expand(callee, visiting)
            recursive = recursive or cyclic
            forwarded = []
            if not cyclic:
                for child in children:
                    if child.get('dispatch') != 'callback' or child['paths'] != [[]]:
                        continue
                    origin = child.get('object', [])
                    if len(origin) != 1 or not origin[0].startswith('parameter:'):
                        continue
                    arguments = {'values': event.get('arguments', []), 'contexts': event.get('argument_contexts', [])}
                    values = [substitute(value, arguments) for value in child.get('arguments', [])]
                    source = substitute(origin, arguments)
                    contexts = {slot: substitute(value, arguments) for slot, value in child.get('contexts', {}).items()}
                    if source is not None and all(value is not None for value in values) and all(
                            value is not None for value in contexts.values()):
                        forwarded.append({**child, 'paths': event['paths'], 'arguments': values,
                                          'object': source, 'contexts': contexts})
            # A helper's independent baseline assertions remain mandatory.
            pure_forwarder = children and len(forwarded) == len(children)
            if not pure_forwarder:
                result.append(event)
            result.extend(forwarded)
        return result, recursive

    def expand(name, visiting):
        if name in visiting:
            return [], True
        if name not in cache:
            cache[name] = expand_events(graph.get(name, ()), visiting | {name})
        return cache[name]

    return {name: expand(name, frozenset())[0] for name in graph}


def preserves_flow(before, after):
    """Each baseline check must still be reached on its original paths."""
    if before == after:
        return True
    before, after = _expand_callback_wrappers(before), _expand_callback_wrappers(after)

    def matches(check, actual, compatible):
        if check['kind'] != actual['kind'] or check.get('dispatch') != actual.get('dispatch'):
            return False
        if any(check.get(key) != actual.get(key) for key in ('arguments', 'context', 'contexts', 'api', 'slot')):
            return False
        if check['kind'] == 'checking-helper':
            if (check.get('callee'), actual.get('callee')) not in compatible:
                return False
            if check.get('dispatch') == 'callback' and (
                    check['callee'].rsplit('.', 1)[-1] != actual['callee'].rsplit('.', 1)[-1]):
                return False
        paths = [set(map(tuple, path)) for path in actual['paths']]
        return all(any(path.issubset(set(map(tuple, wanted))) for path in paths)
                   for wanted in check['paths'])

    def preserves_checks(expected, available, compatible):
        # A selected callback executes one alternative. Every possible
        # execution must preserve the baseline checks; their union cannot
        # compensate for a missing or weakened check in one alternative.
        for index, event in enumerate(available):
            if event['kind'] == 'alternatives':
                return all(preserves_checks(expected, available[:index] + branch + available[index + 1:], compatible)
                           for branch in sorted(event['branches'], key=len))
        for index, event in enumerate(expected):
            if event['kind'] == 'alternatives':
                return all(preserves_checks(expected[:index] + branch + expected[index + 1:], available, compatible)
                           for branch in event['branches'])
        if len(expected) > len(available):
            return False
        choices = [[index for index, actual in enumerate(available)
                    if matches(check, actual, compatible)] for check in expected]
        matched = {}

        def assign(check, visited):
            for index in choices[check]:
                if index in visited:
                    continue
                visited.add(index)
                if index not in matched or assign(matched[index], visited):
                    matched[index] = check
                    return True
            return False

        # Match checks independently of source order. A guarded check must not
        # consume the only unguarded match needed by another baseline check.
        return all(assign(check, set()) for check in range(len(expected)))

    roots = {name for name in before if name.startswith(('Test', 'Example', 'Benchmark'))}
    # Test identities are fixed, while helper names may change. Compare helper
    # bodies through their actual call sites, including recursive helper graphs.
    compatible = {(name, other) for name in before for other in after
                  if name not in roots or name == other}
    while True:
        reduced = {(name, other) for name, other in compatible
                   if preserves_checks(before[name], after[other], compatible)}
        if reduced == compatible:
            break
        compatible = reduced
    return all((name, name) in compatible for name in roots)
