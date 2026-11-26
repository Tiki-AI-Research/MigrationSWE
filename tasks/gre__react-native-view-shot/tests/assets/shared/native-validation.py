"""Parse actual scoped native sources and inspect the release path control flow.

This is syntax/control-flow evidence, not a claim of Android/UWP compilation.
The ordinary public build and all existing tests still run independently.
"""
from pathlib import Path
import json
import re
import sys

sys.path.insert(0, '/opt/migration/native-parser')
from tree_sitter import Language, Parser
import tree_sitter_java
import tree_sitter_c_sharp


def walk(node):
    yield node
    for child in node.named_children:
        yield from walk(child)


def _method_declarations(tree, name):
    wanted = name.decode() if isinstance(name, bytes) else name
    out = []
    for node in walk(tree.root_node):
        if node.type != 'method_declaration':
            continue
        name_node = node.child_by_field_name('name')
        if name_node is not None and name_node.text.decode() == wanted:
            out.append(node)
    return out


def _release_path_bodies(body, helpers_tree):
    """The release method body plus the bodies of any helper it calls.

    A behavior-equivalent candidate may keep the confinement inline in
    ``releaseCapture`` or delegate it to a helper method; both are inspected.
    """
    bodies = [body]
    visited = set()
    for current in bodies:
        for node in walk(current):
            if node.type != 'invocation_expression':
                continue
            function = node.child_by_field_name('function')
            if function is None:
                continue
            name = function.text.rsplit(b'.', 1)[-1]
            if name in visited or helpers_tree is None:
                continue
            visited.add(name)
            for method in _method_declarations(helpers_tree, name):
                helper_body = method.child_by_field_name('body')
                if helper_body is not None:
                    bodies.append(helper_body)
    return bodies


def _code(node):
    """Code with comments removed; string literals cannot supply statements."""
    text = bytearray(node.text)
    for child in walk(node):
        if child.type in {'comment', 'line_comment', 'block_comment'}:
            start = child.start_byte - node.start_byte
            text[start:start + len(child.text)] = b' ' * len(child.text)
    return text.decode()


def _calls(node, names):
    for child in walk(node):
        if child.type not in {'invocation_expression', 'method_invocation'}:
            continue
        name = child.child_by_field_name('function' if child.type == 'invocation_expression' else 'name')
        if name is not None and name.text.rsplit(b'.', 1)[-1].decode() in names:
            yield child


def _rejects(condition, consequence):
    if consequence is None:
        return False
    if consequence.type in {'return_statement', 'throw_statement'}:
        return True
    if consequence.type == 'block':
        statements = [n for n in consequence.named_children if n.type != 'comment']
        return bool(statements) and _rejects(condition, statements[-1])
    if consequence.type == 'if_statement':
        return (_rejects(condition, consequence.child_by_field_name('consequence')) and
                _rejects(condition, consequence.child_by_field_name('alternative')))
    return False


def _raw_path_guard(body, argument):
    # Rootedness must be checked on the input, not on a subsequently computed
    # relative path. The rejection must precede every deletion.
    deletions = list(_calls(body, {'Delete', 'DeleteAsync', 'DeleteFile', 'DeleteDirectory'}))
    cutoff = min((n.start_byte for n in deletions), default=body.end_byte)
    for node in walk(body):
        if node.type != 'if_statement' or node.end_byte > cutoff:
            continue
        condition = node.child_by_field_name('condition')
        consequence = node.child_by_field_name('consequence')
        if condition is None or not _rejects(condition, consequence):
            continue
        if _when_rooted(condition, argument) is True:
            return True
    return False


def _when_rooted(node, argument):
    """Three-valued evaluation: must this guard fire for a rooted input?"""
    if node.type == 'parenthesized_expression' and node.named_children:
        return _when_rooted(node.named_children[0], argument)
    if node.type == 'invocation_expression':
        fn = node.child_by_field_name('function')
        args = node.child_by_field_name('arguments')
        if (fn is not None and fn.text == b'Path.IsPathRooted' and args is not None and
                re.fullmatch(r'\(\s*' + re.escape(argument) + r'\s*\)', args.text.decode())):
            return True
    if node.text in {b'true', b'false'}:
        return node.text == b'true'
    if node.type == 'prefix_unary_expression' and node.text.lstrip().startswith(b'!'):
        value = _when_rooted(node.named_children[-1], argument)
        return None if value is None else not value
    if node.type == 'binary_expression':
        left = node.child_by_field_name('left')
        right = node.child_by_field_name('right')
        op = node.child_by_field_name('operator')
        if op is None:
            op = next((child for child in node.children if child.text in {b'||', b'&&', b'==', b'!='}), None)
        if left is None or right is None or op is None:
            return None
        a, b = _when_rooted(left, argument), _when_rooted(right, argument)
        if op.text == b'||':
            return True if a is True or b is True else False if a is False and b is False else None
        if op.text == b'&&':
            return False if a is False or b is False else True if a is True and b is True else None
        if a is not None and b is not None and op.text in {b'==', b'!='}:
            return (a == b) if op.text == b'==' else (a != b)
    return None


def windows_guard(tree, helpers_tree=None):
    """True when the release path confines deletion to the capture directory.

    Recognizes the declared behavior rather than a fixed spelling: the path must
    reject rooted inputs (``Path.IsPathRooted``) and enforce containment
    (``StartsWith("..")`` or a canonical ``Path.GetFullPath``/``GetRelativePath``
    prefix check), inline in ``releaseCapture`` or delegated to a helper it
    calls. The deletion must still go through the module's release path.
    """
    methods = _method_declarations(tree, 'releaseCapture')
    if len(methods) != 1:
        return False
    body = methods[0].child_by_field_name('body')
    if body is None:
        return False
    # Inspect every deletion, including directory/instance/async APIs. A guarded
    # File.Delete cannot justify a second deletion of the whole LocalFolder.
    deletes = [n for n in walk(body) if n.type == 'invocation_expression'
               and (n.child_by_field_name('function') is not None)
               and n.child_by_field_name('function').text.rsplit(b'.', 1)[-1]
               in {b'Delete', b'DeleteAsync', b'DeleteFile', b'DeleteDirectory'}]
    if not deletes:
        return False
    for delete in deletes:
        # Each deletion must go through a resolved variable, never a literal
        # path or the LocalFolder root directly (a guarded File.Delete cannot
        # justify a second deletion of the whole folder). The variable spelling
        # is free.
        args = delete.child_by_field_name('arguments')
        if args is not None:
            for argument in args.named_children:
                text = argument.text.decode()
                if '"' in text or 'ApplicationData' in text or 'LocalFolder' in text:
                    return False
    parameters = methods[0].child_by_field_name('parameters')
    if parameters is None or not parameters.named_children:
        return False
    parameter = parameters.named_children[0].child_by_field_name('name')
    if parameter is None:
        return False
    argument = parameter.text.decode()
    candidates = _release_path_bodies(body, helpers_tree)
    text = '\n'.join(_code(candidate) for candidate in candidates)
    rooted = _raw_path_guard(body, argument)
    # Also follow an actual helper call with the raw input. Helpers are checked
    # against their own parameter names, rather than any unrelated rooted test.
    if helpers_tree is not None:
        for call in _calls(body, {m.child_by_field_name('name').text.decode()
                                 for m in walk(helpers_tree.root_node) if m.type == 'method_declaration'}):
            args = call.child_by_field_name('arguments')
            fn = call.child_by_field_name('function')
            if args is None or fn is None:
                continue
            for method in _method_declarations(helpers_tree, fn.text.rsplit(b'.', 1)[-1]):
                params = method.child_by_field_name('parameters')
                helper_body = method.child_by_field_name('body')
                if params is None or helper_body is None:
                    continue
                for actual, formal in zip(args.named_children, params.named_children):
                    name = formal.child_by_field_name('name')
                    if actual.text.decode() != argument or name is None:
                        continue
                    raw = name.text.decode()
                    if _raw_path_guard(helper_body, raw):
                        rooted = True
                    # Flat filename validators confine by construction. They
                    # must be used as an early rejection guard in releaseCapture.
                    compact = re.sub(r'\s+', '', _code(helper_body))
                    whitelist = ('||' not in compact and f'!Path.IsPathRooted({raw})' in compact and
                                 f'{raw}!="."' in compact and f'{raw}!=".."' in compact and
                                 f'{raw}.IndexOfAny(' in compact and ')<0' in compact and
                                 "'/'" in compact and "'\\\\'" in compact and "':'" in compact)
                    for guard in walk(body):
                        if guard.type != 'if_statement':
                            continue
                        cond = guard.child_by_field_name('condition')
                        consequence = guard.child_by_field_name('consequence')
                        if (cond is not None and call.start_byte >= cond.start_byte and call.end_byte <= cond.end_byte
                                and guard.end_byte < min(n.start_byte for n in deletes)
                                and _rejects(cond, consequence) and _code(cond).lstrip(' (').startswith('!') and whitelist):
                            return 'Path.Combine(' in text and 'ApplicationData.Current.LocalFolder' in text
    containment_guard = False
    for candidate in candidates:
        cutoff = min((n.start_byte for n in _calls(candidate, {'Delete', 'DeleteAsync', 'DeleteFile', 'DeleteDirectory'})),
                     default=candidate.end_byte)
        for guard in walk(candidate):
            if guard.type != 'if_statement' or guard.end_byte > cutoff:
                continue
            cond = guard.child_by_field_name('condition')
            consequence = guard.child_by_field_name('consequence')
            if cond is None or not _rejects(cond, consequence):
                continue
            for call in _calls(cond, {'StartsWith'}):
                args = call.child_by_field_name('arguments')
                if args is None:
                    continue
                before = _code(cond)[:call.start_byte - cond.start_byte].rstrip()
                if before.endswith('!') or re.match(r'\(\s*"\.\."', args.text.decode()):
                    containment_guard = True
    containment = (
        'Path.Combine(' in text and 'Path.GetFullPath(' in text and
        containment_guard
    )
    return rooted and containment and 'ApplicationData.Current.LocalFolder' in text


def windows_size_guard(tree):
    """Encoded-size rejection must dominate tmpfile as well as base64 returns."""

    def throws_invalid_operation(node):
        """Recognize direct and locally-bound throws in the guarded branch.

        ``throw new InvalidOperationException()`` and the behavior-equivalent
        ``var error = new InvalidOperationException(); throw error;`` both
        guarantee the contract.  Keep the binding local to this consequence
        and require its declaration to precede the throw so an unrelated type
        name elsewhere in the method cannot satisfy the check.
        """
        invalid_operation_bindings = set()
        for child in sorted(walk(node), key=lambda item: item.start_byte):
            if child.type == 'variable_declarator':
                name = child.child_by_field_name('name')
                value = child.child_by_field_name('value')
                # The pinned tree-sitter-c-sharp grammar leaves the initializer
                # field unnamed: variable_declarator has the name followed by
                # the initializer as its second named child.  Retain the field
                # lookup for compatible grammar versions, but only fall back to
                # that direct child rather than searching arbitrary descendants.
                if value is None and len(child.named_children) == 2:
                    value = child.named_children[1]
                if name is None or value is None:
                    continue
                # Some grammar versions wrap the initializer. Unwrap only a
                # single-expression initializer (and harmless parentheses), so
                # a nested constructor argument cannot masquerade as the value
                # that is subsequently thrown.
                initializer = value
                while (initializer.type in {'equals_value_clause', 'parenthesized_expression'}
                       and len(initializer.named_children) == 1):
                    initializer = initializer.named_children[0]
                if (initializer.type == 'object_creation_expression' and
                        initializer.child_by_field_name('type') is not None and
                        initializer.child_by_field_name('type').text == b'InvalidOperationException'):
                    invalid_operation_bindings.add(name.text)
            if child.type != 'throw_statement':
                continue
            for thrown in walk(child):
                if (thrown.type == 'object_creation_expression' and
                        thrown.child_by_field_name('type') is not None and
                        thrown.child_by_field_name('type').text == b'InvalidOperationException'):
                    return True
            expression = child.named_children[0] if child.named_children else None
            if expression is not None and expression.text in invalid_operation_bindings:
                return True
        return False

    for method in _method_declarations(tree, 'Execute'):
        body = method.child_by_field_name('body')
        if body is None:
            continue
        first_return = min((n.start_byte for n in walk(body) if n.type == 'return_statement'), default=body.end_byte)
        for node in walk(body):
            if node.type != 'if_statement' or node.end_byte >= first_return:
                continue
            condition = node.child_by_field_name('condition')
            consequence = node.child_by_field_name('consequence')
            if condition is None or consequence is None:
                continue
            comparison = re.sub(r'\s+', '', _code(condition))
            if not re.fullmatch(r'\(?\w+\.Size>int\.MaxValue\)?', comparison):
                continue
            if throws_invalid_operation(consequence):
                return True
    return False


def windows_tmpfile_releasable(tree):
    """The Windows tmpfile result must be accepted by confined cleanup.

    ``releaseCapture`` is required to reject absolute paths, so returning the
    ``StorageFile.Path`` absolute path makes the newly captured file impossible
    to release through the preserved public API.  A ``StorageFile.Name`` (or a
    basename derived with ``Path.GetFileName``) is relative to LocalFolder and
    remains releasable.
    """
    for method in _method_declarations(tree, 'Execute'):
        body = method.child_by_field_name('body')
        if body is None:
            continue
        for branch in walk(body):
            if branch.type != 'if_statement':
                continue
            condition = branch.child_by_field_name('condition')
            consequence = branch.child_by_field_name('consequence')
            if condition is None or consequence is None:
                continue
            compact_condition = re.sub(r'\s+', '', _code(condition))
            if ('result=="tmpfile"' not in compact_condition and
                    '"tmpfile"==result' not in compact_condition and
                    'result.Equals("tmpfile")' not in compact_condition and
                    '"tmpfile".Equals(result)' not in compact_condition):
                continue
            for returned in walk(consequence):
                if returned.type != 'return_statement':
                    continue
                expression = returned.named_children[0] if returned.named_children else None
                if expression is None:
                    continue
                value = re.sub(r'\s+', '', _code(expression))
                if re.fullmatch(r'[A-Za-z_]\w*\.Name', value):
                    return True
                if re.fullmatch(r'(?:Path\.)?GetFileName\(.+\)', value):
                    return True
    return False


def windows_error_code(tree, code_constant=True):
    """Follow registered capture methods to a coded ReactError rejection."""
    methods = [n for n in walk(tree.root_node) if n.type == 'method_declaration']
    by_name = {}
    for method in methods:
        by_name.setdefault(method.child_by_field_name('name').text.decode(), []).append(method)

    def mapped(method, seen):
        name = method.child_by_field_name('name').text.decode()
        if name in seen:
            return False
        seen = seen | {name}
        body = method.child_by_field_name('body')
        if body is None:
            return False
        for call in _calls(body, {'Reject'}):
            args = call.child_by_field_name('arguments')
            if args is None:
                continue
            for assignment in walk(args):
                if assignment.type != 'assignment_expression':
                    continue
                left = assignment.child_by_field_name('left')
                right = assignment.child_by_field_name('right')
                if (left is not None and right is not None and left.text == b'Code' and
                        (right.text == b'"E_UNABLE_TO_SNAPSHOT"' or
                         (code_constant and right.text == b'ViewShot.ErrorUnableToSnapshot'))):
                    return True
        for call in _calls(body, set(by_name)):
            fn = call.child_by_field_name('function')
            if fn is not None and any(mapped(m, seen) for m in by_name[fn.text.rsplit(b'.', 1)[-1].decode()]):
                return True
        return False

    for exported in ('captureRef', 'captureScreen'):
        entries = []
        for method in methods:
            attributes = [n for n in method.named_children if n.type == 'attribute_list']
            name = method.child_by_field_name('name').text.decode()
            if any(re.search(r'\bReactMethod\s*\(\s*"' + exported + r'"\s*\)', _code(a)) or
                   (name == exported and re.search(r'\bReactMethod\s*\]', _code(a))) for a in attributes):
                entries.append(method)
        if not entries or not all(mapped(m, set()) for m in entries):
            return False
    return True


def _gate_free(node, body):
    """False when ``node`` is only reachable behind the compile-time flag.

    The route must not depend solely on ``IS_NEW_ARCHITECTURE_ENABLED``; a
    condition that also consults ``isBridgeless`` is accepted, mirroring the
    reference's "or the bridgeless condition" allowance.
    """
    parent = node.parent
    def condition_nodes(condition):
        yield condition
        for child in condition.named_children:
            # A callback passed to runOnUiThread is work to execute, not a
            # boolean operand of the surrounding scheduling-failure guard.
            if child.type not in {'lambda_expression', 'class_body'}:
                yield from condition_nodes(child)
    while parent is not None and parent != body:
        if parent.type == 'if_statement':
            condition = parent.child_by_field_name('condition')
            if condition is not None:
                identifiers = {n.text for n in condition_nodes(condition)
                               if n.type in {'identifier', 'field_access'}}
                bridgeless = any(n.type == 'method_invocation'
                                 and n.child_by_field_name('name') is not None
                                 and n.child_by_field_name('name').text == b'isBridgeless'
                                 for n in condition_nodes(condition))
                if b'IS_NEW_ARCHITECTURE_ENABLED' in identifiers and not bridgeless:
                    return False
        parent = parent.parent
    return True


def android_fabric_route(tree, capture_tree=None):
    """Follow a runtime UI-manager selection into the capture route.

    Accepts any runtime manager selection (``UIManagerHelper.getUIManager`` or
    ``getUIManagerForReactTag``) that then schedules the snapshot through a gate
    free route: ``addUIBlock`` (posting a UI block), ``resolveView`` (resolving
    the target view) or ``execute`` (handing the manager to the scoped
    ``ViewShot`` capture object). The route must not be reachable only behind
    the compile-time ``IS_NEW_ARCHITECTURE_ENABLED`` flag. The reference's
    legacy fallback and local variable spellings are not required; string or
    comment nodes cannot supply method invocations or the manager variable.
    """
    methods = [n for n in walk(tree.root_node) if n.type == 'method_declaration'
               and n.child_by_field_name('name').text == b'captureRef']
    if len(methods) != 1:
        return False
    body = methods[0].child_by_field_name('body')
    if body is None:
        return False

    # The request can hand a ViewShot Runnable to the UI thread;
    # manager lookup and resolveView may then live in ViewShot.java. Follow
    # calls and those execution entries rather than requiring them in captureRef.
    methods_by_name = {}
    for source in [tree, capture_tree]:
        if source is None:
            continue
        for method in walk(source.root_node):
            if method.type == 'method_declaration':
                name = method.child_by_field_name('name')
                if name is not None:
                    methods_by_name.setdefault(name.text.decode(), []).append(method)
    bodies = [body]
    visited = {'captureRef'}
    for current in bodies:
        # Preserve the caller's compile-time reachability when expanding the
        # local call graph.  In particular, an ``execute`` call in the legacy
        # alternative of ``IS_NEW_ARCHITECTURE_ENABLED`` cannot make that method
        # evidence for a new-architecture branch which only registers a listener.
        calls = {n.child_by_field_name('name').text.decode()
                 for n in _calls(current, set(methods_by_name))
                 if _gate_free(n, current)}
        # Only scheduled entry points become reachable. Merely constructing a
        # capture object must not make its unused methods evidence of work.
        if any(_gate_free(call, current)
               for call in _calls(current, {'runOnUiThread', 'post'})):
            calls.add('run')
        if any(_gate_free(call, current)
               for call in _calls(current, {'addUIBlock'})):
            calls.add('execute')
        for name in calls - visited:
            visited.add(name)
            for method in methods_by_name.get(name, []):
                target_body = method.child_by_field_name('body')
                if target_body is not None:
                    bodies.append(target_body)

    runtime_manager = False
    for current in bodies:
        for node in walk(current):
            if node.type != 'method_invocation':
                continue
            obj = node.child_by_field_name('object')
            method = node.child_by_field_name('name')
            if (obj is not None and obj.text == b'UIManagerHelper'
                    and method is not None
                    and method.text in {b'getUIManager', b'getUIManagerForReactTag'}
                    and _gate_free(node, current)):
                runtime_manager = True
    if not runtime_manager:
        return False

    # A capture request must schedule work that is guaranteed to run. Merely
    # registering a UIManagerListener is not sufficient: if no later mount is
    # dispatched, its callback never runs and the capture promise stays pending.
    # Follow addUIBlock and immediate UI-thread dispatches, but do not invent a
    # future listener callback when checking reachability.
    resolved = any(_gate_free(call, current) for current in bodies
                   for call in _calls(current, {'resolveView'}))
    block = any(_gate_free(call, current) for current in bodies
                for call in _calls(current, {'addUIBlock'}))
    ui_dispatch = any(_gate_free(call, current) for current in bodies
                      for call in _calls(current, {'runOnUiThread', 'post'}))
    return resolved and (block or ui_dispatch)


def check(root):
    parsers = {'.java': Parser(Language(tree_sitter_java.language())),
               '.cs': Parser(Language(tree_sitter_c_sharp.language()))}
    failures = []
    files = []
    fabric_route = False
    windows_error = False
    windows_size = False
    windows_tmpfile = False
    windows_confined = False
    helpers_path = root / 'windows' / 'RNViewShot' / 'Helpers.cs'
    helpers_tree = None
    if helpers_path.is_file() and not helpers_path.is_symlink():
        helpers_tree = parsers['.cs'].parse(helpers_path.read_bytes())
    capture_path = root / 'android/src/main/java/fr/greweb/reactnativeviewshot/ViewShot.java'
    capture_tree = (parsers['.java'].parse(capture_path.read_bytes())
                    if capture_path.is_file() and not capture_path.is_symlink() else None)
    win_capture_path = root / 'windows/RNViewShot/ViewShot.cs'
    code_constant = False
    if win_capture_path.is_file() and not win_capture_path.is_symlink():
        win_capture_tree = parsers['.cs'].parse(win_capture_path.read_bytes())
        for node in walk(win_capture_tree.root_node):
            if node.type == 'variable_declarator':
                name = node.child_by_field_name('name')
                if name is not None and name.text == b'ErrorUnableToSnapshot':
                    code_constant = bool(re.fullmatch(r'ErrorUnableToSnapshot\s*=\s*"E_UNABLE_TO_SNAPSHOT"', _code(node)))
    for directory in ['android/src/main', 'android/src/paper', 'windows/RNViewShot']:
        for file in sorted((root / directory).rglob('*')):
            if not file.is_file() or file.is_symlink() or file.suffix not in parsers:
                continue
            relative = file.relative_to(root).as_posix()
            tree = parsers[file.suffix].parse(file.read_bytes())
            files.append(relative)
            if tree.root_node.has_error:
                failures.append({'path': relative, 'reason': 'native_syntax_error'})
            if relative == 'windows/RNViewShot/RNViewShotModule.cs':
                windows_confined = windows_guard(tree, helpers_tree)
                windows_error = windows_error_code(tree, code_constant)
                if not windows_confined:
                    failures.append({'path': relative, 'reason': 'delete_not_confined_by_raw_input_guard'})
            if relative == 'windows/RNViewShot/ViewShot.cs':
                windows_size = windows_size_guard(tree)
                windows_tmpfile = windows_tmpfile_releasable(tree)
            if relative.endswith('/RNViewShotModule.java'):
                fabric_route = not tree.root_node.has_error and android_fabric_route(tree, capture_tree)
    if not any(p.endswith('RNViewShotModule.cs') for p in files):
        failures.append({'reason': 'missing_windows_module'})
    return {'ok': not failures, 'parsed_files': files, 'failures': failures,
            'android_fabric_route': fabric_route,
            'windows_release_confined': windows_confined,
            'windows_capture_error_code': windows_error,
            'windows_size_guard': windows_size,
            'windows_tmpfile_releasable': windows_tmpfile,
            'scope': 'native_syntax_and_capture_control_flow'}


if __name__ == '__main__':
    result = check(Path(sys.argv[1]))
    print(json.dumps(result))
    sys.exit(0 if result['ok'] else 1)
