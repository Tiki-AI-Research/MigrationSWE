from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent.parent
_ERROR_KEYS = (
    "infrastructure_error",
    "agent_time_out",
    "build error",
    "compile error",
)


def _load_grader():
    # Direct execution puts verifier/ on sys.path, shadowing the verifier
    # package with verifier/verifier.py; drop it so grader resolves its imports.
    script_dir = str(Path(__file__).resolve().parent)
    shadowed = [entry for entry in sys.path if entry in ("", script_dir)]
    sys.path[:] = [entry for entry in sys.path if entry not in ("", script_dir)]
    sys.path.insert(0, str(_TESTS_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(
            "reward_details_selftest_grader", _TESTS_ROOT / "grader.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        del sys.path[0]
        sys.path.extend(shadowed)
    return module


def _write_details(grader, result):
    with tempfile.TemporaryDirectory(prefix="reward-details-selftest-") as directory:
        output = Path(directory)
        grader._write_reward_artifacts(output, result)
        return json.loads((output / "reward-details.json").read_text(encoding="utf-8"))


def _receipt(kind, *, returncode, stderr=""):
    receipt = {"kind": kind, "returncode": returncode, "timed_out": False}
    if stderr:
        receipt["stderr"] = stderr
    return receipt


def _result(status, receipts):
    return {"status": status, "receipts": {"commands": receipts}}


_MISSING_MANIFEST = "error: could not find `Cargo.toml` in `/tmp/harbor-candidate` or any parent directory\n"
_RUSTC_ERROR = (
    "error[E0425]: cannot find value `missing` in this scope\n"
    " --> src/lib.rs:3:5\n"
    "error: could not compile `stepperize-core-native` (lib) due to 1 previous error\n"
)
_EPERM_PREREQUISITE = "EPERM: operation not permitted, chmod '/tmp/harbor-candidate/node_modules/.pnpm/acorn@8.16.0/node_modules/acorn/bin/acorn'\n"
_TEST_ASSERTION = "AssertionError: expected 'upcoming' to be 'active'\nFAILED (failures=1)"


class DeceptiveFallbackCorroborationTest(unittest.TestCase):
    """Pins the contract-obliged corroboration rule for the anti-cheat scan."""

    OBLIGED = 'const result = await schema["~standard"].validate(value);'
    LOOKUP = "steps.find((step) => step.id === id) as Get.StepById"

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            import evidence
        finally:
            sys.path.pop(0)
        cls.evidence = evidence
        cls.config = json.loads((_TESTS_ROOT / "config.json").read_text(encoding="utf-8"))

    def _hardgate(self, source):
        with tempfile.TemporaryDirectory(prefix="deceptive-selftest-") as directory:
            target = Path(directory) / "packages/core/src/utils.ts"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
            return self.evidence.build_hardgate_evidence(
                Path(directory),
                config=self.config,
                observations=[],
                observation_errors=[],
                receipts=[],
                patch_applicable=True,
                source_test_baseline=None,
            )

    def test_contract_obliged_literal_alone_is_not_a_fallback(self):
        evidence = self._hardgate(self.OBLIGED + "\n")
        self.assertEqual(evidence["deceptive_fallback_hits"], [])
        self.assertFalse(evidence["cheating"])
        self.assertEqual(len(evidence["contract_obliged_hits"]), 1)

    def test_contract_obliged_literal_with_residue_is_a_fallback(self):
        evidence = self._hardgate(self.OBLIGED + "\n" + self.LOOKUP + "\n")
        self.assertTrue(evidence["deceptive_fallback_hits"])
        self.assertTrue(evidence["cheating"])


class RewardDetailsErrorFlagsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grader = _load_grader()

    def _errors(self, result):
        return _write_details(self.grader, result)["errors"]

    def test_success_run_writes_four_false_flags_and_required_fields(self):
        result = _result("success", [_receipt("target-build", returncode=0)])
        details = _write_details(self.grader, result)
        errors = details["errors"]
        self.assertEqual(sorted(errors), sorted(_ERROR_KEYS))
        self.assertTrue(all(isinstance(value, bool) for value in errors.values()))
        self.assertEqual(errors, dict.fromkeys(_ERROR_KEYS, False))
        for field in (
            "cheating_detected",
            "test",
            "migration_scope_complete_ratio",
            "f2p_pass_ratio",
            "p2p_pass_ratio",
            "reward",
            "errors",
        ):
            self.assertIn(field, details)

    def test_missing_manifest_is_build_error_not_compile_error(self):
        result = _result("failed", [_receipt("target-build", returncode=101, stderr=_MISSING_MANIFEST)])
        errors = self._errors(result)
        self.assertTrue(errors["build error"])
        self.assertFalse(errors["compile error"])
        self.assertFalse(errors["infrastructure_error"])
        self.assertFalse(errors["agent_time_out"])

    def test_rust_compile_failure_sets_build_and_compile_error(self):
        result = _result("failed", [_receipt("target-build", returncode=101, stderr=_RUSTC_ERROR)])
        errors = self._errors(result)
        self.assertTrue(errors["build error"])
        self.assertTrue(errors["compile error"])
        self.assertFalse(errors["infrastructure_error"])

    def test_infrastructure_result_sets_infrastructure_error_only(self):
        errors = self._errors(_result("infra_error", []))
        self.assertTrue(errors["infrastructure_error"])
        self.assertFalse(errors["build error"])
        self.assertFalse(errors["compile error"])
        self.assertFalse(errors["agent_time_out"])

    def test_infrastructure_status_suppresses_candidate_flags(self):
        result = _result("infra_error", [_receipt("target-build", returncode=101, stderr=_RUSTC_ERROR)])
        errors = self._errors(result)
        self.assertTrue(errors["infrastructure_error"])
        self.assertFalse(errors["build error"])
        self.assertFalse(errors["compile error"])

    def test_permission_failure_on_prerequisite_is_a_build_error(self):
        result = _result("failed", [_receipt("target-build", returncode=255, stderr=_EPERM_PREREQUISITE)])
        errors = self._errors(result)
        self.assertTrue(errors["build error"])
        self.assertFalse(errors["compile error"])
        self.assertFalse(errors["infrastructure_error"])

    def test_native_test_assertion_failure_sets_no_error_flags(self):
        result = _result("failed", [_receipt("target-native-test", returncode=1, stderr=_TEST_ASSERTION)])
        errors = self._errors(result)
        self.assertFalse(errors["build error"])
        self.assertFalse(errors["compile error"])
        self.assertFalse(errors["infrastructure_error"])


if __name__ == "__main__":
    unittest.main()
