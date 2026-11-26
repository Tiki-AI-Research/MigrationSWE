"""Paired acceptance/rejection tests plus harmless /bin/sh semantic oracles."""
import os
from pathlib import Path
import shlex
import subprocess
import unittest

from regressions import test_shell_execution as prior
from launch import printf_output, shell_fields


class ShellBindingTests(unittest.TestCase):
    setUp = prior.ShellExecutionTests.setUp
    tearDown = prior.ShellExecutionTests.tearDown
    inspect = prior.ShellExecutionTests.inspect
    accepts = prior.ShellExecutionTests.accepts
    detects = prior.ShellExecutionTests.detects

    def compare_shell(self, text):
        """Execute only test-authored shell with inert action/runtime stubs."""
        # This oracle executes /bin/sh on the host. Pin that interpreter in
        # the analysed fixture too, instead of inheriting Alpine's ash from
        # the generic Docker fixture (the host can use Dash or Bash).
        interpreter = Path('/bin/sh').resolve().name
        if interpreter == 'busybox':
            interpreter = 'ash'
        script = '#!/bin/' + interpreter + '\n' + text
        if interpreter == 'bash':
            script = '#!/bin/bash\nset -o posix\n' + text
        inspected = self.inspect(script)
        stubs = self.root / 'oracle-bin'
        stubs.mkdir(exist_ok=True)
        log = self.root / 'oracle.log'
        log.write_text('')
        node = stubs / 'node'
        node.write_text('#!/bin/sh\nprintf "NODE\\n" >> "$AUDIT_TRACE"\n')
        node.chmod(0o755)
        go = self.root / 'release-action'
        go.write_text('#!/bin/sh\nprintf "GO\\n" >> "$AUDIT_TRACE"\n')
        go.chmod(0o755)
        env = {'PATH': str(stubs) + os.pathsep + os.defpath, 'AUDIT_TRACE': str(log)}
        try:
            run = subprocess.run(['/bin/sh', '-c', text.replace('/action', str(self.root))],
                                 env=env, capture_output=True, text=True, timeout=3)
            self.assertEqual(run.returncode, 0, run.stderr)
            selected = log.read_text().splitlines()
            self.assertEqual(len(inspected['source_runtime_hits']), selected.count('NODE'), (text, inspected, selected))
            self.assertEqual(inspected['go'], 'GO' in selected, (text, inspected, selected))
        finally:
            go.unlink()

    def test_printf_reuses_format_for_paths_and_runtime_names(self):
        self.accepts('BIN="$(printf \'%s\' /action/ release-action)"\nexec "$BIN"')
        self.detects('RUNTIME="$(printf \'%s\' no de)"\n"$RUNTIME" dist/index.js\n/action/release-action')
        self.detects('RUNTIME="$(printf \'%c\' n o d e)"\n"$RUNTIME" dist/index.js\n/action/release-action')
        self.detects('RUNTIME="$(printf \'%b\' \'no\\0144e\')"\n"$RUNTIME" dist/index.js\n/action/release-action')
        self.accepts('VALUE="$(printf \'%s\' no de)"\nprintf "%s" "$VALUE"\n/action/release-action')

    def test_printf_supported_formats_agree_with_real_shell(self):
        formats = ['%s', '%s%s', '<%s>%%', '%8s', '%-8.3s', '%.0s', '%b', '%c',
                   '%d', '%i', '%03d', '%+d', '%x', '%X', '%#o', '%%', r'\045s', r'%s\n', 'literal']
        arguments = [[], [''], ['node'], ['no', 'de'], ['a', 'b', 'c'], ['10', '12'],
                     ['010'], [r'no\0144e'], [r'a\cb', 'ignored'], [r'\0'], ['-10']]
        checked = 0
        for fmt in formats:
            for args in arguments:
                with self.subTest(fmt=fmt, args=args):
                    actual = printf_output(fmt, args)
                    run = subprocess.run(['/bin/sh', '-c', 'printf "$@"', 'audit', fmt, *args],
                                         capture_output=True, text=True, timeout=3)
                    if actual is not None:
                        self.assertEqual(actual, run.stdout)
                        checked += 1
        self.assertGreater(checked, 120)
        self.assertIsNone(printf_output('%q', ['node']))

    def test_for_fields_and_empty_expansions_agree_with_real_shell(self):
        values = ['', 'node', 'true node', ' true  node ', 'true:node', '::', 'node::true', '\tnode\ntrue ']
        separators = [None, '', ':', ' :\t']
        for ifs in separators:
            for value in values:
                for expression in ['$VALUES', '"$VALUES"', '""$VALUES', '$VALUES""']:
                    prefix = '' if ifs is None else 'IFS=' + shlex.quote(ifs) + '\n'
                    text = (prefix + 'VALUES=' + shlex.quote(value) + '\nfor item in ' + expression +
                            '; do case "$item" in node) node dist/index.js;; esac; done\n/action/release-action')
                    with self.subTest(ifs=ifs, value=value, expression=expression):
                        self.compare_shell(text)

    def test_for_expansions_preserve_quoting_and_positional_arguments(self):
        self.accepts('VALUES=""\nfor x in $VALUES; do node dist/index.js; done\n/action/release-action')
        self.detects('VALUES=""\nfor x in "$VALUES"; do node dist/index.js; done\n/action/release-action')
        self.detects('VALUES="true node"\nfor x in $VALUES; do "$x" dist/index.js; done\n/action/release-action')
        self.accepts('VALUES="true node"\nfor x in "$VALUES"; do case "$x" in node) node dist/index.js;; esac; done\n/action/release-action')
        for expression in ['"$@"', '$@']:
            self.compare_shell('run() { for item in ' + expression + '; do case "$item" in node) node dist/index.js;; esac; done; }\nrun "" "true node" node\n/action/release-action')

    def test_assignment_expansion_order_and_temporary_scope(self):
        for text in [
            'DIR=/action BIN="$DIR/release-action"\nexec "$BIN"',
            'DIR="$(dirname "$0")" BIN="$DIR/release-action"\nexec "$BIN"',
            'RUNTIME=true\nRUNTIME=node printf "%s" "$RUNTIME"\n"$RUNTIME" version\n/action/release-action',
            'RUNTIME=true\nrun() { printf "%s" "$RUNTIME"; }\nRUNTIME=node run\n"$RUNTIME" version\n/action/release-action',
        ]:
            with self.subTest(text=text):
                self.accepts(text)
        self.detects('PREFIX=no RUNTIME="${PREFIX}de"\n"$RUNTIME" dist/index.js\n/action/release-action')
        self.detects('A="$(node dist/index.js)" B="$A"\n/action/release-action')

    def test_unset_state_defaults_and_function_scope(self):
        for text in [
            'RUNTIME=node\nunset RUNTIME\nexec "${RUNTIME:-/action/release-action}"',
            'RUNTIME=node\nunset -v -- RUNTIME\nexec "${RUNTIME-/action/release-action}"',
            'RUNTIME=true\nrun() { local RUNTIME=node; unset RUNTIME; }\nrun\n"$RUNTIME" version\n/action/release-action',
            'RUNTIME=true\n(unset RUNTIME)\n"$RUNTIME" version\n/action/release-action',
        ]:
            with self.subTest(text=text):
                self.accepts(text)
        self.detects('unset RUNTIME\n"${RUNTIME:-node}" dist/index.js\n/action/release-action')
        self.detects('node() { true; }\nunset -f node\nnode dist/index.js\n/action/release-action')
        self.accepts('RUNTIME=""\nunset OTHER\n"${RUNTIME:+node}"\n/action/release-action')

    def test_readonly_and_quoted_assignment_words_do_not_fabricate_go(self):
        for text in ['readonly RUNTIME=node\nunset RUNTIME\n/action/release-action',
                     'readonly RUNTIME=node\nRUNTIME=/action/release-action\n"$RUNTIME"',
                     '"VALUE=1" go run .',
                     '"VALUE=1" go build -o /tmp/app .\nexec /tmp/app']:
            with self.subTest(text=text):
                self.assertFalse(self.inspect(text)['go'])
        self.accepts('run() { local RUNTIME=true; readonly RUNTIME; }\nrun\nunset RUNTIME\nexec /action/release-action')

    def test_lookup_is_inactive_and_execution_remains_active(self):
        for query in ['command -v node', 'command -V node', 'command -pv node',
                      'command -v -- node npm', 'command -v node >/dev/null',
                      'VALUE="$(command -v node)"\nprintf "%s" "$VALUE"']:
            with self.subTest(query=query):
                self.accepts(query + '\nexec /action/release-action')
        for call in ['command node dist/index.js', 'command -p node dist/index.js',
                     'command -- node dist/index.js', 'VALUE="$(command -v node)"\n"$VALUE" dist/index.js']:
            with self.subTest(call=call):
                self.detects(call + '\n/action/release-action')
        self.accepts('BIN="$(command -v /action/release-action)"\nexec "$BIN"')


class GoBindingTests(unittest.TestCase):
    scan = prior.PluginScopeTests.scan

    def test_local_var_type_const_and_multi_declarations_shadow_import(self):
        for declaration in ['var p Local', 'var p = Local{}', 'var p Local = Local{}',
                            'var (p Local)', 'var (p Local\n other string)',
                            'p, other := Local{}, 0', 'other, p := 0, Local{}',
                            'var p, other = Local{}, 0', 'type p Local', 'const p = "safe"']:
            with self.subTest(declaration=declaration):
                self.assertEqual(self.scan('func f(){' + declaration + '; p.Open("node")}'), [])

    def test_initializers_are_checked_before_simultaneous_binding_updates(self):
        for declaration in ['var p = p.Open("node")', 'p, err := p.Open("node")',
                            'var p, err = p.Open("node")', 'var (p = p.Open("node"))',
                            'open, p := p.Open, Local{}; open("node")',
                            'var open, p = p.Open, Local{}; open("node")']:
            with self.subTest(declaration=declaration):
                self.assertTrue(self.scan('func f(){' + declaration + '}'))

    def test_shadowed_blocks_headers_and_else_restore_imports(self):
        for body in ['{var p Local; p.Open("node")}',
                     'if p:=Local{}; true {p.Open("node")} else {p.Open("node")}',
                     'if p,err:=Local{},nil; err==nil {p.Open("node")}',
                     'for _,p:=range values {p.Open("node")}',
                     'for _,p:=range []Local{{}} {p.Open("node")}',
                     'switch p:=Local{}; true {default:p.Open("node")}']:
            with self.subTest(body=body):
                self.assertEqual(self.scan('func f(){' + body + '}'), [])
                self.assertTrue(self.scan('func f(){' + body + '; p.Open("node")}'))

    def test_alias_capture_and_selector_assignments_preserve_real_loads(self):
        for body in ['open:=p.Open; var p Local; open("node")',
                     'open,other:=p.Open,0; open("node")',
                     'var open = p.Open; open("node")',
                     'obj.p=Local{};p.Open("node")',
                     'obj.p,other=Local{},0;p.Open("node")',
                     'values[p]=Local{};p.Open("node")']:
            with self.subTest(body=body):
                self.assertTrue(self.scan('func f(){' + body + '}'))
        for body in ['open:=p.Open; obj.field,open=Local{},safe;open("node")',
                     'open:=p.Open; values[0],open=Local{},safe;open("node")',
                     'open,other:=safe,0;obj.field,open=Local{},p.Open;open("node")']:
            with self.subTest(mixed_targets=body):
                self.assertEqual(bool(self.scan('func f(){' + body + '}')), '=Local{},p.Open' in body)


if __name__ == '__main__':
    unittest.main()
