"""Lightweight regressions for the verifier's Go source classification."""

from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

from evidence import _marker_counts
from residue import go_test_skip_count, scan_residue_rules


ASSETS = Path(__file__).resolve().parents[1] / 'assets'
PROBES_PATH = ASSETS / 'shared/batches/BATCH-F2P-0003/probes.py'
PROBE_SOURCE = ASSETS / 'target/batches/BATCH-F2P-0003/f2p_observation_test.go'
spec = importlib.util.spec_from_file_location('quality_probes', PROBES_PATH)
probes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probes)


class GoClassificationTests(unittest.TestCase):
    def test_iterator_skip_is_an_operation(self):
        source = '''package p
import it "itertools"
func TestOperations(t *testing.T) { _ = it.Skip(source(), 3) }
'''
        self.assertEqual(go_test_skip_count(source), 0)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as name:
            root = Path(name)
            (root / 'iterator_test.go').write_text(source)
            self.assertEqual(_marker_counts(root), {'skip': 0, 'xfail': 0, 'disabled': 0})

    def test_renamed_testing_receivers_still_disable_tests(self):
        for call in ('alias.Skip("reason")', 'alias.SkipNow()', 'alias.Skipf("%s", "reason")'):
            self.assertEqual(go_test_skip_count('''package p
import "testing"
func TestCase(t *testing.T) { alias := t; ''' + call + ' }'), 1)

    def test_import_shadow_is_not_an_exemption(self):
        self.assertEqual(go_test_skip_count('''package p
import it "itertools"
func TestCase(t *testing.T) { it := t; it.Skip("reason") }
'''), 1)

    def test_comments_literals_and_short_query_do_not_disable_tests(self):
        self.assertEqual(go_test_skip_count('''package p
import "testing"
// alias.Skip("reason")
var text = `alias.SkipNow()`
func TestCase(t *testing.T) { _ = testing.Short() }
'''), 0)

    def test_import_comments_cannot_hide_a_skip(self):
        self.assertEqual(go_test_skip_count('''package p
import (
    "testing"
    // alias "example.invalid/package"
)
func TestCase(t *testing.T) { alias := t; alias.Skip("reason") }
'''), 1)

    def test_skip_in_an_ordinary_go_helper_is_counted(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as name:
            root = Path(name)
            (root / 'helper.go').write_text('package p\nfunc helper(probe *testing.T) { probe.SkipNow() }')
            self.assertEqual(_marker_counts(root)['skip'], 1)

    def test_production_fallback_scan_keeps_real_bridges(self):
        rules = [{
            'id': 'production-subprocess', 'kind': 'regex',
            'paths': ['**/*.go'], 'target': r'\bexec\.Command\s*\(',
            'scope': 'production_go',
        }]
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as name:
            root = Path(name)
            (root / 'build_tags_test.go').write_text('package p\nfunc TestTags(t *testing.T) { exec.Command("go", "build") }')
            self.assertEqual(scan_residue_rules(root, rules), [])
            (root / 'dist').mkdir()
            (root / 'dist' / 'bridge.go').write_text('package p\nfunc bridge() { exec.Command("cargo", "run") }')
            self.assertEqual([hit['path'] for hit in scan_residue_rules(root, rules)], ['dist/bridge.go'])


class GoProbeCompatibilityTests(unittest.TestCase):
    def run_protocol(self, runner):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'go.mod').write_text('module itertools\n\ngo 1.23.0\n')
            with patch.object(probes, 'run', side_effect=runner):
                return probes.check_go_tests(
                    str(root), str(PROBE_SOURCE), {'protocol': 'TestF2P059Protocol'},
                )

    def test_core_factory_and_root_factory_receive_identical_assertions(self):
        attempts = []

        def runner(command, cwd, timeout):
            source = Path(command[-1]).read_text()
            attempts.append(source)
            if 'return it.RcIter(source)' not in source:
                return 1, 'undefined: iterator constructor\n[build failed]'
            return 0, '--- PASS: TestF2P059Protocol (0.00s)'

        results, _, status = self.run_protocol(runner)
        self.assertTrue(results['protocol'][0])
        self.assertEqual(status, 0)
        self.assertIn('return core.NewRcIter(source)', attempts[0])
        # Constructor location changes must retain even the difficult checks.
        for source in attempts:
            for assertion in ('borrow panic=%v, want %v', 'shared initial hint:',
                              'FlattenOK lost DoubleEnded', 'product must resume at step'):
                self.assertIn(assertion, source)

    def test_runtime_failure_cannot_be_overridden_by_another_signature(self):
        for output in ('--- FAIL: TestF2P059Protocol (0.00s)\nwrong shared bound',
                       'panic: runtime failure before a test result'):
            with self.subTest(output=output):
                attempts = []

                def runner(command, cwd, timeout):
                    attempts.append(command)
                    if len(attempts) == 1:
                        return 1, output
                    return 0, '--- PASS: TestF2P059Protocol (0.00s)'

                results, _, status = self.run_protocol(runner)
                self.assertFalse(results['protocol'][0])
                self.assertEqual(status, 1)
                self.assertEqual(len(attempts), 1)

    def test_adaptor_probes_do_not_assume_empty_and_once_are_functions(self):
        source = PROBE_SOURCE.read_text()
        for test in ('TestF2P055Laziness', 'TestF2P059Protocol'):
            selected = probes.observation_source(source, test)
            self.assertNotIn('core.EmptyIter[', selected)
            self.assertNotIn('core.OnceIter(', selected)
            self.assertIn('core.FromSlice([]int{', selected)

    def test_successful_scan_does_not_hide_the_failed_export_check(self):
        rows = [('exports', [], 'TestExports', ['dep_scan'])]
        with patch.object(probes, 'OBSERVATIONS', rows), \
             patch.object(probes, 'check_go_tests', return_value=(
                 {'exports': (False, 'missing core.TupleBuffer')}, '', 1)), \
             patch.object(probes, 'check_dep_scan', return_value=(True, 'no Rust bridge')):
            results, _ = probes.evaluate('unused', 'unused')
        self.assertFalse(results['exports'][0])
        self.assertIn('missing core.TupleBuffer', results['exports'][1])
        self.assertIn('no Rust bridge', results['exports'][1])

    def test_successful_build_does_not_hide_the_failed_module_check(self):
        rows = [('module', [], None, ['go_mod', 'cgo_build'])]
        with patch.object(probes, 'OBSERVATIONS', rows), \
             patch.object(probes, 'check_go_tests', return_value=({}, '', 0)), \
             patch.object(probes, 'check_go_mod', return_value=(False, 'wrong module path')), \
             patch.object(probes, 'check_cgo_build', return_value=(True, 'build succeeded')):
            results, _ = probes.evaluate('unused', 'unused')
        self.assertFalse(results['module'][0])
        self.assertIn('wrong module path', results['module'][1])


if __name__ == '__main__':
    unittest.main()
