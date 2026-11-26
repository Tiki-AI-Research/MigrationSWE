from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Mapping

from verifier.harbor_entry import run_harbor_verifier
from verifier.security_selfcheck import run_security_selfcheck


def _apply_model_patch(candidate_root: Path, patch_path: Path) -> tuple[bool, str]:
    if patch_path.is_symlink() or not patch_path.is_file():
        return True, "model patch is missing"
    raw = patch_path.read_bytes()
    if not raw.strip():
        # No bytes means no candidate contribution.  Treating this as "applied"
        # would score the pristine repository (and mis-report its legacy Cobra
        # CLI as a deceptive fallback), so it is a candidate-validity failure.
        return True, "model patch is empty"
    # Candidates are often authored with CRLF while the verifier repository is LF
    # (.gitattributes: * text=auto eol=lf); normalize line endings and ignore
    # whitespace so an otherwise-identical patch still applies.
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    patch = patch_path
    if normalized != raw:
        patch = patch_path.with_name(patch_path.name + ".lf")
        patch.write_bytes(normalized)
    flags = ["--binary", "--ignore-whitespace"]
    check = subprocess.run(
        ["git", "-C", str(candidate_root), "apply", "--check", *flags, str(patch)],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if check.returncode != 0:
        return True, (check.stderr or check.stdout or "model patch is not applicable").strip()[-2000:]
    applied = subprocess.run(
        ["git", "-C", str(candidate_root), "apply", *flags, str(patch)],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if applied.returncode != 0:
        return True, (applied.stderr or applied.stdout or "model patch failed").strip()[-2000:]
    return False, "model patch applied"


def _write_ctrf(output_root: Path, *, task_id: str, base_commit: str, verifier_result: Mapping[str, object]) -> None:
    rows = []
    observations = verifier_result.get("observations", [])
    if isinstance(observations, list):
        for index, row in enumerate(observations):
            if not isinstance(row, Mapping):
                continue
            status = str(row.get("status", "INCONCLUSIVE"))
            ctrf_status = "passed" if status == "PASS" else "failed" if status == "FAIL" else "other"
            kind = str(row.get("kind", "UNKNOWN"))
            group = "p2p" if kind == "P2P" else "f2p" if kind == "F2P" else "diagnostic"
            identifier = str(row.get("id", f"observation-{index + 1}"))
            rows.append({
                "testId": identifier,
                "name": identifier,
                "status": ctrf_status,
                "duration": 0,
                "message": str(row.get("reason", "")),
                "extra": {"deepswe": {"group": group, "required": group in {"p2p", "f2p"}}, "migration": {"kind": kind, "status": status}},
            })
    statuses = ("passed", "failed", "skipped", "pending", "other")
    counts = {status: sum(row["status"] == status for row in rows) for status in statuses}
    payload = {
        "reportFormat": "CTRF",
        "specVersion": "0.1.0",
        "reportId": str(uuid.uuid4()),
        "runId": str(task_id),
        "generatedBy": "migration-grader",
        "results": {
            "tool": {"name": "migration-grader"},
            "summary": {"tests": len(rows), **counts, "start": 0, "stop": 0},
            "tests": rows,
            "extra": {"deepswe": {"taskId": str(task_id), "baseCommit": str(base_commit)}},
        },
    }
    (output_root / "ctrf.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _infrastructure_result(message: str) -> dict[str, object]:
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": "infra_error",
        "hardgate": "INFRA_ERROR",
        "cheating": False,
        "components": {},
        "test_progress": None,
        "observation_contract_errors": [str(message)[-2000:]],
        "observations": [],
        "receipts": {"commands": [], "harness_returncode": None},
        "reward": {
            "branch": "inconclusive",
            "test_weight": 1.0,
            "T": None,
            "value": None,
            "migration_scope_complete_ratio": None,
            "f2p_pass_ratio": None,
            "p2p_pass_ratio": None,
        },
    }


_BUILD_RECEIPT_KINDS = frozenset({"target-build", "target-native-test"})
_COMPILER_SIGNALS = (
    "# package",
    "undefined",
    "cannot use",
    "syntax error",
    "declared and not used",
    "missing return",
)
_AGENT_TIMEOUT_CLASSES = frozenset({"agent_time_out", "agent_timeout", "agent-timeout"})
_GO_BUILD_HEADER = re.compile(r"(?m)^#\s+\S+")


def _has_compiler_signal(text: str) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in _COMPILER_SIGNALS) or bool(_GO_BUILD_HEADER.search(text))


def _finite_number(value: object) -> float | None:
    """Return a finite float, or None when the value is absent or not a number."""

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _ratio(value: object) -> float | None:
    number = _finite_number(value)
    if number is None or not 0.0 <= number <= 1.0:
        return None
    return number


def _reward_mapping(result: Mapping[str, object]) -> dict[str, object]:
    reward = result.get("reward")
    return dict(reward) if isinstance(reward, Mapping) else {}


def _receipts(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    receipts = result.get("receipts")
    if not isinstance(receipts, Mapping):
        return []
    commands = receipts.get("commands")
    if not isinstance(commands, list):
        return []
    return [row for row in commands if isinstance(row, Mapping)]


def _hardgate_status(result: Mapping[str, object]) -> str:
    value = result.get("hardgate")
    if isinstance(value, Mapping):
        value = value.get("status")
    return str(value or "").strip().upper()


def _diagnostic_text(result: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in (
        "candidate_patch_error",
        "candidate_execution_error",
        "candidate_harness_error",
        "observation_contract_errors",
    ):
        value = result.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value if isinstance(item, str))
    for receipt in _receipts(result):
        for key in ("stderr", "stdout", "diagnostic"):
            value = receipt.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _build_pass_failed(result: Mapping[str, object]) -> bool:
    candidates: list[Mapping[str, object]] = [result]
    for key in ("hardgate_detail", "evidence"):
        nested = result.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    hardgate = result.get("hardgate")
    if isinstance(hardgate, Mapping):
        candidates.append(hardgate)
    for candidate in candidates:
        if "build_pass" not in candidate:
            continue
        value = candidate["build_pass"]
        status = value.get("status") if isinstance(value, Mapping) else value
        if str(status or "").strip().upper() == "FAIL":
            return True
    return False


def _build_failed(result: Mapping[str, object], receipts: list[Mapping[str, object]]) -> bool:
    for receipt in receipts:
        if str(receipt.get("kind")) not in _BUILD_RECEIPT_KINDS:
            continue
        if receipt.get("timed_out") is True or receipt.get("infrastructure_error") is True:
            continue
        returncode = receipt.get("returncode")
        if isinstance(returncode, int) and not isinstance(returncode, bool) and returncode != 0:
            return True
    return _build_pass_failed(result)


def _error_flags(result: Mapping[str, object]) -> dict[str, bool]:
    """Single deterministic attribution of infrastructure, timeout and build errors."""

    receipts = _receipts(result)
    infrastructure = (
        str(result.get("status", "")).strip().lower() == "infra_error"
        or _hardgate_status(result) == "INFRA_ERROR"
        or result.get("infrastructure_error") is True
        or bool(result.get("configuration_error"))
        or any(receipt.get("timed_out") is True for receipt in receipts)
    )
    agent_timeout = (
        result.get("agent_time_out") is True
        or result.get("agent_timeout") is True
        or str(result.get("failure_class", "")).strip().lower() in _AGENT_TIMEOUT_CLASSES
    )
    build_error = _build_failed(result, receipts)
    compile_error = build_error and _has_compiler_signal(_diagnostic_text(result))
    return {
        "infrastructure_error": bool(infrastructure),
        "agent_time_out": bool(agent_timeout),
        "build error": bool(build_error),
        "compile error": bool(compile_error),
    }


def _reward_value(result: Mapping[str, object]) -> float:
    number = _finite_number(_reward_mapping(result).get("value"))
    if number is None:
        raise ValueError("reward value is missing or not a finite number")
    return number


def _harbor_reward_number(value: float | None) -> float:
    """Coerce an unmeasurable metric to ``0.0`` for the Harbor-facing reward file.

    Harbor's ``_parse_reward_json`` rejects any non-numeric value, including
    ``null``, with ``VerifierOutputParseError``.  That would turn a genuine
    candidate failure into an infrastructure-looking exception and hide the real
    attribution.  The top-level ``reward.json`` therefore carries numbers only;
    the missing measurement is preserved verbatim in ``reward-details.json``,
    which is what diagnostics are read from.
    """

    return 0.0 if value is None else value


def _reward_payload(result: Mapping[str, object]) -> dict[str, object]:
    """Verifier-only reward.json payload.

    An unmeasurable run yields an empty map.  Every other branch stays numeric
    so Harbor can always parse it; see ``_harbor_reward_number``.
    """

    if _error_flags(result)["infrastructure_error"] and not result.get("cheating"):
        # An unmeasurable run is only unmeasurable while nothing was proven about
        # the candidate.  A detected cheat is a *measurement*: the penalty has to
        # reach Harbor even when the same run also hit an infrastructure error,
        # otherwise the environment failure launders the -1 away.
        return {}
    reward = _reward_mapping(result)
    return {
        "test": _harbor_reward_number(_finite_number(reward.get("T"))),
        "migration_scope_complete_ratio": _harbor_reward_number(
            _ratio(reward.get("migration_scope_complete_ratio"))
        ),
        "f2p_pass_ratio": _harbor_reward_number(_ratio(reward.get("f2p_pass_ratio"))),
        "reward": _reward_value(result),
    }


def _reward_details_payload(result: Mapping[str, object]) -> dict[str, object]:
    reward = _reward_mapping(result)
    flags = _error_flags(result)
    payload: dict[str, object] = {
        "cheating_detected": bool(result.get("cheating")),
        "test": _finite_number(reward.get("T")),
        "migration_scope_complete_ratio": _ratio(reward.get("migration_scope_complete_ratio")),
        "f2p_pass_ratio": _ratio(reward.get("f2p_pass_ratio")),
        "p2p_pass_ratio": _ratio(reward.get("p2p_pass_ratio")),
        "reward": (
            None
            if flags["infrastructure_error"] and not result.get("cheating")
            else _reward_value(result)
        ),
        "errors": flags,
    }
    branch = reward.get("branch")
    if isinstance(branch, str):
        payload["branch"] = branch
    failure_class = result.get("failure_class")
    if isinstance(failure_class, str):
        payload["failure_class"] = failure_class
    return payload


def _exit_code(result: Mapping[str, object]) -> int:
    """0 measured, 1 candidate failure, 2 infrastructure or configuration error."""

    if _error_flags(result)["infrastructure_error"] and not result.get("cheating"):
        return 2
    if str(result.get("status", "")).strip().lower() == "success" and _hardgate_status(result) == "PASS":
        return 0
    return 1


def _validated_result(result: object) -> dict[str, object]:
    """Normalise a verifier result so every report payload satisfies the contract."""

    if not isinstance(result, Mapping):
        return _infrastructure_result("verifier returned a non-object result")
    normalised = dict(result)
    try:
        _reward_payload(normalised)
        _reward_details_payload(normalised)
    except (TypeError, ValueError) as exc:
        return _infrastructure_result(f"reward report contract violation: {exc}")
    return normalised


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_reward_artifacts(output: Path, result: Mapping[str, object]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "reward-details.json", _reward_details_payload(result))
    _write_json(output / "reward.json", _reward_payload(result))


def _write_verifier_result(output: Path, result: Mapping[str, object]) -> Path:
    path = output / "verifier-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, result)
    return path


def _candidate_source(config: Mapping[str, object]) -> Path:
    runtime = config.get("runtime")
    if not isinstance(runtime, Mapping):
        target = config.get("target_environment")
        runtime = target.get("runtime") if isinstance(target, Mapping) else None
    workdir = runtime.get("workdir") if isinstance(runtime, Mapping) else None
    for raw in (workdir, "/workspace/repo", "/app"):
        if not isinstance(raw, str) or not raw.startswith("/"):
            continue
        path = Path(raw)
        if path.is_dir() and not path.is_symlink() and any(path.iterdir()):
            return path
    raise RuntimeError("Harbor candidate repository was not found")


def main(bundle: Path = Path("/tests"), output: Path = Path("/logs/verifier")) -> int:
    task_id = "unknown"
    base_commit = ""
    try:
        # Re-verify the verifier's own enforcement wiring before scoring: a
        # self-check failure must never be reported as a candidate score.
        run_security_selfcheck()
        config = json.loads((bundle / "config.json").read_text(encoding="utf-8"))
        if not isinstance(config, Mapping):
            raise ValueError("Harbor config must be a JSON object")
        harbor = config.get("harbor", config)
        if not isinstance(harbor, Mapping):
            harbor = config
        task_id = str(harbor.get("task_id", config.get("task_id", "unknown")))
        base_commit = str(config.get("base_commit", ""))
        candidate = Path("/tmp/harbor-candidate")
        if candidate.exists():
            shutil.rmtree(candidate)
        shutil.copytree(_candidate_source(config), candidate, symlinks=True)
        patch_failed, patch_message = _apply_model_patch(candidate, Path("/logs/artifacts/model.patch"))
        result = run_harbor_verifier(
            candidate_root=candidate,
            bundle_root=bundle,
            logs_root=output,
            apply_failed=patch_failed,
            patch_message=patch_message,
        )
    except Exception as exc:
        result = _infrastructure_result(str(exc))

    result = _validated_result(result)
    try:
        _write_verifier_result(output, result)
        _write_reward_artifacts(output, result)
    except Exception as exc:
        print(f"grader: failed to write required verifier reports: {exc}", file=sys.stderr)
        return 2
    try:
        _write_ctrf(
            output,
            task_id=task_id,
            base_commit=base_commit,
            verifier_result=result,
        )
    except Exception as exc:
        print(f"grader: failed to write optional ctrf report: {exc}", file=sys.stderr)
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
