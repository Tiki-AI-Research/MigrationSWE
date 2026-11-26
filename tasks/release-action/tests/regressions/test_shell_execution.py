"""Execution/quoting/scope regressions for the second verifier repair.

Both action-entry consumers use the same static parser. No candidate shell,
Node or Go program is executed by these unit tests.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TESTS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TESTS), str(TESTS / 'assets/shared/integration')]
from launch import inspect_action
from verifier.detectors.entrypoint import detect_entrypoint_source_runtime
from verifier.detectors.dynamic import detect_source_runtime_dynamic_load
from regressions.test_verifier_boundaries import DOCKER, DOCKER_ACTION, CONFIG


class ShellExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'action.yml').write_text(DOCKER_ACTION)
        (self.root / 'Dockerfile').write_text(DOCKER.replace('/action/release-action"]', '/action/entry.sh"]'))

    def tearDown(self):
        self.temp.cleanup()

    def inspect(self, text):
        (self.root / 'entry.sh').write_text(text)
        return inspect_action(self.root, DOCKER_ACTION)

    def accepts(self, text):
        result = self.inspect(text)
        self.assertTrue(result['go'], result)
        self.assertEqual(result['source_runtime_hits'], [], result)
        self.assertEqual(detect_entrypoint_source_runtime(self.root, CONFIG), [])

    def detects(self, text):
        result = self.inspect(text)
        self.assertTrue(result['source_runtime_hits'], result)
        self.assertTrue(detect_entrypoint_source_runtime(self.root, CONFIG))

    def test_subshell_and_brace_groups_launch_go(self):
        for text in [
            '(cd "$(dirname "$0")" && ./release-action)',
            '(cd /action; exec ./release-action)',
            '( ( /action/release-action ) )',
            '{ cd /action; /action/release-action; }',
            '(cd /elsewhere; true)\nexec /action/release-action',
            '(cd /action && go build -o /tmp/app .)\nexec /tmp/app',
        ]:
            with self.subTest(text=text):
                self.accepts(text)

    def test_groups_do_not_hide_node_or_turn_unreachable_go_into_evidence(self):
        for text in ['(node dist/index.js)\nexec /action/release-action',
                     '{ node dist/index.js; }\nexec /action/release-action',
                     '(/action/release-action)\nnode dist/index.js']:
            with self.subTest(text=text):
                self.detects(text)
        for text in ['(false && /action/release-action)',
                     '(exit 0; /action/release-action)',
                     'if false; then (/action/release-action); fi']:
            with self.subTest(text=text):
                self.assertFalse(self.inspect(text)['go'])

    def test_called_functions_and_nested_definitions_launch_go(self):
        for text in [
            'run_action() { exec /action/release-action "$@"; }\nrun_action value',
            'function run_action { /action/release-action; }\nrun_action',
            'run_action() ( /action/release-action )\nrun_action',
            'first() { second() { /action/release-action; }; second; }\nfirst',
            'run_action() { exec "$1"; }\nrun_action /action/release-action',
        ]:
            with self.subTest(text=text):
                self.accepts(text)

    def test_called_functions_detect_node_but_unused_functions_are_inactive(self):
        for text in [
            '/action/release-action --help\nrun_action() { exec node dist/index.js; }\nrun_action',
            'first() { second() { exec "$1" dist/index.js; }; second "$1"; }\nfirst node\n/action/release-action',
            'run_action() { node dist/index.js; }\nRESULT="$(run_action)"\n/action/release-action',
            'go() { exec node dist/index.js; }\ngo run .',
        ]:
            with self.subTest(text=text):
                self.detects(text)
        self.accepts('unused() { node dist/index.js; }\nexec /action/release-action')
        self.assertFalse(self.inspect('unused() { /action/release-action; }\necho done')['go'])
        self.accepts('node() { /action/release-action; }\nnode')
        self.detects('node() { /action/release-action; }\ncommand node dist/index.js')

    def test_function_return_only_stops_that_call_and_local_values_restore(self):
        self.accepts('run_action() { /action/release-action; return 0; node dist/index.js; }\nrun_action')
        self.detects('run_action() { /action/release-action; return 0; }\nrun_action\nnode dist/index.js')
        self.accepts('RUNTIME=git\nrun_action() { local RUNTIME=node; echo "$RUNTIME"; }\nrun_action\n"$RUNTIME" version\n/action/release-action')
        self.detects('RUNTIME=git\nrun_action() { RUNTIME=node; }\nrun_action\n"$RUNTIME" dist/index.js\n/action/release-action')
        self.accepts('RUNTIME=git\nRUNTIME=node printf "%s" "$RUNTIME"\n"$RUNTIME" version\n/action/release-action')

    def test_command_substitutions_execute_in_assignments_and_arguments(self):
        for text in [
            'RESULT="$(node dist/index.js)"\nexec /action/release-action',
            'echo "$(node dist/index.js)"\nexec /action/release-action',
            'RESULT=`node dist/index.js`\nexec /action/release-action',
            'RESULT="$(echo "$(node dist/index.js)")"\nexec /action/release-action',
            'RESULT="${MISSING:-$(node dist/index.js)}"\nexec /action/release-action',
            'RUNTIME="$(printf "%s" node)"\n"$RUNTIME" dist/index.js\n/action/release-action',
            'RESULT="$(/action/release-action)"\nnode dist/index.js',
            'CMD="$(printf "%s" "node dist/index.js")"\n$CMD\n/action/release-action',
            'CMD="node dist/index.js"\n$CMD\n/action/release-action',
            ': > "$(node dist/index.js)"\n/action/release-action',
        ]:
            with self.subTest(text=text):
                self.detects(text)

    def test_quoted_escaped_and_commented_substitution_text_is_inactive(self):
        for text in [
            "RESULT='$(node dist/index.js)'\nexec /action/release-action",
            "echo '`node dist/index.js`'\nexec /action/release-action",
            'echo "\\$(node dist/index.js)"\nexec /action/release-action',
            '# $(node dist/index.js)\nexec /action/release-action',
            'echo "node dist/index.js"\nexec /action/release-action',
            'VALUE="$(printf "%s" "node dist/index.js")"\necho "$VALUE"\n/action/release-action',
            'VALUE=yes\necho "${VALUE:-$(node dist/index.js)}"\n/action/release-action',
        ]:
            with self.subTest(text=text):
                self.accepts(text)
        self.detects("VALUE='node dist/index.js'\neval \"$VALUE\"\n/action/release-action")

    def test_substitution_cwd_and_variables_do_not_leak(self):
        self.accepts('RUNTIME=git\nVALUE="$(cd /elsewhere; RUNTIME=node; printf "%s" literal)"\n"$RUNTIME" version\nexec ./release-action')
        self.accepts('RUNTIME=git\n(RUNTIME=node; cd /elsewhere)\n"$RUNTIME" version\nexec ./release-action')
        self.detects('RUNTIME=git\n{ RUNTIME=node; }\n"$RUNTIME" dist/index.js\n/action/release-action')

    def test_substitution_tracks_go_build_outputs_and_safe_path_builtins(self):
        for text in [
            'DIR="$(dirname "$0")"\nexec "$DIR/release-action"',
            'HERE="$(pwd)"\nexec "$HERE/release-action"',
            'BIN="$(mktemp /tmp/action.XXXXXX)"\ngo build -o "$BIN" .\nexec "$BIN"',
            'BIN="$(go build -o /tmp/app .; printf "%s" /tmp/app)"\nexec "$BIN"',
        ]:
            with self.subTest(text=text):
                self.accepts(text)
        self.assertFalse(self.inspect('BIN="$(if test "$UNKNOWN"; then go build -o /tmp/app .; fi; printf "%s" /tmp/app)"\nexec "$BIN"')['go'])

    def test_case_selects_only_reachable_arms(self):
        self.accepts('KIND=go\ncase "$KIND" in node) node dist/index.js;; go) /action/release-action;; esac')
        self.detects('KIND=node\ncase "$KIND" in node) node dist/index.js;; go) /action/release-action;; esac')
        self.detects('case "$UNKNOWN" in node) node dist/index.js;; *) /action/release-action;; esac')
        self.accepts('case /action in /action|/other) /action/release-action;; *) node dist/index.js;; esac')
        self.accepts("case text in '*') node dist/index.js;; *) /action/release-action;; esac")
        self.accepts('PATTERN="*"\ncase text in "$PATTERN") node dist/index.js;; *) /action/release-action;; esac')
        self.detects('PATTERN="*"\ncase text in $PATTERN) node dist/index.js;; *) /action/release-action;; esac')
        self.accepts("case '$TEXT' in node) node dist/index.js;; *) /action/release-action;; esac")
        self.accepts("TEXT='$TEXT'\nif test \"$TEXT\" = node; then node dist/index.js; else /action/release-action; fi")
        self.accepts("TEXT=\"$(printf '%s' '$TEXT')\"\ncase \"$TEXT\" in node) node dist/index.js;; *) /action/release-action;; esac")

    def test_loops_do_not_make_dormant_commands_active(self):
        self.accepts('for RUNTIME in git; do "$RUNTIME" version; done\n/action/release-action')
        self.detects('for RUNTIME in git node; do "$RUNTIME" dist/index.js; done\n/action/release-action')
        self.accepts('for RUNTIME in; do node dist/index.js; done\n/action/release-action')
        self.accepts('while false; do node dist/index.js; done\n/action/release-action')
        self.detects('while true; do node dist/index.js; done\n/action/release-action')
        self.accepts('until true; do node dist/index.js; done\n/action/release-action')
        self.accepts('for x in first second; do break; node dist/index.js; done\n/action/release-action')
        self.accepts('for x in first second; do continue; node dist/index.js; done\n/action/release-action')
        self.accepts('while true; do break; node dist/index.js; done\n/action/release-action')
        self.accepts('FLAG=yes\nwhile test "$FLAG" = yes; do FLAG=no; done\n/action/release-action')
        self.assertFalse(self.inspect('while true; do continue; done\n/action/release-action')['go'])

    def test_heredocs_distinguish_literal_body_and_executed_substitutions(self):
        self.accepts("cat <<'EOF'\nnode dist/index.js\n$(node dist/index.js)\nEOF\n/action/release-action")
        self.accepts('cat <<EOF\nnode dist/index.js\nEOF\n/action/release-action')
        self.detects('cat <<EOF\n$(node dist/index.js)\nEOF\n/action/release-action')
        self.accepts("cat <<-'EOF'\n\tnode dist/index.js\n\tEOF\n/action/release-action")

    def test_sourced_helper_function_is_traced_when_called(self):
        (self.root / 'helpers.sh').write_text('run_action() { exec "$1" dist/index.js; }\n')
        self.detects('. /action/helpers.sh\nrun_action node\n/action/release-action')
        self.accepts('. /action/helpers.sh\n/action/release-action')

    def test_pipelines_and_negation_inspect_every_executed_command(self):
        self.detects('node dist/index.js | cat\n/action/release-action')
        self.detects('printf "%s" input | node dist/index.js\n/action/release-action')
        self.detects('! node dist/index.js\n/action/release-action')
        self.accepts('! false && /action/release-action')
        self.accepts('(exec /action/release-action) | cat')

    def test_malformed_or_unbounded_constructs_do_not_gain_go_evidence(self):
        for text in ['(/action/release-action',
                     '/action/release-action\nVALUE="$(node dist/index.js"',
                     '/action/release-action\nrecursive() { recursive; }\nrecursive',
                     '/action/release-action\ncat <<EOF\nunclosed',
                     'if false; then /action/release-action; fi']:
            with self.subTest(text=text):
                self.assertFalse(self.inspect(text)['go'])

    def test_detector_imports_shared_parser_without_integration_on_sys_path(self):
        self.inspect('node dist/index.js')
        code = 'import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from verifier.detectors.entrypoint import detect_entrypoint_source_runtime;assert detect_entrypoint_source_runtime(Path(sys.argv[2]),json.loads(sys.argv[3]))'
        run = subprocess.run([sys.executable, '-B', '-c', code, str(TESTS), str(self.root), json.dumps(CONFIG)], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)


class PluginScopeTests(unittest.TestCase):
    def scan(self, code):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'main.go').write_text('package main\nimport p "plugin"\nvar _ = p.Open\n' + code)
            return detect_source_runtime_dynamic_load(root, CONFIG)

    def test_parameter_shadowing_with_all_return_signature_forms(self):
        for code in [
            'func f(p Local) error {return p.Open("node")}',
            'func f(p Local) (string,error) {return p.Open("node")}',
            'func f(p Local) (result error) {return p.Open("node")}',
            'func f[T any](p Local) error {return p.Open("node")}',
            'func f[T any](x T,p Local) error {return p.Open("node")}',
            'func f(p Local) *Result {p.Open("node");return nil}',
            'func f(p Local) []Result {p.Open("node");return nil}',
            'func f(p Local) map[string]Result {p.Open("node");return nil}',
            'func f(p Local) <-chan Result {p.Open("node");return nil}',
            'func f(p Local) struct{} {p.Open("node");return struct{}{}}',
            'func f(p Local) interface{} {p.Open("node");return nil}',
            'func f(p Local) func() error {p.Open("node");return nil}',
            'func (p Local) Run() error {return p.Open("node")}',
            'func (*Receiver) Run(p Local) error {return p.Open("node")}',
            'var f = func(p Local) error {return p.Open("node")}',
            'func f(p, other Local) error {return p.Open("node")}',
            'func f() (p Local, err error) {p.Open("node");return}',
        ]:
            with self.subTest(code=code):
                self.assertEqual(self.scan(code), [])

    def test_type_references_do_not_shadow_the_import_and_scopes_restore(self):
        for code in [
            'func f(x *p.Plugin) error {_,err:=p.Open("node");return err}',
            'func f(x []p.Plugin) error {_,err:=p.Open("node");return err}',
            'func f(p.Plugin) {p.Open("node")}',
            'func f(Generic[p.Plugin]) {p.Open("node")}',
            'func f(p Local) error {return nil}\nfunc g(){p.Open("node")}',
            'type Factory func(p Local) error\nfunc g(){p.Open("node")}',
            'func f() func(p Local) error {p.Open("node");return nil}',
            'func f() []func(p Local) error {p.Open("node");return nil}',
            'func f(p Local) error {return p.Open("safe")}\nfunc g(){p.Open("node")}',
        ]:
            with self.subTest(code=code):
                self.assertTrue(self.scan(code))

    def test_local_method_does_not_make_actual_nested_load_inactive(self):
        self.assertTrue(self.scan('func outer(){p.Open("node"); f:=func(p Local) error{return p.Open("node")};_=f}'))
        self.assertTrue(self.scan('func outer(){f:=func(p Local) error{return p.Open("node")};_=f;p.Open("node")}'))


if __name__ == '__main__':
    unittest.main()
