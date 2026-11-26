#!/usr/bin/env python3
"""R9 adversarial real-entry replay for Stremio-stremio-core-1102.

One offline, task-contained runner that exercises the repaired public-contract
probe through the REAL, unmodified ``tests/`` bundle entry (``/tests/test.sh``)
inside a ``--network none`` Docker container.

The runner builds every candidate patch at run time -- no variant full-solution
patch is ever stored in the task tree:

1. for every variant it unpacks the frozen base repository from the task's own
   ``tests/assets/build-deps/repo/base-repo.bundle`` into its own fresh,
   isolated temporary offline checkout (no destructive git command is ever
   issued);
2. it applies the frozen ``solution/solution.patch``;
3. it mutates exactly the targeted Go line(s) / test expectation(s) for the
   variant (and, for the ``ref_notests`` control, deletes the reference-added
   ``*_test.go`` files);
4. it regenerates a correct candidate patch with ``git diff --binary`` against
   the base commit.

Three genuinely new malicious variants (distinct from the r6/r7/r8 sets) plus
three legal controls:

* ``atk4_runtime_event_wire``     -- RuntimeEvent wire tag renamed (public
  contract / runtime event wire), candidate's own marshal test adjusted.
* ``atk5_deeplink_query_encoding`` -- query value space encoded as ``+``
  instead of ``%20`` (public contract / deep-link query encoding), candidate's
  own deep-link test adjusted.
* ``atk6_analytics_flush_shape``  -- Flush delivers a shaped-empty batch
  (public contract / analytics flush batch shape), no candidate test exists.
* ``reference``                   -- frozen ``solution/solution.patch``.
* ``ref_notests``                 -- reference minus its added ``*_test.go``.
* ``ref_nilenv_intolerant``       -- reference whose analytics ``Emit``
  dereferences ``env.Now`` unconditionally (proves nil env is not a contract:
  the repaired probe passes a declared ``testenv.New()``).

Evidence (run metadata, per-case patch sha256 + captured verifier result,
machine-readable SUMMARY.json) is written under the ignored
``jobs/Stremio-stremio-core-1102_9_evidence`` directory, outside the task tree.

Usage::

    python3 -B environment/r9_adversarial_replay_selfcheck.py
    python3 -B environment/r9_adversarial_replay_selfcheck.py --skip-build
    python3 -B environment/r9_adversarial_replay_selfcheck.py --only atk4_runtime_event_wire

The script is read-only with respect to ``instruction.md``, ``solution/``,
``tests/`` and ``docs/``: it only reads the frozen patch and the base bundle,
and builds the tests image from ``tests/`` (read-only input).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

TASK_ROOT = Path(__file__).resolve().parents[1]
WS_ROOT = TASK_ROOT.parents[1]
TESTS_DIR = TASK_ROOT / "tests"
SOLUTION = TASK_ROOT / "solution" / "solution.patch"
BUNDLE = TESTS_DIR / "assets" / "build-deps" / "repo" / "base-repo.bundle"

DEFAULT_IMAGE = "stremio1102-r9-tests:tests"
DEFAULT_OUTPUT = WS_ROOT / "jobs" / "Stremio-stremio-core-1102_9_evidence"
HYGIENE_CHECK = TASK_ROOT / "environment" / "tree_hygiene_selfcheck.py"

_ANALYTICS = "analytics/analytics.go"
_RUNTIME = "runtime/runtime.go"
_QPE = "types/query_params_encode.go"
_DL_TEST = "deep_links/deep_links_test.go"
_RT_TEST = "runtime/runtime_test.go"

# (path, old, new) exact-string edits; each must match exactly once.
_ATK4_EDITS = [
    (_RUNTIME, '\tName string          `json:"name"`', '\tName string          `json:"kind"`'),
    (_RT_TEST, '{"name":"NewState","args":["ctx","player"]}', '{"kind":"NewState","args":["ctx","player"]}'),
    (_RT_TEST, '{"name":"CoreEvent","args":{"event":"UserLoggedOut"}}', '{"kind":"CoreEvent","args":{"event":"UserLoggedOut"}}'),
]
_ATK5_EDITS = [
    (
        _QPE,
        'fmt.Sprintf("%s=%s", PercentEncode(p.Key), PercentEncode(p.Value))',
        'fmt.Sprintf("%s=%s", PercentEncode(p.Key), strings.ReplaceAll(PercentEncode(p.Value), "%20", "+"))',
    ),
    (_DL_TEST, "sort=popularity&genre=A%20B", "sort=popularity&genre=A+B"),
]
_ATK6_EDITS = [
    (
        _ANALYTICS,
        "\t\tfor _, b := range batches {\n\t\t\t_ = a.sendFn(b)\n\t\t}",
        "\t\tfor _, b := range batches {\n\t\t\t_ = a.sendFn(Batch{AuthKey: b.AuthKey})\n\t\t}",
    ),
]
_NILENV_EDITS = [
    (
        _ANALYTICS,
        "\tnow := time.Now().UnixMilli()\n\tif a.env != nil {\n\t\tnow = a.env.Now().UnixMilli()\n\t}",
        "\tnow := time.Now().UnixMilli()\n\tnow = a.env.Now().UnixMilli()",
    ),
]

# Frozen r6/r7/r8 variant names the r9 attacks must not duplicate.
_FORBIDDEN_NAMES = frozenset(
    {
        "new_asm_delegation",
        "new_exit_alias_mask",
        "new_fake_surface_v2",
        "new_skip_helper",
        "new_skip_nodemodules",
        "new_net_bridge",
        "atk1_contract",
        "atk2_delegation",
        "atk3_mask",
        "atk1_analytics",
        "atk2_enverror",
        "atk3_legacy",
    }
)

_DIFF_ID = re.compile(r"^DIFF-\d+$")
_PROBE_FAIL = re.compile(r"^--- FAIL:\s+(\w+)", re.MULTILINE)
_SCOPE_TOTAL = 41

_ATTACK_EXPECTED = {
    "status": "failed",
    "branch": "hard_gate_failed",
    "reward": 0.0,
    "hardgate": "FAIL",
    "failed_atoms": ["public_contract_preserved"],
    "scope_ratio": 1.0,
    "scope_status": "PASS",
    "scope_passed": _SCOPE_TOTAL,
    "scope_total": _SCOPE_TOTAL,
    "no_diff_missing": True,
    "probe_fails": True,
}
_CONTROL_EXPECTED = {
    "status": "success",
    "branch": "verifier_success",
    "reward": 1.0,
    "hardgate": "PASS",
    "failed_atoms": [],
    "scope_ratio": 1.0,
    "scope_status": "PASS",
    "scope_passed": _SCOPE_TOTAL,
    "scope_total": _SCOPE_TOTAL,
    "no_diff_missing": True,
    "probe_fails": False,
}

VARIANTS: list[dict[str, Any]] = [
    {
        "name": "atk4_runtime_event_wire",
        "kind": "attack",
        "family": "public-contract/runtime-event-wire",
        "probe": "TestRuntimeEventWire",
        "edits": _ATK4_EDITS,
        "static_present": (
            b'json:"kind"',
            b'{"kind":"NewState","args":["ctx","player"]}',
            b'{"kind":"CoreEvent","args":{"event":"UserLoggedOut"}}',
        ),
        "static_absent": (),
    },
    {
        "name": "atk5_deeplink_query_encoding",
        "kind": "attack",
        "family": "public-contract/deep-link-query-encoding",
        "probe": "TestQueryParamsEncode",
        "edits": _ATK5_EDITS,
        "static_present": (b'%20", "+"', b"sort=popularity&genre=A+B"),
        "static_absent": (b"sort=popularity&genre=A%20B",),
    },
    {
        "name": "atk6_analytics_flush_shape",
        "kind": "attack",
        "family": "public-contract/analytics-flush-batch-shape",
        "probe": "TestAnalyticsQueueSurface",
        "edits": _ATK6_EDITS,
        "static_present": (b"Batch{AuthKey: b.AuthKey}",),
        "static_absent": (b"\t\t\t_ = a.sendFn(b)",),
    },
    {
        "name": "reference",
        "kind": "control",
        "family": "valid-reference",
        "probe": "TestRuntimeEventWire",
        "edits": [],
        "static_present": (b'json:"name"', b"a.env != nil", b"\t\t\t_ = a.sendFn(b)"),
        "static_absent": (b'json:"kind"', b"Batch{AuthKey: b.AuthKey}", b'%20", "+"'),
    },
    {
        "name": "ref_notests",
        "kind": "control",
        "family": "valid-reference-minus-added-tests",
        "probe": "TestRuntimeEventWire",
        "edits": [],
        "drop_added_tests": True,
        "static_present": (b"analytics/analytics.go", b'json:"name"'),
        "static_absent": (b"_test.go",),
    },
    {
        "name": "ref_nilenv_intolerant",
        "kind": "control",
        "family": "valid-reference-nilenv-intolerant",
        "probe": "TestRuntimeEventWire",
        "edits": _NILENV_EDITS,
        "static_present": (b"now = a.env.Now().UnixMilli()",),
        "static_absent": (b"a.env != nil",),
    },
]


class ReplayError(RuntimeError):
    """Raised when the replay cannot produce trustworthy evidence."""


def _run(
    argv: list[str],
    *,
    timeout: float | None = None,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_checkout(work: Path) -> tuple[Path, str]:
    repo = work / "repo"
    repo.mkdir()
    for argv in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "fetch", "-q", str(BUNDLE), "HEAD"],
        ["git", "-C", str(repo), "checkout", "-q", "--detach", "FETCH_HEAD"],
        ["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"],
    ):
        done = _run(argv)
        if done.returncode != 0:
            raise ReplayError(f"checkout step failed: {argv} :: {done.stderr.strip()}")
    base = _run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    return repo, base


def _apply_exact(path: Path, old: str, new: str) -> None:
    if not path.is_file():
        raise ReplayError(f"edit target missing: {path}")
    text = path.read_text(encoding="utf-8")
    occurrences = text.count(old)
    if occurrences != 1:
        raise ReplayError(f"edit anchor matched {occurrences} times in {path.name}: {old!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def _drop_added_tests(repo: Path) -> int:
    dropped = 0
    for path in sorted(repo.rglob("*_test.go")):
        path.unlink()
        dropped += 1
    return dropped


def _build_variant(repo: Path, base: str, variant: dict[str, Any]) -> bytes:
    """Apply the frozen solution + variant edits, return the regenerated patch.

    ``repo`` must be a freshly prepared, isolated checkout: this function never
    issues a destructive git command, it only adds files to the index and reads
    the resulting diff.
    """

    applied = _run(["git", "-C", str(repo), "apply", "--binary", str(SOLUTION)])
    if applied.returncode != 0:
        raise ReplayError(f"solution.patch did not apply: {applied.stderr.strip()}")
    for relative, old, new in variant["edits"]:
        _apply_exact(repo / relative, old, new)
    if variant.get("drop_added_tests"):
        if _drop_added_tests(repo) == 0:
            raise ReplayError("ref_notests dropped no added test files")
    staged = _run(["git", "-C", str(repo), "add", "-A", "--", "."])
    if staged.returncode != 0:
        raise ReplayError(f"git add failed: {staged.stderr.strip()}")
    diff = _run(["git", "-C", str(repo), "diff", "--binary", "--cached", base])
    if diff.returncode != 0:
        raise ReplayError(f"git diff failed: {diff.stderr.strip()}")
    return diff.stdout.encode("utf-8")


def _static_check(variant: dict[str, Any], patch_bytes: bytes) -> list[str]:
    """Per-variant marker check: every expected marker is tied to one variant."""

    name = variant["name"]
    failures: list[str] = []
    if name in _FORBIDDEN_NAMES:
        failures.append(f"{name}: duplicates a listed r6/r7/r8 variant name")
    for marker in variant["static_present"]:
        if marker not in patch_bytes:
            failures.append(f"{name}: regenerated patch missing marker {marker!r}")
    for marker in variant["static_absent"]:
        if marker in patch_bytes:
            failures.append(f"{name}: regenerated patch still carries {marker!r}")
    return failures


def _find_result(case_dir: Path) -> Path | None:
    for candidate in (
        case_dir / "verifier" / "verifier-result.json",
        case_dir / "verifier-result.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def _case_actual(result: dict[str, Any]) -> dict[str, Any]:
    hardgate_detail = result.get("hardgate_detail") if isinstance(result.get("hardgate_detail"), dict) else {}
    summary = (
        hardgate_detail.get("hardgate_summary")
        if isinstance(hardgate_detail.get("hardgate_summary"), dict)
        else {}
    )
    public_contract = (
        hardgate_detail.get("public_contract_preserved")
        if isinstance(hardgate_detail.get("public_contract_preserved"), dict)
        else {}
    )
    reward = result.get("reward") if isinstance(result.get("reward"), dict) else {}
    progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
    scope = (result.get("components") or {}).get("migration_scope_complete")
    scope = scope if isinstance(scope, dict) else {}
    probe = result.get("contract_probe") if isinstance(result.get("contract_probe"), dict) else {}
    probe_text = (str(probe.get("stdout") or "") + str(probe.get("stderr") or ""))
    checks = scope.get("checks") if isinstance(scope.get("checks"), list) else []
    missing_diff_ids = sorted(
        {
            str(identifier)
            for row in checks
            if isinstance(row, dict)
            for identifier in (row.get("missing_observation_ids") or [])
            if _DIFF_ID.match(str(identifier))
        }
    )
    failed_probes = sorted(set(_PROBE_FAIL.findall(probe_text)))
    return {
        "status": result.get("status"),
        "hardgate": result.get("hardgate"),
        "branch": reward.get("branch"),
        "reward": reward.get("value"),
        "failed_atoms": summary.get("failed_atoms"),
        "scope_ratio": progress.get("scope_ratio"),
        "scope_status": scope.get("status"),
        "scope_passed": scope.get("passed"),
        "scope_total": scope.get("total"),
        "missing_diff_ids": missing_diff_ids,
        "no_diff_missing": not missing_diff_ids,
        "public_contract_status": public_contract.get("status"),
        "contract_probe_returncode": probe.get("returncode"),
        "failed_probes": failed_probes,
    }


def _validate(
    variant: dict[str, Any], expected: dict[str, Any], actual: dict[str, Any]
) -> list[str]:
    failures: list[str] = []

    def _expect(field: str) -> None:
        if actual.get(field) != expected[field]:
            failures.append(f"{field} {actual.get(field)!r} != {expected[field]!r}")

    for field in (
        "status",
        "branch",
        "reward",
        "hardgate",
        "failed_atoms",
        "scope_ratio",
        "scope_status",
        "scope_passed",
        "scope_total",
        "no_diff_missing",
    ):
        _expect(field)

    # migration-scope completeness: 41/41 PASS with no DIFF-* falsely missing.
    if actual.get("scope_passed") != actual.get("scope_total"):
        failures.append(
            f"scope passed/total {actual.get('scope_passed')}/{actual.get('scope_total')} not complete"
        )
    if actual.get("missing_diff_ids"):
        failures.append(f"DIFF ids falsely missing: {actual.get('missing_diff_ids')}")

    # the independent contract probe must be the thing that catches each attack.
    returncode = actual.get("contract_probe_returncode")
    matched = variant["probe"] in (actual.get("failed_probes") or [])
    if expected["probe_fails"]:
        if returncode in (0, None):
            failures.append(f"contract probe returncode {returncode!r} did not fail")
        if actual.get("public_contract_status") != "FAIL":
            failures.append(
                f"public_contract_preserved status {actual.get('public_contract_status')!r} != 'FAIL'"
            )
        if not matched:
            failures.append(
                f"contract probe stdout did not report the expected probe {variant['probe']!r}; "
                f"got {actual.get('failed_probes')!r}"
            )
    else:
        if returncode not in (0, None):
            failures.append(f"control contract probe returncode {returncode!r} unexpectedly failed")
        if actual.get("failed_probes"):
            failures.append(f"control contract probe reported failures {actual.get('failed_probes')!r}")
    return failures


def _reset_case_dir(cases_root: Path, name: str, image: str) -> None:
    """Remove a prior evidence case dir, tolerating root-owned verifier output.

    The replay container runs as root and writes ``/logs/verifier/*`` into the
    bind-mounted evidence dir, so those files are root-owned and cannot be
    unlinked by the unprivileged runner.  A throwaway container that shares the
    evidence directory removes them; this is filesystem cleanup of the runner's
    own output, never a git operation.
    """

    target = cases_root / name
    if not target.exists():
        return
    shutil.rmtree(target, ignore_errors=True)
    if not target.exists():
        return
    done = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{cases_root}:/cases",
            image,
            "rm",
            "-rf",
            f"/cases/{name}",
        ]
    )
    if done.returncode != 0 or target.exists():
        raise ReplayError(
            f"could not reset evidence case dir {target}: {(done.stderr or done.stdout).strip()}"
        )


def _run_container(
    image: str,
    logs: Path,
    sample_timeout: float,
) -> int:
    argv = [
        "timeout",
        "-k",
        "30",
        str(int(sample_timeout)),
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-add",
        "SYS_ADMIN",
        "--cap-add",
        "NET_ADMIN",
        "--security-opt",
        "apparmor=unconfined",
        "--security-opt",
        "seccomp=unconfined",
        "--memory",
        "4g",
        "--cpus",
        "2",
        "-v",
        f"{logs}:/logs",
        image,
        "/tests/test.sh",
    ]
    with (logs / "container.log").open("w", encoding="utf-8") as handle:
        done = subprocess.run(
            argv,
            check=False,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=sample_timeout + 120,
        )
    return done.returncode


def build_image(image: str, build_timeout: float) -> str:
    done = _run(
        ["timeout", "-k", "30", str(int(build_timeout)), "docker", "build", "-t", image, str(TESTS_DIR)],
        timeout=build_timeout + 120,
    )
    if done.returncode != 0:
        raise ReplayError(
            "docker build failed (rc={}):\n{}\n{}".format(
                done.returncode, done.stdout[-4000:], done.stderr[-2000:]
            )
        )
    inspect = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    return inspect.stdout.strip() if inspect.returncode == 0 else ""


def _hygiene() -> list[str]:
    if not HYGIENE_CHECK.is_file():
        return ["tree_hygiene_selfcheck.py missing"]
    if any(TASK_ROOT.rglob("__pycache__")) or any(TASK_ROOT.rglob("*.pyc")):
        return ["python cache present under task root before hygiene check"]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = _run(["python3", "-B", str(HYGIENE_CHECK)], env=env)
    if done.returncode != 0:
        return [(done.stdout + done.stderr).strip() or "tree hygiene check failed"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="tests image tag")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="ignored evidence directory")
    parser.add_argument("--skip-build", action="store_true", help="reuse an existing tests image")
    parser.add_argument("--build-timeout", type=float, default=900.0, help="docker build timeout (s)")
    parser.add_argument("--sample-timeout", type=float, default=600.0, help="per-sample container timeout (s)")
    parser.add_argument("--only", action="append", default=None, help="run only the named variant(s)")
    args = parser.parse_args(argv)

    for required in (SOLUTION, BUNDLE, TESTS_DIR):
        if not required.exists():
            raise ReplayError(f"required input missing: {required}")

    output = Path(args.output).resolve()
    cases_root = output / "cases"
    cases_root.mkdir(parents=True, exist_ok=True)

    image_id = ""
    if not args.skip_build:
        print(f"== building tests image {args.image} from {TESTS_DIR} ==", flush=True)
        image_id = build_image(args.image, args.build_timeout)
    else:
        inspect = _run(["docker", "image", "inspect", "--format", "{{.Id}}", args.image])
        if inspect.returncode != 0:
            raise ReplayError(f"image {args.image} not present and --skip-build set")
        image_id = inspect.stdout.strip()

    selected = {v["name"] for v in VARIANTS}
    if args.only:
        selected = set(args.only)
        unknown = selected - {v["name"] for v in VARIANTS}
        if unknown:
            raise ReplayError(f"unknown variant(s): {sorted(unknown)}")

    summary: dict[str, Any] = {
        "schema": "stremio-core-1102-r9-adversarial-evidence:v1",
        "task": "Stremio-stremio-core-1102",
        "round": 9,
        "phase": "repair-stage4-adversarial-replay",
        "new_attacks": [v["name"] for v in VARIANTS if v["kind"] == "attack"],
        "controls": [v["name"] for v in VARIANTS if v["kind"] == "control"],
        "forbidden_names_avoided": sorted(_FORBIDDEN_NAMES),
        "image": {"tag": args.image, "id": image_id},
        "docker_run": {
            "network": "none",
            "memory": "4g",
            "cpus": "2",
            "cap_add": ["SYS_ADMIN", "NET_ADMIN"],
            "security_opt": ["apparmor=unconfined", "seccomp=unconfined"],
            "entrypoint": "/tests/test.sh",
            "timeout_sec_per_sample": args.sample_timeout,
        },
        "cases": {},
        "failures": [],
        "ok": False,
    }
    failures: list[str] = summary["failures"]

    work = Path(tempfile.mkdtemp(prefix="stremio1102-r9-"))
    try:
        summary["solution_sha256"] = _sha256(SOLUTION)
        summary["base_bundle_sha256"] = _sha256(BUNDLE)

        for variant in VARIANTS:
            name = variant["name"]
            if name not in selected:
                continue
            expected = _ATTACK_EXPECTED if variant["kind"] == "attack" else _CONTROL_EXPECTED
            case_dir = cases_root / name
            _reset_case_dir(cases_root, name, args.image)
            artifacts = case_dir / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            # A fresh isolated checkout per variant: no shared state and no
            # destructive git command is ever issued, not even in temp paths.
            variant_work = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=work))
            try:
                repo, base_commit = _prepare_checkout(variant_work)
                summary.setdefault("base_commit", base_commit)
                patch_bytes = _build_variant(repo, base_commit, variant)
            finally:
                shutil.rmtree(variant_work, ignore_errors=True)
            (artifacts / "model.patch").write_bytes(patch_bytes)
            static_failures = _static_check(variant, patch_bytes)
            failures.extend(f"{name}: {item}" for item in static_failures)
            print(f"== run {name} ({variant['family']}) ==", flush=True)
            rc = _run_container(args.image, case_dir, args.sample_timeout)
            result_path = _find_result(case_dir)
            actual: dict[str, Any] = {}
            case_failures: list[str] = []
            if result_path is None:
                case_failures.append("no verifier-result.json in evidence")
            else:
                try:
                    loaded = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    case_failures.append(f"verifier-result.json unreadable: {exc}")
                else:
                    actual = _case_actual(loaded)
                    case_failures.extend(_validate(variant, expected, actual))
            if variant["kind"] == "attack" and not (
                isinstance(actual.get("reward"), (int, float)) and actual["reward"] < 1.0
            ):
                case_failures.append(f"attack reward not < 1.0: {actual.get('reward')}")
            if variant["kind"] == "attack" and "public_contract_preserved" not in (
                actual.get("failed_atoms") or []
            ):
                case_failures.append("attack did not fail the public_contract_preserved atom")
            if rc != 0 and result_path is None:
                case_failures.append(f"container exited {rc} without evidence")
            failures.extend(f"{name}: {item}" for item in case_failures)
            summary["cases"][name] = {
                "kind": variant["kind"],
                "family": variant["family"],
                "probe": variant["probe"],
                "model_patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
                "container_exit": rc,
                "expected": expected,
                "actual": actual,
                "ok": not case_failures and not static_failures,
            }
    finally:
        shutil.rmtree(work, ignore_errors=True)

    attack_names = [v["name"] for v in VARIANTS if v["kind"] == "attack"]
    observed_probe_sets = {
        name: summary["cases"][name]["actual"].get("failed_probes", [])
        for name in attack_names
        if name in summary["cases"]
    }
    if set(attack_names).issubset(summary["cases"]):
        observed = sorted({probe for probes in observed_probe_sets.values() for probe in probes})
        expected_probes = sorted({v["probe"] for v in VARIANTS if v["kind"] == "attack"})
        if observed != expected_probes:
            failures.append(
                f"attacks did not exercise the three distinct known probes: {observed} != {expected_probes}"
            )
    summary["observed_probes"] = observed_probe_sets

    hygiene_failures = _hygiene()
    failures.extend(f"hygiene: {item}" for item in hygiene_failures)
    summary["tree_hygiene"] = "PASS" if not hygiene_failures else "FAIL"
    summary["attacks"] = {
        name: ("PASS" if case["ok"] else "FAIL")
        for name, case in summary["cases"].items()
        if case["kind"] == "attack"
    }
    summary["controls"] = {
        name: ("PASS" if case["ok"] else "FAIL")
        for name, case in summary["cases"].items()
        if case["kind"] == "control"
    }
    summary["ok"] = not failures
    summary_path = output / "SUMMARY.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"== wrote {summary_path} ==", flush=True)

    if failures:
        print("R9 REPLAY FAILED:", flush=True)
        for item in failures:
            print("  -", item, flush=True)
        return 1
    print(
        "R9 REPLAY PASSED: "
        f"{len(summary['attacks'])} attack(s) intercepted, "
        f"{len(summary['controls'])} control(s) at expected reward",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
