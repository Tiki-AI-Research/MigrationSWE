"""Exercise scoring transitions without compiling candidate Go code."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from verifier.evidence import _derive_execution_atoms, _marker_counts
from verifier.harbor_entry import _run_declared_commands
from grader import _validate_change_scope


class SemanticScoringChecks(unittest.TestCase):
    def test_change_scope_rejects_non_go_files_and_keeps_go_test_assets(self):
        def added(name):
            return (
                f"diff --git a/{name} b/{name}\n"
                "new file mode 100644\n"
                "index 0000000..c1b0730\n"
                "--- /dev/null\n"
                f"+++ b/{name}\n"
                "@@ -0,0 +1 @@\n"
                "+fixture\n"
            )

        with tempfile.TemporaryDirectory() as directory:
            patch = Path(directory) / "model.patch"
            for name in ("go.mod", "url/url.go", "idna/testdata/corpus.txt", "url/tests/new_vectors.json"):
                patch.write_text(added(name))
                self.assertIsNone(_validate_change_scope(patch), name)
            for name in ("README-go.md", "idna/idna_data.bin", "data-url/new.go"):
                patch.write_text(added(name))
                self.assertIsNotNone(_validate_change_scope(patch), name)

    def test_plain_go_boolean_is_not_a_testing_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "go.mod").write_text("module example\ngo 1.23\n")
            source = root / "check_test.go"
            source.write_text('package example\nfunc check() { skip := false; skip = true; _ = skip }\n')
            self.assertEqual(_marker_counts(root), {"skip": 0, "xfail": 0, "disabled": 0})
            source.write_text('package example\nimport "testing"\nfunc TestCheck(t *testing.T) { t.Skip("disabled") }\n')
            self.assertGreater(_marker_counts(root)["skip"], 0)

    def receipt(self, command, kind, index, returncode):
        return {
            "schema_version": "migration-execution-receipt:v1",
            "command": command, "kind": kind, "index": index,
            "returncode": returncode, "timed_out": False,
        }

    def test_lint_failure_retains_receipt_and_runs_behavior_checks(self):
        def run(command, *, kind, index, **kwargs):
            code = 1 if kind == "target-diagnostic" else 0
            return subprocess.CompletedProcess(command, code), self.receipt(command, kind, index, code)
        commands = [
            ("target-build", ["go", "build", "./..."]),
            ("target-diagnostic", ["go", "vet", "./..."]),
            ("target-native-test", ["go", "test", "-vet=off", "./..."]),
        ]
        with patch("verifier.harbor_entry._run_with_receipt", side_effect=run):
            receipts, failed, infra = _run_declared_commands(
                commands, cwd=Path("."), timeout=1, environment={},
            )
        self.assertFalse(failed or infra)
        self.assertEqual(len(receipts), 3)
        self.assertEqual(receipts[1]["returncode"], 1)
        build, _, valid = _derive_execution_atoms(receipts)
        self.assertEqual(build["status"], "PASS")
        self.assertEqual(len(valid), 3)

    def test_build_and_test_failures_still_reject(self):
        for kind in ("target-build", "target-native-test"):
            receipt = self.receipt(["go", "test"], kind, 1, 1)
            with patch("verifier.harbor_entry._run_with_receipt", return_value=(
                subprocess.CompletedProcess(receipt["command"], 1), receipt,
            )):
                receipts, failed, infra = _run_declared_commands(
                    [(kind, receipt["command"])], cwd=Path("."), timeout=1, environment={},
                )
            self.assertTrue(failed)
            self.assertFalse(infra)
            build, _, _ = _derive_execution_atoms(receipts)
            self.assertEqual(build["status"], "FAIL")

    def test_probe_module_uses_exact_local_toolchain_version(self):
        root = Path(__file__).resolve().parents[1] / "assets"
        spec = importlib.util.spec_from_file_location("semantic_harness", root / "shared/integration/harness.py")
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        def run(command, *, cwd, **kwargs):
            module = (Path(cwd) / "go.mod").read_text()
            self.assertIn("go 1.23.0\n", module)
            self.assertIn("replace github.com/servo/rust-url => /candidate\n", module)
            return subprocess.CompletedProcess(command, 0, json.dumps({}), "")
        with patch.object(harness.shutil, "which", return_value="go"), patch.object(harness.subprocess, "run", side_effect=run):
            projections, error = harness.run_go_probe(str(root), "/candidate")
        self.assertEqual(projections, {})
        self.assertIsNone(error)
