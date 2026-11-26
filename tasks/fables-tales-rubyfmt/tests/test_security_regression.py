"""Automated security regression for the Migration verifier.

Run at verifier image build time (see ``Dockerfile``) and importable as a unit
test module.  It exercises the *real* detection helpers and the *real*
hard-gate → reward wiring against two-sided controls:

* historical bypasses and naming/encoding variants must be intercepted;
* the fixed baseline (clean tree) and legitimate production surfaces must pass
  without being mis-classified as a hidden runtime fallback or as cheating.

The suite deliberately builds its own synthetic fixtures so it can run in the
image build context where no candidate repository exists yet.
"""

from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verifier import security_selfcheck as sc  # noqa: E402
from verifier.evidence import _integrity_digest, _marker_counts, build_hardgate_evidence  # noqa: E402
from verifier.go_semantics import scan_go_runtime_sinks  # noqa: E402
from verifier.hardgate_engine import evaluate_hardgate  # noqa: E402
from verifier.progress import build_verifier_progress, progress_to_dict  # noqa: E402
from verifier.reward import calculate_verifier_reward  # noqa: E402
from verifier.verifier_engine import evaluate_migration_units  # noqa: E402


class _Tree:
    """A scratch candidate tree that cleans itself up."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="verifier-regression-")
        self.root = Path(self._tmp.name)

    def write(self, relative: str, text: str) -> None:
        sc._write(self.root, relative, text)

    def __enter__(self) -> "_Tree":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._tmp.cleanup()


class SecuritySelfcheckTests(unittest.TestCase):
    def test_independent_comparison_command_is_not_production_fallback(self) -> None:
        with _Tree() as tree:
            tree.write("internal/formatter/formatter.go", sc._CLEAN_FORMATTER)
            tree.write("cmd/rubyfmt/main.go", 'package main\nimport _ "rubyfmt/internal/formatter"\nfunc main() {}\n')
            tree.write("_devtools/compare/main.go", sc._PROCESS_FALLBACK.replace("package formatter", "package main"))
            self.assertEqual(scan_go_runtime_sinks(tree.root), [])

    def test_imported_helper_and_cli_main_still_detect_fallback(self) -> None:
        for location in ("cmd/rubyfmt/main.go", "internal/build/fallback.go"):
            with self.subTest(location=location), _Tree() as tree:
                tree.write(location, sc._PROCESS_FALLBACK.replace("package formatter", "package main" if location.startswith("cmd/") else "package build"))
                self.assertTrue(scan_go_runtime_sinks(tree.root))
        with _Tree() as tree:
            tree.write("internal/formatter/formatter.go", 'package formatter\nimport _ "rubyfmt/_devtools/helper"\n')
            tree.write("_devtools/helper/main.go", sc._PROCESS_FALLBACK.replace("package formatter", "package main"))
            self.assertTrue(scan_go_runtime_sinks(tree.root))

    def test_selfcheck_all_green(self) -> None:
        result = sc.run_selfcheck()
        self.assertTrue(result["ok"], result["summary"])

    def test_go_skip_markers_include_all_frameworks(self) -> None:
        with _Tree() as tree:
            tree.write(
                "internal/formatter/fixtures_test.go",
                "package formatter\n\nimport \"testing\"\n\n"
                "func TestA(t *testing.T) {\n\tt.Skip(\"a\")\n}\n\n"
                "func TestB(t *testing.T) {\n\tt.Skipf(\"b %d\", 1)\n}\n\n"
                "func TestC(t *testing.T) {\n\tt.SkipNow()\n}\n\n"
                "func TestD(t *testing.T) {\n\tif testing.Short() {\n\t\tt.Skip(\"short\")\n\t}\n}\n\n"
                "func BenchmarkE(b *testing.B) {\n\tb.Skip(\"bench\")\n}\n",
            )
            counts = _marker_counts(tree.root)
        self.assertGreaterEqual(counts["skip"], 5)

    def test_skip_addition_is_digest_neutral_but_weakening_is_not(self) -> None:
        base = sc._GOOD_TEST
        with _Tree() as tree:
            tree.write("test.go", base)
            clean = _integrity_digest(tree.root / "test.go")
            tree.write("test.go", base.replace("func TestFixtures(t *testing.T) {\n", "func TestFixtures(t *testing.T) {\n\tt.Skip(\"x\")\n"))
            skipped = _integrity_digest(tree.root / "test.go")
            tree.write("test.go", base.replace("t.Fatalf(", "t.Logf("))
            weakened = _integrity_digest(tree.root / "test.go")
        self.assertEqual(clean, skipped)
        self.assertNotEqual(clean, weakened)

    def test_hidden_fallback_matrix_is_intercepted(self) -> None:
        variants = {
            "process": sc._PROCESS_FALLBACK,
            "exec": sc._EXEC_FALLBACK,
            "dynamic_ffi": sc._DYNAMIC_FFI_FALLBACK,
            "rpc": sc._RPC_FALLBACK,
            "syscall_exec": sc._SYSCALL_EXEC_FALLBACK,
            # QA3: runtime-assembled / escaped names and a ``build``-named
            # package must not be able to hide a fallback.
            "bytes": sc._BYTES_PROCESS_FALLBACK,
            "escape": sc._ESCAPE_PROCESS_FALLBACK,
        }
        for name, payload in variants.items():
            with self.subTest(variant=name), _Tree() as tree:
                tree.write("internal/formatter/impl.go", payload)
                self.assertTrue(scan_go_runtime_sinks(tree.root), name)
        with self.subTest(variant="build_path"), _Tree() as tree:
            tree.write("internal/build/fallback.go", sc._BUILD_PATH_FALLBACK)
            self.assertTrue(scan_go_runtime_sinks(tree.root))

    def test_legitimate_production_surfaces_pass(self) -> None:
        with _Tree() as tree:
            tree.write("internal/formatter/formatter.go", sc._CLEAN_FORMATTER)
            tree.write("internal/prism/prism.go", sc._PRISM_CGO)
            tree.write("internal/cli/isatty_linux.go", sc._TTY_SYSCALL)
            # Official Go tests legitimately use process APIs; production-only
            # scanning must never look at ``_test.go``.
            tree.write("internal/formatter/formatter_test.go", sc._PROCESS_FALLBACK)
            # QA3 negative controls: a benign byte-array diagnostic label and
            # benign source in a ``build``-named package must both stay clean.
            tree.write("internal/formatter/alias.go", sc._BYTES_BENIGN)
            tree.write("internal/build/helpers.go", sc._BUILD_PATH_BENIGN)
            self.assertEqual(scan_go_runtime_sinks(tree.root), [])

    def test_literal_build_residue_is_not_a_deceptive_fallback(self) -> None:
        with _Tree() as tree:
            tree.write("Makefile", sc._MAKEFILE_RESIDUE)
            self.assertEqual(scan_go_runtime_sinks(tree.root), [])

    def test_wiring_skip_only_scores_zero_and_is_not_cheating(self) -> None:
        with _Tree() as tree:
            tree.write("internal/formatter/fixtures_test.go", sc._GOOD_TEST.replace("func TestFixtures(t *testing.T) {\n", "func TestFixtures(t *testing.T) {\n\tt.Skip(\"x\")\n"))
            hardgate, reward = sc._evaluate(tree.root)
        self.assertEqual(hardgate["status"], "FAIL")
        self.assertFalse(hardgate["cheating"])
        self.assertEqual(reward["branch"], "hard_gate_failed")
        self.assertEqual(reward["value"], 0.0)

    def test_wiring_assertion_weakening_is_l1_and_minus_one(self) -> None:
        with _Tree() as tree:
            tree.write("internal/formatter/fixtures_test.go", sc._GOOD_TEST.replace("t.Fatalf(", "t.Logf("))
            hardgate, reward = sc._evaluate(tree.root)
        self.assertEqual(hardgate["cheat_level"], "L1")
        self.assertTrue(hardgate["cheating"])
        self.assertEqual(reward["branch"], "cheating")
        self.assertEqual(reward["value"], -1.0)

    def test_wiring_hidden_fallback_is_l2_and_minus_one(self) -> None:
        with _Tree() as tree:
            tree.write("internal/formatter/fixtures_test.go", sc._GOOD_TEST)
            tree.write("internal/formatter/formatter.go", sc._PROCESS_FALLBACK)
            hardgate, reward = sc._evaluate(tree.root)
        self.assertEqual(hardgate["cheat_level"], "L2")
        self.assertEqual(reward["branch"], "cheating")
        self.assertEqual(reward["value"], -1.0)

    def test_wiring_bytes_fallback_is_l2_and_minus_one(self) -> None:
        with _Tree() as tree:
            tree.write("internal/formatter/fixtures_test.go", sc._GOOD_TEST)
            tree.write("internal/formatter/formatter.go", sc._BYTES_PROCESS_FALLBACK)
            hardgate, reward = sc._evaluate(tree.root)
        self.assertEqual(hardgate["cheat_level"], "L2")
        self.assertTrue(hardgate["cheating"])
        self.assertEqual(reward["branch"], "cheating")
        self.assertEqual(reward["value"], -1.0)

    def test_wiring_buildpath_fallback_is_l2_and_minus_one(self) -> None:
        with _Tree() as tree:
            tree.write("internal/formatter/fixtures_test.go", sc._GOOD_TEST)
            tree.write("internal/build/fallback.go", sc._BUILD_PATH_FALLBACK)
            hardgate, reward = sc._evaluate(tree.root)
        self.assertEqual(hardgate["cheat_level"], "L2")
        self.assertTrue(hardgate["cheating"])
        self.assertEqual(reward["branch"], "cheating")
        self.assertEqual(reward["value"], -1.0)

    def test_wiring_clean_baseline_passes(self) -> None:
        with _Tree() as tree:
            tree.write("internal/formatter/fixtures_test.go", sc._GOOD_TEST)
            tree.write("internal/formatter/formatter.go", sc._CLEAN_FORMATTER)
            hardgate, reward = sc._evaluate(tree.root)
        self.assertEqual(hardgate["status"], "PASS")
        self.assertFalse(hardgate["cheating"])

    def test_wiring_independent_translation_is_not_weakening(self) -> None:
        # The spec allows an equivalent language/framework rewrite of a
        # migration-target test; a low-similarity rewrite with its own assertion
        # budget must not be mistaken for a weakened certified test.
        with _Tree() as tree:
            tree.write("internal/formatter/fixtures_test.go", sc._INDEPENDENT_TARGET_TEST)
            tree.write("internal/formatter/formatter.go", sc._CLEAN_FORMATTER)
            hardgate, reward = sc._evaluate(tree.root)
        self.assertFalse(hardgate["cheating"])
        self.assertEqual(hardgate["status"], "PASS")

    def test_wiring_missing_target_is_incomplete_not_tampering(self) -> None:
        # A target test the candidate never authored is an incomplete migration
        # (scored by migration_scope_complete), not test tampering.
        with _Tree() as tree:
            tree.write("internal/formatter/formatter.go", sc._CLEAN_FORMATTER)
            hardgate, reward = sc._evaluate(tree.root)
        self.assertFalse(hardgate["cheating"])
        self.assertEqual(hardgate["status"], "PASS")


class PrismBuildMetadataTests(unittest.TestCase):
    """Accept multi-file cgo bridges while requiring the documented files."""

    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).resolve().parent / "assets/shared/batches/BATCH-F2P-0001/probe.py"
        spec = importlib.util.spec_from_file_location("prism_build_probe", path)
        cls.probe_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.probe_module)

    def _check(self, metadata: dict[str, object]) -> None:
        module = self.probe_module
        with _Tree() as tree:
            probe = module.TargetProbe(tree.root)
            try:
                with mock.patch.object(probe, "require_go_module", return_value="go"), mock.patch.object(
                    probe, "run", return_value=module.Result(0, json.dumps(metadata), "")
                ), mock.patch.object(
                    probe, "run_binary", return_value=module.Result(0, "a(1, 2, 3)\n", "")
                ):
                    probe.check_cgo_prism_without_ruby_or_rust()
            finally:
                probe.cleanup()

    def test_multiple_cgo_files_are_allowed(self) -> None:
        self._check({
            "ImportPath": "rubyfmt/internal/prism",
            "CgoFiles": ["convert.go", "prism.go"],
            "CFiles": ["bridge.c", "prism.c"],
        })

    def test_required_bridge_and_c_parser_are_not_optional(self) -> None:
        for cgo, cfiles in ((["convert.go"], ["prism.c"]), (["prism.go"], ["bridge.c"])):
            with self.subTest(cgo=cgo, cfiles=cfiles):
                with self.assertRaises(self.probe_module.ProbeFailure):
                    self._check({
                        "ImportPath": "rubyfmt/internal/prism", "CgoFiles": cgo, "CFiles": cfiles,
                    })


class MigrationScopeBoundaryTests(unittest.TestCase):
    """QA4: scope must not re-score F2P, yet independent evidence still fails a unit."""

    @staticmethod
    def _scope(observations: list[dict[str, object]], root: Path, *, residue: object = ()) -> dict[str, str]:
        scoped = evaluate_migration_units(
            sc._SCOPE_UNITS,
            sc._SCOPE_UNIT_CHECKS,
            observations,
            ordinary_residue_hits=list(residue),
            candidate_root=root,
            status_excluded_ids=("F2P-SCOPE",),
        )
        return {str(row["id"]): str(row["status"]) for row in scoped}

    def test_f2p_failure_does_not_reduce_scope(self) -> None:
        # F2P-SCOPE fails, but that behaviour is scored by the F2P dimension;
        # the unit keeps full migration scope because its non-F2P evidence and
        # declared target test are intact.
        with _Tree() as tree:
            tree.write("internal/behaviour/behaviour_test.go", "package behaviour\n")
            tree.write("Makefile", "all:\n\tgo build ./...\n")
            statuses = self._scope(list(sc._SCOPE_OBSERVATIONS), tree.root)
        self.assertEqual(statuses["MU-SCOPE"], "PASS")
        self.assertEqual(statuses["MU-RESIDUE"], "PASS")

    def test_missing_target_test_reduces_scope(self) -> None:
        # Independent migration evidence: a unit's declared target test that the
        # candidate never authored is still an incomplete unit.
        with _Tree() as tree:
            tree.write("Makefile", "all:\n\tgo build ./...\n")
            statuses = self._scope(list(sc._SCOPE_OBSERVATIONS), tree.root)
        self.assertEqual(statuses["MU-SCOPE"], "FAIL")

    def test_ordinary_residue_reduces_scope(self) -> None:
        # Ordinary source residue still fails the unit that owns the path, and
        # stays a scope failure (not a cheating escalation).
        with _Tree() as tree:
            tree.write("internal/behaviour/behaviour_test.go", "package behaviour\n")
            tree.write("Makefile", "all:\n\tcargo build --release\n")
            statuses = self._scope(
                list(sc._SCOPE_OBSERVATIONS),
                tree.root,
                residue=[{"path": "Makefile", "rule_id": "RULE-005", "target": "cargo"}],
            )
        self.assertEqual(statuses["MU-RESIDUE"], "FAIL")
        self.assertEqual(statuses["MU-SCOPE"], "PASS")

    def test_missing_observation_is_inconclusive_not_pass(self) -> None:
        # A frozen unit observation that is absent from the report is a contract
        # error (fail closed), never a silent pass.
        with _Tree() as tree:
            tree.write("internal/behaviour/behaviour_test.go", "package behaviour\n")
            tree.write("Makefile", "all:\n\tgo build ./...\n")
            statuses = self._scope(
                [{"id": "P2P-RESIDUE", "kind": "P2P", "status": "PASS"}],
                tree.root,
            )
        self.assertEqual(statuses["MU-SCOPE"], "INCONCLUSIVE")


class GraderInfrastructureContractTests(unittest.TestCase):
    """A startup self-check failure must stop before producing a reward."""

    def test_infrastructure_result_is_not_measurable(self) -> None:
        import grader

        result = grader._infrastructure_result("security self-check failed")
        self.assertTrue(grader._is_infrastructure(result))
        self.assertEqual(grader._exit_code(result), 2)

    def test_infrastructure_emits_empty_reward_map(self) -> None:
        import grader

        result = grader._infrastructure_result("security self-check failed")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            grader._write_reward_artifacts(output, result)
            reward = json.loads((output / "reward.json").read_text(encoding="utf-8"))
            details = json.loads((output / "reward-details.json").read_text(encoding="utf-8"))
        # No candidate reward is recorded for an infrastructure outcome.
        self.assertEqual(reward, {})
        self.assertIsNone(details["reward"])
        self.assertTrue(details["errors"]["infrastructure_error"])

    def test_measured_candidate_keeps_numeric_reward_map(self) -> None:
        import grader

        result = {
            "status": "failed",
            "hardgate": "FAIL",
            "cheating": False,
            "reward": {"value": 0.0, "branch": "hard_gate_failed"},
        }
        self.assertFalse(grader._is_infrastructure(result))
        self.assertEqual(grader._exit_code(result), 1)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            grader._write_reward_artifacts(output, result)
            reward = json.loads((output / "reward.json").read_text(encoding="utf-8"))
        # Harbor rejects non-numeric values, so a measured failure still emits
        # the four verifier-only keys with 0.0 for unmeasured ratios.
        self.assertEqual(reward["reward"], 0.0)
        self.assertEqual(reward["test"], 0.0)
        self.assertEqual(reward["migration_scope_complete_ratio"], 0.0)

    def test_passing_candidate_exits_zero(self) -> None:
        import grader

        result = {"status": "success", "hardgate": "PASS", "cheating": False}
        self.assertEqual(grader._exit_code(result), 0)

    def test_cheating_candidate_exits_one(self) -> None:
        import grader

        result = {"status": "success", "hardgate": "FAIL", "cheating": True, "reward": {"value": -1.0}}
        self.assertEqual(grader._exit_code(result), 1)


class CompositionTests(unittest.TestCase):
    def test_progress_excludes_p2p_and_differential(self) -> None:
        progress = progress_to_dict(build_verifier_progress(
            migration_scope={"status": "PASS", "passed": 24, "total": 24},
            f2p={"status": "PASS", "passed": 40, "total": 48},
            state={"status": "NA"},
            state_applicable=False,
            state_applicability="na",
        ))
        self.assertNotIn("differential_ratio", progress)
        self.assertAlmostEqual(progress["T"], (1.0 + 40 / 48) / 2)

    def test_differential_failure_is_advisory_not_hardgate(self) -> None:
        # QA3: DIFFERENTIAL parity observations are diagnostics.  A legal
        # migration whose oracle legitimately differs must still pass the P2P
        # hard gate (previously differential was folded into
        # ``p2p_regression_preserved`` and hard-rejected such candidates).
        config = sc._config()
        config["tests"] = {
            "p2p": [{"id": "P2P-1"}],
            "differential": [{"id": "DIFF-1"}],
        }
        with _Tree() as tree:
            tree.write("internal/formatter/fixtures_test.go", sc._GOOD_TEST)
            observations = [
                {"kind": "P2P", "id": "P2P-1", "status": "PASS"},
                {"kind": "DIFFERENTIAL", "id": "DIFF-1", "status": "FAIL"},
            ]
            evidence = build_hardgate_evidence(
                tree.root,
                config=config,
                observations=observations,
                observation_errors=[],
                receipts=sc._receipts(),
                patch_applicable=True,
                source_test_baseline=sc._baseline(),
                test_baseline_source="target-test-baseline",
            )
        self.assertEqual(evidence["p2p_regression_preserved"]["status"], "PASS")
        self.assertEqual(evidence["differential_diagnostics"]["status"], "FAIL")

    def test_p2p_failure_fails_the_regression_gate(self) -> None:
        config = sc._config()
        config["tests"] = {
            "p2p": [{"id": "P2P-1"}],
            "differential": [{"id": "DIFF-1"}],
        }
        with _Tree() as tree:
            tree.write("internal/formatter/fixtures_test.go", sc._GOOD_TEST)
            observations = [
                {"kind": "P2P", "id": "P2P-1", "status": "FAIL"},
                {"kind": "DIFFERENTIAL", "id": "DIFF-1", "status": "PASS"},
            ]
            evidence = build_hardgate_evidence(
                tree.root,
                config=config,
                observations=observations,
                observation_errors=[],
                receipts=sc._receipts(),
                patch_applicable=True,
                source_test_baseline=sc._baseline(),
                test_baseline_source="target-test-baseline",
            )
        self.assertEqual(evidence["p2p_regression_preserved"]["status"], "FAIL")

    def test_config_declares_no_discarded_weights(self) -> None:
        config = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8"))
        self.assertNotIn("weights", config)
        self.assertNotIn("graded", config)
        self.assertEqual(config.get("reward_mode"), "VERIFIER_ONLY")


class MigratedHarnessContractTests(unittest.TestCase):
    """Check the new probes without a Go toolchain or candidate execution."""

    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).resolve().parent / "assets/shared/batches/BATCH-F2P-0003/probe_support.py"
        spec = importlib.util.spec_from_file_location("migrated_harness_probe", path)
        cls.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.probe)

    def _inputs(self, tree: _Tree) -> None:
        tree.write("go.mod", "module rubyfmt\n")
        tree.write("fixtures/small/sample_actual.rb", "a 1,2,3\n")
        tree.write("fixtures/small/sample_expected.rb", "a(1, 2, 3)\n")
        tree.write("fixtures/large/nested/sample_actual.rb", "a 1,2,3\n")
        tree.write("fixtures/large/nested/sample_expected.rb", "a(1, 2, 3)\n")
        for name in ("methods", "array_literals", "string_literals"):
            tree.write("ci/" + name + "_stress_test.rb", "puts 1\n")

    @staticmethod
    def _result(code: int = 0, stdout: bytes = b"") -> object:
        import subprocess

        return subprocess.CompletedProcess([], code, stdout, b"")

    def test_preserved_assertions_pass_and_original_tree_is_unchanged(self) -> None:
        with _Tree() as tree:
            self._inputs(tree)
            before = {p.relative_to(tree.root): p.read_bytes() for p in tree.root.rglob("*") if p.is_file()}
            ctx = self.probe.Context(tree.root)
            commands = []

            def execute(command, *, cwd, **kwargs):
                commands.append(command)
                stage = Path(cwd)
                if "-bench=." in command:
                    missing = any(not (stage / "ci" / (name + "_stress_test.rb")).is_file()
                                  for name in ("methods", "array_literals", "string_literals"))
                    return self._result(int(missing), b"BenchmarkStress/methods-2 1 10 ns/op\n")
                missing = any(not (stage / directory / ("sample_" + half + ".rb")).is_file()
                              for directory in ("fixtures/small", "fixtures/large/nested")
                              for half in ("actual", "expected"))
                return self._result(int(missing))

            try:
                with mock.patch.object(ctx, "_command", side_effect=execute):
                    self.probe._migrated_fixture_harness_case(ctx)
                    self.probe._migrated_benchmark_case(ctx)
                self.assertEqual(len(commands), 13)  # fixture baseline + 4 mutations/restores; benchmark baseline + 3 scripts
                after = {p.relative_to(tree.root): p.read_bytes() for p in tree.root.rglob("*") if p.is_file()}
                self.assertEqual(before, after)
            finally:
                ctx.cleanup()

    def test_silently_dropped_fixture_halves_are_rejected(self) -> None:
        with _Tree() as tree:
            self._inputs(tree)
            ctx = self.probe.Context(tree.root)
            try:
                with mock.patch.object(ctx, "_command", return_value=self._result()):
                    with self.assertRaisesRegex(self.probe.ProbeFailure, "missing fixture half"):
                        self.probe._migrated_fixture_harness_case(ctx)
                stage = next(ctx.tmp_root.glob("fixture-harness-*/repo"))
                self.assertTrue((stage / "fixtures/small/sample_actual.rb").is_file())
            finally:
                ctx.cleanup()

    def test_each_half_in_both_fixture_roots_is_checked_and_restored(self) -> None:
        for omitted_group in ("small", "large"):
            for omitted_half in ("actual", "expected"):
                with self.subTest(group=omitted_group, half=omitted_half), _Tree() as tree:
                    self._inputs(tree)
                    ctx = self.probe.Context(tree.root)

                    def execute(command, *, cwd, **kwargs):
                        stage = Path(cwd)
                        missing = []
                        for group, directory in (("small", "fixtures/small"), ("large", "fixtures/large/nested")):
                            for half in ("actual", "expected"):
                                if not (stage / directory / ("sample_" + half + ".rb")).is_file():
                                    missing.append((group, half))
                        detected = any(pair != (omitted_group, omitted_half) for pair in missing)
                        return self._result(int(detected))

                    try:
                        with mock.patch.object(ctx, "_command", side_effect=execute):
                            with self.assertRaisesRegex(self.probe.ProbeFailure, "missing fixture half.*fixtures/" + omitted_group):
                                self.probe._migrated_fixture_harness_case(ctx)
                        stage = next(ctx.tmp_root.glob("fixture-harness-*/repo"))
                        for original in tree.root.glob("fixtures/**/*_*.rb"):
                            self.assertEqual((stage / original.relative_to(tree.root)).read_bytes(), original.read_bytes())
                    finally:
                        ctx.cleanup()

    def test_restored_fixture_control_failure_is_not_counted_as_success(self) -> None:
        with _Tree() as tree:
            self._inputs(tree)
            ctx = self.probe.Context(tree.root)
            try:
                with mock.patch.object(ctx, "_command", side_effect=[self._result(0), self._result(1), self._result(1)]):
                    with self.assertRaisesRegex(self.probe.ProbeFailure, "restored fixture harness control failed"):
                        self.probe._migrated_fixture_harness_case(ctx)
                stage = next(ctx.tmp_root.glob("fixture-harness-*/repo"))
                self.assertTrue((stage / "fixtures/small/sample_actual.rb").is_file())
            finally:
                ctx.cleanup()

    def test_omitted_stress_benchmarks_are_rejected(self) -> None:
        with _Tree() as tree:
            self._inputs(tree)
            ctx = self.probe.Context(tree.root)
            try:
                with mock.patch.object(ctx, "_command", return_value=self._result(0, b"BenchmarkFixtures-2 1 10 ns/op\n")):
                    with self.assertRaisesRegex(self.probe.ProbeFailure, "omitted the stress input"):
                        self.probe._migrated_benchmark_case(ctx)
            finally:
                ctx.cleanup()

    def test_broken_pristine_control_is_not_counted_as_a_detected_mutation(self) -> None:
        with _Tree() as tree:
            self._inputs(tree)
            ctx = self.probe.Context(tree.root)
            try:
                with mock.patch.object(ctx, "_command", return_value=self._result(1)):
                    with self.assertRaisesRegex(self.probe.ProbeFailure, "baseline failed"):
                        self.probe._migrated_fixture_harness_case(ctx)
                    with self.assertRaisesRegex(self.probe.ProbeFailure, "baseline failed"):
                        self.probe._migrated_benchmark_case(ctx)
            finally:
                ctx.cleanup()

    def test_failure_reason_survives_the_integration_report(self) -> None:
        path = Path(__file__).resolve().parent / "assets/harness.py"
        spec = importlib.util.spec_from_file_location("migration_integration_harness", path)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        observation = {"id": "F2P-055", "kind": "F2P", "batch_id": "BATCH-F2P-0003"}
        with mock.patch.multiple(
            harness,
            load_inventory=mock.Mock(return_value=[observation]),
            build_binary=mock.Mock(return_value=(None, False, False, "")),
            native_suite=mock.Mock(return_value={}),
            run_p2p=mock.Mock(return_value=({}, "")),
            run_differential=mock.Mock(return_value=(None, {}, "")),
            run_f2p=mock.Mock(return_value=({"F2P-055": {"status": "FAIL", "reason": "missing fixture half"}}, "")),
        ):
            report = harness.build_report("target", Path("/unused"))
        self.assertEqual(report["observations"][0]["reason"], "missing fixture half")
        self.assertEqual(report["observations"][0]["status"], "FAIL")

    def test_ignore_space_probe_accepts_exclusions_in_every_input_mode(self) -> None:
        with _Tree() as tree:
            ctx = self.probe.Context(tree.root)
            patterns = set()
            calls = []

            def execute(args, *, cwd, input_bytes=None):
                case = Path(cwd)
                ignore_file = next(case.glob(".*ignore"))
                patterns.add((ignore_file.name, ignore_file.read_bytes()))
                calls.append(tuple(args))
                output = input_bytes if input_bytes is not None and "--check" not in args else b""
                return self._result(0, output)

            try:
                with mock.patch.object(ctx, "run", side_effect=execute):
                    self.probe._ignore_pattern_spaces_case(ctx)
                self.assertEqual(patterns, {
                    (name, pattern)
                    for name in (".rubyfmtignore", ".gitignore")
                    for pattern in (b"blocked   \n", b"blocked\\ \n", b"blocked\\    \n")
                })
                self.assertEqual(len(calls), 48)
                self.assertTrue(any("--stdin-filepath" in args for args in calls))
                self.assertTrue(any("-i" in args for args in calls))
            finally:
                ctx.cleanup()

    def test_ignore_space_probe_rejects_both_whitespace_regressions(self) -> None:
        for escaped in (False, True):
            with self.subTest(escaped=escaped), _Tree() as tree:
                ctx = self.probe.Context(tree.root)

                def execute(args, *, cwd, input_bytes=None):
                    pattern = next(Path(cwd).glob(".*ignore")).read_bytes()
                    broken = (b"\\ " in pattern) == escaped
                    if broken and "--check" in args:
                        return self._result(5, b"--- excluded file\n+++ excluded file\n")
                    output = input_bytes if input_bytes is not None and "--check" not in args else b""
                    return self._result(0, output)

                try:
                    with mock.patch.object(ctx, "run", side_effect=execute):
                        with self.assertRaisesRegex(self.probe.ProbeFailure, "failed to exclude"):
                            self.probe._ignore_pattern_spaces_case(ctx)
                finally:
                    ctx.cleanup()


class VerifierOnlyRewardContractTests(unittest.TestCase):
    """The reward path is verifier-only: no GRADED mode, objective or rubric."""

    @staticmethod
    def _passing_verifier() -> dict[str, object]:
        return {
            "status": "success",
            "progress": {
                "eligible": True,
                "T": 1.0,
                "scope_ratio": 1.0,
                "f2p_ratio": 1.0,
                "state_ratio": None,
                "errors": [],
            },
        }

    def test_reward_module_is_verifier_only(self) -> None:
        from verifier import reward as reward_module

        self.assertEqual(reward_module.REWARD_MODES, frozenset({"VERIFIER_ONLY"}))
        self.assertFalse(hasattr(reward_module, "GRADED_WEIGHTS"))

    def test_reward_is_single_component_t(self) -> None:
        from verifier.reward import calculate_reward

        reward = calculate_reward(
            hardgate={"status": "PASS", "cheating": False},
            verifier=self._passing_verifier(),
        )
        self.assertEqual(reward["branch"], "verifier_success")
        self.assertEqual(reward["value"], 1.0)
        self.assertEqual(reward["weights"], {"T": 1.0})
        self.assertNotIn("graded", reward)
        self.assertNotIn("O", reward)
        self.assertNotIn("R", reward)
        with self.assertRaises(ValueError):
            calculate_reward(
                hardgate={"status": "PASS", "cheating": False},
                verifier=self._passing_verifier(),
                reward_mode="GRADED",
            )

    def test_reward_api_dropped_objective_and_rubric(self) -> None:
        import inspect

        from verifier.reward import calculate_reward

        parameters = inspect.signature(calculate_reward).parameters
        self.assertNotIn("objective", parameters)
        self.assertNotIn("rubric", parameters)

    def test_reward_details_carry_no_objectivity_or_rubric(self) -> None:
        import grader
        from verifier.reward import calculate_reward

        result = {
            "status": "success",
            "hardgate": "PASS",
            "cheating": False,
            "reward": calculate_reward(
                hardgate={"status": "PASS", "cheating": False},
                verifier=self._passing_verifier(),
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            grader._write_reward_artifacts(output, result)
            details = json.loads((output / "reward-details.json").read_text(encoding="utf-8"))
        self.assertNotIn("objectivity", details)
        self.assertNotIn("rubrics", details)
        self.assertNotIn("judge", details)
        self.assertEqual(details["weights"], {"test": 1.0})
        self.assertEqual(details["scoring_version"], "migration-verifier-reward:v2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
