"""Regression tests for process policy and action wrapper execution.

The executable fixture isolates the runner adapter; it does not stand in for
Go compilation. Go-policy execution remains covered by selftest.py when Go is
available. Run both scripts before regenerating/regrading the task.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "assets/target/blackbox"))
import action_runtime as runtime
import labeler_blackbox as blackbox
import docker_build
from action_expressions import evaluate, ExpressionError
from detectors.process_commands import runtime_in_argv, runtime_in_shell
from detectors.subprocess import detect_source_runtime_subprocess


PROGRAM = (ROOT / 'verifier/fixtures/wrapper_program.py').read_text()


def can_bind_localhost():
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False

DOCKER = '''FROM golang:1.24 AS arbitrary_builder
WORKDIR /build
COPY go.mod ./
COPY cmd ./cmd
RUN CGO_ENABLED=0 go build -trimpath -o /out/custom-name ./cmd/labeler
FROM scratch
COPY --from=arbitrary_builder /out/custom-name /opt/run-labels
ENTRYPOINT ["/opt/run-labels"]
'''


class RuntimeTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        (root / "cmd/labeler").mkdir(parents=True)
        (root / "cmd/labeler/entry.go").write_text("package main\nfunc main() {}\n")
        (root / "go.mod").write_text("module github.com/actions/labeler\n\ngo 1.24.0\n")
        executable = root / "fixture-executable"
        executable.write_text(PROGRAM)
        executable.chmod(0o755)
        action = {
            "inputs": {name: {"default": default} for name, default in [
                ("repo-token", "${{ github.token }}"), ("configuration-path", ".github/labeler.yml"),
                ("sync-labels", False), ("dot", True), ("pr-number", "")]},
            "outputs": {name: {"description": name, "value": "${{ steps.execute.outputs." + name + " }}"}
                        for name in ("new-labels", "all-labels")},
            "runs": {"using": "composite", "steps": [{"id": "execute", "shell": "bash",
                "env": {"INPUT_" + name.upper(): "${{ inputs." + name + " }}" for name in
                        ("repo-token", "configuration-path", "sync-labels", "dot", "pr-number")},
                "run": '"$GITHUB_ACTION_PATH/fixture-executable"'}]},
        }
        (root / "action.yml").write_text(json.dumps(action))  # JSON is valid YAML
        return root, executable, action

    def verify(self, root, executable, action):
        (root / "action.yml").write_text(json.dumps(action))
        adapter = runtime.ActionRuntime(root, str(executable), blackbox.parse_outputs)
        with tempfile.TemporaryDirectory(prefix='labeler-caller-') as caller:
            workspace = Path(caller)
            (workspace / '.github').mkdir()
            (workspace / '.github/labeler.yml').write_text(blackbox.BASIC)
            output = workspace / 'output'
            output.touch()
            receipt = workspace / 'api-receipt'
            environment = {**os.environ, 'INPUT_REPO-TOKEN': 'token',
                           'INPUT_CONFIGURATION-PATH': '.github/labeler.yml',
                           'INPUT_PR-NUMBER': '123',
                           'GITHUB_OUTPUT': str(output), 'GITHUB_API_URL': 'http://127.0.0.1:1',
                           'LABELER_ADAPTER_TEST_RECEIPT': str(receipt)}
            # The adapter must request a stage build; this fixture isolates
            # wrapper execution without pretending the host has a Go compiler.
            with patch.object(runtime, 'execute_go', return_value=(0, str(executable), '')):
                code, log, outputs = adapter.run(environment, caller)
            adapter.close()
            if code or outputs != {'new-labels': 'documentation', 'all-labels': 'documentation'} or not receipt.exists():
                raise runtime.RuntimeContractError('wrapper failed: %s %s %s' % (code, outputs, log))
            self.assertEqual(json.loads(receipt.read_text()), {'labels': ['documentation']})

    @unittest.skipUnless(can_bind_localhost(), 'sandbox does not permit binding a localhost API fixture')
    def test_full_entrypoint_probe_uses_real_api_and_local_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root, executable, action = self.fixture(directory)
            with patch.object(blackbox, 'build_binary', return_value=str(executable)):
                runtime.verify_entrypoint(root)

    def test_process_policy_keeps_shell_data_and_nearby_strings_inert(self):
        cases = [
            ('printf %s node', False), ('echo "node"', False), ('printf %s "dist/index.js"', False),
            ("printf '%s' '$(node)'", False), ('printf ok # node\nprintf ok', False),
            ('# comment $(node)\nprintf ok', False), ('command -v node', False),
            ('exec -a node printf ok', False),
            ('node dist/index.js', True), ('printf ok # comment\nnode dist/index.js', True),
            ('printf ok; /usr/bin/node dist/index.js', True), ('exec env X=y node dist/index.js', True),
            ('printf %s "$(node -p 1)"', True), ('r="no""de"; "$r" dist/index.js', True),
            ('"$RUNTIME" dist/index.js', True),
            ('timeout 10 node dist/index.js', True), ('nice -n 10 node dist/index.js', True),
            ('''python3 -c 'print("node")' ''', False),
            ('''python3 -c 'import subprocess; subprocess.run(["node", "dist/index.js"])' ''', True),
        ]
        for script, forbidden in cases:
            with self.subTest(script=script):
                self.assertEqual(bool(runtime_in_shell(script)), forbidden)
                for prefix in ('exec.Command(', 'exec.CommandContext(ctx, ', 'child.CommandContext(ctx, '):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        (root / "main.go").write_text('package api\nimport child "os/exec"\nconst field="node"\n'
                            'func run(){ ' + prefix + '"sh", "-c", ' + json.dumps(script) + ').Run() }\n')
                        hits = detect_source_runtime_subprocess(root, {}, tokens=["node"])
                        self.assertEqual(bool(hits), forbidden, hits)
        self.assertIsNone(runtime_in_argv(["env", "X=node", "printf", "node"]))
        self.assertEqual(runtime_in_argv(["env", "-u", "NODE_OPTIONS", "node", "app.js"]), "node")

    def test_composite_executes_from_caller_workspace_and_maps_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root, executable, action = self.fixture(directory)
            self.verify(root, executable, action)

    def test_composite_allows_supporting_scripts_and_different_step_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root, executable, action = self.fixture(directory)
            script = root / "launch-any-name.sh"
            script.write_text('#!/bin/bash\nexec "$GITHUB_ACTION_PATH/fixture-executable"\n')
            script.chmod(0o755)
            action["runs"]["steps"][0]["id"] = "arbitrary"
            action["runs"]["steps"][0]["run"] = '"$GITHUB_ACTION_PATH/launch-any-name.sh"'
            for name in action["outputs"]:
                action["outputs"][name]["value"] = "${{ steps['arbitrary'].outputs['" + name + "'] }}"
            self.verify(root, executable, action)

    def test_composite_preserves_environment_and_path_between_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            root, executable, action = self.fixture(directory)
            action['runs']['steps'].insert(0, {'shell': 'bash', 'run':
                'echo "LABELER_NAME=fixture-executable" >> "$GITHUB_ENV"\n'
                'echo "$GITHUB_ACTION_PATH" >> "$GITHUB_PATH"\n'})
            action['runs']['steps'][1]['run'] = '${{ env.LABELER_NAME }}'
            self.verify(root, executable, action)

    def test_composite_rejects_missing_or_swapped_output_bindings(self):
        for mutation in ("missing", "wrong-step", "constant"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                action["outputs"]["new-labels"]["value"] = {
                    "missing": "", "wrong-step": "${{ steps.nonexistent.outputs.new-labels }}", "constant": "no-labels"}[mutation]
                with self.assertRaises(runtime.RuntimeContractError):
                    self.verify(root, executable, action)

    def test_composite_rejects_noop_wrong_cwd_and_missing_input_forwarding(self):
        for mutation in ("noop", "wrong-cwd", "missing-input", "node"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                step = action["runs"]["steps"][0]
                if mutation == "noop":
                    step["run"] = "echo noop"
                elif mutation == "wrong-cwd":
                    step["working-directory"] = "${{ github.action_path }}"
                elif mutation == "missing-input":
                    del step["env"]["INPUT_CONFIGURATION-PATH"]
                else:
                    step["run"] = "node dist/index.js"
                with self.assertRaises(runtime.RuntimeContractError):
                    self.verify(root, executable, action)

    def test_docker_traces_final_binary_and_executes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root, executable, action = self.fixture(directory)
            (root / "arbitrary.Containerfile").write_text(DOCKER)
            action["runs"] = {"using": "docker", "image": "arbitrary.Containerfile"}
            self.verify(root, executable, action)

    def test_runner_accepts_renamed_main_and_wrappers_without_inline_go(self):
        loader = importlib.machinery.SourceFileLoader('runtime_policy_runner', str(ROOT / 'assets/target/runner'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        runner = importlib.util.module_from_spec(spec)
        loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as directory:
            root, executable, action = self.fixture(directory)
            # No main.go, Dockerfile, or go command in YAML; the supported
            # wrapper shape must not be rejected by textual preconditions.
            self.assertTrue(runner.target_technology_active(str(root)))
            (root / 'action.yml').write_text(json.dumps(action) + '\n# node24 / dist/index.js are retired\n')
            self.assertTrue(runner.target_technology_active(str(root)))
            (root / 'arbitrary.Containerfile').write_text(DOCKER)
            action['runs'] = {'using': 'docker', 'image': 'arbitrary.Containerfile'}
            (root / 'action.yml').write_text(json.dumps(action))
            self.assertTrue(runner.target_technology_active(str(root)))
            (root / 'blackbox/action_runtime.py').write_text('raise SystemExit(0)\n')
            action['runs'] = {'using': 'node24', 'main': 'dist/index.js'}
            (root / 'action.yml').write_text(json.dumps(action))
            self.assertFalse(runner.target_technology_active(str(root)))
            runner.cleanup()

    def test_docker_allows_shell_wrapper_and_binary_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root, executable, action = self.fixture(directory)
            (root / "Dockerfile").write_text(DOCKER.replace('FROM scratch', 'FROM debian:bookworm-slim').replace(
                'ENTRYPOINT ["/opt/run-labels"]', 'COPY launcher.sh /launch.sh\nENTRYPOINT ["/launch.sh"]'))
            script = root / "launcher.sh"
            script.write_text('#!/bin/bash\nexec /opt/run-labels "$@"\n')
            script.chmod(0o755)
            action["runs"] = {"using": "docker", "image": "Dockerfile"}
            self.verify(root, executable, action)

    def test_docker_rejects_json_node_noop_and_unreachable_build(self):
        for mutation in ("json-node", "shell-node", "noop", "wrong-copy", "wrong-package", "missing-sources"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                recipe = DOCKER
                if mutation == "json-node":
                    recipe = recipe.replace('["/opt/run-labels"]', '["node", "dist/index.js"]')
                elif mutation == "shell-node":
                    recipe = recipe.replace('["/opt/run-labels"]', '["sh", "-c", "node dist/index.js"]')
                elif mutation == "noop":
                    recipe = recipe.replace('["/opt/run-labels"]', '["sh", "-c", "echo noop"]')
                elif mutation == "wrong-copy":
                    recipe = recipe.replace('COPY --from=arbitrary_builder /out/custom-name /opt/run-labels', 'COPY go.mod /opt/run-labels')
                elif mutation == "wrong-package":
                    recipe = recipe.replace('./cmd/labeler', './cmd/other')
                else:
                    recipe = recipe.replace('COPY cmd ./cmd\n', '')
                (root / "Dockerfile").write_text(recipe)
                action["runs"] = {"using": "docker", "image": "Dockerfile"}
                with self.assertRaises(runtime.RuntimeContractError):
                    self.verify(root, executable, action)

    def test_composite_conditions_follow_expression_syntax_and_false_values(self):
        for condition in ("${{ inputs.repo-token != '' }}", "${{ !(inputs.repo-token == '') && runner.os == 'linux' }}",
                          "${{ contains(inputs.repo-token, 'TOK') }}"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                action['runs']['steps'][0]['if'] = condition
                self.verify(root, executable, action)
        for condition in (False, "${{ false }}", "${{ inputs.repo-token == '' }}", '0'):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                action['runs']['steps'].insert(0, {'if': condition, 'shell': 'bash', 'run': 'exit 9'})
                self.verify(root, executable, action)

    def test_docker_path_order_and_script_entrypoints(self):
        for shell in (False, True):
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                recipe = DOCKER.replace('FROM scratch', 'FROM alpine:3.21\nENV PATH=/custom:/usr/local/bin:$PATH')
                recipe = recipe.replace('/opt/run-labels', '/custom/labeler')
                recipe = recipe.replace('ENTRYPOINT ["/custom/labeler"]', 'ENTRYPOINT ["labeler"]')
                if shell:
                    recipe = recipe.replace('FROM alpine:3.21', 'FROM debian:bookworm-slim')
                    recipe = recipe.replace('ENTRYPOINT ["labeler"]', 'COPY launch.sh /launch.sh\nENTRYPOINT ["/launch.sh"]')
                    script = root / 'launch.sh'; script.write_text('#!/bin/bash\nexec labeler "$@"\n'); script.chmod(0o755)
                (root / 'Dockerfile').write_text(recipe)
                action['runs'] = {'using': 'docker', 'image': 'Dockerfile'}
                self.verify(root, executable, action)

    def test_docker_failed_and_unreached_builds_are_rejected(self):
        for script in ('false && BUILD', 'BUILD && false', 'true || BUILD', 'exit 0; BUILD',
                       'if false; then BUILD; fi', 'set -e; false; BUILD'):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                recipe = DOCKER.replace('CGO_ENABLED=0 go build -trimpath -o /out/custom-name ./cmd/labeler',
                    script.replace('BUILD', 'CGO_ENABLED=0 go build -trimpath -o /out/custom-name ./cmd/labeler'))
                (root / 'Dockerfile').write_text(recipe); action['runs'] = {'using': 'docker', 'image': 'Dockerfile'}
                with self.assertRaises(runtime.RuntimeContractError): self.verify(root, executable, action)

    def test_docker_recovered_failures_and_conditions_keep_reachable_builds(self):
        for script in ('true && BUILD', 'false || BUILD', 'false; BUILD',
                       'if false; then false; else BUILD; fi', '(exit 1) || BUILD',
                       'set -e; false && false; BUILD', "printf '%s' '&&'; BUILD"):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                recipe = DOCKER.replace('CGO_ENABLED=0 go build -trimpath -o /out/custom-name ./cmd/labeler',
                    script.replace('BUILD', 'CGO_ENABLED=0 go build -trimpath -o /out/custom-name ./cmd/labeler'))
                (root / 'Dockerfile').write_text(recipe); action['runs'] = {'using': 'docker', 'image': 'Dockerfile'}
                self.verify(root, executable, action)

    def test_docker_uses_the_stage_build_and_propagates_compiler_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root, executable, action = self.fixture(directory)
            (root / 'Dockerfile').write_text(DOCKER); action['runs'] = {'using': 'docker', 'image': 'Dockerfile'}
            (root / 'action.yml').write_text(json.dumps(action))
            with patch.object(runtime, 'execute_go', return_value=(1, None, 'declared build flag failed')) as compiler:
                adapter = runtime.ActionRuntime(root, executable, blackbox.parse_outputs)
                with self.assertRaisesRegex(ValueError, 'declared build flag failed'):
                    runtime.docker_plan(root, action, lambda *args: runtime.execute_go(*args, root, root / 'build'))
                self.assertEqual(compiler.call_count, 1)
                adapter.close()

    def input_matrix(self, root, executable, action):
        (root / 'action.yml').write_text(json.dumps(action))
        adapter = runtime.ActionRuntime(root, executable, blackbox.parse_outputs)
        original = blackbox.run_action
        state = blackbox.State(); state.api_url = 'http://127.0.0.1:1'
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'state.json'
            def file_transport(binary, current, *args, **kwargs):
                def invoke(environment, workspace):
                    receipt.write_text(json.dumps(current.__dict__))
                    result = binary(environment, workspace)
                    current.__dict__.update(json.loads(receipt.read_text()))
                    return result
                with patch.dict(os.environ, {'LABELER_ADAPTER_TEST_STATE': str(receipt)}):
                    result = original(invoke, current, *args, **kwargs)
                return result
            try:
                with patch.object(blackbox, 'run_action', side_effect=file_transport), patch.object(
                        runtime, 'execute_go', return_value=(0, str(executable), '')):
                    runtime.verify_runtime_inputs(adapter, blackbox, state)
            finally:
                adapter.close()

    def test_input_matrix_accepts_composite_and_docker_wrappers(self):
        for docker in (False, True):
            with self.subTest(docker=docker), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                if docker:
                    (root / 'Dockerfile').write_text(DOCKER); action['runs'] = {'using': 'docker', 'image': 'Dockerfile'}
                self.input_matrix(root, executable, action)

    def test_input_matrix_rejects_lost_non_default_inputs_and_constant_outputs(self):
        mutations = {'INPUT_SYNC-LABELS': 'false', 'INPUT_DOT': 'true', 'INPUT_PR-NUMBER': '123',
                     'INPUT_REPO-TOKEN': 'token', 'INPUT_CONFIGURATION-PATH': '.github/labeler.yml'}
        for docker in (False, True):
            for name, value in mutations.items():
                with self.subTest(docker=docker, input=name), tempfile.TemporaryDirectory() as directory:
                    root, executable, action = self.fixture(directory)
                    if docker:
                        (root / 'Dockerfile').write_text(DOCKER)
                        action['runs'] = {'using': 'docker', 'image': 'Dockerfile', 'env': {name: value}}
                    else:
                        action['runs']['steps'][0]['env'][name] = value
                    with self.assertRaises(runtime.RuntimeContractError): self.input_matrix(root, executable, action)
        for name in ('new-labels', 'all-labels'):
            with self.subTest(output=name), tempfile.TemporaryDirectory() as directory:
                root, executable, action = self.fixture(directory)
                action['outputs'][name]['value'] = 'documentation'
                with self.assertRaises(runtime.RuntimeContractError): self.input_matrix(root, executable, action)


class ExpressionTests(unittest.TestCase):
    def test_operator_precedence_quoted_operators_and_short_circuit(self):
        context = {'inputs': {'token': 'Token'}, 'steps': {'step': {'outputs': {'x': 'a.b'}}}}
        cases = [
            ("inputs.token != ''", True), ("!false && (true || false)", True),
            ("false || true && false", False), ("'a||b' == 'A||B'", True),
            ("'a&&b!=c' != 'other'", True), ("'can''t' == 'CAN''T'", True),
            ("false && unknown()", False), ("true || unknown()", True),
            ("'false' && 'value' || 'fallback'", 'value'),
            ("'' && 'value' || 'fallback'", 'fallback'),
            ("'2' == 2", True), ("null == 0", True), ("'abc' < 1", False),
            ("steps['step'].outputs['x']", 'a.b'), ("steps.missing.outputs.x", ''),
            ("INPUTS.TOKEN == 'token'", True), ("0x10 >= 16", True),
            ("format('{{{0}}}', inputs.token)", '{Token}'),
            ("contains(fromJSON('[1,2]'), 2)", True),
            ("join(fromJSON('[1,2]'), ':')", '1:2'),
            ("startsWith('Abc', 'a') && endsWith('Abc','C')", True),
        ]
        for text, expected in cases:
            with self.subTest(text=text): self.assertEqual(evaluate(text, context), expected)
        for text in ("inputs.token !=", "true trailing", 'unknown()', '(' * 100 + 'true' + ')' * 100):
            with self.subTest(text=text), self.assertRaises((ValueError, IndexError)): evaluate(text, context)

    def test_wildcards_and_status_functions(self):
        context = {'items': [{'name': 'A'}, {'name': 'B'}], '_status': 'failure'}
        self.assertTrue(evaluate("contains(items.*.name, 'b')", context))
        self.assertTrue(evaluate('failure() && !success() && always() && !cancelled()', context))


if __name__ == "__main__":
    unittest.main(verbosity=2)
