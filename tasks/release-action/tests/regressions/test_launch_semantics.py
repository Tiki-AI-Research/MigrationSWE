"""Shell field/status oracles and definite Go branch binding regressions."""
import itertools
import os
import shutil
import subprocess
import shlex
import unittest

from regressions import test_shell_bindings, test_shell_execution
from launch import inspect_action


class ShellLaunchSemanticsTests(unittest.TestCase):
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    inspect = test_shell_execution.ShellExecutionTests.inspect
    compare_shell = test_shell_bindings.ShellBindingTests.compare_shell

    def test_at_concatenations_select_the_real_executable(self):
        for expression in ['"$@"\'\'', '\'\'"$@"', '"$@"""', '"""$@"', '"${@}"\'\'']:
            for arguments in ['node /action/dist/index.js', '/action/release-action value']:
                with self.subTest(expression=expression, arguments=arguments):
                    self.compare_shell('set -- ' + arguments + '\n' + expression + '\n/action/release-action')

    def test_at_prefix_suffix_empty_and_repeated_fields_follow_shell(self):
        expressions = ['"$@"', '$@', '"$@"\'\'', '\'\'"$@"', 'pre"$@"', '"$@"post',
                       'pre"$@"post', '"$@""$@"', '"$@"x"$@"', '"$@"$EMPTY', '$EMPTY"$@"']
        arguments = [[], [''], ['', ''], ['node'], ['true', 'node'],
                     ['true node', 'node', ''], ['node:true', ':node:', '']]
        for separator, args, expression in itertools.product([' ', '', ':', ' :'], arguments, expressions):
            text = ('EMPTY=\'\'; IFS=' + shlex.quote(separator) + '\nset -- ' + shlex.join(args) +
                    '\nfor item in ' + expression + '; do case "$item" in node) node;; esac; done\n/action/release-action')
            with self.subTest(separator=separator, args=args, expression=expression):
                self.compare_shell(text)

    def test_at_concatenations_preserve_argument_count_and_edge_text(self):
        for expression, count, first, second in [
            ('"$@"\'\'', '2', 'first', 'last'),
            ('pre"$@"post', '2', 'prefirst', 'lastpost'),
            ('"$@""$@"', '3', 'first', 'lastfirst'),
        ]:
            text = ('verify(){ if test "$#" = ' + count + ' && test "$1" = ' + first +
                    ' && test "$2" = ' + second + '; then /action/release-action; else node; fi; }\n'
                    'set -- first last\nverify ' + expression + '\n/action/release-action')
            with self.subTest(expression=expression):
                self.compare_shell(text)

    def test_unknown_at_argument_does_not_taint_known_command_fields(self):
        for command in ['node', '/action/release-action']:
            for expression in ['"$@"', '"$@"\'\'', '\'\'"$@"']:
                inspected = self.inspect('set -- ' + command + ' "$ENV_UNKNOWN"\n' + expression + '\n/action/release-action')
                self.assertTrue(inspected['go'], inspected)
                self.assertEqual(bool(inspected['source_runtime_hits']), command == 'node', inspected)

    def test_shift_default_zero_explicit_and_all_parameters(self):
        for setup in ['set -- true node', 'set -- true true node', 'set -- node', 'set --']:
            size = len(shlex.split(setup)) - 2
            for count in range(size + 1):
                for wrapper in ['shift', 'command shift', "eval 'shift COUNT'"]:
                    command = wrapper.replace('COUNT', str(count)) if wrapper.startswith('eval') else wrapper + ' ' + str(count)
                    with self.subTest(setup=setup, count=count, wrapper=wrapper):
                        self.compare_shell(setup + '\n' + command + '\n"$@"\n/action/release-action')
        self.compare_shell('set -- true node\nshift\n"$@"\n/action/release-action')
        self.compare_shell('set -- node /action/release-action\nshift\nexec "$@"')
        self.compare_shell('set -- true node\nshift 0001\n"$@"\n/action/release-action')

    def test_shift_function_subshell_and_substitution_scopes(self):
        for operation in ['shift', 'command shift', "eval 'shift'"]:
            for text in [
                'set -- node\nrun(){ OP; "$@"; }\nrun true node\n"$@"\n/action/release-action',
                'set -- node\nrun(){ OP; }\nrun true node\n"$@"\n/action/release-action',
                'set -- node\n(OP)\n"$@"\n/action/release-action',
                'set -- node\nVALUE="$(OP)"\n"$@"\n/action/release-action',
            ]:
                with self.subTest(operation=operation, text=text):
                    self.compare_shell(text.replace('OP', operation))

    def test_shift_unknown_invalid_and_out_of_range_fail_closed(self):
        for operand in ['$UNRESOLVED', '-1', 'invalid', '3', '1 2', '9' * 5000]:
            inspected = self.inspect('/action/release-action\nset -- true node\nshift ' + operand + '\n"$@"')
            self.assertFalse(inspected['go'], inspected)
            self.assertTrue(inspected['unresolved'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [], inspected)

    def test_eval_inherits_status_until_a_command_changes_it(self):
        for previous, expression, branch in itertools.product(
                ['true', 'false'], ['eval return', 'eval \'eval return\'', 'command eval return',
                                   'eval \'return "$?"\'', 'eval \'\'; return', 'eval; return'],
                ['then node; else :', 'then :; else node']):
            text = 'run(){ ' + previous + '; ' + expression + '; }\nif run; ' + branch + '; fi\n/action/release-action'
            with self.subTest(text=text):
                self.compare_shell(text)

    def test_command_substitutions_inherit_status_and_isolate_control(self):
        for previous, expression, branch in itertools.product(
                ['true', 'false'], ['VALUE="$(return)"', 'VALUE="$(exit)"',
                                   'VALUE="$(eval return)"', 'VALUE="$(VALUE=\"$(return)\")"'],
                ['then node; else :', 'then :; else node']):
            text = 'run(){ ' + previous + '; ' + expression + '; }\nif run; ' + branch + '; fi\n/action/release-action'
            with self.subTest(text=text):
                self.compare_shell(text)

    def test_last_status_is_read_before_argument_and_assignment_commands(self):
        for previous, condition in itertools.product(['true', 'false'], [
                'test "$?" = 0', 'test "$?" != 0', 'test "$?" = "$?"',
                'test "${?}" = 1', 'VALUE="$?"; test "$VALUE" = 1',
                'VALUE="$(printf \'%s\' "$?")"; test "$VALUE" = 1',
                'eval \'test "$?" = 1\'']):
            text = previous + '\nif ' + condition + '; then node; fi\n/action/release-action'
            with self.subTest(text=text):
                self.compare_shell(text)

    def test_numeric_status_survives_functions_and_process_boundaries(self):
        for code in [0, 1, 2, 7, 255, 256, 257, 512]:
            for invocation, expected in [('run', code), ('(run)', code % 256),
                                         ('VALUE="$(run)"', code % 256), ('true | run', code % 256)]:
                text = ('run(){ return ' + str(code) + '; }\n' + invocation +
                        '\nif test "$?" = ' + str(expected) + '; then node; fi\n/action/release-action')
                with self.subTest(text=text):
                    self.compare_shell(text)

    def test_pipeline_children_inherit_the_status_before_the_pipeline(self):
        for previous, pipeline in itertools.product(['true', 'false'], ['true | run', 'false | run', 'run | run']):
            self.compare_shell('run(){ return; }\n' + previous + '\nif ' + pipeline + '; then node; fi\n/action/release-action')


class GoDefiniteBranchTests(unittest.TestCase):
    scan = test_shell_execution.PluginScopeTests.scan

    def test_integer_literal_conditions_preserve_the_selected_assignment(self):
        conditions = [('1==1', True), ('1!=1', False), ('1<2', True), ('2<=1', False),
                      ('2>1', True), ('2>=3', False), ('0x10==16', True), ('010==8', True),
                      ('0b10==2', True), ('0o10==8', True), ('1_000==1000', True),
                      ('-1<0', True), ('(+1)==1', True), ('!(1==1)', False),
                      ('(1<2)&&(2<3)', True), ('(1>2)||(2<3)', True),
                      ('true==false', False)]
        for condition, active in conditions:
            for initial, assigned in [('safe', 'p.Open'), ('p.Open', 'safe')]:
                code = 'func f(){open:=' + initial + ';if ' + condition + '{open=' + assigned + '};open("node")}'
                with self.subTest(code=code):
                    self.assertEqual(bool(self.scan(code)), (assigned if active else initial) == 'p.Open')
                code = 'func f(){open:=safe;if ' + condition + '{open=p.Open}else{open=safe};open("node")}'
                self.assertEqual(bool(self.scan(code)), active, code)

    def test_literal_conditions_keep_nested_header_and_shadow_bindings(self):
        self.assertTrue(self.scan('func f(){open:=safe;if other:=0;1==1{{open=p.Open};_=other};open("node")}'))
        self.assertEqual(self.scan('func f(){open:=p.Open;if open:=safe;1==1{_=open};open=safe;open("node")}'), [])
        self.assertTrue(self.scan('func f(){open:=p.Open;if 1!=1{open=safe}else if 2>1{open=p.Open};open("node")}'))
        self.assertEqual(self.scan('func f(){open:=safe;if 1==1{if 2>1{open=p.Open};open=safe};open("node")}'), [])
        self.assertEqual(self.scan('func f(){if 1!=1{p.Open("node")}else{if 2>3{p.Open("node")}}}'), [])

    def test_default_only_switches_preserve_outer_callable_and_string_writes(self):
        for header in ['switch', 'switch value', 'switch x:=value.(type)', 'switch x:=value; x']:
            for initial, assigned in [('safe', 'p.Open'), ('p.Open', 'safe')]:
                body = '{default:open=' + assigned + '}'
                code = 'func f(value interface{}){open:=' + initial + ';' + header + body + ';open("node")}'
                with self.subTest(code=code):
                    self.assertEqual(bool(self.scan(code)), assigned == 'p.Open')
            self.assertTrue(self.scan('func f(value interface{}){name:="safe";' + header + '{default:name="node"};p.Open(name)}'))
            self.assertEqual(self.scan('func f(value interface{}){name:="node";' + header + '{default:name="safe"};p.Open(name)}'), [])

    def test_default_only_switches_restore_local_declarations_and_import_aliases(self):
        for body in ['switch x:=value.(type){default:open:=safe;_=open;_=x}',
                     'switch p:=value.(type){default:p.Open("node")}',
                     'switch{default:var open=safe;_=open}']:
            with self.subTest(body=body):
                self.assertTrue(self.scan('func f(value interface{}){open:=p.Open;' + body + ';open("node")}'))
        self.assertEqual(self.scan('func f(value interface{}){open:=safe;switch x:=value.(type){default:{open=p.Open};switch{default:open=safe};_=x};open("node")}'), [])
        self.assertTrue(self.scan('func f(value interface{}){switch p:=value.(type){default:_=p};p.Open("node")}'))

    def test_switch_case_keywords_inside_nested_constructs_are_not_outer_cases(self):
        self.assertTrue(self.scan('func f(value interface{}){open:=safe;switch x:=value.(type){default:switch x{case 1:};open=p.Open};open("node")}'))
        self.assertEqual(self.scan('func f(value interface{}){open:=p.Open;switch x:=value.(type){default:switch x{case 1:};open=safe};open("node")}'), [])
        self.assertTrue(self.scan('func f(value interface{}){switch p:=value.(type){case Local:p.Open("node");default:p.Open("node")};p.Open("node")}'))

    def test_default_switch_break_keeps_prior_writes_and_ignores_dead_writes(self):
        for initial, assigned in [('safe', 'p.Open'), ('p.Open', 'safe')]:
            for body in ['open=TARGET;break;open=INITIAL', '{open=TARGET;break};open=INITIAL',
                         'if true{open=TARGET;break};open=INITIAL']:
                body = body.replace('TARGET', assigned).replace('INITIAL', initial)
                code = 'func f(){open:=' + initial + ';switch{default:' + body + '};open("node")}'
                with self.subTest(code=code):
                    self.assertEqual(bool(self.scan(code)), assigned == 'p.Open')
        self.assertEqual(self.scan('func f(){switch{default:break;p.Open("node")}}'), [])
        self.assertTrue(self.scan('func f(){switch{default:p.Open("node");break}}'))
        self.assertTrue(self.scan('func f(){open:=safe;switch{default:if false{break};open=p.Open};open("node")}'))

    def test_select_break_does_not_stop_the_enclosing_switch(self):
        for initial, assigned in [('safe', 'p.Open'), ('p.Open', 'safe')]:
            code = ('func f(){open:=' + initial + ';switch{default:select{default:break};open=' +
                    assigned + '};open("node")}')
            self.assertEqual(bool(self.scan(code)), assigned == 'p.Open', code)

    def test_definite_return_stops_dead_calls_after_evaluating_return_values(self):
        for control in ['switch{default:BODY}', 'if 1==1{BODY}', 'if true{BODY}']:
            for body in ['return;open=p.Open', 'return\nopen=p.Open']:
                code = 'func f(){open:=safe;' + control.replace('BODY', body) + ';open("node")}'
                self.assertEqual(self.scan(code), [], code)
            self.assertTrue(self.scan('func f()error{' + control.replace('BODY', 'return p.Open("node")') + ';return nil}'))
        self.assertTrue(self.scan('func f(){open:=p.Open;if false{return};open("node")}'))
        self.assertTrue(self.scan('func f(){open:=p.Open;if unknown{return};open("node")}'))
        self.assertTrue(self.scan('func f(){fn:=func(){return};_=fn;p.Open("node")}'))
        self.assertTrue(self.scan('func f(){switch{default:select{case <-channel:return;default:};p.Open("node")}}'))
        self.assertEqual(self.scan('func f(){switch{default:select{default:return};p.Open("node")}}'), [])


class ShellOptionTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown

    def compare(self, text, *, dialects=('bash', 'ash'), returncode=0, launch=None, shell_flags=(), exact_trace=True):
        """Only authored scripts execute; Go/Node are successful trace stubs."""
        for dialect in dialects:
            with self.subTest(text=text, dialect=dialect, launch=launch):
                shell = [shutil.which('busybox'), 'ash'] if dialect == 'ash' else [shutil.which(dialect)]
                if not shell[0]:
                    self.skipTest('Shell oracle unavailable: ' + dialect)
                entry = self.root / 'entry.sh'
                entry.write_text('#!/bin/' + dialect + ' ' + ' '.join(shell_flags) + '\n' + text + '\n')
                docker = self.root / 'Dockerfile'
                original = docker.read_text()
                if launch is not None:
                    import json
                    words = [dialect if x == 'SHELL' else x for x in launch]
                    docker.write_text(original.replace('["/action/entry.sh"]', json.dumps(words)))
                inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
                docker.write_text(original)
                stubs = self.root / 'oracle-bin'
                stubs.mkdir(exist_ok=True)
                log = self.root / 'oracle.log'
                log.write_text('')
                for file, marker in [(stubs / 'node', 'NODE'), (self.root / 'release-action', 'GO')]:
                    file.write_text('#!/bin/sh\nprintf "' + marker + '\\n" >> "$AUDIT_TRACE"\n')
                    file.chmod(0o755)
                env = {'PATH': str(stubs) + os.pathsep + os.defpath, 'AUDIT_TRACE': str(log)}
                script = text.replace('/action', str(self.root))
                args = list(shell_flags) + ['-c', script]
                if launch is not None:
                    args = [x.replace('/action', str(self.root)) for x in launch[1:]]
                    if '-c' not in args and not any('c' in x[1:] for x in args if x.startswith('-') and not x.startswith('--')):
                        entry.write_text(script)
                try:
                    run = subprocess.run(shell + args, env=env, capture_output=True, text=True, timeout=3)
                    if returncode is not None:
                        self.assertEqual(run.returncode, returncode, run.stderr)
                    else:
                        self.assertIn(run.returncode, {0, 1}, run.stderr)
                    trace = log.read_text().splitlines()
                    type(self).oracle_results.append({'script': text, 'dialect': dialect,
                        'launch': launch, 'shell_flags': list(shell_flags), 'returncode': run.returncode,
                        'trace': trace, 'inspected': inspected})
                    if exact_trace:
                        self.assertEqual(len(inspected['source_runtime_hits']), trace.count('NODE'), (inspected, trace))
                    else:
                        self.assertEqual(bool(inspected['source_runtime_hits']), 'NODE' in trace, (inspected, trace))
                    self.assertEqual(inspected['go'], 'GO' in trace, (inspected, trace))
                finally:
                    (self.root / 'release-action').unlink()

    def test_reported_six_counterexamples_and_seven_controls(self):
        for text in [
            'set -o pipefail\nif false | true; then :; else node; fi\nexec /action/release-action',
            'set -o pipefail\nif false | true; then node; fi\nexec /action/release-action',
            "trap 'node' EXIT\n/action/release-action",
            "trap '/action/release-action' EXIT\n:",
            '(set -e; false; node)\nexec /action/release-action',
            'set -e\nif false; then node; fi\nexec /action/release-action',
            'set -e\nset +e\nfalse\nnode\nexec /action/release-action',
            'if false | true; then node; fi\nexec /action/release-action',
            'if false | true; then :; else node; fi\nexec /action/release-action',
            "trap 'node' EXIT\nexec /action/release-action",
            "trap 'node' EXIT\ntrap - EXIT\nexec /action/release-action",
            "trap 'printf \"%s\\n\" \"node\"' EXIT\n/action/release-action",
        ]:
            self.compare(text)
        self.compare('set -e\nfalse\nexec /action/release-action', returncode=1)

    def test_pipefail_toggles_combined_options_and_last_failure_status(self):
        for setup, commands, branch in itertools.product(
                ['set -o pipefail', 'set -eo pipefail', 'set -o pipefail;set +o pipefail'],
                ['false | true', 'true | false', 'true | true', 'false | false | true'],
                ['then node;else :', 'then :;else node']):
            self.compare(setup + '\nif ' + commands + ';' + branch + ';fi\n/action/release-action')
        for setup, expected in [('set -o pipefail', '7'), ('set -o pipefail;set +o pipefail', '0')]:
            self.compare('first(){ return 3; };last(){ return 7; };' + setup +
                         '\nfirst | last | true\nif test "$?" = ' + expected + ';then node;fi\n/action/release-action')
        for query in ['set -o', 'set +o', 'set -eo', 'set +eo']:
            self.compare(query + '\n/action/release-action')

    def test_errexit_exempt_conditions_functions_and_lists(self):
        for condition in ['false', 'true && false', 'false || false', '! true',
                          '{ false;node; }', 'run', '(false;node)', 'eval "false;node"']:
            self.compare('set -e\nrun(){ false;node; }\nif ' + condition + ';then :;fi\n/action/release-action')
        for text in ['false && node', '! true', 'while false;do node;done', 'until true;do node;done',
                     'run(){ false;node; };run || true', 'run(){ false;node; };! run',
                     '{ false && node; }', 'run(){ set -e;false;node; };run || true']:
            self.compare('set -e\n' + text + '\n/action/release-action')
        for text in ['true && false', 'true || false;false', 'run(){ false;node; };run',
                     'run(){ return 1; };run', '(false && true)']:
            self.compare('set -e\n' + text + '\nnode\n/action/release-action', returncode=1)

    def test_errexit_scopes_and_pipeline_command_exemptions(self):
        for text in ['(set -e;false;node)',
                     'set -e\nfalse | true', 'set -e\n{ false;node; } | true',
                     '(set -e;true | { false;node; })',
                     'run(){ set +e;false; };set -e;run',
                     '(set -e);false']:
            self.compare(text + '\n/action/release-action')
        self.compare('set -e\ntrue | false\nnode\n/action/release-action', returncode=1)
        self.compare('set -e\n(false;node)\n/action/release-action', returncode=1)
        self.compare('set -eo pipefail\nfalse | true\nnode\n/action/release-action', returncode=1)

    def test_command_substitution_errexit_follows_interpreter(self):
        self.compare('set -e\nVALUE="$(false;node)"\n/action/release-action', dialects=('bash',))
        self.compare('set -e\nVALUE="$(false;node)"\n/action/release-action', dialects=('ash',))
        self.compare('set -e\nVALUE="$(false;node)"\n/action/release-action', dialects=('dash',), returncode=1)
        self.compare('set -e\nif VALUE="$(false;node)";then :;fi\n/action/release-action')
        self.compare('set -eo posix\nVALUE="$(false;node)"\n/action/release-action', dialects=('bash',), returncode=1)

    def test_exit_traps_run_once_at_their_own_process_boundary(self):
        for text in [
            "trap 'node' EXIT\n(/action/release-action)\n:",
            "trap 'node' EXIT\nVALUE=\"$(true)\"\n/action/release-action",
            "trap 'node' EXIT\ntrue | true\n/action/release-action",
            "trap 'node' EXIT\nrun(){ /action/release-action;return; };run",
            "(trap 'node' EXIT;true)\n/action/release-action",
            "(trap 'node' EXIT;exec /action/release-action)",
            "VALUE=\"$(trap 'node' EXIT;true)\"\n/action/release-action",
            "(trap 'node' EXIT;true) | true\n/action/release-action",
            "trap 'node' EXIT\nrun(){ trap - EXIT; };run\n/action/release-action",
            "trap 'exec /action/release-action' EXIT\n:",
        ]:
            self.compare(text)

    def test_trap_replacement_reset_quoting_and_multiple_signals(self):
        for text in [
            "trap 'node' EXIT\ntrap ':' EXIT\n/action/release-action",
            "trap ':' EXIT\ntrap 'node' EXIT\n/action/release-action",
            "trap 'node' EXIT\ntrap -- - 0\n/action/release-action",
            "trap 'node' EXIT\ntrap 0\n/action/release-action",
            "trap 'node' EXIT\ntrap '' EXIT\n/action/release-action",
            "RUNTIME=true\ntrap '$RUNTIME' EXIT\nRUNTIME=node\n/action/release-action",
            'RUNTIME=true\ntrap "$RUNTIME" EXIT\nRUNTIME=node\n/action/release-action',
            "trap 'node' TERM INT\n/action/release-action",
            "trap -- 'node' EXIT TERM\n/action/release-action",
            'trap\n/action/release-action',
            "trap 'node' EXIT\ntrap\n/action/release-action",
        ]:
            self.compare(text)
        self.compare('trap -p\n/action/release-action', dialects=('bash',))
        self.compare("trap 'node' EXIT\ntrap -p EXIT\n/action/release-action", dialects=('bash',))

    def test_exit_trap_preserves_status_unless_it_exits(self):
        for handler, code in itertools.product(['false', 'true', 'exit 0', 'exit 7',
                'if test "$?" = 3;then node;fi'], [0, 3]):
            text = ('if (trap ' + shlex.quote(handler) + ' EXIT;exit ' + str(code) +
                    ');then node;fi\n/action/release-action')
            self.compare(text)
        self.compare("trap '/action/release-action' EXIT\nset -e\nfalse\nnode", returncode=1)
        self.compare("trap 'node' EXIT\nexit 0")

    def test_interpreter_options_apply_to_script_and_command_string(self):
        for launch in [['SHELL', '-e', '/action/entry.sh'], ['SHELL', '-o', 'errexit', '/action/entry.sh'],
                       ['SHELL', '-e', '-c', 'false; /action/release-action']]:
            self.compare('false\n/action/release-action', launch=launch, returncode=1)
        self.compare('if false | true;then node;fi\n/action/release-action',
                     launch=['SHELL', '-o', 'pipefail', '/action/entry.sh'])

    def test_noexec_and_unknown_control_options_do_not_fabricate_launches(self):
        self.compare('set -n\nnode\n/action/release-action')
        self.compare('node\n/action/release-action', launch=['SHELL', '-n', '/action/entry.sh'])
        for text in ["trap 'node' DEBUG\n/action/release-action", "trap 'node' ERR\n/action/release-action",
                     "trap '$UNRESOLVED' EXIT\n/action/release-action", 'set -o unknown\n/action/release-action']:
            (self.root / 'entry.sh').write_text('#!/bin/bash\n' + text)
            inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(inspected['go'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [], inspected)
            self.assertTrue(inspected['unresolved'], inspected)

    def test_shebang_options_and_default_sh_implementation(self):
        self.compare('false\nnode\n/action/release-action', shell_flags=('-e',), returncode=1)
        self.compare('if false | true;then node;fi\n/action/release-action', shell_flags=('-eu',))
        # /bin/sh in the fixture's Alpine image is BusyBox ash. The host sh
        # is dash; use ash as the independent execution oracle for this case.
        text = 'set -e\nVALUE="$(false;node)"\n/action/release-action'
        (self.root / 'entry.sh').write_text('#!/bin/sh\n' + text)
        inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
        self.assertTrue(inspected['go'], inspected)
        self.assertEqual(len(inspected['source_runtime_hits']), 1, inspected)
        self.compare(text, dialects=('ash',))

    def test_composite_shell_defaults_and_explicit_options(self):
        for selector, launches in [('bash', False), ('bash {0}', True), ('bash -e {0}', False),
                                   ('sh', False), ('sh {0}', True)]:
            action = ('runs:\n  using: composite\n  steps:\n    - shell: ' + selector +
                      '\n      run: |\n        false\n        go run .\n')
            inspected = inspect_action(self.root, action)
            self.assertEqual(inspected['go'], launches, (selector, inspected))
        for selector, node in [('bash', False), ('bash {0}', True), ('bash -o pipefail {0}', False)]:
            action = ('runs:\n  using: composite\n  steps:\n    - shell: ' + selector +
                      '\n      run: |\n        if false | true;then node;fi\n        go run .\n')
            inspected = inspect_action(self.root, action)
            self.assertTrue(inspected['go'], inspected)
            self.assertEqual(bool(inspected['source_runtime_hits']), node, (selector, inspected))

    def test_option_context_matrix_matches_real_interpreters(self):
        contexts = ['run', 'if run;then :;fi', 'run && :', 'run || :', '! run',
                    'while run;do break;done', 'until run;do break;done', 'run | true',
                    'true | run', 'if run | true;then :;fi', '(run)', 'VALUE="$(run)"',
                    '{ run; }', "eval 'run'"]
        for setup, body, context in itertools.product(
                ['', 'set -e', 'set -o pipefail', 'set -eo pipefail'], ['false', 'false;node'], contexts):
            self.compare('run(){ ' + body + '; }\n' + setup + '\n' + context + '\n/action/release-action',
                         returncode=None, exact_trace=False)

    def test_nested_interpreter_exit_status_reaches_the_caller(self):
        helper = self.root / 'helper.sh'
        for header, body, call in itertools.product(
                ['#!/bin/bash\n', ''], ['false', "trap 'exit 0' EXIT;false"],
                ['/action/helper.sh', 'bash /action/helper.sh', 'env /action/helper.sh']):
            helper.write_text(header + body + '\n')
            helper.chmod(0o755)
            self.compare('set -e\n' + call + '\nnode\n/action/release-action',
                         returncode=1 if body == 'false' else 0)
            self.compare('if ' + call + ';then node;fi\n/action/release-action')
        for dialect in ['bash', 'dash']:
            self.compare('set -e\n' + dialect + " -c 'exit 3'\nnode\n/action/release-action", returncode=3)


class NounsetChildTrapTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    compare = ShellOptionTests.compare

    def fails_expansion(self, text, *, dialects=('bash', 'ash', 'dash')):
        for dialect in dialects:
            self.compare(text, dialects=(dialect,), returncode=127 if dialect == 'bash' else 2)

    def test_reported_four_counterexamples_and_five_controls(self):
        self.fails_expansion('unset AUDIT_UNSET_VALUE;set -u;: "$AUDIT_UNSET_VALUE";exec /action/release-action')
        self.compare('unset AUDIT_UNSET_VALUE;set -u;(: "$AUDIT_UNSET_VALUE";node);exec /action/release-action',
                     dialects=('bash', 'ash', 'dash'))
        for text in ["trap 'node' CHLD;(/action/release-action);:",
                     "trap '/action/release-action' CHLD;(:);:",
                     'set -u;set +u;: "$AUDIT_UNSET_VALUE";exec /action/release-action',
                     'set -u;AUDIT_UNSET_VALUE=known;: "$AUDIT_UNSET_VALUE";exec /action/release-action',
                     'set -u;: "${AUDIT_UNSET_VALUE:-fallback}";exec /action/release-action',
                     "trap 'node' CHLD;trap - CHLD;(/action/release-action);:",
                     "trap '' CHLD;(/action/release-action);:"]:
            self.compare(text, dialects=('bash',))

    def test_nounset_options_apply_to_scripts_strings_and_shebangs(self):
        text = 'unset MISSING;: "$MISSING";node;/action/release-action'
        for dialect in ('bash', 'ash', 'dash'):
            for option in ('-u', '-o nounset'):
                words = option.split()
                self.compare(text, dialects=(dialect,), launch=['SHELL'] + words + ['/action/entry.sh'],
                             returncode=1 if dialect == 'bash' else 2)
            self.compare('unset MISSING;set -u;set +o nounset;: "$MISSING";/action/release-action', dialects=(dialect,))
        self.compare(text, dialects=('bash',), shell_flags=('-u',), returncode=127)

    def test_nounset_stops_later_words_assignments_and_control_headers(self):
        for text in [': "$MISSING" "$(node)"', 'A="$MISSING" B="$(node)"',
                     'for value in "$MISSING" "$(node)";do node;done',
                     'case "$MISSING" in *) node;;esac',
                     'if : "$MISSING";then node;fi', '! : "$MISSING"',
                     ': "${MISSING:-$OTHER}" "$(node)"']:
            self.fails_expansion('unset MISSING OTHER;set -u;' + text + ';/action/release-action')

    def test_nounset_parameter_defaults_assignments_and_empty_vectors(self):
        for text in ['unset VALUE;: "${VALUE:-ok}"', 'unset VALUE;: "${VALUE-ok}"',
                     'unset VALUE;: "${VALUE+$(node)}"', 'unset VALUE;: "${VALUE:+$(node)}"',
                     'unset VALUE;: "${VALUE:=ok}";: "$VALUE"',
                     'unset VALUE;: "${VALUE=ok}";: "$VALUE"',
                     'VALUE=;: "$VALUE" "${VALUE-ok}"', 'VALUE=abc;: "${#VALUE}"',
                     'set --;: "$@" "$*" "${#@}" "${#*}"',
                     'VALUE=ok;: "${VALUE?$(node)}" "${VALUE:?$(node)}"']:
            self.compare('set -u;' + text + ';/action/release-action', dialects=('bash', 'ash', 'dash'))
        self.fails_expansion('unset VALUE;set -u;: "${#VALUE}";node;/action/release-action')
        for operator in ('?', ':?'):
            self.fails_expansion('unset VALUE;: "${VALUE' + operator + '$(node)}";/action/release-action')

    def test_nounset_positional_parameters_and_function_process_scopes(self):
        for text in ['set -- one;: "$2"', 'run(){ : "$2";node; };run one',
                     'run(){ : "$2";node; };if run one;then :;fi',
                     'run(){ local MISSING;: "$MISSING";node; };run']:
            self.fails_expansion('unset MISSING;set -u;' + text + ';/action/release-action')
        for text in ['set -- one;(: "$2";node)',
                     'unset VALUE;VALUE="$( : "$VALUE";node )"',
                     'unset VALUE;if (: "$VALUE";node);then node;fi']:
            self.compare('set -u;' + text + ';/action/release-action', dialects=('bash', 'ash', 'dash'))

    def test_nounset_exit_handlers_and_child_status(self):
        for dialect in ('bash', 'ash', 'dash'):
            self.fails_expansion("unset VALUE;set -u;trap 'node' EXIT;: \"$VALUE\";/action/release-action", dialects=(dialect,))
            expected = '1' if dialect == 'bash' else '2'
            self.compare('unset VALUE;set -u;(: "$VALUE");if test "$?" = ' + expected + ';then node;fi;/action/release-action', dialects=(dialect,))
            self.compare('unset VALUE;set -u;VALUE="$( : "$VALUE" )";if test "$?" = ' + expected + ';then node;fi;/action/release-action', dialects=(dialect,))

    def test_nounset_unknown_environment_does_not_invent_execution(self):
        for text in ['set -u;: "$UNKNOWN_ENVIRONMENT_VALUE";node;/action/release-action',
                     'set -u;: "${UNKNOWN_ENVIRONMENT_VALUE:1}";node;/action/release-action']:
            (self.root / 'entry.sh').write_text('#!/bin/bash\n' + text)
            inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(inspected['go'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [], inspected)
            self.assertTrue(inspected['unresolved'], inspected)

    def test_chld_builtins_functions_and_exec_do_not_spawn_children(self):
        for text in ['true;false;:;printf "";echo quiet', 'run(){ :; };run;eval ":"',
                     'trap -p CHLD', 'trap', 'exec /action/release-action']:
            self.compare("trap 'node' CHLD;" + text + ';/action/release-action', dialects=('bash',))

    def test_chld_external_commands_substitutions_and_pipelines(self):
        for text in ['/action/release-action', 'bash -c ":"', 'VALUE="$(true)"',
                     'true | true', 'true | true | true', '(true | true)',
                     'run(){ /action/release-action; };run', '(true)',
                     '( : "$(true)" )']:
            self.compare("trap 'node' CHLD;" + text + ';/action/release-action', dialects=('bash',))

    def test_chld_handlers_run_after_assignments_and_argument_expansion(self):
        for text in [
            "R=true;trap 'R=node' CHLD;: \"$(true)\" \"$R\";$R;/action/release-action",
            "R=true;trap 'R=node' CHLD;R=$(printf value);$R;/action/release-action",
            "trap 'if test \"$?\" = 3;then node;fi' CHLD;(exit 3);/action/release-action",
            "trap 'false' CHLD;(:);if test \"$?\" = 0;then node;fi;/action/release-action",
        ]:
            self.compare(text, dialects=('bash',), exact_trace=False)

    def test_chld_replacement_reset_aliases_and_queued_pipeline_handlers(self):
        for signal in ('CHLD', 'SIGCHLD', '17'):
            for operation in ["trap 'node' " + signal,
                              "trap 'node' " + signal + ';trap - ' + signal,
                              "trap 'node' " + signal + ";trap '' " + signal,
                              "trap 'node' " + signal + ";trap ':' " + signal]:
                self.compare(operation + ';(:);/action/release-action', dialects=('bash',))
        self.compare("trap 'node;trap - CHLD' CHLD;true | true;/action/release-action", dialects=('bash',))

    def test_chld_caught_handlers_reset_in_children_and_do_not_recurse(self):
        for handler in ['node;(:)', 'node;VALUE="$(true)"', 'node;/action/release-action']:
            self.compare('trap ' + shlex.quote(handler) + ' CHLD;(:);exec /action/release-action', dialects=('bash',))
        self.compare("trap 'node' CHLD;(trap '/action/release-action' CHLD;(:));:", dialects=('bash',))

    def test_chld_handler_control_transfers_and_errexit(self):
        self.compare("trap 'exit 7' CHLD;(:);node;/action/release-action", dialects=('bash',))
        self.compare("trap 'exec /action/release-action' CHLD;(:);node", dialects=('bash',))
        self.compare("trap 'return 7' CHLD;run(){ (:);node; };run;exec /action/release-action", dialects=('bash',))
        self.compare("trap 'false' CHLD;set -e;(:);node;/action/release-action", dialects=('bash',))

    def test_unsupported_chld_processes_are_unresolved(self):
        for header, text in [('#!/bin/bash', "trap 'node' CHLD;read VALUE;/action/release-action")]:
            (self.root / 'entry.sh').write_text(header + '\n' + text)
            inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(inspected['go'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [], inspected)
            self.assertTrue(inspected['unresolved'], inspected)

    def test_nounset_arithmetic_and_local_declaration_boundaries(self):
        self.fails_expansion('unset VALUE;set -u;: "$((VALUE))";node;/action/release-action', dialects=('bash',))
        self.compare('unset VALUE;set -u;: "$((VALUE))";node;/action/release-action', dialects=('ash', 'dash'))
        self.fails_expansion('set -u;run(){ local VALUE;: "$VALUE";node; };run;/action/release-action')
        for text in ['set -u;: "$((1+2))"', 'VALUE=3;set -u;: "$((VALUE+1))"',
                     'set -u;run(){ local VALUE=one;local VALUE;: "$VALUE"; };run']:
            self.compare(text + ';/action/release-action', dialects=('bash', 'ash', 'dash'))
        for dialect in ('bash', 'ash'):
            self.fails_expansion('VALUE=outer;set -u;run(){ local VALUE;: "$VALUE"; };run;/action/release-action', dialects=(dialect,))
        self.compare('VALUE=outer;set -u;run(){ local VALUE;: "$VALUE"; };run;/action/release-action', dialects=('dash',))

    def test_chld_control_header_substitutions_run_before_the_body(self):
        for text in ['for value in "$(true)";do $R;done',
                     'case "$(true)" in "") $R;;esac',
                     'case "$(true)" in absent) node;;esac;$R',
                     'for value in $(true);do node;done;$R']:
            self.compare("R=true;trap 'R=node' CHLD;" + text + ';exec /action/release-action', dialects=('bash',))

    def test_non_bash_chld_builtins_clearing_and_exit_status(self):
        for dialect in ('ash', 'dash'):
            for text in ["trap ':' CHLD;(:);/action/release-action",
                         "trap 'R=/action/release-action' CHLD;(:);exec $R",
                         "trap 'trap - CHLD;node' CHLD;(:);/action/release-action",
                         "trap 'trap - CHLD;/action/release-action' CHLD;(:)",
                         "trap 'R=/action/release-action' CHLD;true | true;exec $R"]:
                self.compare(text, dialects=(dialect,))
            self.compare("trap 'exit 7' CHLD;(:);node;/action/release-action", dialects=(dialect,), returncode=7)


class ParameterProcessTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown

    def compare(self, text, *, dialects=('bash', 'ash', 'dash'), returncode=0,
                launch=None, helpers=None, env_updates=None):
        """Compare authored launchers in the same cwd; candidate code never runs."""
        import json
        for dialect in dialects:
            with self.subTest(text=text, dialect=dialect, launch=launch):
                executable = shutil.which('busybox' if dialect == 'ash' else dialect)
                if not executable:
                    self.skipTest('Shell oracle unavailable: ' + dialect)
                shell = [executable, 'ash'] if dialect == 'ash' else [executable]
                if dialect == 'bash':
                    shell += ['--noprofile', '--norc']
                child = 'bash --noprofile --norc' if dialect == 'bash' else dialect
                script = text.replace('SHELL', child)
                entry = self.root / 'entry.sh'
                entry.write_text('#!/bin/' + dialect + '\n' + script + '\n')
                entry.chmod(0o755)
                for name, content in (helpers or {}).items():
                    path = self.root / name
                    path.write_text(content.replace('SHELL', dialect))
                    path.chmod(0o755)
                docker = self.root / 'Dockerfile'
                original = docker.read_text()
                words = [dialect if x == 'SHELL' else x for x in launch] if launch else None
                if words:
                    docker.write_text(original.replace('["/action/entry.sh"]', json.dumps(words)))
                try:
                    inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
                finally:
                    docker.write_text(original)
                stubs = self.root / 'oracle-bin'
                stubs.mkdir(exist_ok=True)
                log = self.root / 'oracle.log'
                log.write_text('')
                for file, marker in [(stubs / 'node', 'NODE'), (self.root / 'release-action', 'GO')]:
                    file.write_text('#!/bin/sh\nprintf "' + marker + '\\n" >> "$AUDIT_TRACE"\n')
                    file.chmod(0o755)
                # BusyBox supplies ash on hosts without a standalone ash binary.
                busybox = shutil.which('busybox')
                if busybox:
                    shim = stubs / 'ash'
                    shim.write_text('#!/bin/sh\nexec ' + shlex.quote(busybox) + ' ash "$@"\n')
                    shim.chmod(0o755)
                env = {'PATH': str(stubs) + os.pathsep + os.defpath, 'AUDIT_TRACE': str(log)}
                env.update(env_updates or {})
                entry.write_text(script.replace('/action', str(self.root)).replace('/bin/ash', str(stubs / 'ash')) + '\n')
                for name, content in (helpers or {}).items():
                    actual = content.replace('SHELL', dialect).replace('/action', str(self.root))
                    if busybox:
                        actual = actual.replace('#!/bin/ash\n', '#!' + busybox + ' ash\n')
                    (self.root / name).write_text(actual)
                args = [str(entry)] if words is None else [x.replace('/action', str(self.root)) for x in words[1:]]
                try:
                    ran = subprocess.run(shell + args, cwd=self.root, env=env,
                                         capture_output=True, text=True, timeout=10)
                    expected = returncode.get(dialect) if isinstance(returncode, dict) else returncode
                    self.assertEqual(ran.returncode, expected, (text, ran.stderr))
                    trace = log.read_text().splitlines()
                    type(self).oracle_results.append({'script': text, 'dialect': dialect,
                        'launch': words, 'helpers': helpers, 'env_updates': env_updates,
                        'returncode': ran.returncode, 'trace': trace, 'inspected': inspected})
                    self.assertEqual(len(inspected['source_runtime_hits']), trace.count('NODE'), (inspected, trace))
                    self.assertEqual(inspected['go'], 'GO' in trace, (inspected, trace))
                    self.assertEqual(inspected['unresolved'], [], inspected)
                finally:
                    (self.root / 'release-action').unlink()

    def test_reported_five_counterexamples_and_eight_controls(self):
        self.compare('unset VALUE;readonly VALUE;set -u;: "${VALUE:=ready}";exec /action/release-action',
                     returncode=2)
        self.compare('unset VALUE;readonly VALUE;set -u;(: "${VALUE:=ready}";node /action/dist/index.js);exec /action/release-action')
        self.compare("set -- one two;bash --noprofile --norc -uc ': \"$2\";exec /action/release-action'", returncode=127)
        self.compare("set -- one two;bash --noprofile --norc -uc ': \"$2\";node /action/dist/index.js';exec /action/release-action")
        for text in [
            'set -u;: "$PATH" "$PWD" "$IFS";exec /action/release-action',
            'VALUE=ready;readonly VALUE;set -u;: "${VALUE:=fallback}";exec /action/release-action',
            'unset VALUE;readonly VALUE;set -u;: "${VALUE:-ready}";exec /action/release-action',
            'unset VALUE;readonly VALUE;set -u;exec /action/release-action',
            "set -- one two;bash --noprofile --norc -uc ': \"$2\";exec /action/release-action' child first second",
            "set -- one two;bash --noprofile --norc -uc ': \"$2\";node /action/dist/index.js' child first second;exec /action/release-action",
            "set --;bash --noprofile --norc -uc ': \"${2:-ready}\";exec /action/release-action'",
            'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin;PWD=/action;IFS=" ";set -u;: "$PATH" "$PWD" "$IFS";exec /action/release-action',
            ': "$PATH" "$PWD" "$IFS";exec /action/release-action',
        ]:
            self.compare(text)

    def test_readonly_assignment_operators_and_value_states(self):
        for operator, setup in itertools.product(('=', ':='), ('unset VALUE', 'VALUE=', 'VALUE=ready')):
            fails = setup == 'unset VALUE' or setup == 'VALUE=' and operator == ':='
            for nounset in ('', 'set -u;'):
                self.compare(setup + ';readonly VALUE;' + nounset + ': "${VALUE' + operator + 'fallback}";/action/release-action',
                             returncode=2 if fails else 0)
        for operator in ('-', ':-'):
            self.compare('unset VALUE;readonly VALUE;set -u;: "${VALUE' + operator + 'ready}";/action/release-action')

    def test_readonly_operand_runs_before_failure_and_later_words_do_not(self):
        for expression in [': "${VALUE:=$(node)}" "$(node)"',
                           'A="${VALUE=$(node)}" B="$(node)"',
                           ': "${VALUE:=${OTHER:-$(node)}}";node']:
            self.compare('unset VALUE OTHER;readonly VALUE;set -u;' + expression + ';/action/release-action',
                         returncode=2)
        self.compare('VALUE=ready;readonly VALUE;: "${VALUE:=$(node)}";/action/release-action')

    def test_readonly_error_status_conditions_errexit_and_exit_handlers(self):
        for dialect in ('bash', 'ash', 'dash'):
            for operation in ['(: "${VALUE:=ready}";node)', 'RESULT="$( : "${VALUE:=ready}";node )"']:
                status = 1 if dialect == 'bash' and operation.startswith('RESULT=') else 2
                self.compare('unset VALUE;readonly VALUE;' + operation + ';if test "$?" = ' + str(status) + ';then node;fi;/action/release-action',
                             dialects=(dialect,))
                self.compare('unset VALUE;readonly VALUE;set -e;' + operation + ';node;/action/release-action',
                             dialects=(dialect,), returncode=1 if dialect == 'bash' else 2)
            self.compare('unset VALUE;readonly VALUE;if (: "${VALUE:=ready}";node);then node;fi;/action/release-action', dialects=(dialect,))
            self.compare('unset VALUE;readonly VALUE;trap \'node\' EXIT;: "${VALUE:=ready}";/action/release-action',
                         dialects=(dialect,), returncode=2)
            self.compare('unset VALUE;readonly VALUE;: "${VALUE:=ready}";node;/action/release-action',
                         dialects=(dialect,), launch=['SHELL', '-c', 'unset VALUE;readonly VALUE;: "${VALUE:=ready}";node;/action/release-action'], returncode=1 if dialect == 'bash' else 2)

    def test_readonly_bash_status_contexts_match_options(self):
        for setup, script_status, command_status in [('', 2, 1), ('set -e;', 1, 1),
                ('set -o posix;', 1, 127), ('set -eo posix;', 1, 1)]:
            prefix = 'unset VALUE;readonly VALUE;' + setup
            for command_string in (False, True):
                text = prefix + ': "${VALUE:=ready}";node;/action/release-action'
                launch = ['SHELL', '-c', text] if command_string else None
                self.compare(text, dialects=('bash',), launch=launch,
                             returncode=command_status if command_string else script_status)
                for operation in ['(: "${VALUE:=ready}";node)', 'RESULT="$( : "${VALUE:=ready}";node )"']:
                    status = 1 if setup or operation.startswith('RESULT=') else 2
                    text = prefix + 'if ' + operation + ';then node;else if test "$?" = ' + str(status) + ';then /action/release-action;else node;fi;fi'
                    self.compare(text, dialects=('bash',), launch=['SHELL', '-c', text] if command_string else None)
            for invocation in ('run', 'if run;then :;fi', 'run || :'):
                text = prefix + 'run(){ : "${VALUE:=ready}";node; };' + invocation + ';/action/release-action'
                self.compare(text, dialects=('bash',), returncode=script_status)

    def test_new_interpreters_clear_all_parent_numbered_parameters(self):
        for parameter in ('1', '2', '10'):
            self.compare('set -- one two three four five six seven eight nine ten;SHELL -uc \' : "${' + parameter + '}";node\';/action/release-action')
        self.compare('set -- one two;SHELL -uc \'if test "$#" = 0;then /action/release-action;else node;fi\'')

    def test_new_interpreters_install_their_own_arguments_and_zero(self):
        self.compare('set -- stale stale stale;SHELL -uc \'if test "$#" = 2 && test "$0" = child && test "$1" = first && test "$2" = second;then /action/release-action;else node;fi;: "${3+$(node)}"\' child first second')
        self.compare('set -- node;SHELL -uc \'"$@";/action/release-action\'')
        self.compare('set -- true;SHELL -uc \'"$@";/action/release-action\' child node')

    def test_script_and_env_launchers_do_not_inherit_parent_arguments(self):
        helper = '#!/bin/SHELL\nset -u;: "$2";node\n'
        for call in ('/action/helper.sh', 'SHELL /action/helper.sh', 'env /action/helper.sh'):
            self.compare('set -- one two;' + call + ';/action/release-action', helpers={'helper.sh': helper})
            self.compare('set -- stale stale;' + call + ' first second;/action/release-action', helpers={'helper.sh': helper})

    def test_entry_script_and_command_string_arguments(self):
        for launch in [['SHELL', '/action/entry.sh', 'first', 'second'],
                       ['SHELL', '-c', 'set -u;: "$2";/action/release-action', 'child', 'first', 'second']]:
            self.compare('set -u;: "$2";/action/release-action', launch=launch)

    def test_subshell_substitution_functions_and_source_keep_their_scope(self):
        for call in ['(check "$@")', 'RESULT="$(check "$@")"', 'check "$@"', '. /action/helper.sh',
                     '( : "$2";/action/release-action )', 'RESULT="$( : "$2";/action/release-action )"']:
            self.compare('set -u;set -- first second;check(){ if test "$#" = 2 && test "$2" = second;then /action/release-action;else node;fi; };' + call,
                         helpers={'helper.sh': 'if test "$#" = 2 && test "$2" = second;then /action/release-action;else node;fi\n'})

    def test_initial_variables_reset_only_at_a_new_interpreter(self):
        self.compare('set -u;: "$PATH" "$PWD" "$IFS";/action/release-action', env_updates={'IFS': ':', 'PWD': '/invalid'})
        self.compare('export IFS=: PWD=/invalid;SHELL -uc \'if test "$PWD" = /action && test "$IFS" = " \t\n";then /action/release-action;else node;fi\'')
        self.compare('IFS=:;PWD=/invalid;(if test "$IFS" = : && test "$PWD" = /invalid;then /action/release-action;else node;fi)')

    def test_explicitly_unset_initial_variables_stay_unset_in_the_same_shell(self):
        for name in ('PATH', 'PWD', 'IFS'):
            self.compare('unset ' + name + ';set -u;: "$' + name + '";/action/release-action',
                         returncode={'bash': 1, 'ash': 2, 'dash': 2})

    def test_path_defaults_explicit_empty_and_known_values(self):
        for setup in ('unset PATH', 'export PATH=', 'export PATH=/chosen'):
            for dialect in ('bash', 'ash', 'dash'):
                child = '/bin/' + dialect + (' --noprofile --norc' if dialect == 'bash' else '')
                check = ': "$PATH"' if setup == 'unset PATH' else 'test "$PATH" = ' + ("''" if setup == 'export PATH=' else '/chosen')
                self.compare(setup + ';' + child + " -uc 'if " + check.replace("''", '""') + ";then /action/release-action;else /action/oracle-bin/node;fi'", dialects=(dialect,))

    def test_unknown_environment_still_stops_nounset_analysis(self):
        for name in ('UNKNOWN_ENVIRONMENT_VALUE',):
            (self.root / 'entry.sh').write_text('#!/bin/bash\nset -u;: "$' + name + '";node;/action/release-action')
            inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(inspected['go'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [], inspected)
            self.assertTrue(inspected['unresolved'], inspected)


class ShellEnvironmentTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown

    def compare(self, text, *, dialects=('bash', 'ash', 'dash'), returncode=0,
                helpers=None, docker_env=None, launch=None):
        """Execute only authored scripts and absolute, environment-free stubs."""
        import json
        import re
        for dialect in dialects:
            with self.subTest(text=text, dialect=dialect, launch=launch):
                binary = shutil.which('busybox' if dialect == 'ash' else dialect)
                if not binary:
                    self.skipTest('Shell oracle unavailable: ' + dialect)
                shell = [binary, 'ash'] if dialect == 'ash' else [binary]
                if dialect == 'bash':
                    shell += ['--noprofile', '--norc']
                child = 'bash --noprofile --norc' if dialect == 'bash' else dialect
                def authored(value, *, header=False):
                    value = re.sub(r'\bSHELL\b', lambda _: dialect if header else child, value)
                    return re.sub(r'\bNODE\b', '/action/oracle-bin/node /action/dist/index.js', value)
                entry = self.root / 'entry.sh'
                script = authored(text)
                entry.write_text('#!/bin/' + dialect + '\n' + script + '\n')
                entry.chmod(0o755)
                for name, value in (helpers or {}).items():
                    path = self.root / name
                    path.write_text(authored(value, header=True))
                    path.chmod(0o755)
                docker = self.root / 'Dockerfile'
                original = docker.read_text()
                recipe = original
                if docker_env:
                    declarations = ''.join('ENV ' + name + '=' + shlex.quote(value) + '\n' for name, value in docker_env.items())
                    recipe = recipe.replace('ENTRYPOINT', declarations + 'ENTRYPOINT')
                words = [dialect if x == 'SHELL' else authored(x) for x in launch] if launch else None
                if words:
                    recipe = recipe.replace('["/action/entry.sh"]', json.dumps(words))
                docker.write_text(recipe)
                try:
                    inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
                finally:
                    docker.write_text(original)
                stubs = self.root / 'oracle-bin'
                stubs.mkdir(exist_ok=True)
                log = self.root / 'oracle.log'
                log.write_text('')
                for file, marker in [(stubs / 'node', 'NODE'), (self.root / 'release-action', 'GO')]:
                    file.write_text('#!/bin/sh\nprintf "' + marker + '\\n" >> ' + shlex.quote(str(log)) + '\n')
                    file.chmod(0o755)
                busybox = shutil.which('busybox')
                if busybox:
                    shim = stubs / 'ash'
                    shim.write_text('#!/bin/sh\nexec ' + shlex.quote(busybox) + ' ash "$@"\n')
                    shim.chmod(0o755)
                def host(value):
                    return value.replace('/action', str(self.root)).replace('/bin/ash', str(stubs / 'ash'))
                entry.write_text(host(script) + '\n')
                for name, value in (helpers or {}).items():
                    actual = authored(value, header=True)
                    if busybox:
                        actual = actual.replace('#!/bin/ash\n', '#!' + busybox + ' ash\n')
                    (self.root / name).write_text(host(actual))
                args = shell + [str(entry)] if words is None else shell + [host(x) for x in words[1:]] if words[0] == dialect else [host(x) for x in words]
                env = {'PATH': str(stubs) + os.pathsep + os.defpath, **(docker_env or {})}
                try:
                    ran = subprocess.run(args, cwd=self.root, env=env,
                                         capture_output=True, text=True, timeout=10)
                    expected = returncode[dialect] if isinstance(returncode, dict) else returncode
                    self.assertEqual(ran.returncode, expected, (text, ran.stderr))
                    trace = log.read_text().splitlines()
                    type(self).oracle_results.append({'script': script, 'dialect': dialect,
                        'helpers': helpers, 'docker_env': docker_env, 'launch': words,
                        'returncode': ran.returncode, 'trace': trace, 'inspected': inspected})
                    self.assertEqual(len(inspected['source_runtime_hits']), trace.count('NODE'), (inspected, trace))
                    self.assertEqual(inspected['go'], 'GO' in trace, (inspected, trace))
                    self.assertEqual(inspected['unresolved'], [], inspected)
                finally:
                    (self.root / 'release-action').unlink()

    def test_reported_nine_counterexamples_and_eleven_controls(self):
        for text in [
            'set -u;: "$OPTIND";exec /action/release-action',
            ': "${OPTIND:=ready}";if test "$OPTIND" = ready;then NODE;fi;exec /action/release-action',
            ': "${OPTIND:=ready}";if test "$OPTIND" = ready;then :;else NODE;fi;exec /action/release-action',
            "VALUE=ready;bash --noprofile --norc -uc ': \"$VALUE\";NODE';exec /action/release-action",
            "export VALUE=ready;env -i /bin/bash --noprofile --norc -uc ': \"$VALUE\";NODE';exec /action/release-action",
            "export OPTIND=7;bash --noprofile --norc -uc 'if test \"$OPTIND\" = 1;then exec /action/release-action;else NODE;fi'",
            'OPTIND=1;set -u;: "$OPTIND";exec /action/release-action',
            'OPTIND=1;if test "$OPTIND" = 1;then NODE;fi;exec /action/release-action',
            ': "$UID" "$EUID";exec /action/release-action',
            "export VALUE=ready;bash --noprofile --norc -uc ': \"$VALUE\";exec /action/release-action'",
            "export VALUE=ready;bash --noprofile --norc -uc ': \"$VALUE\";NODE';exec /action/release-action",
            "VALUE=ready bash --noprofile --norc -uc ': \"$VALUE\";exec /action/release-action'",
            'VALUE=ready;set -u;(: "$VALUE";exec /action/release-action)',
            'VALUE=ready;set -u;RESULT="$( : "$VALUE";/action/release-action )"',
            "export VALUE=ready;env -u VALUE /bin/bash --noprofile --norc -uc ': \"$VALUE\";NODE';exec /action/release-action",
            "export VALUE=old;env -i VALUE=ready /bin/bash --noprofile --norc -uc ': \"$VALUE\";exec /action/release-action'",
        ]:
            self.compare(text)
        for text in ["VALUE=ready;bash --noprofile --norc -uc ': \"$VALUE\";exec /action/release-action'",
                     "export VALUE=ready;env -i /bin/bash --noprofile --norc -uc ': \"$VALUE\";exec /action/release-action'"]:
            self.compare(text, returncode=127)
        self.compare('set -u;: "$UID" "$EUID";exec /action/release-action', dialects=('bash',))
        self.compare('unset OPTIND;: "${OPTIND:=1}";exec /action/release-action', dialects=('bash',))

    def test_automatic_variables_exist_without_inventing_their_values(self):
        self.compare('set -u;: "$OPTIND" "$PPID" "$PS4";/action/release-action')
        self.compare('set -u;: "$UID" "$EUID" "$OPTERR" "$BASH_VERSION" "$BASHOPTS" "$SHELLOPTS" "$BASHPID" "$LINENO" "$SECONDS" "$RANDOM" "$SHLVL";/action/release-action', dialects=('bash',))
        self.compare('set -u;: "$LINENO" "$RANDOM" "$SHLVL" "$PS1" "$PS2";/action/release-action', dialects=('ash',))
        self.compare('set -u;: "$PS1" "$PS2";/action/release-action', dialects=('dash',))

    def test_optind_resets_at_interpreter_boundaries_only(self):
        self.compare('export OPTIND=7;SHELL -uc \'if test "$OPTIND" = 1;then /action/release-action;else NODE;fi\'')
        for operation in ['(check)', 'RESULT="$(check)"', 'check', '. /action/helper.sh']:
            self.compare('OPTIND=7;check(){ if test "$OPTIND" = 7;then /action/release-action;else NODE;fi; };' + operation,
                         helpers={'helper.sh': 'check\n'})

    def test_unexported_variables_do_not_cross_script_and_env_boundaries(self):
        helper = '#!/bin/SHELL\nset -u;: "$VALUE";NODE\n'
        for call in ['/action/helper.sh', 'SHELL /action/helper.sh', 'env /action/helper.sh']:
            self.compare('VALUE=ready;' + call + ';/action/release-action', helpers={'helper.sh': helper})
            self.compare('export VALUE=ready;' + call + ';/action/release-action', helpers={'helper.sh': helper})

    def test_export_attribute_survives_assignment_and_is_removed_by_unset(self):
        for setup in ['export VALUE=one;VALUE=two', 'VALUE=one;export VALUE;VALUE=two', 'export VALUE;VALUE=two']:
            self.compare(setup + ';SHELL -uc \'if test "$VALUE" = two;then /action/release-action;else NODE;fi\'')
        self.compare('export VALUE=one;unset VALUE;VALUE=two;SHELL -uc \': "$VALUE";NODE\';/action/release-action')
        for dialect in ('bash', 'ash'):
            self.compare('export VALUE=one;export -n VALUE;VALUE=two;SHELL -uc \': "$VALUE";NODE\';/action/release-action', dialects=(dialect,))
        self.compare('export VALUE=one;export -n VALUE;NODE;/action/release-action', dialects=('dash',), returncode=2)

    def test_allexport_toggles_assignments_defaults_declarations_and_loops(self):
        for assignment in ['VALUE=ready', ': "${VALUE:=ready}"', 'for VALUE in ready;do :;done', 'readonly VALUE=ready']:
            for option in ['set -a', 'set -o allexport']:
                self.compare('unset VALUE;' + option + ';' + assignment + ';set +a;SHELL -uc \': "$VALUE";/action/release-action\'')
        self.compare('unset VALUE;set -a;set +o allexport;VALUE=ready;SHELL -uc \': "$VALUE";NODE\';/action/release-action')

    def test_temporary_assignments_reach_only_the_selected_command(self):
        self.compare('unset VALUE;VALUE=ready SHELL -uc \': "$VALUE";/action/release-action\';SHELL -uc \': "$VALUE";NODE\';/action/release-action')
        self.compare('unset VALUE;run(){ SHELL -uc \': "$VALUE";/action/release-action\'; };VALUE=ready run;SHELL -uc \': "$VALUE";NODE\';/action/release-action')

    def test_local_exports_restore_without_losing_global_exports(self):
        for outer in ['unset VALUE', 'export VALUE=outer']:
            for declaration in ['local VALUE=inner;export VALUE', 'local VALUE=inner']:
                self.compare(outer + ';run(){ ' + declaration + ';SHELL -uc \': "$VALUE";NODE\'; };run;SHELL -uc \': "$VALUE";NODE\';/action/release-action')

    def test_clean_environment_options_reset_before_explicit_assignments(self):
        for option in ['-i', '--ignore-environment', '-']:
            self.compare('export VALUE=ready;env ' + option + ' /bin/bash --noprofile --norc -uc \': "$VALUE";NODE\';/action/release-action')
            self.compare('export VALUE=old;env ' + option + ' VALUE=ready /bin/bash --noprofile --norc -uc \': "$VALUE";/action/release-action\'')
        for option in ['-u VALUE', '--unset VALUE', '--unset=VALUE', '-uVALUE']:
            self.compare('export VALUE=ready;env ' + option + ' /bin/bash --noprofile --norc -uc \': "$VALUE";NODE\';/action/release-action')

    def test_clean_environment_closes_unknown_names_and_preserves_the_parent(self):
        self.compare('export VALUE=ready;env -i /bin/bash --noprofile --norc -uc \': "$UNKNOWN_ENVIRONMENT_VALUE";NODE\';: "$VALUE";/action/release-action')
        self.compare('env -i /bin/bash --noprofile --norc -c \'if test -z "$UNKNOWN_ENVIRONMENT_VALUE";then /action/release-action;else NODE;fi\'')
        self.compare('export VALUE=ready;env -i /bin/bash --noprofile --norc -uc \': "$PATH" "$PWD" "$IFS" "$OPTIND" "$UID";/action/release-action\'')

    def test_clean_environment_initializes_json_and_env_shebang_launchers(self):
        body = 'set -u;: "$UNKNOWN_ENVIRONMENT_VALUE";NODE\n'
        self.compare('env /action/helper.sh;/action/release-action', dialects=('bash',),
                     helpers={'helper.sh': '#!/usr/bin/env -S -i bash\n' + body})
        self.compare('set -u;: "$UNKNOWN_ENVIRONMENT_VALUE";NODE', dialects=('bash',),
                     launch=['env', '-i', '/bin/bash', '--noprofile', '--norc', '/action/entry.sh'], returncode=1)

    def test_environment_changes_preserve_operand_expansions_and_parent_directory(self):
        self.compare('env -i VALUE="$(NODE)" /bin/bash --noprofile --norc -c ":";/action/release-action')
        for option in ['-C /tmp', '--chdir=/tmp', '-C/tmp']:
            self.compare('env ' + option + ' /bin/bash --noprofile --norc -uc \'if test "$PWD" = /tmp;then /action/release-action;else NODE;fi\';if test "$PWD" = /action;then /action/release-action;else NODE;fi')

    def test_imported_exports_and_branch_scopes_keep_their_attributes(self):
        self.compare('VALUE=changed;SHELL -uc \'if test "$VALUE" = changed;then /action/release-action;else NODE;fi\'', docker_env={'VALUE': 'inherited'})
        for operation in ['(export VALUE)', 'RESULT="$(export VALUE)"', 'true | export VALUE']:
            self.compare('VALUE=ready;' + operation + ';SHELL -uc \': "$VALUE";NODE\';/action/release-action')
        self.compare('VALUE=ready;run(){ export VALUE; };run;SHELL -uc \': "$VALUE";/action/release-action\'')

    def test_command_lookup_uses_the_shell_path_before_filtering_exports(self):
        self.compare('unset PATH;PATH=/action;cd /tmp;release-action')
        self.compare('unset PATH;PATH=/action;cd /tmp;helper.sh',
                     helpers={'helper.sh': '#!/bin/SHELL\nset -u;if test "$PATH" = /action;then NODE;else /action/release-action;fi\n'})
        for dialect in ('bash', 'ash'):
            self.compare('export PATH=/action;export -n PATH;cd /tmp;release-action', dialects=(dialect,))
        self.compare('env -i PATH=/action release-action')
        # glibc's execvp default excludes /usr/local/bin; Alpine/musl's includes
        # it. Match the host's Debian base for the real-process comparison.
        docker = self.root / 'Dockerfile'
        original = docker.read_text()
        recipe = original.replace('ENTRYPOINT', 'COPY --from=build /action/release-action /usr/local/bin/release-action\nENTRYPOINT')
        docker.write_text(recipe)
        script = 'exec env -i release-action'
        (self.root / 'entry.sh').write_text('#!/bin/bash\n' + script + '\n')
        self.assertTrue(inspect_action(self.root, (self.root / 'action.yml').read_text())['go'])
        docker.write_text(recipe.replace('FROM alpine:3.20', 'FROM debian:bookworm'))
        log = self.root / 'oracle.log'
        log.write_text('')
        marker = self.root / 'release-action'
        marker.write_text('#!/bin/sh\nprintf "GO\\n" >> ' + shlex.quote(str(log)) + '\n')
        marker.chmod(0o755)
        try:
            inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(inspected['go'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [], inspected)
            ran = subprocess.run([shutil.which('bash'), '--noprofile', '--norc', '-c', script],
                                 cwd=self.root, env={'PATH': str(self.root) + os.pathsep + os.defpath},
                                 text=True, capture_output=True, timeout=10)
            self.assertEqual(ran.returncode, 127, ran.stderr)
            self.assertEqual(log.read_text(), '')
            type(self).oracle_results.append({'script': script, 'dialect': 'bash',
                'returncode': ran.returncode, 'trace': [], 'inspected': inspected})
        finally:
            marker.unlink()
            docker.write_text(original)

    def test_unset_fixed_variables_follow_each_shells_export_rules(self):
        for name in ['PS4', 'PS1', 'PS2']:
            self.compare('export ' + name + '=one;unset ' + name + ';' + name + '=two;/bin/bash --noprofile --norc -c \'if test "${' + name + '-absent}" = two;then NODE;else /action/release-action;fi\';/action/release-action')
        self.compare('export IFS=:;unset IFS;IFS=,;/bin/bash --noprofile --norc -c \': "$IFS";/action/release-action\'')

    def test_unset_or_unexported_locals_preserve_the_correct_outer_environment(self):
        for declaration in ['local VALUE', 'local VALUE=', 'local VALUE=inner;unset VALUE',
                            'local VALUE=inner;unset VALUE;VALUE=again']:
            self.compare('export VALUE=outer;run(){ ' + declaration + ';SHELL -c \'if test "${VALUE-absent}" = outer;then NODE;else /action/release-action;fi\'; };run;/action/release-action')
        self.compare('export VALUE=outer;run(){ local VALUE=inner;export -n VALUE;SHELL -c \'if test "${VALUE-absent}" = outer;then NODE;else /action/release-action;fi\'; };run;/action/release-action', dialects=('bash', 'ash'))

    def test_unknown_inherited_environment_remains_unresolved(self):
        (self.root / 'entry.sh').write_text('#!/bin/bash\nset -u;: "$UNKNOWN_ENVIRONMENT_VALUE";NODE;/action/release-action')
        result = inspect_action(self.root, (self.root / 'action.yml').read_text())
        self.assertFalse(result['go'], result)
        self.assertEqual(result['source_runtime_hits'], [], result)
        self.assertTrue(result['unresolved'], result)

    def test_invalid_env_options_do_not_raise_or_scan_the_script(self):
        for header in ['#!/usr/bin/env --unsupported bash', '#!/usr/bin/env -u', '#!/usr/bin/env -S "unterminated']:
            (self.root / 'entry.sh').write_text(header + '\nnode /action/dist/index.js\n/action/release-action\n')
            result = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(result['go'], result)
            self.assertEqual(result['source_runtime_hits'], [], result)
            self.assertTrue(result['unresolved'], result)

    def test_bash_local_export_and_readonly_flags_keep_child_environment(self):
        for option in ['-x', '-rx', '-xr']:
            self.compare('run(){ local ' + option + ' VALUE=ready;SHELL -uc \': "$VALUE";/action/release-action\'; };run', dialects=('bash',))
        self.compare('run(){ local -r VALUE=ready;SHELL -uc \': "$VALUE";NODE\'; };run;/action/release-action', dialects=('bash',))
        self.compare('export VALUE=ready;export -np;SHELL -uc \': "$VALUE";NODE\';/action/release-action', dialects=('bash',))


class ProcessStartupTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    compare = ShellEnvironmentTests.compare

    def test_exec_clear_environment_and_preserve_the_parent(self):
        self.compare('export VALUE=ready;exec -c /bin/bash --noprofile --norc -uc \': "$VALUE";exec /action/release-action\'', dialects=('bash',), returncode=127)
        child = "exec -c /bin/bash --noprofile --norc -uc ': \"$VALUE\";NODE'"
        self.compare('export VALUE=ready;/bin/bash --noprofile --norc -c ' + shlex.quote(child) + ';: "$VALUE";/action/release-action', dialects=('bash',))
        self.compare('export VALUE=ready;exec /bin/bash --noprofile --norc -uc \': "$VALUE";exec /action/release-action\'', dialects=('bash',))

    def test_exec_argv0_selects_the_real_command_string_branch(self):
        for option in ['-a ready', '-aready', '-ca ready', '-c -a ready']:
            for branches in [('NODE', '/action/release-action'), ('/action/release-action', 'NODE')]:
                program = 'if test "$0" = ready;then ' + branches[0] + ';else ' + branches[1] + ';fi'
                self.compare('exec ' + option + ' /bin/bash --noprofile --norc -c ' + shlex.quote(program), dialects=('bash',))
        self.compare('exec -a ready /bin/bash --noprofile --norc -c \'if test "$0" = ready;then /action/release-action;else NODE;fi\'', dialects=('ash',))

    def test_explicit_command_name_script_and_env_override_exec_argv0(self):
        self.compare('exec -a other /bin/bash --noprofile --norc -c \'if test "$0" = ready;then /action/release-action;else NODE;fi\' ready', dialects=('bash',))
        self.compare('exec -a ready /bin/bash --noprofile --norc /action/helper.sh', dialects=('bash',),
                     helpers={'helper.sh': 'if test "$0" = /action/helper.sh;then /action/release-action;else NODE;fi\n'})
        self.compare('exec -a ready env /bin/bash --noprofile --norc -c \'if test "$0" = /bin/bash;then /action/release-action;else NODE;fi\'', dialects=('bash',))
        self.compare('exec -a ready /action/helper.sh', dialects=('bash',), helpers={'helper.sh': '#!/bin/bash\nif test "$0" = /action/helper.sh;then /action/release-action;else NODE;fi\n'})

    def test_exec_empty_name_login_and_no_command(self):
        self.compare('exec -a "" /bin/bash --noprofile --norc -c \'if test -z "$0";then /action/release-action;else NODE;fi\'', dialects=('bash',))
        self.compare('exec -l /bin/bash --noprofile --norc -c \'if test "$0" = -/bin/bash;then /action/release-action;else NODE;fi\'', dialects=('bash',))
        for text in ['exec;/action/release-action', 'exec -c;/action/release-action', 'exec -a ready;/action/release-action', 'exec --;/action/release-action']:
            self.compare(text, dialects=('bash',))
        self.compare('exec -z;/action/release-action', dialects=('bash',))
        self.compare('exec -a;/action/release-action', dialects=('bash',))
        self.compare('set -e;exec -z;NODE;/action/release-action', dialects=('bash',), returncode=2)

    def test_exec_options_follow_the_calling_shell_dialect(self):
        self.compare('exec -c /bin/bash --noprofile --norc -c NODE;/action/release-action', dialects=('ash',), returncode=2)
        self.compare('exec -a ready /bin/bash --noprofile --norc -c NODE;/action/release-action', dialects=('dash',), returncode=127)
        self.compare('exec -l /bin/bash --noprofile --norc -c NODE;/action/release-action', dialects=('ash',), returncode=2)

    def test_bash_env_runs_before_command_script_and_json_bodies(self):
        for body in ['NODE\n', 'exec /action/release-action\n', 'exit 0\n']:
            for command in ["/bin/bash --noprofile --norc -c 'NODE'", '/bin/bash --noprofile --norc /action/helper.sh']:
                self.compare('export BASH_ENV=/action/startup.sh;' + command + ';/action/release-action', dialects=('bash',),
                             helpers={'startup.sh': body, 'helper.sh': 'NODE\n'})
        self.compare('exec /action/release-action', dialects=('bash',),
                     launch=['env', 'BASH_ENV=/action/startup.sh', '/bin/bash', '--noprofile', '--norc', '/action/entry.sh'],
                     helpers={'startup.sh': 'NODE\n'})

    def test_bash_env_only_go_and_exit_dead_node_counterexamples(self):
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c :', dialects=('bash',),
                     helpers={'startup.sh': 'exec /action/release-action\n'})
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c NODE;/action/release-action', dialects=('bash',),
                     helpers={'startup.sh': 'exit 0\n'})
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c \'exec /action/release-action\'', dialects=('bash',),
                     helpers={'startup.sh': 'NODE\n'})

    def test_bash_env_obeys_export_clear_unset_and_privileged_modes(self):
        for setup, command in [
            ('BASH_ENV=/action/startup.sh;', '/bin/bash --noprofile --norc -c'),
            ('export BASH_ENV=/action/startup.sh;', 'env -i /bin/bash --noprofile --norc -c'),
            ('export BASH_ENV=/action/startup.sh;', 'env -u BASH_ENV /bin/bash --noprofile --norc -c'),
            ('export BASH_ENV=/action/startup.sh;', '/bin/bash --noprofile --norc -pc'),
            ('export BASH_ENV=/action/startup.sh;', 'exec -c /bin/bash --noprofile --norc -c'),
        ]:
            self.compare(setup + command + " 'exec /action/release-action'", dialects=('bash',), helpers={'startup.sh': 'NODE\n'})
        self.compare('BASH_ENV=/action/startup.sh /bin/bash --noprofile --norc -c \'exec /action/release-action\'', dialects=('bash',), helpers={'startup.sh': 'NODE\n'})

    def test_posix_and_sh_invocations_skip_bash_env(self):
        for options in ['--posix -c', '-o posix -c']:
            self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc ' + options + " 'exec /action/release-action'", dialects=('bash',), helpers={'startup.sh': 'NODE\n'})
        self.compare('export BASH_ENV=/action/startup.sh;exec -a sh /bin/bash --noprofile --norc -c \'exec /action/release-action\'', dialects=('bash',), helpers={'startup.sh': 'NODE\n'})
        self.compare('export BASH_ENV=/action/startup.sh;exec -a ready /bin/bash --noprofile --norc -c \'exec /action/release-action\' sh', dialects=('bash',), helpers={'startup.sh': 'NODE\n'})

    def test_startup_definitions_variables_and_directory_share_shell_scope(self):
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -uc \'run;: "$VALUE";if test "$PWD" = /tmp;then /action/release-action;else NODE;fi\';if test "$PWD" = /action;then /action/release-action;else NODE;fi',
                     dialects=('bash',), helpers={'startup.sh': 'VALUE=ready;run(){ :; };cd /tmp\n'})

    def test_startup_return_exit_status_and_errexit_follow_bash(self):
        for options in ['', '-e ']:
            for startup, status in [('return 7\n', '0'), ('false;return 7\n', '1')]:
                self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc ' + options + "-c 'if test \"$?\" = " + status + ";then /action/release-action;else NODE;fi';/action/release-action", dialects=('bash',), helpers={'startup.sh': startup})
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c \'if test "$?" = 1;then /action/release-action;else NODE;fi\'', dialects=('bash',), helpers={'startup.sh': 'false\n'})
        self.compare('export BASH_ENV=/action/startup.sh;exec /bin/bash --noprofile --norc -ec NODE', dialects=('bash',), helpers={'startup.sh': 'false\n'})
        self.compare('export BASH_ENV=/action/startup.sh;exec /bin/bash --noprofile --norc -ec NODE', dialects=('bash',), returncode=1, helpers={'startup.sh': 'set -e;false\n'})
        self.compare('export BASH_ENV=/action/startup.sh;exec /bin/bash --noprofile --norc -c NODE', dialects=('bash',), returncode=7, helpers={'startup.sh': 'exit 7\n'})

    def test_bash_env_filename_expands_without_splitting_or_removing_quotes(self):
        for filename in ['/action/start up.sh', '${ROOT}/start up.sh', '$FILE', '$(printf "%s" "$FILE")', '${MISSING:-/action/start up.sh}']:
            self.compare('export ROOT=/action FILE="/action/start up.sh";export BASH_ENV=' + shlex.quote(filename) + ';/bin/bash --noprofile --norc -c NODE;/action/release-action', dialects=('bash',), helpers={'start up.sh': 'exit 0\n'})
        for filename in ['"/action/startup.sh"', "'/action/startup.sh'", '"$FILE"', '/action/missing.sh']:
            self.compare('export FILE=/action/startup.sh BASH_ENV=' + shlex.quote(filename) + ';/bin/bash --noprofile --norc -c \'exec /action/release-action\'', dialects=('bash',), helpers={'startup.sh': 'NODE\n'})

    def test_startup_traps_and_subshells_remain_in_the_correct_process(self):
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c :;/action/release-action', dialects=('bash',), helpers={'startup.sh': "trap 'NODE' EXIT\n"})
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c \'exec /action/release-action\'', dialects=('bash',), helpers={'startup.sh': "trap 'NODE' EXIT\n"})
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c \': "$VALUE";/action/release-action\'', dialects=('bash',), helpers={'startup.sh': 'VALUE=ready;(:);RESULT="$(printf ready)"\n'})

    def test_inherited_nounset_and_errexit_control_reachability(self):
        self.compare('exec env -i SHELLOPTS=nounset /bin/bash --noprofile --norc -c \': "$VALUE";exec /action/release-action\'', dialects=('bash',), returncode=127)
        self.compare('env -i SHELLOPTS=nounset /bin/bash --noprofile --norc -c \': "$VALUE";NODE\';/action/release-action', dialects=('bash',))
        self.compare('env -i SHELLOPTS=errexit /bin/bash --noprofile --norc -c \'false;NODE\';/action/release-action', dialects=('bash',))

    def test_inherited_pipefail_selects_both_condition_branches(self):
        for branches in [('NODE', ':'), (':', 'NODE')]:
            program = 'if false | true;then ' + branches[0] + ';else ' + branches[1] + ';fi;/action/release-action'
            self.compare('env -i SHELLOPTS=pipefail /bin/bash --noprofile --norc -c ' + shlex.quote(program), dialects=('bash',))

    def test_inherited_options_apply_after_command_line_and_can_be_reset(self):
        self.compare('exec env -i SHELLOPTS=nounset /bin/bash --noprofile --norc +u -c \': "$VALUE";NODE\'', dialects=('bash',), returncode=127)
        self.compare('env -i SHELLOPTS=errexit /bin/bash --noprofile --norc +e -c \'false;NODE\';/action/release-action', dialects=('bash',))
        self.compare('env -i SHELLOPTS=noexec /bin/bash --noprofile --norc +n -c NODE;/action/release-action', dialects=('bash',))
        self.compare('env -i SHELLOPTS=nounset /bin/bash --noprofile --norc -c \'set +u;: "$VALUE";/action/release-action\'', dialects=('bash',))
        self.compare('env -i SHELLOPTS=errexit /bin/bash --noprofile --norc -c \'set +e;false;NODE\';/action/release-action', dialects=('bash',))

    def test_exported_shellopts_tracks_current_options_for_new_interpreters(self):
        self.compare('set -e;export SHELLOPTS;if /bin/bash --noprofile --norc -c \'false;NODE\';then :;fi;/action/release-action', dialects=('bash',))
        self.compare('set -e;export SHELLOPTS;set +e;/bin/bash --noprofile --norc -c \'false;NODE\';/action/release-action', dialects=('bash',))
        self.compare('unset VALUE;set -u;export SHELLOPTS;/bin/bash --noprofile --norc -c \': "$VALUE";NODE\';/action/release-action', dialects=('bash',))
        self.compare('set -u;export -n SHELLOPTS;/bin/bash --noprofile --norc -c \': "$VALUE";NODE\';/action/release-action', dialects=('bash',))

    def test_privileged_posix_noexec_and_startup_options_interact(self):
        self.compare('env -i SHELLOPTS=nounset /bin/bash --noprofile --norc -pc \': "$VALUE";/action/release-action\'', dialects=('bash',))
        for option in ['posix', 'privileged']:
            self.compare('env -i SHELLOPTS=' + option + ' BASH_ENV=/action/startup.sh /bin/bash --noprofile --norc -c \'exec /action/release-action\'', dialects=('bash',), helpers={'startup.sh': 'NODE\n'})
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -nc NODE;/action/release-action', dialects=('bash',), helpers={'startup.sh': 'NODE\n'})
        self.compare('env -i SHELLOPTS=banana /bin/bash --noprofile --norc -c \'exec /action/release-action\'', dialects=('bash',))

    def test_startup_recursion_and_unknown_options_are_bounded(self):
        (self.root / 'startup.sh').write_text('/bin/bash --noprofile --norc -c :\n')
        for text in ['export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c /action/release-action',
                     'env SHELLOPTS=onecmd /bin/bash --noprofile --norc -c /action/release-action',
                     'BASH_ENV="$UNKNOWN" /bin/bash --noprofile --norc -c /action/release-action']:
            (self.root / 'entry.sh').write_text('#!/bin/bash\n' + text + '\n')
            result = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(result['go'], result)
            self.assertTrue(result['unresolved'], result)
        (self.root / 'startup.sh').write_bytes(b'\xff\xfe\x00')
        (self.root / 'entry.sh').write_text('#!/bin/bash\nexport BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c /action/release-action\n')
        result = inspect_action(self.root, (self.root / 'action.yml').read_text())
        self.assertFalse(result['go'], result)
        self.assertTrue(result['unresolved'], result)

    def test_shebang_and_json_launches_apply_startup_environment(self):
        self.compare('unset VALUE;/action/helper.sh;/action/release-action', dialects=('bash',),
                     helpers={'helper.sh': '#!/usr/bin/env -S SHELLOPTS=nounset bash\n: "$VALUE";NODE\n'})
        self.compare('/action/helper.sh;/action/release-action', dialects=('bash',),
                     helpers={'helper.sh': '#!/usr/bin/env -S BASH_ENV=/action/startup.sh bash\n/action/release-action\n',
                              'startup.sh': 'NODE\n'})
        self.compare(': "$VALUE";NODE;/action/release-action', dialects=('bash',), returncode=1,
                     launch=['env', '-i', 'SHELLOPTS=nounset', '/bin/bash', '--noprofile', '--norc', '/action/entry.sh'])
        self.compare('env SHELLOPTS=noexec BASH_ENV=/action/startup.sh /action/helper.sh', dialects=('bash',),
                     helpers={'helper.sh': '#!/bin/bash -p\n/action/release-action\n', 'startup.sh': 'NODE\n'})

    def test_enoexec_fallback_uses_calling_shell_startup_rules(self):
        self.compare('export BASH_ENV=/action/startup.sh;/action/helper.sh;/action/release-action', dialects=('bash',),
                     helpers={'helper.sh': 'NODE\n', 'startup.sh': 'exit 0\n'})
        self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc /action/helper.sh;/action/release-action', dialects=('bash',),
                     helpers={'helper.sh': 'NODE\n', 'startup.sh': 'exit 0\n'})
        self.compare('export BASH_ENV=/action/startup.sh;env /action/helper.sh;/action/release-action', dialects=('bash',),
                     helpers={'helper.sh': 'NODE\n', 'startup.sh': 'exit 0\n'})


    def test_noexec_startup_filename_substitutions_follow_bash(self):
        self.compare('export BASH_ENV=\'$(NODE;printf /action/startup.sh)\';/bin/bash --noprofile --norc -nc NODE;/action/release-action', dialects=('bash',),
                     helpers={'startup.sh': 'NODE\n'})


class StartupBoundaryTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    compare = ShellEnvironmentTests.compare

    def startup(self, text, body='exec /action/release-action', *, options='', returncode=0):
        self.compare('export BASH_ENV=/action/startup.sh;exec /bin/bash --noprofile --norc ' + options +
                     '-c ' + shlex.quote(body), dialects=('bash',), returncode=returncode,
                     helpers={'startup.sh': text})

    def test_inherited_trace_verbose_and_noglob_keep_go_and_node_evidence(self):
        for option in ['xtrace', 'verbose', 'noglob']:
            for body in ['exec /action/release-action', 'NODE']:
                self.compare('env -i SHELLOPTS=' + option + ' /bin/bash --noprofile --norc -c ' +
                             shlex.quote(body) + ';/action/release-action', dialects=('bash',))

    def test_short_and_named_options_refresh_shellopts_and_export(self):
        base = 'braceexpand:hashall:interactive-comments'
        for flag, option in [('x', 'xtrace'), ('v', 'verbose'), ('f', 'noglob')]:
            for setting in ['set -' + flag, 'set -o ' + option]:
                self.compare(setting + ';if test "$SHELLOPTS" = ' + shlex.quote(base + ':' + option) +
                             ';then /action/release-action;else NODE;fi;set +' + flag +
                             ';if test "$SHELLOPTS" = ' + shlex.quote(base) + ';then /action/release-action;else NODE;fi', dialects=('bash',))
                self.compare(setting + ';export SHELLOPTS;/bin/bash --noprofile --norc -c ' +
                             shlex.quote('if test "$SHELLOPTS" = ' + shlex.quote(base + ':' + option) +
                                         ';then /action/release-action;else NODE;fi'), dialects=('bash',))

    def test_inherited_options_override_cli_disable_and_allow_explicit_reset(self):
        for flag, option in [('x', 'xtrace'), ('v', 'verbose'), ('f', 'noglob')]:
            self.compare('env -i SHELLOPTS=' + option + ' /bin/bash --noprofile --norc +' + flag +
                         ' -c ' + shlex.quote('set +o ' + option + ';if test "$SHELLOPTS" = braceexpand:hashall:interactive-comments;then /action/release-action;else NODE;fi'), dialects=('bash',))
            self.compare('env -i SHELLOPTS=' + option + ' /bin/bash --noprofile --norc -pc ' +
                         shlex.quote('if test "$SHELLOPTS" = braceexpand:hashall:interactive-comments:privileged;then /action/release-action;else NODE;fi'), dialects=('bash',))

    def test_xtrace_prefix_substitutions_are_runtime_evidence(self):
        for marker in ['NODE', '/action/release-action']:
            self.compare("PS4='$(" + marker + ")';set -x;:;set +x;/action/release-action", dialects=('bash',))
            self.compare('env -i SHELLOPTS=xtrace PS4=' + shlex.quote('$(' + marker + ')') +
                         " /bin/bash --noprofile --norc -c 'exec /action/release-action'", dialects=('bash',))

    def test_xtrace_prefix_is_inactive_when_disabled_or_noexec(self):
        self.compare("PS4='$(NODE)';:;/action/release-action", dialects=('bash',))
        self.compare("env -i SHELLOPTS=xtrace:noexec PS4='$(NODE)' /bin/bash --noprofile --norc -c NODE;/action/release-action", dialects=('bash',))
        self.compare("PS4='$(NODE)';set -x;set +x;:;/action/release-action", dialects=('bash',))

    def test_xtrace_keeps_status_output_and_parameter_side_effects(self):
        self.compare("PS4='$(printf prefix)';set -x;V=$(printf ready);set +x;if test \"$V\" = ready;then /action/release-action;else NODE;fi", dialects=('bash',))
        self.compare("PS4='${VALUE:=ready}';set -x;:;set +x;if test \"$VALUE\" = ready;then /action/release-action;else NODE;fi", dialects=('bash',))
        self.compare("unset VALUE;PS4='$VALUE';set -ux;:;set +x;/action/release-action", dialects=('bash',))

    def test_startup_empty_lists_are_syntax_errors_and_body_continues(self):
        for text in ['if ;then NODE;fi\n', 'if\nthen NODE;fi\n', 'if true;then ;fi\n',
                     'if true;then :;else ;fi\n', 'while ;do NODE;done\n',
                     'until ;do NODE;done\n', 'for V in a;do ;done\n', ';;NODE\n']:
            self.startup(text, 'if test "$?" = 2;then /action/release-action;else NODE;fi')

    def test_startup_syntax_errors_preserve_prior_lines_and_reject_same_line(self):
        for prefix in ['NODE', '/action/release-action']:
            for separator in ['\n', ';']:
                self.startup(prefix + separator + 'if ;then NODE;fi\n')
        self.startup('NODE\nif true;then\nNODE\nelse ;fi\n')

    def test_startup_lexical_errors_preserve_completed_lines(self):
        for suffix in ['printf "unterminated\n', 'printf "$(printf ready\n', 'printf "${VALUE\n']:
            self.startup('NODE\n' + suffix)
            self.startup('NODE;' + suffix)

    def test_startup_multiline_syntax_is_read_as_complete_units(self):
        for text in ['if true;then\nNODE\nfi\n', 'run()\n{\nNODE\n}\nrun\n',
                     'for V in first second;do\nNODE\ndone\n', 'printf "%s" "line\nline"\nNODE\n',
                     ': \\\nNODE\n', ': <<EOF\nNODE\nEOF\n']:
            self.startup(text)

    def test_startup_syntax_error_obeys_explicit_and_inhibited_errexit(self):
        self.startup('if ;then NODE;fi\n', options='-e ')
        self.startup('set -e\nif ;then NODE;fi\n', returncode=2)
        self.startup('set -e;if ;then NODE;fi\n')
        self.startup('set -e\nprintf "unterminated\n', returncode=2)

    def test_startup_readonly_assignment_aborts_file_and_retains_status(self):
        self.startup('SHELLOPTS=bad;NODE\n', 'if test "$?" = 1;then /action/release-action;else NODE;fi')
        self.startup('NODE\nSHELLOPTS=bad;NODE\n')
        self.startup('/action/release-action\nSHELLOPTS=bad;NODE\n')
        self.startup('SHELLOPTS=bad;NODE\n', options='-e ')
        self.startup('set -e;SHELLOPTS=bad;NODE\n', returncode=1)

    def test_startup_readonly_error_obeys_function_and_subshell_scope(self):
        self.startup('run(){ SHELLOPTS=bad;NODE; };run;NODE\n')
        self.startup('(SHELLOPTS=bad;NODE);NODE\n')
        self.startup('SHELLOPTS=$(NODE);NODE\n')

    def test_readonly_environment_assignment_still_executes_bash_command(self):
        for command in ['NODE', '/action/release-action', ':']:
            self.startup('SHELLOPTS=bad ' + command + ';NODE\n')
        self.compare('readonly VALUE=ready;VALUE=bad NODE;/action/release-action', dialects=('bash',))
        self.compare('set -o posix;readonly VALUE=ready;VALUE=bad NODE;/action/release-action', dialects=('bash',), returncode=1)

    def test_readonly_declaration_failure_follows_shell_and_command_context(self):
        for builtin in ['export', 'readonly']:
            self.compare('readonly VALUE=ready;' + builtin + ' VALUE=bad;NODE;/action/release-action',
                         returncode={'bash': 0, 'ash': 2, 'dash': 2})
            self.compare('readonly VALUE=ready;command ' + builtin + ' VALUE=bad;NODE;/action/release-action')
            self.startup(builtin + ' SHELLOPTS=bad;NODE\n')

    def test_unset_readonly_shellopts_reports_failure_and_keeps_running(self):
        for command in ['unset SHELLOPTS', 'unset -v SHELLOPTS', 'command unset SHELLOPTS']:
            self.compare(command + ';if test "$?" = 1;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('unset SHELLOPTS;exec /action/release-action', dialects=('bash',))

    def test_unset_readonly_variable_obeys_each_shell(self):
        self.compare('readonly VALUE=ready;unset VALUE;NODE;/action/release-action',
                     returncode={'bash': 0, 'ash': 2, 'dash': 2})
        self.compare('readonly VALUE=ready;command unset VALUE;NODE;/action/release-action')
        self.compare('readonly VALUE=ready;set -e;unset VALUE;NODE;/action/release-action',
                     returncode={'bash': 1, 'ash': 2, 'dash': 2})

    def test_unset_readonly_failure_selects_conditions_without_errexit(self):
        for command in ['if unset VALUE;then NODE;else /action/release-action;fi',
                        'unset VALUE || /action/release-action', 'unset VALUE && NODE;/action/release-action']:
            self.compare('readonly VALUE=ready;set -e;' + command,
                         returncode={'bash': 0, 'ash': 2, 'dash': 2})
        self.compare('readonly VALUE=ready;set -e;if command unset VALUE;then NODE;else /action/release-action;fi')

    def test_unset_processes_other_operands_and_keeps_readonly_value(self):
        for names in ['VALUE OTHER', 'OTHER VALUE', 'VALUE OTHER VALUE']:
            self.compare('readonly VALUE=ready;OTHER=present;unset ' + names +
                         ';if test "$?" = 1 && test "$VALUE" = ready && test "${OTHER-absent}" = absent;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('readonly VALUE=ready;VALUE(){ NODE; };unset -f VALUE;unset VALUE;: "$VALUE";/action/release-action', dialects=('bash',))

    def test_failed_unset_retains_exported_options_for_children(self):
        self.compare('set -f;export SHELLOPTS;unset SHELLOPTS;/bin/bash --noprofile --norc -c ' +
                     shlex.quote('if test "$SHELLOPTS" = braceexpand:hashall:interactive-comments:noglob;then /action/release-action;else NODE;fi'), dialects=('bash',))
        self.startup('unset SHELLOPTS;NODE\n')

    def test_readonly_error_in_a_sourced_helper_returns_to_its_caller(self):
        for body in ['SHELLOPTS=bad;NODE\n', 'if ;then NODE;fi\n', 'NODE\nif ;then NODE;fi\n', 'return 7;NODE\n']:
            self.compare('export BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c \'exec /action/release-action\'', dialects=('bash',),
                         helpers={'startup.sh': '. /action/helper.sh;NODE\n', 'helper.sh': body})
        self.compare('. /action/helper.sh;NODE;/action/release-action', dialects=('bash',),
                     helpers={'helper.sh': 'SHELLOPTS=bad;NODE\n'})

    def test_failed_export_preserves_export_attribute_for_readonly_variables(self):
        self.compare('readonly VALUE=ready;export VALUE=bad;/bin/bash --noprofile --norc -c ' +
                     shlex.quote('if test "$VALUE" = ready;then /action/release-action;else NODE;fi'), dialects=('bash',))
        self.compare('set -f;export SHELLOPTS=bad;/bin/bash --noprofile --norc -c ' +
                     shlex.quote('if test "$SHELLOPTS" = braceexpand:hashall:interactive-comments:noglob;then /action/release-action;else NODE;fi'), dialects=('bash',))

    def test_trace_and_noglob_flags_reflect_current_and_inherited_options(self):
        for flag, option in [('x', 'xtrace'), ('v', 'verbose'), ('f', 'noglob')]:
            program = 'case "$-" in *' + flag + '*) /action/release-action ;; *) NODE ;; esac'
            self.compare('env -i SHELLOPTS=' + option + ' /bin/bash --noprofile --norc -c ' + shlex.quote(program), dialects=('bash',))
            self.compare('set -' + flag + ';' + program + ';set +' + flag +
                         ';if test "$-" = hB;then /action/release-action;else NODE;fi', dialects=('bash',))

    def test_startup_errors_follow_posix_mode_enabled_inside_the_file(self):
        for text in ['SHELLOPTS=bad;NODE\n', 'SHELLOPTS=bad NODE;NODE\n',
                     'unset SHELLOPTS;NODE\n', 'command export SHELLOPTS=bad;NODE\n',
                     '\nif ;then NODE;fi\n', '\nprintf "unterminated\n']:
            self.startup('set -o posix;' + text)
        self.startup('set -o posix;export SHELLOPTS=bad;NODE\n', returncode=1)

    def test_xtrace_prefix_children_keep_chld_traps_and_assignment_boundaries(self):
        self.compare("PS4='$(printf prefix)';trap 'NODE' CHLD;set -x;:;set +x;/action/release-action", dialects=('bash',))
        self.compare("PS4='$(NODE)';set -x;VALUE=ready;VALUE=other :;export VALUE=last;set +x;/action/release-action", dialects=('bash',))

    def test_unmodelled_child_cannot_be_hidden_by_a_later_go_command(self):
        for prefix in ['', '/action/release-action;']:
            for command in ['env SHELLOPTS=onecmd /bin/bash --noprofile --norc -c NODE',
                            '/bin/bash --noprofile --norc -c \'set -o monitor;NODE\'',
                            'VALUE=ready SHELLOPTS=bad :']:
                (self.root / 'entry.sh').write_text('#!/bin/bash\n' + prefix + command + ';/action/release-action\n')
                inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
                self.assertFalse(inspected['go'], inspected)
                self.assertTrue(inspected['unresolved'], inspected)
                self.assertEqual(inspected['source_runtime_hits'], [], inspected)

    def test_unsupported_valid_startup_syntax_is_not_recovered_as_an_error(self):
        for text in ['time :\n', 'coproc :\n']:
            (self.root / 'startup.sh').write_text(text)
            (self.root / 'entry.sh').write_text('#!/bin/bash\nexport BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c /action/release-action\n')
            inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertTrue(inspected['go'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [], inspected)
        for text in ['time ( NODE )\n', 'time if true;then NODE;fi\n', 'coproc { NODE; }\n']:
            self.startup(text, ':')
        for text in ['case ready in ready) : ;& other) NODE ;; esac\n', '((1)) && NODE\n',
                     '[[ ( ready ) ]] && NODE\n', 'VALUES=();NODE\n']:
            (self.root / 'startup.sh').write_text(text)
            (self.root / 'entry.sh').write_text('#!/bin/bash\nexport BASH_ENV=/action/startup.sh;/bin/bash --noprofile --norc -c :;/action/release-action\n')
            inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(inspected['go'], inspected)
            self.assertTrue(inspected['unresolved'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [], inspected)

class InputUnitAndPrefixTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    compare = ShellEnvironmentTests.compare
    startup = StartupBoundaryTests.startup

    def test_blank_and_comment_lines_keep_startup_and_source_failure_status(self):
        for tail in ['\n', '# comment\n', ' \t\n', '# comment']:
            for branch in ['if test "$?" = 1;then /action/release-action;else NODE;fi',
                           'if test "$?" = 1;then NODE;fi;/action/release-action']:
                self.startup('false\n' + tail, branch)
                self.compare('. /action/helper.sh;' + branch, dialects=('bash',),
                             helpers={'helper.sh': 'false\n' + tail})

    def test_empty_sourced_file_succeeds_without_clobbering_nonempty_files(self):
        for text in ['', '\n', '# comment\n', ' \t\n#comment']:
            self.compare('false;. /action/helper.sh;if test "$?" = 0;then /action/release-action;else NODE;fi',
                         dialects=('bash',), helpers={'helper.sh': text})
        self.compare('false;. /action/helper.sh', dialects=('bash',),
                     helpers={'helper.sh': '\n# comment\nif test "$?" = 1;then /action/release-action;else NODE;fi\n'})

    def test_blank_lines_preserve_numeric_status_and_return_status(self):
        for tail in ['\n\n', '\n#comment\n']:
            self.compare('. /action/helper.sh;if test "$?" = 7;then /action/release-action;else NODE;fi',
                         dialects=('bash',), helpers={'helper.sh': '/bin/bash --noprofile --norc -c "exit 7"' + tail})
            self.startup('false' + tail + 'return 7\n', 'if test "$?" = 1;then /action/release-action;else NODE;fi')

    def test_status_inside_nested_sources_and_startup_files(self):
        self.compare('false;. /action/outer.sh;if test "$?" = 1;then NODE;fi;/action/release-action', dialects=('bash',),
                     helpers={'outer.sh': '. /action/inner.sh\n# outer\n', 'inner.sh': 'false\n#inner\n'})
        self.startup('false\n#comment\nif test "$?" = 1;then NODE;fi\n')

    def test_real_commands_after_blank_lines_still_replace_status(self):
        for text in ['false\n\n:\n#comment\n', 'false\n#comment\ntrue\n\n']:
            self.startup(text, 'if test "$?" = 0;then /action/release-action;else NODE;fi')
        self.startup('set -e\n#comment\nfalse\n\nNODE\n', returncode=1)

    def test_multiline_for_and_case_find_live_go_and_node_commands(self):
        for command in ['NODE', '/action/release-action']:
            for text in ['for VALUE\nin ready;do ' + command + ';done\n',
                         'for VALUE\n#comment\n\nin ready;do ' + command + ';done\n',
                         'case ready\nin ready) ' + command + ';;esac\n',
                         'case ready\n#comment\nin\nready) ' + command + ';;\nesac\n']:
                self.startup(text)

    def test_multiline_for_preserves_implicit_arguments_and_empty_lists(self):
        for text in ['for VALUE\ndo NODE;done\n', 'for VALUE in\ndo NODE;done\n',
                     'for VALUE in;do NODE;done\n', 'for VALUE\n;do NODE;done\n']:
            self.startup(text)
        self.compare('set -- ready;for VALUE\ndo if test "$VALUE" = ready;then /action/release-action;else NODE;fi;done', dialects=('bash',))

    def test_invalid_case_and_for_startup_units_continue_with_status_two(self):
        for text in ['case\nready in ready) NODE;;esac\n', 'for VALUE in\nready;do NODE;done\n',
                     'case ready;in ready) NODE;;esac\n', 'for VALUE\n;do NODE;done\n']:
            self.startup(text, 'if test "$?" = 2;then /action/release-action;else NODE;fi')
            self.startup(text, options='-e ')

    def test_startup_grammar_errors_keep_only_earlier_complete_units(self):
        for malformed in ['case\nready in ready) NODE;;esac\n', 'for VALUE in\nready;do NODE;done\n']:
            self.startup('NODE\n' + malformed)
            self.startup('NODE;' + malformed)
            self.startup('set -e\n' + malformed, returncode=2)

    def test_multiline_sourced_helpers_share_variables_and_status(self):
        self.compare('. /action/helper.sh;if test "$VALUE" = ready;then /action/release-action;else NODE;fi', dialects=('bash',),
                     helpers={'helper.sh': 'for VALUE\nin ready;do :;done\n#comment\n'})
        self.compare('. /action/helper.sh;NODE;/action/release-action', dialects=('bash',),
                     helpers={'helper.sh': 'case\nready in ready) NODE;;esac\n'})

    def test_time_and_coproc_prefixes_find_real_executables(self):
        for prefix in ['time', 'time -p', 'time --', 'time -p --', 'coproc']:
            self.compare(prefix + ' /action/release-action', dialects=('bash',))
            self.compare(prefix + ' NODE;/action/release-action', dialects=('bash',))

    def test_timing_keyword_preserves_condition_and_errexit_status(self):
        for expression in ['time false', 'time -p false', 'time false | true', 'time ! false', '! time false']:
            self.compare('if ' + expression + ';then /action/release-action;else NODE;/action/release-action;fi', dialects=('bash',))
        self.compare('set -e;time false;NODE;/action/release-action', dialects=('bash',), returncode=1)
        self.compare('set -e;time ! false;/action/release-action', dialects=('bash',))

    def test_time_wraps_whole_pipeline_and_keeps_pipefail(self):
        for setting in ['set -o pipefail;', 'set +o pipefail;']:
            self.compare(setting + 'if time false | true;then /action/release-action;else NODE;/action/release-action;fi', dialects=('bash',))
            self.compare(setting + 'time NODE | true;/action/release-action', dialects=('bash',))

    def test_timing_keeps_stdout_and_current_shell_assignments(self):
        self.compare('time VALUE=ready;if test "$VALUE" = ready;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('VALUE=$(time printf ready);if test "$VALUE" = ready;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('run(){ /action/release-action; };time run', dialects=('bash',))

    def test_empty_timing_keyword_and_options_do_not_run_following_lines(self):
        for text in ['time\n', 'time -p\n', 'time --\n', 'time;']:
            self.compare(text + '/action/release-action', dialects=('bash',))
        self.compare('time -q NODE;/action/release-action', dialects=('bash',))

    def test_quoted_and_escaped_prefix_words_use_function_lookup(self):
        for prefix in ['time', 'coproc']:
            for command in ['"' + prefix + '"', '\\' + prefix]:
                self.compare('function ' + prefix + '(){ NODE; };' + command + ';/action/release-action', dialects=('bash',))

    def test_coproc_status_is_success_even_when_its_child_fails(self):
        for body in ['false', 'exit 7', 'exec /action/release-action']:
            self.compare('coproc ' + body + ';if test "$?" = 0;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('set -e;coproc false;/action/release-action', dialects=('bash',))

    def test_coproc_does_not_leak_child_variables_options_or_exit(self):
        self.compare('VALUE=outer;coproc VALUE=inner;if test "$VALUE" = outer;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('coproc set -u;: "$MISSING";/action/release-action', dialects=('bash',))
        self.compare('coproc exit 7;NODE;/action/release-action', dialects=('bash',))

    def test_coproc_pipeline_status_belongs_to_the_parent_pipeline(self):
        for expression in ['coproc true | false', 'coproc false | true', 'coproc NODE | true']:
            self.compare('if ' + expression + ';then /action/release-action;else NODE;/action/release-action;fi', dialects=('bash',))

    def test_coproc_resets_child_traps_and_keeps_parent_exit_handler(self):
        self.compare("trap 'NODE' EXIT;coproc :;exec /action/release-action", dialects=('bash',))
        self.compare("trap 'NODE' EXIT;coproc exit 7;/action/release-action", dialects=('bash',))

    def test_inactive_timed_and_coprocess_branches_do_not_create_evidence(self):
        for prefix in ['time', 'coproc']:
            self.compare('if false;then ' + prefix + ' NODE;fi;exec /action/release-action', dialects=('bash',))
            self.compare('set -n;' + prefix + ' NODE;/action/release-action', dialects=('bash',))

    def test_coprocess_races_and_complex_forms_stay_explicitly_unresolved(self):
        for text in ['coproc NAME { NODE; };/action/release-action', 'time ( NODE );/action/release-action']:
            self.compare(text, dialects=('bash',))
        for text in ["trap ':' CHLD;coproc :;/action/release-action", 'coproc :;: "$COPROC_PID";/action/release-action',
                     'coproc :;: "$!";/action/release-action']:
            (self.root / 'entry.sh').write_text('#!/bin/bash\n' + text + '\n')
            result = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(result['go'], result)
            self.assertTrue(result['unresolved'], result)
            self.assertEqual(result['source_runtime_hits'], [], result)

    def test_case_traces_run_even_without_a_matching_or_nonempty_arm(self):
        for command in ['NODE', '/action/release-action']:
            for syntax in ['case ready in never) :;;esac', 'case ready in ready) ;;esac']:
                self.compare("PS4='$(" + command + ")';set -x;" + syntax, dialects=('bash',))
        self.compare("/action/release-action;PS4='$(NODE)';set -x;case ready in never) :;;esac", dialects=('bash',))

    def test_case_trace_precedes_subject_and_pattern_expansion(self):
        self.compare("PS4='${VALUE:=ready}';set -x;case \"${VALUE-absent}\" in ready) /action/release-action;;*) NODE;;esac", dialects=('bash',))
        self.compare("PS4='${VALUE:=ready}';set -x;case ready in \"${VALUE-absent}\") /action/release-action;;*) NODE;;esac", dialects=('bash',))

    def test_for_header_traces_happen_per_iteration_after_list_expansion(self):
        self.compare("PS4='$(NODE)';set -x;for VALUE in first second;do :;done;set +x;/action/release-action", dialects=('bash',))
        self.compare("PS4='${VALUE:=ready}';set -x;for ITEM in \"${VALUE-absent}\";do if test \"$ITEM\" = absent;then /action/release-action;else NODE;fi;done", dialects=('bash',))
        self.compare("PS4='$(NODE)';set -x;for VALUE in;do :;done", dialects=('bash',))

    def test_trace_assignment_effects_survive_a_temporary_command_environment(self):
        for command in [':', 'printf ready', 'export OTHER=ready', 'readonly OTHER=ready', 'eval :', 'command :']:
            for branch in ['if test "${VALUE-absent}" = ready;then /action/release-action;else NODE;fi',
                           'if test "${VALUE-absent}" = ready;then NODE;fi;/action/release-action']:
                self.compare("unset VALUE;PS4='${VALUE:=ready}';set -x;VALUE=temp " + command + ';set +x;' + branch, dialects=('bash',))

    def test_trace_assignments_reach_functions_and_external_child_environments(self):
        program = 'if test "$VALUE" = ready;then /action/release-action;else NODE;fi'
        self.compare("unset VALUE;PS4='${VALUE:=ready}';set -x;VALUE=temp /bin/bash --noprofile --norc -c " + shlex.quote(program) + ';set +x', dialects=('bash',))
        self.compare("unset VALUE;run(){ " + program + "; };PS4='${VALUE:=ready}';set -x;VALUE=temp run;set +x", dialects=('bash',))

    def test_prompt_side_effects_keep_the_callers_export_attribute(self):
        for setup in ['unset VALUE;', 'export VALUE=;']:
            for mode in ['', 'set -a;']:
                self.compare(setup + mode + "PS4='${VALUE:=ready}';set -x;VALUE=temp :;set +x;/bin/bash --noprofile --norc -c " +
                             shlex.quote('if test "${VALUE-absent}" = ' + ('ready' if mode or setup.startswith('export') else 'absent') + ';then /action/release-action;else NODE;fi'), dialects=('bash',))

    def test_prompt_effects_preserve_unrelated_temporary_bindings(self):
        self.compare("unset VALUE OTHER;PS4='${VALUE:=ready}';set -x;VALUE=temp OTHER=two /bin/bash --noprofile --norc -c " +
                     shlex.quote('if test "$VALUE:$OTHER" = ready:two;then /action/release-action;else NODE;fi') +
                     ';set +x;if test "${OTHER-absent}" = absent;then /action/release-action;else NODE;fi', dialects=('bash',))

    def test_prompt_assignment_export_scope_respects_declarations_and_posix(self):
        for posix in ['', 'set -o posix;']:
            for command in [':', 'export OTHER=ready', 'export VALUE', 'export -n VALUE',
                            'readonly VALUE', 'eval :', 'eval "export VALUE"', 'unset VALUE', 'set -o posix']:
                exported = command in {'export VALUE', 'readonly VALUE'} or bool(posix) and command not in {'export -n VALUE', 'unset VALUE'}
                self.compare(posix + "unset VALUE;PS4='${VALUE:=ready}';set -x;VALUE=temp " + command +
                             ';set +x;/bin/bash --noprofile --norc -c ' + shlex.quote(
                                 'if test "${VALUE-absent}" = ' + ('ready' if exported else 'absent') +
                                 ';then /action/release-action;else NODE;fi'), dialects=('bash',))

    def test_disabled_trace_has_no_prompt_assignment_or_runtime_effect(self):
        self.compare("unset VALUE;PS4='${VALUE:=ready}$(NODE)';VALUE=temp true;if test \"${VALUE-absent}\" = absent;then /action/release-action;else NODE;fi", dialects=('bash',))
        self.compare("PS4='$(NODE)';set -x;set +x;case ready in never) :;;esac;/action/release-action", dialects=('bash',))

    def test_timing_and_coprocess_commands_keep_xtrace_runtime_evidence(self):
        self.compare("PS4='$(NODE)';set -x;time :;set +x;/action/release-action", dialects=('bash',))
        self.compare("PS4='$(NODE)';set -x;coproc :;set +x;/action/release-action", dialects=('bash',))


class PatternAndCommandScopeTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    compare = ShellEnvironmentTests.compare
    startup = StartupBoundaryTests.startup

    def test_temporary_builtin_bindings_restore_existing_caller_values(self):
        for command in [':', 'export OTHER=ready', 'eval :', 'unset VALUE', 'readonly OTHER=ready']:
            for trace in ['', "PS4='${VALUE:=ready}';set -x;"]:
                for branch in ['if test "$VALUE" = outer;then /action/release-action;else NODE;fi',
                               'if test "$VALUE" = outer;then NODE;fi;/action/release-action']:
                    self.compare('VALUE=outer;' + trace + 'VALUE=temp ' + command + ';set +x;' + branch,
                                 dialects=('bash',))

    def test_temporary_values_are_visible_inside_eval_and_sources(self):
        for command, helpers in [('eval \'if test "$VALUE" = temp;then /action/release-action;else NODE;fi\'', {}),
                                 ('. /action/helper.sh', {'helper.sh': 'if test "$VALUE" = temp;then /action/release-action;else NODE;fi\nVALUE=changed;OTHER=kept\n'})]:
            self.compare('VALUE=outer;VALUE=temp ' + command +
                         ';if test "$VALUE" = outer;then /action/release-action;else NODE;fi',
                         dialects=('bash',), helpers=helpers)

    def test_temporary_nested_declarations_restore_readonly_and_export_flags(self):
        for setup in ['VALUE=outer;', 'export VALUE=outer;']:
            for command in ['eval "VALUE=changed"', 'eval "export VALUE=changed"', 'eval "readonly VALUE"',
                            'export -n VALUE', 'unset VALUE', 'command unset VALUE']:
                expected = 'outer' if setup.startswith('export') else 'absent'
                self.compare(setup + 'VALUE=temp ' + command +
                             ';VALUE=after;/bin/bash --noprofile --norc -c ' + shlex.quote(
                                 'if test "${VALUE-absent}" = ' + ('after' if expected == 'outer' else 'absent') +
                                 ';then /action/release-action;else NODE;fi'), dialects=('bash',))

    def test_direct_export_and_readonly_promote_only_declared_temporary_names(self):
        for command in ['export VALUE', 'readonly VALUE', 'command export VALUE',
                        'export VALUE=changed', 'readonly VALUE=changed']:
            expected = 'changed' if '=changed' in command else 'temp'
            self.compare('VALUE=outer;OTHER=outer;VALUE=temp OTHER=two ' + command +
                         ';if test "$VALUE:$OTHER" = ' + expected + ':outer;then /action/release-action;else NODE;fi',
                         dialects=('bash',))

    def test_posix_special_builtins_persist_but_command_bypass_does_not(self):
        for setup in ['VALUE=outer;', 'export VALUE=outer;']:
            for command, value in [(':', 'temp'), ('export OTHER=ready', 'temp'), ('set -a', 'temp'),
                                   ('eval "VALUE=changed"', 'changed'), ('unset VALUE', 'outer'),
                                   ('eval "unset VALUE"', 'outer'), ('command :', 'outer'),
                                   ('command export VALUE', 'temp'), ('command export -n VALUE', 'outer')]:
                self.compare('set -o posix;' + setup + 'VALUE=temp ' + command +
                             ';if test "$VALUE" = ' + value + ';then /action/release-action;else NODE;fi', dialects=('bash',))

    def test_local_declarations_keep_their_own_value_and_restore_the_function_caller(self):
        for declaration, expected in [('local VALUE', 'temp'), ('local VALUE=inner', 'inner'),
                                      ('local -x VALUE=inner', 'inner')]:
            self.compare('VALUE=outer;run(){ VALUE=temp ' + declaration +
                         ';if test "$VALUE" = ' + expected + ';then /action/release-action;else NODE;fi;};run;'
                         'if test "$VALUE" = outer;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('VALUE=outer;VALUE=temp local VALUE;'
                     'if test "$?" = 1 && test "$VALUE" = outer;then /action/release-action;else NODE;fi', dialects=('bash',))

    def test_other_dialects_preserve_special_builtin_prefix_semantics(self):
        for dialect in ['ash', 'dash']:
            for command, value in [(':', 'temp'), ('export VALUE', 'temp'), ('unset VALUE', 'absent'),
                                   ('true', 'outer'), ('command :', 'outer')]:
                self.compare('VALUE=outer;VALUE=temp ' + command +
                             ';if test "${VALUE-absent}" = ' + value + ';then /action/release-action;else NODE;fi', dialects=(dialect,))

    def test_posix_case_classes_and_negation_select_both_runtime_branches(self):
        for value, pattern in [('1', '[[:digit:]]'), ('a', '[![:digit:]]'), ('a', '[^[:digit:]]')]:
            for command in ['NODE', '/action/release-action']:
                self.compare('case ' + value + ' in ' + pattern + ') ' + command + ';;esac;' +
                             ('/action/release-action' if command == 'NODE' else ':'), dialects=('bash', 'ash', 'dash'))

    def test_all_standard_named_classes_and_mixed_brackets(self):
        values = {'alnum': '1', 'alpha': 'a', 'blank': '\t', 'cntrl': '\x01', 'digit': '2',
                  'graph': '!', 'lower': 'a', 'print': ' ', 'punct': '!', 'space': '\n', 'upper': 'A', 'xdigit': 'F'}
        for name, value in values.items():
            for negate in ['', '!']:
                self.compare('case ' + shlex.quote(value) + ' in [' + negate + '[:' + name + ':]]) NODE;;esac;/action/release-action',
                             dialects=('bash', 'ash', 'dash'))
        for value in ['_', '1', 'A', 'z', ']']:
            self.compare('case ' + shlex.quote(value) + ' in [A[:digit:]_]) NODE;;esac;/action/release-action', dialects=('bash',))

    def test_sh_uses_the_base_image_bracket_negation_rules(self):
        docker = self.root / 'Dockerfile'
        docker.write_text(docker.read_text().replace('FROM alpine:3.20', 'FROM debian:bookworm-slim'))
        for value in ['a', '^', '1', '[']:
            for pattern in ['[^[:digit:]]', '[![:digit:]]', '[a[^]', '[\\^]']:
                self.compare('case ' + shlex.quote(value) + ' in ' + pattern +
                             ') NODE;;esac;/action/release-action', dialects=('sh',))
        for pattern in ['?', '??', '[é]', '[[:alpha:]]']:
            self.compare('case é in ' + pattern + ') NODE;;esac;/action/release-action',
                         dialects=('sh',), docker_env={'LC_ALL': 'C.UTF-8'})

    def test_quoted_pattern_metacharacters_stay_literal_inside_and_outside_brackets(self):
        for value, pattern in [('*', '"*"'), ('x', '"*"'), ('[[:digit:]]', '"[[:digit:]]"'),
                               ('1', '"[[:digit:]]"'), ('-', '[a"-"c]'), ('b', '[a"-"c]'),
                               ('!', '["!"a]'), ('x', '["!"a]'), (']', '["]"a]'),
                               ('\\', '"\\\\"'), ('x', r'\?')]:
            self.compare('case ' + shlex.quote(value) + ' in ' + pattern + ') NODE;;esac;/action/release-action',
                         dialects=('bash', 'ash', 'dash'))

    def test_locale_controls_character_classes_without_changing_the_python_locale(self):
        import locale
        before = locale.setlocale(locale.LC_CTYPE)
        for selected in ['C', 'C.UTF-8']:
            for value in ['é', '١', 'A', '9']:
                self.compare('LC_ALL=' + selected + ';case ' + value +
                             ' in [[:alpha:]]) NODE;;esac;/action/release-action')
        for value in ['é', 'éabc']:
            for pattern in ['?', '??', '[é]', '[!é]', '??a*', '[é][é]*']:
                self.compare('case ' + value + ' in ' + pattern + ') NODE;;esac;/action/release-action',
                             docker_env={'LC_ALL': 'C.UTF-8'})
        self.assertEqual(locale.setlocale(locale.LC_CTYPE), before)

    def test_bracket_quotes_and_collating_elements_follow_each_interpreter(self):
        for value in ['a', 'b', '^', 'a]', '.]']:
            for pattern in ['["^"a]', "['^'a]", r'[\^a]', r'[\^"a"]',
                            '[[=a=]]', '[[.a.]]']:
                self.compare('case ' + shlex.quote(value) + ' in ' + pattern +
                             ') NODE;;esac;/action/release-action')

    def test_case_alternatives_stop_before_inactive_substitutions(self):
        self.compare('case ready in ready|"$(NODE)") :;;esac;/action/release-action')
        self.compare('case ready in ready|"$(/action/release-action)") :;;esac')
        self.compare('case ready in never|"$(NODE;printf ready)") :;;esac;/action/release-action')

    def test_case_alternatives_keep_later_arms_and_expansion_status_lazy(self):
        self.compare('case ready in never|other) NODE;;ready|"$(NODE)") :;;*) NODE;;esac;/action/release-action')
        self.compare('false;case ready in "$(exit 7)"|"$(printf ready)"|"$(NODE)") :;;esac;/action/release-action', dialects=('bash',))
        self.compare('unset VALUE;case ready in ready|"${VALUE:=changed}") :;;esac;'
                     'if test "${VALUE-absent}" = absent;then /action/release-action;else NODE;fi')

    def test_timing_wraps_compound_groups_loops_conditions_and_pipelines(self):
        for body in ['{ COMMAND; }', '( COMMAND )', 'if true;then COMMAND;fi',
                     'for VALUE in ready;do COMMAND;done', 'case ready in ready) COMMAND;;esac',
                     'while false;do NODE;done;COMMAND']:
            for command in ['NODE', '/action/release-action']:
                self.compare('time ' + body.replace('COMMAND', command) + ';/action/release-action', dialects=('bash',))

    def test_timed_compounds_preserve_errexit_status_scope_and_stdout(self):
        self.compare('set -e;time { false; };NODE;/action/release-action', dialects=('bash',), returncode=1)
        self.compare('if time { false; };then NODE;else /action/release-action;fi', dialects=('bash',))
        self.compare('VALUE=$(time { printf ready; });if test "$VALUE" = ready;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('VALUE=outer;time { VALUE=inner; };if test "$VALUE" = inner;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('VALUE=outer;time ( VALUE=inner );if test "$VALUE" = outer;then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare('set -o pipefail;if time { false; } | true;then NODE;else /action/release-action;fi', dialects=('bash',))

    def test_coprocess_compounds_keep_child_scope_and_find_both_runtimes(self):
        for name in ['', 'WORK ']:
            for body in ['{ COMMAND; }', '( COMMAND )', 'if true;then COMMAND;fi',
                         'for VALUE in ready;do COMMAND;done', 'case ready in ready) COMMAND;;esac']:
                for command in ['NODE', '/action/release-action']:
                    self.compare('coproc ' + name + body.replace('COMMAND', command) + ';/action/release-action', dialects=('bash',))
        self.compare('VALUE=outer;coproc WORK { VALUE=inner;set -u;exit 7; };'
                     'if test "$?" = 0 && test "$VALUE" = outer;then /action/release-action;else NODE;fi', dialects=('bash',))

    def test_coprocess_stdout_and_traps_do_not_leak_into_the_parent(self):
        self.compare('VALUE="$(coproc { printf ready; })";if test "$VALUE" = "";then /action/release-action;else NODE;fi', dialects=('bash',))
        self.compare("trap 'NODE' EXIT;coproc { exit 7; };exec /action/release-action", dialects=('bash',))
        self.compare('set -e;coproc { false;NODE; };/action/release-action', dialects=('bash',))

    def test_external_time_wrapper_follows_its_executable_without_function_lookup(self):
        for wrapper in ['time', 'time -p', 'time --', '/usr/bin/time', '/usr/bin/time -p --']:
            for command in ['NODE', '/action/release-action']:
                self.compare('coproc ' + wrapper + ' ' + command + ';/action/release-action', dialects=('bash',))
        self.compare('function time(){ NODE; };"time";/action/release-action', dialects=('bash',))
        self.compare('function time(){ NODE; };/usr/bin/time /action/release-action', dialects=('bash',))
        self.compare('if /usr/bin/time false;then NODE;else /action/release-action;fi', dialects=('bash',))

    def test_replaced_time_executable_is_resolved_before_timing_wrapper_semantics(self):
        self.compare('/action/time NODE;/action/release-action', dialects=('bash',),
                     helpers={'time': '#!/bin/sh\n/action/release-action\n'})
        self.compare('/action/time /action/release-action', dialects=('bash',),
                     helpers={'time': '#!/bin/sh\nNODE\n'})
        self.compare('PATH=/action:$PATH;command time NODE;/action/release-action', dialects=('bash',),
                     helpers={'time': '#!/bin/sh\n/action/release-action\n'})
        self.compare('PATH=/action:$PATH;command time /action/release-action', dialects=('bash',),
                     helpers={'time': '#!/bin/sh\nNODE\n'})

    def test_inactive_compound_wrappers_never_create_runtime_evidence(self):
        for text in ['time { NODE; }', 'coproc { NODE; }', 'coproc WORK { NODE; }', 'coproc time NODE']:
            self.compare('if false;then ' + text + ';fi;/action/release-action', dialects=('bash',))
            self.compare('set -n;' + text + ';/action/release-action', dialects=('bash',))

    def test_unmodelled_coprocess_races_and_pattern_locales_do_not_grant_go_credit(self):
        for text in ['coproc WORK { :; };: "$WORK_PID";/action/release-action',
                     "trap ':' CHLD;coproc { :; };/action/release-action",
                     'LC_ALL="$UNRESOLVED";case A in [[:alpha:]]) NODE;;esac;/action/release-action']:
            (self.root / 'entry.sh').write_text('#!/bin/bash\n' + text + '\n')
            inspected = inspect_action(self.root, (self.root / 'action.yml').read_text())
            self.assertFalse(inspected['go'], inspected)
            self.assertTrue(inspected['unresolved'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [], inspected)


class BracketBoundaryTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    compare = ShellEnvironmentTests.compare

    def test_caret_and_closing_brackets_select_the_actual_runtime(self):
        for value in ['^', '^]', '[^]', 'a', ']', '1']:
            for pattern in ['[^]', '[^]]', '[^^]', '[!^]', '[!]]', '[]^]',
                            r'[\^]', '["^"]', '[^[:digit:]]', '[[:digit:]^]']:
                for command in ['NODE', '/action/release-action']:
                    self.compare('case ' + shlex.quote(value) + ' in ' + pattern + ') ' + command +
                                 ';;esac;' + ('/action/release-action' if command == 'NODE' else ':'))

    def test_debian_sh_uses_literal_carets_at_the_bracket_boundary(self):
        docker = self.root / 'Dockerfile'
        docker.write_text(docker.read_text().replace('FROM alpine:3.20', 'FROM debian:bookworm-slim'))
        for value, pattern in [('^', '[^]'), ('^]', '[^]]'), ('[^]', '[^]'), ('a', '[^]')]:
            for command in ['NODE', '/action/release-action']:
                self.compare('case ' + shlex.quote(value) + ' in ' + pattern + ') ' + command +
                             ';;esac;' + ('/action/release-action' if command == 'NODE' else ':'), dialects=('sh',))


class CommandResolutionTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown

    def compare(self, text, *, dialects=('bash',), helpers=None, returncode=0):
        import re
        from regressions.test_verifier_boundaries import CONFIG, DOCKER_ACTION
        from public_behavior import PublicBehavior
        from verifier.detectors.entrypoint import detect_entrypoint_source_runtime

        def render(value):
            value = re.sub(r'\bGO\b', '/action/release-action', value)
            return re.sub(r'\bNODE\b', '/action/oracle-bin/node /action/dist/index.js', value)

        for dialect in dialects:
            with self.subTest(text=text, dialect=dialect, helpers=helpers):
                executable = shutil.which('busybox' if dialect == 'ash' else dialect)
                if not executable:
                    self.skipTest('Shell oracle unavailable: ' + dialect)
                shell = [executable, 'ash'] if dialect == 'ash' else [executable]
                if dialect == 'bash':
                    shell += ['--noprofile', '--norc']
                docker = self.root / 'Dockerfile'
                # Keep /bin/sh's identity explicit as well as the shebang.
                original = docker.read_text()
                docker.write_text(original.replace('FROM alpine:3.20', 'FROM debian:bookworm-slim'))
                (self.root / 'entry.sh').write_text('#!/bin/' + dialect + '\n' + render(text) + '\n')
                for name, content in (helpers or {}).items():
                    (self.root / name).write_text(render(content))
                try:
                    inspected = inspect_action(self.root, DOCKER_ACTION)
                    public = PublicBehavior.__new__(PublicBehavior)
                    public.mod = self.root
                    entry, detail = True, None
                    try:
                        public.entry()
                    except AssertionError as error:
                        entry, detail = False, str(error)
                    hits = detect_entrypoint_source_runtime(self.root, CONFIG)
                finally:
                    docker.write_text(original)

                oracle = self.root / 'oracle'
                stubs = oracle / 'oracle-bin'
                stubs.mkdir(parents=True, exist_ok=True)
                trace = oracle / 'trace'
                trace.write_text('')
                for file, marker in [(oracle / 'release-action', 'GO'), (stubs / 'node', 'NODE')]:
                    file.write_text('#!/bin/sh\nprintf "' + marker + '\\n" >> "$AUDIT_TRACE"\n')
                    file.chmod(0o755)
                for name, content in {'entry.sh': text, **(helpers or {})}.items():
                    (oracle / name).write_text(render(content).replace('/action', str(oracle)) + '\n')
                ran = subprocess.run(shell + [str(oracle / 'entry.sh')], cwd=oracle,
                                     env={'PATH': os.defpath, 'AUDIT_TRACE': str(trace)},
                                     capture_output=True, text=True, timeout=10)
                markers = trace.read_text().splitlines()
                expected_status = returncode.get(dialect) if isinstance(returncode, dict) else returncode
                record = {'script': text, 'dialect': dialect, 'helpers': helpers,
                          'returncode': ran.returncode, 'trace': markers, 'stderr': ran.stderr,
                          'inspected': inspected, 'entry': entry, 'entry_detail': detail,
                          'entrypoint_hits': hits}
                type(self).oracle_results.append(record)
                self.assertEqual(ran.returncode, expected_status, record)
                self.assertEqual(inspected['unresolved'], [], record)
                self.assertEqual(inspected['go'], 'GO' in markers, record)
                self.assertEqual(len(inspected['source_runtime_hits']), markers.count('NODE'), record)
                self.assertEqual(bool(hits), 'NODE' in markers, record)
                self.assertEqual(entry, 'GO' in markers and 'NODE' not in markers, record)

    def test_reported_ordinary_function_pairs_and_controls(self):
        for name in ['export', 'readonly', 'local', 'exit', 'return', 'break', 'continue',
                     'eval', 'set', 'unset', 'exec', 'trap', 'true']:
            self.compare(name + '(){ GO; };' + name)
            self.compare('GO;' + name + '(){ NODE; };' + name)

    def test_reported_command_bypasses_and_top_level_jumps(self):
        for name in ['export', 'readonly', 'local', 'exit', 'return', 'break', 'continue']:
            self.compare(name + '(){ NODE; };command ' + name + ';exec GO')
        for name in ['return', 'break', 'continue']:
            self.compare(name + ';GO')
            self.compare('GO;' + name + ';NODE')
            self.compare('GO;' + name + '(){ NODE; };command ' + name + ';NODE')

    def test_reported_function_status_and_prefix_pairs(self):
        for name in ['export', 'readonly', 'local']:
            declaration = name + '(){ ' + (':' if name == 'local' else 'false') + '; };'
            for good in [True, False]:
                success = good if name == 'local' else not good
                branch = 'if ' + name + ';then ' + ('GO;else NODE' if success else 'NODE;else GO') + ';fi'
                self.compare(declaration + branch)
        for name in ['export', 'readonly']:
            prefix = 'VALUE=outer;' + name + '(){ :; };VALUE=temp ' + name + ' VALUE;'
            self.compare(prefix + 'if test "$VALUE" = outer;then GO;else NODE;fi')
            self.compare(prefix + 'if test "$VALUE" = outer;then NODE;fi;GO')

    def test_reported_posix_special_builtin_pairs(self):
        for name in ['eval', 'set', 'unset', 'exec', 'trap']:
            self.compare('GO;' + name + '(){ NODE; };command set -o posix;' + name)
            self.compare(name + '(){ GO; };command set -o posix;' + name)

    def test_posix_toggle_and_command_keep_their_lookup_rules(self):
        for name in ['export', 'readonly', 'eval', 'set', 'unset', 'exec', 'trap']:
            for toggle, invoke in itertools.product(
                    ['', 'command set -o posix;', 'command set -o posix;command set +o posix;'],
                    [name, 'command ' + name, 'command -p -- ' + name, 'command command ' + name]):
                self.compare('GO;' + name + '(){ NODE; };' + toggle + invoke)
                self.compare(name + '(){ GO; };' + toggle + invoke)

    def test_special_function_definitions_require_posix_mode_to_be_disabled(self):
        for name in ['break', 'continue', 'eval', 'exec', 'exit', 'export', 'readonly', 'return',
                     'set', 'shift', 'times', 'trap', 'unset']:
            self.compare('set -o posix;' + name + '(){ NODE; };GO', returncode=2)
            self.compare('GO;set -o posix;' + name + '(){ NODE; };NODE', returncode=2)
            self.compare('set -o posix;command set +o posix;' + name + '(){ GO; };' + name)

    def test_function_named_command_only_bypasses_when_it_is_itself_bypassed(self):
        for invocation in ['command export', 'command command export']:
            self.compare('command(){ GO; };export(){ NODE; };' + invocation)
        self.compare('command(){ NODE; };export(){ NODE; };GO;"command" export')

    def test_declaration_expansion_is_lexical_even_for_functions(self):
        for name in ['export', 'readonly', 'local']:
            for invocation, expected in [(name, '1:X=a b:'), ('"' + name + '"', '2:X=a:b'),
                                         (name[:2] + '\\' + name[2:], '2:X=a:b'),
                                         ('NAME=' + name + ';$NAME', '2:X=a:b')]:
                self.compare(name + '(){ if test "$#:$1:${2-}" = ' + shlex.quote(expected) +
                             ';then GO;else NODE;fi; };VALUE="a b";' + invocation + ' X=$VALUE')
        for invocation in ['command export', 'NAME=export;$NAME']:
            self.compare('VALUE="a b";' + invocation + ' X=$VALUE;'
                         'if test "$X" = a;then GO;else NODE;fi')

    def test_declaration_arguments_follow_quoting_command_options_and_mode(self):
        for dialect, mode, invocation, operand in itertools.product(
                ['bash', 'ash', 'dash'], ['', 'set -o posix;'],
                ['export', '"export"', 'ex\\port', 'command export', '"command" export',
                 'command "export"', 'command command export', 'command -p export',
                 'command -- export', 'NAME=export;$NAME'],
                ['X=$VALUE', '$ARGUMENT']):
            if mode and dialect != 'bash':
                continue
            for expected in ['a', 'a b']:
                self.compare(mode + 'VALUE="a b";ARGUMENT="X=a b";' + invocation + ' ' + operand +
                             ';if test "$X" = ' + shlex.quote(expected) + ';then GO;else NODE;fi', dialects=(dialect,))

    def test_actual_declarations_promote_prefixes_but_functions_do_not(self):
        for name in ['export', 'readonly']:
            for mode, invocation, expected in itertools.product(
                    ['', 'command set -o posix;'], [name, 'command ' + name], ['outer', 'temp']):
                self.compare(name + '(){ :; };VALUE=outer;' + mode + 'VALUE=temp ' + invocation + ' VALUE;'
                             'if test "$VALUE" = ' + expected + ';then GO;else NODE;fi')
        self.compare('local(){ :; };VALUE=outer;run(){ VALUE=temp local VALUE;'
                     'if test "$VALUE" = outer;then GO;else NODE;fi; };run')

    def test_invalid_return_obeys_posix_command_and_errexit_context(self):
        for mode, invocation in itertools.product(['', 'set -o posix;'], ['return', 'command return']):
            fatal = bool(mode) and invocation == 'return'
            self.compare(mode + invocation + ';NODE;GO', returncode=2 if fatal else 0)
            self.compare('set -e;' + mode + invocation + ';NODE;GO', returncode=2)
            self.compare('set -e;' + mode + 'if ' + invocation + ';then NODE;else GO;fi')
            self.compare(mode + invocation + ' || GO')

    def test_out_of_loop_jumps_are_successful_noops(self):
        for dialect, command, operand in itertools.product(['bash', 'ash', 'dash'], ['break', 'continue'], ['', ' 1', ' 2']):
            self.compare('set -e;' + command + operand + ';GO;NODE', dialects=(dialect,))
            self.compare('if ' + command + operand + ';then GO;else NODE;fi', dialects=(dialect,))

    def test_active_loop_control_works_through_eval_and_command(self):
        for dialect, command, wrapper in itertools.product(['bash', 'ash', 'dash'], ['break', 'continue'], ['', 'command ', 'eval ']):
            for loop in ['for x in a b;do BODY;done', 'while true;do BODY;done', 'until false;do BODY;done']:
                # Continue in a finite for loop; break also covers both loop
                # conditions. Avoid genuinely infinite while/continue scripts.
                if command == 'continue' and not loop.startswith('for'):
                    continue
                self.compare(loop.replace('BODY', wrapper + command + ';NODE') + ';GO', dialects=(dialect,))
        self.compare('while break;do NODE;done;GO')

    def test_function_and_subshell_loop_boundaries_follow_the_shell(self):
        for dialect, command in itertools.product(['bash', 'ash', 'dash'], ['break', 'continue']):
            self.compare('run(){ ' + command + ';NODE; };for x in a;do run;NODE;done;GO', dialects=(dialect,))
            self.compare('for x in a;do (' + command + ';NODE);NODE;done;GO', dialects=(dialect,))
            self.compare('for x in a;do V=$(' + command + ';NODE);NODE;done;GO', dialects=(dialect,))
            self.compare('run(){ for x in a;do ' + command + ';NODE;done; };run;GO;NODE', dialects=(dialect,))
        self.compare('for x in a;do { break;NODE; } | true;NODE;done;GO')

    def test_source_and_function_returns_resume_only_their_callers(self):
        for invocation in ['return', 'command return', 'eval return']:
            helpers = {'helper.sh': invocation + ';NODE\n'}
            self.compare('. /action/helper.sh;GO', helpers=helpers, dialects=('bash', 'ash', 'dash'))
            self.compare('run(){ . /action/helper.sh;NODE; };run;GO', helpers=helpers, dialects=('bash', 'ash', 'dash'))
            self.compare('run(){ ' + invocation + ';NODE; };run;GO', dialects=('bash', 'ash', 'dash'))
            self.compare('run(){ V=$(' + invocation + ';NODE);NODE; };run;GO', dialects=('bash', 'ash', 'dash'))
        for command in ['break', 'continue']:
            self.compare('for x in a;do . /action/helper.sh;NODE;done;GO', helpers={'helper.sh': command + ';NODE\n'},
                         dialects=('bash', 'ash', 'dash'))


class BuiltinOperandTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    compare = CommandResolutionTests.compare

    def test_invalid_declarations_choose_failure_and_keep_later_valid_bindings(self):
        for builtin, wrapper, mode, operand in itertools.product(
                ['export', 'readonly', 'local'], ['', 'command ', 'eval '],
                ['', 'set -o posix;'], ['1bad', 'bad-name=value', 'aé=value']):
            call = wrapper + builtin + ' ' + operand + ' VALUE=node'
            body = 'if ' + call + ';then GO;else NODE;fi;GO'
            if builtin == 'local':
                body = 'run(){ ' + body + '; };run'
            self.compare(mode + body)
            # An invalid operand must not suppress a later valid declaration.
            body = call + ';if test "$VALUE" = node;then GO;else NODE;fi'
            if builtin == 'local':
                body = 'run(){ ' + body + '; };run'
            self.compare(mode + body)

    def test_non_bash_declaration_errors_respect_command_and_stop_at_first_error(self):
        for dialect, builtin, wrapper in itertools.product(
                ['dash', 'ash'], ['export', 'readonly', 'local'], ['', 'command ']):
            call = wrapper + builtin + ' 1bad VALUE=node'
            body = 'GO;' + call + ';NODE'
            if builtin == 'local':
                body = 'run(){ ' + body + '; };run'
            self.compare(body, dialects=(dialect,), returncode=0 if wrapper else 2)
            if wrapper:
                body = call + ';if test "${VALUE-unset}" = unset;then GO;else NODE;fi'
                if builtin == 'local':
                    body = 'run(){ ' + body + '; };run'
                self.compare(body, dialects=(dialect,))

    def test_bash_return_statuses_and_numeric_error_precedence(self):
        operands = ['-1', '+1', '-0', '256', '257', '-257', '" 1 "',
                    'invalid', 'invalid extra', '--', '-- 7', '-- -1',
                    '9223372036854775807', '9223372036854775808',
                    '-9223372036854775808', '-9223372036854775809', '9' * 5000]
        for initial, operand, wrapper, branch in itertools.product(
                ['true', 'false'], operands, ['', 'command ', 'eval '],
                ['GO;else NODE', 'NODE;else GO']):
            self.compare('run(){ ' + initial + ';' + wrapper + 'return ' + operand +
                         ';NODE; };if run;then ' + branch + ';fi;GO')

    def test_bash_return_arity_error_precedes_invalid_context(self):
        for wrapper, context, prefix in itertools.product(
                ['', 'command ', 'eval '], ['top', 'function', 'source'], ['', 'GO;']):
            call = wrapper + 'return 0 extra;NODE'
            if context == 'function':
                text, helpers = prefix + 'run(){ ' + call + '; };if run;then NODE;else NODE;fi', None
            elif context == 'source':
                text, helpers = prefix + '. /action/helper.sh;NODE', {'helper.sh': call}
            else:
                text, helpers = prefix + call, None
            self.compare(text, helpers=helpers, returncode=1)
        for wrapper, mode in itertools.product(['', 'command ', 'eval '], ['', 'set -o posix;']):
            fatal = bool(mode) and wrapper != 'command '
            self.compare('GO;' + mode + wrapper + 'return invalid extra;NODE', returncode=2 if fatal else 0)

    def test_bash_exit_statuses_reach_parent_conditions_and_traps(self):
        for operand, wrapper in itertools.product(
                ['-1', '+1', '-256', '256', '257', 'invalid', '0 extra', 'invalid extra',
                 '--', '-- 0 extra', '" 1 "', '9223372036854775808'],
                ['', 'command ', 'eval ']):
            self.compare('if (true;' + wrapper + 'exit ' + operand + ';NODE);then GO;else NODE;fi;GO')
            self.compare('trap GO EXIT;false;' + wrapper + 'exit ' + operand,
                         returncode={'-1': 255, '+1': 1, '-256': 0, '256': 0, '257': 1,
                                     'invalid': 2, '0 extra': 1, 'invalid extra': 2, '--': 1,
                                     '-- 0 extra': 1, '" 1 "': 1, '9223372036854775808': 2}[operand])

    def test_bash_return_and_exit_expose_normalized_status_values(self):
        for operand, expected in [('-1', '255'), ('+1', '1'), ('256', '0'), ('257', '1'),
                                  ('invalid', '2'), ('" 7 "', '7'), ('-- 258', '2')]:
            self.compare('run(){ return ' + operand + '; };run;'
                         'if test "$?" = ' + expected + ';then GO;else NODE;fi')
            self.compare('(exit ' + operand + ');if test "$?" = ' + expected + ';then GO;else NODE;fi')

    def test_out_of_loop_bash_jumps_ignore_invalid_operands_before_errexit(self):
        for command, operand, wrapper, mode in itertools.product(
                ['break', 'continue'], ['invalid', '0', '-1', '1 2', '-- 0', '9' * 5000],
                ['', 'command ', 'eval '], ['', 'set -e;', 'set -o posix;']):
            self.compare(mode + wrapper + command + ' ' + operand + ';GO')
            self.compare(mode + 'GO;' + wrapper + command + ' ' + operand + ';NODE')

    def test_non_bash_jump_operands_preserve_fatal_and_command_status(self):
        for dialect, command, operand, wrapper in itertools.product(
                ['dash', 'ash'], ['break', 'continue'], ['invalid', '0', '-1', '1 2'], ['', 'command ']):
            invalid = operand != '1 2'
            self.compare('GO;' + wrapper + command + ' ' + operand + ';NODE',
                         dialects=(dialect,), returncode=2 if invalid and not wrapper else 0)

    def test_other_shell_return_and_exit_operands_have_their_own_numeric_rules(self):
        for dialect, builtin, operand, wrapper in itertools.product(
                ['dash', 'ash'], ['return', 'exit'],
                ['-1', '+1', '256', '257', 'invalid', '0 extra', '" 1 "',
                 '2147483648', '4294967295', '9223372036854775808'], ['', 'command ']):
            invalid = operand in ['-1', 'invalid'] or operand in ['+1', '" 1 "'] and dialect == 'ash'
            invalid = invalid or dialect == 'dash' and operand in ['2147483648', '4294967295', '9223372036854775808']
            # The production analyser correctly treats arbitrary process exit
            # statuses as unknown. Pin the fall-through status explicitly so
            # only the builtin's control transfer selects the parent branch.
            call = wrapper + builtin + ' ' + operand + ';NODE;true'
            # A child process lets the parent record both error and success
            # without making a shell-specific failure the test's exit status.
            if builtin == 'return':
                call = 'run(){ ' + call + '; };run'
            self.compare('if (' + call + ');then GO;else NODE;fi;GO', dialects=(dialect,))
            if invalid and wrapper:
                self.compare(call + ';GO', dialects=(dialect,))

    def test_bash_startup_return_bypasses_operand_errors(self):
        for operand in ['-1', 'invalid', '0 extra', '-- 7']:
            self.compare('BASH_ENV=/action/startup.sh bash --noprofile --norc -c '
                         + shlex.quote('if test "$?" = 1;then GO;else NODE;fi'),
                         helpers={'startup.sh': 'false;return ' + operand + ';NODE'})

    def test_help_does_not_transfer_control_and_unknown_status_fails_closed(self):
        for command in ['return', 'exit']:
            self.compare('run(){ ' + command + ' --help;GO; };run;NODE')
            from regressions.test_verifier_boundaries import DOCKER_ACTION
            (self.root / 'entry.sh').write_text('#!/bin/bash\n/action/release-action;' + command + ' "$UNKNOWN_STATUS";node\n')
            inspected = inspect_action(self.root, DOCKER_ACTION)
            self.assertTrue(inspected['unresolved'], inspected)
            self.assertFalse(inspected['go'], inspected)

    def test_unidentified_shell_accepts_portable_status_and_rejects_ambiguous_extensions(self):
        from regressions.test_verifier_boundaries import DOCKER_ACTION
        docker = self.root / 'Dockerfile'
        docker.write_text(docker.read_text().replace('FROM alpine:3.20', 'FROM custom-image:1'))
        for operand in ['-1', '+1', 'invalid', '256', '0 extra']:
            (self.root / 'entry.sh').write_text('#!/bin/sh\n/action/release-action;exit ' + operand + ';node\n')
            inspected = inspect_action(self.root, DOCKER_ACTION)
            self.assertTrue(inspected['unresolved'], inspected)
            self.assertFalse(inspected['go'], inspected)
        (self.root / 'entry.sh').write_text('#!/bin/sh\n/action/release-action;exit 0;node\n')
        inspected = inspect_action(self.root, DOCKER_ACTION)
        self.assertEqual(inspected['unresolved'], [], inspected)
        self.assertTrue(inspected['go'], inspected)
        self.assertEqual(inspected['source_runtime_hits'], [], inspected)


class RepairFollowupTests(unittest.TestCase):
    oracle_results = []
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    compare = CommandResolutionTests.compare

    def test_bash_append_declarations_preserve_success_and_values(self):
        for builtin, wrapper, mode in itertools.product(
                ['export', 'readonly', 'local'], ['', 'command ', 'eval '], ['', 'set -o posix;']):
            for branch in ['GO;else NODE', 'NODE;else GO']:
                body = 'VALUE=no;if ' + wrapper + builtin + ' VALUE+=de;then ' + branch + ';fi;GO'
                if builtin == 'local':
                    body = 'run(){ ' + body + '; };run'
                self.compare(mode + body)
            body = ('VALUE=no;' + wrapper + builtin + ' VALUE+=de;'
                    'if test "$VALUE" = node;then NODE;else GO;fi;GO')
            if builtin == 'local':
                body = 'run(){ ' + body + '; };run'
            self.compare(mode + body)

    def test_append_declaration_expansion_and_temporary_bindings(self):
        for builtin in ['export', 'readonly', 'local']:
            for mode in ['', 'set -o posix;']:
                body = 'VALUE=no;TAIL="d e";' + builtin + ' VALUE+=$TAIL;'
                body += 'if test "$VALUE" = "nod e";then GO;else NODE;fi'
                if builtin == 'local':
                    body = 'run(){ ' + body + '; };run'
                self.compare(mode + body)
            body = 'VALUE=outer;VALUE=no ' + builtin + ' VALUE+=de;'
            body += 'if test "$VALUE" = node;then NODE;else GO;fi;GO'
            if builtin == 'local':
                body = 'run(){ ' + body + '; };run'
            self.compare(body)
        self.compare('unset VALUE;export VALUE+=node;'
                     'if test "$VALUE" = node;then NODE;else GO;fi;GO')
        self.compare('run(){ local VALUE=no;TAIL="d e";local VALUE+=$TAIL;'
                     'if test "$VALUE" = "nod e";then GO;else NODE;fi; };run')
        self.compare('VALUE=no;readonly VALUE;export VALUE+=de;'
                     'if test "$VALUE" = no;then GO;else NODE;fi')

    def test_local_index_zero_declarations_keep_status_and_value(self):
        for operand, wrapper in itertools.product(['VALUE[0]', 'VALUE[0]=node', 'VALUE[0]+=node'],
                                                  ['', 'command ', 'eval ']):
            self.compare('run(){ if ' + wrapper + 'local ' + operand + ';then GO;else NODE;fi; };run')
            if '=' in operand:
                self.compare('run(){ unset VALUE;' + wrapper + 'local ' + operand + ';'
                             'if test "$VALUE" = node;then NODE;else GO;fi;GO; };run')
        self.compare('run(){ TAIL="n o";local VALUE[0]=$TAIL;'
                     'if test "$VALUE" = "n o";then GO;else NODE;fi; };run')

    def test_builtin_help_obeys_posix_error_and_conditional_context(self):
        for builtin, mode, wrapper, context in itertools.product(
                ['return', 'exit', 'break', 'continue'], ['', 'set -o posix;'],
                ['', 'command ', 'eval ', 'command eval ', 'eval command ', 'eval command eval '],
                ['plain', 'condition', 'function_condition', 'loop']):
            call = wrapper + builtin + ' --help'
            if context == 'plain':
                body = call + ';NODE'
            elif context == 'condition':
                body = 'if ' + call + ';then GO;else NODE;fi'
            elif context == 'function_condition':
                body = 'run(){ ' + call + ';NODE; };if run;then :;fi'
            else:
                body = 'for x in a;do ' + call + ';NODE;done'
            fatal = (bool(mode) and wrapper not in {'command ', 'eval command '}
                     and (context in {'plain', 'loop'} or 'eval ' in wrapper))
            self.compare('GO;' + mode + body, returncode=2 if fatal else 0)
        self.compare('set -o posix;eval :;if return --help;then GO;else NODE;fi;GO')

    def test_non_bash_loop_counts_validate_sign_and_range_before_context(self):
        for dialect, builtin, operand, wrapper in itertools.product(
                ['dash', 'ash'], ['break', 'continue'],
                ['+1', '01', '1 extra', '2147483647', '2147483648', '4294967295'],
                ['', 'command ', 'eval ']):
            invalid = operand in ['2147483648', '4294967295'] or dialect == 'ash' and operand == '+1'
            fatal = invalid and wrapper != 'command '
            self.compare('GO;' + wrapper + builtin + ' ' + operand + ';NODE',
                         dialects=(dialect,), returncode=2 if fatal else 0)
            self.compare(wrapper + builtin + ' ' + operand + ';GO',
                         dialects=(dialect,), returncode=2 if fatal else 0)

    def test_help_conditions_inside_eval_and_errexit_inhibition(self):
        for builtin, wrapper in itertools.product(
                ['return', 'exit', 'break', 'continue'], ['eval ', 'command eval ']):
            for body in ['if BUILTIN --help;then GO;else NODE;fi;GO',
                         'BUILTIN --help || NODE;GO',
                         'run(){ BUILTIN --help;NODE;true; };if run;then GO;fi']:
                self.compare('set -o posix;' + wrapper + shlex.quote(body.replace('BUILTIN', builtin)))
            # An eval call tested by if still inhibits errexit for ordinary
            # failures, even though fatal special-builtin errors differ.
            self.compare('set -e;set -o posix;if ' + wrapper + shlex.quote('false;NODE;GO') + ';then :;fi')

    def test_active_loop_extra_arguments_follow_each_interpreter(self):
        for dialect, builtin, wrapper in itertools.product(
                ['bash', 'dash', 'ash'], ['break', 'continue'], ['', 'command ', 'eval ']):
            body = 'GO;for x in a;do ' + wrapper + builtin + ' 1 extra;NODE;done;GO'
            self.compare(body, dialects=(dialect,), returncode=1 if dialect == 'bash' else 0)

    def test_bash_loop_option_terminators_and_unknown_sh_outside_loops(self):
        from regressions.test_verifier_boundaries import DOCKER_ACTION
        for builtin, operand, wrapper in itertools.product(
                ['break', 'continue'], ['--', '-- 1'], ['', 'command ', 'eval ']):
            self.compare('GO;for x in a;do ' + wrapper + builtin + ' ' + operand + ';NODE;done;GO')
        docker = self.root / 'Dockerfile'
        docker.write_text(docker.read_text().replace('FROM alpine:3.20', 'FROM unknown-shell-image'))
        for builtin in ['break', 'continue']:
            for operand in ['', ' 1', ' 2', ' 01', ' 2147483647', ' 1 extra']:
                (self.root / 'entry.sh').write_text('#!/bin/sh\n/action/release-action;' + builtin + operand + ';node\n')
                inspected = inspect_action(self.root, DOCKER_ACTION)
                self.assertEqual(inspected['unresolved'], [], inspected)
                self.assertTrue(inspected['go'], inspected)
                self.assertEqual(len(inspected['source_runtime_hits']), 1, inspected)
            for body in [builtin + ' +1', builtin + ' 2147483648',
                         'for x in a;do ' + builtin + ' 1 extra;done']:
                (self.root / 'entry.sh').write_text('#!/bin/sh\n/action/release-action;' + body + ';node\n')
                inspected = inspect_action(self.root, DOCKER_ACTION)
                self.assertTrue(inspected['unresolved'], inspected)
                self.assertFalse(inspected['go'], inspected)


if __name__ == '__main__':
    unittest.main()
