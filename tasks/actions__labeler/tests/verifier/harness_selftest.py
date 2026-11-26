"""Offline regressions for runner permissions and Harbor report handling.

Run with: python3 tasks/tests/verifier/harness_selftest.py
External Go commands and privilege setup are mocked; asset copies, runner
dispatch, file writes, report parsing and observation validation are real.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
# Avoid the sibling verifier.py shadowing the verifier namespace package when
# this file is executed directly from the verifier directory.
sys.path = [value for value in sys.path if Path(value or ".").resolve() != ROOT / "verifier"]
sys.path.insert(0, str(ROOT))
from verifier import harbor_entry as entry


def load_runner():
    loader = importlib.machinery.SourceFileLoader("labeler_harness_regression", str(ROOT / "assets/target/runner"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def relax(directory):
    for parent, dirs, files in os.walk(directory, followlinks=False):
        Path(parent).chmod(0o755)
        for name in dirs + files:
            path = Path(parent) / name
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o700)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labeler-runner-test-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.addCleanup(relax, self.work)
        self.repo = self.work / "repo"
        (self.repo / "cmd/labeler").mkdir(parents=True)
        (self.repo / "go.mod").write_text("module fixture\ngo 1.24.0\n")
        (self.repo / "cmd/labeler/entry.go").write_text("package main\nfunc main() {}\n")
        self.runner = load_runner()
        runtime = json.loads((ROOT / "config.json").read_text())["runtime"]
        self.assets = entry._prepare_candidate_assets(ROOT, self.repo, runtime, private=False)
        self.runner.ASSET_ROOT = str(self.assets)
        self.runner.repo_root = lambda: str(self.repo)

    def test_readonly_assets_refresh_restores_modified_helpers(self):
        original = self.assets / "target/blackbox/action_runtime.py"
        expected = original.read_bytes()
        original_mode = stat.S_IMODE(original.stat().st_mode)
        self.runner.ensure_asset_dir(str(self.repo), "target/blackbox", "blackbox")
        helper = self.repo / "blackbox/action_runtime.py"
        self.assertTrue(helper.stat().st_mode & stat.S_IWUSR)
        self.assertTrue(helper.parent.stat().st_mode & stat.S_IWUSR)
        helper.write_text("raise SystemExit(0)\n")
        self.runner.ensure_asset_dir(str(self.repo), "target/blackbox", "blackbox")
        self.assertEqual(helper.read_bytes(), expected)
        self.assertEqual(original.read_bytes(), expected)
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), original_mode)
        self.assertFalse(original_mode & stat.S_IWUSR)
        self.assertEqual(stat.S_IMODE(original.parent.stat().st_mode), 0o555)
        self.runner.cleanup()
        self.assertFalse(helper.parent.exists())

    def test_refresh_handles_existing_readonly_tree(self):
        target = self.repo / "blackbox/nested"
        target.mkdir(parents=True)
        (target / "old.py").write_text("old")
        target.chmod(0o555)
        target.parent.chmod(0o555)
        self.runner.ensure_asset_dir(str(self.repo), "target/blackbox", "blackbox")
        self.assertFalse(target.exists())
        self.assertTrue((self.repo / "blackbox/action_runtime.py").exists())

    def test_refresh_unlinks_destination_symlink_without_changing_target(self):
        outside = self.work / "outside"
        outside.mkdir()
        sentinel = outside / "keep"
        sentinel.write_text("unchanged")
        outside.chmod(0o555)
        (self.repo / "blackbox").symlink_to(outside, target_is_directory=True)
        self.runner.ensure_asset_dir(str(self.repo), "target/blackbox", "blackbox")
        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o555)
        self.assertFalse((self.repo / "blackbox").is_symlink())

    def test_refresh_does_not_follow_nested_directory_symlinks(self):
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "keep").write_text("unchanged")
        outside.chmod(0o555)
        blackbox = self.repo / "blackbox"
        blackbox.mkdir()
        (blackbox / "linked").symlink_to(outside, target_is_directory=True)
        blackbox.chmod(0o555)
        self.runner.ensure_asset_dir(str(self.repo), "target/blackbox", "blackbox")
        self.assertEqual((outside / "keep").read_text(), "unchanged")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o555)

    def test_full_98_observation_dispatch_finishes_with_readonly_source_assets(self):
        def command_stub(*args, **kwargs):
            events = [{"Action": "run", "Package": "fixture", "Test": "TestFixture"},
                      {"Action": "pass", "Package": "fixture", "Test": "TestFixture"}]
            return "PASS", 0, "\n".join(json.dumps(row) for row in events) + '\nLABELER_PROJECTION {"fixture": true}\n'

        report = self.repo / ".harbor-result.json"
        with patch.object(self.runner, "_run_command", side_effect=command_stub), redirect_stdout(io.StringIO()):
            self.assertEqual(self.runner.main([str(report)]), 0)
        payload = json.loads(report.read_text())
        harness = json.loads((ROOT / "assets/shared/integration/harness.json").read_text())
        self.assertEqual([row["id"] for row in payload["observations"]], harness["observation_ids"])
        self.assertEqual(len(payload["observations"]), 98)
        self.assertFalse((self.repo / "blackbox").exists())
        self.assertFalse(self.runner._CREATED)

    def test_report_write_failure_propagates_and_keeps_previous_file(self):
        report = self.repo / "report.json"
        report.write_text("old")
        with patch.object(self.runner.os, "replace", side_effect=PermissionError("write denied")):
            with self.assertRaisesRegex(PermissionError, "write denied"):
                self.runner._write_text(str(report), "new")
        self.assertEqual(report.read_text(), "old")
        self.assertFalse(list(self.repo.glob(".migration-report-*")))

    def test_report_write_is_complete_and_replaces_existing_file(self):
        report = self.repo / "subdir/report.json"
        self.runner._write_text(str(report), '{"observations": []}')
        self.assertEqual(json.loads(report.read_text()), {"observations": []})
        self.assertFalse(list(report.parent.glob(".migration-report-*")))


class EntryTests(unittest.TestCase):
    def run_case(self, content=None, code=1, stale=False, symlink=False, prerequisite_code=0):
        temporary = tempfile.TemporaryDirectory(prefix="labeler-entry-test-")
        self.addCleanup(temporary.cleanup)
        work = Path(temporary.name)
        self.addCleanup(relax, work)
        repo, logs = work / "repo", work / "logs"
        repo.mkdir()
        logs.mkdir()
        (repo / "go.mod").write_text("module fixture\ngo 1.24.0\n")
        config = copy.deepcopy(json.loads((ROOT / "config.json").read_text()))
        config["runtime"]["workspace_materializations"] = []
        original_load = entry._load_json
        commands, paths = [], {}
        if stale:
            (repo / ".harbor-result.json").write_text('{"stale": true}')
            (logs / "result.json").write_text('{"stale": true}')

        def load(path):
            return copy.deepcopy(config) if path == ROOT / "config.json" else original_load(path)

        def execute(command, **kwargs):
            commands.append(list(command))
            harness = command[0].endswith("/target/runner")
            if harness:
                destination = Path(command[-1])
                self.assertFalse(destination.exists(), "stale formal report reached the harness")
                paths["harness"] = str(destination)
                if symlink:
                    outside = work / "outside.json"
                    outside.write_text('{"must_not_be_read": true}')
                    destination.symlink_to(outside)
                elif content is not None:
                    destination.write_text(content)
                return subprocess.CompletedProcess(command, code, "harness stdout", "PermissionError: fixture failure")
            for arg in command:
                if arg.startswith("--junitfile="):
                    destination = Path(arg.split("=", 1)[1])
                    paths["native"] = str(destination)
                    destination.write_text('<testsuites tests="1" failures="0" errors="0"/>')
                    # Even a prerequisite that writes the formal path cannot
                    # provide the report for a later harness invocation.
                    if stale:
                        (repo / ".harbor-result.json").write_text('{"prerequisite": true}')
            return subprocess.CompletedProcess(command, prerequisite_code, "native stdout", "native stderr")

        with ExitStack() as stack:
            stack.enter_context(patch.object(entry, "_load_json", side_effect=load))
            stack.enter_context(patch.object(entry, "_require_isolation"))
            stack.enter_context(patch.object(entry, "validate_public_integrity"))
            stack.enter_context(patch.object(entry, "validate_public_projection_contract"))
            stack.enter_context(patch.object(entry, "_rewrite_candidate_paths"))
            stack.enter_context(patch.object(entry, "_candidate_isolation_mode", return_value="setuid"))
            stack.enter_context(patch.object(entry, "_run", side_effect=execute))
            stack.enter_context(patch.object(entry, "_merge_native_p2p_observations",
                                            side_effect=lambda **kw: kw["observations"]))
            result = entry.run_harbor_verifier(candidate_root=repo, bundle_root=ROOT, logs_root=logs)
        return result, logs, commands, paths

    def assert_infra(self, result, kind, code):
        self.assertEqual(result["status"], "infra_error")
        self.assertEqual(result["hardgate"], "INFRA_ERROR")
        self.assertEqual(result["failure"]["kind"], kind)
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["reward"]["branch"], "inconclusive")
        self.assertEqual(result["receipts"]["harness_returncode"], code)
        receipts = result["receipts"]["commands"]
        self.assertEqual(len(receipts), 4)
        self.assertEqual(receipts[-1]["returncode"], code)
        self.assertIn("fixture failure", receipts[-1]["stderr"])
        self.assertEqual(receipts[-1]["stdout"], "harness stdout")

    def test_missing_report_after_runner_crash_is_infra_and_keeps_stderr(self):
        result, _, _, _ = self.run_case()
        self.assert_infra(result, "report-missing", 1)
        self.assertIn("fixture failure", result["failure"]["stderr_tail"])

    def test_success_exit_without_report_is_infra(self):
        result, _, _, _ = self.run_case(code=0)
        self.assert_infra(result, "report-missing", 0)

    def test_native_xml_has_a_separate_path_and_is_archived(self):
        result, logs, _, paths = self.run_case()
        self.assert_infra(result, "report-missing", 1)
        self.assertNotEqual(paths["native"], paths["harness"])
        self.assertTrue((logs / "native-report.xml").read_text().startswith("<testsuites"))
        self.assertFalse((logs / "result.json").exists())

    def test_stale_and_prerequisite_reports_are_removed(self):
        result, logs, _, _ = self.run_case(stale=True)
        self.assert_infra(result, "report-missing", 1)
        self.assertFalse((logs / "result.json").exists())

    def test_xml_report_is_infra_and_keeps_command_receipts(self):
        result, _, _, _ = self.run_case(content="<testsuites/>")
        self.assert_infra(result, "report-parse", 1)
        self.assertEqual(result["failure"]["error_type"], "JSONDecodeError")

    def test_invalid_report_with_zero_exit_keeps_stdout_and_stderr(self):
        result, _, _, _ = self.run_case(content="not JSON", code=0)
        self.assert_infra(result, "report-parse", 0)

    def test_json_array_is_infra_and_keeps_command_receipts(self):
        result, _, _, _ = self.run_case(content="[]")
        self.assert_infra(result, "report-parse", 1)
        self.assertEqual(result["failure"]["error_type"], "ValueError")

    def test_symlink_report_is_rejected_before_copying(self):
        result, logs, _, _ = self.run_case(symlink=True)
        self.assert_infra(result, "report-unsafe", 1)
        self.assertFalse((logs / "result.json").exists())

    def test_valid_json_business_failures_still_reach_scoring_and_fail(self):
        config = json.loads((ROOT / "config.json").read_text())
        rows = [{"id": row["id"], "kind": row["kind"], "status": "FAIL",
                 "value": {"type": "json", "value": {"result": "FAIL"}}}
                for group in config["tests"].values() for row in group]
        result, logs, _, _ = self.run_case(content=json.dumps({
            "schema_version": "migration-observations:v6", "observations": rows,
        }), code=0)
        self.assertEqual(result["observation_contract_errors"], [])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["observations"]), 98)
        self.assertLessEqual(result["reward"]["value"], 0)
        self.assertEqual(result["receipts"]["harness_returncode"], 0)
        self.assertEqual(len(json.loads((logs / "result.json").read_text())["observations"]), 98)

    def test_candidate_build_failure_still_counts_as_business_failure(self):
        result, _, commands, _ = self.run_case(prerequisite_code=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["hardgate"], "FAIL")
        self.assertEqual(len(commands), 1)
        self.assertIn("native stderr", result["receipts"]["commands"][0]["stderr"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
