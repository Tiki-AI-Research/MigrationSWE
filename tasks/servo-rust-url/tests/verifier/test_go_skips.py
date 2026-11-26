"""Regression checks for platform skips and the static/runtime skip gates."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verifier.evidence import _go_skip_count, _combine_skip_evidence, runtime_skip_evidence_from_junit


class GoSkipChecks(unittest.TestCase):
    def test_inactive_platform_and_package_constant(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory)
            (root / "platform.go").write_text('package p\nimport "runtime"\nconst isWindows = runtime.GOOS == "windows"\n')
            path = root / "paths_test.go"
            for condition in ('isWindows', 'runtime.GOOS == "windows"', 'runtime.GOOS != "linux"'):
                source = 'package p\nimport "runtime"\nfunc TestPaths(t *testing.T) { if ' + condition + ' { t.Skip("Unix paths") }; t.Log("checked") }\n'
                path.write_text(source)
                with patch("sys.platform", "linux"):
                    self.assertEqual(_go_skip_count(source, path), 0, condition)
                with patch("sys.platform", "win32"):
                    self.assertGreater(_go_skip_count(source, path), 0, condition)

    def test_real_and_indirect_skips_still_fail(self):
        for statement in (
            't.Skip("reason")', 't.Skipf("%s", "reason")', 't.SkipNow()',
            '(*testing.T).Skip(t, "reason")', 'f := t.Skip; f("reason")',
            'apply(t.Skip)', 'h := Holder{fn: t.Skip}; h.fn("reason")',
            'reflect.ValueOf(t).MethodByName("Skip").Call(nil)',
        ):
            source = 'package p\nfunc TestCase(t *testing.T) { ' + statement + ' }'
            self.assertGreater(_go_skip_count(source), 0, statement)
        for condition in ('runtime.GOOS == "linux"', 'unknown', 'isWindows'):
            source = 'package p\nimport "runtime"\nfunc TestCase(t *testing.T) { if ' + condition + ' { t.Skip("reason") } }'
            with patch("sys.platform", "linux"):
                self.assertGreater(_go_skip_count(source), 0, condition)

    def test_comments_strings_and_boolean_fields_are_not_skips(self):
        for source in (
            '// t.Skip("example")\nvar message = "t.Skip()"',
            'func f() { if fixture.Skip { return } }',
            'var example = `if runtime.GOOS == "windows" {`\nfunc TestReal(t *testing.T) { t.SkipNow() }',
        ):
            expected = 1 if 'TestReal' in source else 0
            self.assertEqual(_go_skip_count(source), expected)

    def test_shadowed_constant_and_active_else_remain_visible(self):
        source = 'package p\nimport "runtime"\nconst isWindows = runtime.GOOS == "windows"\nfunc TestCase(t *testing.T) { isWindows := true; if isWindows { t.SkipNow() } }'
        with patch("sys.platform", "linux"):
            self.assertGreater(_go_skip_count(source), 0)
            source = 'package p\nimport "runtime"\nfunc TestCase(t *testing.T) { if runtime.GOOS == "windows" { t.Skip("Unix") } else { t.SkipNow() } }'
            self.assertGreater(_go_skip_count(source), 0)

    def test_runtime_skip_and_missing_report_cannot_pass(self):
        clean_static = {"status": "PASS", "evidence": {}}
        skipped = '<testsuite><testcase name="TestCase"><skipped/></testcase></testsuite>'
        self.assertEqual(_combine_skip_evidence(clean_static, runtime_skip_evidence_from_junit(True, skipped))["status"], "FAIL")
        self.assertEqual(_combine_skip_evidence(clean_static, runtime_skip_evidence_from_junit(False, None))["status"], "INCONCLUSIVE")


if __name__ == "__main__":
    unittest.main()
