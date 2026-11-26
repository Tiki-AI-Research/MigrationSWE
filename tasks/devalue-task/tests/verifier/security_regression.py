#!/usr/bin/env python3
"""End-to-end security regression for the verifier report and reward wiring.

This is intentionally heavier than ``acceptance_checks.py``: it starts from a
real candidate tree, runs the real ``run_harbor_verifier`` entry point, then
writes the real reward files through the grader helpers.  The Docker build runs
it so a broken L1/L2 classification or a stale report bridge fails the image
build instead of silently scoring a candidate.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = HERE.parent
_default_candidate = Path("/app")
if not _default_candidate.is_dir():
    _default_candidate = Path("<SCRATCH>/devalue-base")
REPO_ROOT = Path(
    os.environ.get("MIGRATION_SECURITY_CANDIDATE", str(_default_candidate))
)
# The verifier directory also contains ``verifier.py``.  If it stays on
# sys.path first, Python imports that module instead of the ``verifier``
# namespace package and grader's ``verifier.component_result`` import fails.
sys.path[:] = [
    entry for entry in sys.path if Path(entry or ".").resolve() != HERE
]
sys.path.insert(0, str(HERE.parent))

import grader  # noqa: E402
from verifier import harbor_entry  # noqa: E402


def _fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def _prepare_candidate(destination: Path, mutation: str) -> None:
    shutil.copytree(
        REPO_ROOT,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns("node_modules", ".git"),
    )
    # Corepack creates this root-owned environment file during image setup.
    # It is not part of the source tree and would make the candidate identity
    # fail its toolchain probe before the security classification is reached.
    for stale in (destination / ".corepack.env", destination / ".corepack-home"):
        if stale.is_symlink() or stale.is_file():
            stale.unlink()
        elif stale.is_dir():
            shutil.rmtree(stale)
    # The source checkout pins the old release's development dependencies.
    # The evaluator store contains the target toolchain, so use its manifest for
    # these isolated integration runs; the attacks live in the source/test
    # files and do not depend on the old dev dependency pins.
    dependencies = BUNDLE / "environment" / "dependencies"
    for name in ("package.json", "pnpm-lock.yaml"):
        source = dependencies / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    if mutation == "test-skip":
        test_file = destination / "test" / "test.js"
        if not test_file.is_file():
            raise AssertionError("the baseline test file is missing")
        with test_file.open("a", encoding="utf-8") as handle:
            handle.write("\ntest.skip('security-regression inert skip', () => {});\n")
    elif mutation == "hidden-answer":
        entry = destination / "index.js"
        if not entry.is_file():
            raise AssertionError("the baseline entry file is missing")
        with entry.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n// security regression: /tests/assets/source-baseline-outputs.json\n"
            )
    else:  # pragma: no cover - programming error
        raise AssertionError(f"unknown security mutation: {mutation}")


def _run_case(work: Path, mutation: str, expected_level: str) -> int:
    # ``run_harbor_verifier`` derives its candidate log directory from
    # ``logs_root.parent / "candidate"``.  Keep the candidate at a different
    # name so that directory is never mistaken for the tree being verified.
    candidate = work / "app"
    logs = work / "logs"
    logs.mkdir(parents=True)
    _prepare_candidate(candidate, mutation)
    if shutil.which("setpriv") and hasattr(harbor_entry, "_candidate_identity"):
        identity = harbor_entry._candidate_identity()
        if identity is not None:
            import subprocess

            subprocess.run(
                ["chown", "-R", f"{identity[0]}:{identity[1]}", str(candidate)],
                check=False,
            )
    result = harbor_entry.run_harbor_verifier(
        candidate_root=candidate,
        bundle_root=BUNDLE,
        logs_root=logs,
    )
    if result.get("cheat_level") != expected_level:
        return _fail(
            f"{mutation}: expected {expected_level}, got "
            f"{result.get('cheat_level')!r}: {result.get('status')!r}"
        )
    hardgate = result.get("hardgate_result")
    if not isinstance(hardgate, dict) or hardgate.get("reward_override") != -1:
        return _fail(f"{mutation}: the hard-gate result did not carry reward_override -1")
    if result.get("cheating") is not True:
        return _fail(f"{mutation}: the verifier result did not set cheating")

    # Exercise the same report writers the grader uses after the verifier.
    output = work / "reward"
    output.mkdir()
    grader._write_reward(output, result)
    grader._write_reward_details(output, result)
    reward = json.loads((output / "reward.json").read_text(encoding="utf-8"))
    details = json.loads((output / "reward-details.json").read_text(encoding="utf-8"))
    if reward.get("reward") != -1:
        return _fail(f"{mutation}: expected reward -1, got {reward.get('reward')!r}")
    if details.get("cheating_detected") is not True:
        return _fail(f"{mutation}: reward-details did not preserve cheating_detected")
    if details.get("reward") != -1 or details.get("cheat_level") != expected_level:
        return _fail(
            f"{mutation}: reward-details did not preserve the {expected_level}/-1 "
            f"classification: reward={details.get('reward')!r} "
            f"level={details.get('cheat_level')!r}"
        )
    return 0


def main() -> int:
    cases = (("test-skip", "L1"), ("hidden-answer", "L2"))
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        # The temporary root is 0700 by default; the isolated candidate
        # identity must be able to traverse it.
        root.chmod(0o755)
        for index, (mutation, expected_level) in enumerate(cases, start=1):
            work = root / str(index)
            work.mkdir()
            work.chmod(0o755)
            code = _run_case(work, mutation, expected_level)
            if code:
                return code
    print("PASS: end-to-end security regression")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
