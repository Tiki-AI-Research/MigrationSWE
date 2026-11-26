"""Parameter-vector shell oracles and forward Go jump reachability checks."""
import itertools
import shlex
import unittest

from regressions import test_shell_bindings, test_shell_execution


class ParameterVectorTests(unittest.TestCase):
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    inspect = test_shell_execution.ShellExecutionTests.inspect
    compare_shell = test_shell_bindings.ShellBindingTests.compare_shell

    def test_at_parameter_operators_preserve_executable_fields(self):
        for arguments, expression in itertools.product(
                ['node /action/dist/index.js', '/action/release-action value', '', '"" ""'],
                ['"${@-true}"', '"${@:-true}"', '"${@+node}"', '"${@:+node}"']):
            self.compare_shell('set -- ' + arguments + '\n' + expression + '\n/action/release-action')

    def test_nested_defaults_keep_vectors_and_quote_context(self):
        for setup, arguments, expression in itertools.product(
                ['unset VALUE', 'VALUE=""', 'VALUE=true'],
                ['node /action/dist/index.js', '/action/release-action value', '"" ""', ''],
                ['${VALUE:-"$@"}', '"${VALUE:-$@}"', '"${VALUE:-"$@"}"',
                 '${VALUE-"$@"}', '"${VALUE-$@}"', '"${VALUE:+$@}"']):
            with self.subTest(setup=setup, arguments=arguments, expression=expression):
                self.compare_shell(setup + '\nset -- ' + arguments + '\n' + expression + '\n/action/release-action')

    def test_default_vector_empty_fields_and_repetition_follow_shell(self):
        for args, expression in itertools.product([[], [''], ['', ''], ['first', 'last'], ['a b', 'node']], [
                '"${@:-true}"', 'pre"${@:-true}"post', '"${VALUE:-$@}"',
                '${VALUE:-"$@"}', '"${VALUE:-$@}${VALUE:-$@}"',
                '"${VALUE:-prefix$@suffix}"']):
            text = 'unset VALUE\nset -- ' + shlex.join(args) + '\nfor item in ' + expression + ';do node;done\n/action/release-action'
            with self.subTest(args=args, expression=expression):
                self.compare_shell(text)

    def test_unquoted_star_vectors_survive_literal_concatenation(self):
        for separator, args, expression in itertools.product(['', ':', ' ', ' :'], [[], [''], ['true', 'node'], ['true node', 'node', '']], [
                '$*', '\'\'$*', '$*\'\'', 'pre$*', '$*post', '$*$*', '"$*"']):
            text = ('IFS=' + shlex.quote(separator) + '\nset -- ' + shlex.join(args) +
                    '\nfor item in ' + expression + ';do case "$item" in node)node;;esac;done\n/action/release-action')
            with self.subTest(separator=separator, args=args, expression=expression):
                self.compare_shell(text)


class ParameterWordTests(unittest.TestCase):
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    inspect = test_shell_execution.ShellExecutionTests.inspect
    compare_shell = test_shell_bindings.ShellBindingTests.compare_shell

    def compare_value(self, expression, expected, setup='unset VALUE OTHER'):
        # Both branch directions matter: a bad expansion can either hide a
        # real runtime invocation or fabricate an invocation in the else arm.
        for yes, no in [('node', ':'), (':', 'node')]:
            text = (setup + '\nTEXT=' + expression + '\nif test "$TEXT" = ' + shlex.quote(expected) +
                    ';then ' + yes + ';else ' + no + ';fi\n/action/release-action')
            self.compare_shell(text)

    def test_default_word_whitespace_and_metacharacters_are_data(self):
        for value in ['a  b', ' a\t b ', 'a\nb', '#node', 'a;b', 'a|b', 'a&b', '(node)', '{node}']:
            with self.subTest(value=value):
                self.compare_value('"${VALUE:-' + value + '}"', value)
        self.compare_value('${VALUE:-{node}}', '{node}')

    def test_default_quotes_inherit_outer_double_quotes(self):
        for expression, expected in [
                ('"${VALUE:-\'node\'}"', "'node'"), ('${VALUE:-\'node\'}', 'node'),
                ('${VALUE:-"node space"}', 'node space'), ('"${VALUE:-"node space"}"', 'node space'),
                ('"${VALUE:-${OTHER:-\'node\'}}"', "'node'"),
                ('${VALUE:-${OTHER:-"node space"}}', 'node space')]:
            with self.subTest(expression=expression):
                self.compare_value(expression, expected)
        for expression in ['"${VALUE:-\'node\'}"', '${VALUE:-"node /action/dist/index.js"}']:
            self.compare_shell('unset VALUE\n' + expression + '\n/action/release-action')

    def test_default_backslashes_follow_parameter_quote_context(self):
        for expression, expected in [
                (r'"${VALUE:-a\ b}"', r'a\ b'), (r'${VALUE:-a\ b}', 'a b'),
                (r'"${VALUE:-\"node\"}"', '"node"'),
                (r'"${VALUE:-a\}b}"', 'a}b'), (r'${VALUE:-a\}b}', 'a}b'),
                ('"${VALUE:-a\\\nb}"', 'ab')]:
            with self.subTest(expression=expression):
                self.compare_value(expression, expected)

    def test_brace_termination_respects_quotes_and_substitutions(self):
        for expression, expected in [
                ('${VALUE:-"}"}', '}'), ("${VALUE:-'}'}", '}'), ('"${VALUE:-"}"}"', '}'),
                ('"${VALUE:-$(printf "%s" "a}b")}"', 'a}b'),
                ('"${VALUE:-$(printf "%s" "a)b")}"', 'a)b'),
                ('$(printf "%s" ${VALUE:-)})', ')'),
                ('"${VALUE:-`printf "%s" "a}b"`}"', 'a}b')]:
            with self.subTest(expression=expression):
                self.compare_value(expression, expected)

    def test_all_default_operators_preserve_whitespace_and_lazy_execution(self):
        for setup, operator, quoted in itertools.product(
                ['unset VALUE', 'VALUE=""', 'VALUE="kept value"'], ['-', ':-', '+', ':+', '=', ':='], [False, True]):
            present = setup != 'unset VALUE' and (setup != 'VALUE=""' or not operator.startswith(':'))
            alternate = present if operator.endswith('+') else not present
            expected = 'a  b' if alternate else '' if operator.endswith('+') or setup == 'VALUE=""' else 'kept value'
            expression = '${VALUE' + operator + 'a  b}'
            if quoted:
                expression = '"' + expression + '"'
            with self.subTest(setup=setup, operator=operator, quoted=quoted):
                self.compare_value(expression, expected, setup)
                self.compare_shell(setup + '\nTEXT="${VALUE' + operator + '$(node)}"\n/action/release-action')

    def test_inner_quotes_keep_fields_with_prefixes_and_vectors(self):
        for arguments, expression in itertools.product(['', '"" ""', 'true node', '"true node" node'], [
                '${VALUE:-"true node"}', '${VALUE:-true  node}', '${VALUE:-true\\ node}',
                '${VALUE:-"$@"}', '"${VALUE:-$@}"', '${VALUE:-"true node"$@}',
                '"${VALUE:-pre$@post}"', '${VALUE:-"${OTHER:-true node}"}']):
            text = ('unset VALUE OTHER\nset -- ' + arguments + '\nfor item in ' + expression +
                    ';do case "$item" in node)node;;esac;done\n/action/release-action')
            with self.subTest(arguments=arguments, expression=expression):
                self.compare_shell(text)

    def test_default_field_boundaries_preserve_every_argument(self):
        for separator, expression, expected in [
                (' \t\n', '${VALUE:-"true node"}', ['true node']),
                (' \t\n', '${VALUE:-true  node}', ['true', 'node']),
                (' \t\n', '${VALUE:-true\\ node}', ['true node']),
                ('', '${VALUE:-$@}', ['true', 'node']),
                (':', '${VALUE:-true:node}', ['true', 'node']),
                (':', '"${VALUE:-true:node}"', ['true:node']),
                (' \t\n', '"${VALUE:-$@}"', ['true', 'node'])]:
            condition = 'test "$#" = ' + str(len(expected)) + ''.join(
                ' && test "${' + str(index + 1) + '}" = ' + shlex.quote(value)
                for index, value in enumerate(expected))
            for yes, no in [('node', ':'), (':', 'node')]:
                text = ('unset VALUE\nIFS=' + shlex.quote(separator) + '\nset -- true node\nset -- ' + expression +
                        '\nif ' + condition + ';then ' + yes + ';else ' + no + ';fi\n/action/release-action')
                with self.subTest(separator=separator, expression=expression, yes=yes):
                    self.compare_shell(text)

    def test_parameter_braces_follow_the_selected_shell_dialect(self):
        for expression, expected in [
                (r'''"${VALUE:-'}'}"''', {'sh': "''}", 'bash': "'}'"}),
                (r'${VALUE:-"\}"}', {'sh': '}', 'bash': r'\}'})]:
            for dialect, value in expected.items():
                for yes, no in [('node', ':'), (':', 'node')]:
                    text = ('unset VALUE\nTEXT=' + expression + '\nif test "$TEXT" = ' + shlex.quote(value) +
                            ';then ' + yes + ';else ' + no + ';fi\n/action/release-action')
                    with self.subTest(expression=expression, dialect=dialect, yes=yes):
                        self.compare_shell(dialect + ' -c ' + shlex.quote(text))
                        checked = self.inspect('#!/bin/' + dialect + '\n' + text)
                        self.assertTrue(checked['go'], checked)
                        self.assertEqual(bool(checked['source_runtime_hits']), yes == 'node', checked)
                        checked = self.inspect('#!/usr/bin/env -S ' + dialect + '\n' + text)
                        self.assertEqual(bool(checked['source_runtime_hits']), yes == 'node', checked)

    def test_explicit_script_interpreter_overrides_its_shebang(self):
        expression = r'''"${VALUE:-'}'}"'''
        for dialect, value, shebang in [('sh', "''}", 'bash'), ('bash', "'}'", 'sh')]:
            docker = self.root / 'Dockerfile'
            original = docker.read_text()
            docker.write_text(original.replace('["/action/entry.sh"]', '["/bin/' + dialect + '","/action/entry.sh"]'))
            try:
                script = ('#!/bin/' + shebang + '\nunset VALUE\nTEXT=' + expression +
                          '\nif test "$TEXT" = ' + shlex.quote(value) + ';then :;else node;fi\n/action/release-action')
                checked = self.inspect(script)
                self.assertTrue(checked['go'], checked)
                self.assertEqual(checked['source_runtime_hits'], [], checked)
            finally:
                docker.write_text(original)

    def test_composite_shell_selection_is_scoped_to_each_step(self):
        action = 'runs:\n  using: composite\n  steps:\n'
        for index, (dialect, value) in enumerate([('bash', "'}'"), ('sh', "''}")]):
            body = ('unset VALUE\nTEXT=' + r'''"${VALUE:-'}'}"''' +
                    '\nif test "$TEXT" = ' + shlex.quote(value) + ';then :;else node;fi\ngo run .')
            if index == 0:
                action += '    - shell: ' + dialect + '\n      run: |\n'
            else:
                action += '    - run: |\n'
            action += ''.join('        ' + line + '\n' for line in body.splitlines())
            if index:
                action += '      shell: ' + dialect + '\n'
        checked = test_shell_execution.inspect_action(self.root, action)
        self.assertTrue(checked['go'], checked)
        self.assertEqual(checked['source_runtime_hits'], [], checked)

    def test_bash_default_operands_join_unquoted_vectors_before_splitting(self):
        arguments = ['node', '', 'true']
        for separator, expression, actual, expected in [
                (':', '${VALUE:-$@}', arguments, ['node  true']),
                (':', '${VALUE:-pre$@post}', arguments, ['prenode  truepost']),
                (':', '${VALUE:-$*}', arguments, ['node', '', 'true']),
                ('', '${VALUE:-$@}', arguments, ['node', '', 'true']),
                (':', '${VALUE:-"$@"}', arguments, ['node', '', 'true']),
                (':', '"${VALUE:-$@}"', arguments, ['node', '', 'true']),
                (':', '${VALUE:-$@}', [''], ['']),
                (': ', '${VALUE:-$@}', ['', ''], ['', '']),
                ('\t ', '${VALUE:-$@}', ['', ''], ['', '']),
                (': ', '${VALUE:-$@}', ['true node', 'node'], ['true node', 'node']),
                (':', '${VALUE:-$@}', ['tr:ue node', 'node'], ['tr:ue node node']),
                (':', '${VALUE:-pre:$@post}', ['tr:ue node', 'node'], ['pre tr:ue node nodepost']),
                (':', '${VALUE:-pre$@post:}', ['tr:ue node', 'node'], ['pretr:ue node nodepost']),
                (':', '"${VALUE:-pre:$@post}"', ['tr:ue node', 'node'], ['pre', 'tr:ue node', 'nodepost']),
                (':', '"${VALUE:-pre$@post:}"', ['tr:ue node', 'node'], ['pretr:ue node', 'nodepost']),
                (':', '"${VALUE:-pre:$@post}"', [], ['pre', 'post']),
                (':', '${VALUE:-$*}', ['', ''], ['']),
                ('', '${VALUE:-$@}', [''], [])]:
            condition = 'test "$#" = ' + str(len(expected)) + ''.join(
                ' && test "${' + str(index + 1) + '}" = ' + shlex.quote(value)
                for index, value in enumerate(expected))
            text = ('unset VALUE\nIFS=' + shlex.quote(separator) + '\nset -- ' + shlex.join(actual) + '\nset -- ' + expression +
                    '\nif ' + condition + ';then :;else node;fi\n/action/release-action')
            with self.subTest(separator=separator, expression=expression):
                self.compare_shell('bash -c ' + shlex.quote(text))

    def compare_helper_fallback(self, script, expected_dialect, shebang='', nested=False):
        value = "'}'" if expected_dialect == 'bash' else "''}"
        for yes, no in [('node', ':'), (':', 'node')]:
            body = (shebang + 'unset VALUE\nTEXT=' + r'''"${VALUE:-'}'}"''' +
                    '\nif test "$TEXT" = ' + shlex.quote(value) + ';then ' + yes + ';else ' + no + ';fi\n' +
                    'HERE=$(dirname "$0")\n"$HERE/release-action"\n')
            helper = self.root / 'helper.sh'
            helper.write_text(body)
            helper.chmod(0o755)
            if nested:
                outer = self.root / 'outer.sh'
                outer.write_text('HERE=$(dirname "$0")\n"$HERE/helper.sh"\n')
                outer.chmod(0o755)
            self.compare_shell(script)

    def test_no_shebang_helpers_follow_caller_and_env_fallback(self):
        for script, dialect, nested in [
                ('bash -c /action/helper.sh', 'bash', False),
                ('bash -c "command /action/helper.sh"', 'bash', False),
                ('bash -c "exec /action/helper.sh"', 'bash', False),
                ('bash -c "env /action/helper.sh"', 'sh', False),
                ('sh -c /action/helper.sh', 'sh', False),
                ('bash -c /action/outer.sh', 'bash', True)]:
            with self.subTest(script=script):
                self.compare_helper_fallback(script, dialect, nested=nested)

    def test_helper_fallback_keeps_explicit_interpreter_and_shebang_precedence(self):
        for script, dialect, shebang in [
                ('bash -c "sh /action/helper.sh"', 'sh', '#!/bin/bash\n'),
                ('sh -c "bash /action/helper.sh"', 'bash', '#!/bin/sh\n'),
                ('bash -c /action/helper.sh', 'sh', '#!/bin/sh\n'),
                ('sh -c /action/helper.sh', 'bash', '#!/bin/bash\n')]:
            with self.subTest(script=script):
                self.compare_helper_fallback(script, dialect, shebang)


class GoJumpTests(unittest.TestCase):
    scan = test_shell_execution.PluginScopeTests.scan

    def test_forward_goto_skips_dead_return_break_and_assignments(self):
        self.assertTrue(self.scan('func f(){goto live;return;live:p.Open("node")}'))
        self.assertTrue(self.scan('func f(){switch{default:goto live;break;live:p.Open("node")}}'))
        for initial, skipped in [('safe', 'p.Open'), ('p.Open', 'safe')]:
            code = 'func f(){open:=' + initial + ';goto live;open=' + skipped + ';live:open("node")}'
            self.assertEqual(bool(self.scan(code)), initial == 'p.Open', code)
        self.assertTrue(self.scan('func f(){name:="node";goto live;name="safe";live:p.Open(name)}'))
        self.assertEqual(self.scan('func f(){name:="safe";goto live;name="node";live:p.Open(name)}'), [])

    def test_conditional_incoming_jumps_keep_live_labels_reachable(self):
        for condition in ['true', 'unknown', '1==1']:
            self.assertTrue(self.scan('func f(){if ' + condition + '{goto live};return;live:p.Open("node")}'))
            self.assertTrue(self.scan('func f(){switch{default:if ' + condition + '{goto live};break;live:p.Open("node")}}'))
        self.assertEqual(self.scan('func f(){if false{goto live};return;live:p.Open("node")}'), [])
        self.assertEqual(self.scan('func f(){switch{default:if false{goto live};break;live:p.Open("node")}}'), [])
        self.assertTrue(self.scan('func f(){goto live;live:p.Open("node");return;p.Open("node")}'))

    def test_jump_targets_respect_function_block_and_case_scopes(self):
        self.assertTrue(self.scan('func f(){{goto live};return;live:p.Open("node")}'))
        self.assertTrue(self.scan('func f(){goto live;_=struct{live int}{live:1};live:p.Open("node")}'))
        self.assertTrue(self.scan('func f(){switch value{case live:goto live};return;live:p.Open("node")}'))
        self.assertEqual(self.scan('func f(){goto live;live:return;p.Open("node")}'), [])
        self.assertTrue(self.scan('func f(){goto live;live:return;p.Open("node")}\nfunc g(){p.Open("node")}'))
        self.assertTrue(self.scan('func f(){fn:=func(){goto live;live:return};_=fn;p.Open("node")}'))

    def test_nested_forward_jumps_preserve_outer_writes_and_shadow_restoration(self):
        self.assertTrue(self.scan('func f(){open:=safe;{open=p.Open;goto live;open=safe};live:open("node")}'))
        self.assertEqual(self.scan('func f(){open:=safe;{open:=p.Open;_=open;goto live};live:open("node")}'), [])
        self.assertTrue(self.scan('func f(){goto first;return;first:goto second;return;second:p.Open("node")}'))
        self.assertEqual(self.scan('func f(){open:=safe;if true{goto live}else{open=p.Open};live:open("node")}'), [])

    def test_labelled_statements_and_nested_function_namespaces(self):
        for code in [
                'func f(){goto live;return;live:{p.Open("node")}}',
                'func f(){goto live;return;live:if true{p.Open("node")}}',
                'func f(){switch value{case\nlive:goto live};return;live:p.Open("node")}',
                'func f(){fn:=func(){goto live;live:return};_=fn;goto live;return;live:p.Open("node")}',
                'func f(){switch{default:select{default:goto live};return};live:p.Open("node")}',
                'func f()(*p.Plugin,error){goto live;return nil,nil;live:return p.Open("node")}']:
            with self.subTest(code=code):
                self.assertTrue(self.scan(code))


if __name__ == '__main__':
    unittest.main()
