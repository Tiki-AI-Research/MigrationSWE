"""Regressions for target probes incorrectly borrowing TypeScript capability."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ASSETS = Path(__file__).resolve().parents[1] / "assets"
SPEC = importlib.util.spec_from_file_location(
    "integration_dispatch", ASSETS / "shared/integration/dispatch.py"
)
assert SPEC is not None and SPEC.loader is not None
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)


class TargetCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pev2-dispatch-test-")
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        (self.repo / "src").mkdir()
        (self.repo / "src/interfaces.ts").write_text("export class Node {}")
        crate = self.repo / "crates/pev2-core"
        (crate / "src").mkdir(parents=True)
        (crate / "Cargo.toml").write_text('[package]\nname = "pev2-core"\n')
        (crate / "src/lib.rs").write_text("pub type Node = serde_json::Map<String, serde_json::Value>;")
        corpus = json.loads(
            (ASSETS / "shared/batches/BATCH-DIFFERENTIAL-0002/node-type-cases.json").read_text()
        )
        self.nodes = [
            {"input": case["input"], "actual": case["expect"]}
            for case in corpus["cases"]
        ]

    def project(self, *outcomes):
        with patch.object(dispatch, "_run_cargo_probe", side_effect=outcomes):
            return dispatch._diff0002_results(str(self.repo), str(ASSETS), 10)

    def test_missing_exports_cannot_borrow_existing_typescript_node(self):
        # Real Gemini 01/03 candidates export aliases but no callable API.
        result = self.project(
            (None, "probe-build-failed:unresolved import parse_node_type"),
            (None, "probe-build-failed:unresolved imports"),
        )
        self.assertEqual(result["DIFF-033"]["status"], "FAIL")
        self.assertEqual(result["DIFF-034"]["status"], "FAIL")

    def test_typescript_only_target_does_not_satisfy_rust_contract(self):
        result = self.project(
            (None, "target-crate-absent"), (None, "target-crate-absent")
        )
        self.assertEqual(result["DIFF-033"]["status"], "FAIL")
        self.assertEqual(result["DIFF-034"]["status"], "FAIL")

    def test_reexports_are_judged_by_compilation_not_source_spelling(self):
        (self.repo / "crates/pev2-core/src/lib.rs").write_text("mod api; pub use api::*;")
        result = self.project((self.nodes, ""), (True, ""))
        self.assertEqual(result["DIFF-033"]["status"], "PASS")
        self.assertEqual(result["DIFF-034"]["status"], "PASS")

    def test_public_names_in_comments_do_not_prove_callability(self):
        (self.repo / "crates/pev2-core/src/lib.rs").write_text(
            "// from_source create_plan from_text from_json cleanup_source split_into_lines\n"
            "// split_balanced find_node_by_id find_node_by_subplan_name should_show_prop parse_node_type\n"
        )
        result = self.project((self.nodes, ""), (None, "probe-build-failed:missing API"))
        self.assertEqual(result["DIFF-034"]["status"], "FAIL")

    def test_node_semantics_remain_independent_of_api_check(self):
        self.nodes[0]["actual"] = {"Node Type": "wrong"}
        result = self.project((self.nodes, ""), (True, ""))
        self.assertEqual(result["DIFF-033"]["status"], "FAIL")
        self.assertEqual(result["DIFF-034"]["status"], "PASS")

    def test_empty_or_missing_node_output_cannot_pass(self):
        for output in ([], None, {}):
            with self.subTest(output=output):
                result = self.project((output, ""), (True, ""))
                self.assertNotEqual(result["DIFF-033"]["status"], "PASS")

    def test_evaluator_failure_and_timeout_are_not_semantic_failures(self):
        result = self.project(
            (None, "probe-source-absent"), (None, "probe-timeout")
        )
        self.assertEqual(result["DIFF-033"]["status"], "INFRA_ERROR")
        self.assertEqual(result["DIFF-034"]["status"], "TIMEOUT")

    def test_source_projection_remains_explicit(self):
        (self.repo / "crates/pev2-core/Cargo.toml").unlink()
        with patch.object(dispatch, "_run_cargo_probe") as probe:
            result = dispatch._diff0002_results(str(self.repo), str(ASSETS), 10, "source")
        probe.assert_not_called()
        self.assertEqual(result["DIFF-033"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
