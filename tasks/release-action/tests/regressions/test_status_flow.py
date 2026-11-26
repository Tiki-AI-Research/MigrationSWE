"""Paired shell status/parameter oracles and Go expression-scope checks."""
import unittest

from regressions import test_shell_bindings as prior
from regressions import test_shell_execution


class ShellStatusTests(unittest.TestCase):
    setUp = test_shell_execution.ShellExecutionTests.setUp
    tearDown = test_shell_execution.ShellExecutionTests.tearDown
    inspect = test_shell_execution.ShellExecutionTests.inspect
    compare_shell = prior.ShellBindingTests.compare_shell

    def test_while_and_until_return_last_body_status_or_zero(self):
        for count in [0, 1, 2, 17]:
            for kind, operator in [('while', '!='), ('until', '=')]:
                for body in ['true', 'false']:
                    for previous in ['true', 'false']:
                        text = ('FLAG=""\n' + previous + '\nif ' + kind + ' test "$FLAG" ' + operator +
                                ' "' + 'x' * count + '"; do FLAG="${FLAG}x"; ' + body +
                                '; done; then node dist/index.js; fi\n/action/release-action')
                        with self.subTest(count=count, kind=kind, body=body, previous=previous):
                            self.compare_shell(text)

    def test_for_loop_zero_iterations_and_control_transfers_return_zero(self):
        for values in ['', 'one', 'one two']:
            for body in ['true', 'false', 'false; break', 'false; continue']:
                with self.subTest(values=values, body=body):
                    self.compare_shell('false\nif for item in ' + values + '; do ' + body +
                                       '; done; then node dist/index.js; fi\n/action/release-action')

    def test_loop_break_continue_and_nesting_preserve_body_status(self):
        for loop in ['while true; do false; break; done',
                     'until false; do false; break; done',
                     'while break; do :; done', 'until break; do :; done',
                     'while test "$FLAG" != xx; do FLAG="${FLAG}x"; false; continue; done',
                     'until test "$FLAG" = xx; do FLAG="${FLAG}x"; false; continue; done',
                     'while test "$FLAG" != xx; do FLAG="${FLAG}x"; for item in; do false; done; done']:
            with self.subTest(loop=loop):
                self.compare_shell('FLAG=""\nif ' + loop + '; then node dist/index.js; fi\n/action/release-action')
        for loop in ['while continue; do node; done', 'until continue; do node; done']:
            inspected = self.inspect('/action/release-action\n' + loop + '\nnode')
            self.assertFalse(inspected['go'], inspected)
            self.assertEqual(inspected['source_runtime_hits'], [])
        self.compare_shell('RUNTIME=true\nrun(){ for RUNTIME in node true; do return 0; done; }\nrun\n"$RUNTIME"\n/action/release-action')

    def test_assignment_only_commands_use_the_last_substitution_status(self):
        for expression in ['VALUE="$(false)"', 'VALUE="$(true)"',
                           'A="$(true)" B="$(false)"', 'A="$(false)" B="$(true)"',
                           'A="$(false)$(true)"', 'A="$(true)$(false)"',
                           'A="${MISSING:-$(false)}"', 'A="$(VALUE=\"$(false)\")"',
                           'A="$(if false; then :; fi)"',
                           'A="$(false)" true', ': "$(false)"', 'export A="$(false)"',
                           'eval \'A="$(false)"\'', 'eval \'A="$(true)"\'']:
            with self.subTest(expression=expression):
                self.compare_shell('if ' + expression + '; then node dist/index.js; fi\n/action/release-action')

    def test_empty_if_and_case_results_do_not_inherit_a_previous_failure(self):
        for expression in ['if false; then :; fi', 'case literal in other) :;; esac',
                           'if false; then :; elif false; then :; fi',
                           'if true; then false; fi', 'case literal in literal) false;; esac']:
            with self.subTest(expression=expression):
                self.compare_shell('false\nif ' + expression + '; then node dist/index.js; fi\n/action/release-action')

    def test_eval_and_command_set_replace_and_clear_positional_parameters(self):
        for expression in ["eval 'set -- node'", 'set node', 'command set -- node',
                           'set -- true; set -- node', 'set -- true; eval \'set -- node\'']:
            with self.subTest(expression=expression):
                self.compare_shell(expression + '\n"$@" dist/index.js\n/action/release-action')
        self.compare_shell("eval 'set -- /action/release-action'\nexec \"$@\"")
        for expression in ['set -- node; set --', 'set -- node; eval \'set --\'']:
            self.compare_shell(expression + '\nfor item in "$@"; do "$item"; done\n/action/release-action')

    def test_set_count_star_and_at_follow_current_ifs(self):
        self.compare_shell('set -- node\nif test "$#" = 1; then "$1"; fi\n/action/release-action')
        self.compare_shell('set -- node;set --\nif test "$#" = 0; then /action/release-action; else node; fi')
        for separator in ['', ':', ' ', ' :']:
            for expansion in ['$@', '"$@"', '$*', '"$*"']:
                with self.subTest(separator=separator, expansion=expansion):
                    self.compare_shell('IFS="' + separator + '"\nset -- "true node" node ""\nfor item in ' +
                                       expansion + '; do case "$item" in node) node;; esac; done\n/action/release-action')

    def test_set_in_functions_subshells_and_substitutions_preserves_scope(self):
        for text in [
            'set -- true\nrun(){ eval \'set -- node\'; "$@"; }\nrun\n"$@"\n/action/release-action',
            'set -- true\n(set -- node)\n"$@"\n/action/release-action',
            'set -- true\nVALUE="$(eval \'set -- node\'; printf value)"\n"$@"\n/action/release-action',
            'set -- true\nrun(){ set -- node; }\nrun\n"$@"\n/action/release-action',
            'set -- true\nrun(){ set --; for item in "$@"; do node; done; }\nrun\n"$@"\n/action/release-action',
        ]:
            with self.subTest(text=text):
                self.compare_shell(text)

    def test_set_option_operands_are_not_positional_parameters(self):
        for setup in ['set -e -- node', 'set -eu -- node', 'set -o errexit -- node',
                      'set +o errexit -- node', 'set -- node; set -', 'set -- true; set - node']:
            with self.subTest(setup=setup):
                self.compare_shell(setup + '\n"$@"\n/action/release-action')

    def test_status_composes_with_eval_negation_and_pipelines(self):
        for condition in ["eval 'while false; do :; done'", '! until true; do false; done',
                          'true | while false; do :; done', 'while false; do :; done | false',
                          'A="$(false)" || true', 'A="$(false)" && true']:
            with self.subTest(condition=condition):
                self.compare_shell('if ' + condition + '; then node; fi\n/action/release-action')
        for code in [0, 1, 2, 255, 256, 257, 512]:
            self.compare_shell('run(){ return ' + str(code) + '; }\nif run; then node; fi\n/action/release-action')
            self.compare_shell('if (exit ' + str(code) + '); then node; fi\n/action/release-action')
            self.compare_shell('run(){ return ' + str(code) + '; }\nif VALUE="$(run)"; then node; fi\n/action/release-action')
        self.compare_shell('run(){ ! return 1; }\nif run; then node; fi\n/action/release-action')
        self.compare_shell('if VALUE="$(! exit 1)"; then node; fi\n/action/release-action')


class GoExpressionTests(unittest.TestCase):
    scan = test_shell_execution.PluginScopeTests.scan

    def test_control_initializers_mutate_outer_bindings_before_unknown_conditions(self):
        for initial, assigned in [('safe', 'p.Open'), ('p.Open', 'safe')]:
            for control in ['if open=TARGET; flag() {}', 'for open=TARGET; flag(); {break}',
                            'switch open=TARGET; flag(){default:}']:
                code = 'func f(){open:=' + initial + ';' + control.replace('TARGET', assigned) + ';open("node")}'
                with self.subTest(code=code):
                    self.assertEqual(bool(self.scan(code)), assigned == 'p.Open')
        self.assertEqual(self.scan('func f(){open:=safe;if open:=p.Open;flag(){_=open};open("node")}'), [])
        self.assertTrue(self.scan('func f(){open:=p.Open;if open:=safe;flag(){_=open};open("node")}'))

    def test_parenthesized_and_negated_literal_conditions_select_the_right_arm(self):
        for condition, active in [('(true)', True), ('((true))', True), ('!(false)', True),
                                  ('!!true', True), ('!((!false))', False), ('((false))', False)]:
            with self.subTest(condition=condition):
                self.assertEqual(bool(self.scan('func f(){open:=safe;if ' + condition + '{open=p.Open};open("node")}')), active)
                self.assertEqual(bool(self.scan('func f(){open:=p.Open;if ' + condition + '{open=safe};open("node")}')), not active)
                self.assertEqual(bool(self.scan('func f(){if ' + condition + '{p.Open("node")}}')), active)

    def test_type_switch_headers_end_at_the_switch_body(self):
        for statement in ['switch p:=value.(type){case Local:p.Open("node")}',
                          'switch p:=value.(type){case Local:p.Open("node");default:_=p}']:
            with self.subTest(statement=statement):
                self.assertEqual(self.scan('func f(value interface{}){' + statement + '}'), [])
                self.assertTrue(self.scan('func f(value interface{}){' + statement + ';p.Open("node")}'))
                self.assertTrue(self.scan('func f(value interface{}){' + statement + ';for _,x:=range values{_=x};p.Open("node")}'))
        self.assertTrue(self.scan('func f(value interface{}){switch x:=value.(type){case Local:_=x;p.Open("node")}}'))

    def test_boolean_shadowing_is_local_to_the_function_or_package_binding(self):
        self.assertEqual(self.scan('func other(true bool){}\nfunc f(){open:=p.Open;if (true){open=safe};open("node")}'), [])
        self.assertEqual(self.scan('func other(){false:=true;_=false}\nfunc f(){if (false){p.Open("node")}}'), [])
        self.assertTrue(self.scan('var true=false\nfunc f(){open:=p.Open;if (true){open=safe};open("node")}'))
        self.assertTrue(self.scan('func f(true bool){open:=p.Open;if (true){open=safe};open("node")}'))

    def test_parenthesized_alias_values_and_callees_preserve_loader_identity(self):
        for target in ['p.Open', '(p.Open)', '((p.Open))']:
            for callee in ['open', '(open)', '((open))']:
                with self.subTest(target=target, callee=callee):
                    self.assertTrue(self.scan('func f(){open:=safe;{open=' + target + '};' + callee + '("node")}'))
                    self.assertEqual(self.scan('func f(){open:=p.Open;{open=(safe)};' + callee + '("node")}'), [])
        self.assertTrue(self.scan('func f(){(p.Open)("node")}'))
        self.assertEqual(self.scan('func f(p Local){(p.Open)("node")}'), [])

    def test_parenthesized_aliases_keep_initializer_and_shadow_scope_timing(self):
        self.assertTrue(self.scan('func f(){open,p:=(p.Open),Local{};(open)("node")}'))
        self.assertTrue(self.scan('func f(){var open=(p.Open);{open:=(safe);_=open};(open)("node")}'))
        self.assertEqual(self.scan('func f(){open:=safe;{open:=(p.Open);_=open};(open)("node")}'), [])
        self.assertTrue(self.scan('func f(){if open:=(p.Open);(true){(open)("node")}}'))


if __name__ == '__main__':
    unittest.main()
