"""Two-sided regressions for the GopenPGP regrade fixes (no Go execution)."""
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verifier import evidence, residue
from verifier.hardgate_engine import COMPOUND_ATOMS, evaluate_hardgate

TESTS = Path(__file__).resolve().parents[1]
CONFIG = json.loads((TESTS / 'config.json').read_text())
TARGET = 'github.com/emersion/go-message'
INTEGRATION = TESTS / 'assets/shared/integration'
sys.path.insert(0, str(INTEGRATION))
spec = importlib.util.spec_from_file_location('regrade_harness', INTEGRATION / 'harness_lib.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
sys.path.pop(0)


class RegradeRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'mime').mkdir()

    def source(self, text):
        (self.root / 'mime/mime.go').write_text('package mime\n' + text)

    def atom(self):
        return evidence.build_hardgate_evidence(
            self.root, config=CONFIG, observations=[], observation_errors=[], receipts=[],
            patch_applicable=True, source_test_baseline=None)['no_deceptive_source_fallback']

    def test_supported_parser_apis_and_parenthesized_function_values(self):
        for call in ('gm.Read(nil)', 'gm.ReadWithOptions(nil, nil)', 'gm.New(h, r)',
                     'gm.NewMultipart(h, parts)', '(gm.Read)(nil)', '((gm.Read))(nil)',
                     'parse := (gm.Read); again := ((parse)); again(nil)'):
            with self.subTest(call=call):
                self.source(f'import gm "{TARGET}"\nfunc f() {{ {call} }}')
                self.assertEqual(self.atom()['status'], 'PASS')

    def test_function_references_and_wrong_imports_still_fail(self):
        for code in (f'import gm "{TARGET}"\nvar _ = (gm.Read)',
                     f'import gm "{TARGET}"\nvar parse = (gm.Read)',
                     f'import gm "{TARGET}"\nfunc f() {{ consume(gm.Read)(nil) }}',
                     'import gm "example.org/message"\nfunc f() { (gm.Read)(nil) }'):
            with self.subTest(code=code):
                self.source(code)
                self.assertEqual(self.atom()['status'], 'FAIL')

    def test_parenthesized_legacy_parser_still_detects_consumption(self):
        for call, expected in (
            ('m, _ := (mail.ReadMessage)(nil); use(m)', True),
            ('return ((mail.ReadMessage))(nil)', True),
            ('_, err := consume((mail.ReadMessage)(nil)); _ = err', True),
            ('_, err := (mail.ReadMessage)(nil); _ = err', False),
            ('((mail.ReadMessage))(nil)', False),
            ('r := (multipart.NewReader)(nil, "b"); r.NextPart()', True),
        ):
            with self.subTest(call=call):
                self.source(f'import ("net/mail"; "mime/multipart"; gm "{TARGET}")\n'
                            f'func f() {{ {call}; gm.Read(nil) }}')
                # Preserve the lexical consumption check, while distinguishing
                # baseline envelope reading from migrated multipart traversal.
                source = evidence.GoSource((self.root / 'mime/mime.go').read_text())
                package = 'mime/multipart' if 'multipart.NewReader' in call else 'net/mail'
                symbol = 'NewReader' if package == 'mime/multipart' else 'ReadMessage'
                consumed = any(not source.discards_first_result(start, opening)
                               for start, opening in source.calls(package, [symbol]))
                self.assertEqual(consumed, expected)
                self.assertEqual(evidence._target_reimplemented(self.root, CONFIG['policy']),
                                 expected and package == 'mime/multipart')

    def test_diagnostics_are_not_runtime_imports(self):
        for word in ('unsafe', 'plugin', 'syscall', 'os/exec', 'exec.Command',
                     'ForkExec', 'StartProcess', 'net/rpc', 'github.com/ProtonMail/go-mime'):
            with self.subTest(word=word):
                self.source(f'import gm "{TARGET}"\nconst diagnostic = "{word}"\n'
                            'func f() { gm.Read(nil) }')
                self.assertEqual(self.atom()['status'], 'PASS')
                self.assertTrue(harness.check_runtime_dependency(str(self.root))[0])
                self.assertTrue(harness.check_mime_imports_target(str(self.root))[0])

    def test_actual_runtime_imports_and_import_aliases_remain_banned(self):
        for imported in ('alias "os/exec"', '. "syscall"', '_ "plugin"', '`unsafe`',
                         'x "net/rpc"', 'legacy "github.com/ProtonMail/go-\\x6dime"'):
            with self.subTest(imported=imported):
                self.source(f'import (gm "{TARGET}"; {imported})\nfunc f() {{ gm.Read(nil) }}')
                self.assertEqual(self.atom()['status'], 'FAIL')
                self.assertFalse(harness.check_runtime_dependency(str(self.root))[0])
        self.source(f'import (gm "{TARGET}"; "C")\nfunc f() {{ gm.Read(nil) }}')
        self.assertFalse(harness.check_runtime_dependency(str(self.root))[0])

    def test_go_mod_requires_are_parsed_not_searched_as_prose(self):
        mod = self.root / 'go.mod'
        mod.write_text(f'module sample\n// github.com/ProtonMail/go-mime\nrequire (\n\t{TARGET}\tv0.18.0 // indirect\n)\n')
        self.assertTrue(harness.check_go_mod_target(str(self.root))[0])
        mod.write_text(mod.read_text() + 'require github.com/ProtonMail/go-mime v0.0.1\n')
        self.assertFalse(harness.check_go_mod_target(str(self.root))[0])
        rules = CONFIG['policy']['deceptive_fallback_rules']
        self.assertTrue(residue.scan_residue_rules(self.root, rules))
        mod.write_text(f'module sample\nrequire {TARGET} v0.18.1\n')
        self.assertFalse(harness.check_go_mod_target(str(self.root))[0])

    def integrity(self, before, after):
        path = self.root / 'mime/a_test.go'
        path.write_text(before)
        baseline = evidence.build_source_test_baseline(self.root)
        path.write_text(after)
        return evidence._test_integrity(self.root, baseline, CONFIG)

    def test_assertion_import_alias_dot_import_and_function_alias(self):
        before = ('package mime\nimport "github.com/stretchr/testify/assert"\n'
                  'func TestA(t *testing.T) { assert.Exactly(t, want, got) }')
        for after in (
            before.replace('"github.com/stretchr/testify/assert"', 'check "github.com/stretchr/testify/assert"').replace('assert.', 'check.'),
            before.replace('"github.com/stretchr/testify/assert"', '. "github.com/stretchr/testify/assert"').replace('assert.', ''),
            before.replace('assert.Exactly(t, want, got)', '(assert.Exactly)(t, want, got)'),
            before.replace('assert.Exactly(t, want, got)', 'equal := assert.Exactly; equal(t, want, got)'),
        ):
            with self.subTest(after=after):
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_guarded_or_dead_assertions_cannot_be_compensated_by_new_tests(self):
        before = ('package mime\nimport "github.com/stretchr/testify/assert"\n'
                  'func TestA(t *testing.T) { assert.Exactly(t, want, got) }')
        for replacement in (
            'return; assert.Exactly(t, want, got)',
            'if len(os.Args) > 0 { return }; assert.Exactly(t, want, got)',
            'if false { assert.Exactly(t, want, got) }',
            'for false { assert.Exactly(t, want, got) }',
        ):
            with self.subTest(replacement=replacement):
                after = before.replace('assert.Exactly(t, want, got)', replacement)
                after += '\nfunc TestPadding(t *testing.T) { assert.Exactly(t, want, got) }'
                integrity, skips = self.integrity(before, after)
                self.assertEqual(integrity['status'], 'FAIL')
                self.assertEqual(skips['status'], 'PASS')

    def test_return_inside_checking_helper_is_rejected(self):
        before = ('package mime\nfunc compare(t *testing.T) { if got != want { t.Errorf("wrong") } }\n'
                  'func TestA(t *testing.T) { compare(t) }')
        after = before.replace('if got != want', 'if len(os.Args) > 0 { return }; if got != want')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_helper_rename_preserves_calls_but_cannot_substitute_weaker_helper(self):
        before = ('package mime\nfunc compare(t *testing.T) { if got != want { t.Errorf("wrong") } }\n'
                  'func TestA(t *testing.T) { compare(t) }')
        renamed = before.replace('compare', 'compareValues')
        self.assertEqual(self.integrity(before, renamed)[0]['status'], 'PASS')
        guarded = renamed.replace('if got != want', 'if len(os.Args) > 0 { return }; if got != want')
        self.assertEqual(self.integrity(before, guarded)[0]['status'], 'FAIL')
        replaced = renamed.replace('compareValues(t) }', 'compareWeakly(t) }')
        replaced += '\nfunc compareWeakly(t *testing.T) { assert.NotNil(t, got) }'
        self.assertEqual(self.integrity(before, replaced)[0]['status'], 'FAIL')

    def test_reordered_checks_keep_a_distinct_match_for_each_baseline_check(self):
        before = ('package mime\nfunc TestA(t *testing.T) { '
                  'if checkExtra { assert.Exactly(t, want, got) }; assert.Exactly(t, want, got) }')
        after = ('package mime\nfunc TestA(t *testing.T) { '
                 'assert.Exactly(t, want, got); if checkExtra { assert.Exactly(t, want, got) } }')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_equivalent_success_and_failure_return_branches_are_allowed(self):
        before = 'package mime\nfunc TestA(t *testing.T) { if got != want { t.Errorf("wrong") } }'
        after = 'package mime\nfunc TestA(t *testing.T) { if got == want { return }; t.Errorf("wrong") }'
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        before = ('package mime\nfunc TestA(t *testing.T) { '
                  'if err != nil { t.Fatal(err) }; if got != want { t.Error("wrong") } }')
        self.assertEqual(self.integrity(before, before.replace('t.Fatal(err)', 't.Fatal(err); return'))[0]['status'], 'PASS')

    def test_strength_weakening_and_tautology_stay_rejected(self):
        before = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, want, got) }'
        for after in (before.replace('Exactly(t, want, got)', 'NotNil(t, got)'),
                      before.replace('want, got', 'got, got')):
            self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_local_renames_and_unrelated_declarations_preserve_checking_paths(self):
        before = ('package mime\nfunc TestA(t *testing.T) { result := readResult(); '
                  'if result.Body != want { t.Error("body") } }')
        renamed = before.replace('result', 'received')
        for after in (renamed, renamed.replace('received :=',
                      'diagnostic := 1; if diagnostic == 1 { logValue(diagnostic) }; received :='),
                      renamed.replace('received :=', 'var received =')):
            with self.subTest(after=after):
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_parameter_loop_variable_and_helper_renames_preserve_paths(self):
        before = ('package mime\nfunc compare(expected []int, actual []int, t *testing.T) { '
                  'for i, status := range actual { if status != expected[i] { t.Error("status") } } }\n'
                  'func TestA(t *testing.T) { compare(wanted, observed, t) }')
        after = before.replace('expected', 'want').replace('actual', 'got').replace('status', 'value')
        after = after.replace(' i,', ' index,').replace('[i]', '[index]').replace('compare', 'checkStatuses')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_shadowed_bindings_are_resolved_without_renaming_fields_or_globals(self):
        before = ('package mime\nfunc TestA(t *testing.T) { result := readResult(); '
                  '{ result := readOther(); if result.Body != want { t.Error("inner") } }; '
                  'if result.Body != want { t.Error("outer") } }')
        renamed = before.replace('result := readOther(); if result.Body',
                                 'nested := readOther(); if nested.Body')
        self.assertEqual(self.integrity(before, renamed)[0]['status'], 'PASS')
        for after in (renamed.replace('.Body', '.OtherBody'), renamed.replace('!= want', '!= other')):
            with self.subTest(after=after):
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_typed_local_and_control_header_binding_renames(self):
        for before, after in (
            ('var result = readResult(); if result.Body != want { t.Error("body") }',
             'var received = readResult(); if received.Body != want { t.Error("body") }'),
            ('if result := readResult(); result.Body != want { t.Error("body") }',
             'if received := readResult(); received.Body != want { t.Error("body") }'),
        ):
            with self.subTest(before=before):
                prefix = 'package mime\nfunc TestA(t *testing.T) { '
                self.assertEqual(self.integrity(prefix + before + ' }', prefix + after + ' }')[0]['status'], 'PASS')

    def test_switch_and_loop_returns_cannot_bypass_checks(self):
        before = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, want, got) }'
        for guard in ('switch { case len(os.Args) > 0: return }; ',
                      'switch len(os.Args) { case 0: default: return }; ',
                      'select { default: return }; ',
                      'for len(os.Args) > 0 { return }; ',
                      'for { return }; ',
                      'for len(os.Args) > 0 { for { return } }; '):
            with self.subTest(guard=guard):
                after = before.replace('assert.Exactly', guard + 'assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_loop_break_and_continue_keep_their_correct_exit_paths(self):
        before = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, want, got) }'
        for harmless in ('for { break }; ', 'for false { return }; ',
                         'for condition { break }; ', 'for i := 0; i < size; i++ { continue }; '):
            with self.subTest(harmless=harmless):
                after = before.replace('assert.Exactly', harmless + 'assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        before = ('package mime\nfunc TestA(t *testing.T) { '
                  'for _, value := range values { assert.Exactly(t, want, value) } }')
        after = before.replace('assert.Exactly', 'continue; assert.Exactly')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_equivalent_switch_checks_breaks_and_fallthrough_are_allowed(self):
        before = 'package mime\nfunc TestA(t *testing.T) { if got != want { t.Error("wrong") } }'
        for check in ('switch { case got != want: t.Error("wrong") }',
                      'switch got { case want: default: t.Error("wrong") }'):
            after = 'package mime\nfunc TestA(t *testing.T) { ' + check + ' }'
            self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        before = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, want, got) }'
        for harmless in ('switch { default: break }; ',
                         'select { default: break }; ',
                         'switch value { case 1: break; case 2: break; default: break }; '):
            after = before.replace('assert.Exactly', harmless + 'assert.Exactly')
            self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        after = ('package mime\nfunc TestA(t *testing.T) { switch { case condition: fallthrough; '
                 'default: assert.Exactly(t, want, got) } }')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_callback_assertions_cannot_be_replaced_by_constant_or_self_comparisons(self):
        before = ('package mime\nimport "github.com/stretchr/testify/assert"\n'
                  'func (r *Callbacks) OnBody(body string, mimetype string) { '
                  'assert.Exactly(r.Testing, readFile("body"), body) }\n'
                  'func TestA(t *testing.T) { decrypt(&Callbacks{Testing: t}) }')
        for operands in ('1, 1', '(1), ((1))', '1, 0x1', 'body, body', 'result.Body, result.Body',
                         '[]byte{1, 2}, []byte{1, 2}', '"a", `a`', '1 + 1, 2', '-1, -0x1',
                         '(1 << 2) + 1, 5', '"a" + "b", "ab"'):
            with self.subTest(operands=operands):
                after = before.replace('readFile("body"), body', operands)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_tautology_calls_resolve_assertion_aliases_and_formatted_arguments(self):
        before = ('package mime\nimport "github.com/stretchr/testify/assert"\n'
                  'func TestA(t *testing.T) { assert.Exactly(t, want, got) }')
        for assertion in ('(assert.Exactly)(t, 1, 1)',
                          'check := assert.Exactly; check(t, 1, 1)',
                          'assert.Exactlyf(t, 1, 1, "value %d", got)'):
            after = before.replace('assert.Exactly(t, want, got)', assertion)
            self.assertGreater(evidence._assertion_signature(after)['tautology_count'], 0)
            self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')
        for imported, call in (('check "github.com/stretchr/testify/assert"', 'check.Exactly'),
                               ('. "github.com/stretchr/testify/assert"', 'Exactly')):
            after = ('package mime\nimport ' + imported + '\nfunc TestA(t *testing.T) { '
                     + call + '(t, 1, 1) }')
            self.assertGreater(evidence._assertion_signature(after)['tautology_count'], 0)

    def test_repeated_calls_and_distinct_numeric_types_are_not_tautologies(self):
        for operands in ('nextValue(), nextValue()', 'want, got', '1, 1.0'):
            with self.subTest(operands=operands):
                text = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, ' + operands + ') }'
                self.assertEqual(evidence._assertion_signature(text)['tautology_count'], 0)

    def test_constant_tautologies_follow_go_precedence_and_integer_division(self):
        for operands in ('1 << 2 + 1, 5', '1 | 2 * 3, 7', '-5 / 2, -2', '-5 % 2, -1',
                         '1 / -2, 0', '7 &^ 3, 4', '1 ^ 3, 2', 'true && false, false'):
            with self.subTest(operands=operands):
                text = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, ' + operands + ') }'
                self.assertEqual(evidence._assertion_signature(text)['tautology_count'], 1)
        for constant in ('1 / 0', '1 << 100000'):
            text = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, ' + constant + ', 1) }'
            self.assertEqual(evidence._assertion_signature(text)['tautology_count'], 0)

    def test_equivalent_initializers_grouped_declarations_and_predeclarations(self):
        prefix = 'package mime\nfunc TestA(t *testing.T) { '
        before = prefix + 'result, err := readResult(); if err != nil { t.Fatal(err) }; if result.Body != want { t.Error("body") } }'
        for declaration in ('result, err := (readResult())',
                            'result, err := ((readResult()))',
                            'var err error; result, err := readResult()',
                            'var (result, err = readResult())',
                            'var result *Result; var err error; result, err = readResult()'):
            with self.subTest(declaration=declaration):
                after = before.replace('result, err := readResult()', declaration)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        after = before.replace('result, err := readResult()', 'var (result, err = readOther())')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_assignment_versions_preserve_aliases_but_detect_changed_values(self):
        before = ('package mime\nfunc TestA(t *testing.T) { expected := []int{0}; '
                  'assert.Exactly(t, expected, observed) }')
        for declaration in ('var (expected = []int{0})',
                            'var expected []int; expected = []int{0}',
                            'initial := []int{0}; expected := initial'):
            with self.subTest(declaration=declaration):
                after = before.replace('expected := []int{0}', declaration)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        for declaration in ('expected := []int{1}', 'expected := observed',
                            'expected := []int{0}; expected = observed'):
            with self.subTest(declaration=declaration):
                after = before.replace('expected := []int{0}', declaration)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_testing_parameter_aliases_are_bound_by_type_and_scope(self):
        before = ('package mime\nimport "testing"\nfunc compare(t *testing.T, got int) { '
                  'if got != want { t.Error("wrong") } }\n'
                  'func TestA(t *testing.T) { compare(t, observed) }')
        for after in (re.sub(r'\bt\b', 'context', before),
                      before.replace('import "testing"', 'import check "testing"').replace('*testing.T', '*check.T'),
                      before.replace('import "testing"', 'import . "testing"').replace('*testing.T', '*T')):
            with self.subTest(after=after):
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
                self.assertEqual(evidence._assertion_signature(before)['assertion_count'],
                                 evidence._assertion_signature(after)['assertion_count'])
        shadow = ('package mime\nfunc TestA(t *testing.T) { '
                  '{ t := newRecorder(); t.Error("log entry") } }')
        self.assertEqual(evidence._assertion_signature(shadow)['assertion_count'], 0)
        weakened = before.replace('if got != want', 't := newRecorder(); if got != want')
        self.assertEqual(self.integrity(before, weakened)[0]['status'], 'FAIL')

    def test_renamed_testing_parameter_cannot_hide_new_skip(self):
        before = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, want, got) }'
        after = before.replace('t *testing.T', 'context *testing.T').replace('(t,', '(context,')
        after = after.replace('assert.Exactly', 'context.Skip("bypass"); assert.Exactly')
        self.assertEqual(self.integrity(before, after)[1]['status'], 'FAIL')

    def test_labeled_returns_cannot_bypass_checks(self):
        before = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, want, got) }'
        for guard in ('bypass: for len(os.Args) > 0 { if false { break bypass }; return }; ',
                      'bypass: switch { default: if false { break bypass }; return }; ',
                      'if false { goto bypass }; bypass: return; '):
            with self.subTest(guard=guard):
                after = before.replace('assert.Exactly', guard + 'assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_labeled_break_and_continue_resolve_the_target_control(self):
        before = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, want, got) }'
        for guard in ('outer: for { break outer }; ',
                      'outer: for { for { break outer } }; ',
                      'outer: for condition { for { continue outer } }; ',
                      'outer: switch { default: for { break outer } }; '):
            with self.subTest(guard=guard):
                self.assertEqual(self.integrity(before, before.replace('assert.Exactly', guard + 'assert.Exactly'))[0]['status'], 'PASS')
        loop = ('package mime\nfunc TestA(t *testing.T) { outer: for condition { '
                'if false { continue outer }; assert.Exactly(t, want, got) } }')
        after = loop.replace('if false { continue outer }', 'if len(os.Args) > 0 { continue outer }')
        self.assertEqual(self.integrity(loop, after)[0]['status'], 'FAIL')

    def test_callback_checks_cannot_be_padded_after_weakening_or_deletion(self):
        before = ('package mime\ntype Callbacks struct { Testing *testing.T }\n'
                  'func (r *Callbacks) OnBody(body string, mimeType string) { '
                  'assert.Exactly(r.Testing, readFile("body"), body) }\n'
                  'func TestA(t *testing.T) { callbacks := Callbacks{Testing: t}; decrypt(&callbacks) }')
        for replacement in ('assert.NotEmpty(r.Testing, body)', '',
                            'assert.Exactly(r.Testing, readFile("different"), body)',
                            'assert.Exactly(r.Testing, readFile("body"), mimeType)'):
            with self.subTest(replacement=replacement):
                after = before.replace('assert.Exactly(r.Testing, readFile("body"), body)', replacement)
                after += '\nfunc TestPadding(t *testing.T) { assert.Exactly(t, want, got); assert.Exactly(t, other, value) }'
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')
        # A dormant copy must not substitute for the callback passed to the API.
        after = before.replace('assert.Exactly(r.Testing, readFile("body"), body)', '')
        after += '\nfunc (r *Unused) OnBody(body string, mimeType string) { assert.Exactly(r.Testing, readFile("body"), body) }'
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')
        relocated = before.replace('OnBody(body string, mimeType string)', 'OnAttachment(body string, data []byte)')
        self.assertEqual(self.integrity(before, relocated)[0]['status'], 'FAIL')
        renamed = before.replace('Callbacks', 'NewCallbacks').replace('callbacks', 'handler')
        self.assertEqual(self.integrity(before, renamed)[0]['status'], 'PASS')
        for declaration in ('var callbacks Callbacks; callbacks.Testing = t',
                            'original := Callbacks{Testing: t}; callbacks := original'):
            after = before.replace('callbacks := Callbacks{Testing: t}', declaration)
            self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_testing_failure_in_callback_survives_receiver_and_field_rename(self):
        before = ('package mime\ntype Callbacks struct { Testing *testing.T }\n'
                  'func (r *Callbacks) OnError(err error) { r.Testing.Fatal("error", err) }\n'
                  'func TestA(t *testing.T) { decrypt(&Callbacks{Testing: t}) }')
        after = before.replace('r *Callbacks', 'receiver *Callbacks').replace('r.Testing', 'receiver.Context').replace('Testing', 'Context')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        after = before.replace('r.Testing.Fatal("error", err)', '')
        after += '\nfunc TestPadding(t *testing.T) { t.Fatal("unrelated") }'
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_helper_arguments_keep_expected_and_observed_values_distinct(self):
        before = ('package mime\nfunc compare(expected []int, actual []int, t *testing.T) { '
                  'if len(expected) != len(actual) { t.Error("length") } }\n'
                  'func TestA(t *testing.T) { result := runScenario(t); expected := []int{0}; '
                  'compare(expected, result.onVerified, t) }')
        for replacement in ('expected := result.onVerified',
                            'expected := []int{0}; expected = result.onVerified',
                            'expected := []int{1}'):
            with self.subTest(replacement=replacement):
                after = before.replace('expected := []int{0}', replacement)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')
        for replacement in ('var (expected = []int{0})',
                            'var expected []int; expected = []int{0}',
                            'initial := []int{0}; expected := initial'):
            with self.subTest(replacement=replacement):
                after = before.replace('expected := []int{0}', replacement)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        after = before.replace('compare(expected, result.onVerified, t)', 'compare(result.onVerified, result.onVerified, t)')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_assertion_values_are_protected_but_diagnostics_can_change(self):
        before = ('package mime\nfunc TestA(t *testing.T) { '
                  'assert.Exactly(t, want, got, "old diagnostic") }')
        after = before.replace('"old diagnostic"', '"new diagnostic", other')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        for after in (before.replace('want, got', 'other, got'),
                      before.replace('want, got', 'want, other')):
            self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_failure_and_skip_function_aliases_keep_testing_binding(self):
        before = ('package mime\nfunc TestA(t *testing.T) { '
                  'if got != want { t.Errorf("wrong") } }')
        after = before.replace('if got != want', 'report := t.Errorf; if got != want').replace('t.Errorf("wrong")', 'report("wrong")')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        renamed = re.sub(r'\bt\b', 'context', after)
        self.assertEqual(self.integrity(before, renamed)[0]['status'], 'PASS')
        skipped = before.replace('if got != want', 'skip := t.Skip; skip("bypass"); if got != want')
        self.assertEqual(self.integrity(before, skipped)[1]['status'], 'FAIL')
        global_failure = ('package mime\nvar context *testing.T\n'
                          'func TestA(t *testing.T) { report := context.Errorf; report("wrong") }')
        self.assertEqual(evidence._assertion_signature(global_failure)['assertion_count'], 1)
        global_skip = 'package mime\nvar t *testing.T\nfunc TestA() { t.Skip("bypass") }'
        self.assertGreater(evidence.go_assertion_counts(global_skip)[2], 0)

    def test_formatted_assertions_and_failure_diagnostics_keep_value_checks(self):
        for assertion, formatted in (('Exactly(t, want, got)', 'Exactlyf(t, want, got, "value %v", got)'),
                                     ('NoError(t, err)', 'NoErrorf(t, err, "error %v", err)'),
                                     ('Fail(t, "old")', 'Fail(t, "new")'),
                                     ('FailNow(t, "old")', 'FailNowf(t, "new %v", other)')):
            with self.subTest(assertion=assertion):
                before = 'package mime\nfunc TestA(t *testing.T) { assert.' + assertion + ' }'
                after = 'package mime\nfunc TestA(t *testing.T) { assert.' + formatted + ' }'
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_helper_function_value_parentheses_preserve_provenance(self):
        before = ('package mime\nfunc TestA(t *testing.T) { '
                  'result := readResult(t, "fixture"); assert.Exactly(t, want, result.Body) }')
        for call in ('(readResult)(t, "fixture")', '((readResult))(t, "fixture")',
                     '(readResult)((t), ("fixture"))'):
            with self.subTest(call=call):
                self.assertEqual(self.integrity(before, before.replace('readResult(t, "fixture")', call))[0]['status'], 'PASS')
        self.assertEqual(self.integrity(before, before.replace('readResult(t,', '(readWrong)(t,'))[0]['status'], 'FAIL')
        before = 'package mime\nfunc TestA(t *testing.T) { expected := (1 + 2) * 3; assert.Exactly(t, expected, observed) }'
        self.assertEqual(self.integrity(before, before.replace('(1 + 2) * 3', '1 + 2 * 3'))[0]['status'], 'FAIL')

    def test_exactly_swaps_preserve_values_and_failure_recipient(self):
        before = ('package mime\nfunc compare(t *testing.T, body string) { '
                  'assert.Exactly(t, readFile("body"), body) }\n'
                  'func TestA(t *testing.T) { compare(t, observed) }')
        swapped = before.replace('readFile("body"), body', 'body, readFile("body")')
        self.assertEqual(self.integrity(before, swapped)[0]['status'], 'PASS')
        self.assertEqual(self.integrity(before, swapped.replace('readFile("body")', 'readFile("other")'))[0]['status'], 'FAIL')
        for kind in ('Greater', 'Contains', 'EqualValues'):
            ordered = before.replace('Exactly', kind)
            self.assertEqual(self.integrity(ordered, swapped.replace('Exactly', kind))[0]['status'], 'FAIL')

    def test_exactly_does_not_ignore_argument_evaluation_order(self):
        for expected, actual in (('nextValue()', 'otherValue()'), ('mutate(&body)', 'body')):
            before = ('package mime\nfunc compare(t *testing.T, body string) { assert.Exactly(t, '
                      + expected + ', ' + actual + ') }\nfunc TestA(t *testing.T) { compare(t, observed) }')
            after = before.replace(expected + ', ' + actual, actual + ', ' + expected)
            self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_outer_writes_flow_through_blocks_branches_and_loop_exits(self):
        before = ('package mime\nfunc TestA(t *testing.T) { expected := []int{0}; '
                  'assert.Exactly(t, expected, observed) }')
        for write in ('{ expected = observed }',
                      '{ { expected = observed } }',
                      'if true { expected = observed }',
                      'if condition { expected = observed }',
                      'for condition { expected = observed; break }',
                      'for { expected = observed; break }',
                      'outer: for { for { expected = observed; break outer } }',
                      'switch { default: expected = observed }',
                      'switch condition { case true: expected = observed; default: }',
                      'select { default: expected = observed }'):
            with self.subTest(write=write):
                after = before.replace('assert.Exactly', write + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_control_flow_joins_allow_equal_values_shadowing_and_dead_writes(self):
        before = ('package mime\nfunc TestA(t *testing.T) { expected := []int{0}; '
                  'assert.Exactly(t, expected, observed) }')
        for write in ('{ expected = []int{0} }',
                      'if condition { expected = []int{0} }',
                      'if condition { expected = []int{0} } else { expected = []int{0} }',
                      'if false { expected = observed }',
                      'for false { expected = observed }',
                      'switch { case false: expected = observed }',
                      'switch { case true: default: expected = observed }',
                      '{ expected := observed; use(expected) }'):
            with self.subTest(write=write):
                after = before.replace('assert.Exactly', write + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        for initialization in (
                'var expected []int; if condition { expected = []int{0} } else { expected = []int{0} }',
                'var expected []int; switch condition { case true: expected = []int{0}; default: expected = []int{0} }',
                'var expected []int; for { expected = []int{0}; break }'):
            with self.subTest(initialization=initialization):
                after = before.replace('expected := []int{0}', initialization)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_parallel_assignments_read_old_values_and_aliases_keep_their_value(self):
        before = ('package mime\nfunc TestA(t *testing.T) { '
                  'first := []int{0}; second := []int{1}; use(second); '
                  'assert.Exactly(t, first, observed) }')
        after = before.replace('assert.Exactly', '{ first, second = second, first }; use(second); assert.Exactly')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')
        after = before.replace('assert.Exactly', '{ first, second = second, first; first, second = second, first }; use(second); assert.Exactly')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_live_branches_keep_values_after_early_return(self):
        before = ('package mime\nfunc TestA(t *testing.T) { expected := []int{0}; '
                  'if condition { return }; assert.Exactly(t, expected, observed) }')
        after = ('package mime\nfunc TestA(t *testing.T) { var expected []int; '
                 'if condition { return } else { expected = []int{0} }; assert.Exactly(t, expected, observed) }')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_already_failed_branches_do_not_pollute_live_values(self):
        for declaration, context in (
            ('', 't'),
            ('context := t;', 'context'),
            ('callbacks := Callbacks{Testing: t}; context := callbacks.Testing;', 'context'),
        ):
            with self.subTest(context=context, declaration=declaration):
                before = ('package mime\ntype Callbacks struct { Testing *testing.T }\n'
                          'func TestA(t *testing.T) { ' + declaration + ' expected := []int{0}; '
                          'if condition { ' + context + '.Error("failure") }; '
                          'assert.Exactly(t, expected, observed) }')
                after = before.replace('if condition {', 'if condition { expected = observed;')
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
                aliased = after.replace('if condition {', 'report := ' + context + '.Error; again := report; if condition {')
                aliased = aliased.replace(context + '.Error("failure")', 'again("failure")')
                self.assertEqual(self.integrity(before, aliased)[0]['status'], 'PASS')

    def callback_source(self):
        return ('package mime\ntype Callbacks struct { Testing *testing.T }\n'
                'func (r *Callbacks) OnBody(body string, mimeType string) { '
                'assert.Exactly(r.Testing, readFile("body"), body) }\n'
                'func TestA(t *testing.T) { decrypt(message, &Callbacks{Testing: t}) }')

    def test_callback_dispatch_requires_the_actual_api_consumer(self):
        before = self.callback_source()
        for call, extra in (
            ('ignore(message, &Callbacks{Testing: t})', '\nfunc ignore(values ...interface{}) {}'),
            ('ignore(message, &Callbacks{Testing: t})', '\nfunc ignore(message string, callbacks *Callbacks) {}'),
            ('otherDecrypt(message, &Callbacks{Testing: t})', ''),
            ('print(&Callbacks{Testing: t})', ''),
            ('assert.NotNil(t, &Callbacks{Testing: t})', ''),
        ):
            with self.subTest(call=call, extra=extra):
                after = before.replace('decrypt(message, &Callbacks{Testing: t})', call) + extra
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_api_aliases_and_transparent_callback_forwarders_are_allowed(self):
        before = self.callback_source()
        alias = before.replace('decrypt(message,', 'invoke := (decrypt); invoke(message,')
        self.assertEqual(self.integrity(before, alias)[0]['status'], 'PASS')
        for wrapper in (
            '\nfunc forward(message string, callbacks *Callbacks) { decrypt(message, callbacks) }',
            '\nfunc forward(message string, callbacks *Callbacks) { next(message, callbacks) }\n'
            'func next(message string, callbacks *Callbacks) { decrypt(message, callbacks) }',
        ):
            after = before.replace('decrypt(message, &Callbacks', 'forward(message, &Callbacks') + wrapper
            self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        for wrapper in (
            '\nfunc forward(message string, callbacks *Callbacks) { if condition { decrypt(message, callbacks) } }',
            '\nfunc forward(message string, callbacks *Callbacks) { decrypt("fixed", callbacks) }',
            '\nfunc forward(message string, callbacks *Callbacks) { if false { decrypt(message, callbacks) } }',
            '\nfunc forward(message string, callbacks *Callbacks) { forward(message, callbacks) }',
        ):
            with self.subTest(wrapper=wrapper):
                after = before.replace('decrypt(message, &Callbacks', 'forward(message, &Callbacks') + wrapper
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_callback_forwarders_keep_their_independent_checks(self):
        direct = self.callback_source()
        wrapped = direct.replace('decrypt(message, &Callbacks', 'forward(t, message, &Callbacks')
        wrapped += ('\nfunc forward(t *testing.T, message string, callbacks *Callbacks) { '
                    'assert.Exactly(t, want, got); decrypt(message, callbacks) }')
        self.assertEqual(self.integrity(direct, wrapped)[0]['status'], 'PASS')
        renamed = wrapped.replace('forward', 'forwardRenamed')
        self.assertEqual(self.integrity(wrapped, renamed)[0]['status'], 'PASS')
        for changed in (wrapped.replace('assert.Exactly(t, want, got); ', ''),
                        wrapped.replace('assert.Exactly(t, want, got)', 'assert.Exactly(t, got, got)'),
                        wrapped.replace('decrypt(message, callbacks)', 'ignore(message, callbacks)') +
                        '\nfunc ignore(message string, callbacks *Callbacks) {}'):
            with self.subTest(changed=changed):
                self.assertEqual(self.integrity(wrapped, changed)[0]['status'], 'FAIL')

    def test_assertions_keep_their_real_failure_recipient(self):
        before = self.callback_source()
        dummy = '\ntype discard struct{}\nfunc (sink discard) Errorf(format string, args ...interface{}) {}'
        for arguments in (
            'discard{}, readFile("body"), body',
            'discard{}, readFile("body"), body, r.Testing',
            'nil, readFile("body"), body, r.Testing',
            'new(testing.T), readFile("body"), body, r.Testing',
        ):
            with self.subTest(arguments=arguments):
                after = before.replace('r.Testing, readFile("body"), body', arguments) + dummy
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_testing_field_aliases_and_renames_keep_the_recipient(self):
        before = self.callback_source()
        alias = before.replace('assert.Exactly(r.Testing,', 'context := r.Testing; assert.Exactly(context,')
        self.assertEqual(self.integrity(before, alias)[0]['status'], 'PASS')
        renamed = alias.replace('Testing', 'Context').replace('r *Callbacks', 'handler *Callbacks').replace('r.Context', 'handler.Context')
        self.assertEqual(self.integrity(before, renamed)[0]['status'], 'PASS')

    def test_failure_calls_keep_their_real_testing_recipient(self):
        before = ('package mime\nfunc TestA(t *testing.T) { '
                  'if got != want { t.Errorf("wrong") } }')
        for recipient in ('t', 'new(testing.T)', 'nil'):
            after = before.replace('if got != want', 'context := ' + recipient + '; if got != want')
            after = after.replace('t.Errorf("wrong")', 'context.Errorf("wrong")')
            expected = 'PASS' if recipient == 't' else 'FAIL'
            with self.subTest(recipient=recipient):
                self.assertEqual(self.integrity(before, after)[0]['status'], expected)
                alias = after.replace('if got != want', 'report := context.Errorf; again := report; if got != want')
                alias = alias.replace('context.Errorf("wrong")', 'again("wrong")')
                self.assertEqual(self.integrity(before, alias)[0]['status'], expected)

    def test_grouped_primary_calls_keep_failure_context(self):
        before = ('package mime\ntype Callbacks struct { Testing *testing.T }\n'
                  'func (r *Callbacks) OnError(err error) { r.Testing.Fatal("error", err) }\n'
                  'func TestA(t *testing.T) { decrypt(&Callbacks{Testing: t}) }')
        for callee in ('(r.Testing).Fatal', '(r).Testing.Fatal', '((r).Testing).Fatal',
                       '((r.Testing).Fatal)', '(((r.Testing).Fatal))'):
            with self.subTest(callee=callee):
                after = before.replace('r.Testing.Fatal', callee)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        after = before.replace('r.Testing.Fatal("error", err)',
                               'report := ((r.Testing).Fatal); again := report; again("error", err)')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_api_function_values_respect_scope_shadowing_and_reassignment(self):
        before = self.callback_source()
        call = 'decrypt(message, &Callbacks{Testing: t})'
        for replacement in (
            'invoke := decrypt; again := (invoke); again(message, &Callbacks{Testing: t})',
            'invoke := decrypt; decrypt := func(message string, callbacks *Callbacks) {}; '
            'decrypt(message, &Callbacks{Testing: t}); invoke(message, &Callbacks{Testing: t})',
            '{ decrypt := func(message string, callbacks *Callbacks) {}; '
            'decrypt(message, &Callbacks{Testing: t}) }; ' + call,
        ):
            with self.subTest(replacement=replacement):
                self.assertEqual(self.integrity(before, before.replace(call, replacement))[0]['status'], 'PASS')
        for replacement in (
            'decrypt := func(message string, callbacks *Callbacks) {}; ' + call,
            'invoke := decrypt; invoke = func(message string, callbacks *Callbacks) {}; '
            'invoke(message, &Callbacks{Testing: t})',
            'invoke := decrypt; if condition { invoke = func(message string, callbacks *Callbacks) {} }; '
            'invoke(message, &Callbacks{Testing: t})',
        ):
            with self.subTest(replacement=replacement):
                self.assertEqual(self.integrity(before, before.replace(call, replacement))[0]['status'], 'FAIL')

    def test_assertion_aliases_cannot_borrow_another_scope_or_future_assignment(self):
        before = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, want, got) }'
        for replacement in (
            'equal := func(t *testing.T, expected, actual int) {}; equal(t, want, got)',
            'equal := assert.Exactly; equal = func(t *testing.T, expected, actual int) {}; equal(t, want, got)',
            'equal := func(t *testing.T, expected, actual int) {}; equal(t, want, got); equal = assert.Exactly; _ = equal',
        ):
            after = before.replace('assert.Exactly(t, want, got)', replacement)
            after += '\nfunc unrelated() { equal := assert.Exactly; _ = equal }'
            with self.subTest(replacement=replacement):
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_named_helper_aliases_keep_their_exact_callee(self):
        before = ('package mime\nfunc compare(t *testing.T) { assert.Exactly(t, want, got) }\n'
                  'func weak(t *testing.T) { assert.NotNil(t, got) }\n'
                  'func TestA(t *testing.T) { compare(t) }')
        for callee in ('(compare)', '((compare))'):
            self.assertEqual(self.integrity(before, before.replace('compare(t) }', callee + '(t) }'))[0]['status'], 'PASS')
        good = before.replace('compare(t) }', 'run := compare; run(t) }')
        self.assertEqual(self.integrity(before, good)[0]['status'], 'PASS')
        self.assertEqual(self.integrity(before, good.replace('compare', 'compareRenamed'))[0]['status'], 'PASS')
        bad = good.replace('run := compare; run(t)', 'first := compare; _ = first; run := weak; run(t)')
        self.assertEqual(self.integrity(before, bad)[0]['status'], 'FAIL')

    def test_interface_forwarders_keep_concrete_callbacks_and_contexts(self):
        before = self.callback_source()
        for kind in ('MIMECallbacks', '*Callbacks'):
            wrapper = ('\ntype MIMECallbacks interface { OnBody(string, string) }\n'
                       'func forward(message string, cb ' + kind + ') { decrypt(message, cb) }')
            after = before.replace('decrypt(message, &Callbacks', 'forward(message, &Callbacks') + wrapper
            with self.subTest(kind=kind):
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
                alias = after.replace('decrypt(message, cb)', 'original := decrypt; original(message, cb)')
                self.assertEqual(self.integrity(before, alias)[0]['status'], 'PASS')
                chain = after.replace('decrypt(message, cb)', 'next(message, cb)')
                chain += '\nfunc next(message string, cb ' + kind + ') { decrypt(message, cb) }'
                self.assertEqual(self.integrity(before, chain)[0]['status'], 'PASS')
                self.assertEqual(self.integrity(before, after.replace('Testing: t', 'Testing: new(testing.T)'))[0]['status'], 'FAIL')
                self.assertEqual(self.integrity(before, after.replace('decrypt(message, cb)', 'if condition { decrypt(message, cb) }'))[0]['status'], 'FAIL')

    def test_callback_context_construction_and_field_updates_preserve_real_t(self):
        before = self.callback_source().replace('decrypt(message, &Callbacks{Testing: t})',
                                                 'callbacks := Callbacks{Testing: t}; decrypt(message, &callbacks)')
        for declaration in (
            'callbacks := Callbacks{t}',
            'context := t; callbacks := Callbacks{Testing: context}',
            'var callbacks Callbacks; callbacks.Testing = t',
            'callbacks := Callbacks{Testing: new(testing.T)}; callbacks.Testing = t',
            'callbacks := Callbacks{Testing: t}; callbacks.Testing = new(testing.T); callbacks.Testing = t',
            'original := Callbacks{Testing: t}; callbacks := original; original.Testing = new(testing.T)',
        ):
            with self.subTest(declaration=declaration):
                after = before.replace('callbacks := Callbacks{Testing: t}', declaration)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        for write in ('callbacks.Testing = new(testing.T)',
                      '{ callbacks.Testing = new(testing.T) }',
                      'if condition { callbacks.Testing = new(testing.T) }',
                      'alias := &callbacks; alias.Testing = new(testing.T)',
                      'callbacks.Testing = nil'):
            with self.subTest(write=write):
                after = before.replace('decrypt(message, &callbacks)', write + '; decrypt(message, &callbacks)')
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_constructor_field_aliases_are_real_testing_recipients(self):
        before = 'package mime\nfunc TestA(t *testing.T) { assert.Exactly(t, want, got) }'
        after = ('package mime\ntype Context struct { Testing *testing.T }\n'
                 'func TestA(t *testing.T) { context := Context{Testing: t}; '
                 'recipient := context.Testing; assert.Exactly(recipient, want, got) }')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        self.assertEqual(self.integrity(before, after.replace('Testing: t', 'Testing: new(testing.T)'))[0]['status'], 'FAIL')

    def container_source(self):
        return ('package mime\nfunc TestA(t *testing.T) { expected := []int{0}; '
                'assert.Exactly(t, expected, observed) }')

    def test_container_contents_follow_copy_index_and_storage_aliases(self):
        before = self.container_source()
        for write in ('copy(expected, observed)',
                      'count := copy(expected, observed); use(count)',
                      'var count = copy(expected, observed); use(count)',
                      'expected[0] = observed[0]',
                      'alias := expected; copy(alias, observed)',
                      'alias := expected[:]; copy(alias, observed)',
                      'alias := expected; alias[0] = observed[0]',
                      'pointer := &expected[0]; *pointer = observed[0]',
                      '{ copy(expected, observed) }',
                      'if condition { copy(expected, observed) }'):
            with self.subTest(write=write):
                self.assertEqual(self.integrity(before, before.replace('assert.Exactly', write + '; assert.Exactly'))[0]['status'], 'FAIL')

    def test_clear_and_empty_copy_preserve_or_change_actual_contents(self):
        before = self.container_source().replace('[]int{0}', '[]int{1}')
        after = before.replace('assert.Exactly', 'clear(expected); assert.Exactly')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')
        after = self.container_source().replace('assert.Exactly',
                    'copy(expected, observed); copy(expected, []int{}); assert.Exactly')
        self.assertEqual(self.integrity(self.container_source(), after)[0]['status'], 'FAIL')

    def test_equal_container_values_do_not_share_backing_storage(self):
        before = self.container_source()
        for write in ('other := []int{0}; copy(other, observed)',
                      'other := []int{0}; other[0] = observed[0]',
                      '{ other := []int{0}; copy(other, observed) }'):
            with self.subTest(write=write):
                self.assertEqual(self.integrity(before, before.replace('assert.Exactly', write + '; assert.Exactly'))[0]['status'], 'PASS')

    def test_same_value_and_restored_container_writes_remain_allowed(self):
        before = self.container_source()
        for write in ('expected[0] = 0', 'copy(expected, []int{0})', 'copy(expected, expected)',
                      'copy(expected[0:0], observed)', 'copy(expected, []int{})', 'copy(expected[:0], observed)',
                      'clear(expected)', 'expected[0] = observed[0]; copy(expected, []int{0})',
                      'copy(expected, observed); expected[0] = 0', 'copy(expected, observed); clear(expected)',
                      'expected[0] = observed[0]; expected[0] = 0',
                      'copy(expected, observed); expected = []int{0}',
                      'if false { copy(expected, observed) }',
                      'copy := func(dst []int, src []int) {}; copy(expected, observed)'):
            with self.subTest(write=write):
                self.assertEqual(self.integrity(before, before.replace('assert.Exactly', write + '; assert.Exactly'))[0]['status'], 'PASS')
        before = before.replace('assert.Exactly', 'if condition { t.Error("failed") }; assert.Exactly')
        after = before.replace('if condition {', 'if condition { copy(expected, observed);')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_container_compound_writes_and_append_overwrites_are_protected(self):
        before = self.container_source()
        for write in ('expected[0] += observed[0]', 'expected[0]++',
                      '_ = append(expected[:0], observed...)'):
            with self.subTest(write=write):
                self.assertEqual(self.integrity(before, before.replace('assert.Exactly', write + '; assert.Exactly'))[0]['status'], 'FAIL')
        after = before.replace('assert.Exactly', 'expected[0] += 0; assert.Exactly')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_mutation_summaries_follow_helpers_aliases_and_call_chains(self):
        before = self.container_source()
        for call, declarations in (
            ('overwrite(expected, observed)', 'func overwrite(dst []int, src []int) { copy(dst, src) }'),
            ('overwrite(expected, observed)', 'func overwrite(dst []int, src []int) { copy(dst, src); return }'),
            ('overwrite(expected, observed)', 'func overwrite(dst []int, src []int) { if condition { copy(dst, src); return } }'),
            ('invoke := overwrite; invoke(expected, observed)', 'func overwrite(dst []int, src []int) { copy(dst, src) }'),
            ('forward(expected, observed)', 'func forward(dst []int, src []int) { overwrite(dst, src) }\n'
             'func overwrite(dst []int, src []int) { copy(dst, src) }'),
            ('overwrite(expected, observed)', 'func overwrite(dst []int, src []int) { alias := dst[:]; alias[0] = src[0] }'),
        ):
            with self.subTest(call=call, declarations=declarations):
                after = before.replace('assert.Exactly', call + '; assert.Exactly') + '\n' + declarations
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')
        after = before.replace('assert.Exactly', 'other := []int{0}; overwrite(other, observed); assert.Exactly')
        after += '\nfunc overwrite(dst []int, src []int) { copy(dst, src) }'
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        after = before.replace('assert.Exactly', 'overwrite(expected, []int{0}); assert.Exactly')
        after += '\nfunc overwrite(dst []int, src []int) { copy(dst, src); return }'
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_callback_context_mutations_follow_helpers_and_receiver_methods(self):
        before = self.callback_source().replace('decrypt(message, &Callbacks{Testing: t})',
                                                 'callbacks := Callbacks{Testing: t}; decrypt(message, &callbacks)')
        for call, declarations in (
            ('detach(&callbacks)', 'func detach(cb *Callbacks) { cb.Testing = new(testing.T) }'),
            ('change := detach; change(&callbacks)', 'func detach(cb *Callbacks) { cb.Testing = new(testing.T) }'),
            ('detach(&callbacks)', 'func detach(cb *Callbacks) { next(cb) }\n'
             'func next(cb *Callbacks) { cb.Testing = new(testing.T) }'),
            ('callbacks.Detach()', 'func (cb *Callbacks) Detach() { cb.Testing = new(testing.T) }'),
            ('change := callbacks.Detach; change()', 'func (cb *Callbacks) Detach() { cb.Testing = new(testing.T); return }'),
        ):
            with self.subTest(call=call, declarations=declarations):
                after = before.replace('decrypt(message, &callbacks)', call + '; decrypt(message, &callbacks)')
                self.assertEqual(self.integrity(before, after + '\n' + declarations)[0]['status'], 'FAIL')
        after = before.replace('callbacks := Callbacks{Testing: t}',
                               'var callbacks Callbacks; attach(&callbacks, t)')
        after += '\nfunc attach(cb *Callbacks, t *testing.T) { cb.Testing = t }'
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        # Struct values copy their fields; changing a by-value parameter must
        # not detach the caller's callback object.
        after = before.replace('decrypt(message, &callbacks)', 'detachCopy(callbacks); decrypt(message, &callbacks)')
        after += '\nfunc detachCopy(cb Callbacks) { cb.Testing = new(testing.T) }'
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_slice_self_reads_and_views_preserve_current_contents(self):
        before = self.container_source().replace('[]int{0}', '[]int{1, 2, 3}')
        for operation in (
            'copy(expected, expected[:])', 'copy(expected[:], expected[:])',
            'copy(expected[0:], expected[:len(expected)])',
            'expected[0] = expected[0]', 'expected[1] = []int{1, 2, 3}[1]',
            'copy(expected[1:], []int{2, 3})',
            'alias := expected[1:]; copy(alias, alias[:])',
            'alias := expected[1:]; alias[0] = expected[1]',
            'copy(expected, expected[len(expected):])',
            'copy(expected[len(expected):], observed)',
            'copy(expected[2:2:3], observed)',
        ):
            with self.subTest(operation=operation):
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_empty_and_partial_copies_cannot_restore_other_elements(self):
        before = self.container_source().replace('[]int{0}', '[]int{1, 2, 3}')
        for operation in (
            'copy(expected[3:], []int{1, 2, 3})',
            'alias := expected[3:]; copy(alias, []int{1, 2, 3})',
            'copy(expected[:1], []int{1, 2, 3})',
            'copy(expected[1:], []int{2, 3})',
            'copy(expected[:], []int{1})',
            'copy(expected, expected[:])',
            'alias := expected[1:]; copy(alias, alias[:])',
        ):
            after = before.replace('assert.Exactly',
                                   'copy(expected, observed); ' + operation + '; assert.Exactly')
            with self.subTest(operation=operation):
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_resliced_sources_and_pointer_reads_keep_identity_and_capacity(self):
        before = self.container_source()
        for operation, status in (
            ('pointer := &expected[0]; *pointer = *pointer', 'PASS'),
            ('pointer := &expected[0]; alias := pointer; *alias = *pointer', 'PASS'),
            ('pointer := &expected[0]; alias := pointer; *alias = observed[0]', 'FAIL'),
            ('copy(expected, expected[:0][:1])', 'PASS'),
            ('copy(expected[:0][:1], expected)', 'PASS'),
            ('alias := expected[:0]; copy(expected, alias[:1])', 'PASS'),
            ('copy(expected, observed); copy(expected, expected[:0][:1])', 'FAIL'),
            ('copy(expected, observed); alias := expected[:0]; copy(expected, alias[:1])', 'FAIL'),
        ):
            with self.subTest(operation=operation):
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_slice_alias_offsets_and_complete_element_restoration(self):
        before = self.container_source().replace('[]int{0}', '[]int{1, 2, 3}')
        for operation, status in (
            ('alias := expected[1:]; next := alias[:1]; next[0] = observed[0]', 'FAIL'),
            ('alias := expected[1:]; alias[0] = 1', 'FAIL'),
            ('alias := expected[1:]; alias[0] = 2', 'PASS'),
            ('copy(expected, observed); copy(expected[:1], []int{1}); '
             'tail := expected[1:]; copy(tail, []int{2, 3})', 'PASS'),
            ('copy(expected, observed); expected[0] = []int{1}[0]; '
             'expected[1] = 2; expected[2] = 3', 'PASS'),
            ('copy(expected, observed); expected[0] = expected[0]', 'FAIL'),
        ):
            with self.subTest(operation=operation):
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_overlapping_copy_reads_source_before_writing_destination(self):
        before = self.container_source().replace('[]int{0}', '[]int{1, 2, 3}')
        for operation, status in (
            ('copy(expected[1:], expected[:2])', 'FAIL'),
            ('copy(expected[:2], expected[1:])', 'FAIL'),
            ('copy(expected[1:], expected[:2]); copy(expected, []int{1, 2, 3})', 'PASS'),
            ('copy(expected[1:], expected[:2]); expected[1] = 2', 'FAIL'),
            ('copy(expected[1:], expected[:2]); expected[1] = 2; expected[2] = 3', 'PASS'),
        ):
            with self.subTest(operation=operation):
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_clear_respects_slice_window_and_previous_modifications(self):
        before = self.container_source().replace('[]int{0}', '[]int{0, 0, 1}')
        for operation, status in (
            ('clear(expected[:2])', 'PASS'), ('clear(expected[2:])', 'FAIL'),
            ('clear(expected[3:])', 'PASS'),
            ('copy(expected, observed); clear(expected[:2])', 'FAIL'),
            ('copy(expected, observed); clear(expected[:2]); expected[2] = 1', 'PASS'),
        ):
            with self.subTest(operation=operation):
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_append_capacity_distinguishes_allocation_from_overwrite(self):
        before = self.container_source().replace('[]int{0}', '[]int{1, 2, 3}')
        for operation, status in (
            ('_ = append(expected[:0:0], observed...)', 'PASS'),
            ('_ = append(expected[:0], 1, 2, 3, 4)', 'PASS'),
            ('_ = append(expected[:0], 1)', 'PASS'),
            ('_ = append(expected[1:1], 2, 3)', 'PASS'),
            ('_ = append(expected[:0], 9)', 'FAIL'),
            ('_ = append(expected[:0:1], observed...)', 'FAIL'),
        ):
            with self.subTest(operation=operation):
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_partial_copy_helpers_preserve_ranges_through_aliases(self):
        before = self.container_source().replace('[]int{0}', '[]int{1, 2, 3}')
        for body, status in (
            ('copy(dst[3:], []int{1, 2, 3})', 'FAIL'),
            ('tail := dst[1:]; copy(tail, []int{2, 3})', 'FAIL'),
            ('copy(dst[:1], []int{1}); copy(dst[1:], []int{2, 3})', 'PASS'),
            ('copy(dst, []int{1, 2, 3})', 'PASS'),
        ):
            after = before.replace('assert.Exactly', 'copy(expected, observed); restore(expected); assert.Exactly')
            after += '\nfunc restore(dst []int) { ' + body + ' }'
            with self.subTest(body=body):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_defer_mutations_take_effect_after_current_function_checks(self):
        before = self.container_source()
        for operation, status in (
            ('defer copy(expected, observed)', 'PASS'),
            ('defer clear(expected)', 'PASS'),
            ('copy(expected, observed); defer copy(expected, []int{0})', 'FAIL'),
            ('copy(expected, observed); defer clear(expected)', 'FAIL'),
            ('defer use(copy(expected, observed))', 'FAIL'),
        ):
            with self.subTest(operation=operation):
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_deferred_helper_effects_run_in_reverse_registration_order(self):
        before = self.container_source()
        for body, status in (
            ('defer copy(dst, src)', 'FAIL'),
            ('defer copy(dst, src); return', 'FAIL'),
            ('defer copy(dst, []int{0}); copy(dst, src)', 'PASS'),
            ('defer copy(dst, []int{0}); defer copy(dst, src)', 'PASS'),
            ('defer copy(dst, src); defer copy(dst, []int{0})', 'FAIL'),
            ('if condition { defer copy(dst, []int{0}) }; copy(dst, src)', 'FAIL'),
            ('defer copy(dst, src); dst = []int{0}', 'FAIL'),
            ('defer copy(dst, src); src = []int{0}', 'FAIL'),
        ):
            after = before.replace('assert.Exactly', 'change(expected, observed); assert.Exactly')
            after += '\nfunc change(dst []int, src []int) { ' + body + ' }'
            with self.subTest(body=body):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_deferred_assertions_read_live_slice_contents_at_invocation(self):
        before = self.container_source()
        for replacement, status in (
            ('defer assert.Exactly(t, expected, observed)', 'PASS'),
            ('defer assert.Exactly(t, expected, observed); copy(expected, observed)', 'FAIL'),
            ('defer copy(expected, observed); defer assert.Exactly(t, expected, observed)', 'PASS'),
            ('defer assert.Exactly(t, expected, observed); defer copy(expected, observed)', 'FAIL'),
        ):
            with self.subTest(replacement=replacement):
                after = before.replace('assert.Exactly(t, expected, observed)', replacement)
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_deferred_failure_does_not_end_the_function_at_registration(self):
        before = ('package mime\nfunc TestA(t *testing.T) { '
                  't.Errorf("diagnostic"); assert.Exactly(t, want, got) }')
        after = before.replace('t.Errorf("diagnostic")', 'defer t.Errorf("diagnostic")')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')
        before = self.container_source()
        after = before.replace('assert.Exactly', 'defer t.Errorf("diagnostic"); return; assert.Exactly')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_deferred_closures_apply_captures_and_effects_at_exit(self):
        before = self.container_source()
        for body, status in (
            ('defer func() { copy(dst, src) }()', 'FAIL'),
            ('defer func() { copy(dst, []int{0}) }(); copy(dst, src)', 'PASS'),
            ('defer func() { copy(dst, src) }(); dst = []int{0}', 'PASS'),
            ('defer func(dst []int) { copy(dst, src) }(dst); dst = []int{0}', 'FAIL'),
            ('defer func() { copy(dst, src) }(); src = []int{0}', 'PASS'),
            ('{ alias := dst; defer func() { copy(alias, src) }(); alias = []int{0} }', 'PASS'),
        ):
            after = before.replace('assert.Exactly', 'change(expected, observed); assert.Exactly')
            after += '\nfunc change(dst []int, src []int) { ' + body + ' }'
            with self.subTest(body=body):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_checks_in_deferred_closures_keep_their_conditions_and_values(self):
        before = self.container_source()
        for replacement, status in (
            ('defer func() { assert.Exactly(t, expected, observed) }()', 'PASS'),
            ('defer (func() { assert.Exactly(t, expected, observed) })()', 'PASS'),
            ('defer func() { assert.Exactly(t, expected, observed) }(); copy(expected, observed)', 'FAIL'),
            ('defer func() { if condition { assert.Exactly(t, expected, observed) } }()', 'FAIL'),
            ('defer func() { return; assert.Exactly(t, expected, observed) }()', 'FAIL'),
        ):
            with self.subTest(replacement=replacement):
                after = before.replace('assert.Exactly(t, expected, observed)', replacement)
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_defer_inside_an_uncalled_closure_cannot_preserve_checks(self):
        before = self.container_source()
        for replacement in (
            'unused := func() { defer func() { assert.Exactly(t, expected, observed) }() }; _ = unused',
            'unused := func() { defer assert.Exactly(t, expected, observed) }; _ = unused',
            'defer func() { unused := func() { defer func() { assert.Exactly(t, expected, observed) }() }; _ = unused }()',
        ):
            with self.subTest(replacement=replacement):
                after = before.replace('assert.Exactly(t, expected, observed)', replacement)
                self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')
        after = before.replace('assert.Exactly(t, expected, observed)',
                               'defer func() { defer func() { assert.Exactly(t, expected, observed) }() }()')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'PASS')

    def test_array_views_keep_implicit_elements_and_require_full_restoration(self):
        before = self.container_source().replace('[]int{0}', '[3]int{1}')
        for operation, status in (
            ('copy(expected[:], expected[:])', 'PASS'),
            ('copy(expected[:], observed); expected[0] = 1', 'FAIL'),
            ('copy(expected[:], observed); copy(expected[:1], []int{1})', 'FAIL'),
            ('copy(expected[:], observed); expected[0] = 1; clear(expected[1:])', 'PASS'),
            ('expected[2] = 0', 'PASS'), ('expected[2] = 1', 'FAIL'),
        ):
            with self.subTest(operation=operation):
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_slice_fields_share_storage_across_construction_and_value_copies(self):
        before = self.container_source()
        declarations = '\ntype Box struct { Value []int }; type Envelope struct { Inner Box }\n'
        for setup, target in (
            ('box := Box{Value: expected}', 'box.Value'),
            ('box := Box{expected}', 'box.Value'),
            ('box := &Box{Value: expected}', 'box.Value'),
            ('box := Envelope{Inner: Box{Value: expected}}', 'box.Inner.Value'),
            ('box := Box{Value: expected}; other := box', 'other.Value'),
            ('box := Box{Value: expected}; other := box; box.Value = []int{0}', 'other.Value'),
            ('var box Box; box.Value = expected', 'box.Value'),
            ('box := Box{Value: []int{0}}; box.Value = expected', 'box.Value'),
        ):
            for write in ('copy(TARGET, observed)', 'alias := TARGET; copy(alias, observed)',
                          'TARGET[0] = observed[0]', '_ = append(TARGET[:0], observed...)'):
                operation = setup + '; ' + write.replace('TARGET', target)
                after = before.replace('assert.Exactly', operation + '; assert.Exactly') + declarations
                with self.subTest(operation=operation):
                    self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_slice_field_rebinding_preserves_old_aliases_and_independent_storage(self):
        before = self.container_source()
        for operation, status in (
            ('box := Box{Value: expected}; box.Value = []int{0}; copy(box.Value, observed)', 'PASS'),
            ('box := Box{Value: expected}; alias := box.Value; box.Value = []int{0}; copy(alias, observed)', 'FAIL'),
            ('box := Box{Value: expected}; box.Value = []int{0}; alias := box.Value; copy(alias, observed)', 'PASS'),
            ('box := Box{Value: []int{0}}; copy(box.Value, observed)', 'PASS'),
            ('box := Box{Value: expected}; copy(box.Value[:0], observed)', 'PASS'),
            ('box := Box{Value: expected}; box.Value = expected[:0]; copy(box.Value, observed)', 'PASS'),
            ('box := Box{Value: expected}; box.Value = expected[:0]; copy(box.Value[:1], observed)', 'FAIL'),
            ('box := Box{Value: expected}; copy(box.Value, observed); box.Value = []int{0}', 'FAIL'),
            ('box := Box{Value: expected}; copy(box.Value, observed); copy(box.Value, []int{0})', 'PASS'),
        ):
            after = before.replace('assert.Exactly', operation + '; assert.Exactly') + '\ntype Box struct { Value []int }'
            with self.subTest(operation=operation):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_deferred_field_sources_capture_descriptors_and_live_backing_contents(self):
        before = self.container_source()
        for body, status in (
            ('box := Box{Value: src}; defer copy(dst, box.Value); box.Value = []int{0}', 'FAIL'),
            ('copy(dst, src); box := Box{Value: []int{0}}; defer copy(dst, box.Value); box.Value = src', 'PASS'),
            ('box := Box{Value: src}; alias := box.Value; defer copy(dst, alias); box.Value = []int{0}', 'FAIL'),
            ('copy(dst, src); box := Box{Value: []int{0}}; defer copy(dst, box.Value); copy(box.Value, src)', 'FAIL'),
            ('box := Box{Value: src}; defer copy(dst, box.Value[:]); box.Value = []int{0}', 'FAIL'),
            ('copy(dst, src); box := Box{Value: []int{0}}; defer copy(dst, box.Value[:]); box.Value = src', 'PASS'),
            ('copy(dst, src); box := Box{Value: []int{0}}; defer func(v []int) { copy(dst, v) }(box.Value); box.Value = src', 'PASS'),
            ('copy(dst, src); box := Box{Value: []int{0}}; defer func() { copy(dst, box.Value) }(); box.Value = src', 'FAIL'),
        ):
            after = before.replace('assert.Exactly', 'change(expected, observed); assert.Exactly')
            after += '\ntype Box struct { Value []int }; func change(dst, src []int) { ' + body + ' }'
            with self.subTest(body=body):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_deferred_field_destinations_keep_captured_array_and_window(self):
        before = self.container_source()
        for body, status in (
            ('box := Box{Value: dst}; defer copy(box.Value, src); box.Value = []int{0}', 'FAIL'),
            ('box := Box{Value: []int{0}}; defer copy(box.Value, src); box.Value = dst', 'PASS'),
            ('box := Box{Value: dst}; defer copy(box.Value[:0], src); box.Value = []int{0}', 'PASS'),
            ('box := Box{Value: dst}; defer copy(box.Value, []int{0}); copy(dst, src); box.Value = src', 'PASS'),
        ):
            after = before.replace('assert.Exactly', 'change(expected, observed); assert.Exactly')
            after += '\ntype Box struct { Value []int }; func change(dst, src []int) { ' + body + ' }'
            with self.subTest(body=body):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_deferred_checks_capture_field_values_but_observe_later_array_writes(self):
        before = self.container_source()
        for operation, status in (
            ('box := Box{Value: expected}; defer assert.Exactly(t, box.Value, observed); box.Value = observed', 'PASS'),
            ('box := Box{Value: observed}; defer assert.Exactly(t, box.Value, observed); box.Value = expected', 'FAIL'),
            ('box := Box{Value: expected}; defer assert.Exactly(t, box.Value, observed); copy(box.Value, observed)', 'FAIL'),
            ('box := Box{Value: expected}; defer assert.Exactly(t, box.Value, observed); box.Value = []int{0}; copy(box.Value, observed)', 'PASS'),
        ):
            after = before.replace('assert.Exactly(t, expected, observed)', operation) + '\ntype Box struct { Value []int }'
            with self.subTest(operation=operation):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_complete_slice_arguments_normalize_without_loosening_bounds(self):
        before = self.container_source().replace('[]int{0}', '[]int{1, 2, 3}')
        for argument, status in (
            ('expected[:]', 'PASS'), ('expected[0:]', 'PASS'), ('expected[:3]', 'PASS'),
            ('expected[:len(expected)]', 'PASS'), ('expected[0:3:3]', 'PASS'),
            ('expected[:0][:3]', 'PASS'), ('expected[:2]', 'FAIL'), ('expected[1:]', 'FAIL'),
            ('expected[0:2:2]', 'FAIL'), ('[]int{1, 2}', 'FAIL'),
        ):
            for alias in (False, True):
                after = before.replace('assert.Exactly(t, expected, observed)',
                    ('alias := ' + argument + '; assert.Exactly(t, alias, observed)' if alias else
                     'assert.Exactly(t, ' + argument + ', observed)'))
                with self.subTest(argument=argument, alias=alias):
                    self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_complete_slice_arguments_keep_helper_dispatch_and_mutation_checks(self):
        before = self.container_source().replace('assert.Exactly(t, expected, observed)', 'check(expected, observed, t)')
        before += '\nfunc check(expected, observed []int, t *testing.T) { assert.Exactly(t, expected, observed) }'
        for operation, argument, status in (
            ('', 'expected[:]', 'PASS'), ('', 'expected[:len(expected)]', 'PASS'),
            ('copy(expected, observed); ', 'expected[:]', 'FAIL'), ('', 'expected[:0]', 'FAIL'),
        ):
            after = before.replace('check(expected, observed, t)', operation + 'check(' + argument + ', observed, t)')
            with self.subTest(operation=operation, argument=argument):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_slice_fields_propagate_mutations_through_value_and_pointer_helpers(self):
        before = self.container_source()
        for kind, argument in (('Box', 'box'), ('*Box', '&box')):
            for body, status in (
                ('copy(box.Value, src)', 'FAIL'),
                ('alias := box.Value; copy(alias, src)', 'FAIL'),
                ('box.Value = []int{0}; copy(box.Value, src)', 'PASS'),
            ):
                after = before.replace('assert.Exactly', 'box := Box{Value: expected}; change(' + argument + ', observed); assert.Exactly')
                after += '\ntype Box struct { Value []int }; func change(box ' + kind + ', src []int) { ' + body + ' }'
                with self.subTest(kind=kind, body=body):
                    self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_conditional_field_aliases_cannot_hide_possible_mutations(self):
        before = self.container_source()
        for operation, status in (
            ('box := Box{Value: []int{0}}; if condition { box.Value = expected }; copy(box.Value, observed)', 'FAIL'),
            ('box := Box{Value: expected}; if condition { box.Value = []int{0} }; copy(box.Value, observed)', 'FAIL'),
            ('box := Box{Value: []int{0}}; if condition { box.Value = []int{0} }; copy(box.Value, observed)', 'PASS'),
            ('box := Box{Value: expected}; if condition { box.Value = []int{0} } else { box.Value = []int{0} }; copy(box.Value, observed)', 'PASS'),
        ):
            after = before.replace('assert.Exactly', operation + '; assert.Exactly') + '\ntype Box struct { Value []int }'
            with self.subTest(operation=operation):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_helper_field_assignments_export_references_and_keep_old_backings(self):
        before = self.container_source()
        for setup, body, following, status in (
            ('var box Box', 'box.Value = src', 'copy(box.Value, observed)', 'FAIL'),
            ('box := Box{Value: []int{0}}', 'box.Value = src', 'copy(box.Value, observed)', 'FAIL'),
            ('box := Box{Value: []int{0}}', 'if condition { box.Value = src }', 'copy(box.Value, observed)', 'FAIL'),
            ('box := Box{Value: expected}', 'if condition { box.Value = []int{0} }', 'copy(box.Value, observed)', 'FAIL'),
            ('box := Box{Value: expected}', 'box.Value = []int{0}', 'copy(box.Value, observed)', 'PASS'),
            ('box := Box{Value: expected}', 'alias := box.Value; box.Value = []int{0}; copy(alias, observed)', '', 'FAIL'),
            ('box := Box{Value: expected}', 'defer copy(box.Value, observed); box.Value = []int{0}', '', 'FAIL'),
            ('box := Box{Value: expected}', 'box.Value = []int{0}; copy(box.Value, observed)', '', 'PASS'),
        ):
            for forward in (False, True):
                call = 'forward' if forward else 'bind'
                operation = setup + '; ' + call + '(&box, expected); ' + following
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                after += '\ntype Box struct { Value []int }; func bind(box *Box, src []int) { ' + body + ' }'
                if forward:
                    after += '\nfunc forward(box *Box, src []int) { bind(box, src) }'
                with self.subTest(setup=setup, body=body, following=following, forward=forward):
                    self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_nested_array_and_slice_elements_retain_reference_field_identity(self):
        before = self.container_source()
        for setup, target in (
            ('boxes := [1]Box{{Value: expected}}', 'boxes[0].Value'),
            ('boxes := []Box{{Value: expected}}', 'boxes[0].Value'),
            ('boxes := [][]int{expected}', 'boxes[0]'),
            ('boxes := [1][]int{expected}', 'boxes[0]'),
        ):
            for initializer, status in (('expected', 'FAIL'), ('[]int{0}', 'PASS')):
                operation = setup.replace('expected', initializer) + '; copy(' + target + ', observed)'
                after = before.replace('assert.Exactly', operation + '; assert.Exactly') + '\ntype Box struct { Value []int }'
                with self.subTest(operation=operation):
                    self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_complete_views_of_partial_windows_keep_capacity_and_slice_type(self):
        before = self.container_source().replace('expected := []int{0}',
                   'backing := []int{1, 2, 3}; expected := backing[:2]')
        for argument, status in (('expected[:]', 'PASS'), ('expected[0:2]', 'PASS'),
                                 ('expected[:2:3]', 'PASS'), ('expected[:2:2]', 'PASS'),
                                 ('expected[:1]', 'FAIL'), ('expected[1:]', 'FAIL')):
            for alias in (False, True):
                after = before.replace('assert.Exactly(t, expected, observed)',
                    ('alias := ' + argument + '; assert.Exactly(t, alias, observed)' if alias else
                     'assert.Exactly(t, ' + argument + ', observed)'))
                with self.subTest(argument=argument, alias=alias):
                    self.assertEqual(self.integrity(before, after)[0]['status'], status)
        before = self.container_source().replace('[]int{0}', '[1]int{0}')
        after = before.replace('assert.Exactly(t, expected, observed)', 'assert.Exactly(t, expected[:], observed)')
        self.assertEqual(self.integrity(before, after)[0]['status'], 'FAIL')

    def test_deferred_checks_preserve_fields_of_typed_function_results(self):
        before = ('package mime\ntype Result struct { Values []int }; '
                  'func collect() *Result { return &Result{Values: observed} }; '
                  'func TestA(t *testing.T) { expected := []int{0}; result := collect(); '
                  'assert.Exactly(t, expected, result.Values) }')
        for replacement, status in (
            ('defer assert.Exactly(t, expected, result.Values)', 'PASS'),
            ('saved := result.Values; defer assert.Exactly(t, expected, saved)', 'PASS'),
            ('defer assert.Exactly(t, expected, result.Values); copy(expected, result.Values)', 'FAIL'),
        ):
            after = before.replace('assert.Exactly(t, expected, result.Values)', replacement)
            with self.subTest(replacement=replacement):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_helper_aggregate_assignments_keep_nested_reference_fields(self):
        before = self.container_source()
        for field, constructor in (('Inner', 'Box'), ('Ptr', '&Box')):
            for initializer, status in (('src', 'FAIL'), ('[]int{0}', 'PASS')):
                operation = 'var box Envelope; bind(&box, expected); copy(box.' + field + '.Value, observed)'
                after = before.replace('assert.Exactly', operation + '; assert.Exactly')
                after += ('\ntype Box struct { Value []int }; type Envelope struct { Inner Box; Ptr *Box }; '
                          'func bind(box *Envelope, src []int) { box.' + field + ' = ' + constructor +
                          '{Value: ' + initializer + '} }')
                with self.subTest(field=field, initializer=initializer):
                    self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_restoring_expected_values_does_not_change_opaque_result_provenance(self):
        before = ('package mime\ntype Result struct { Values []int }; '
                  'func collect() *Result { return &Result{Values: observed} }; '
                  'func TestA(t *testing.T) { expected := []int{0}; result := collect(); '
                  'assert.Exactly(t, expected, result.Values) }')
        for operation, status in (
            ('copy(expected, result.Values); restore(expected, []int{0})', 'PASS'),
            ('copy(expected, result.Values); restore(expected[:], []int{0})', 'PASS'),
            ('copy(expected, result.Values); restore(expected[:0], []int{0})', 'FAIL'),
        ):
            after = before.replace('assert.Exactly', operation + '; assert.Exactly')
            after += '\nfunc restore(dst, src []int) { copy(dst, src); return }'
            with self.subTest(operation=operation):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_deferred_methods_capture_receiver_values_and_read_pointer_fields_live(self):
        before = self.container_source()
        for kind, initial, following, status in (
            ('*Box', 'src', 'box.Value = original', 'PASS'),
            ('*Box', 'original', 'copy(dst, src); box.Value = src', 'FAIL'),
            ('Box', 'src', 'box.Value = original', 'FAIL'),
            ('Box', 'original', 'copy(dst, src); box.Value = src', 'PASS'),
        ):
            for bound in (False, True):
                call = 'saved := box.Apply; defer saved(dst)' if bound else 'defer box.Apply(dst)'
                body = 'box := Box{Value: ' + initial + '}; ' + call + '; ' + following
                after = before.replace('assert.Exactly', 'change(expected, observed, []int{0}); assert.Exactly')
                after += ('\ntype Box struct { Value []int }; func (box ' + kind + ') Apply(dst []int) '
                          '{ copy(dst, box.Value) }; func change(dst, src, original []int) { ' + body + ' }')
                with self.subTest(kind=kind, body=body):
                    self.assertEqual(self.integrity(before, after)[0]['status'], status)
            # A method value captures its receiver when bound, even when
            # registration is delayed until after the original is rebound.
            body = 'box := Box{Value: ' + initial + '}; saved := box.Apply; ' + following + '; defer saved(dst)'
            after = before.replace('assert.Exactly', 'change(expected, observed, []int{0}); assert.Exactly')
            after += ('\ntype Box struct { Value []int }; func (box ' + kind + ') Apply(dst []int) '
                      '{ copy(dst, box.Value) }; func change(dst, src, original []int) { ' + body + ' }')
            with self.subTest(kind=kind, delayed=body):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)
        for bind, status in (('dst', 'FAIL'), ('original', 'PASS')):
            for bound in (False, True):
                call = 'saved := holder.Ptr.Apply; defer saved(src)' if bound else 'defer holder.Ptr.Apply(src)'
                after = before.replace('assert.Exactly', 'change(expected, observed, []int{0}); assert.Exactly')
                after += ('\ntype Box struct { Value []int }; type Holder struct { Ptr *Box }; '
                          'func (box *Box) Apply(src []int) { copy(box.Value, src) }; '
                          'func change(dst, src, original []int) { holder := Holder{Ptr: &Box{Value: ' + bind +
                          '}}; ' + call + '; holder.Ptr = &Box{Value: original} }')
                with self.subTest(bind=bind, bound=bound):
                    self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_deferred_full_views_keep_unknown_parameter_lengths_and_captured_arrays(self):
        before = self.container_source()
        for body, status in (
            ('copy(dst, src); box := Box{Value: original}; defer copy(dst, box.Value[:]); box.Value = src', 'PASS'),
            ('box := Box{Value: src}; defer copy(dst, box.Value[:]); box.Value = original', 'FAIL'),
            ('copy(dst, src); box := Box{Value: original}; defer copy(dst, box.Value[:0]); box.Value = src', 'FAIL'),
            ('copy(dst, src); box := Box{Value: original}; '
             'defer copy(dst, box.Value[:len(original):cap(original)]); box.Value = src', 'PASS'),
        ):
            after = before.replace('assert.Exactly', 'change(expected, observed, []int{0}); assert.Exactly')
            after += '\ntype Box struct {Value []int}; func change(dst, src, original []int) { ' + body + ' }'
            with self.subTest(body=body):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)
        before = self.container_source().replace('[]int{0}', '[1]int{0}')
        for operation, status in (('alias := expected[:]; alias[0] = observed[0]', 'FAIL'),
                                  ('alias := expected[:]; alias[0] = 0', 'PASS')):
            after = before.replace('assert.Exactly', operation + '; assert.Exactly')
            with self.subTest(operation=operation):
                self.assertEqual(self.integrity(before, after)[0]['status'], status)

    def test_f2p3_runs_public_path_sources_and_propagates_failures(self):
        (self.root / 'go.mod').write_text(f'module sample\nrequire {TARGET} v0.18.0\n')
        states = {name: 'pass' for name in harness.F2P3_TESTS}
        states['TestProbeGoMessageUnknownEncoding'] = 'fail'
        observations = {}
        with patch.object(harness, 'run_go_probe', return_value=(states, '', 1)) as run:
            harness.collect_f2p0003(str(self.root), '', {}, str(TESTS / 'assets'), observations)
        self.assertEqual(len(observations), 24)
        for oid in harness.F2P3_TESTS['TestProbeGoMessageUnknownEncoding']:
            self.assertEqual(observations[oid]['status'], 'FAIL')
        self.assertEqual(observations['F2P-072']['status'], 'PASS')
        sources, args = run.call_args.args[2:4]
        self.assertEqual(len(sources), 2)
        self.assertIn('BATCH-F2P-0002', sources[0][0])
        selected = args[1]
        self.assertFalse(re.search(selected, 'TestProbeRawSignedPart'))
        self.assertTrue(all(re.search(selected, name) for name in states))

    def test_f2p3_no_target_is_failure_not_library_only_success(self):
        (self.root / 'go.mod').write_text('module sample\n')
        observations = {}
        with patch.object(harness, 'run_go_probe') as run:
            harness.collect_f2p0003(str(self.root), '', {}, str(TESTS / 'assets'), observations)
            run.assert_not_called()
        self.assertEqual(len(observations), 24)
        self.assertTrue(all(o['status'] == 'FAIL' for o in observations.values()))

    def test_f2p3_wrappers_bind_to_trusted_public_api_probes(self):
        wrapper = (TESTS / 'assets/target/batches/BATCH-F2P-0003/probe_test.go').read_text()
        shared = (TESTS / 'assets/target/batches/BATCH-F2P-0002/probe_target_test.go').read_text()
        self.assertTrue(wrapper.startswith('package mime_test'))
        self.assertNotIn('github.com/emersion/go-message', wrapper)
        self.assertEqual(set(re.findall(r'func (Test\w+)\(', wrapper)), set(harness.F2P3_TESTS))
        self.assertIn('mime.Decrypt(', shared)
        for name in ('TestProbeBodySelection', 'TestProbeRawSignedPart',
                     'TestProbeContentDispositionAndHeaders', 'TestProbeUnknownEncodingTolerated',
                     'TestProbeUnknownCharsetTolerated', 'TestProbeGoMessageCharsetDecoding'):
            self.assertIn(name + '(t)', wrapper)
            self.assertIn('func ' + name + '(', shared)


# FIXED is replaced with the original test's independent expected literal.
# The full-candidate replay uses these same operation descriptions.
REFERENCE_CASES = [
  [
    "map_shared",
    "FAIL",
    "box := map[string][]int{\"v\":expected}; copy(box[\"v\"],observed)",
    ""
  ],
  [
    "map_shared_alias",
    "FAIL",
    "box := map[string][]int{\"v\":expected}; alias := box[\"v\"]; copy(alias,observed)",
    ""
  ],
  [
    "map_independent",
    "PASS",
    "box := map[string][]int{\"v\":FIXED}; copy(box[\"v\"],observed)",
    ""
  ],
  [
    "map_restore",
    "PASS",
    "box := map[string][]int{\"v\":FIXED}; copy(expected,observed); copy(expected,box[\"v\"])",
    ""
  ],
  [
    "map_assigned_shared",
    "FAIL",
    "box := map[string][]int{}; box[\"v\"]=expected; copy(box[\"v\"],observed)",
    ""
  ],
  [
    "map_rebound",
    "PASS",
    "box := map[string][]int{\"v\":expected}; box[\"v\"]=FIXED; copy(box[\"v\"],observed)",
    ""
  ],
  [
    "map_old_alias",
    "FAIL",
    "box := map[string][]int{\"v\":expected}; alias := box[\"v\"]; box[\"v\"]=FIXED; copy(alias,observed)",
    ""
  ],
  [
    "return_shared",
    "FAIL",
    "alias := identity(expected); copy(alias,observed)",
    "func identity(v []int) []int {return v}"
  ],
  [
    "return_restore",
    "PASS",
    "copy(expected,observed); copy(expected,identity(FIXED))",
    "func identity(v []int) []int {return v}"
  ],
  [
    "return_independent",
    "PASS",
    "alias := identity(FIXED); copy(alias,observed)",
    "func identity(v []int) []int {return v}"
  ],
  [
    "return_constant",
    "PASS",
    "alias := identity(expected); copy(alias,observed)",
    "func identity(v []int) []int {return []int{0}}"
  ],
  [
    "return_field_shared",
    "FAIL",
    "box := Box{Value:expected}; alias := identity(box); copy(alias,observed)",
    "type Box struct {Value []int}; func identity(v Box) []int {return v.Value}"
  ],
  [
    "named_shared",
    "FAIL",
    "alias := Statuses(expected); copy(alias,observed)",
    "type Statuses []int"
  ],
  [
    "named_alias_shared",
    "FAIL",
    "alias := Statuses(expected); copy(alias,observed)",
    "type Statuses = []int"
  ],
  [
    "map_integer_key",
    "FAIL",
    "box := map[int][]int{2:expected}; copy(box[2],observed)",
    ""
  ],
  [
    "map_escaped_key",
    "FAIL",
    "box := map[string][]int{\"a/b c\":expected}; copy(box[\"a/b c\"],observed)",
    ""
  ],
  [
    "map_key_alias",
    "FAIL",
    "key := \"v\"; box := map[string][]int{key:expected}; copy(box[key],observed)",
    ""
  ],
  [
    "map_struct",
    "FAIL",
    "box := map[string]Box{\"v\":{Value:expected}}; copy(box[\"v\"].Value,observed)",
    "type Box struct{Value []int}"
  ],
  [
    "map_pointer",
    "FAIL",
    "box := map[string]*Box{\"v\":{Value:expected}}; copy(box[\"v\"].Value,observed)",
    "type Box struct{Value []int}"
  ],
  [
    "map_nested",
    "FAIL",
    "box := map[string]map[int][]int{\"v\":{2:expected}}; copy(box[\"v\"][2],observed)",
    ""
  ],
  [
    "map_helper",
    "FAIL",
    "box := map[string][]int{\"v\":expected}; change(box, observed)",
    "func change(box map[string][]int, src []int){copy(box[\"v\"],src)}"
  ],
  [
    "map_defer_dst",
    "FAIL",
    "box := map[string][]int{\"v\":expected}; change(box, observed)",
    "func change(box map[string][]int, src []int){defer copy(box[\"v\"],src);box[\"v\"]=[]int{0}}"
  ],
  [
    "map_defer_restore",
    "PASS",
    "copy(expected,observed); box := map[string][]int{\"v\":FIXED}; restore(expected,box,observed)",
    "func restore(dst []int,box map[string][]int,src []int){defer copy(dst,box[\"v\"]);box[\"v\"]=src}"
  ],
  [
    "map_empty",
    "PASS",
    "box := map[string][]int{\"v\":expected}; copy(box[\"v\"][:0],observed)",
    ""
  ],
  [
    "return_chain",
    "FAIL",
    "alias := forward(expected);copy(alias,observed)",
    "func identity(v []int)[]int{return v};func forward(v []int)[]int{return identity(v)}"
  ],
  [
    "return_direct_view",
    "FAIL",
    "copy(identity(expected),observed)",
    "func identity(v []int)[]int{return v[:]}"
  ],
  [
    "return_pointer",
    "FAIL",
    "box := identity(expected);copy(box.Value,observed)",
    "type Box struct{Value []int};func identity(v []int)*Box{return &Box{Value:v}}"
  ],
  [
    "return_struct",
    "FAIL",
    "box := identity(expected);copy(box.Value,observed)",
    "type Box struct{Value []int};func identity(v []int)Box{return Box{Value:v}}"
  ],
  [
    "return_map",
    "FAIL",
    "box := identity(expected);copy(box[\"v\"],observed)",
    "func identity(v []int)map[string][]int{return map[string][]int{\"v\":v}}"
  ],
  [
    "return_conditional",
    "FAIL",
    "alias := identity(expected, observed, len(observed)>0);copy(alias,observed)",
    "func identity(v,src []int,b bool)[]int{if b{return v};return src}"
  ],
  [
    "return_named",
    "FAIL",
    "alias := identity(expected);copy(alias,observed)",
    "func identity(v []int)(r []int){r=v;return}"
  ],
  [
    "return_multi_first",
    "FAIL",
    "alias,_ := identity(expected);copy(alias,observed)",
    "func identity(v []int)([]int,error){return v,nil}"
  ],
  [
    "return_multi_second",
    "FAIL",
    "_,alias := identity(expected);copy(alias,observed)",
    "func identity(v []int)(error,[]int){return nil,v}"
  ],
  [
    "return_defer_write",
    "FAIL",
    "alias := identity(expected,observed);_ = alias",
    "func identity(v,src []int)[]int{defer copy(v,src);return v}"
  ],
  [
    "return_defer_named_rebind",
    "PASS",
    "alias := identity(expected,FIXED);copy(alias,observed)",
    "func identity(v,original []int)(r []int){r=v;defer func(){r=original}();return}"
  ],
  [
    "return_defer_local_rebind",
    "FAIL",
    "alias := identity(expected,FIXED);copy(alias,observed)",
    "func identity(v,original []int)[]int{defer func(){v=original}();return v}"
  ],
  [
    "return_method",
    "FAIL",
    "box := Box{Value:expected};alias := box.Values();copy(alias,observed)",
    "type Box struct{Value []int};func(box Box)Values()[]int{return box.Value}"
  ],
  [
    "return_bound_method",
    "FAIL",
    "box := Box{Value:expected};saved:=box.Values;alias := saved();copy(alias,observed)",
    "type Box struct{Value []int};func(box Box)Values()[]int{return box.Value}"
  ],
  [
    "named_roundtrip",
    "FAIL",
    "alias:=[]int(Statuses(expected));copy(alias,observed)",
    "type Statuses []int"
  ],
  [
    "named_chain",
    "FAIL",
    "alias:=Other(expected);copy(alias,observed)",
    "type Statuses []int;type Other Statuses"
  ],
  [
    "named_alias_chain",
    "FAIL",
    "alias:=Other(expected);copy(alias,observed)",
    "type Statuses []int;type Other = Statuses"
  ],
  [
    "named_literal",
    "PASS",
    "alias:=Statuses{0};copy(alias,observed)",
    "type Statuses []int"
  ],
  [
    "named_restore",
    "PASS",
    "copy(expected,observed);copy(expected,Statuses(FIXED))",
    "type Statuses []int"
  ],
  [
    "named_helper",
    "FAIL",
    "change(Statuses(expected),observed)",
    "type Statuses []int;func change(v Statuses,src []int){copy(v,src)}"
  ],
  [
    "named_map",
    "FAIL",
    "box:=Boxes{\"v\":expected};copy(box[\"v\"],observed)",
    "type Boxes map[string][]int"
  ],
  [
    "named_map_alias",
    "FAIL",
    "box:=map[string][]int{\"v\":expected};alias:=Boxes(box);copy(alias[\"v\"],observed)",
    "type Boxes map[string][]int"
  ],
  [
    "map_deleted_old_alias",
    "FAIL",
    "box:=map[string][]int{\"v\":expected};alias:=box[\"v\"];delete(box,\"v\");copy(alias,observed)",
    ""
  ],
  [
    "map_clear_old_alias",
    "FAIL",
    "box:=map[string][]int{\"v\":expected};alias:=box[\"v\"];clear(box);copy(alias,observed)",
    ""
  ],
  [
    "map_empty_source_restore",
    "FAIL",
    "box:=map[string][]int{\"v\":FIXED};copy(expected,observed);copy(expected,box[\"v\"][:0])",
    ""
  ],
  [
    "map_full_view_restore",
    "PASS",
    "box:=map[string][]int{\"v\":FIXED};copy(expected,observed);copy(expected,box[\"v\"][:])",
    ""
  ],
  [
    "return_independent_allocations",
    "PASS",
    "one:=fresh(FIXED);two:=fresh(FIXED);copy(one,observed);copy(expected,two)",
    "func fresh(v []int)[]int{return v}"
  ],
  [
    "return_branch_independent",
    "PASS",
    "alias:=identity(len(observed)>0);copy(alias,observed)",
    "func identity(b bool)[]int{if b{return []int{0}};return []int{0}}"
  ],
  [
    "return_source_restore",
    "PASS",
    "copy(expected,observed);copy(expected,identity(FIXED))",
    "func identity(v []int)[]int{return v[:]}"
  ],
  [
    "return_empty_restore",
    "FAIL",
    "copy(expected,observed);copy(expected,identity(FIXED))",
    "func identity(v []int)[]int{return v[:0]}"
  ],
  [
    "return_method_restore",
    "PASS",
    "box:=Box{Value:FIXED};copy(expected,observed);copy(expected,box.Values())",
    "type Box struct{Value []int};func(box Box)Values()[]int{return box.Value}"
  ],
  [
    "return_deferred_old_field",
    "FAIL",
    "box:=Box{Value:expected};alias:=box.Values(FIXED);copy(alias,observed)",
    "type Box struct{Value []int};func(box *Box)Values(v []int)[]int{defer func(){box.Value=v}();return box.Value}"
  ],
  [
    "return_deferred_named_field",
    "PASS",
    "box:=Box{Value:expected};alias:=box.Values(FIXED);copy(alias,observed)",
    "type Box struct{Value []int};func(box *Box)Values(v []int)(r []int){r=box.Value;defer func(){r=v}();return}"
  ],
  [
    "named_empty_window",
    "PASS",
    "alias:=Statuses(expected[:0]);copy(alias,observed)",
    "type Statuses []int"
  ],
  [
    "named_reslice_window",
    "FAIL",
    "alias:=Statuses(expected[:0]);copy(alias[:1],observed)",
    "type Statuses []int"
  ],
  [
    "named_defer",
    "FAIL",
    "change(Statuses(expected),observed)",
    "type Statuses []int;func change(v Statuses,src []int){defer copy(v,src)}"
  ],
  [
    "named_return",
    "FAIL",
    "alias:=identity(expected);copy(alias,observed)",
    "type Statuses []int;func identity(v []int)Statuses{return Statuses(v)}"
  ],
  [
    "named_defined_parameter",
    "FAIL",
    "change(Statuses(expected),observed)",
    "type Statuses []int;func change(v Statuses,src []int){v[0]=src[0]}"
  ],
  [
    "named_alias_parameter",
    "FAIL",
    "change(Statuses(expected),observed)",
    "type Statuses = []int;func change(v Statuses,src []int){copy(v,src)}"
  ]
]

REFERENCE_CASES += [
    ('map_parameter_key_write', 'FAIL', 'box:=map[string][]int{"v":expected};change(box,"v",observed)',
     'func change(m map[string][]int,key string,src []int){copy(m[key],src)}'),
    ('map_parameter_key_bind', 'FAIL', 'box:=map[string][]int{};bind(box,"v",expected);copy(box["v"],observed)',
     'func bind(m map[string][]int,key string,src []int){m[key]=src}'),
    ('map_return_parameter_key', 'FAIL', 'box:=map[string][]int{"v":expected};alias:=identity(box,"v");copy(alias,observed)',
     'func identity(m map[string][]int,key string)[]int{return m[key]}'),
    ('return_function_alias', 'FAIL', 'alias:=forward(expected);copy(alias,observed)',
     'func identity(v []int)[]int{return v};func forward(v []int)[]int{saved:=identity;return saved(v)}'),
    ('return_named_grouped', 'FAIL', '_,alias:=identity(expected);copy(alias,observed)',
     'func identity(v []int)(a,b []int){a=v;b=v;return}'),
    ('named_field_constructor', 'FAIL', 'box:=Box{Value:Statuses(expected)};copy(box.Value,observed)',
     'type Statuses []int;type Box struct{Value Statuses}'),
    ('named_field_assignment', 'FAIL', 'var box Box;box.Value=expected;copy(box.Value,observed)',
     'type Statuses []int;type Box struct{Value Statuses}'),
    ('named_map_values', 'FAIL', 'box:=map[string]Statuses{"v":Statuses(expected)};copy(box["v"],observed)',
     'type Statuses []int'),
    ('named_var_declaration', 'FAIL', 'var alias Statuses=expected;copy(alias,observed)',
     'type Statuses []int'),
]

ARGUMENT_CASES = [
    ('identity', 'PASS', 'identity(expected)', 'func identity(v []int) []int { return v }'),
    ('full_view', 'PASS', 'identity(expected)', 'func identity(v []int) []int { return v[:] }'),
    ('empty', 'FAIL', 'identity(expected)', 'func identity(v []int) []int { return v[:0] }'),
    ('actual', 'FAIL', 'identity(observed)', 'func identity(v []int) []int { return v }'),
    ('defined_conversion', 'PASS', 'Statuses(expected)', 'type Statuses []int'),
    ('alias_conversion', 'PASS', 'Statuses(expected)', 'type Statuses = []int'),
    ('roundtrip_conversion', 'PASS', '[]int(Statuses(expected))', 'type Statuses []int'),
]


ALLOCATION_CASES = [
 ('clone_slice','PASS','saved:=make([]int,len(expected));copy(saved,expected)','saved',''),
 ('clone_alias','PASS','saved:=make(Statuses,len(expected));copy(saved,expected)','saved','type Statuses = []int'),
 ('clone_defined','PASS','saved:=make(Statuses,len(expected));copy(saved,expected)','saved','type Statuses []int'),
 ('clone_roundtrip','PASS','saved:=make(Statuses,len(expected));copy(saved,expected)','[]int(saved)','type Statuses []int'),
 ('clone_changed_source','FAIL','saved:=make([]int,len(expected));copy(saved,observed)','saved',''),
 ('clone_empty','FAIL','saved:=make([]int,0);copy(saved,expected)','saved',''),
 ('clone_short','FAIL','saved:=make([]int,len(expected)-1);copy(saved,expected)','saved',''),
 ('clone_long','FAIL','saved:=make([]int,len(expected)+1);copy(saved,expected)','saved',''),
 ('clone_skipped_copy','FAIL','saved:=make([]int,len(expected));copy(saved[:0],expected)','saved',''),
 ('clone_restore','PASS','saved:=make([]int,len(expected));copy(saved,expected);copy(expected,observed);copy(expected,saved)','expected',''),
 ('clone_alias_mutation','FAIL','saved:=make([]int,len(expected));copy(saved,expected);alias:=saved;copy(alias,observed)','saved',''),
 ('clone_independent_mutation','PASS','saved:=make([]int,len(expected));copy(saved,observed)','expected',''),
 ('clone_zero_bound_control','PASS','saved:=make([]int,0);copy(saved,observed)','expected',''),
 ('independent_interface','PASS','saved:=make([]interface{},1);_ = saved','expected',''),
 ('independent_struct','PASS','saved:=make([]struct{Value int},1);_ = saved','expected',''),
 ('independent_function','PASS','saved:=make([]func()int,1);_ = saved','expected',''),
]


def _review_reference_cases():
    cases = [
        ('tuple_second_shared', 'FAIL', '_,alias:=split(expected,FIXED);copy(alias,observed)', 'func split(v,fresh []int)([]int,[]int){return fresh,v}'),
        ('tuple_second_independent', 'PASS', '_,alias:=split(expected,FIXED);copy(alias,observed)', 'func split(v,fresh []int)([]int,[]int){return v,fresh}'),
        ('tuple_third_shared', 'FAIL', '_,_,alias:=split(expected,FIXED);copy(alias,observed)', 'func split(v,fresh []int)([]int,[]int,[]int){return fresh,fresh,v}'),
        ('tuple_named_second_shared', 'FAIL', '_,alias:=split(expected,FIXED);copy(alias,observed)', 'func split(v,fresh []int)(a,b []int){a=fresh;b=v;return}'),
        ('tuple_named_second_independent', 'PASS', '_,alias:=split(expected,FIXED);copy(alias,observed)', 'func split(v,fresh []int)(a,b []int){a=v;b=fresh;return}'),
        ('grouped_three_first_shared', 'FAIL', 'alias,_,_:=split(expected,FIXED);copy(alias,observed)', 'func split(v,fresh []int)(a,b,c []int){a=v;b=fresh;c=fresh;return}'),
        ('grouped_three_last_shared', 'FAIL', '_,_,alias:=split(expected,FIXED);copy(alias,observed)', 'func split(v,fresh []int)(a,b,c []int){a=fresh;b=fresh;c=v;return}'),
        ('grouped_three_independent', 'PASS', '_,_,alias:=split(expected,FIXED);copy(alias,observed)', 'func split(v,fresh []int)(a,b,c []int){a=v;b=fresh;c=fresh;return}'),
        ('returned_object_method', 'FAIL', 'alias:=makeBox(expected).Values();copy(alias,observed)', 'type Box struct{Value []int};func makeBox(v []int)Box{return Box{Value:v}};func(b Box)Values()[]int{return b.Value}'),
        ('returned_pointer_method', 'FAIL', 'alias:=makeBox(expected).Values();copy(alias,observed)', 'type Box struct{Value []int};func makeBox(v []int)*Box{return &Box{Value:v}};func(b *Box)Values()[]int{return b.Value}'),
        ('returned_pointer_method_independent', 'PASS', 'alias:=makeBox(FIXED).Values();copy(alias,observed)', 'type Box struct{Value []int};func makeBox(v []int)*Box{return &Box{Value:v}};func(b *Box)Values()[]int{return b.Value}'),
        ('returned_slice_index', 'FAIL', 'identity(expected)[0]=observed[0]', 'func identity(v []int)[]int{return v}'),
        ('returned_slice_view', 'FAIL', 'copy(identity(expected)[:],observed)', 'func identity(v []int)[]int{return v}'),
        ('map_tuple_second_shared', 'FAIL', '_,box:=split(expected,FIXED);copy(box["v"],observed)', 'func split(v,fresh []int)(map[string][]int,map[string][]int){return map[string][]int{"v":fresh},map[string][]int{"v":v}}'),
        ('pointer_tuple_second_shared', 'FAIL', '_,box:=split(expected,FIXED);copy(box.Value,observed)', 'type Box struct{Value []int};func split(v,fresh []int)(*Box,*Box){return &Box{Value:fresh},&Box{Value:v}}'),
        ('struct_tuple_second_shared', 'FAIL', '_,box:=split(expected,FIXED);copy(box.Value,observed)', 'type Box struct{Value []int};func split(v,fresh []int)(Box,Box){return Box{Value:fresh},Box{Value:v}}'),
    ]

    # Independent oracle: only the selected result is copied. Returning v there
    # must affect expected; returning fresh there must leave expected unchanged.
    for width in range(1, 6):
        for slot in range(width):
            for named in (False, True):
                for shared in (False, True):
                    expressions = ['fresh'] * width
                    expressions[slot if shared else (slot + 1) % width] = 'v'
                    if not shared and width == 1:
                        expressions[0] = 'fresh'
                    names = ['r' + str(n) for n in range(width)]
                    signature = '(' + (','.join(names) + ' []int' if named else ','.join(['[]int'] * width)) + ')'
                    body = (';'.join(n + '=' + value for n,value in zip(names,expressions)) + ';return'
                            if named else 'return ' + ','.join(expressions))
                    left = ['_'] * width; left[slot] = 'alias'
                    operation = ','.join(left) + ':=split(expected,FIXED);copy(alias,observed)'
                    name = 'result_matrix_' + '_'.join(map(str,(width,slot,int(named),int(shared))))
                    cases.append((name,'FAIL' if shared else 'PASS',operation,'func split(v,fresh []int)' + signature + '{' + body + '}'))

    for setup,target in (
        ('var box Box;box.A,box.B=split(expected,FIXED)', 'box.B'),
        ('box:=make([][]int,2);box[0],box[1]=split(expected,FIXED)', 'box[1]'),
        ('box:=map[string][]int{};box["a"],box["b"]=split(expected,FIXED)', 'box["b"]'),
    ):
        for shared in (False,True):
            returned = 'fresh,v' if shared else 'v,fresh'
            cases.append(('field_tuple_' + str(len(cases)), 'FAIL' if shared else 'PASS',
                setup+';copy('+target+',observed)',
                'type Box struct{A []int;B []int};func split(v,fresh []int)([]int,[]int){return '+returned+'}'))

    boxdecl = ('type Box struct{Value []int};func makeBox(v []int)Box{return Box{Value:v}};'
               'func(b Box)Next()Box{return b};func(b Box)Values()[]int{return b.Value}')
    cases += [
        ('returned_object_chain_three', 'FAIL', 'alias:=makeBox(expected).Next().Values();copy(alias,observed)', boxdecl),
        ('returned_object_chain_three_independent', 'PASS', 'alias:=makeBox(FIXED).Next().Values();copy(alias,observed)', boxdecl),
        ('returned_object_parentheses', 'FAIL', 'alias:=(makeBox(expected)).Values();copy(alias,observed)', boxdecl),
        ('returned_object_bound_method', 'FAIL', 'saved:=makeBox(expected).Values;alias:=saved();copy(alias,observed)', boxdecl),
        ('returned_object_index_method', 'FAIL', 'alias:=makeBoxes(expected)[0].Values();copy(alias,observed)', 'type Box struct{Value []int};func makeBoxes(v []int)[]Box{return []Box{{Value:v}}};func(b Box)Values()[]int{return b.Value}'),
        ('returned_object_deferred_destination', 'FAIL', 'change(expected,observed)', boxdecl+';func change(dst,src []int){defer copy(makeBox(dst).Values(),src)}'),
    ]
    return cases


REVIEW_REFERENCE_CASES = _review_reference_cases()


def group(name):
    if name.startswith('map_'):
        return 'map_capture' if 'defer' in name else 'map_storage'
    if name.startswith('return_'):
        if 'defer' in name:
            return 'return_capture'
        if 'multi' in name:
            return 'return_slots'
        if 'method' in name:
            return 'return_methods'
        return 'return_values'
    return 'named_storage'


class ReferenceProvenance(unittest.TestCase):
    def setUp(self):
        self.case = RegradeRegressions()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def check_group(self, selected):
        before = self.case.container_source()
        for name, expected, operation, declaration in REFERENCE_CASES:
            if group(name) != selected:
                continue
            rewritten = before.replace('assert.Exactly',
                operation.replace('FIXED', '[]int{0}') + '; assert.Exactly') + '\n' + declaration
            with self.subTest(name=name):
                self.assertEqual(self.case.integrity(before, rewritten)[0]['status'], expected)

    def test_map_storage(self):
        self.check_group('map_storage')

    def test_map_capture(self):
        self.check_group('map_capture')

    def test_return_values(self):
        self.check_group('return_values')

    def test_return_slots(self):
        self.check_group('return_slots')

    def test_return_methods(self):
        self.check_group('return_methods')

    def test_return_capture(self):
        self.check_group('return_capture')

    def test_named_storage(self):
        self.check_group('named_storage')

    def check_review_group(self, selected):
        before = self.case.container_source()
        for name, expected, operation, declaration in REVIEW_REFERENCE_CASES:
            category = ('matrix' if name.startswith('result_matrix_') else
                        'fields' if name.startswith('field_tuple_') else
                        'chains' if name.startswith('returned_') else 'slots')
            if category != selected:
                continue
            after = before.replace('assert.Exactly',
                operation.replace('FIXED', '[]int{0}') + '; assert.Exactly') + '\n' + declaration
            with self.subTest(name=name):
                self.assertEqual(self.case.integrity(before, after)[0]['status'], expected)

    def test_multiple_results_keep_each_reference_slot(self):
        self.check_review_group('slots')

    def test_each_named_and_unnamed_result_position(self):
        self.check_review_group('matrix')

    def test_parallel_results_written_to_fields_and_elements(self):
        self.check_review_group('fields')

    def test_returned_receivers_and_chained_method_values(self):
        self.check_review_group('chains')

    def test_bounded_allocations_and_full_copy_preserve_expected_values(self):
        before = self.case.container_source().replace('[]int{0}', '[]int{1,2}')
        before = before.replace('assert.Exactly(t, expected, observed)', 'check(expected,observed,t)')
        before += '\nfunc check(v,actual []int,t *testing.T){assert.Exactly(t,v,actual)}'
        for name, expected, operation, argument, declaration in ALLOCATION_CASES:
            after = before.replace('check(expected,observed,t)',
                operation + ';check(' + argument + ',observed,t)') + '\n' + declaration
            with self.subTest(name=name):
                self.assertEqual(self.case.integrity(before, after)[0]['status'], expected)

        for replacement in (
            'saved:=make([]func()int,1);_=saved;if false { check(expected,observed,t) }',
            'saved:=make([]func()int,1);_=saved;unused:=func(){check(expected,observed,t)};_=unused',
        ):
            after = before.replace('check(expected,observed,t)', replacement)
            with self.subTest(unreached=replacement):
                self.assertEqual(self.case.integrity(before, after)[0]['status'], 'FAIL')

    def test_allocated_slices_keep_exactly_type_distinctions(self):
        before = self.case.container_source().replace('[]int{0}', '[]int{1,2}')
        for declaration, argument, expected in (
            ('type Statuses []int', 'saved', 'FAIL'),
            ('type Statuses = []int', 'saved', 'PASS'),
            ('type Statuses []int', '[]int(saved)', 'PASS'),
            ('type Statuses = []int', '[]int(saved)', 'PASS'),
        ):
            after = before.replace('assert.Exactly(t, expected, observed)',
                'saved:=make(Statuses,len(expected));copy(saved,expected);'
                'assert.Exactly(t,' + argument + ',observed)') + '\n' + declaration
            with self.subTest(declaration=declaration, argument=argument):
                self.assertEqual(self.case.integrity(before, after)[0]['status'], expected)
        after = before.replace('assert.Exactly(t, expected, observed)',
            'saved:=[2]int{};copy(saved[:],expected);assert.Exactly(t,saved,observed)')
        self.assertEqual(self.case.integrity(before, after)[0]['status'], 'FAIL')
        before = self.case.container_source()
        for kind, declaration, expected in (
            ('Statuses', 'type Statuses []int', 'FAIL'),
            ('Statuses', 'type Statuses = []int', 'PASS'),
            ('[]int', '', 'PASS'),
        ):
            after = before.replace('assert.Exactly(t, expected, observed)',
                'assert.Exactly(t,make(' + kind + ',1),observed)') + '\n' + declaration
            with self.subTest(kind=kind, declaration=declaration):
                self.assertEqual(self.case.integrity(before, after)[0]['status'], expected)

    def test_checking_helper_arguments_preserve_parameter_assignment_semantics(self):
        before = self.case.container_source().replace(
            'assert.Exactly(t, expected, observed)', 'check(expected,observed,t)')
        before += '\nfunc check(v,actual []int,t *testing.T){assert.Exactly(t,v,actual)}'
        for name, expected, argument, declaration in ARGUMENT_CASES:
            after = before.replace('check(expected,observed,t)',
                'check(' + argument + ',observed,t)') + '\n' + declaration
            with self.subTest(name=name):
                self.assertEqual(self.case.integrity(before, after)[0]['status'], expected)

    def test_exactly_keeps_defined_types_and_normalizes_type_aliases(self):
        before = self.case.container_source()
        for argument, declaration, expected in (
            ('Statuses(expected)', 'type Statuses []int', 'FAIL'),
            ('Statuses(expected)', 'type Statuses = []int', 'PASS'),
            ('[]int(Statuses(expected))', 'type Statuses []int', 'PASS'),
        ):
            for alias in (False, True):
                replacement = ('saved := ' + argument + '; assert.Exactly(t,saved,observed)'
                    if alias else 'assert.Exactly(t,' + argument + ',observed)')
                after = before.replace('assert.Exactly(t, expected, observed)', replacement)
                with self.subTest(argument=argument, declaration=declaration, alias=alias):
                    self.assertEqual(self.case.integrity(before, after + '\n' + declaration)[0]['status'], expected)

    def test_returned_slice_types_are_preserved_at_assertions(self):
        before = self.case.container_source()
        for returned, expression, expected in (
            ('Statuses', 'Statuses(v)', 'FAIL'),
            ('[]int', 'Statuses(v)', 'PASS'),
        ):
            for alias in (False, True):
                replacement = ('saved:=identity(expected);assert.Exactly(t,saved,observed)' if alias else
                               'assert.Exactly(t,identity(expected),observed)')
                after = before.replace('assert.Exactly(t, expected, observed)', replacement)
                after += '\ntype Statuses []int;func identity(v []int)' + returned + '{return ' + expression + '}'
                with self.subTest(returned=returned, alias=alias):
                    self.assertEqual(self.case.integrity(before, after)[0]['status'], expected)



LITERAL_COPY_CASES = []
for label, result, returned in (
    ('slice', '[]int', 'nil'),
    ('struct', 'struct{}', 'struct{}{}'),
    ('interface', 'interface{}', 'nil'),
    ('function', 'func()', 'nil'),
    ('grouped_struct', '(struct{})', 'struct{}{}'),
    ('grouped_interface', '(interface{})', 'nil'),
):
    literal = 'func()' + result + '{CHECK;return ' + returned + '}'
    LITERAL_COPY_CASES.append(('uncalled_' + label, 'FAIL', 'unused:=' + literal + ';_=unused', ''))
    LITERAL_COPY_CASES.append(('called_' + label, 'PASS', 'unused:=' + literal + '();_=unused', ''))
    unused_write = 'func()' + result + '{copy(expected,observed);return ' + returned + '}'
    LITERAL_COPY_CASES.append(('uncalled_write_' + label, 'PASS', 'unused:=' + unused_write + ';_=unused;CHECK', ''))

for name, allocation in (
    ('two_arguments', 'make([]int,len(expected))'),
    ('same_capacity', 'make([]int,len(expected),len(expected))'),
    ('larger_capacity', 'make([]int,len(expected),len(expected)+1)'),
):
    LITERAL_COPY_CASES.append(('clone_' + name, 'PASS',
        'saved:=' + allocation + ';copy(saved,expected);CALL(saved,observed,t)', ''))
    LITERAL_COPY_CASES.append(('independent_' + name, 'PASS',
        'saved:=' + allocation + ';copy(saved,observed);CHECK', ''))
    LITERAL_COPY_CASES.append(('overwrite_' + name, 'FAIL',
        'saved:=' + allocation + ';copy(saved,observed);CALL(saved,observed,t)', ''))

for name, body in (
    ('allocated_clone', 'r:=make([]int,len(v));copy(r,v);return r'),
    ('allocated_clone_capacity', 'r:=make([]int,len(v),len(v)+1);copy(r,v);return r'),
    ('append_clone', 'return append([]int(nil),v...)'),
    ('shared', 'return v'),
):
    declaration = 'func values(v []int)[]int{' + body + '}'
    LITERAL_COPY_CASES.append(('helper_check_' + name, 'PASS', 'saved:=values(expected);CALL(saved,observed,t)', declaration))
    LITERAL_COPY_CASES.append(('helper_write_' + name, 'FAIL' if name == 'shared' else 'PASS',
        'saved:=values(expected);copy(saved,observed);CHECK', declaration))

assert len(LITERAL_COPY_CASES) == 35
for label, result, returned in (
    ('map', 'map[string]int', 'nil'), ('channel', 'chan int', 'nil'),
    ('receive_channel', '<-chan int', 'nil'), ('send_channel', 'chan<-int', 'nil'),
    ('pointer_struct', '*struct{}', 'nil'), ('struct_fields', 'struct{Value int}', 'struct{Value int}{}'),
    ('interface_method', 'interface{Read([]byte)(int,error)}', 'nil'),
    ('function_result', 'func()struct{}', 'nil'), ('multi_result', '([]int,error)', 'nil,nil'),
    ('named_result', '(r []int)', 'nil'), ('grouped_function', '(func())', 'nil'),
):
    literal = 'func()'+result+'{CHECK;return '+returned+'}'
    lhs = 'unused,err:=' if label == 'multi_result' else 'unused:='
    used = ';_=unused;_=err' if label == 'multi_result' else ';_=unused'
    for grouping in ('', 'grouped_'):
        expr = '('+literal+')' if grouping else literal
        LITERAL_COPY_CASES.append((grouping+'uncalled_'+label,'FAIL','unused:='+expr+';_=unused',''))
        LITERAL_COPY_CASES.append((grouping+'called_'+label,'PASS',lhs+expr+'()'+used,''))
        LITERAL_COPY_CASES.append((grouping+'guarded_'+label,'FAIL','if len(observed)>0{'+lhs+expr+'()'+used+'}',''))

EXTRA_LITERAL_COPY_CASES = [
 ('iife_guarded_inside','FAIL','func(){if len(observed)>0{CHECK}}()',''),
 ('iife_dead_inside','FAIL','func(){if false{CHECK}}()',''),
 ('iife_early_return','FAIL','func(){return;CHECK}()',''),
 ('iife_inner_unused','FAIL','func(){unused:=func()struct{}{CHECK;return struct{}{}};_=unused}()',''),
 ('iife_nested','PASS','func(){func()[]int{CHECK;return nil}()}()',''),
 ('iife_arguments','PASS','func(v,actual []int,ctx *testing.T){CALL(v,actual,ctx)}(expected,observed,t)',''),
 ('iife_wrong_arguments','FAIL','func(v,actual []int,ctx *testing.T){CALL(v,actual,ctx)}(observed,observed,t)',''),
 ('iife_shared_write','FAIL','func(v []int){copy(v,observed)}(expected);CHECK',''),
 ('iife_independent_write','PASS','func(v []int){copy(v,observed)}([]int{1,2});CHECK',''),
 ('iife_capture_write','FAIL','func(){copy(expected,observed)}();CHECK',''),
 ('iife_capture_rebind','PASS','func(){saved:=expected;expected=saved}();CHECK',''),
 ('iife_capture_rebind_bad','FAIL','func(){expected=observed}();CHECK',''),
 ('iife_return_alias','FAIL','saved:=func(v []int)[]int{return v}(expected);copy(saved,observed);CHECK',''),
 ('iife_return_clone','PASS','saved:=func(v []int)[]int{r:=make([]int,len(v),len(v)+1);copy(r,v);return r}(expected);CALL(saved,observed,t)',''),
 ('iife_return_named','PASS','saved:=func(v []int)(r []int){r=v;return}(expected);CALL(saved,observed,t)',''),
 ('iife_named_defer','PASS','saved:=func(v []int)(r []int){r=observed;defer func(){r=v}();return}(expected);CALL(saved,observed,t)',''),
 ('iife_outer_return','PASS','func(){return}();CHECK',''),
 ('deferred_result_struct','PASS','defer func()struct{}{CHECK;return struct{}{}}()',''),
 ('deferred_result_interface','PASS','defer (func()interface{}{CHECK;return nil})()',''),
 ('deferred_guarded_result','FAIL','defer func()struct{}{if len(observed)>0{CHECK};return struct{}{}}()',''),
 ('helper_clone_forward','PASS','saved:=forward(expected);CALL(saved,observed,t)','func clone(v []int)[]int{r:=make([]int,len(v),len(v)+1);copy(r,v);return r};func forward(v []int)[]int{return clone(v)}'),
 ('helper_partial_copy','FAIL','saved:=values(expected);CALL(saved,observed,t)','func values(v []int)[]int{r:=make([]int,len(v),len(v)+1);copy(r[:1],v);return r}'),
 ('helper_wrong_copy','FAIL','saved:=values(expected,observed);CALL(saved,observed,t)','func values(v,src []int)[]int{r:=make([]int,len(v),len(v)+1);copy(r,src);return r}'),
 ('helper_clone_modified','FAIL','saved:=values(expected,observed);CALL(saved,observed,t)','func values(v,src []int)[]int{r:=make([]int,len(v),len(v)+1);copy(r,v);copy(r,src);return r}'),
 ('helper_clone_deferred_copy','PASS','saved:=values(expected);CALL(saved,observed,t)','func values(v []int)[]int{r:=make([]int,len(v),len(v)+1);defer copy(r,v);return r}'),
 ('helper_append_forward','PASS','saved:=forward(expected);CALL(saved,observed,t)','func clone(v []int)[]int{return append([]int(nil),v...)};func forward(v []int)[]int{return clone(v)}'),
 ('append_clone_wrong_source','FAIL','saved:=append([]int(nil),observed...);CALL(saved,observed,t)',''),
 ('append_clone_write','PASS','saved:=append([]int(nil),expected...);copy(saved,observed);CHECK',''),
]
for cap in ('len(expected)', 'len(expected)+1'):
    prefix='saved:=make([]int,len(expected),'+cap+');copy(saved,expected);'
    label='extra' if cap.endswith('+1') else 'exact'
    EXTRA_LITERAL_COPY_CASES.extend([
     ('append_'+label+'_alias_write','FAIL' if label=='extra' else 'PASS',prefix+'alias:=append(saved,9);copy(alias,observed);CALL(saved,observed,t)',''),
     ('append_'+label+'_view_write','FAIL',prefix+'alias:=append(saved[:0],9);copy(alias,observed);CALL(saved,observed,t)',''),
     ('append_'+label+'_capped_view','PASS',prefix+'alias:=append(saved[:0:0],9);copy(alias,observed);CALL(saved,observed,t)',''),
     ('append_'+label+'_unassigned_tail','PASS',prefix+'_=append(saved,9);CALL(saved,observed,t)',''),
     ('append_'+label+'_restore','PASS',prefix+'copy(saved,observed);_=append(saved[:0],expected...);CALL(saved,observed,t)',''),
     ('cap_'+label+'_known','PASS',prefix+'if cap(saved)=='+cap+'{CALL(saved,observed,t)}',''),
    ])
LITERAL_COPY_CASES.extend(EXTRA_LITERAL_COPY_CASES)
LITERAL_COPY_CASES.extend([
    ('cyclic_return_independent', 'PASS', 'n:=node([]int{0});_=n;CHECK',
     'type Node struct{Next *Node;Values []int};func node(v []int)*Node{r:=&Node{Values:v};r.Next=r;return r}'),
    ('cyclic_return_shared', 'FAIL', 'n:=node(expected);copy(n.Values,observed);CHECK',
     'type Node struct{Next *Node;Values []int};func node(v []int)*Node{r:=&Node{Values:v};r.Next=r;return r}'),
    ('iife_short_circuit_grouped_dead', 'FAIL', '_=false&&(func()bool{CHECK;return true})()', ''),
    ('iife_short_circuit_nested_dead', 'FAIL', '_=false&&(true||func()bool{CHECK;return false}())', ''),
    ('iife_short_circuit_and_dead', 'FAIL', '_=false&&func()bool{CHECK;return true}()', ''),
    ('iife_short_circuit_or_dead', 'FAIL', '_=true||func()bool{CHECK;return false}()', ''),
    ('iife_short_circuit_and_live', 'PASS', '_=true&&func()bool{CHECK;return true}()', ''),
    ('iife_short_circuit_or_live', 'PASS', '_=false||func()bool{CHECK;return false}()', ''),
    ('iife_short_circuit_unknown', 'FAIL', '_=(len(observed)>0)&&func()bool{CHECK;return true}()', ''),
    ('iife_short_circuit_dead_write', 'PASS', '_=false&&func()bool{copy(expected,observed);return true}();CHECK', ''),
    ('iife_short_circuit_optional_write', 'FAIL', '_=(len(observed)>0)&&func()bool{copy(expected,observed);return true}();CHECK', ''),
    ('capacity_reslice_clone', 'PASS',
     'saved:=make([]int,len(expected)-1,len(expected));copy(saved,expected);'
     'copy(saved[len(saved):len(expected)],expected[len(saved):]);CALL(saved[:len(expected)],observed,t)', ''),
    ('capacity_append_reslice_clone', 'PASS',
     'saved:=make([]int,0,len(expected));saved=append(saved,expected...);CALL(saved,observed,t)', ''),
    ('capacity_reslice_wrong_source', 'FAIL',
     'saved:=make([]int,len(expected)-1,len(expected));copy(saved,expected);'
     'copy(saved[len(saved):len(expected)],observed);CALL(saved[:len(expected)],observed,t)', ''),
    ('capacity_reslice_index_read', 'PASS',
     'saved:=make([]int,len(expected)-1,len(expected));copy(saved[:len(expected)],expected);'
     'saved[len(saved):len(expected)][0]=expected[len(expected)-1];CALL(saved[:len(expected)],observed,t)', ''),
])




class LiteralAndCopyRegressions(unittest.TestCase):
    def test_slice_comparisons_keep_opaque_call_occurrences(self):
        case = RegradeRegressions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        before = ('package mime\nimport "sync/atomic"\nvar counter int32\n'
                  'func next()int{return int(atomic.AddInt32(&counter,1))}\n'
                  'func TestA(t *testing.T){expected:=[]int{next()};saved:=[]int{next()};'
                  'observed:=[]int{1};_=saved;assert.Exactly(t,expected,observed)}')
        after = before.replace('assert.Exactly(t,expected,observed)', 'assert.Exactly(t,saved,observed)')
        self.assertEqual(case.integrity(before, after)[0]['status'], 'FAIL')

    def test_comparison_ignores_capacity_but_helpers_can_observe_it(self):
        case = RegradeRegressions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        before = case.container_source().replace('expected := []int{0}',
            'backing:=[]int{1,2,3};expected:=backing[:2]')
        for assertion in ('Exactly', 'Equal', 'EqualValues', 'EqualExportedValues'):
            original = before.replace('assert.Exactly', 'assert.' + assertion)
            after = original.replace('t, expected, observed', 't, expected[:2:2], observed')
            with self.subTest(assertion=assertion):
                self.assertEqual(case.integrity(original, after)[0]['status'], 'PASS')
        before = before.replace('assert.Exactly(t, expected, observed)', 'check(expected,observed,t)')
        before += '\nfunc check(v,actual []int,t *testing.T){if cap(v)==3{assert.Exactly(t,v,actual)}}'
        after = before.replace('check(expected,observed,t)', 'check(expected[:2:2],observed,t)')
        self.assertEqual(case.integrity(before, after)[0]['status'], 'FAIL')

    def check_cases(self, select):
        case = RegradeRegressions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        before = case.container_source().replace('[]int{0}', '[]int{1,2}')
        call = 'check(expected,observed,t)'
        before = before.replace('assert.Exactly(t, expected, observed)', call)
        before += '\nfunc check(v,actual []int,t *testing.T){assert.Exactly(t,v,actual)}'
        for name, expected, template, declaration in LITERAL_COPY_CASES:
            if select(name):
                replacement = template.replace('CHECK', call).replace('CALL', 'check')
                after = before.replace(call, replacement) + '\n' + declaration
                with self.subTest(name=name):
                    self.assertEqual(case.integrity(before, after)[0]['status'], expected)

    def test_nil_and_empty_slice_clones_keep_distinct_values(self):
        case = RegradeRegressions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        for initial in ('[]int{}', '[]int(nil)', '[]int{1,2}'):
            before = case.container_source().replace('[]int{0}', initial)
            for body, expected in (
                ('return append([]int(nil),v...)', 'FAIL' if initial == '[]int{}' else 'PASS'),
                ('r:=make([]int,len(v),len(v)+1);copy(r,v);return r',
                 'FAIL' if initial == '[]int(nil)' else 'PASS'),
            ):
                after = before.replace('assert.Exactly(t, expected, observed)',
                    'saved:=clone(expected);assert.Exactly(t,saved,observed)')
                after += '\nfunc clone(v []int)[]int{' + body + '}'
                with self.subTest(initial=initial, body=body):
                    self.assertEqual(case.integrity(before, after)[0]['status'], expected)

    def test_append_clones_keep_defined_slice_types(self):
        case = RegradeRegressions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        before = case.container_source().replace('[]int{0}', '[]int{1,2}')
        for allocation in ('Statuses(nil)', 'Statuses(expected[:0])'):
            for declaration, expected in (('type Statuses []int', 'FAIL'), ('type Statuses = []int', 'PASS')):
                after = before.replace('assert.Exactly(t, expected, observed)',
                    'saved:=append(' + allocation + ',expected...);assert.Exactly(t,saved,observed)')
                with self.subTest(allocation=allocation, declaration=declaration):
                    self.assertEqual(case.integrity(before, after + '\n' + declaration)[0]['status'], expected)

    def test_literal_signatures_and_actual_execution(self):
        self.check_cases(lambda name: name.startswith(('uncalled_', 'called_', 'grouped_', 'guarded_')))

    def test_closure_arguments_captures_returns_and_defers(self):
        self.check_cases(lambda name: name.startswith(('iife_', 'deferred_')))

    def test_copy_results_allocation_capacity_and_independence(self):
        self.check_cases(lambda name: not name.startswith(
            ('uncalled_', 'called_', 'grouped_', 'guarded_', 'iife_', 'deferred_')))


if __name__ == '__main__':
    unittest.main()


# Independent, two-sided counterexamples from both postrepair audits.
COMPLETE_REPAIR_CASES = [
    ('if_header_check', 'PASS', 'if func()bool{CHECK;return true}(){}', ''),
    ('if_header_true', 'PASS', 'if func()bool{return true}(){CHECK}', ''),
    ('if_header_false', 'FAIL', 'if func()bool{return false}(){CHECK}', ''),
    ('for_header_check', 'PASS', 'for func()bool{CHECK;return false}(){}', ''),
    ('for_header_false', 'FAIL', 'for func()bool{return false}(){CHECK}', ''),
    ('switch_header_check', 'PASS', 'switch func()int{CHECK;return 1}(){case 1:}', ''),
    ('switch_header_false', 'FAIL', 'switch func()int{return 0}(){case 1:CHECK}', ''),
    ('stored_closure_called', 'PASS', 'f:=func(){CHECK};f()', ''),
    ('stored_closure_uncalled', 'FAIL', 'f:=func(){CHECK};_=f', ''),
    ('stored_closure_alias', 'PASS', 'f:=func(){CHECK};g:=f;g()', ''),
    ('stored_closure_guarded', 'FAIL', 'f:=func(){CHECK};if len(observed)>0{f()}', ''),
    ('fresh_tuple_same_check', 'PASS', 'one,two:=values(expected);_=one;CALL(two,observed,t)', 'func values(v []int)([]int,[]int){r:=make([]int,len(v));copy(r,v);return r,r}'),
    ('fresh_tuple_same_write', 'FAIL', 'one,two:=values(expected);copy(one,observed);CALL(two,observed,t)', 'func values(v []int)([]int,[]int){r:=make([]int,len(v));copy(r,v);return r,r}'),
    ('fresh_tuple_independent_write', 'PASS', 'one,two:=values(expected);copy(one,observed);CALL(two,observed,t)', 'func values(v []int)([]int,[]int){r:=make([]int,len(v));s:=make([]int,len(v));copy(r,v);copy(s,v);return r,s}'),
    ('fresh_tuple_view_write', 'FAIL', 'one,two:=values(expected);copy(one,observed);CALL(two,observed,t)', 'func values(v []int)([]int,[]int){r:=make([]int,len(v));copy(r,v);return r,r[:]}'),
    ('fresh_struct_same_write', 'FAIL', 'box:=values(expected);copy(box.A,observed);CALL(box.B,observed,t)', 'type Pair struct{A,B []int};func values(v []int)Pair{r:=make([]int,len(v));copy(r,v);return Pair{A:r,B:r}}'),
    ('fresh_struct_independent_write', 'PASS', 'box:=values(expected);copy(box.A,observed);CALL(box.B,observed,t)', 'type Pair struct{A,B []int};func values(v []int)Pair{r:=make([]int,len(v));s:=make([]int,len(v));copy(r,v);copy(s,v);return Pair{A:r,B:s}}'),
    ('fresh_map_same_write', 'FAIL', 'box:=values(expected);copy(box["a"],observed);CALL(box["b"],observed,t)', 'func values(v []int)map[string][]int{r:=make([]int,len(v));copy(r,v);return map[string][]int{"a":r,"b":r}}'),
    ('fresh_append_same_write', 'FAIL', 'one,two:=values(expected);copy(one,observed);CALL(two,observed,t)', 'func values(v []int)([]int,[]int){r:=append([]int(nil),v...);return r,r}'),
    ('fresh_forward_same_write', 'FAIL', 'one,two:=forward(expected);copy(one,observed);CALL(two,observed,t)', 'func values(v []int)([]int,[]int){r:=make([]int,len(v));copy(r,v);return r,r};func forward(v []int)([]int,[]int){return values(v)}'),
    ('clone_calls_independent', 'PASS', 'one:=values(expected);two:=values(expected);copy(one,observed);CALL(two,observed,t)', 'func values(v []int)[]int{r:=make([]int,len(v));copy(r,v);return r}'),
    ('append_allocated_check', 'PASS', 'saved:=append([]int(nil),expected...);CALL(saved,observed,t)', ''),
    ('append_allocated_noop_alias', 'FAIL', 'saved:=append([]int(nil),expected...);alias:=append(saved,[]int{}...);copy(alias,observed);CALL(saved,observed,t)', ''),
    ('append_tail_unobserved', 'PASS', 'saved:=append([]int(nil),expected...);_=append(saved,9);CALL(saved,observed,t)', ''),
    ('cyclic_next_shared', 'FAIL', 'n:=values(expected);copy(n.Next.Values,observed);CHECK', 'type Node struct{Next *Node;Values []int};func values(v []int)*Node{r:=&Node{Values:v};r.Next=r;return r}'),
    ('cyclic_next_independent', 'PASS', 'n:=values([]int{1,2});copy(n.Next.Values,observed);CHECK', 'type Node struct{Next *Node;Values []int};func values(v []int)*Node{r:=&Node{Values:v};r.Next=r;return r}'),
    ('goroutine_unjoined', 'FAIL', 'go func(){CHECK}()', ''),
    ('goroutine_named_unjoined', 'FAIL', 'go CALL(expected,observed,t)', ''),
    ('goroutine_blocked_receive', 'FAIL', 'go func(){<-make(chan struct{});CHECK}()', ''),
    ('goroutine_blocked_select', 'FAIL', 'go func(){select{};CHECK}()', ''),
    ('goroutine_joined', 'PASS', 'done:=make(chan struct{});go func(){CHECK;close(done)}();<-done', ''),
    ('goroutine_no_check_then_sync', 'FAIL', 'done:=make(chan struct{});go func(){close(done);return;CHECK}();<-done', ''),
    ('goroutine_unrelated', 'PASS', 'go func(){}();CHECK', ''),
    ('cleanup_callback', 'PASS', 't.Cleanup(func(){CHECK})', ''),
    ('subtest_callback', 'PASS', 't.Run("probe",func(_ *testing.T){CHECK})', ''),
    ('subtest_unrelated', 'PASS', 't.Run("probe",func(_ *testing.T){});CHECK', ''),
    ('recovered_panic_test', 'FAIL', 'defer func(){recover()}();auditStop();CHECK', 'func auditStop(){panic("skip check")}'),
    ('recovered_panic_literal', 'FAIL', 'func(){defer func(){recover()}();auditStop();CHECK}()', 'func auditStop(){panic("skip check")}'),
    ('recovery_without_panic', 'PASS', 'func(){defer func(){recover()}();CHECK}()', ''),
    ('recovered_panic_after_check', 'PASS', 'func(){defer func(){recover()}();CHECK;auditStop()}()', 'func auditStop(){panic("after check")}'),
    ('loop_second_iteration_write', 'FAIL', 'for i:=0;i<2;i++{if i==1{copy(expected,observed)}};CHECK', ''),
    ('loop_second_iteration_write_post', 'FAIL', 'i:=0;for i<2{if i==1{copy(expected,observed)};i++};CHECK', ''),
    ('loop_third_iteration_write', 'FAIL', 'for i:=0;i<3;i++{if i==2{copy(expected,observed)}};CHECK', ''),
    ('loop_no_write', 'PASS', 'for i:=0;i<2;i++{if i==3{copy(expected,observed)}};CHECK', ''),
    ('loop_break_before_write', 'PASS', 'for i:=0;i<2;i++{if i==1{break};if i==1{copy(expected,observed)}};CHECK', ''),
    ('loop_first_iteration_write', 'FAIL', 'for i:=0;i<2;i++{if i==0{copy(expected,observed)}};CHECK', ''),
    ('loop_restores_first_element', 'PASS', 'saved:=expected[0];expected[0]=9;for i:=0;i<2;i++{if i==1{expected[0]=saved}};CHECK', ''),
    ('range_single_unconditional', 'PASS', 'for range [1]int{}{CHECK}', ''),
    ('range_zero_skipped', 'FAIL', 'for range [0]int{}{CHECK}', ''),
    ('range_empty_slice', 'FAIL', 'for range []int{}{CHECK}', ''),
    ('range_parenthesized_empty', 'FAIL', 'for range ([]int{}){CHECK}', ''),
    ('range_parenthesized_single', 'PASS', 'for range ([]int{1}){CHECK}', ''),
    ('range_make_zero', 'FAIL', 'for range make([]int,0){CHECK}', ''),
    ('range_make_single', 'PASS', 'for range make([]int,1){CHECK}', ''),
    ('fresh_fields_separate_same', 'FAIL', 'box:=values(expected);copy(box.A,observed);CALL(box.B,observed,t)', 'type AuditPair struct{A []int;B []int};func values(v []int)AuditPair{r:=make([]int,len(v));copy(r,v);return AuditPair{A:r,B:r}}'),
    ('fresh_fields_separate_independent', 'PASS', 'box:=values(expected);copy(box.A,observed);CALL(box.B,observed,t)', 'type AuditPair struct{A []int;B []int};func values(v []int)AuditPair{r:=make([]int,len(v));s:=make([]int,len(v));copy(r,v);copy(s,v);return AuditPair{A:r,B:s}}'),
    ('fresh_slice_of_slices_shared', 'FAIL', 'box:=values(expected);copy(box[0],observed);CALL(box[1],observed,t)', 'func values(v []int)[][]int{r:=make([]int,len(v));copy(r,v);return [][]int{r,r}}'),
    ('fresh_slice_of_slices_independent', 'PASS', 'box:=values(expected);copy(box[0],observed);CALL(box[1],observed,t)', 'func values(v []int)[][]int{r:=make([]int,len(v));s:=make([]int,len(v));copy(r,v);copy(s,v);return [][]int{r,s}}'),
    ('fresh_array_of_slices_shared', 'FAIL', 'box:=values(expected);copy(box[0],observed);CALL(box[1],observed,t)', 'func values(v []int)[2][]int{r:=make([]int,len(v));copy(r,v);return [2][]int{r,r}}'),
    ('fresh_array_of_slices_independent', 'PASS', 'box:=values(expected);copy(box[0],observed);CALL(box[1],observed,t)', 'func values(v []int)[2][]int{r:=make([]int,len(v));s:=make([]int,len(v));copy(r,v);copy(s,v);return [2][]int{r,s}}'),
    ('fresh_tuple_equal_independent', 'PASS', 'one,two:=values(expected);copy(one,observed);CALL(two,observed,t)', 'func values(v []int)([]int,[]int){r:=append([]int(nil),v...);s:=append([]int(nil),v...);return r,s}'),
    ('fresh_slice_result_effect_shared', 'FAIL', 'var alias []int;one:=values(expected,&alias);copy(one,observed);CALL(alias,observed,t)', 'func values(v []int,out *[]int)[]int{r:=make([]int,len(v));copy(r,v);*out=r;return r}'),
    ('fresh_slice_result_effect_independent', 'PASS', 'var alias []int;one:=values(expected,&alias);copy(one,observed);CALL(alias,observed,t)', 'func values(v []int,out *[]int)[]int{r:=make([]int,len(v));s:=make([]int,len(v));copy(r,v);copy(s,v);*out=s;return r}'),
    ('fresh_pointer_duplicate', 'FAIL', 'one,two:=values(expected);copy(*one,observed);CALL(*two,observed,t)', 'func values(v []int)(*[]int,*[]int){r:=make([]int,len(v));copy(r,v);return &r,&r}'),
    ('fresh_pointer_independent', 'PASS', 'one,two:=values(expected);copy(*one,observed);CALL(*two,observed,t)', 'func values(v []int)(*[]int,*[]int){r:=make([]int,len(v));s:=make([]int,len(v));copy(r,v);copy(s,v);return &r,&s}'),
    ('map_delete_independent_key', 'PASS', 'box:=map[string][]int{"a":expected,"b":observed};delete(box,"b");CALL(box["a"],observed,t)', ''),
    ('map_delete_expected_key', 'FAIL', 'box:=map[string][]int{"a":expected};delete(box,"a");CALL(box["a"],observed,t)', ''),
    ('map_clear_original_alias', 'PASS', 'box:=map[string][]int{"a":expected};clear(box);CHECK', ''),
    ('struct_copy_slice_shared', 'FAIL', 'box:=AuditBox{V:expected};other:=box;copy(other.V,observed);CHECK', 'type AuditBox struct{V []int}'),
    ('struct_copy_array_independent', 'PASS', 'box:=AuditArray{V:[2]int{1,2}};other:=box;other.V[0]=9;_=other;CHECK', 'type AuditArray struct{V [2]int}'),
    ('stored_closure_mutation', 'FAIL', 'f:=func(){copy(expected,observed)};f();CHECK', ''),
    ('stored_closure_uncalled_mutation', 'PASS', 'f:=func(){copy(expected,observed)};_=f;CHECK', ''),
    ('stored_closure_capture_rebind', 'FAIL', 'alias:=expected;f:=func(){copy(alias,observed)};alias=expected;f();CHECK', ''),
    ('stored_closure_capture_independent', 'PASS', 'alias:=expected;f:=func(){copy(alias,observed)};alias=[]int{1,2};f();CHECK', ''),
    ('stored_closure_return_alias', 'FAIL', 'f:=func()[]int{return expected};alias:=f();copy(alias,observed);CHECK', ''),
    ('stored_closure_return_independent', 'PASS', 'f:=func()[]int{return []int{1,2}};alias:=f();copy(alias,observed);CHECK', ''),
]


NEIGHBOR_REPAIR_CASES = [
    ('cleanup_iife_registration_late_write', 'FAIL', 'func(){t.Cleanup(func(){CHECK})}();copy(expected,observed)', ''),
    ('cleanup_defer_registration_late_write', 'FAIL', 'defer func(){t.Cleanup(func(){CHECK})}();defer copy(expected,observed)', ''),
    ('cleanup_alternative_writes', 'FAIL', 't.Cleanup(func(){CHECK});saved:=append([]int(nil),expected...);if len(observed)>0{t.Cleanup(func(){copy(expected,observed)})}else{t.Cleanup(func(){copy(expected,saved)})}', ''),
    ('cleanup_iife_registration_unchanged', 'PASS', 'func(){t.Cleanup(func(){CHECK})}()', ''),
    ('cleanup_alternatives_unchanged', 'PASS', 't.Cleanup(func(){CHECK});saved:=append([]int(nil),expected...);if len(observed)>0{t.Cleanup(func(){copy(expected,saved)})}else{t.Cleanup(func(){copy(expected,saved)})}', ''),
    ('cleanup_nested_registration', 'FAIL', 't.Cleanup(func(){t.Cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('cleanup_stored_registration_late_write', 'FAIL', 'f:=func(){t.Cleanup(func(){CHECK})};f();copy(expected,observed)', ''),
    ('cleanup_stored_registration_unchanged', 'PASS', 'f:=func(){t.Cleanup(func(){CHECK})};f()', ''),
    ('cleanup_subtest_before_parent_write', 'PASS', 't.Run("probe",func(sub *testing.T){sub.Cleanup(func(){CALL(expected,observed,sub)})});copy(expected,observed)', ''),
    ('cleanup_parent_after_subtest_write', 'FAIL', 't.Cleanup(func(){CHECK});t.Run("probe",func(sub *testing.T){copy(expected,observed)})', ''),
    ('cleanup_captured_local_rebound', 'FAIL', 'func(){v:=expected;t.Cleanup(func(){CALL(v,observed,t)});v=observed}()', ''),
    ('cleanup_captured_local_unchanged', 'PASS', 'func(){v:=expected;t.Cleanup(func(){CALL(v,observed,t)})}()', ''),
    ('loop_bound_shrinks_before_check', 'FAIL', 'auditLimit:=2;for i:=0;i<auditLimit;i++{auditLimit=0;if i==1{CHECK}}', ''),
    ('loop_stable_named_bound', 'PASS', 'auditLimit:=2;for i:=0;i<auditLimit;i++{if i==1{CHECK}}', ''),
    ('loop_closure_bound_shrinks', 'FAIL', 'auditLimit:=2;auditShrink:=func(){auditLimit=0};for i:=0;i<auditLimit;i++{auditShrink();if i==1{CHECK}}', ''),
    ('loop_multiple_assignment_bound', 'FAIL', 'auditLimit:=2;auditUnused:=0;for i:=0;i<auditLimit;i++{auditLimit,auditUnused=0,1;if i==1{CHECK}};_=auditUnused', ''),
    ('loop_growing_bound_late_write', 'FAIL', 'auditLimit:=1;for i:=0;i<auditLimit;i++{auditLimit=2;if i==1{copy(expected,observed)}};CHECK', ''),
    ('loop_shrinking_bound_dead_write', 'PASS', 'auditLimit:=2;for i:=0;i<auditLimit;i++{auditLimit=0;if i==1{copy(expected,observed)}};CHECK', ''),
    ('loop_closure_shrinking_dead_write', 'PASS', 'auditLimit:=2;auditShrink:=func(){auditLimit=0};for i:=0;i<auditLimit;i++{auditShrink();if i==1{copy(expected,observed)}};CHECK', ''),
    ('range_empty_body_write', 'PASS', 'for range []int{}{copy(expected,observed)};CHECK', ''),
    ('range_make_zero_body_write', 'PASS', 'for range make([]int,0){copy(expected,observed)};CHECK', ''),
    ('range_single_body_write', 'FAIL', 'for range []int{1}{copy(expected,observed)};CHECK', ''),
    ('branch_initial_actual_unwritten_last', 'FAIL', 'saved:=values(expected,observed,len(observed)>0);CALL(saved,observed,t)', 'func values(v,actual []int,condition bool)[]int{r:=append([]int(nil),actual...);if condition{copy(r,v);return r};return r}'),
    ('branch_initial_actual_unwritten_first', 'FAIL', 'saved:=values(expected,observed,len(observed)>0);CALL(saved,observed,t)', 'func values(v,actual []int,condition bool)[]int{r:=append([]int(nil),actual...);if condition{return r};copy(r,v);return r}'),
    ('branch_initial_expected_unwritten', 'PASS', 'saved:=values(expected,len(observed)>0);CALL(saved,observed,t)', 'func values(v []int,condition bool)[]int{r:=append([]int(nil),v...);if condition{copy(r,v);return r};return r}'),
    ('branch_initial_expected_mutated', 'FAIL', 'saved:=values(expected,observed,len(observed)>0);CALL(saved,observed,t)', 'func values(v,actual []int,condition bool)[]int{r:=append([]int(nil),v...);if condition{copy(r,actual);return r};return r}'),
    ('branch_fresh_mutated_last', 'FAIL', 'saved:=values(expected,observed,len(observed)>0);CALL(saved,observed,t)', 'func values(v,actual []int,condition bool)[]int{r:=make([]int,len(v));copy(r,v);if condition{return r};copy(r,actual);return r}'),
    ('branch_fresh_mutated_first', 'FAIL', 'saved:=values(expected,observed,len(observed)>0);CALL(saved,observed,t)', 'func values(v,actual []int,condition bool)[]int{r:=make([]int,len(v));copy(r,v);if condition{copy(r,actual);return r};return r}'),
    ('branch_fresh_unchanged', 'PASS', 'saved:=values(expected,len(observed)>0);CALL(saved,observed,t)', 'func values(v []int,condition bool)[]int{r:=make([]int,len(v));copy(r,v);if condition{return r};return r}'),
    ('tuple_three_shared', 'FAIL', 'a,b,c:=values(expected);_=b;copy(a,observed);CALL(c,observed,t)', 'func values(v []int)([]int,[]int,[]int){r:=make([]int,len(v));copy(r,v);return r,r,r}'),
    ('tuple_three_independent', 'PASS', 'a,b,c:=values(expected);_=b;copy(a,observed);CALL(c,observed,t)', 'func values(v []int)([]int,[]int,[]int){r:=make([]int,len(v));s:=make([]int,len(v));copy(r,v);copy(s,v);return r,r,s}'),
    ('grouped_unkeyed_shared', 'FAIL', 'box:=values(expected);copy(box.A,observed);CALL(box.B,observed,t)', 'type Box struct{A,B []int};func values(v []int)Box{r:=make([]int,len(v));copy(r,v);return Box{r,r}}'),
    ('grouped_unkeyed_independent', 'PASS', 'box:=values(expected);copy(box.A,observed);CALL(box.B,observed,t)', 'type Box struct{A,B []int};func values(v []int)Box{r:=make([]int,len(v));s:=make([]int,len(v));copy(r,v);copy(s,v);return Box{r,s}}'),
    ('cyclic_two_hops', 'FAIL', 'n:=values(expected);copy(n.Next.Next.Values,observed);CHECK', 'type Node struct{Next *Node;Values []int};func values(v []int)*Node{r:=&Node{Values:v};r.Next=r;return r}'),
    ('cyclic_two_hops_independent', 'PASS', 'n:=values(append([]int(nil),expected...));copy(n.Next.Next.Values,observed);CHECK', 'type Node struct{Next *Node;Values []int};func values(v []int)*Node{r:=&Node{Values:v};r.Next=r;return r}'),
    ('named_shared_returns', 'FAIL', 'a,b:=values(expected);copy(a,observed);CALL(b,observed,t)', 'func values(v []int)(a,b []int){a=make([]int,len(v));copy(a,v);b=a;return}'),
    ('clone_calls_three', 'PASS', 'a:=values(expected);b:=values(expected);c:=values(expected);copy(a,observed);copy(b,observed);CALL(c,observed,t)', 'func values(v []int)[]int{r:=make([]int,len(v));copy(r,v);return r}'),
    ('closure_repeated_checks', 'PASS', 'f:=func(){CHECK};f();f()', ''),
    ('closure_rebound_noop', 'FAIL', 'f:=func(){CHECK};f=func(){};f()', ''),
    ('closure_rebound_effect', 'PASS', 'f:=func(){copy(expected,observed)};f=func(){};f();CHECK', ''),
    ('closure_parameter_shared', 'FAIL', 'f:=func(v []int){copy(v,observed)};f(expected);CHECK', ''),
    ('closure_parameter_independent', 'PASS', 'f:=func(v []int){copy(v,observed)};f(append([]int(nil),expected...));CHECK', ''),
    ('closure_restore', 'PASS', 'saved:=append([]int(nil),expected...);f:=func(v []int){copy(expected,v)};f(observed);f(saved);CHECK', ''),
    ('closure_true_call', 'PASS', 'f:=func(){CHECK};if true{f()}', ''),
    ('closure_false_call', 'FAIL', 'f:=func(){CHECK};if false{f()}', ''),
    ('loop_first_check', 'PASS', 'for i:=0;i<2;i++{if i==0{CHECK}}', ''),
    ('loop_last_check', 'PASS', 'for i:=0;i<2;i++{if i==1{CHECK}}', ''),
    ('loop_no_check', 'FAIL', 'for i:=0;i<2;i++{if i==2{CHECK}}', ''),
    ('loop_decreasing_write', 'FAIL', 'for i:=2;i>0;i--{if i==1{copy(expected,observed)}};CHECK', ''),
    ('loop_zero_check', 'FAIL', 'for i:=0;i<0;i++{CHECK}', ''),
    ('cleanup_after_mutation', 'FAIL', 't.Cleanup(func(){CHECK});copy(expected,observed)', ''),
    ('cleanup_after_deferred_mutation', 'FAIL', 'defer copy(expected,observed);t.Cleanup(func(){CHECK})', ''),
    ('cleanup_write_after_check', 'PASS', 't.Cleanup(func(){copy(expected,observed)});CHECK', ''),
    ('subtest_real_context', 'PASS', 't.Run("probe",func(sub *testing.T){CALL(expected,observed,sub)})', ''),
    ('subtest_return', 'FAIL', 't.Run("probe",func(_ *testing.T){return;CHECK})', ''),
    ('go_argument_effect', 'FAIL', 'go auditConsume(copy(expected,observed));CHECK', 'func auditConsume(n int){}'),
    ('go_close_before_check', 'FAIL', 'done:=make(chan struct{});go func(){close(done);CHECK}();<-done', ''),
    ('go_joined_after_check', 'PASS', 'done:=make(chan struct{});go func(){CHECK;close(done)}();<-done', ''),
    ('panic_literal_escape', 'FAIL', 'defer func(){recover()}();func(){panic("skip")}();CHECK', ''),
    ('panic_helper_conditional', 'FAIL', 'defer func(){recover()}();auditStop();CHECK', 'func auditStop(){if true{panic("skip")}}'),
    ('map_delete_absent', 'PASS', 'box:=map[string][]int{"a":expected};delete(box,"absent");CALL(box["a"],observed,t)', ''),
    ('map_delete_via_alias', 'FAIL', 'box:=map[string][]int{"a":expected};alias:=box;delete(alias,"a");CALL(box["a"],observed,t)', ''),
]


class CompleteRepairRegressions(unittest.TestCase):
    def check_group(self, prefixes, cases=None):
        case = RegradeRegressions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        call = 'check(expected,observed,t)'
        before = case.container_source().replace('[]int{0}', '[]int{1,2}')
        before = before.replace('assert.Exactly(t, expected, observed)', call)
        before += '\nfunc check(v,actual []int,t *testing.T){assert.Exactly(t,v,actual)}'
        for name, expected, template, declaration in (COMPLETE_REPAIR_CASES if cases is None else cases):
            if not name.startswith(prefixes):
                continue
            after = before.replace(call, template.replace('CHECK', call).replace('CALL', 'check'))
            after += '\n' + declaration
            with self.subTest(name=name):
                integrity, skips = case.integrity(before, after)
                self.assertEqual(integrity['status'], expected)
                self.assertEqual(skips['status'], 'PASS')

    def test_control_headers(self):
        self.check_group(('if_header_', 'for_header_', 'switch_header_'))

    def test_stored_closures(self):
        self.check_group(('stored_closure_',))

    def test_allocation_and_reference_identity(self):
        self.check_group(('fresh_', 'clone_calls_', 'cyclic_', 'struct_copy_'))

    def test_append_prefix_and_aliases(self):
        self.check_group(('append_',))

    def test_goroutines_and_synchronization(self):
        self.check_group(('goroutine_',))

    def test_testing_callbacks_and_recovery(self):
        self.check_group(('cleanup_', 'subtest_', 'recovered_', 'recovery_'))

    def test_bounded_loops_and_range(self):
        self.check_group(('loop_', 'range_'))

    def test_map_mutations(self):
        self.check_group(('map_',))

    def test_adjacent_boundaries(self):
        self.check_group(("",), NEIGHBOR_REPAIR_CASES)


CLEANUP_OWNER_CASES = [
    ('alias_bound_root_cleanup', 'PASS', 'cleanup:=t.Cleanup;cleanup(func(){CHECK})', ''),
    ('alias_bound_parent_cleanup', 'FAIL', 't.Run("probe",func(sub *testing.T){cleanup:=t.Cleanup;cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('alias_bound_child_cleanup', 'PASS', 't.Run("probe",func(sub *testing.T){cleanup:=sub.Cleanup;cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('alias_bound_child_chain', 'PASS', 't.Run("probe",func(sub *testing.T){cleanup:=sub.Cleanup;again:=cleanup;again(func(){CHECK})});copy(expected,observed)', ''),
    ('alias_bound_distinct_owners', 'FAIL', 't.Run("probe",func(sub *testing.T){parentCleanup:=t.Cleanup;childCleanup:=sub.Cleanup;parentCleanup(func(){CHECK});childCleanup(func(){})});copy(expected,observed)', ''),
    ('alias_bound_run', 'FAIL', 'run:=t.Run;run("probe",func(sub *testing.T){t.Cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('alias_interface_child', 'PASS', 't.Run("probe",func(sub *testing.T){var owner testing.TB=sub;owner.Cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('alias_interface_parent', 'FAIL', 't.Run("probe",func(sub *testing.T){var owner testing.TB=t;owner.Cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('stored_interface_child', 'PASS', 't.Run("probe",func(sub *testing.T){f:=func(owner testing.TB){owner.Cleanup(func(){CHECK})};f(sub)});copy(expected,observed)', ''),
    ('stored_interface_parent', 'FAIL', 't.Run("probe",func(sub *testing.T){f:=func(owner testing.TB){owner.Cleanup(func(){CHECK})};f(t)});copy(expected,observed)', ''),
    ('order_parent_registered_by_child_cleanup', 'FAIL', 't.Run("probe",func(sub *testing.T){sub.Cleanup(func(){t.Cleanup(func(){CHECK})})});copy(expected,observed)', ''),
    ('order_parent_registered_by_child_cleanup_unchanged', 'PASS', 't.Run("probe",func(sub *testing.T){sub.Cleanup(func(){t.Cleanup(func(){CHECK})})})', ''),
    ('root_parent_late_write', 'FAIL', 't.Run("probe",func(sub *testing.T){t.Cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('root_parent_restored', 'PASS', 'saved:=append([]int(nil),expected...);t.Run("probe",func(sub *testing.T){t.Cleanup(func(){CHECK});copy(expected,observed)});copy(expected,saved)', ''),
    ('root_child_before_parent_write', 'PASS', 't.Run("probe",func(sub *testing.T){sub.Cleanup(func(){CALL(expected,observed,sub)})});copy(expected,observed)', ''),
    ('root_parent_unchanged', 'PASS', 't.Run("probe",func(sub *testing.T){t.Cleanup(func(){CHECK})})', ''),
    ('root_child_before_parent_restore', 'FAIL', 'saved:=append([]int(nil),expected...);t.Run("probe",func(sub *testing.T){sub.Cleanup(func(){CALL(expected,observed,sub)});copy(expected,observed)});copy(expected,saved)', ''),
    ('root_multiple_subtests', 'FAIL', 't.Run("first",func(sub *testing.T){t.Cleanup(func(){CHECK})});t.Run("second",func(sub *testing.T){copy(expected,observed)})', ''),
    ('alias_parent_from_outer', 'FAIL', 'parent:=t;t.Run("probe",func(sub *testing.T){parent.Cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('alias_child', 'PASS', 't.Run("probe",func(sub *testing.T){owner:=sub;owner.Cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('alias_parent_rebound', 'FAIL', 't.Run("probe",func(sub *testing.T){owner:=t;owner.Cleanup(func(){CHECK});owner=sub;_=owner});copy(expected,observed)', ''),
    ('alias_child_rebound', 'PASS', 't.Run("probe",func(sub *testing.T){owner:=sub;owner.Cleanup(func(){CHECK});owner=t;_=owner});copy(expected,observed)', ''),
    ('alias_run_receiver', 'FAIL', 'parent:=t;parent.Run("probe",func(sub *testing.T){t.Cleanup(func(){CHECK})});copy(expected,observed)', ''),
    ('stored_parent_argument', 'FAIL', 't.Run("probe",func(sub *testing.T){f:=func(owner *testing.T){owner.Cleanup(func(){CHECK})};f(t)});copy(expected,observed)', ''),
    ('stored_child_argument', 'PASS', 't.Run("probe",func(sub *testing.T){f:=func(owner *testing.T){owner.Cleanup(func(){CHECK})};f(sub)});copy(expected,observed)', ''),
    ('field_parent_handle', 'FAIL', 'box:=CleanupOwnerHolder{T:t};t.Run("probe",func(sub *testing.T){box.T.Cleanup(func(){CHECK})});copy(expected,observed)', 'type CleanupOwnerHolder struct{T *testing.T}'),
    ('field_child_handle', 'PASS', 't.Run("probe",func(sub *testing.T){box:=CleanupOwnerHolder{T:sub};box.T.Cleanup(func(){CHECK})});copy(expected,observed)', 'type CleanupOwnerHolder struct{T *testing.T}'),
    ('nested_grandparent', 'FAIL', 't.Run("child",func(sub *testing.T){sub.Run("grand",func(grand *testing.T){t.Cleanup(func(){CHECK})})});copy(expected,observed)', ''),
    ('nested_parent', 'FAIL', 't.Run("child",func(sub *testing.T){sub.Run("grand",func(grand *testing.T){sub.Cleanup(func(){CALL(expected,observed,sub)})});copy(expected,observed)})', ''),
    ('nested_grandchild', 'PASS', 't.Run("child",func(sub *testing.T){sub.Run("grand",func(grand *testing.T){grand.Cleanup(func(){CALL(expected,observed,grand)})});copy(expected,observed)})', ''),
    ('nested_parent_restored', 'PASS', 'saved:=append([]int(nil),expected...);t.Run("child",func(sub *testing.T){sub.Run("grand",func(grand *testing.T){sub.Cleanup(func(){CALL(expected,observed,sub)});copy(expected,observed)});copy(expected,saved)})', ''),
    ('order_parent_write_after_child_restore', 'FAIL', 'saved:=append([]int(nil),expected...);t.Cleanup(func(){CHECK});t.Run("child",func(sub *testing.T){t.Cleanup(func(){copy(expected,observed)});sub.Cleanup(func(){copy(expected,saved)})})', ''),
    ('order_parent_restore_after_child_write', 'PASS', 'saved:=append([]int(nil),expected...);t.Cleanup(func(){CHECK});t.Run("child",func(sub *testing.T){t.Cleanup(func(){copy(expected,saved)});sub.Cleanup(func(){copy(expected,observed)})})', ''),
    ('defer_parent_registration', 'FAIL', 't.Run("child",func(sub *testing.T){defer func(){t.Cleanup(func(){CHECK})}()});copy(expected,observed)', ''),
    ('defer_child_registration', 'PASS', 't.Run("child",func(sub *testing.T){defer func(){sub.Cleanup(func(){CHECK})}()});copy(expected,observed)', ''),
    ('order_repeated_subtest_site', 'PASS', 'for i:=0;i<2;i++{t.Run("child",func(sub *testing.T){sub.Cleanup(func(){CALL(expected,observed,sub)})})};copy(expected,observed)', ''),
]


class CleanupOwnerRegressions(unittest.TestCase):
    def test_parent_and_child_boundaries(self):
        CompleteRepairRegressions.check_group(self, ("root_",), CLEANUP_OWNER_CASES)

    def test_aliases_and_stored_arguments(self):
        CompleteRepairRegressions.check_group(self, ("alias_", "stored_", "field_"), CLEANUP_OWNER_CASES)

    def test_nested_subtests(self):
        CompleteRepairRegressions.check_group(self, ("nested_",), CLEANUP_OWNER_CASES)

    def test_per_owner_lifo_and_defers(self):
        CompleteRepairRegressions.check_group(self, ("order_", "defer_"), CLEANUP_OWNER_CASES)


STORED_TEST_CALLBACK_CASES = [
    ('value_cleanup_check', 'PASS', 'f:=func(){CHECK};t.Cleanup(f)', ''),
    ('value_cleanup_alias', 'PASS', 'f:=func(){CHECK};g:=f;t.Cleanup(g)', ''),
    ('value_cleanup_parentheses', 'PASS', 'f:=(func(){CHECK});t.Cleanup((f))', ''),
    ('value_cleanup_declared', 'PASS', 'var f func();f=func(){CHECK};t.Cleanup(f)', ''),
    ('value_cleanup_method_alias', 'PASS', 'cleanup:=t.Cleanup;f:=func(){CHECK};cleanup(f)', ''),
    ('value_run_check', 'PASS', 'f:=func(sub *testing.T){CALL(expected,observed,sub)};t.Run("probe",f)', ''),
    ('value_run_alias', 'PASS', 'f:=func(sub *testing.T){CALL(expected,observed,sub)};g:=f;run:=t.Run;run("probe",g)', ''),
    ('order_cleanup_write_before', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};t.Cleanup(f)', ''),
    ('order_cleanup_write_after', 'PASS', 'f:=func(){copy(expected,observed)};t.Cleanup(f);t.Cleanup(func(){CHECK})', ''),
    ('order_run_write', 'FAIL', 'f:=func(sub *testing.T){copy(expected,observed)};t.Run("probe",f);CHECK', ''),
    ('order_unused_write', 'PASS', 'f:=func(){copy(expected,observed)};_=f;CHECK', ''),
    ('order_unused_check', 'FAIL', 'f:=func(){CHECK};_=f', ''),
    ('order_restored_before_check', 'PASS', 'saved:=append([]int(nil),expected...);f:=func(){CHECK};t.Cleanup(f);copy(expected,observed);copy(expected,saved)', ''),
    ('order_late_write', 'FAIL', 'f:=func(){CHECK};t.Cleanup(f);copy(expected,observed)', ''),
    ('binding_selected_check', 'PASS', 'f:=func(){CHECK};t.Cleanup(f);f=func(){};_=f', ''),
    ('binding_selected_write', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};t.Cleanup(f);f=func(){};_=f', ''),
    ('binding_alias_retains_check', 'PASS', 'f:=func(){CHECK};g:=f;f=func(){};t.Cleanup(g)', ''),
    ('binding_replaced_check', 'FAIL', 'f:=func(){CHECK};f=func(){};t.Cleanup(f)', ''),
    ('binding_replaced_write', 'PASS', 'f:=func(){copy(expected,observed)};f=func(){};t.Cleanup(f);CHECK', ''),
    ('capture_live_rebound', 'FAIL', 'v:=expected;f:=func(){CALL(v,observed,t)};t.Cleanup(f);v=observed', ''),
    ('capture_live_restored', 'PASS', 'v:=expected;f:=func(){CALL(v,observed,t)};t.Cleanup(f);v=observed;v=expected', ''),
    ('capture_shadow_at_registration', 'PASS', 'f:=func(){CHECK};{expected:=observed;_=expected;t.Cleanup(f)}', ''),
    ('capture_scope_escaped', 'PASS', 'var f func();{v:=expected;f=func(){CALL(v,observed,t)}};t.Cleanup(f)', ''),
    ('capture_scope_escaped_write', 'FAIL', 'var f func();{v:=expected;f=func(){CALL(v,observed,t)};v=observed};t.Cleanup(f)', ''),
    ('capture_scope_escaped_restored', 'PASS', 'var f func();{v:=expected;f=func(){CALL(v,observed,t)};v=observed;v=expected};t.Cleanup(f)', ''),
    ('capture_scope_optional_write', 'FAIL', 'var f func();{v:=expected;f=func(){CALL(v,observed,t)};if len(observed)>0{v=observed}};t.Cleanup(f)', ''),
    ('capture_scope_optional_noop', 'PASS', 'var f func();{v:=expected;f=func(){CALL(v,observed,t)};if len(observed)>0{v=expected}};t.Cleanup(f)', ''),
    ('capture_scope_sibling_write', 'FAIL', 'var f,g func();{v:=expected;f=func(){CALL(v,observed,t)};g=func(){v=observed}};t.Cleanup(f);t.Cleanup(g)', ''),
    ('capture_scope_sibling_after', 'PASS', 'var f,g func();{v:=expected;f=func(){CALL(v,observed,t)};g=func(){v=observed}};t.Cleanup(g);t.Cleanup(f)', ''),
    ('capture_shadow_after_definition', 'PASS', 'v:=expected;f:=func(){CALL(v,observed,t)};{v:=observed;_=v};t.Cleanup(f)', ''),
    ('owner_parent_from_child', 'FAIL', 'f:=func(){CHECK};t.Run("probe",func(sub *testing.T){t.Cleanup(f)});copy(expected,observed)', ''),
    ('owner_child_before_parent_write', 'PASS', 'f:=func(){CHECK};t.Run("probe",func(sub *testing.T){sub.Cleanup(f)});copy(expected,observed)', ''),
    ('owner_stored_run_parent', 'FAIL', 'f:=func(sub *testing.T){t.Cleanup(func(){CHECK})};t.Run("probe",f);copy(expected,observed)', ''),
    ('owner_stored_run_child', 'PASS', 'f:=func(sub *testing.T){sub.Cleanup(func(){CALL(expected,observed,sub)})};t.Run("probe",f);copy(expected,observed)', ''),
    ('owner_nested_stored', 'PASS', 'f:=func(sub *testing.T){g:=func(){CALL(expected,observed,sub)};sub.Cleanup(g)};t.Run("probe",f);copy(expected,observed)', ''),
    ('owner_child_method_alias', 'PASS', 't.Run("probe",func(sub *testing.T){cleanup:=sub.Cleanup;f:=func(){CALL(expected,observed,sub)};cleanup(f)});copy(expected,observed)', ''),
    ('reach_guarded_registration', 'FAIL', 'f:=func(){CHECK};if len(observed)>0{t.Cleanup(f)}', ''),
    ('reach_dead_registration', 'FAIL', 'f:=func(){CHECK};if false{t.Cleanup(f)}', ''),
    ('reach_guarded_write', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};if len(observed)>0{t.Cleanup(f)}', ''),
    ('reach_dead_write', 'PASS', 't.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};if false{t.Cleanup(f)}', ''),
    ('reach_return_before_registration', 'FAIL', 'f:=func(){CHECK};return;t.Cleanup(f)', ''),
    ('reach_register_inside_cleanup', 'PASS', 'f:=func(){CHECK};t.Cleanup(func(){t.Cleanup(f)})', ''),
    ('defer_cleanup_check', 'PASS', 'f:=func(){CHECK};defer t.Cleanup(f)', ''),
    ('defer_cleanup_write_before', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};defer t.Cleanup(f)', ''),
    ('defer_selected_function_value', 'PASS', 'f:=func(){CHECK};defer t.Cleanup(f);f=func(){};_=f', ''),
    ('defer_run_check', 'PASS', 'f:=func(sub *testing.T){CALL(expected,observed,sub)};defer t.Run("probe",f)', ''),
    ('defer_run_write_before_cleanup', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(sub *testing.T){copy(expected,observed)};defer t.Run("probe",f)', ''),
    ('defer_parent_owner', 'FAIL', 'f:=func(){CHECK};t.Run("probe",func(sub *testing.T){defer t.Cleanup(f)});copy(expected,observed)', ''),
    ('defer_child_owner', 'PASS', 'f:=func(){CHECK};t.Run("probe",func(sub *testing.T){defer sub.Cleanup(f)});copy(expected,observed)', ''),
    ('defer_literal_scope_write', 'FAIL', '{v:=expected;defer t.Cleanup(func(){CALL(v,observed,t)});v=observed}', ''),
    ('defer_selected_receiver', 'PASS', 'owner:=t;f:=func(){CHECK};defer owner.Cleanup(f);owner=new(testing.T);_=owner', ''),
    ('defer_after_deferred_write', 'FAIL', 'f:=func(){CHECK};defer t.Cleanup(f);defer copy(expected,observed)', ''),
    ('defer_write_after_ordinary_check', 'PASS', 'f:=func(){copy(expected,observed)};defer t.Cleanup(f);CHECK', ''),
    ('defer_replaced_check', 'FAIL', 'f:=func(){CHECK};f=func(){};defer t.Cleanup(f)', ''),
]


class StoredTestCallbackRegressions(unittest.TestCase):
    def test_function_values_and_aliases(self):
        CompleteRepairRegressions.check_group(self, ('value_',), STORED_TEST_CALLBACK_CASES)

    def test_execution_order_and_unused_closures(self):
        CompleteRepairRegressions.check_group(self, ('order_',), STORED_TEST_CALLBACK_CASES)

    def test_callback_selection_and_rebinding(self):
        CompleteRepairRegressions.check_group(self, ('binding_',), STORED_TEST_CALLBACK_CASES)

    def test_lexical_captures(self):
        CompleteRepairRegressions.check_group(self, ('capture_',), STORED_TEST_CALLBACK_CASES)

    def test_parent_and_child_ownership(self):
        CompleteRepairRegressions.check_group(self, ('owner_',), STORED_TEST_CALLBACK_CASES)

    def test_registration_reachability(self):
        CompleteRepairRegressions.check_group(self, ('reach_',), STORED_TEST_CALLBACK_CASES)

    def test_deferred_registration(self):
        CompleteRepairRegressions.check_group(self, ('defer_',), STORED_TEST_CALLBACK_CASES)


BRANCH_TEST_CALLBACK_CASES = [
    ('check_cleanup_both', 'PASS', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};t.Cleanup(f)', ''),
    ('check_run_both', 'PASS', 'f:=func(sub *testing.T){CALL(expected,observed,sub)};if len(observed)>0{f=func(sub *testing.T){CALL(expected,observed,sub)}};t.Run("probe",f)', ''),
    ('check_cleanup_missing', 'FAIL', 'f:=func(){CHECK};if len(observed)>0{f=func(){}};t.Cleanup(f)', ''),
    ('check_run_missing', 'FAIL', 'f:=func(sub *testing.T){CALL(expected,observed,sub)};if len(observed)>0{f=func(sub *testing.T){}};t.Run("probe",f)', ''),
    ('check_cleanup_missing_initial', 'FAIL', 'f:=func(){};if len(observed)>0{f=func(){CHECK}};t.Cleanup(f)', ''),
    ('check_run_missing_initial', 'FAIL', 'f:=func(sub *testing.T){};if len(observed)>0{f=func(sub *testing.T){CALL(expected,observed,sub)}};t.Run("probe",f)', ''),
    ('check_cleanup_three', 'PASS', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}}else if len(observed)==0{f=func(){CHECK}};t.Cleanup(f)', ''),
    ('check_cleanup_three_missing', 'FAIL', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}}else if len(observed)==0{f=func(){}};t.Cleanup(f)', ''),
    ('check_renamed_helpers', 'PASS', 'f:=func(){left(expected,observed,t)};if len(observed)>0{f=func(){right(expected,observed,t)}};t.Cleanup(f)', 'func left(v,a []int,t *testing.T){assert.Exactly(t,v,a)};func right(v,a []int,t *testing.T){assert.Exactly(t,v,a)}'),
    ('check_renamed_helper_missing', 'FAIL', 'f:=func(){left(expected,observed,t)};if len(observed)>0{f=func(){right(expected,observed,t)}};t.Cleanup(f)', 'func left(v,a []int,t *testing.T){assert.Exactly(t,v,a)};func right(v,a []int,t *testing.T){}'),
    ('value_alias_chain', 'PASS', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};g:=f;h:=g;t.Cleanup(h)', ''),
    ('value_selected_before_rebind', 'PASS', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};t.Cleanup(f);f=func(){};_=f', ''),
    ('value_selected_alias', 'PASS', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};g:=f;f=func(){};t.Cleanup(g)', ''),
    ('value_replaced_before_register', 'FAIL', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};f=func(){};t.Cleanup(f)', ''),
    ('value_method_alias', 'PASS', 'cleanup:=t.Cleanup;f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};cleanup(f)', ''),
    ('write_cleanup_both', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};if len(observed)>0{f=func(){copy(expected,observed)}};t.Cleanup(f)', ''),
    ('write_run_both', 'FAIL', 'f:=func(sub *testing.T){copy(expected,observed)};if len(observed)>0{f=func(sub *testing.T){copy(expected,observed)}};t.Run("probe",f);CHECK', ''),
    ('write_only_first', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};if len(observed)>0{f=func(){}};t.Cleanup(f)', ''),
    ('write_only_second', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(){};if len(observed)>0{f=func(){copy(expected,observed)}};t.Cleanup(f)', ''),
    ('write_alternative_restore', 'FAIL', 'saved:=append([]int(nil),expected...);t.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};if len(observed)>0{f=func(){copy(expected,saved)}};t.Cleanup(f)', ''),
    ('write_restore_alternative', 'FAIL', 'saved:=append([]int(nil),expected...);t.Cleanup(func(){CHECK});f:=func(){copy(expected,saved)};if len(observed)>0{f=func(){copy(expected,observed)}};t.Cleanup(f)', ''),
    ('order_write_after_check', 'PASS', 'f:=func(){copy(expected,observed)};if len(observed)>0{f=func(){copy(expected,observed)}};t.Cleanup(f);t.Cleanup(func(){CHECK})', ''),
    ('order_unused_callback', 'PASS', 'f:=func(){copy(expected,observed)};if len(observed)>0{f=func(){copy(expected,observed)}};_=f;CHECK', ''),
    ('order_restore_all', 'PASS', 'saved:=append([]int(nil),expected...);t.Cleanup(func(){CHECK});f:=func(){copy(expected,saved)};if len(observed)>0{f=func(){copy(expected,saved)}};t.Cleanup(f);copy(expected,observed)', ''),
    ('owner_parent_after_child', 'FAIL', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};t.Run("probe",func(sub *testing.T){t.Cleanup(f)});copy(expected,observed)', ''),
    ('owner_child_before_parent', 'PASS', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};t.Run("probe",func(sub *testing.T){sub.Cleanup(f)});copy(expected,observed)', ''),
    ('owner_run_child_cleanup', 'PASS', 'f:=func(sub *testing.T){sub.Cleanup(func(){CALL(expected,observed,sub)})};if len(observed)>0{f=func(sub *testing.T){sub.Cleanup(func(){CALL(expected,observed,sub)})}};t.Run("probe",f);copy(expected,observed)', ''),
    ('owner_run_parent_cleanup', 'FAIL', 'f:=func(sub *testing.T){t.Cleanup(func(){CHECK})};if len(observed)>0{f=func(sub *testing.T){t.Cleanup(func(){CHECK})}};t.Run("probe",f);copy(expected,observed)', ''),
    ('defer_cleanup_check', 'PASS', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};defer t.Cleanup(f)', ''),
    ('defer_cleanup_write', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};if len(observed)>0{f=func(){copy(expected,observed)}};defer t.Cleanup(f)', ''),
    ('defer_selected_value', 'PASS', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};defer t.Cleanup(f);f=func(){};_=f', ''),
    ('defer_run_check', 'PASS', 'f:=func(sub *testing.T){CALL(expected,observed,sub)};if len(observed)>0{f=func(sub *testing.T){CALL(expected,observed,sub)}};defer t.Run("probe",f)', ''),
    ('defer_run_write', 'FAIL', 't.Cleanup(func(){CHECK});f:=func(sub *testing.T){copy(expected,observed)};if len(observed)>0{f=func(sub *testing.T){copy(expected,observed)}};defer t.Run("probe",f)', ''),
    ('capture_late_rebind', 'FAIL', 'v:=expected;f:=func(){CALL(v,observed,t)};if len(observed)>0{f=func(){CALL(v,observed,t)}};t.Cleanup(f);v=observed', ''),
    ('capture_late_restore', 'PASS', 'v:=expected;f:=func(){CALL(v,observed,t)};if len(observed)>0{f=func(){CALL(v,observed,t)}};t.Cleanup(f);v=observed;v=expected', ''),
    ('capture_escaped_scope', 'PASS', 'var f func();{v:=expected;f=func(){CALL(v,observed,t)};if len(observed)>0{f=func(){CALL(v,observed,t)}}};t.Cleanup(f)', ''),
    ('capture_escaped_write', 'FAIL', 'var f func();{v:=expected;f=func(){CALL(v,observed,t)};if len(observed)>0{f=func(){CALL(v,observed,t)}};v=observed};t.Cleanup(f)', ''),
    ('capture_deferred_scope_write', 'FAIL', '{v:=expected;f:=func(){CALL(v,observed,t)};if len(observed)>0{f=func(){CALL(v,observed,t)}};defer t.Cleanup(f);v=observed}', ''),
    ('capture_deferred_scope_restore', 'PASS', '{v:=expected;f:=func(){CALL(v,observed,t)};if len(observed)>0{f=func(){CALL(v,observed,t)}};defer t.Cleanup(f);v=observed;v=expected}', ''),
    ('reach_optional_register', 'FAIL', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};if len(observed)>1{t.Cleanup(f)}', ''),
    ('reach_dead_write', 'PASS', 't.Cleanup(func(){CHECK});f:=func(){copy(expected,observed)};if false{f=func(){copy(expected,observed)};t.Cleanup(f)}', ''),
    ('reach_nested_registration', 'PASS', 'f:=func(){CHECK};if len(observed)>0{f=func(){CHECK}};t.Cleanup(func(){t.Cleanup(f)})', ''),
    ('recursive_possible_missing_check', 'FAIL', 'var f func(*testing.T);f=func(sub *testing.T){sub.Run("loop",f)};if len(observed)>0{f=func(sub *testing.T){CALL(expected,observed,sub)}};t.Run("root",f)', ''),
    ('recursive_unselected_definition', 'PASS', 'var f func(*testing.T);f=func(sub *testing.T){sub.Run("loop",f)};f=func(sub *testing.T){CALL(expected,observed,sub)};if len(observed)>0{f=func(sub *testing.T){CALL(expected,observed,sub)}};t.Run("root",f)', ''),
]


class BranchTestCallbackRegressions(unittest.TestCase):
    def test_checks_and_helpers(self):
        CompleteRepairRegressions.check_group(self, ('check_',), BRANCH_TEST_CALLBACK_CASES)

    def test_function_value_selection(self):
        CompleteRepairRegressions.check_group(self, ('value_',), BRANCH_TEST_CALLBACK_CASES)

    def test_alternative_effects(self):
        CompleteRepairRegressions.check_group(self, ('write_', 'order_'), BRANCH_TEST_CALLBACK_CASES)

    def test_testing_ownership(self):
        CompleteRepairRegressions.check_group(self, ('owner_',), BRANCH_TEST_CALLBACK_CASES)

    def test_deferred_registration(self):
        CompleteRepairRegressions.check_group(self, ('defer_',), BRANCH_TEST_CALLBACK_CASES)

    def test_captured_scopes(self):
        CompleteRepairRegressions.check_group(self, ('capture_',), BRANCH_TEST_CALLBACK_CASES)

    def test_reachability(self):
        CompleteRepairRegressions.check_group(self, ('reach_',), BRANCH_TEST_CALLBACK_CASES)

    def test_recursive_callbacks_terminate_analysis(self):
        CompleteRepairRegressions.check_group(self, ('recursive_',), BRANCH_TEST_CALLBACK_CASES)

    def test_existing_alternative_baseline_keeps_identical_behavior(self):
        case = RegradeRegressions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        before = ('package mime\nfunc TestA(t *testing.T){f:=func(){assert.Exactly(t,want,actual)};'
                  'if len(actual)>0{f=func(){assert.Exactly(t,other,second)}};t.Cleanup(f)}')
        for expected, after in (
                ('PASS', before + '\n// Preserve the same checks and callback selection.\n'),
                ('FAIL', before.replace('other,second', 'other,actual'))):
            with self.subTest(expected=expected):
                integrity, skip = case.integrity(before, after)
                self.assertEqual(integrity['status'], expected)
                self.assertEqual(skip['status'], 'PASS')


    def test_baseline_checks_cannot_be_distributed_between_alternatives(self):
        case = RegradeRegressions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        before = ('package mime\nfunc TestA(t *testing.T){'
                  'assert.Exactly(t,want,actual);assert.Exactly(t,other,second)}')
        for expected, left, right in (
                ('FAIL', 'assert.Exactly(t,want,actual)', 'assert.Exactly(t,other,second)'),
                ('FAIL', 'assert.Exactly(t,want,actual);assert.Exactly(t,other,second)', ''),
                ('PASS', 'assert.Exactly(t,want,actual);assert.Exactly(t,other,second)',
                         'assert.Exactly(t,other,second);assert.Exactly(t,want,actual)')):
            after = ('package mime\nfunc TestA(t *testing.T){f:=func(){' + left +
                     '};if len(actual)>0{f=func(){' + right + '}};t.Cleanup(f)}')
            with self.subTest(expected=expected, left=left, right=right):
                integrity, skip = case.integrity(before, after)
                self.assertEqual(integrity['status'], expected)
                self.assertEqual(skip['status'], 'PASS')


RESOLVED_TEST_CALLBACK_CASES = [
    ('correlation_cleanup_restore', 'PASS', 'saved:=append([]int(nil),expected...);f:=func(){CHECK};if len(observed)>0{copy(expected,observed);f=func(){copy(expected,saved);CHECK}};t.Cleanup(f)', ''),
    ('correlation_run_restore', 'PASS', 'saved:=append([]int(nil),expected...);f:=func(sub *testing.T){CALL(expected,observed,sub)};if len(observed)>0{copy(expected,observed);f=func(sub *testing.T){copy(expected,saved);CALL(expected,observed,sub)}};t.Run("probe",f)', ''),
    ('correlation_missing_restore', 'FAIL', 'saved:=append([]int(nil),expected...);f:=func(){copy(expected,saved);CHECK};if len(observed)>0{copy(expected,observed);f=func(){CHECK}};t.Cleanup(f)', ''),
    ('correlation_late_write', 'FAIL', 'saved:=append([]int(nil),expected...);f:=func(){CHECK};if len(observed)>0{copy(expected,observed);f=func(){copy(expected,saved);CHECK}};copy(expected,observed);t.Cleanup(f)', ''),
    ('correlation_late_restore', 'PASS', 'saved:=append([]int(nil),expected...);f:=func(){CHECK};if len(observed)>0{copy(expected,observed);f=func(){copy(expected,saved);CHECK}};copy(expected,saved);t.Cleanup(f)', ''),
    ('correlation_independent_branch', 'FAIL', 'saved:=append([]int(nil),expected...);f:=func(){CHECK};if len(observed)>0{copy(expected,observed);f=func(){copy(expected,saved);CHECK}};if otherCondition{copy(expected,observed)};t.Cleanup(f)', ''),
    ('correlation_alias_selection', 'PASS', 'saved:=append([]int(nil),expected...);f:=func(){CHECK};if len(observed)>0{copy(expected,observed);f=func(){copy(expected,saved);CHECK}};g:=f;f=func(){copy(expected,observed)};t.Cleanup(g)', ''),
    ('correlation_deferred_cleanup', 'PASS', 'saved:=append([]int(nil),expected...);f:=func(){CHECK};if len(observed)>0{copy(expected,observed);f=func(){copy(expected,saved);CHECK}};defer t.Cleanup(f)', ''),
    ('correlation_conditional_registration', 'PASS', 'saved:=append([]int(nil),expected...);if len(observed)>0{copy(expected,observed);t.Cleanup(func(){copy(expected,saved);CHECK})}else{t.Cleanup(func(){CHECK})}', ''),
    ('correlation_same_choices_independent_write', 'FAIL', 'saved:=append([]int(nil),expected...);f:=func(){CHECK};if firstCondition{copy(expected,observed);f=func(){copy(expected,saved);CHECK}};if secondCondition{copy(expected,observed)};t.Cleanup(f)', ''),
    ('correlation_conditional_missing_restore', 'FAIL', 'if len(observed)>0{copy(expected,observed);t.Cleanup(func(){CHECK})}else{t.Cleanup(func(){CHECK})}', ''),
    ('return_run_check', 'PASS', 'factory:=func()func(*testing.T){return func(sub *testing.T){CALL(expected,observed,sub)}};t.Run("probe",factory())', ''),
    ('return_run_write', 'FAIL', 'factory:=func()func(*testing.T){return func(sub *testing.T){copy(expected,observed)}};t.Run("probe",factory());CHECK', ''),
    ('return_cleanup_check', 'PASS', 'factory:=func()func(){return func(){CHECK}};t.Cleanup(factory())', ''),
    ('return_cleanup_write', 'FAIL', 't.Cleanup(func(){CHECK});factory:=func()func(){return func(){copy(expected,observed)}};t.Cleanup(factory())', ''),
    ('return_late_capture_write', 'FAIL', 'v:=expected;factory:=func()func(){return func(){CALL(v,observed,t)}};t.Cleanup(factory());v=observed', ''),
    ('return_late_capture_restore', 'PASS', 'v:=expected;factory:=func()func(){return func(){CALL(v,observed,t)}};t.Cleanup(factory());v=observed;v=expected', ''),
    ('return_independent_calls', 'PASS', 'factory:=func(v []int)func(){return func(){CALL(v,observed,t)}};first:=factory(expected);second:=factory(observed);_=second;t.Cleanup(first)', ''),
    ('return_second_call_weakened', 'FAIL', 'factory:=func(v []int)func(){return func(){CALL(v,observed,t)}};first:=factory(expected);second:=factory(observed);_=first;t.Cleanup(second)', ''),
    ('return_rebound_parameter', 'FAIL', 'factory:=func(v []int)func(){f:=func(){CALL(v,observed,t)};v=observed;return f};t.Cleanup(factory(expected))', ''),
    ('return_nested_factory', 'PASS', 'factory:=func()func()func(){v:=expected;return func()func(){return func(){CALL(v,observed,t)}}};next:=factory();t.Cleanup(next())', ''),
    ('return_nested_factory_write', 'FAIL', 'factory:=func()func()func(){v:=expected;return func()func(){v=observed;return func(){CALL(v,observed,t)}}};next:=factory();t.Cleanup(next())', ''),
    ('return_branch_both_check', 'PASS', 'factory:=func()func(){if len(observed)>0{return func(){CHECK}};return func(){CHECK}};t.Cleanup(factory())', ''),
    ('return_branch_missing_check', 'FAIL', 'factory:=func()func(){if len(observed)>0{return func(){CHECK}};return func(){}};t.Cleanup(factory())', ''),
    ('return_branch_write', 'FAIL', 'factory:=func()func(){if len(observed)>0{return func(){copy(expected,observed)}};return func(){}};t.Run("probe",func(sub *testing.T){factory()()});CHECK', ''),
    ('return_tuple_shared_cells', 'FAIL', 'factory:=func()(func(),func()){v:=expected;return func(){CALL(v,observed,t)},func(){v=observed}};check,write:=factory();write();t.Cleanup(check)', ''),
    ('return_deferred_selection', 'PASS', 'factory:=func()func(){return func(){CHECK}};defer t.Cleanup(factory());factory=func()func(){return func(){}}', ''),
    ('return_named_factory_check', 'PASS', 't.Run("probe",makeCallback(expected,observed))', 'func makeCallback(v,a []int)func(*testing.T){return func(sub *testing.T){check(v,a,sub)}}'),
    ('return_named_factory_write', 'FAIL', 't.Run("probe",makeCallback(expected,observed));CHECK', 'func makeCallback(v,a []int)func(*testing.T){return func(sub *testing.T){copy(v,a)}}'),
    ('return_named_factory_alias', 'PASS', 'factory:=makeCallback;t.Run("probe",factory(expected,observed))', 'func makeCallback(v,a []int)func(*testing.T){return func(sub *testing.T){check(v,a,sub)}}'),
    ('return_named_factory_cannot_capture_caller', 'FAIL', 't.Run("probe",makeCallback())', 'func makeCallback()func(*testing.T){return func(sub *testing.T){check(expected,observed,sub)}}'),
    ('return_deferred_check', 'PASS', 'factory:=func()func(){return func(){CHECK}};f:=factory();defer f()', ''),
    ('return_deferred_write', 'FAIL', 't.Cleanup(func(){CHECK});factory:=func()func(){return func(){copy(expected,observed)}};f:=factory();defer f()', ''),
    ('return_correlated_cleanup', 'PASS', 'saved:=append([]int(nil),expected...);factory:=func()func(){if len(observed)>0{copy(expected,observed);return func(){copy(expected,saved);CHECK}};return func(){CHECK}};t.Cleanup(factory())', ''),
    ('return_correlated_stored', 'PASS', 'saved:=append([]int(nil),expected...);factory:=func()func(){if len(observed)>0{copy(expected,observed);return func(){copy(expected,saved);CHECK}};return func(){CHECK}};f:=factory();{g:=f;t.Cleanup(g)}', ''),
    ('return_correlated_missing_restore', 'FAIL', 'factory:=func()func(){if len(observed)>0{copy(expected,observed);return func(){CHECK}};return func(){CHECK}};f:=factory();t.Cleanup(f)', ''),
    ('aggregate_slice_run_check', 'PASS', 'callbacks:=[]func(*testing.T){func(sub *testing.T){CALL(expected,observed,sub)}};t.Run("probe",callbacks[0])', ''),
    ('aggregate_slice_run_write', 'FAIL', 'callbacks:=[]func(*testing.T){func(sub *testing.T){copy(expected,observed)}};t.Run("probe",callbacks[0]);CHECK', ''),
    ('aggregate_slice_cleanup_check', 'PASS', 'callbacks:=[]func(){func(){CHECK}};t.Cleanup(callbacks[0])', ''),
    ('aggregate_slice_cleanup_write', 'FAIL', 't.Cleanup(func(){CHECK});callbacks:=[]func(){func(){copy(expected,observed)}};t.Cleanup(callbacks[0])', ''),
    ('aggregate_map_check', 'PASS', 'callbacks:=map[string]func(){"a":func(){CHECK}};t.Cleanup(callbacks["a"])', ''),
    ('aggregate_map_write', 'FAIL', 't.Cleanup(func(){CHECK});callbacks:=map[string]func(){"a":func(){copy(expected,observed)}};t.Cleanup(callbacks["a"])', ''),
    ('aggregate_struct_check', 'PASS', 'box:=Callback{F:func(){CHECK}};t.Cleanup(box.F)', 'type Callback struct{F func()}'),
    ('aggregate_struct_write', 'FAIL', 't.Cleanup(func(){CHECK});box:=Callback{F:func(){copy(expected,observed)}};t.Cleanup(box.F)', 'type Callback struct{F func()}'),
    ('aggregate_array_check', 'PASS', 'callbacks:=[2]func(){func(){CHECK},func(){}};t.Cleanup(callbacks[0])', ''),
    ('aggregate_array_missing', 'FAIL', 'callbacks:=[2]func(){func(){CHECK},func(){}};t.Cleanup(callbacks[1])', ''),
    ('aggregate_unused_write', 'PASS', 'callbacks:=[]func(){func(){CHECK},func(){copy(expected,observed)}};t.Cleanup(callbacks[0])', ''),
    ('aggregate_selected_before_rebind', 'PASS', 'callbacks:=[]func(){func(){CHECK}};t.Cleanup(callbacks[0]);callbacks[0]=func(){}', ''),
    ('aggregate_write_selected_before_rebind', 'FAIL', 't.Cleanup(func(){CHECK});callbacks:=[]func(){func(){copy(expected,observed)}};t.Cleanup(callbacks[0]);callbacks[0]=func(){}', ''),
    ('aggregate_slice_alias_rebind', 'FAIL', 'callbacks:=[]func(){func(){CHECK}};alias:=callbacks;callbacks[0]=func(){};t.Cleanup(alias[0])', ''),
    ('aggregate_late_capture', 'FAIL', 'v:=expected;callbacks:=[]func(){func(){CALL(v,observed,t)}};t.Cleanup(callbacks[0]);v=observed', ''),
    ('aggregate_late_restore', 'PASS', 'v:=expected;callbacks:=[]func(){func(){CALL(v,observed,t)}};t.Cleanup(callbacks[0]);v=observed;v=expected', ''),
    ('aggregate_struct_copy', 'PASS', 'box:=Callback{F:func(){CHECK}};other:=box;box.F=func(){};t.Cleanup(other.F)', 'type Callback struct{F func()}'),
    ('aggregate_deferred_selection', 'PASS', 'callbacks:=[]func(){func(){CHECK}};defer t.Cleanup(callbacks[0]);callbacks[0]=func(){}', ''),
    ('aggregate_dynamic_index_all_check', 'PASS', 'callbacks:=[]func(){func(){CHECK},func(){CHECK}};index:=len(observed)%2;t.Cleanup(callbacks[index])', ''),
    ('aggregate_dynamic_index_missing', 'FAIL', 'callbacks:=[]func(){func(){CHECK},func(){}};index:=len(observed)%2;t.Cleanup(callbacks[index])', ''),
    ('aggregate_dynamic_index_write', 'FAIL', 't.Cleanup(func(){CHECK});callbacks:=[]func(){func(){copy(expected,observed)},func(){}};index:=len(observed)%2;t.Cleanup(callbacks[index])', ''),
    ('aggregate_dynamic_map_all_check', 'PASS', 'callbacks:=map[string]func(){"a":func(){CHECK},"b":func(){CHECK}};t.Cleanup(callbacks[key])', ''),
    ('aggregate_dynamic_map_write', 'FAIL', 't.Cleanup(func(){CHECK});callbacks:=map[string]func(){"a":func(){copy(expected,observed)},"b":func(){}};t.Cleanup(callbacks[key])', ''),
    ('aggregate_returned_container', 'PASS', 'factory:=func()[]func(){return []func(){func(){CHECK}}};t.Cleanup(factory()[0])', ''),
    ('aggregate_returned_container_write', 'FAIL', 't.Cleanup(func(){CHECK});factory:=func()[]func(){return []func(){func(){copy(expected,observed)}}};t.Cleanup(factory()[0])', ''),
]


class ResolvedTestCallbackRegressions(unittest.TestCase):
    def test_correlated_callback_state(self):
        CompleteRepairRegressions.check_group(self, ('correlation_',), RESOLVED_TEST_CALLBACK_CASES)

    def test_returned_callbacks_and_captured_cells(self):
        CompleteRepairRegressions.check_group(self, ('return_',), RESOLVED_TEST_CALLBACK_CASES)

    def test_callbacks_from_aggregate_values(self):
        CompleteRepairRegressions.check_group(self, ('aggregate_',), RESOLVED_TEST_CALLBACK_CASES)

    def test_disjoint_check_paths_keep_runtime_multiplicity(self):
        case = RegradeRegressions()
        case.setUp()
        self.addCleanup(case.doCleanups)
        call = 'assert.Exactly(t,want,actual)'
        source = 'package mime\nvar condition bool\nfunc TestA(t *testing.T){BODY}'
        for expected, count, replacement in (
                ('PASS', 1, 'if condition{CHECK}else{CHECK}'),
                ('FAIL', 2, 'if condition{CHECK}else{CHECK}'),
                ('PASS', 2, 'if condition{CHECK;CHECK}else{CHECK;CHECK}'),
                ('FAIL', 1, 'if condition{CHECK}'),
                ('FAIL', 2, 'if condition{CHECK;CHECK}else{CHECK}'),
                ('FAIL', 1, 'if condition{CHECK};condition=!condition;if !condition{CHECK}')):
            with self.subTest(expected=expected, count=count, replacement=replacement):
                before = source.replace('BODY', ';'.join([call] * count))
                after = source.replace('BODY', replacement.replace('CHECK', call))
                integrity, skip = case.integrity(before, after)
                self.assertEqual(integrity['status'], expected)
                self.assertEqual(skip['status'], 'PASS')


METHOD_FACTORY_CASES = [
    ('basic_value_check', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'return func(){check(m.V,m.A,m.T)}'),
    ('basic_value_write', 'FAIL', 'MethodMaker', 't.Cleanup(func(){CHECK});m:=MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'return func(){copy(m.V,m.A)}'),
    ('basic_pointer_check', 'PASS', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'return func(){check(m.V,m.A,m.T)}'),
    ('basic_pointer_write', 'FAIL', '*MethodMaker', 't.Cleanup(func(){CHECK});m:=&MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'return func(){copy(m.V,m.A)}'),
    ('basic_auto_address_check', 'PASS', '*MethodMaker', 'm:=MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'return func(){check(m.V,m.A,m.T)}'),
    ('basic_auto_address_write', 'FAIL', '*MethodMaker', 't.Cleanup(func(){CHECK});m:=MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'return func(){copy(m.V,m.A)}'),
    ('basic_stored_result', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback();g:=f;t.Cleanup(g)', 'return func(){check(m.V,m.A,m.T)}'),
    ('basic_returned_direct_write', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};m.Callback()();CHECK', 'return func(){copy(m.V,m.A)}'),
    ('basic_unused_return', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback();_=f;CHECK', 'return func(){copy(m.V,m.A)}'),
    ('basic_wrong_testing_object', 'FAIL', 'MethodMaker', 'm:=MethodMaker{expected,observed,new(testing.T)};t.Cleanup(m.Callback())', 'return func(){check(m.V,m.A,m.T)}'),
    ('basic_value_method_expression', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};t.Cleanup(MethodMaker.Callback(m))', 'return func(){check(m.V,m.A,m.T)}'),
    ('basic_pointer_method_expression', 'PASS', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};t.Cleanup((*MethodMaker).Callback(m))', 'return func(){check(m.V,m.A,m.T)}'),
    ('basic_value_expression_write', 'FAIL', 'MethodMaker', 't.Cleanup(func(){CHECK});m:=MethodMaker{expected,observed,t};t.Cleanup(MethodMaker.Callback(m))', 'return func(){copy(m.V,m.A)}'),
    ('basic_pointer_expression_write', 'FAIL', '*MethodMaker', 't.Cleanup(func(){CHECK});m:=&MethodMaker{expected,observed,t};t.Cleanup((*MethodMaker).Callback(m))', 'return func(){copy(m.V,m.A)}'),
    ('binding_value_alias', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback;m.V=observed;t.Cleanup(f())', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_pointer_alias', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};f:=m.Callback;m.V=observed;t.Cleanup(f())', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_value_alias_chain', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback;g:=f;m.V=observed;t.Cleanup(g())', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_pointer_alias_chain', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};f:=m.Callback;g:=f;m.V=observed;t.Cleanup(g())', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_value_from_pointer', 'PASS', 'MethodMaker', 'm:=&MethodMaker{expected,observed,t};f:=m.Callback;m.V=observed;t.Cleanup(f())', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_pointer_rebound_variable', 'PASS', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};f:=m.Callback;m=&MethodMaker{observed,observed,t};t.Cleanup(f())', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_value_selected_after_change', 'FAIL', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};m.V=observed;f:=m.Callback;t.Cleanup(f())', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_value_deferred', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};defer t.Cleanup(m.Callback());m.V=observed', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_pointer_deferred', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};defer t.Cleanup(m.Callback());m.V=observed', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_expression_alias', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=MethodMaker.Callback;t.Cleanup(f(m))', 'return func(){check(m.V,m.A,m.T)}'),
    ('binding_pointer_expression_alias', 'FAIL', '*MethodMaker', 't.Cleanup(func(){CHECK});m:=&MethodMaker{expected,observed,t};f:=(*MethodMaker).Callback;t.Cleanup(f(m))', 'return func(){copy(m.V,m.A)}'),
    ('capture_value_field_rebound', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback();m.V=observed;t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_pointer_field_rebound', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};f:=m.Callback();m.V=observed;t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_pointer_field_restored', 'PASS', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};f:=m.Callback();m.V=observed;m.V=expected;t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_value_shared_slice', 'FAIL', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback();copy(m.V,observed);t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_pointer_shared_slice', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};f:=m.Callback();copy(m.V,observed);t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_pointer_variable_rebound', 'PASS', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};f:=m.Callback();m=&MethodMaker{observed,observed,t};t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_value_variable_rebound', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback();m=MethodMaker{observed,observed,t};t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_auto_address_variable_rebound', 'FAIL', '*MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback();m=MethodMaker{observed,observed,t};t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_auto_address_bound_rebound', 'FAIL', '*MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback;m=MethodMaker{observed,observed,t};t.Cleanup(f())', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_auto_address_restored', 'PASS', '*MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback();m=MethodMaker{observed,observed,t};m=MethodMaker{expected,observed,t};t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_auto_address_independent', 'PASS', '*MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=m.Callback();other:=MethodMaker{observed,observed,t};_=other;t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_address_expression_rebound', 'FAIL', '*MethodMaker', 'm:=MethodMaker{expected,observed,t};f:=(*MethodMaker).Callback(&m);m=MethodMaker{observed,observed,t};t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_address_pointer_alias_rebound', 'PASS', '*MethodMaker', 'm:=MethodMaker{expected,observed,t};p:=&m;f:=p.Callback();p=&MethodMaker{observed,observed,t};t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_address_pointer_alias_value_write', 'FAIL', '*MethodMaker', 'm:=MethodMaker{expected,observed,t};p:=&m;f:=p.Callback();m=MethodMaker{observed,observed,t};t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_value_independent_calls', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};first:=m.Callback();m.V=observed;second:=m.Callback();_=second;t.Cleanup(first)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_value_second_call_weakened', 'FAIL', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};first:=m.Callback();m.V=observed;second:=m.Callback();_=first;t.Cleanup(second)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_local_receiver_rebound', 'FAIL', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'f:=func(){check(m.V,m.A,m.T)};m.V=m.A;return f'),
    ('capture_local_slice_descriptor', 'PASS', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};f:=m.Callback();m.V=observed;t.Cleanup(f)', 'v:=m.V;return func(){check(v,m.A,m.T)}'),
    ('capture_pointer_holder_rebound', 'PASS', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};holder:=struct{M *MethodMaker}{m};f:=holder.M.Callback();holder.M=&MethodMaker{observed,observed,t};t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('capture_pointer_holder_shared_write', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};holder:=struct{M *MethodMaker}{m};f:=holder.M.Callback();holder.M.V=observed;t.Cleanup(f)', 'return func(){check(m.V,m.A,m.T)}'),
    ('control_branch_both_check', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'if len(m.A)>0{return func(){check(m.V,m.A,m.T)}};return func(){check(m.V,m.A,m.T)}'),
    ('control_branch_missing_check', 'FAIL', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'if len(m.A)>0{return func(){check(m.V,m.A,m.T)}};return func(){}'),
    ('control_branch_write', 'FAIL', '*MethodMaker', 't.Cleanup(func(){CHECK});m:=&MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'if len(m.A)>0{return func(){copy(m.V,m.A)}};return func(){}'),
    ('control_correlated_restoration', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'saved:=append([]int(nil),m.V...);if len(m.A)>0{copy(m.V,m.A);return func(){copy(m.V,saved);check(m.V,m.A,m.T)}};return func(){check(m.V,m.A,m.T)}'),
    ('control_correlated_missing_restoration', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'if len(m.A)>0{copy(m.V,m.A);return func(){check(m.V,m.A,m.T)}};return func(){check(m.V,m.A,m.T)}'),
    ('control_parent_cleanup_late_write', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};t.Run("probe",func(sub *testing.T){t.Cleanup(m.Callback())});copy(expected,observed)', 'return func(){check(m.V,m.A,m.T)}'),
    ('control_child_cleanup_before_write', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};t.Run("probe",func(sub *testing.T){sub.Cleanup(m.Callback())});copy(expected,observed)', 'return func(){check(m.V,m.A,m.T)}'),
    ('control_deferred_returned_write', 'FAIL', 'MethodMaker', 't.Cleanup(func(){CHECK});m:=MethodMaker{expected,observed,t};f:=m.Callback();defer f()', 'return func(){copy(m.V,m.A)}'),
    ('control_deferred_value_factory_expression', 'PASS', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};defer MethodMaker.Callback(m);m.V=observed', 'check(m.V,m.A,m.T);return func(){}'),
    ('control_deferred_value_factory_shared_write', 'FAIL', 'MethodMaker', 'm:=MethodMaker{expected,observed,t};defer MethodMaker.Callback(m);copy(expected,observed)', 'check(m.V,m.A,m.T);return func(){}'),
    ('control_deferred_pointer_factory_expression', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};defer (*MethodMaker).Callback(m);m.V=observed', 'check(m.V,m.A,m.T);return func(){}'),
    ('control_deferred_pointer_factory_rebound', 'PASS', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};defer (*MethodMaker).Callback(m);m=&MethodMaker{observed,observed,t}', 'check(m.V,m.A,m.T);return func(){}'),
    ('control_recursive_missing_check', 'FAIL', '*MethodMaker', 'm:=&MethodMaker{expected,observed,t};t.Cleanup(m.Callback())', 'return m.Callback()'),
]


class MethodFactoryRegressions(unittest.TestCase):
    def check_cases(self, prefix):
        cases = [(name, expected, template,
                  'type MethodMaker struct{V,A []int;T *testing.T};func(m ' + kind + ')Callback()func(){' + body + '}')
                 for name, expected, kind, template, body in METHOD_FACTORY_CASES]
        CompleteRepairRegressions.check_group(self, (prefix,), cases)

    def test_direct_method_factories(self):
        self.check_cases('basic_')

    def test_bound_method_selection(self):
        self.check_cases('binding_')

    def test_receiver_capture_and_rebinding(self):
        self.check_cases('capture_')

    def test_execution_paths_and_testing_lifetime(self):
        self.check_cases('control_')

    def test_factories_returning_run_callbacks(self):
        cases = []
        for kind, initial in (('MethodMaker', 'MethodMaker'), ('*MethodMaker', '&MethodMaker')):
            for write in (False, True):
                body = 'copy(m.V,m.A)' if write else 'check(m.V,m.A,sub)'
                template = 'm:=' + initial + '{expected,observed};t.Run("probe",m.Callback())' + (';CHECK' if write else '')
                declaration = ('type MethodMaker struct{V,A []int};func(m ' + kind + ')Callback()func(*testing.T){'
                               'return func(sub *testing.T){' + body + '}}')
                cases.append(('run_' + kind + str(write), 'FAIL' if write else 'PASS', template, declaration))
        CompleteRepairRegressions.check_group(self, ('run_',), cases)

    def test_method_parameters_and_multiple_results(self):
        cases = []
        for kind, initial, method in (('MethodMaker', 'MethodMaker', 'MethodMaker.Callback'),
                                      ('*MethodMaker', '&MethodMaker', '(*MethodMaker).Callback')):
            for expression in (False, True):
                for write in (False, True):
                    body = 'copy(v,a)' if write else 'check(v,a,target)'
                    call = method + '(m,expected,observed,t)' if expression else 'm.Callback(expected,observed,t)'
                    template = ('t.Cleanup(func(){CHECK});' if write else '') + 'm:=' + initial + '{};t.Cleanup(' + call + ')'
                    declaration = ('type MethodMaker struct{};func(m ' + kind + ')Callback(v,a []int,target *testing.T)func(){'
                                   'return func(){' + body + '}}')
                    cases.append(('parameters_' + kind + str(expression) + str(write),
                                  'FAIL' if write else 'PASS', template, declaration))
        for name, expected, result, body, selection in (
                ('named_result', 'PASS', '(f func())', 'f=func(){check(m.V,m.A,m.T)};return', 'f:=m.Callback();t.Cleanup(f)'),
                ('tuple_shared_receiver', 'FAIL', '(func(),func())', 'return func(){check(m.V,m.A,m.T)},func(){m.V=m.A}', 'f,write:=m.Callback();write();t.Cleanup(f)'),
                ('tuple_unused_writer', 'PASS', '(func(),func())', 'return func(){check(m.V,m.A,m.T)},func(){m.V=m.A}', 'f,write:=m.Callback();_=write;t.Cleanup(f)')):
            declaration = ('type MethodMaker struct{V,A []int;T *testing.T};func(m MethodMaker)Callback()' + result + '{' + body + '}')
            cases.append(('parameters_' + name, expected, 'm:=MethodMaker{expected,observed,t};' + selection, declaration))
        CompleteRepairRegressions.check_group(self, ('parameters_',), cases)

