"""Paired acceptance/rejection tests for the four regrade boundary repairs.

Pure stdlib and static fixtures: no compiler, sockets or candidate execution.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS / 'assets/shared/integration'))
from launch import inspect_action
from verifier.detectors.dynamic import detect_source_runtime_dynamic_load
from verifier.detectors.entrypoint import detect_entrypoint_source_runtime
from verifier.evidence import build_source_test_baseline, _test_integrity
from verifier.source_syntax import test_flow, preserves_flow
from public_behavior import PublicBehavior

CONFIG = {'source_technology': {'name': 'TypeScript', 'language_name': 'TypeScript'}}
DOCKER_ACTION = 'runs:\n  using: docker\n  image: Dockerfile\n'
DOCKER = '''FROM golang:1.23 AS build
WORKDIR /action
COPY . .
RUN CGO_ENABLED=0 go build -o /action/release-action .
FROM alpine:3.20
WORKDIR /action
COPY --from=build /action /action
ENTRYPOINT ["/action/release-action"]
'''
SOURCE_TEST = '''describe("release", () => {
  const tag = "v1";
  it("validates", () => {
    expect(() => validate(tag))
      .not.toThrow();
  });
});
'''


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def dynamic(self, text):
        self.write('main.go', text)
        return detect_source_runtime_dynamic_load(self.root, CONFIG)

    def test_dynamic_diagnostic_text_is_not_a_call(self):
        examples = [
            '// Diagnostic example: plugin.Open("node")',
            '/* plugin.Open("libnode.so") */',
            'const example = `plugin.Open("node")`',
            'const example = `\n//go:linkname invoke libnode.start\n`',
            '/*\n//go:linkname invoke libnode.start\n*/',
            'import "plugin"\nconst p="node"\nfunc f(p string){plugin.Open(p)}',
            'const example = "plugin.Open(\\\"node\\\")"',
            'import "plugin"\nfunc f(){plugin.Open("safe.so"); println("node")}',
            'import "plugin"\nfunc f(){println("node"); plugin.Open("safe.so")}',
            'const path="plugin"\nfunc f(){plugin.Open("node")}',
            'import "plugin"\nfunc f(){p:="node";p="safe.so";plugin.Open(p)}',
            'import "plugin"\nfunc f(){p:="node";{p:="safe.so";plugin.Open(p)}}',
            'import "plugin"\nfunc f(){plugin:=unrelated;plugin.Open("node")}',
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(self.dynamic('package main\n' + text), [])

    def test_dynamic_real_loads_and_aliases_are_detected(self):
        examples = [
            'import "plugin"\nfunc f(){plugin.Open("node")}',
            'import p "plugin"\nfunc f(){p.Open("/usr/lib/libnode.so")}',
            'import("fmt"; p "plugin")\nfunc f(){p.Open("node")}',
            'import . "plugin"\nfunc f(){Open("node")}',
            'import "plugin"\nconst p="no"+"de"\nfunc f(){plugin.Open(p)}',
            'import "plugin"\nfunc f(){p:="no"+"de";plugin.Open(p)}',
            'import "plugin"\nfunc f(){load:=plugin.Open;load("node")}',
            'import "plugin"\nfunc f(){plugin.Open(p)}\nconst p="node"',
            'import "plugin"\nfunc f(){p:="node";{p:="safe.so";plugin.Open(p)};plugin.Open(p)}',
            '//go:linkname invoke libnode.start',
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertTrue(self.dynamic('package main\n' + text))

    def test_dynamic_test_only_loader_is_inactive(self):
        self.write('example_test.go', 'package main\nimport "plugin"\nfunc f(){plugin.Open("node")}')
        self.assertEqual(detect_source_runtime_dynamic_load(self.root, CONFIG), [])

    def test_multiline_test_flow_rejects_weakened_execution(self):
        baseline = test_flow(SOURCE_TEST)
        variants = [
            SOURCE_TEST.replace('.not.toThrow()', '.toBeDefined()'),
            SOURCE_TEST.replace('.not.toThrow()', '.toThrow()'),
            SOURCE_TEST.replace('validate(tag)', 'validate("fixed")'),
            SOURCE_TEST.replace('const tag = "v1"', 'const tag = "v2"'),
            SOURCE_TEST.replace('expect(()', 'return; expect(()'),
            SOURCE_TEST.replace('expect(()', 'if (false) return; expect(()'),
            SOURCE_TEST.replace('expect(()', 'return\n expect(()'),
            SOURCE_TEST.replace('it("validates"', 'it.skip("validates"'),
            SOURCE_TEST.replace('const tag', 'return; const tag'),
            'if (false) {\n' + SOURCE_TEST + '\n}',
        ]
        for text in variants:
            with self.subTest(text=text):
                self.assertFalse(preserves_flow(baseline, test_flow(text)))

    def test_multiline_test_flow_accepts_formatting_and_independent_cases(self):
        baseline = test_flow(SOURCE_TEST)
        variants = [
            SOURCE_TEST.replace('"', "'"),
            SOURCE_TEST.replace(';', ''),
            SOURCE_TEST.replace('.not.toThrow()', '\n.not\n.toThrow(\n)'),
            '// plugin.Open("node")\n' + SOURCE_TEST,
            SOURCE_TEST.replace('expect(()', '/* expect(false) */ expect(()'),
            SOURCE_TEST.replace('  it(', '  it("additional", () => { expect(2).toBe(2); });\n  it(', 1),
            SOURCE_TEST + '\nit("additional", () => { expect(2).toBe(2); });\n',
        ]
        for text in variants:
            with self.subTest(text=text):
                self.assertTrue(preserves_flow(baseline, test_flow(text)))

    def test_semantic_semicolons_and_asi_are_preserved(self):
        variants = [
            ('it("x",()=>{return; expect(1).toBe(2)})', 'it("x",()=>{return expect(1).toBe(2)})'),
            ('it("x",()=>{if(false); expect(1).toBe(2)})', 'it("x",()=>{if(false) expect(1).toBe(2)})'),
            ('it("x",()=>{for(;;) expect(1).toBe(2)})', 'it("x",()=>{for(;) expect(1).toBe(2)})'),
            ('it("x",()=>{f(); [expect(1).toBe(2)]})', 'it("x",()=>{f() [expect(1).toBe(2)]})'),
        ]
        for before, after in variants:
            with self.subTest(before=before):
                self.assertFalse(preserves_flow(test_flow(before), test_flow(after)))

    def test_bare_return_semicolon_is_optional(self):
        before = 'it("x",()=>{expect(1).toBe(1); return;})'
        after = 'it("x",()=>{expect(1).toBe(1)\n return})'
        self.assertTrue(preserves_flow(test_flow(before), test_flow(after)))

    def test_literal_urls_and_comment_markers_keep_their_identity(self):
        before = 'it("x",()=>{expect(url).toBe("https://api.example/a//b")})'
        self.assertTrue(preserves_flow(test_flow(before), test_flow(before.replace('"', "'"))))
        self.assertFalse(preserves_flow(test_flow(before), test_flow(before.replace('a//b', 'a//c'))))
        before = 'it("x",()=>{expect(text).toBe("/* valid */")})'
        self.assertFalse(preserves_flow(test_flow(before), test_flow(before.replace('valid', 'other'))))

    def test_integrity_uses_full_flow_and_allows_added_cases(self):
        path = self.write('__tests__/ReleaseValidator.test.ts', SOURCE_TEST)
        baseline = build_source_test_baseline(self.root)
        path.write_text(SOURCE_TEST.replace('.not.toThrow()', '.toBeDefined()'))
        self.assertEqual(_test_integrity(self.root, baseline, {})[0]['status'], 'FAIL')
        path.write_text(SOURCE_TEST + '\nit("new",()=>{expect(1).toBe(1)});')
        self.assertEqual(_test_integrity(self.root, baseline, {})[0]['status'], 'PASS')

    def action(self, docker=DOCKER, scripts=None, action=DOCKER_ACTION):
        self.write('action.yml', action)
        self.write('Dockerfile', docker)
        for path, text in (scripts or {}).items():
            self.write(path, text)
        return inspect_action(self.root, action)

    def test_direct_and_renamed_go_docker_entries_pass(self):
        recipes = [DOCKER,
            DOCKER.replace('/action /action', '/action/release-action /usr/local/bin/run').replace('/action/release-action"]', '/usr/local/bin/run"]'),
            DOCKER.replace('ENTRYPOINT ["/action/release-action"]', 'CMD ["/action/release-action"]'),
            DOCKER.replace('ENTRYPOINT ["/action/release-action"]', 'ENTRYPOINT /action/release-action'),
            DOCKER.replace('ENTRYPOINT ["/action/release-action"]', 'ENV PATH="/action:$PATH"\nENTRYPOINT ["release-action"]'),
        ]
        for text in recipes:
            with self.subTest(text=text):
                result = self.action(text)
                self.assertTrue(result['go'], result)
                self.assertFalse(result['source_runtime_hits'])

    def test_go_wrappers_arguments_nested_scripts_and_comments_pass(self):
        scripts = [
            '#!/bin/sh\n# node dist/index.js\necho "node"\nexec /action/release-action "$@"',
            '#!/bin/sh\nexec "$(dirname "$0")/release-action" "$@"',
            '#!/bin/sh\nexec env MODE=prod /action/release-action "$@"',
            '#!/usr/bin/env sh\nexec /action/release-action',
            '#!/bin/sh\nexec env -u UNUSED /action/release-action',
            '#!/bin/sh\nexec sh /action/child "$@"',
            '#!/bin/sh\nif false; then node dist/index.js; else exec /action/release-action; fi',
            '#!/bin/sh\nfalse && node dist/index.js\nexec /action/release-action',
            '#!/bin/sh\ntrue || node dist/index.js\nexec /action/release-action',
            '#!/bin/sh\nif [ -n "yes" ]; then exec /action/release-action; else node dist/index.js; fi',
            '#!/bin/sh\nif test "$UNKNOWN"; then exec /action/release-action; else exec /action/release-action; fi',
        ]
        for text in scripts:
            with self.subTest(text=text):
                result = self.action(DOCKER.replace('/action/release-action"]', '/action/entry"]'),
                                     {'entry': text, 'child': '#!/bin/sh\nexec /action/release-action "$@"', 'unused.sh': '#!/bin/sh\nnode dist/index.js'})
                self.assertTrue(result['go'], result)
                self.assertFalse(result['source_runtime_hits'], result)

    def test_actual_node_launch_chain_is_rejected_by_both_consumers(self):
        scripts = [
            '#!/bin/sh\nexec node "$(dirname "$0")/dist/index.js"',
            '#!/bin/sh\nRUNTIME=node\nexec "$RUNTIME" /action/dist/index.js',
            '#!/bin/sh\nexec env MODE=prod node /action/dist/index.js',
            '#!/bin/sh\nexec env -S "node /action/dist/index.js"',
            "#!/bin/sh\nexec sh -c 'exec \"$1\" dist/index.js' wrapper node",
            '#!/bin/sh\neval "node dist/index.js"',
            '#!/bin/sh\nexec sh -ec "node /action/dist/index.js"',
            '#!/bin/sh\nexec /action/child',
            '#!/bin/sh\nif false; then exec /action/release-action; else exec node dist/index.js; fi',
            '#!/bin/sh\nif test "$UNKNOWN"; then exec /action/release-action; else exec node dist/index.js; fi',
            '#!/bin/sh\ntrue && node dist/index.js\nexec /action/release-action',
        ]
        for text in scripts:
            with self.subTest(text=text):
                result = self.action(DOCKER.replace('/action/release-action"]', '/action/release-action.sh"]'),
                                     {'release-action.sh': text, 'child': '#!/bin/sh\nexec node dist/index.js'})
                self.assertTrue(result['source_runtime_hits'], result)
                self.assertTrue(detect_entrypoint_source_runtime(self.root, CONFIG))
                self.assertTrue(detect_entrypoint_source_runtime(self.root, {'migration': {'source': CONFIG['source_technology']}}))

    def test_binary_name_substrings_and_unreachable_decoys_do_not_pass(self):
        examples = [
            (DOCKER.replace('/action/release-action"]', '/action/release-action.sh"]'), {'release-action.sh': '#!/bin/sh\necho done'}),
            (DOCKER.replace('/action/release-action"]', '/action/entry"]'), {'entry': '#!/bin/sh\nif false; then exec /action/release-action; fi\necho done'}),
            (DOCKER.replace('/action/release-action"]', '/action/entry"]'), {'entry': '#!/bin/sh\nexit 0\nexec /action/release-action'}),
            (DOCKER + 'FROM alpine:3.20\nENTRYPOINT ["/action/release-action"]\n', {}),
            (DOCKER.replace('ENTRYPOINT', 'COPY replacement /action/release-action\nENTRYPOINT'), {'replacement': '#!/bin/sh\necho done'}),
            (DOCKER.replace('/action/release-action"]', 'release-action"]'), {}),
        ]
        for docker, scripts in examples:
            with self.subTest(docker=docker, scripts=scripts):
                self.assertFalse(self.action(docker, scripts)['go'])

    def test_docker_copy_directory_and_existing_workdir_semantics(self):
        for docker in [DOCKER.replace('/action /action', '/action /action/'),
                       DOCKER.replace('/action /action', '/action/release-action /action'),
                       DOCKER.replace('/action /action', '/action/release-action /action/')]:
            result = self.action(docker)
            self.assertTrue(result['go'], result)
        # Source wrappers copied as a directory must replace an exact binary
        # only when a file at that path actually exists in the build context.
        result = self.action(DOCKER.replace('ENTRYPOINT', 'COPY wrappers/ /action/\nENTRYPOINT'),
                             {'wrappers/release-action': '#!/bin/sh\nexec node dist/index.js'})
        self.assertFalse(result['go'])
        self.assertTrue(result['source_runtime_hits'])

    def test_node_shebang_and_versioned_runtime_are_detected(self):
        for script in ['#!/usr/bin/env node\nconsole.log("run")', '#!/usr/bin/node22\nconsole.log("run")', '#!/usr/bin/env -S node --no-warnings\nconsole.log("run")']:
            result = self.action(DOCKER.replace('/action/release-action"]', '/action/entry"]'), {'entry': script})
            self.assertTrue(result['source_runtime_hits'], result)

    def test_go_build_default_output_and_changed_directory(self):
        self.write('go.mod', 'module example.test/release-action\ngo 1.23.0\n')
        for docker in [DOCKER.replace('go build -o /action/release-action .', 'go build .'),
                       DOCKER.replace('go build -o /action/release-action .', 'go -C /action build -o release-action .'),
                       DOCKER.replace('go build -o /action/release-action .', 'go build -o /action/ .'),
                       DOCKER.replace('ENTRYPOINT ["/action/release-action"]', 'ENV PATH /action:$PATH\nENTRYPOINT ["release-action"]')]:
            result = self.action(docker)
            self.assertTrue(result['go'], result)

    def test_recursive_wrapper_is_bounded(self):
        result = self.action(DOCKER.replace('/action/release-action"]', '/action/entry"]'),
                             {'entry': '#!/bin/sh\nexec /action/entry'})
        self.assertFalse(result['go'])

    def test_composite_runtime_calls_are_followed(self):
        action = 'runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: |\n        go build -o ./app .\n        exec ./app\n'
        self.assertTrue(self.action(action=action)['go'])
        self.assertTrue(self.action(action=action.replace('exec ./app', 'exec node dist/index.js'))['source_runtime_hits'])
        self.assertFalse(self.action(action=action.replace('exec ./app', 'if false; then exec ./app; fi'))['go'])

    def test_escaped_glob_oracle_matches_bundled_source(self):
        cases = dict(PublicBehavior.ESCAPED_GLOB_CASES)
        self.assertEqual(cases[r'@(a\|b|c).zip'], ('b.zip', 'c.zip'))


if __name__ == '__main__':
    unittest.main()
