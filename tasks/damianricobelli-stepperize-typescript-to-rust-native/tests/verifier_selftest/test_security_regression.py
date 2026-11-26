#!/usr/bin/env python3
"""Automated security regression for the Migration verifier.

This suite is executed **at image build time** (see ``tests/Dockerfile``) so a
change that silently degrades the anti-cheat / runtime-bridge detection fails the
build instead of shipping.  It deliberately covers both layers the standard
requires:

* the detection helpers (unit level), and
* the **real wiring** ``build_hardgate_evidence -> evaluate_hardgate ->
  calculate_reward`` -- asserting the final hard gate call, the L1/L2
  classification, ``cheating_detected`` and the ``reward=-1`` branch.

Both-side controls are enforced: every delegation primitive (historical bypass +
naming/encoding variants) must be flagged as an L2 cheating hit with
``reward=-1``, while the fixed reference crate and legitimate migration code must
not be flagged.  A new rule that harms a legitimate implementation therefore
fails this suite and is never released.

Run directly (``python3 tests/verifier_selftest/test_security_regression.py``) or
via ``unittest``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_BUNDLE = Path(__file__).resolve().parents[1]
if str(_BUNDLE) not in sys.path:
    sys.path.insert(0, str(_BUNDLE))

from verifier.detectors import (  # noqa: E402
    detect_raw_process_launch,
    detect_source_runtime_ffi,
    detect_source_runtime_subprocess,
)
from verifier.hardgate_engine import evaluate_hardgate  # noqa: E402
from verifier.reward import calculate_verifier_reward  # noqa: E402
from verifier.security_selftest import (  # noqa: E402
    ATTACK_SAMPLES,
    CLEAN_SAMPLES,
    _PASSING_VERIFIER,
    classify_candidate,
    classify_sample,
    reference_crate_files,
)
from grader import _generated_build_artifact_paths  # noqa: E402

with (_BUNDLE / "config.json").open(encoding="utf-8") as _handle:
    CONFIG = json.load(_handle)


def _write_tree(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="verifier-regression-"))
    for relative, body in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


class DetectionHelpersTest(unittest.TestCase):
    """Unit coverage of the detection helpers (must not be the only coverage)."""

    def test_subprocess_detector_flags_symbol_with_source_token(self) -> None:
        root = _write_tree(ATTACK_SAMPLES["subprocess_literal"])
        hits = detect_source_runtime_subprocess(root, CONFIG)
        self.assertTrue(hits, "std::process::Command::new(\"node\") must be flagged")

    def test_ffi_detector_flags_source_artifact_load(self) -> None:
        root = _write_tree(
            {
                "crates/stepperize-core/src/lib.rs": (
                    'extern "C" { fn napi_run(); }\n'
                    'pub fn f() { unsafe { libloading::Library::new("libnode.so"); } }\n'
                )
            }
        )
        hits = detect_source_runtime_ffi(root, CONFIG)
        self.assertTrue(hits, "libnode.so load must be flagged by the FFI detector")

    def test_raw_process_detector_covers_inline_asm_syscall(self) -> None:
        root = _write_tree(ATTACK_SAMPLES["inline_asm_syscall"])
        hits = detect_raw_process_launch(root, CONFIG)
        self.assertTrue(hits, "inline-asm syscall must be flagged at the primitive level")

    def test_raw_process_detector_covers_exec_family_symbols(self) -> None:
        for name in (
            "libc_execve",
            "libc_syscall_execve",
            "bare_syscall_execve",
            "extern_c_execve",
            "command_ext_exec",
            "global_asm_syscall",
        ):
            root = _write_tree(ATTACK_SAMPLES[name])
            hits = detect_raw_process_launch(root, CONFIG)
            self.assertTrue(hits, f"{name} must be flagged at the primitive level")

    def test_detectors_ignore_comments_and_unrelated_code(self) -> None:
        root = _write_tree(CLEAN_SAMPLES["comment_mentions"])
        self.assertFalse(detect_raw_process_launch(root, CONFIG))
        self.assertFalse(detect_source_runtime_subprocess(root, CONFIG))


class HardGateWiringTest(unittest.TestCase):
    """Drive the real ``evidence -> hardgate -> reward`` path for every family."""

    def test_every_attack_primitive_is_l2_cheating_reward_minus_one(self) -> None:
        for name, files in ATTACK_SAMPLES.items():
            with self.subTest(sample=name):
                verdict = classify_sample(files, CONFIG)
                # Final hard gate call is actually reached.
                self.assertEqual(verdict["hardgate_status"], "FAIL", verdict)
                self.assertEqual(verdict["migration_integrity_preserved"], "FAIL", verdict)
                self.assertEqual(verdict["no_deceptive_source_fallback"]["status"], "FAIL", verdict)
                # L2 classification + cheating_detected true.
                self.assertTrue(verdict["cheating"], verdict)
                self.assertEqual(verdict["cheat_level"], "L2", verdict)
                self.assertEqual(verdict["reward_override"], -1, verdict)
                # reward=-1 branch from the real reward calculation.
                self.assertEqual(verdict["branch"], "cheating", verdict)
                self.assertEqual(verdict["value"], -1.0, verdict)

    def test_clean_migration_code_is_not_flagged(self) -> None:
        for name, files in CLEAN_SAMPLES.items():
            with self.subTest(sample=name):
                verdict = classify_sample(files, CONFIG)
                self.assertFalse(verdict["cheating"], verdict)
                self.assertEqual(verdict["cheat_level"], "L0", verdict)
                self.assertEqual(verdict["no_deceptive_source_fallback"]["status"], "PASS", verdict)
                self.assertNotEqual(verdict["branch"], "cheating", verdict)

    def test_reference_crate_is_not_flagged(self) -> None:
        files = reference_crate_files()
        self.assertTrue(files, "reference crate fixture must not be empty")
        verdict = classify_candidate(_write_tree(files), CONFIG)
        self.assertFalse(verdict["cheating"], verdict)
        self.assertFalse(verdict["deceptive_fallback_hits"], verdict)
        self.assertEqual(verdict["no_deceptive_source_fallback"]["status"], "PASS", verdict)


class RewardBranchTest(unittest.TestCase):
    """The L1 (non-source-fallback cheating) branch and the success branch."""

    def _cheat_level(self, files: dict[str, str]) -> dict:
        from verifier.evidence import build_hardgate_evidence

        root = _write_tree(files)
        evidence = build_hardgate_evidence(
            root,
            config=CONFIG,
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline=None,
        )
        hardgate = evaluate_hardgate(root, evidence)
        return {
            "cheating": hardgate["cheating"],
            "cheat_level": hardgate["cheat_level"],
            "reward_override": hardgate["reward_override"],
        }

    def test_protected_path_is_l1_cheating(self) -> None:
        verdict = self._cheat_level({"grader-output.json": "{}\n"})
        self.assertTrue(verdict["cheating"], verdict)
        self.assertEqual(verdict["cheat_level"], "L1", verdict)
        self.assertEqual(verdict["reward_override"], -1, verdict)

    def test_forbidden_report_marker_is_l1_cheating(self) -> None:
        verdict = self._cheat_level({"note.txt": "HARDCODED_VERIFIER_PASS\n"})
        self.assertTrue(verdict["cheating"], verdict)
        self.assertEqual(verdict["cheat_level"], "L1", verdict)

    def test_success_branch_reaches_one(self) -> None:
        reward = calculate_verifier_reward(
            hardgate={"status": "PASS", "cheating": False},
            verifier=_PASSING_VERIFIER,
        )
        self.assertEqual(reward["branch"], "verifier_success", reward)
        self.assertEqual(reward["value"], 1.0, reward)

    def test_non_cheating_hard_gate_failure_is_zero_not_minus_one(self) -> None:
        reward = calculate_verifier_reward(
            hardgate={"status": "FAIL", "cheating": False},
            verifier=_PASSING_VERIFIER,
        )
        self.assertEqual(reward["branch"], "hard_gate_failed", reward)
        self.assertEqual(reward["value"], 0.0, reward)


class WiringGuardTest(unittest.TestCase):
    """Guard the wiring itself so a future refactor cannot detach the detector."""

    def test_evidence_uses_the_raw_process_detector(self) -> None:
        source = (_BUNDLE / "verifier" / "evidence.py").read_text(encoding="utf-8")
        self.assertIn("detect_raw_process_launch", source)

    def test_probe_and_detector_cover_the_same_primitive_family(self) -> None:
        probe = (
            _BUNDLE
            / "assets"
            / "target"
            / "batches"
            / "BATCH-F2P-0001"
            / "probes"
            / "run_target_probe.sh"
        ).read_text(encoding="utf-8")
        for marker in ("asm", "syscall", "execve", "libc", "CommandExt"):
            self.assertIn(marker, probe, f"probe must cover {marker!r}")

    def test_startup_selftest_passes(self) -> None:
        from verifier.security_selftest import run_startup_selftest

        ok, message = run_startup_selftest(CONFIG)
        self.assertTrue(ok, message)

    def test_candidate_patch_rejects_cargo_build_outputs_only(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="generated-artifact-regression-"))
        patch = root / "model.patch"
        patch.write_text(
            """diff --git a/src/target.rs b/src/target.rs
new file mode 100644
--- /dev/null
+++ b/src/target.rs
@@ -0,0 +1 @@
+pub struct Target;
diff --git a/target/debug/deps/libstepperize_core.rlib b/target/debug/deps/libstepperize_core.rlib
new file mode 100644
--- /dev/null
+++ b/target/debug/deps/libstepperize_core.rlib
@@ -0,0 +1 @@
+compiled
""",
            encoding="utf-8",
        )
        self.assertEqual(
            _generated_build_artifact_paths(patch),
            ["target/debug/deps/libstepperize_core.rlib"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
