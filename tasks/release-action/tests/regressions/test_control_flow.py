"""Current-shell eval, pipeline status, loop budgets and lexical Go bindings.

Shell oracles run only authored snippets with inert Go/Node marker stubs.
Go checks are lexical detector tests; the exact compilable audit cases are
replayed separately by the repair verification script.
"""
import itertools
import unittest

from regressions import test_shell_bindings as prior
from regressions import test_shell_execution
from shell_syntax import parse


class ShellControlFlowTests(unittest.TestCase):
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    inspect = test_shell_execution.ShellExecutionTests.inspect
    accepts = test_shell_execution.ShellExecutionTests.accepts
    detects = test_shell_execution.ShellExecutionTests.detects
    compare_shell = prior.ShellBindingTests.compare_shell

    def test_eval_assignments_and_functions_share_the_current_shell(self):
        for text in [
            "eval 'BIN=\"$(dirname \"$0\")/release-action\"'\nexec \"$BIN\"",
            "eval 'RUNTIME=node'\n\"$RUNTIME\" dist/index.js\n/action/release-action",
            "run_action() { exec /action/release-action; }\neval run_action",
            "run_action() { node dist/index.js; }\neval run_action\n/action/release-action",
            "eval 'run_action() { /action/release-action; }'\nrun_action",
            "eval 'run_action() { node dist/index.js; }'\nrun_action\n/action/release-action",
            "eval 'eval '\"'\"'RUNTIME=node'\"'\"''\n\"$RUNTIME\" dist/index.js\n/action/release-action",
        ]:
            # The first case relies on Docker's $0; the remaining cases can
            # also be compared to /bin/sh without an actual action binary.
            with self.subTest(text=text):
                if 'dirname' in text:
                    self.accepts(text)
                else:
                    self.compare_shell(text)

    def test_eval_preserves_cwd_positional_arguments_and_function_locals(self):
        self.accepts("eval 'cd /'\neval 'cd action'\nexec ./release-action")
        for text in [
            "run() { eval '\"$1\" dist/index.js'; }\nrun node\n/action/release-action",
            "RUNTIME=true\nrun() { eval 'local RUNTIME=node'; :; }\nrun\n\"$RUNTIME\"\n/action/release-action",
            "RUNTIME=true\nrun() { local RUNTIME; eval 'RUNTIME=node'; }\nrun\n\"$RUNTIME\"\n/action/release-action",
            "RUNTIME=true\nrun() { eval 'RUNTIME=node'; }\nrun\n\"$RUNTIME\"\n/action/release-action",
            "RUNTIME=true\neval 'RUNTIME=node' | true\n\"$RUNTIME\"\n/action/release-action",
            "RUNTIME=true\nVALUE=\"$(eval 'RUNTIME=node'; printf safe)\"\n\"$RUNTIME\"\n/action/release-action",
        ]:
            with self.subTest(text=text):
                self.compare_shell(text)

    def test_eval_control_transfers_and_exit_status_are_not_lost(self):
        for text in [
            "if eval false; then node dist/index.js; fi\n/action/release-action",
            "if eval true; then node dist/index.js; fi\n/action/release-action",
            "if eval ''; then /action/release-action; else node dist/index.js; fi",
            "run() { eval 'return 0'; node dist/index.js; }\nrun\n/action/release-action",
            "while true; do eval break; node dist/index.js; done\n/action/release-action",
            "for item in first second; do eval continue; node dist/index.js; done\n/action/release-action",
            "eval 'exec /action/release-action'\nnode dist/index.js",
        ]:
            with self.subTest(text=text):
                self.compare_shell(text)
        self.assertFalse(self.inspect("eval 'exit 0'\n/action/release-action")['go'])

    def test_eval_readonly_recursion_and_unknown_programs_fail_closed(self):
        for text in [
            "readonly RUNTIME=true\neval 'RUNTIME=node'\n/action/release-action",
            "/action/release-action\nrun() { eval run; }\nrun",
            "/action/release-action\neval \"$UNRESOLVED\"",
            "/action/release-action\neval 'if true; then'",
        ]:
            with self.subTest(text=text):
                inspected = self.inspect(text)
                self.assertFalse(inspected['go'], inspected)
                self.assertTrue(inspected['unresolved'], inspected)
                self.assertEqual(inspected['source_runtime_hits'], [])

    def test_command_eval_uses_current_state_but_bypasses_function_lookup(self):
        for text in [
            "command eval 'RUNTIME=node'\n\"$RUNTIME\"\n/action/release-action",
            "command -p eval 'BIN=/action/release-action'\nexec \"$BIN\"",
            "eval() { node dist/index.js; }\ncommand eval ':'\n/action/release-action",
            "run() { /action/release-action; }\ncommand eval run",
            "node() { true; }\ncommand -- node dist/index.js\n/action/release-action",
            "command() { node dist/index.js; }\ncommand\n/action/release-action",
            "command() { node dist/index.js; }\ncommand eval ':'\n/action/release-action",
        ]:
            with self.subTest(text=text):
                # Defining a function named eval is not permitted by dash.
                if 'eval()' in text:
                    self.accepts(text)
                else:
                    self.compare_shell(text)

        self.compare_shell('command ' * 1100 + "eval 'BIN=/action/release-action'\nexec \"$BIN\"")

    def test_negation_uses_the_entire_pipeline_status(self):
        for count in [1, 2, 3]:
            for commands in itertools.product(['true', 'false'], repeat=count):
                for negate in ['', '! ']:
                    text = 'if ' + negate + ' | '.join(commands) + '; then node dist/index.js; fi\n/action/release-action'
                    with self.subTest(text=text):
                        self.compare_shell(text)

    def test_negation_composes_with_groups_substitutions_and_conditionals(self):
        for condition in [
            '! { true | false; }', '! (false | true)',
            '! true | false && true', '! false | true || true',
            '! true | false | true', '! false | true | false',
        ]:
            with self.subTest(condition=condition):
                self.compare_shell('if ' + condition + '; then node dist/index.js; fi\n/action/release-action')
        self.detects('VALUE="$(if ! true | false; then node dist/index.js; fi)"\n/action/release-action')
        self.accepts('VALUE="$(if ! false | true; then node dist/index.js; fi)"\n/action/release-action')
        self.detects('! node dist/index.js | false\n/action/release-action')

    def test_quoted_escaped_and_expanded_bang_are_command_words(self):
        for word in ["'!'", '"!"', r'\!']:
            with self.subTest(word=word):
                self.assertEqual(parse(word + ' node dist/index.js')[1][0][0], 'cmd')
                self.accepts(word + ' node dist/index.js\n/action/release-action')
        self.accepts('BANG=!\n$BANG node dist/index.js\n/action/release-action')
        self.assertEqual(parse('! true | false')[1][0][0], 'not')

    def test_finite_while_and_until_loops_cross_the_old_budget(self):
        for length in [1, 2, 15, 16, 17, 18, 32, 64]:
            for kind, operator in [('while', '!='), ('until', '=')]:
                loop = 'FLAG=x\n' + kind + ' test "$FLAG" ' + operator + ' ' + 'x' * length + '; do FLAG="${FLAG}x"; done\n'
                for tail in ['/action/release-action', 'node dist/index.js\n/action/release-action']:
                    with self.subTest(length=length, kind=kind, tail=tail):
                        self.compare_shell(loop + tail)

    def test_loop_budget_and_proven_cycles_do_not_retain_earlier_go_credit(self):
        long_loop = 'FLAG=x\nwhile test "$FLAG" != ' + 'x' * 160 + '; do FLAG="${FLAG}x"; done'
        for loop in [long_loop, 'while true; do continue; done',
                     'FLAG=x\nwhile true; do FLAG=y; FLAG=x; done']:
            for prefix in ['', '/action/release-action --help\n']:
                with self.subTest(loop=loop, prefix=prefix):
                    inspected = self.inspect(prefix + loop + '\nnode dist/index.js\n/action/release-action')
                    self.assertFalse(inspected['go'], inspected)
                    self.assertTrue(inspected['unresolved'], inspected)
                    self.assertEqual(inspected['source_runtime_hits'], [])
        for wrapper in ['(%s)', 'VALUE="$(%s)"', 'eval \'%s\'',
                        '(%s) | true', 'true | (%s)', '(%s) | /action/release-action']:
            inspected = self.inspect(wrapper % 'while true; do :; done' + '\n/action/release-action')
            self.assertFalse(inspected['go'], inspected)

    def test_nested_loop_control_and_eval_updates_reach_the_expected_tail(self):
        for body in ['eval \'FLAG="${FLAG}x"\'',
                     'for inner in first second; do FLAG="${FLAG}x"; break; done',
                     'FLAG="${FLAG}x"; continue; node dist/index.js']:
            with self.subTest(body=body):
                self.compare_shell('FLAG=x\nwhile test "$FLAG" != ' + 'x' * 18 + '; do ' + body + '; done\nnode dist/index.js\n/action/release-action')


class GoControlScopeTests(unittest.TestCase):
    scan = test_shell_execution.PluginScopeTests.scan

    def test_outer_assignments_survive_one_or_multiple_ordinary_blocks(self):
        for initial, assigned in [('safe', 'p.Open'), ('p.Open', 'safe')]:
            for block in ['{open=TARGET}', '{{open=TARGET}}', '{open=TARGET; {other:=0; _=other}}']:
                code = 'func f(){open:=' + initial + ';' + block.replace('TARGET', assigned) + ';open("node")}'
                with self.subTest(code=code):
                    self.assertEqual(bool(self.scan(code)), assigned == 'p.Open')

    def test_shadow_declarations_restore_but_outer_mutations_survive(self):
        for declaration in ['open:=safe', 'var open=safe', 'var (open=safe)']:
            self.assertTrue(self.scan('func f(){open:=p.Open; {' + declaration + '; open("node")}; open("node")}'))
            self.assertEqual(self.scan('func f(){open:=safe; {' + declaration.replace('safe', 'p.Open') + '};open("node")}'), [])
        self.assertTrue(self.scan('func f(){loader:=p.Open;open:=safe; {var p Local; open=loader};open("node")}'))
        self.assertEqual(self.scan('func f(){open:=p.Open; {p:=Local{};open=p.Open};open("node")}'), [])

    def test_shadowing_after_an_outer_assignment_restores_the_mutated_binding(self):
        for initial, assigned in [('safe', 'p.Open'), ('p.Open', 'safe')]:
            for declaration in ['open:=safe', 'var open=safe', 'open,other:=safe,0;_=other']:
                code = 'func f(){open:=' + initial + ';{open=' + assigned + ';' + declaration + '};open("node")}'
                with self.subTest(code=code):
                    self.assertEqual(bool(self.scan(code)), assigned == 'p.Open')
        self.assertTrue(self.scan('func f(){name:="safe";{name="node";name:="safe";_=name};p.Open(name)}'))
        self.assertEqual(self.scan('func f(){name:="node";{name="safe";name:="node";_=name};p.Open(name)}'), [])

    def test_short_redeclarations_and_simultaneous_assignments_use_old_rhs(self):
        self.assertTrue(self.scan('func f(){open:=safe; {open=p.Open};open,other:=open,0;open("node");_=other}'))
        self.assertEqual(self.scan('func f(){open:=p.Open; {open=safe};open,other:=open,0;open("node");_=other}'), [])
        self.assertTrue(self.scan('func f(){open:=safe; other:=p.Open; {open,other=other,open};open("node")}'))
        self.assertEqual(self.scan('func f(){open:=p.Open; other:=safe; {open,other=other,open};open("node")}'), [])
        self.assertTrue(self.scan('func f(){open:=safe; {obj.field,open=0,p.Open};open("node")}'))
        self.assertEqual(self.scan('func f(){open:=p.Open; {values[0],open=0,safe};open("node")}'), [])

    def test_string_assignments_survive_but_new_shadowed_strings_restore(self):
        self.assertTrue(self.scan('func f(){name:="safe";{name="node"};p.Open(name)}'))
        self.assertEqual(self.scan('func f(){name:="node";{name="safe"};p.Open(name)}'), [])
        self.assertTrue(self.scan('func f(){name:="node";{name:="safe";_=name};p.Open(name)}'))
        self.assertEqual(self.scan('func f(){name:="safe";{name:="node";_=name};p.Open(name)}'), [])

    def test_conditional_blocks_do_not_apply_inactive_or_last_scanned_arm(self):
        for condition, expected in [('true', False), ('false', True), ('!true', True), ('!false', False)]:
            with self.subTest(condition=condition):
                self.assertEqual(bool(self.scan('func f(){open:=p.Open;if ' + condition + ' {open=safe};open("node")}')), expected)
                self.assertEqual(bool(self.scan('func f(){open:=safe;if ' + condition + ' {open=p.Open};open("node")}')), not expected)
                self.assertEqual(bool(self.scan('func f(){open:=p.Open;if ' + condition + ' {open=safe}else{open=p.Open};open("node")}')), expected)
        self.assertTrue(self.scan('func f(){open:=p.Open;if unknown {open=safe};open("node")}'))
        self.assertTrue(self.scan('func f(){open:=p.Open;for range values {open=safe};open("node")}'))
        self.assertEqual(self.scan('func f(){open:=safe;if false {{open=p.Open};open("node")};open("node")}'), [])
        self.assertEqual(self.scan('func f(){if true {}else{p.Open("node")}}'), [])
        self.assertTrue(self.scan('func f(){open:=safe;if false {open=safe}else if true {open=p.Open};open("node")}'))
        self.assertTrue(self.scan('var true=false\nfunc f(){open:=p.Open;if true {open=safe};open("node")}'))
        self.assertTrue(self.scan('func f(true bool){open:=p.Open;if true {open=safe};open("node")}'))

    def test_function_definitions_and_closures_do_not_apply_outer_writes(self):
        for body in ['func(){open=safe}', 'func(p Local){open=safe;p.Open("node")}']:
            self.assertTrue(self.scan('func f(){open:=p.Open;fn:=' + body + ';_=fn;open("node")}'))
        self.assertEqual(self.scan('var open=safe\nfunc f(){open=p.Open}\nfunc g(){open("node")}'), [])
        self.assertTrue(self.scan('var open=p.Open\nfunc f(){open=safe}\nfunc g(){open("node")}'))

    def test_range_anonymous_types_and_composites_keep_the_header_scope(self):
        expressions = [
            '[]Local{{}}', '[1]Local{{}}', '[...]Local{{}}',
            '[]struct{Local}{{}}', '[1]struct{Local}{{}}',
            'map[int]struct{Local}{0:{}}',
            '[]struct{Local; Values []struct{ X int }}{{}}',
            '([]struct{Local}{{}})', '[][]struct{Local}{{{}}}',
            'func() []struct{Local} {return []struct{Local}{{}}}()',
        ]
        for expression in expressions:
            # Only the struct-valued expressions select the Local method;
            # all forms nevertheless have the same lexical range scope.
            body = 'for _,p:=range ' + expression + ' {p.Open("node")}'
            with self.subTest(expression=expression):
                self.assertEqual(self.scan('func f(){' + body + '}'), [])
                self.assertTrue(self.scan('func f(){' + body + ';p.Open("node")}'))

    def test_range_scope_nesting_and_initializers_preserve_real_loads(self):
        self.assertTrue(self.scan('func f(){for _,p:=range []struct{Local}{{}} {_=p};p.Open("node")}'))
        self.assertTrue(self.scan('func f(){for _,p:=range values(p.Open("node")) {p.Open("safe")}}'))
        self.assertEqual(self.scan('func f(){for _,p:=range []struct{Local}{{}} {for _,x:=range []struct{Local}{{}} {_=x};p.Open("node")}}'), [])
        self.assertEqual(self.scan('func f(){for _,p:=range []struct{Local}{{}} {if p:=Local{};true{p.Open("node")};p.Open("node")}}'), [])


if __name__ == '__main__':
    unittest.main()
