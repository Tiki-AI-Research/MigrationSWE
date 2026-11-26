"""Two-sided regressions for MIME mechanism evidence, without running Go code."""

import json
import tempfile
import unittest
from pathlib import Path

from verifier.evidence import _target_delegated, _target_reimplemented, build_hardgate_evidence
from verifier.hardgate_engine import COMPOUND_ATOMS, evaluate_hardgate


POLICY = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())["policy"]
TARGET = 'github.com/emersion/go-message'


class MIMEMechanismChecks(unittest.TestCase):
    def inspect(self, source, extra=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mime").mkdir()
            (root / "mime/mime.go").write_text(source)
            for name, text in (extra or {}).items():
                (root / "mime" / name).write_text(text)
            return (_target_delegated(root, POLICY), _target_reimplemented(root, POLICY))

    def test_legitimate_import_forms_and_function_aliases(self):
        cases = [
            f'import "{TARGET}"\nfunc f() {{ _, _ = message.Read(nil) }}',
            f'import gm "{TARGET}"\nfunc f() {{ _, _ = gm /* API */ . Read (nil) }}',
            f'import . "{TARGET}"\nfunc f() {{ _, _ = Read(nil) }}',
            f'import gm `{TARGET}`\nfunc f() {{ _, _ = gm.Read(nil) }}',
            'import gm "github.com/emersion/go-\\x6dessage"\nfunc f() { _, _ = gm.Read(nil) }',
            f'import gm "{TARGET}"\nvar parse = gm.Read\nfunc f() {{ _, _ = parse(nil) }}',
            f'import gm "{TARGET}"\nfunc f() {{ parse := gm.Read; again := parse; _, _ = again(nil) }}',
            f'import tp "{TARGET}/textproto"\nfunc f() {{ _ = tp.NewMultipartReader(nil, "b") }}',
            f'import m "{TARGET}/mail"\nfunc f() {{ _, _ = m.CreateReader(nil) }}',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(self.inspect('package mime\n' + source), (True, False))

    def test_prose_references_and_wrong_imports_do_not_prove_delegation(self):
        cases = [
            f'var note = `import "{TARGET}"; message.Read(nil)`',
            f'// import "{TARGET}"\n// message.Read(nil)',
            f'import "{TARGET}"\nvar _ = message.Read',
            f'import "{TARGET}"\nvar _ *message.Entity',
            f'import _ "{TARGET}/charset"',
            'import message "example.org/go-message"\nfunc f() { _, _ = message.Read(nil) }',
            f'import gm "{TARGET}"\nfunc f() {{ _, _ = config.gm.Read(nil) }}',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(self.inspect('package mime\n' + source), (False, False))
        self.assertEqual(self.inspect('package mime\nfunc f() { gm.Read(nil) }', {
            'other.go': f'package mime\nimport gm "{TARGET}"\nvar _ = gm.Read',
            'mime_test.go': f'package mime\nimport "{TARGET}"\nfunc TestM() {{ message.Read(nil) }}',
            'notes.txt': f'import "{TARGET}"; message.Read(nil)',
        }), (False, False))

    def test_format_preflight_and_diagnostic_strings_are_allowed(self):
        checks = [
            'if _, err := mail.ReadMessage(nil); err != nil { return }',
            '_, _ = mail.ReadMessage(nil)',
            'mail.ReadMessage(nil)',
            'parse := mail.ReadMessage; _, err := parse(nil); _ = err',
            'parsed, err := mail.ReadMessage(nil); _ = parsed; _ = err',
            'defer mail.ReadMessage(nil)',
        ]
        for check in checks:
            with self.subTest(check=check):
                source = (f'package mime\nimport ("net/mail"; gm "{TARGET}")\n'
                          f'var diagnostic = "net/mail // mail.ReadMessage"\n'
                          f'func f() {{ {check}; _, _ = gm.Read(nil) }}')
                self.assertEqual(self.inspect(source), (True, False))
        self.assertEqual(self.inspect(f'package mime\nimport gm "{TARGET}"\n'
                                     'var s = "https://host/net/mail"\n'
                                     'func f() { gm.Read(nil) }'), (True, False))

    def test_baseline_envelope_reader_can_feed_go_message(self):
        source = (f'package mime\nimport ("net/mail"; "bytes"; "io"; gm "{TARGET}")\n'
                  'func f() { envelope, err := mail.ReadMessage(nil); _ = err; '
                  'body, err := io.ReadAll(envelope.Body); _ = err; '
                  'gm.New(gm.HeaderFromMap(envelope.Header), bytes.NewReader(body)) }')
        self.assertEqual(self.inspect(source), (True, False))
        # Consuming an envelope does not itself prove any target delegation.
        self.assertEqual(self.inspect('package mime\nimport "net/mail"\n'
                                     'func f() { m, _ := mail.ReadMessage(nil); use(m) }'),
                         (False, False))

    def test_real_legacy_parsing_is_not_exempted_by_target_padding(self):
        checks = [
            'r := multipart.NewReader(nil, "b"); r.NextPart()',
            'multipart.NewReader(nil, "b").NextRawPart()',
        ]
        for check in checks:
            with self.subTest(check=check):
                self.assertEqual(self.inspect(
                    f'package mime\nimport ("net/mail"; "mime/multipart"; gm "{TARGET}")\n'
                    f'func f() {{ {check}; gm.Read(nil) }}'), (True, True))
        self.assertEqual(self.inspect('package mime\nimport m "net/mail"\n'
                                     'func f() { msg, _ := m.ReadMessage(nil); use(msg) }'), (False, False))
        self.assertEqual(self.inspect('package mime\nimport . "mime/multipart"\n'
                                     'func f() { r := NewReader(nil, "b"); r.NextPart() }'), (False, True))

    def test_final_gate_preserves_independent_failures_and_old_runtime_bans(self):
        good = (f'package mime\nimport ("net/mail"; gm "{TARGET}")\n'
                'func f() { if _, err := mail.ReadMessage(nil); err != nil { return }; gm.Read(nil) }')
        cases = [
            (good, "PASS", False),
            ('package mime\nimport legacy "github.com/ProtonMail/go-mime"\n'
             'var _ = legacy.VisitAll', "FAIL", True),
            (f'package mime\nimport "net/mail"\nfunc f() {{ m, _ := mail.ReadMessage(nil); use(m) }}', "FAIL", True),
            (f'package mime\nimport gm "{TARGET}"\nvar _ = gm.Read', "FAIL", True),
        ]
        for source, status, cheating in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "mime").mkdir()
                (root / "mime/mime.go").write_text(source)
                derived = build_hardgate_evidence(root, config={"policy": POLICY},
                    observations=[], observation_errors=[], receipts=[], patch_applicable=True,
                    source_test_baseline=None)
                self.assertEqual(derived["no_deceptive_source_fallback"]["status"], status)
                evidence = {name: {"status": "PASS"} for names in COMPOUND_ATOMS.values() for name in names}
                evidence.update(evidence_mode="derived",
                                no_deceptive_source_fallback=derived["no_deceptive_source_fallback"])
                final = evaluate_hardgate(root, evidence)
                self.assertEqual(final["cheating"], cheating)
                if status == "PASS":
                    evidence["public_contract_preserved"] = {"status": "FAIL"}
                    final = evaluate_hardgate(root, evidence)
                    self.assertEqual((final["status"], final["cheating"], final["reward_override"]),
                                     ("FAIL", False, 0))


if __name__ == "__main__":
    unittest.main()
