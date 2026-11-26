"""Offline evaluator regression checks: python3 tasks/tests/verifier/selftest.py.

These checks validate the evaluator and calibrate grouping against the bundled
JavaScript baseline. They do not claim to replace Go/Docker end-to-end regrade.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import evidence
from comparison import compare_observation_to_baseline
from integrity import validate_public_integrity, validate_public_projection_contract
from observation_adapter import GenericObservationAdapter
from observations import load_report_selection, project_report
from verifier_engine import evaluate_migration_units
from detectors.subprocess import detect_source_runtime_subprocess

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT.parent.parent / 'jobs'


def load_runner():
    loader = importlib.machinery.SourceFileLoader('labeler_selftest_runner', str(ROOT / 'assets/target/runner'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_blackbox():
    spec = importlib.util.spec_from_file_location('labeler_selftest_blackbox', ROOT / 'assets/target/blackbox/labeler_blackbox.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed_saved_reports():
    reports = []
    for report in sorted(JOBS.glob('*/*/verifier/result.json')):
        saved = json.loads((report.parent / 'verifier-result.json').read_text())
        # Infrastructure attempts can contain native XML and no observations;
        # they are not completed migration reports suitable for behavior replay.
        if saved.get('status') != 'infra_error':
            reports.append(report)
    return reports


class EvaluatorTests(unittest.TestCase):
    def test_process_policy_distinguishes_commands_from_comments_and_graphql_fields(self):
        cases = {
            'field': ('package api\nconst field = "node"\n', False),
            'comment': ('package api\n// Never use exec.Command("node") here.\n', False),
            'git': ('package api\nimport "os/exec"\nconst field = "node"\n'
                    'func version() { exec.Command("git", "rev-parse", "HEAD").Run() }\n', False),
            'direct': ('package api\nimport "os/exec"\n'
                       'func run() { exec.Command("node", "dist/index.js").Run() }\n', True),
            'context-alias': ('package api\nimport child "os/exec"\n'
                              'func run() { child.CommandContext(ctx, "node", "dist/index.js").Run() }\n', True),
            'dynamic': ('package api\nimport "os/exec"\nconst runtime = "node"\n'
                        'func run() { exec.Command(runtime, "dist/index.js").Run() }\n', True),
        }
        for name, (source, rejected) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'main.go').write_text(source)
                self.assertEqual(bool(detect_source_runtime_subprocess(root, {}, tokens=['node'])), rejected)

    @unittest.skipUnless(shutil.which('go'), 'Go is required for actual entrypoint policy execution')
    def test_go_policy_accepts_wrappers_and_entry_filenames_and_rejects_legacy_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'go.mod').write_text('module github.com/actions/labeler\n\ngo 1.24.0\n\nrequire gopkg.in/yaml.v3 v3.0.1\n')
            target = root / 'target/policy'
            shutil.copytree(ROOT / 'assets/target/batches/BATCH-F2P-0003-static', target)
            shutil.copytree(ROOT / 'assets/target/blackbox', root / 'blackbox',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            entry = root / 'cmd/labeler/labeler.go'
            entry.parent.mkdir(parents=True)
            clean = ('package main\nimport ("os/exec"; "os"; "net/http"; "strings")\nconst field = "node"\n'
                     '// Never use exec.Command("node") to delegate.\n'
                     'func version() { exec.Command("git", "rev-parse", "HEAD").Run() }\n'
                     'func main() {\n'
                     ' data, err := os.ReadFile(os.Getenv("INPUT_CONFIGURATION-PATH")); if err != nil || !strings.Contains(string(data), "documentation:") { os.Exit(1) }\n'
                     ' response, err := http.Post(os.Getenv("GITHUB_API_URL")+"/repos/monalisa/helloworld/issues/123/labels", "application/json", strings.NewReader(`{"labels":["documentation"]}`)); if err != nil { os.Exit(1) }; response.Body.Close()\n'
                     ' output, err := os.OpenFile(os.Getenv("GITHUB_OUTPUT"), os.O_APPEND|os.O_WRONLY, 0600); if err != nil { os.Exit(1) }; defer output.Close()\n'
                     ' output.WriteString("new-labels=documentation\\nall-labels=documentation\\n")\n'
                     '}\n')
            entry.write_text(clean)
            (root / 'scripts').mkdir()
            (root / 'scripts/run.sh').write_text('#!/bin/bash\nset -e\ngo build -C "$GITHUB_ACTION_PATH" -o "$RUNNER_TEMP/labeler" ./cmd/labeler\nexec "$RUNNER_TEMP/labeler"\n')
            (root / 'scripts/run.sh').chmod(0o755)
            wrapper = json.dumps({
                'inputs': {'repo-token': {}, 'configuration-path': {}},
                'outputs': {name: {'value': '${{ steps.execute.outputs.' + name + ' }}'}
                            for name in ('new-labels', 'all-labels')},
                'runs': {'using': 'composite', 'steps': [{'id': 'execute',
                    'run': '"$GITHUB_ACTION_PATH/scripts/run.sh"', 'shell': 'bash',
                    'env': {'INPUT_CONFIGURATION-PATH': '${{ inputs.configuration-path }}',
                            'INPUT_REPO-TOKEN': '${{ inputs.repo-token }}'}}]}})
            (root / 'labeler.Dockerfile').write_text('FROM golang:1.24 AS build\nWORKDIR /src\nCOPY . .\nRUN go build -o /custom-labeler ./cmd/labeler\nFROM scratch\nCOPY --from=build /custom-labeler /custom-labeler\nENTRYPOINT ["/custom-labeler"]\n')
            action = root / 'action.yml'
            cases = [
                ('wrapper-and-renamed-main', wrapper, clean, True),
                ('quoted-docker', "'runs': {'using': 'docker', 'image': 'labeler.Dockerfile'}\n", clean, True),
                ('node-runtime', 'runs:\n  using: node24\n  main: dist/index.js\n', clean, False),
                ('missing-main', wrapper, 'package main\n', False),
                ('node-delegate', wrapper, clean.replace('"git", "rev-parse", "HEAD"', '"node", "dist/index.js"'), False),
                ('shell-data', wrapper, clean.replace('"git", "rev-parse", "HEAD"', '"sh", "-c", "printf %s node"'), True),
                ('shell-delegate', wrapper, clean.replace('"git", "rev-parse", "HEAD"', '"sh", "-c", "exec node dist/index.js"'), False),
                ('context-alias-shell-data', wrapper,
                 clean.replace('"os/exec";', 'child "os/exec"; "context";').replace(
                     'exec.Command("git", "rev-parse", "HEAD")',
                     'child.CommandContext(context.Background(), "sh", "-c", "printf %s node")'), True),
            ]
            for name, declaration, source, passes in cases:
                with self.subTest(case=name):
                    action.write_text(declaration)
                    entry.write_text(source)
                    run = subprocess.run(
                        ['go', 'test', '-count=1', '-run', 'TestF2P0(69|70|71|72)', './target/policy/'],
                        cwd=root, capture_output=True, text=True, timeout=120,
                        env={**os.environ, 'GOFLAGS': '-mod=mod', 'GOPROXY': 'off', 'GOSUMDB': 'off'},
                    )
                    self.assertEqual(run.returncode == 0, passes, run.stdout + run.stderr)

    def test_integrity_and_projection_inventory(self):
        config = json.loads((ROOT / 'config.json').read_text())
        validate_public_integrity(ROOT)
        validate_public_projection_contract(ROOT, config['public_contract'])

    def test_harness_cases_exist_and_loading_is_separate(self):
        module = load_blackbox()
        harness = json.loads((ROOT / 'assets/shared/integration/harness.json').read_text())
        for identifier, entry in harness['observations'].items():
            command = entry.get('target', entry.get('predicate', {})).get('command', [])
            if '--case' in command:
                self.assertIn(command[command.index('--case') + 1], module.CASES, identifier)
        for identifier in ('P2P-005', 'P2P-019'):
            self.assertEqual(harness['observations'][identifier]['target']['command'][-1], 'config-loading')
        self.assertEqual(harness['observations']['F2P-040']['predicate']['command'][-1], 'source-aggregation')

    def test_capabilities_are_scored_once_and_have_one_scope_owner(self):
        config = json.loads((ROOT / 'config.json').read_text())
        harness = json.loads((ROOT / 'assets/shared/integration/harness.json').read_text())
        aliases = config['observation_aliases']
        active = {row['id'] for rows in config['tests'].values() for row in rows}
        self.assertFalse(active & set(aliases))
        self.assertEqual(active, set(harness['observation_ids']))
        self.assertTrue(set(aliases.values()) <= set(config['f2p']))
        owners = [identifier for unit in config['migration_unit_checks'].values()
                  for identifier in unit['observation_ids']]
        self.assertCountEqual(owners, config['f2p'])
        self.assertEqual(len(owners), len(set(owners)))
        specs = [json.dumps(harness['observations'][identifier]['predicate'], sort_keys=True)
                 for identifier in config['f2p']]
        self.assertEqual(len(specs), len(set(specs)))
        rows = [{'id': identifier, 'status': 'FAIL' if identifier == 'F2P-029' else 'PASS'}
                for identifier in config['f2p']]
        units = json.loads((ROOT / 'assets/migration-units.json').read_text())['units']
        units = [unit for unit in units if config['migration_unit_checks'][unit['id']]['observation_ids']]
        results = evaluate_migration_units(units, config['migration_unit_checks'], rows)
        self.assertEqual([row['id'] for row in results if row['status'] == 'FAIL'], ['MU006'])

    def test_active_inventories_and_test_migration_references_agree(self):
        config = json.loads((ROOT / 'config.json').read_text())
        active = {row['id'] for rows in config['tests'].values() for row in rows}
        paths = ('assets/shared/integration/observations.json',
                 'assets/source-baseline-outputs.json')
        for path in paths:
            rows = json.loads((ROOT / path).read_text())['observations']
            self.assertCountEqual([row['id'] for row in rows], active, path)
        selection = json.loads((ROOT / 'assets/shared/validated-observations/selection.json').read_text())
        self.assertCountEqual(selection['accepted_ids'], active)
        self.assertCountEqual(selection['original_ids'], active)
        self.assertTrue(active <= set(selection['historical_ids']))
        inventory = json.loads((ROOT / 'assets/case-observation-inventory.json').read_text())
        self.assertCountEqual([row['id'] for rows in inventory['groups'].values() for row in rows], active)
        for source, spec in config['policy']['source_test_migrations'].items():
            self.assertTrue(set(spec['observation_ids']) <= active, source)
        import tomllib
        metadata = tomllib.loads((ROOT.parent / 'task.toml').read_text())['metadata']['tests']
        self.assertEqual(metadata['fail_to_pass_count'], len(config['tests']['f2p']))
        self.assertEqual(metadata['pass_to_pass_count'], len(config['tests']['p2p']))

    def test_current_report_projection_preserves_failures_and_rejects_inventory_mutations(self):
        config = json.loads((ROOT / 'config.json').read_text())
        selection = load_report_selection(ROOT / 'assets')
        rows = [{'id': row['id'], 'kind': row['kind'],
                 'status': 'FAIL' if row['id'] == 'F2P-029' else 'PASS',
                 'value': {'type': 'json', 'value': {'result': 'FAIL' if row['id'] == 'F2P-029' else 'PASS'}}}
                for group in config['tests'].values() for row in group]
        adapter = GenericObservationAdapter()

        def validate(items):
            normalized = adapter.normalize_report({'schema_version': 'migration-observations:v6',
                                                  'observations': items},
                                                 role='candidate', returncode=0,
                                                 report_path='/logs/result.json', config=config)
            projected, errors = project_report(normalized, selection)
            return projected, errors + adapter.validate_report(projected, config, actual_returncode=0)

        projected, errors = validate(rows)
        self.assertEqual(errors, ())
        self.assertEqual(next(row['status'] for row in projected['observations'] if row['id'] == 'F2P-029'), 'FAIL')
        unknown = {**rows[0], 'id': 'UNKNOWN-001'}
        legacy = {**rows[0], 'id': 'F2P-006', 'kind': 'F2P'}
        for label, mutated in [('missing', rows[1:]), ('duplicate', rows + rows[:1]),
                               ('unknown', rows + [unknown]), ('replacement', rows[:-1] + [unknown]),
                               ('historical-alias', rows + [legacy])]:
            with self.subTest(label=label):
                self.assertTrue(validate(mutated)[1])

    @unittest.skipUnless(list(JOBS.glob('*/*/verifier/result.json')), 'Saved jobs are required for report replay')
    def test_saved_job_reports_pass_full_projection_boundary_without_changing_observations(self):
        config = json.loads((ROOT / 'config.json').read_text())
        selection = load_report_selection(ROOT / 'assets')
        adapter = GenericObservationAdapter()
        reports = completed_saved_reports()
        if not reports:
            self.skipTest('No completed saved migration reports; archived attempts are infrastructure errors')
        for report in reports:
            with self.subTest(report=str(report)):
                raw = json.loads(report.read_text())
                normalized = adapter.normalize_report(raw, role='candidate', returncode=0,
                                                      report_path='/logs/result.json', config=config)
                projected, errors = project_report(normalized, selection)
                self.assertEqual(errors, ())
                self.assertEqual(adapter.validate_report(projected, config, actual_returncode=0), ())
                self.assertEqual(projected['observations'], normalized['observations'])
                saved = json.loads((report.parent / 'verifier-result.json').read_text())
                receipts = [dict(row) for row in saved['receipts']['commands']]
                # Mirror the live entrypoint: a structurally valid report now
                # supplies the dependency evidence, without rerunning commands.
                for row in receipts:
                    if row['kind'] == 'report':
                        row['valid'] = True
                build, dependencies, _ = evidence._derive_execution_atoms(receipts)
                self.assertEqual(build['status'], 'PASS')
                self.assertEqual(dependencies['status'], 'PASS')

    @unittest.skipUnless(shutil.which('git'), 'Git is required for patch checks')
    def test_generated_patches_apply_and_match_public_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            projection = work / 'projection'
            projection.mkdir()
            subprocess.run(['git', 'init', '--quiet', str(projection)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(projection), 'apply', str(ROOT / 'verifier/test.patch')],
                           check=True, capture_output=True)
            for tree in ('target', 'shared', 'fixtures'):
                assets = ROOT / 'assets' / tree
                expected = {path.relative_to(assets): path.read_bytes() for path in assets.rglob('*')
                            if path.is_file() and '__pycache__' not in path.parts}
                projected = projection / 'tests' / tree
                actual = {path.relative_to(projected): path.read_bytes() for path in projected.rglob('*')
                          if path.is_file()}
                self.assertEqual(actual, expected, tree)
            self.assertEqual((projection / 'tests/source/runner').read_bytes(),
                             (ROOT / 'assets/target/runner').read_bytes())
            reference = work / 'reference'
            subprocess.run(['git', 'clone', '--quiet', str(ROOT / 'assets/build-deps/repo/base-repo.bundle'),
                            str(reference)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(reference), 'apply', '--binary',
                            str(ROOT.parent / 'solution/solution.patch')], check=True, capture_output=True)
            self.assertEqual((reference / 'testdata/oracle/minimatch_contract.json').read_bytes(),
                             (ROOT / 'assets/target/blackbox/minimatch.json').read_bytes())
            self.assertEqual(evidence.build_source_test_baseline(reference),
                             json.loads((ROOT / 'assets/target-test-baseline.json').read_text()))

    def test_invalid_structure_does_not_fail_unrelated_config_probes(self):
        module = load_blackbox()

        def structure_defect(binary, state, failures, config, *args, **kwargs):
            if 'changed-files: {any-glob-to-any-file:' in config:
                failures.append('accepted malformed changed-files mapping')
            return 0, 'changed-files-labels-limit max-files-changed unknown-option', {}

        state = module.State()
        probes = ['config-file-structure', 'config-glob-types', 'config-limit-validation',
                  'source-file-parser', 'config-unknown']
        with patch.object(module, '_expect', side_effect=structure_defect):
            failed = [name for name in probes if module.CASES[name]('binary', state)]
        self.assertEqual(failed, ['config-file-structure'])

    def test_branch_suites_do_not_share_invalid_pattern_failure(self):
        module = load_blackbox()

        def invalid_pattern_defect(binary, state, failures, config, *args, **kwargs):
            if '[bad' in config:
                failures.append('invalid regexp silently accepted')
            return 0, '', {}

        with patch.object(module, '_expect', side_effect=invalid_pattern_defect):
            state = module.State()
            self.assertTrue(module.CASES['source-head-branches']('binary', state))
            self.assertFalse(module.CASES['source-base-branches']('binary', state))
            self.assertFalse(module.CASES['source-no-branches']('binary', state))

    def test_differential_compares_actual_results_and_requires_projection(self):
        runner = load_runner()
        spec = {'type': 'command', 'command': ['python3', 'blackbox/labeler_blackbox.py'],
                'projection': 'json-line'}
        with patch.object(runner, 'run_command', return_value=('PASS', 0, 'LABELER_PROJECTION {"edge":false}\n')):
            status, code, projection = runner.exec_spec(spec, set())
        self.assertEqual((status, code), ('PASS', 0))
        self.assertEqual(projection, {'data': {'edge': False}})
        baseline = {'status': 'PASS', 'value': {'type': 'json', 'value': {'edge': True}}}
        candidate = {'status': status, 'value': {'type': 'json', 'value': projection['data']}}
        self.assertEqual(compare_observation_to_baseline(baseline, candidate), 'FAIL')
        for output in ('PASS\n', 'LABELER_PROJECTION {}\n', 'LABELER_PROJECTION {"edge":"PASS"}\n'):
            with self.subTest(output=output), patch.object(runner, 'run_command', return_value=('PASS', 0, output)):
                self.assertEqual(runner.exec_spec(spec, set())[0], 'INCONCLUSIVE')

    def test_probes_project_candidate_results_instead_of_expected_values(self):
        module = load_blackbox()
        fixtures = [('minimatch-contract', module.minimatch_cases()),
                    ('source-aggregation', json.loads((ROOT / 'assets/target/blackbox/grouping.json').read_text())['cases'])]
        for case_name, cases in fixtures:
            with self.subTest(case=case_name):
                module.PROJECTION.clear()

                def wrong_result(binary, state, failures, config, want='', *args, **kwargs):
                    failures.append('candidate produced the wrong result')
                    # Invert the expected label addition to model a broken candidate.
                    label = 'matched' if case_name == 'minimatch-contract' else 'both'
                    return 0, '', {'new-labels': '' if want else label}

                with patch.object(module, '_expect', side_effect=wrong_result), \
                     patch.object(module, '_f2p024', return_value=[]):
                    state = module.State()
                    state.api_url = 'http://unused.invalid'
                    self.assertTrue(module.CASES[case_name]('binary', state))
                self.assertEqual(module.PROJECTION, {row['id']: not row['matches'] for row in cases})

    def test_contracts_execute_yaml_and_behavior_checks(self):
        runner = load_runner()
        harness = json.loads((ROOT / 'assets/shared/integration/harness.json').read_text())
        for identifier in ('CONTRACT-001', 'CONTRACT-002', 'CONTRACT-025'):
            spec = harness['observations'][identifier]['target']
            self.assertIn('go_test', spec)
            with patch.object(runner, 'ensure_asset_dir'), patch.object(runner, 'run_command', return_value=('FAIL', 1, 'contract failure')) as execute:
                self.assertEqual(runner.contract_check(spec), 'FAIL')
                package = './target/contracts/surface/' if identifier == 'CONTRACT-025' else './target/contracts/'
                self.assertEqual(execute.call_args.args[0][-1], package)
        for number in range(3, 25):
            self.assertTrue(harness['observations'][f'CONTRACT-{number:03d}']['target']['behavior_cases'])

    def test_configuration_contract_executes_all_validation_families(self):
        runner = load_runner()
        harness = json.loads((ROOT / 'assets/shared/integration/harness.json').read_text())
        spec = harness['observations']['CONTRACT-017']['target']
        expected = {'source-file-parser', 'config-file-structure', 'config-glob-types',
                    'config-limit-validation', 'config-unknown'}
        self.assertEqual(set(spec['behavior_cases']), expected)
        # Strict target postconditions must not be imposed on the older source
        # parser, which legitimately accepts the mapping used by F2P-029.
        source = harness['observations']['CONTRACT-017']['source']
        self.assertNotIn('config-file-structure', source['behavior_cases'])
        self.assertNotIn('config-glob-types', source['behavior_cases'])
        for failing in [None, *sorted(expected)]:
            def execute(command, *args, **kwargs):
                case = command[command.index('--case') + 1]
                return ('FAIL', 1, 'validation defect') if case == failing else ('PASS', 0, '')
            with self.subTest(failing=failing), patch.object(runner, 'ensure_asset_dir'), \
                 patch.object(runner, 'run_command', side_effect=execute) as run:
                self.assertEqual(runner.contract_check(spec), 'PASS' if failing is None else 'FAIL')
                self.assertEqual(run.call_count, len(expected))

    @unittest.skipUnless(list(JOBS.glob('*/*/verifier/verifier-result.json')), 'Saved jobs are required for contract replay')
    def test_saved_malformed_config_failures_cannot_pass_validation_contract(self):
        runner = load_runner()
        harness = json.loads((ROOT / 'assets/shared/integration/harness.json').read_text())
        spec = harness['observations']['CONTRACT-017']['target']
        bindings = {'source-file-parser': 'F2P-054', 'config-file-structure': 'F2P-029',
                    'config-glob-types': 'F2P-032', 'config-limit-validation': 'F2P-041',
                    'config-unknown': 'F2P-063'}
        reports = [report.with_name('verifier-result.json') for report in completed_saved_reports()]
        if not reports:
            self.skipTest('No completed saved migration reports; archived attempts are infrastructure errors')
        for report in reports:
            rows = {row['id']: row for row in json.loads(report.read_text())['observations']}
            if rows['F2P-029']['status'] != 'FAIL':
                continue
            def execute(command, *args, **kwargs):
                row = rows[bindings[command[command.index('--case') + 1]]]
                return row['status'], 0 if row['status'] == 'PASS' else 1, ''
            with self.subTest(report=str(report)), patch.object(runner, 'ensure_asset_dir'), \
                 patch.object(runner, 'run_command', side_effect=execute):
                self.assertEqual(runner.contract_check(spec), 'FAIL')

    @unittest.skipUnless(shutil.which('go'), 'Go is required for actual API probe compilation')
    def test_public_api_accepts_function_and_type_but_rejects_missing_or_uncompilable_exports(self):
        # Compile and execute the real evaluator Go probe against tiny modules.
        # This catches declaration-kind assumptions that dispatch mocks cannot.
        packages = {
            'config': 'type LabelConfigResult struct{}\nfunc GetLabelConfigResultFromObject() {}',
            'core': 'var Out any\nfunc Reset() {}',
            'event': 'type Payload struct{}\ntype PullRequest struct{}\ntype Branch struct{}',
            'githubapi': 'type Client struct{}\nfunc NewClientForTest() {}',
            'inputs': 'type Inputs struct{}\nfunc EscapeNonPrintable() {}',
            'match': 'type BranchContext struct{}\nfunc CheckAnyBranch() {}',
            'minimatch': 'func NewMatcher() {}',
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'go.mod').write_text('module github.com/actions/labeler\n\ngo 1.20\n')
            for name, body in packages.items():
                path = root / 'internal' / name / 'api.go'
                path.parent.mkdir(parents=True)
                path.write_text('package ' + name + '\n' + body + '\n')
            labeler = root / 'internal/labeler/api.go'
            labeler.parent.mkdir(parents=True)
            target = root / 'target/contracts/surface'
            shutil.copytree(ROOT / 'assets/target/contracts/surface', target)
            for name, body, succeeds in [('function', 'func Labeler(options ...any) error { return nil }', True),
                                          ('type', 'type Labeler struct{}', True),
                                          ('missing', '', False),
                                          ('uncompilable', 'type Labeler MissingType', False)]:
                with self.subTest(form=name):
                    labeler.write_text('package labeler\n' + body + '\n')
                    run = subprocess.run(['go', 'test', '-count=1', './target/contracts/surface/'],
                                         cwd=root, capture_output=True, text=True, timeout=60)
                    self.assertEqual(run.returncode == 0, succeeds, run.stdout + run.stderr)
                    if name == 'missing':
                        self.assertIn('internal/labeler must export Labeler', run.stdout + run.stderr)

    @unittest.skipUnless(shutil.which('node') and shutil.which('git'), 'Node and Git are required for source calibration')
    def test_minimatch_oracle_and_known_false_positive_mutations(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            subprocess.run(['git', 'clone', '--quiet', str(ROOT / 'assets/build-deps/repo/base-repo.bundle'),
                            str(work / 'repo')], check=True, capture_output=True)
            source = (work / 'repo/dist/index.js').read_text().rstrip()
            self.assertTrue(source.endswith('run();'))
            module = work / 'oracle.mjs'
            module.write_text(source[:-len('run();')] + '\nexport {Minimatch};\n')
            driver = r"""
import {readFileSync} from 'node:fs';
const write = process.stdout.write.bind(process.stdout);
process.stdout.write = () => true;
const {Minimatch} = await import(process.argv[1]);
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8')).cases;
const result = {};
for (const mode of ['baseline', 'no-normalization', 'ascii-graph']) {
  result[mode] = cases.filter(c => {
    const pattern = mode === 'ascii-graph' ? c.pattern.replace('[[:graph:]]', '[#-~!\"]') : c.pattern;
    const options = {dot: c.dot, optimizationLevel: mode === 'no-normalization' ? 0 : 1};
    return new Minimatch(pattern, options).match(c.path) !== c.matches;
  }).map(c => c.id);
}
write(JSON.stringify(result));
"""
            fixture = ROOT / 'assets/target/blackbox/minimatch.json'
            result = subprocess.run(['node', '--input-type=module', '-e', driver, module.as_uri(), str(fixture)],
                                    capture_output=True, text=True, timeout=30, check=True)
            failures = json.loads(result.stdout)
            self.assertEqual(failures['baseline'], [])
            self.assertIn('parent-file', failures['no-normalization'])
            self.assertIn('graph-positive', failures['ascii-graph'])
            self.assertIn('graph-astral', failures['ascii-graph'])
            baseline = json.loads((ROOT / 'assets/source-baseline-outputs.json').read_text())
            stored = next(row['value']['value'] for row in baseline['observations'] if row['id'] == 'DIFF-006')
            self.assertEqual(stored, {row['id']: row['matches'] for row in json.loads(fixture.read_text())['cases']})

    def test_go_requires_completed_non_skipped_tests(self):
        runner = load_runner()
        for output in ('?\tpkg\t[no test files]\n',
                       '{"Action":"start","Package":"pkg"}\n{"Action":"pass","Package":"pkg"}\n',
                       '{"Action":"run","Package":"pkg","Test":"TestA"}\n'
                       '{"Action":"skip","Package":"pkg","Test":"TestA"}\n'):
            with self.subTest(output=output), patch.object(runner, '_run_command', return_value=('PASS', 0, output)):
                status, _, _ = runner.run_command(['go', 'test', './...'], '/tmp', 10)
                self.assertEqual(status, 'FAIL')
        events = '\n'.join(json.dumps({'Action': action, 'Package': 'pkg', 'Test': 'TestA'}) for action in ('run', 'pass'))
        with patch.object(runner, '_run_command', return_value=('PASS', 0, events)) as execute:
            self.assertEqual(runner.run_command(['go', 'test', './...'], '/tmp', 10)[0], 'PASS')
            self.assertIn('-json', execute.call_args.args[0])
            self.assertIn('-count=1', execute.call_args.args[0])
        with patch.object(runner, '_run_command', return_value=('PASS', 0, 'broken JSON')):
            self.assertEqual(runner.run_command(['go', 'test', './...'], '/tmp', 10)[0], 'INCONCLUSIVE')

    def test_probe_failures_and_infrastructure_are_preserved(self):
        runner = load_runner()
        for status, code in (('FAIL', 1), ('INFRA_ERROR', None), ('TIMEOUT', 124)):
            with self.subTest(status=status), patch.object(runner, '_run_command', return_value=(status, code, 'diagnostic')):
                self.assertEqual(runner.run_command(['go', 'test', './...'], '/tmp', 10)[0], status)

    def test_ported_tests_allow_integration_locations_and_keep_functional_failure(self):
        config = {'migration': {'category': 'language'}, 'policy': {
            'evaluator_injected_paths': ['blackbox'], 'source_test_migrations': {
                '__tests__/branch.test.ts': {'observation_ids': ['P2P-008'], 'coverage_patterns': ['(?i)branch']}}}}
        receipt = [{'kind': 'target-native-test', 'returncode': 0, 'timed_out': False}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            test = root / 'integration/branch_test.go'
            test.parent.mkdir()
            test.write_text('package integration\nimport "testing"\nfunc TestBranch(t *testing.T) { if got != want { t.Fatal("branch mismatch") } }\n')
            for status in ('PASS', 'FAIL'):
                with self.subTest(status=status):
                    result = evidence._ported_source_tests(root, config, [{'id': 'P2P-008', 'status': status}], receipts=receipt)
                    self.assertIn('__tests__/branch.test.ts', result)
            self.assertFalse(evidence._ported_source_tests(root, config, [{'id': 'P2P-008', 'status': 'TIMEOUT'}], receipts=receipt))
            self.assertFalse(evidence._ported_source_tests(root, config, [{'id': 'P2P-008', 'status': 'PASS'}]))
            test.write_text('package integration\nimport "testing"\nfunc TestBranch(t *testing.T) {}\n')
            self.assertFalse(evidence._ported_source_tests(root, config, [{'id': 'P2P-008', 'status': 'PASS'}], receipts=receipt))
            test.unlink()
            test = root / 'blackbox/branch_test.go'
            test.parent.mkdir()
            test.write_text('package blackbox\nimport "testing"\nfunc TestBranch(t *testing.T) { t.Fatal("branch") }\n')
            self.assertFalse(evidence._ported_source_tests(root, config, [{'id': 'P2P-008', 'status': 'PASS'}], receipts=receipt))

    @unittest.skipUnless(shutil.which('node') and shutil.which('git'), 'Node and Git are required for source calibration')
    def test_source_grouping_and_semantic_mutations(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            subprocess.run(['git', '-c', 'advice.detachedHead=false', 'clone', '--quiet',
                            str(ROOT / 'assets/build-deps/repo/base-repo.bundle'), str(work / 'repo')], check=True, capture_output=True)
            original = (work / 'repo/dist/index.js').read_text()
            self.assertTrue(original.rstrip().endswith('run();'))
            original = original.rstrip()[:-len('run();')]
            mutations = {
                'baseline': original,
                'plain-rules-conjoined': re.sub(r'updatedConfig\.push\(\{\s*any:\s*\[newMatchConfig\]\s*\}\);',
                                                'updatedConfig.push({all: [newMatchConfig]});', original),
                'normalized-groups-disjoined': original.replace(
                    'if (!checkMatch(changedFiles, config, dot)) {\n            return false;\n        }\n    }\n    return true;',
                    'if (checkMatch(changedFiles, config, dot)) {\n            return true;\n        }\n    }\n    return false;'),
            }
            fixture = ROOT / 'assets/target/blackbox/grouping.json'
            for name, source in mutations.items():
                with self.subTest(name=name):
                    if name != 'baseline':
                        self.assertTrue(source != original, 'mutation must modify real code')
                    module = work / (name + '.mjs')
                    module.write_text(source + '\nexport {getLabelConfigResultFromObject, checkMatchConfigs, github_context};\n')
                    driver = """
import {readFileSync} from 'node:fs';
const write = process.stdout.write.bind(process.stdout);
process.stdout.write = () => true;
const api = await import(process.argv[1]);
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8')).cases;
const failed = [];
for (const scenario of cases) {
  api.github_context.payload = {pull_request: {head: {ref: scenario.head}, base: {ref: scenario.base}}};
  const configs = api.getLabelConfigResultFromObject({both: scenario.rules}).labelConfigs.get('both');
  if (api.checkMatchConfigs(scenario.files, configs, true) !== scenario.matches) failed.push(scenario.id);
}
write(JSON.stringify({cases: cases.length, failed}));
"""
                    result = subprocess.run(['node', '--input-type=module', '-e', driver, module.as_uri(), str(fixture)],
                                            capture_output=True, text=True, timeout=30, check=True)
                    report = json.loads(result.stdout)
                    self.assertGreaterEqual(report['cases'], 60)
                    if name == 'baseline':
                        self.assertEqual(report['failed'], [])
                        baseline = json.loads((ROOT / 'assets/source-baseline-outputs.json').read_text())
                        stored = next(row['value']['value'] for row in baseline['observations'] if row['id'] == 'DIFF-009')
                        self.assertEqual(stored, {row['id']: row['matches'] for row in json.loads(fixture.read_text())['cases']})
                    else:
                        self.assertTrue(report['failed'], 'semantic mutation escaped the evaluator')


if __name__ == '__main__':
    unittest.main(verbosity=2)
