"""Detect dynamic loading of source-runtime code or plugins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import process_token_present, token_present
from .subprocess import _candidate_files, _line_of, source_runtime_tokens
try:
    from ..source_syntax import tokens as source_tokens, pairs
except ImportError:  # Standalone verifier layout.
    from source_syntax import tokens as source_tokens, pairs


_SINKS = {"importlib.import_module", "import_module", "__import__",
          "module.constructor._load", "process.mainModule", "URLClassLoader",
          "Class.forName", "defineClass", "ScriptEngineManager", "plugin.Open"}


def _imports(items, matching):
    aliases = {}
    for index, token in enumerate(items):
        if token.kind != "word" or token.value != "import":
            continue
        start = index + 1
        stop = matching.get(start, start + 2) if start < len(items) and items[start].value == "(" else min(len(items), start + 2)
        for j in range(start, stop):
            if items[j].kind == "string" and items[j].value == "plugin":
                prev = items[j - 1]
                name = prev.value if j > start and (prev.kind == "word" or prev.value == ".") else "plugin"
                if name != "_":
                    aliases["Open" if name == "." else name + ".Open"] = "plugin.Open"
    return aliases


def _constant(items, start, stop, values):
    """Resolve only a string/identifier concatenation, never nearby literals."""
    parts, want_value, depth = [], True, 0
    for item in items[start:stop]:
        if item.value == "(" and want_value:
            depth += 1
        elif item.value == ")" and depth:
            depth -= 1
        elif want_value and (item.kind == "string" or item.kind == "word" and item.value in values):
            parts.append(item.value if item.kind == "string" else values[item.value])
            want_value = False
        elif not want_value and item.value == "+":
            want_value = True
        else:
            break
    return "".join(parts) if parts and not want_value and depth == 0 else None


def _unparenthesized(items, start, stop, matching):
    while start < stop and items[start].value == '(' and matching.get(start) == stop - 1:
        start, stop = start + 1, stop - 1
    return start, stop


def _callable_name(items, start, stop, matching):
    """Resolve a name/selector expression with transparent outer parentheses."""
    start, stop = _unparenthesized(items, start, stop, matching)
    if start >= stop or (stop - start) % 2 != 1:
        return None
    if any(item.kind != 'word' if (index - start) % 2 == 0 else item.value != '.'
           for index, item in enumerate(items[start:stop], start)):
        return None
    return ''.join(item.value for item in items[start:stop])


def _boolean_literal(items, start, stop, matching, shadowed):
    """Evaluate bounded literal boolean/integer expressions, without eval.

    Unknown identifiers and unsupported expressions remain unknown. Integer
    spellings follow Go (in particular, a leading zero means octal).
    """
    def literal(begin, end):
        begin, end = _unparenthesized(items, begin, end, matching)
        sign = 1
        if begin < end and items[begin].value in {'+', '-'}:
            sign = -1 if items[begin].value == '-' else 1
            begin += 1
            begin, end = _unparenthesized(items, begin, end, matching)
        if end - begin != 1 or items[begin].kind != 'word':
            return None
        value = items[begin].value
        if sign == 1 and value in {'true', 'false'} and value not in shadowed:
            return 'bool', value == 'true'
        value = value.replace('_', '')
        if len(value) > 256 or not re.fullmatch(r'(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|0[0-7]*|[1-9][0-9]*)', value):
            return None
        base = 0 if value.lower().startswith(('0x', '0b', '0o')) else 8 if value.startswith('0') else 10
        return 'int', sign * int(value, base)

    def boolean(begin, end, depth=0):
        if depth > 48:
            return None
        begin, end = _unparenthesized(items, begin, end, matching)
        # Split only top-level operators, respecting Go's precedence.
        for operators in [{'||'}, {'&&'}, {'==', '!=', '<', '<=', '>', '>='}]:
            cursor = begin
            while cursor < end:
                if items[cursor].value in {'(', '[', '{'} and cursor in matching:
                    cursor = matching[cursor] + 1
                    continue
                operator = items[cursor].value
                if operator in operators:
                    if operator in {'&&', '||'}:
                        left, right = boolean(begin, cursor, depth + 1), boolean(cursor + 1, end, depth + 1)
                        return (left and right if operator == '&&' else left or right) if left is not None and right is not None else None
                    left, right = literal(begin, cursor), literal(cursor + 1, end)
                    if left is None or right is None or left[0] != right[0] or left[0] == 'bool' and operator not in {'==', '!='}:
                        return None
                    a, b = left[1], right[1]
                    return {'==': a == b, '!=': a != b, '<': a < b, '<=': a <= b, '>': a > b, '>=': a >= b}[operator]
                cursor += 1
        if begin < end and items[begin].value == '!':
            value = boolean(begin + 1, end, depth + 1)
            return not value if value is not None else None
        value = literal(begin, end)
        return value[1] if value is not None and value[0] == 'bool' else None

    return boolean(start, stop)


def _default_only_switch(items, body, matching):
    """A switch with only default always executes that arm, including types."""
    cursor, defaults = body + 1, 0
    while cursor < matching[body]:
        token = items[cursor]
        if token.kind == 'punct' and token.value in {'(', '[', '{'} and cursor in matching:
            cursor = matching[cursor] + 1
            continue
        if token.kind == 'word' and token.value == 'case':
            return False
        if token.kind == 'word' and token.value == 'default':
            defaults += 1
        cursor += 1
    return defaults == 1


def _go_jump_plan(items, matching, bindings, text):
    """Locate function-local labels without mistaking case or composite keys.

    A legal goto target is in the same or an enclosing lexical block. The
    detector follows definite forward transfers; uncertain incoming transfers
    keep their labels reachable past a later return/break.
    """
    functions, braces, owner, parent, labels, jumps = [], [], {}, {}, {}, {}
    for index, token in enumerate(items):
        while functions and matching.get(functions[-1], functions[-1]) < index:
            functions.pop()
        if index in bindings and index in matching:
            functions.append(index)
        owner[index] = functions[-1] if functions else None
        parent[index] = braces[-1] if braces else None
        if token.kind == 'punct' and token.value == '{':
            braces.append(index)
        elif token.kind == 'punct' and token.value == '}' and braces:
            braces.pop()
        if token.kind != 'word' or owner[index] is None or index + 1 >= len(items):
            continue
        if token.value == 'goto' and items[index + 1].kind == 'word':
            jumps[index] = (owner[index], items[index + 1].value)
        if items[index + 1].value == ':' and items[index + 1].kind == 'punct':
            previous = items[index - 1] if index else None
            boundary = previous is None or previous.value in {'{', '}', ';', ':'} or (
                '\n' in text[previous.end:token.start]
                and previous.value not in {'case', 'goto', 'default'}
                and (previous.kind in {'word', 'string'} or previous.value in {')', ']', '++', '--'}))
            if boundary and token.value not in {'case', 'default'}:
                labels.setdefault((owner[index], token.value), []).append(index)
    resolved = {}
    for index, (function, name) in jumps.items():
        ancestors, block = set(), parent[index]
        while block is not None:
            ancestors.add(block)
            block = parent[block]
        targets = [target for target in labels.get((function, name), []) if parent[target] in ancestors]
        resolved[index] = (function, targets[0] if len(targets) == 1 else None)
    return resolved, owner


def _parameter_names(items, start, stop, matching):
    """Names declared by a Go parameter/result list, excluding type names."""
    segments, row, index = [], [], start
    while index < stop:
        token = items[index]
        if token.value == ",":
            segments.append(row)
            row = []
        elif token.value in {"(", "[", "{"} and index in matching:
            end = matching[index]
            row.extend(items[index:end + 1])
            index = end
        else:
            row.append(token)
        index += 1
    if row:
        segments.append(row)

    def named(segment):
        if len(segment) < 2 or segment[0].kind != "word" or segment[1].value == ".":
            return False
        if segment[1].value == "[":
            # p []T / p [N]T declares p; Generic[T] is an unnamed type.
            nested = pairs(segment)
            return 1 in nested and nested[1] + 1 < len(segment)
        return segment[1].kind == "word" or segment[1].value in {"*", "...", "<-", "<"}

    has_names = any(named(segment) for segment in segments)
    return {segment[0].value for segment in segments if named(segment)
            or has_names and len(segment) == 1 and segment[0].kind == "word"}


def _type_end(items, index, matching):
    """Skip one Go type in a signature without consuming a function body."""
    if index >= len(items):
        return index
    word = items[index].value
    if word == "<" and index + 1 < len(items) and items[index + 1].value == "-":
        return _type_end(items, index + 2, matching)
    if word in {"*", "...", "chan", "<-"}:
        return _type_end(items, index + 1, matching)
    if word == "[" and index in matching:
        return _type_end(items, matching[index] + 1, matching)
    if word == "map" and index + 1 in matching:
        return _type_end(items, matching[index + 1] + 1, matching)
    if word in {"struct", "interface"} and index + 1 in matching:
        return matching[index + 1] + 1
    if word == "func" and index + 1 in matching:
        end = matching[index + 1] + 1
        if end < len(items) and items[end].value != "{":
            return _type_end(items, end, matching)
        return end
    if word == "(" and index in matching:
        return matching[index] + 1
    if items[index].kind == "word":
        index += 1
        while index + 1 < len(items) and items[index].value == ".":
            index += 2
        if index < len(items) and items[index].value == "[" and index in matching:
            index = matching[index] + 1
    return index


def _function_bindings(items, matching):
    """Map each function body to receiver, argument and named-result bindings.

    Return types and generic parameters lie between the arguments and body;
    they do not alter the scope of a parameter that shadows an import alias.
    Function type declarations have no body and create no bindings here.
    """
    bindings, signature_tokens = {}, set()
    for index, token in enumerate(items):
        if index in signature_tokens or token.kind != "word" or token.value != "func":
            continue
        cursor, names = index + 1, set()
        # A receiver list is followed by the method name and argument list.
        if cursor in matching and items[cursor].value == "(":
            end = matching[cursor]
            if end + 2 < len(items) and items[end + 1].kind == "word" and items[end + 2].value == "(":
                names.update(_parameter_names(items, cursor + 1, end, matching))
                cursor = end + 1
        if cursor < len(items) and items[cursor].kind == "word":
            cursor += 1
            if cursor < len(items) and items[cursor].value == "[" and cursor in matching:
                cursor = matching[cursor] + 1
        if cursor >= len(items) or items[cursor].value != "(" or cursor not in matching:
            continue
        end = matching[cursor]
        names.update(_parameter_names(items, cursor + 1, end, matching))
        cursor = end + 1
        if cursor < len(items) and items[cursor].value == "(" and cursor in matching:
            end = matching[cursor]
            names.update(_parameter_names(items, cursor + 1, end, matching))
            cursor = end + 1
        elif cursor < len(items) and items[cursor].value != "{":
            cursor = _type_end(items, cursor, matching)
        signature_tokens.update(range(index + 1, cursor))
        if cursor < len(items) and items[cursor].value == "{":
            bindings[cursor] = names
    return bindings


def _go_binding_plan(items, matching, text):
    """Binding changes occur after initializers, simultaneously for each LHS.

    Header scopes outlive their bodies (including else), then restore the outer
    bindings. Braces inside types and composite values do not end declarations.
    """
    events, starts, ends, headers = {}, {}, {}, {}

    def control_body(start):
        kind, cursor = items[start].value, start + 1
        semicolons = 0
        while cursor < len(items):
            value = items[cursor].value
            # A range expression can contain several brace pairs: the
            # anonymous element type, its composite value, then the loop body.
            # Skip complete types/values instead of guessing from the token
            # immediately before a brace.
            if value == 'range' and cursor + 1 < len(items) and items[cursor + 1].value in {'[', 'map', 'struct'}:
                end = _type_end(items, cursor + 1, matching)
                if end < len(items) and items[end].value == '{' and end in matching:
                    cursor = matching[end] + 1
                    continue
            if value in {'struct', 'interface'} and cursor + 1 in matching:
                cursor = matching[cursor + 1] + 1
                continue
            if value == 'func':
                end = _type_end(items, cursor, matching)
                if end < len(items) and items[end].value == '{' and end in matching:
                    cursor = matching[end] + 1
                    continue
            if value in {'(', '['} and cursor in matching:
                cursor = matching[cursor] + 1
                continue
            if value == ';':
                semicolons += 1
            if value == '{' and cursor in matching:
                before = items[start + 1:cursor]
                initializer = any(t.value == ':=' for t in before)
                range_at = next((j for j in range(start + 1, cursor) if items[j].value == 'range'), None)
                type_switch = kind == 'switch' and [token.value for token in before[-4:]] == ['.', '(', 'type', ')']
                if initializer and range_at is None and semicolons == 0 and not type_switch:
                    cursor = matching[cursor] + 1
                    continue
                return cursor
            if value == '}':
                return None
            cursor += 1
        return None

    def control_end(body):
        end = matching[body] + 1
        while end < len(items) and items[end].value == 'else':
            following = end + 1
            if following < len(items) and items[following].value == 'if':
                child = control_body(following)
                if child is None:
                    return end
                end = matching[child] + 1
                continue
            if following in matching and items[following].value == '{':
                return matching[following] + 1
            break
        return end

    for index, token in enumerate(items):
        if token.kind == 'word' and token.value in {'if', 'for', 'switch'}:
            body = control_body(index)
            if body is not None:
                starts[index] = (control_end(body), body, token.value)
                ends[control_end(body)] = ends.get(control_end(body), 0) + 1
                for position in range(index + 1, body):
                    headers.setdefault(position, body)

    def expression_end(start, limit=None):
        cursor, limit = start, min(headers.get(start, len(items)), len(items) if limit is None else limit)
        while cursor < limit:
            token = items[cursor]
            if token.value in {';', '}'}:
                break
            if cursor > start:
                previous = items[cursor - 1]
                if '\n' in text[previous.end:token.start] and (previous.kind in {'word', 'string'} or previous.value in {')', ']', '}', '++', '--'}):
                    break
            if token.value in {'(', '[', '{'} and cursor in matching:
                cursor = matching[cursor] + 1
            else:
                cursor += 1
        return cursor

    def names_at(start, limit):
        names, cursor = [], start
        while cursor < limit and items[cursor].kind == 'word':
            names.append(items[cursor].value)
            cursor += 1
            if cursor >= limit or items[cursor].value != ',':
                break
            cursor += 1
        return names, cursor

    def schedule(names, start, stop, kind):
        rows, cursor, begin = [], start, start
        while cursor < stop:
            if items[cursor].value in {'(', '[', '{'} and cursor in matching:
                cursor = matching[cursor] + 1
                continue
            if items[cursor].value == ',':
                rows.append((begin, cursor))
                begin = cursor + 1
            cursor += 1
        rows.append((begin, stop))
        events.setdefault(stop, []).append((names, rows, kind))

    handled = set()
    for index, token in enumerate(items):
        if token.kind != 'word' or token.value not in {'var', 'const', 'type'}:
            continue
        cursor = index + 1
        grouped = cursor in matching and items[cursor].value == '('
        limit = matching[cursor] if grouped else len(items)
        if grouped:
            cursor += 1
        while cursor < limit:
            names, after_names = names_at(cursor, limit)
            if not names:
                break
            end = expression_end(after_names, limit)
            equal = next((j for j in range(after_names, end) if items[j].value == '='), None)
            if equal is not None:
                handled.add(equal)
                schedule(names, equal + 1, end, 'declare')
            else:
                events.setdefault(end, []).append((names, [], 'declare'))
            if not grouped:
                break
            cursor = end + (end < limit and items[end].value == ';')

    for index, token in enumerate(items):
        if token.value not in {'=', ':='} or index in handled:
            continue
        cursor, right, names = index - 1, index, []
        while cursor >= 0:
            if items[cursor].value in {')', ']'} and cursor in matching:
                cursor = matching[cursor] - 1
                continue
            if items[cursor].kind == 'word' or items[cursor].value in {'.', '*'}:
                cursor -= 1
                if cursor >= 0 and items[cursor].value == '.':
                    continue
                if cursor >= 0 and items[cursor + 1].value == '.':
                    continue
            else:
                break
            segment = items[cursor + 1:right]
            names.insert(0, segment[0].value if len(segment) == 1 and segment[0].kind == 'word' else None)
            if cursor < 0 or items[cursor].value != ',':
                break
            right, cursor = cursor, cursor - 1
        # Keep ignored selector/index targets in place: the other names still
        # receive their corresponding simultaneous RHS values.
        if not any(name is not None for name in names):
            continue
        end = expression_end(index + 1)
        schedule(names, index + 1, end, 'short' if token.value == ':=' else 'assign')
    return events, starts, ends


@dataclass(frozen=True)
class DynamicHit:
    path: str
    line: int
    sink: str
    token: str
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": "dynamic_load",
            "capability": "callgraph",
            "path": self.path,
            "line": self.line,
            "sink": self.sink,
            "token": self.token,
            "evidence": self.evidence,
        }


def detect_source_runtime_dynamic_load(
    root: Path,
    config: Mapping[str, Any],
    *,
    tokens: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    wanted = {str(item).casefold() for item in (tokens or source_runtime_tokens(config))}
    wanted.discard("")
    if not wanted:
        return []
    hits: list[dict[str, Any]] = []
    files = _candidate_files(root)
    package_values = {}
    package_boolean_shadows = {}
    # Package constants may be declared after the caller or in another file.
    for path in files:
        if path.suffix != ".go" or path.name.endswith("_test.go"):
            continue
        items = source_tokens(path.read_text(encoding="utf-8", errors="ignore"), go=True)
        package = items[1].value if len(items) > 1 and items[0].value == "package" else ""
        values = package_values.setdefault((path.parent, package), {})
        matching = pairs(items)
        plan, _, _ = _go_binding_plan(items, matching, path.read_text(encoding="utf-8", errors="ignore"))
        function_ranges = [(start, matching[start]) for start in _function_bindings(items, matching)]
        shadows = package_boolean_shadows.setdefault((path.parent, package), set())
        shadows.update(name for position, events in plan.items()
                       if not any(start < position <= stop for start, stop in function_ranges)
                       for names, _, kind in events if kind != 'assign'
                       for name in names if name in {'true', 'false'})
        level = 0
        for index, item in enumerate(items):
            if item.kind == "punct" and item.value == "{":
                level += 1
            elif item.kind == "punct" and item.value == "}":
                level -= 1
            elif not level and item.value == "=" and index:
                value = _constant(items, index + 1, len(items), values)
                if value is not None:
                    values[items[index - 1].value] = value
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if path.name.endswith("_test.go"):
            continue
        items = source_tokens(text, python=path.suffix == ".py", go=path.suffix == ".go")
        matching = pairs(items)
        aliases = _imports(items, matching) if path.suffix == ".go" else {s: s for s in _SINKS}
        package = items[1].value if len(items) > 1 and items[0].value == "package" else ""
        values = dict(package_values.get((path.parent, package), {})) if path.suffix == ".go" else {}
        scopes = []
        bindings = _function_bindings(items, matching) if path.suffix == ".go" else {}
        jumps, function_owner = _go_jump_plan(items, matching, bindings, text) if path.suffix == '.go' else ({}, {})
        go_events, scope_starts, scope_ends = _go_binding_plan(items, matching, text) if path.suffix == '.go' else ({}, {}, {})
        def clear_binding(name):
            values.pop(name, None)
            for key in list(aliases):
                if key == name or key.startswith(name + '.'):
                    del aliases[key]

        def push_scope(function=False):
            scopes.append((dict(values), dict(aliases), {}, function))

        def declare_binding(name):
            if name not in scopes[-1][2]:
                # An outer binding may already have been assigned in this
                # block before it is shadowed. Save it at declaration time.
                scopes[-1][2][name] = (name in values, values.get(name),
                                      {key: sink for key, sink in aliases.items()
                                       if key == name or key.startswith(name + '.')})

        controls = []
        body_controls = {}
        else_controls = {}
        dead_starts, dead_until, jump_until, incoming = {}, -1, -1, {}
        select_ranges = [(index + 1, matching[index + 1]) for index, token in enumerate(items)
                         if token.kind == 'word' and token.value == 'select'
                         and index + 1 in matching and items[index + 1].value == '{']

        def snapshot():
            return dict(values), dict(aliases)

        def restore(saved):
            values.clear()
            values.update(saved[0])
            aliases.clear()
            aliases.update(saved[1])

        def pop_scope():
            nonlocal_values, nonlocal_aliases, declared, function = scopes.pop()
            if function or path.suffix != '.go':
                # Scanning a function definition must not execute its writes
                # in the surrounding scope, including captured variables.
                values.clear()
                values.update(nonlocal_values)
                aliases.clear()
                aliases.update(nonlocal_aliases)
            else:
                # Only declarations end here. Assignments to existing outer
                # variables remain visible after a normal block/header exits.
                for name, (present, value, saved_aliases) in declared.items():
                    clear_binding(name)
                    if present:
                        values[name] = value
                    aliases.update(saved_aliases)

        for index, token in enumerate(items):
            dead_until = max(dead_until, dead_starts.get(index, -1))
            for _ in range(scope_ends.get(index, 0)):
                control = controls.pop()
                truth = control['truth']
                if truth is None:
                    # A lexical scan does not prove which dynamic branch or
                    # loop ran. Preserve its previous enclosing-scope behavior
                    # rather than treating the last scanned arm as execution.
                    # The initializer always ran. Restore the environment
                    # after it, then remove only the header's local bindings.
                    restore(control['entry'])
                    pop_scope()
                else:
                    if truth is True and control['then'] is not None:
                        restore(control['then'])
                    elif truth is False and control['then'] is None:
                        restore(control['entry'])
                    pop_scope()
            if index in else_controls:
                control = else_controls[index]
                control['then'] = snapshot()
                restore(control['entry'])
                control['arm'] = 'else'
            if index in scope_starts:
                _, body, kind = scope_starts[index]
                push_scope()
                control = {'before': snapshot(), 'entry': None, 'then': None,
                           'truth': None, 'arm': None, 'start': index, 'kind': kind}
                controls.append(control)
                body_controls[body] = control
                after_body = matching[body] + 1
                if after_body < len(items) and items[after_body].value == 'else':
                    else_controls[after_body] = control
            halted = index < dead_until or index < jump_until or any(index < control.get('halted_until', -1) for control in controls)
            for names, rows, kind in (() if halted else go_events.get(index, ())):
                resolved = []
                for start, stop in rows:
                    target = _callable_name(items, start, stop, matching)
                    resolved.append((_constant(items, start, stop, values), aliases.get(target)))
                for position, name in enumerate(names):
                    if name is None or name == '_':
                        continue
                    value, sink = resolved[position] if len(rows) == len(names) else (None, None)
                    if scopes and (kind == 'declare' or kind == 'short' and name not in scopes[-1][2]):
                        declare_binding(name)
                    clear_binding(name)
                    if value is not None:
                        values[name] = value
                    if sink:
                        aliases[name] = sink
            if index in body_controls:
                control = body_controls[index]
                control['entry'] = snapshot()
                control['arm'] = 'then'
                if control['kind'] == 'if':
                    # Restrict execution claims to supported literal conditions.
                    # Skip complete initializer expressions before the final
                    # top-level semicolon, including anonymous composite types.
                    begin, cursor = control['start'] + 1, control['start'] + 1
                    while cursor < index:
                        if items[cursor].value in {'(', '[', '{'} and cursor in matching:
                            cursor = matching[cursor] + 1
                            continue
                        if items[cursor].value == ';':
                            begin = cursor + 1
                        cursor += 1
                    shadowed = set(package_boolean_shadows.get((path.parent, package), ()))
                    shadowed.update(name for frame in scopes for name in frame[2] if name in {'true', 'false'})
                    control['truth'] = _boolean_literal(items, begin, index, matching, shadowed)
                elif control['kind'] == 'switch' and _default_only_switch(items, index, matching):
                    control['truth'] = True
            if token.kind == "punct" and token.value == "{":
                push_scope(function=index in bindings)
                for name in bindings.get(index, ()):
                    declare_binding(name)
                    clear_binding(name)
            elif token.kind == "punct" and token.value == "}" and scopes:
                pop_scope()
            inactive = any(control['truth'] is False and control['arm'] == 'then'
                           or control['truth'] is True and control['arm'] == 'else' for control in controls)
            if index in jumps and not halted and not inactive:
                function, target = jumps[index]
                if target is not None:
                    incoming.setdefault(function, set()).add(target)
                    if (target > index
                            and all(control['truth'] is not None for control in controls if control['start'] > function)
                            and all(_default_only_switch(items, begin, matching) for begin, end in select_ranges
                                    if function < begin < index < end)):
                        jump_until = target
            if token.kind == 'word' and token.value == 'break' and not halted and not inactive:
                # An unlabelled, definite break exits the nearest switch/loop.
                # Continue balancing lexical scopes, but do not execute later
                # assignments/calls in the arm. Unknown paths retain the
                # conservative handling used for other dynamic controls.
                labelled = (index + 1 < len(items) and items[index + 1].kind == 'word'
                            and '\n' not in text[token.end:items[index + 1].start])
                for position in range(len(controls) - 1, -1, -1):
                    control = controls[position]
                    if control['kind'] not in {'switch', 'for'}:
                        continue
                    if any(control['start'] < begin < index < end for begin, end in select_ranges):
                        # select is not a binding-plan control, but consumes
                        # its own break rather than exiting an outer switch.
                        break
                    if (not labelled and control['truth'] is True
                            and all(branch['truth'] is not None for branch in controls[position:])):
                        end = scope_starts[control['start']][0]
                        control['halted_until'] = min([end] + [target for target in incoming.get(function_owner.get(index), ())
                                                              if index < target < end])
                    break
            if path.suffix == '.go' and token.kind == 'word' and token.value == 'return' and not halted and not inactive:
                function = function_owner.get(index)
                if (function is not None
                        and all(control['truth'] is not None for control in controls if control['start'] > function)
                        and all(_default_only_switch(items, begin, matching) for begin, end in select_ranges
                                if function < begin < index < end)):
                    # Return expressions run before the function stops. Mask
                    # only subsequent statements, still balancing all scopes.
                    end = index + 1
                    while end < matching[function]:
                        current, previous = items[end], items[end - 1]
                        if (current.value in {';', '}'} or '\n' in text[previous.end:current.start]
                                and (previous.kind in {'word', 'string'} or previous.value in {')', ']', '}', '++', '--'})):
                            break
                        end = matching[end] + 1 if current.value in {'(', '[', '{'} and end in matching else end + 1
                    until = min([matching[function]] + [target for target in incoming.get(function, ()) if index < target < matching[function]])
                    dead_starts[end] = max(dead_starts.get(end, -1), until)
            if path.suffix != '.go' and token.value in {"=", ":="} and index and index + 1 < len(items):
                name = items[index - 1]
                if name.kind == "word":
                    value = _constant(items, index + 1, len(items), values)
                    if value is None:
                        values.pop(name.value, None)
                    else:
                        values[name.value] = value
                    # Function values are callable aliases. Other assignments
                    # shadow a package qualifier/previous function binding.
                    end = index + 1
                    while end < len(items) and (items[end].kind == "word" or items[end].value == "."):
                        if end > index + 1 and "\n" in text[items[end - 1].end:items[end].start]:
                            break
                        end += 1
                    target = "".join(t.value for t in items[index + 1:end])
                    sink = aliases.get(target)
                    for key in list(aliases):
                        if key == name.value or key.startswith(name.value + "."):
                            del aliases[key]
                    if sink:
                        aliases[name.value] = sink
            if token.kind != "punct" or token.value != "(" or index not in matching:
                continue
            j = index - 1
            if path.suffix == '.go' and j >= 0 and items[j].value == ')' and j in matching:
                j = matching[j]
            elif j < 0 or items[j].kind != "word":
                continue
            else:
                while j >= 2 and items[j - 1].value in {".", "?."} and items[j - 2].kind == "word":
                    j -= 2
            callee = (_callable_name(items, j, index, matching) if path.suffix == '.go'
                      else "".join(t.value for t in items[j:index]))
            sink = aliases.get(callee)
            if halted or inactive:
                continue
            if sink is None:
                continue
            # Dynamic loaders receive the module/plugin path as their first
            # argument. Resolve it in the current lexical scope only.
            value = _constant(items, index + 1, matching[index], values)
            if value is not None:
                matched = next((name for name in sorted(wanted)
                                if process_token_present(value, name) or token_present(value, f"lib{name}")), None)
                if matched is not None:
                    hits.append(DynamicHit(path.relative_to(root).as_posix(),
                                           _line_of(text, items[j].start), sink,
                                           matched, value[:200]).as_dict())
        if path.suffix == ".go":
            for comment in source_tokens(text, go=True, include_comments=True):
                directive = re.fullmatch(r"//go:linkname\s+\w+\s+(\S+)[ \t]*", comment.value) if comment.kind == "comment" else None
                if not directive or comment.start and text[comment.start - 1] != "\n":
                    continue
                value = directive[1]
                matched = next((name for name in sorted(wanted)
                                if token_present(value, name) or token_present(value, "lib" + name)), None)
                if matched:
                    hits.append(DynamicHit(path.relative_to(root).as_posix(),
                                           _line_of(text, comment.start), "//go:linkname",
                                           matched, value).as_dict())
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        key = (hit["path"], hit["line"], hit["sink"], hit["token"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


__all__ = ["detect_source_runtime_dynamic_load"]
