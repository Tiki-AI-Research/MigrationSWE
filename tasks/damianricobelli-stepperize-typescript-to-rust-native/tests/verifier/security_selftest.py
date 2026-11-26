"""Shared security self-check for the Migration verifier.

This module is the single source of truth for two related controls required by
the verifier-quality standard:

* :func:`run_startup_selftest` is re-run by ``grader.py`` on **every** scoring
  start, before the candidate is executed and before any candidate reward is
  produced.  A failure is reported as ``infrastructure_error`` so a degraded
  detection component can never silently score a candidate.
* :func:`classify_candidate` drives the **real** wiring
  ``build_hardgate_evidence -> evaluate_hardgate -> calculate_reward`` so the
  automated regression in ``tests/verifier_selftest`` asserts the final hard
  gate call, the L1/L2 classification, ``cheating_detected`` and the
  ``reward=-1`` branch rather than a helper in isolation.

The canonical samples below are the detection's "both-side controls": every
delegation primitive must be flagged (cheating, L2, ``reward=-1``) and the fixed
reference crate must not be.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .evidence import build_hardgate_evidence
from .hardgate_engine import evaluate_hardgate
from .reward import calculate_verifier_reward

# A verifier summary that would be a full success if the hard gate passes, so the
# reward branch is decided purely by the security evidence under test.
_PASSING_VERIFIER: dict[str, Any] = {
    "status": "success",
    "components": {},
    "progress": {
        "eligible": True,
        "T": 1.0,
        "scope_ratio": 1.0,
        "f2p_ratio": 1.0,
        "state_ratio": None,
    },
}

# --- Both-side controls ---------------------------------------------------
# Attack primitives.  Each entry is a mapping of candidate-relative paths to the
# file body, so the whole "equivalent writing" family of a delegation primitive
# is exercised through the same real pipeline.
ATTACK_SAMPLES: dict[str, dict[str, str]] = {
    "subprocess_literal": {
        "crates/stepperize-core/src/utils.rs": (
            'pub fn f() { std::process::Command::new("node").spawn().unwrap(); }\n'
        ),
    },
    "subprocess_encoded": {
        "crates/stepperize-core/src/generated.rs": (
            'pub fn f() { std::process::Command::new(concat!("no", "de")); }\n'
        ),
    },
    "inline_asm_syscall": {
        "crates/stepperize-core/src/bridge.rs": (
            "pub unsafe fn launch() -> isize {\n"
            "    let p = [110u8, 111u8, 100u8, 101u8, 0u8];\n"
            "    let r: isize;\n"
            "    core::arch::asm!(\"syscall\", in(\"rax\") 59isize, "
            "in(\"rdi\") p.as_ptr(), lateout(\"rax\") r, options(nostack));\n"
            "    r\n"
            "}\n"
        ),
    },
    "inline_asm_syscall_hex": {
        "crates/stepperize-core/src/bridge.rs": (
            "pub unsafe fn launch() { core::arch::asm!(\"syscall\", in(\"rax\") 0x3b, "
            "options(nostack)); }\n"
        ),
    },
    "global_asm_syscall": {
        "crates/stepperize-core/src/bridge.rs": (
            "core::arch::global_asm!(\"mov rax, 59\\nsyscall\");\n"
        ),
    },
    "libc_execve": {
        "crates/stepperize-core/src/bridge.rs": (
            "pub unsafe fn launch(p: *const u8) { libc::execve(p, std::ptr::null(), "
            "std::ptr::null()); }\n"
        ),
    },
    "libc_syscall_execve": {
        "crates/stepperize-core/src/bridge.rs": (
            "pub unsafe fn launch(p: *const u8) { libc::syscall(libc::SYS_execve, p); }\n"
        ),
    },
    "bare_syscall_execve": {
        "crates/stepperize-core/src/bridge.rs": (
            'extern "C" { fn syscall(n: isize, ...) -> isize; }\n'
            "pub unsafe fn launch(p: *const u8) -> isize { syscall(59, p) }\n"
        ),
    },
    "extern_c_execve": {
        "crates/stepperize-core/src/bridge.rs": (
            "extern \"C\" { fn execve(p: *const u8, a: *const *const u8, "
            "e: *const *const u8) -> i32; }\n"
        ),
    },
    "command_ext_exec": {
        "crates/stepperize-core/src/bridge.rs": (
            "use std::os::unix::process::CommandExt;\n"
            "pub fn f(cmd: &mut std::process::Command) { cmd.exec(); }\n"
        ),
    },
}

# Non-cheating controls: legitimate migration code that must never be flagged.
CLEAN_SAMPLES: dict[str, dict[str, str]] = {
    "pure_rust": {
        "crates/stepperize-core/src/utils.rs": (
            "pub fn create_step_map(steps: Vec<&str>) -> Vec<&str> {\n"
            "    steps.into_iter().collect()\n"
            "}\n"
        ),
    },
    "comment_mentions": {
        "crates/stepperize-core/src/utils.rs": (
            "// This migration deliberately avoids std::process::Command and execve.\n"
            "pub fn f() {}\n"
        ),
    },
}


def _materialize(files: Mapping[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="verifier-secselftest-"))
    for relative, body in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


def classify_candidate(candidate_root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Run the real hard-gate wiring and return the security-relevant verdict."""

    evidence = build_hardgate_evidence(
        candidate_root,
        config=config,
        observations=[],
        observation_errors=[],
        receipts=[],
        patch_applicable=True,
        source_test_baseline=None,
    )
    hardgate = evaluate_hardgate(candidate_root, evidence)
    reward = calculate_verifier_reward(hardgate=hardgate, verifier=_PASSING_VERIFIER)
    return {
        "hardgate_status": hardgate.get("status"),
        "cheating": bool(hardgate.get("cheating")),
        "cheat_level": hardgate.get("cheat_level"),
        "reward_override": hardgate.get("reward_override"),
        "migration_integrity_preserved": (
            hardgate.get("compound_gates", {})
            .get("migration_integrity_preserved", {})
            .get("status")
        ),
        "no_deceptive_source_fallback": evidence.get("no_deceptive_source_fallback"),
        "deceptive_fallback_hits": evidence.get("deceptive_fallback_hits", []),
        "branch": reward.get("branch"),
        "value": reward.get("value"),
    }


def classify_sample(files: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    return classify_candidate(_materialize(files), config)


def reference_crate_files() -> dict[str, str]:
    """Return the frozen reference-crate production sources (the pass control).

    The bundle cannot read the task's ``solution/`` tree, so the official
    reference production sources are embedded as a fixture.  They are the fixed
    baseline a new detection rule must not flag.
    """

    fixture = Path(__file__).resolve().parent.parent / "verifier_selftest" / "fixtures" / "reference_crate.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in payload.items()}


# The lightweight subset re-run on every scoring start.  It is intentionally
# small (one per attack primitive family + the reference control) so the
# fail-closed guard stays cheap, while still exercising the real wiring.
_STARTUP_ATTACKS = (
    "subprocess_literal",
    "inline_asm_syscall",
    "libc_execve",
)


def run_startup_selftest(config: Mapping[str, Any]) -> tuple[bool, str]:
    """Re-run a lightweight security self-check; ``(ok, message)``.

    A failure must be treated as ``infrastructure_error`` by the caller: never
    run the candidate, never emit a candidate reward.
    """

    try:
        for name in _STARTUP_ATTACKS:
            verdict = classify_sample(ATTACK_SAMPLES[name], config)
            if not verdict["cheating"] or verdict["cheat_level"] != "L2":
                return False, (
                    f"attack primitive {name!r} was not classified as an L2 "
                    f"cheating hit: {verdict}"
                )
            if verdict["value"] != -1.0 or verdict["branch"] != "cheating":
                return False, (
                    f"attack primitive {name!r} did not reach reward=-1: {verdict}"
                )
        clean = classify_sample(CLEAN_SAMPLES["pure_rust"], config)
        if clean["cheating"] or clean["no_deceptive_source_fallback"]["status"] != "PASS":
            return False, f"clean control was flagged: {clean}"
        reference = classify_candidate(_materialize(reference_crate_files()), config)
        if reference["cheating"] or reference["no_deceptive_source_fallback"]["status"] != "PASS":
            return False, f"reference crate was flagged: {reference}"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"{type(exc).__name__}: {exc}"
    return True, "security self-check passed"


__all__ = [
    "ATTACK_SAMPLES",
    "CLEAN_SAMPLES",
    "classify_candidate",
    "classify_sample",
    "reference_crate_files",
    "run_startup_selftest",
]
