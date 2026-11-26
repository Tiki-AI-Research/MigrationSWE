"""Independent shell-oracle and stage compiler regressions (no Docker needed)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'assets/target/blackbox'))
from action_runtime import Stage, docker_plan
import docker_build as build


class DockerBuildTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory) / 'repository'
        (root / 'cmd/labeler').mkdir(parents=True)
        (root / 'cmd/labeler/main.go').write_text('package main\nfunc main() {}\n')
        (root / 'go.mod').write_text('module example.com/labeler\n\ngo 1.20\n')
        stage = Stage(workdir='/build')
        stage.files = {'/build/' + name: ('source', str(root / name))
                       for name in ['go.mod', 'cmd/labeler/main.go']}
        return root, stage

    def test_control_flow_matches_real_shell_status_and_build_reachability(self):
        cases = [
            'false && BUILD', 'true || BUILD', 'exit 0; BUILD', 'BUILD && false',
            'false || BUILD', 'false; BUILD', 'true && BUILD',
            'if false; then BUILD; fi', 'if true; then BUILD; else false; fi',
            'if false; then false; elif true; then BUILD; else false; fi',
            'if false; then false; else BUILD; fi', '(exit 3) || BUILD',
            'set -e; false; BUILD', 'set -e; false && false; BUILD',
            'set -e; true && false; BUILD', 'set -e; ! false; BUILD',
            'set -e; if false; then false; fi; BUILD',
            'set -e; (false); BUILD', 'set -e; (false) || BUILD',
            'set -e; (set +e; true); false; BUILD',
            'false | true; BUILD', 'set -e; false | true; BUILD',
            'true | false && BUILD', "printf '%s' '&&'; BUILD",
            "sh -c 'false && go build -o /out/prog ./cmd/labeler' || BUILD",
            "sh -ec 'false; go build -o /out/prog ./cmd/labeler'; BUILD",
            "sh -c 'false; go build -o /out/prog ./cmd/labeler'",
        ]
        for script in cases:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                root, stage = self.fixture(directory)
                receipt = Path(directory) / 'calls'
                shim = Path(directory) / 'go'
                shim.write_text('#!/bin/sh\nprintf "build\\n" >> "$BUILD_RECEIPT"\n')
                shim.chmod(0o755)
                text = script.replace('BUILD', 'go build -o /out/prog ./cmd/labeler')
                oracle = subprocess.run(['sh', '-c', text], env={**os.environ,
                    'PATH': directory + os.pathsep + os.environ['PATH'], 'BUILD_RECEIPT': str(receipt)},
                    capture_output=True, text=True)
                calls = []
                def compiler(*args):
                    calls.append(args)
                    return 0, str(shim), ''
                try:
                    actual = build.run(stage, text, root, compiler)
                except build.BuildError as error:
                    self.assertIsNotNone(error.status, str(error))
                    actual = error.status
                self.assertEqual(actual, oracle.returncode)
                self.assertEqual(len(calls), len(receipt.read_text().splitlines()) if receipt.exists() else 0)

    def test_shell_pipefail_and_shell_option_overrides(self):
        for script in ['false | true; BUILD', 'set +o pipefail; false | true; BUILD',
                       "bash -e -o pipefail -c 'false | true'; BUILD"]:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                root, stage = self.fixture(directory)
                stage.shell = ['/bin/bash', '-e', '-o', 'pipefail', '-c']
                text = script.replace('BUILD', 'true')
                oracle = subprocess.run([*stage.shell, text], capture_output=True)
                try: actual = build.run(stage, text, root)
                except build.BuildError as error: actual = error.status
                self.assertEqual(actual, oracle.returncode)

    def test_quoting_cwd_and_environment_follow_the_selected_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = self.fixture(directory)
            calls = []
            def compiler(current, words, cwd, environment):
                calls.append((words, cwd, dict(environment)))
                return 0, str(root / 'cmd/labeler/main.go'), ''
            build.run(stage, 'mkdir -p /nested/source; cd /nested; export TAG=expanded; '
                      '(export TAG=temporary; cd /build); '
                      'CGO_ENABLED=0 go build -tags "$TAG" -ldflags \'$TAG literal\' ./cmd/labeler', root, compiler)
            words, cwd, environment = calls[0]
            self.assertEqual(cwd, '/nested')
            self.assertEqual(words, ['go', 'build', '-tags', 'expanded', '-ldflags', '$TAG literal', './cmd/labeler'])
            self.assertEqual(environment['TAG'], 'expanded')
            self.assertEqual(environment['CGO_ENABLED'], '0')
            self.assertNotIn('CGO_ENABLED', stage.env)

    def test_exec_run_does_not_expand_or_reinterpret_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = self.fixture(directory)
            stage.env['TAG'] = 'expanded'
            calls = []
            def compiler(current, words, cwd, environment):
                calls.append(words)
                return 0, str(root / 'cmd/labeler/main.go'), ''
            build.run(stage, '', root, compiler, argv=['go', 'build', '-tags', '$TAG', './cmd/labeler'])
            self.assertEqual(calls[0][3], '$TAG')

    def test_compiler_receives_copied_context_declared_flags_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = self.fixture(directory)
            (root / 'uncopied-secret.go').write_text('must not be in the image')
            calls = []
            def go(command, **options):
                calls.append((command, options))
                work = Path(options['cwd'])
                self.assertTrue((work / 'main.go').is_file())
                self.assertFalse((work.parents[1] / 'uncopied-secret.go').exists())
                self.assertEqual(options['env']['CGO_ENABLED'], '0')
                self.assertEqual(options['env']['GOOS'], 'linux')
                self.assertEqual(options['env']['GOPROXY'], 'off')
                self.assertIn('-tags=custom', command)
                self.assertIn('-trimpath', command)
                output = Path(command[command.index('-o') + 1])
                output.parent.mkdir(parents=True)
                output.write_bytes(b'real-stage-result')
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch.object(build.subprocess, 'run', side_effect=go):
                status, artifact, error = build.execute_go(stage,
                    ['go', '-C', '/build/cmd/labeler', 'build', '-tags=custom', '-trimpath', '-o', '/out/labeler', '.'],
                    '/build', {'CGO_ENABLED': '0', 'GOOS': 'linux'}, root, Path(directory) / 'builds')
            self.assertEqual(status, 0)
            self.assertEqual(Path(artifact).read_bytes(), b'real-stage-result')
            self.assertEqual(len(calls), 1)

    def test_each_compilation_uses_a_fresh_copy_without_deleted_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = self.fixture(directory)
            unused = root / 'cmd/labeler/deleted.go'; unused.write_text('package main\n')
            stage.files['/build/cmd/labeler/deleted.go'] = ('source', str(unused))
            directories = []
            def go(command, **options):
                work = Path(options['cwd']); directories.append(work)
                self.assertEqual((work / 'cmd/labeler/deleted.go').exists(), len(directories) == 1)
                if len(directories) == 1: (work / 'go.sum').write_text('module update\n')
                else: self.assertEqual((work / 'go.sum').read_text(), 'module update\n')
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch.object(build.subprocess, 'run', side_effect=go):
                for index in range(2):
                    build.execute_go(stage, ['go', 'mod', 'download'], '/build', {}, root, Path(directory) / 'builds')
                    stage.files.pop('/build/cmd/labeler/deleted.go', None)
            self.assertNotEqual(*directories)

    def test_compile_errors_and_missing_artifacts_cannot_establish_a_binary(self):
        for result in [(2, None, 'invalid flag'), (0, None, '')]:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as directory:
                root, stage = self.fixture(directory)
                with self.assertRaises(build.BuildError):
                    build.run(stage, 'go build -o /out/program ./cmd/labeler', root, lambda *args: result)
                self.assertFalse(any(kind == 'go' for kind, _ in stage.files.values()))

    def test_docker_shell_and_json_run_forms(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = self.fixture(directory)
            recipe = 'FROM golang:1.24\nWORKDIR /build\nCOPY . .\n'
            command = ['go', 'build', '-o', '/labeler', './cmd/labeler']
            (root / 'Dockerfile').write_text(recipe + 'RUN ' + json.dumps(command) + '\nENTRYPOINT ["/labeler"]\n')
            action = {'runs': {'image': 'Dockerfile'}}
            self.assertEqual(docker_plan(root, action)[1], ['/labeler'])
            (root / 'Dockerfile').write_text(recipe + 'SHELL ["/bin/sh", "-ec"]\nRUN false; '
                                           + ' '.join(command) + '\nENTRYPOINT ["/labeler"]\n')
            with self.assertRaises(build.BuildError): docker_plan(root, action)

    @unittest.skipUnless(shutil.which('go'), 'Go toolchain is unavailable on this host')
    def test_real_go_compilation_and_invalid_declared_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = self.fixture(directory)
            def compiler(current, words, cwd, environment):
                return build.execute_go(current, words, cwd, environment, root, Path(directory) / 'builds')
            build.run(stage, 'CGO_ENABLED=0 go build -o /out/program ./cmd/labeler', root, compiler)
            self.assertEqual(subprocess.run([stage.files['/out/program'][1]]).returncode, 0)
            with self.assertRaisesRegex(build.BuildError, 'flag'):
                build.run(stage, 'go build -invalid-verifier-regression-flag -o /out/broken ./cmd/labeler', root, compiler)


if __name__ == '__main__':
    unittest.main(verbosity=2)
