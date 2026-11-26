"""Direct Harbor verifier entry; this module never starts Docker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .baseline import BaselineError, load_source_baseline
from .comparison import merge_comparison_status
from .dependency_graph import audit_dependency_graph
from .differential import compare
from .evidence import ConfigurationError, build_hardgate_evidence
from .hardgate_engine import evaluate_hardgate
from .integrity import load_equivalence
from .observations import normalize_observation_report, observation_contract_errors
from .state_preservation import (
    ApplicabilityError,
    load_migration_contract,
    resolve_state_applicability,
)
from .verifier_engine import evaluate_verifier
from .unit_checks import assertion_helper_imports


class InfrastructureError(RuntimeError):
    """The evaluator itself cannot produce trustworthy evidence."""


_CONFIGURATION_FILES = ("config.json", "grader.py", "runtime-manifest.json")
_DEFAULT_PNPM_STORE = "/opt/migration/pnpm-store"
_FORMAT_OBSERVATION_IDS = (
    "P2P-007",
    "P2P-008",
    "P2P-011",
    "P2P-014",
    "P2P-021",
    "P2P-022",
    "P2P-038",
    "P2P-044",
    "P2P-051",
)
_CROSS_INPUTS_SCHEMA = "migration-format-cross-inputs:v1"
FORMAT_EVIDENCE_ASSET = "format-evidence.json"
FORMAT_EVIDENCE_SCHEMA = "migration-target-format-evidence:v1"
_EVALUATOR_NODE_MODULES = "/opt/migration/dependencies/node_modules"


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
        env={**environment},
    )


def _run_with_receipt(
    command: list[str],
    *,
    kind: str,
    index: int,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[subprocess.CompletedProcess[str] | None, dict[str, object]]:
    receipt: dict[str, object] = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": kind,
        "index": index,
        "command": list(command),
        "returncode": None,
        "timed_out": False,
        "signal": None,
        "failed_phase": None,
        "report_present": False,
    }
    try:
        completed = _run(command, cwd=cwd, timeout=timeout, environment=environment)
    except subprocess.TimeoutExpired as exc:
        receipt["timed_out"] = True
        receipt["failed_phase"] = "timeout"
        receipt["diagnostic"] = str(exc)[-2000:]
        return None, receipt
    except OSError as exc:
        receipt["infrastructure_error"] = True
        receipt["failed_phase"] = "launch"
        receipt["diagnostic"] = str(exc)[-2000:]
        return None, receipt
    receipt["returncode"] = completed.returncode
    if completed.returncode < 0:
        receipt["signal"] = -completed.returncode
        receipt["failed_phase"] = "signal"
    elif completed.returncode != 0:
        receipt["failed_phase"] = "exit"
    # Some tools (notably pnpm with ``--reporter=append-only``) write the
    # actionable failure to stdout rather than stderr.  Dropping stdout made
    # dependency conflicts impossible to locate from the receipt.
    if completed.stdout:
        receipt["stdout"] = completed.stdout[-8000:]
    if completed.stderr:
        receipt["stderr"] = completed.stderr[-4000:]
    return completed, receipt


# The upstream sparse-array test measures a one-off wall-clock duration.  On a
# loaded host the same correct implementation can exceed the canonical 100 ms
# bound (observed 112-124 ms while the host load average was far above its core
# count), while the implementation completes in a few milliseconds when idle.
# A single such failure is a host artefact, not a migration failure, so the
# verifier retries the command instead of relaxing the bound.  The retry is
# deliberately narrow: the sparse-array test must be the only failing test,
# every assertion payload must be the wall-clock message, and the measured
# duration must be plausible jitter.  A genuinely slow implementation fails
# every attempt and still fails the gate.  The passing attempt is the only
# receipt recorded; earlier attempts are preserved in ``timing_jitter_retries``.
_TIMING_JITTER_TEST = "handles very sparse arrays efficiently"
_TIMING_JITTER_LIMIT_MS = 2_000.0
_TIMING_JITTER_MAX_ATTEMPTS = 3
_TIMING_JITTER_KINDS = frozenset({"target-native-test", "test-execution"})
_TIMING_JITTER_ASSERTION = re.compile(
    r"(?:uneval|stringify) took (?P<elapsed>\d+(?:\.\d+)?)ms, "
    r"expected < \d+ms"
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _timing_jitter_failure(
    receipt: Mapping[str, object],
) -> list[float] | None:
    """Return retryable wall-clock durations, or ``None`` for a real failure.

    The verifier must not turn host scheduling noise into a zero reward.  The
    caller may retry the command when this returns durations, but acceptance
    still requires an actual passing run: a consistently slow implementation
    (or any other failing test) is not masked.
    """

    if receipt.get("kind") not in _TIMING_JITTER_KINDS:
        return None
    text = _ANSI_ESCAPE.sub("", str(receipt.get("stderr") or ""))
    if _TIMING_JITTER_TEST not in text:
        return None
    durations = [
        float(match.group("elapsed"))
        for match in _TIMING_JITTER_ASSERTION.finditer(text)
    ]
    if not durations or any(
        value > _TIMING_JITTER_LIMIT_MS for value in durations
    ):
        return None
    failed = re.search(r"Failed Tests?\s+(\d+)", text)
    if failed is not None and int(failed.group(1)) != len(durations):
        return None
    failing_lines = [
        line for line in text.splitlines() if re.search(r"\bFAIL\b", line)
    ]
    if failing_lines and not all(
        _TIMING_JITTER_TEST in line for line in failing_lines
    ):
        return None
    assertion_lines = [
        line for line in text.splitlines() if "AssertionError" in line
    ]
    if not assertion_lines or not all(
        _TIMING_JITTER_ASSERTION.search(line) for line in assertion_lines
    ):
        return None
    return durations


def _run_declared_commands(
    commands: Sequence[tuple[str, Sequence[str]]],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
    start_index: int = 1,
    identity: tuple[int, int] | None = None,
    candidate_owned: bool = True,
    retry_reset: Callable[[], None] | None = None,
) -> tuple[list[dict[str, object]], bool, bool]:
    receipts: list[dict[str, object]] = []
    for offset, (kind, raw) in enumerate(commands):
        command = [str(item) for item in raw]
        if identity is not None:
            command = _run_as_candidate(command, identity=identity)
        history: list[dict[str, object]] = []
        attempts = 0
        while True:
            attempts += 1
            if attempts > 1 and retry_reset is not None:
                retry_reset()
            completed, receipt = _run_with_receipt(
                command,
                kind=kind,
                index=start_index + offset,
                cwd=cwd,
                timeout=timeout,
                environment=environment,
            )
            if completed is None:
                if history:
                    receipt["timing_jitter_retries"] = history
                    receipt["timing_jitter_attempt_count"] = attempts
                receipts.append(receipt)
                if receipt.get("infrastructure_error") is True:
                    return receipts, False, True
                if candidate_owned:
                    return receipts, True, False
                return receipts, False, True
            if completed.returncode == 0:
                if history:
                    receipt["timing_jitter_retries"] = history
                    receipt["timing_jitter_attempt_count"] = attempts
                break
            jitter = _timing_jitter_failure(receipt)
            if jitter is None or attempts >= _TIMING_JITTER_MAX_ATTEMPTS:
                if history:
                    receipt["timing_jitter_retries"] = history
                    receipt["timing_jitter_attempt_count"] = attempts
                receipts.append(receipt)
                return receipts, True, False
            history.append(
                {
                    "attempt": attempts,
                    "returncode": completed.returncode,
                    "durations_ms": jitter,
                }
            )
        receipts.append(receipt)
    return receipts, False, False


def _receipt_failed(receipt: Mapping[str, object]) -> bool:
    """Return whether a receipt records a real command failure."""

    if receipt.get("infrastructure_error") is True:
        return True
    if receipt.get("timed_out") or receipt.get("failed_phase") is not None:
        return True
    returncode = receipt.get("returncode")
    return (
        isinstance(returncode, int)
        and not isinstance(returncode, bool)
        and returncode != 0
    )


_MAX_RECEIPT_DIAGNOSTIC = 1500


def _receipt_diagnostic_tail(receipt: Mapping[str, object]) -> str:
    """Return the actionable tail of a failed command's captured output."""

    chunks: list[str] = []
    for field in ("stdout", "stderr"):
        value = receipt.get(field)
        if isinstance(value, str) and value.strip():
            chunks.append(value.strip())
    if not chunks:
        return ""
    lines = [
        line.rstrip()
        for chunk in chunks
        for line in chunk.splitlines()
        if line.strip()
    ]
    text = "\n".join(lines[-12:])
    if len(text) > _MAX_RECEIPT_DIAGNOSTIC:
        text = text[-_MAX_RECEIPT_DIAGNOSTIC:]
    return text


def _candidate_execution_error(
    message: str, receipts: Sequence[Mapping[str, object]]
) -> str:
    """Describe the first real candidate-owned command failure."""

    if not receipts:
        return message
    # A later successful dependency-graph receipt must not overwrite the
    # command that actually failed (for example target-build rc=1 followed by
    # dependency-graph rc=0).  Prefer the first failing receipt and fall back to
    # the last receipt only when every receipt looks successful.
    receipt = next(
        (value for value in receipts if _receipt_failed(value)),
        receipts[-1],
    )
    kind = str(receipt.get("kind", "candidate command"))
    phase = receipt.get("failed_phase")
    if phase == "timeout":
        detail = f"{message}: {kind} timed out"
    elif phase == "signal":
        detail = (
            f"{message}: {kind} terminated by signal {receipt.get('signal')}"
        )
    else:
        returncode = receipt.get("returncode")
        if isinstance(returncode, int) and not isinstance(returncode, bool):
            detail = f"{message}: {kind} exited with code {returncode}"
        else:
            detail = f"{message}: {kind} did not complete"
    output = _receipt_diagnostic_tail(receipt)
    if output:
        # Keep the stable prefix above intact; the command's own diagnostics
        # (for example pnpm's conflicting specifier and version) follow.
        detail = f"{detail}\n{output}"
    return detail


def _annotate_report_presence(
    receipt: dict[str, object], report: Path
) -> dict[str, object]:
    """Record report availability independently from the process outcome."""

    present = report.is_file()
    receipt["report_present"] = present
    if not present:
        receipt["report_missing"] = True
        if receipt.get("failed_phase") is None:
            receipt["failed_phase"] = "no-report"
    return receipt


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _load_migration_units(
    assets: Path, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    assets_config = config.get("assets")
    if not isinstance(assets_config, Mapping):
        raise ValueError("config.assets is missing")
    name = assets_config.get("migration_units")
    if not isinstance(name, str) or not name:
        raise ValueError("config.assets.migration_units is missing")
    payload = _load_json(assets / name)
    rows = payload.get("units")
    if not isinstance(rows, list) or not rows:
        raise ValueError("migration units must be a non-empty array")
    units: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"migration unit {index} must be an object")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"migration unit {index} has an invalid id")
        if identifier in identifiers:
            raise ValueError(f"migration units contain a duplicate id: {identifier}")
        identifiers.add(identifier)
        units.append(dict(row))
    return units


def _load_protected_test_assets(
    assets: Path, config: Mapping[str, Any]
) -> dict[str, Any]:
    assets_config = config.get("assets")
    if not isinstance(assets_config, Mapping):
        raise ValueError("config.assets is missing")
    baseline_name = assets_config.get("source_test_baseline")
    if not isinstance(baseline_name, str) or not baseline_name:
        raise ValueError("config.assets.source_test_baseline is missing")
    baseline = _load_json(assets / baseline_name)
    if baseline.get("schema_version") != "migration-source-test-baseline:v1":
        raise ValueError("source test baseline has an unsupported schema")
    if not isinstance(baseline.get("files"), Mapping) or not baseline["files"]:
        raise ValueError("source test baseline must declare test files")
    equivalence_name = assets_config.get("source_test_equivalence")
    if not isinstance(equivalence_name, str) or not equivalence_name:
        raise ValueError("config.assets.source_test_equivalence is missing")
    load_equivalence(assets / equivalence_name, baseline=baseline)
    return baseline


def _write_result(logs: Path, result: dict[str, object]) -> dict[str, object]:
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "verifier-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _atom(status: str, source: str, evidence: object = None) -> dict[str, object]:
    return {
        "status": status,
        "source": source,
        "evidence": [] if evidence is None else evidence,
    }


def _infrastructure_evidence(message: str) -> dict[str, object]:
    """Build an explicitly evaluator-owned hardgate evidence set."""

    atom = _atom("INFRA_ERROR", "evaluator-infrastructure", [message])
    return {
        "observation_contract_valid": dict(atom),
        "isolation_enforced": dict(atom),
        "patch_applicable": dict(atom),
        "build_pass": dict(atom),
        "dependency_resolved": dict(atom),
        "no_deceptive_source_fallback": dict(atom),
        "p2p_pass": dict(atom),
        "public_contract_preserved": dict(atom),
        "critical_data_preserved": dict(atom),
        "tests_not_tampered": dict(atom),
        "no_new_skip_or_xfail": dict(atom),
        "protected_paths_unchanged": dict(atom),
        "anti_cheating_pass": dict(atom),
        "source_hits": [],
        "ordinary_residue_hits": [],
        "deceptive_fallback_hits": [],
        "cheating": False,
        "evidence_mode": "derived",
    }


def _finish_result(
    *,
    config: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    hardgate: Mapping[str, Any],
    receipts: Sequence[Mapping[str, object]],
    p2p: Sequence[Mapping[str, Any]] = (),
    f2p: Sequence[Mapping[str, Any]] = (),
    differential: Sequence[Mapping[str, Any]] = (),
    state: Mapping[str, Any] | None = None,
    observations: Sequence[Mapping[str, Any]] = (),
    observation_errors: Sequence[str] = (),
    message: str = "",
    harness_returncode: int | None = None,
    isolation: Mapping[str, object] | None = None,
    candidate_root: Path | None = None,
    not_run_reason: str | None = None,
    diagnostic: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Run the verifier aggregator and keep every output on one verdict."""

    state_value = dict(state) if isinstance(state, Mapping) else {}
    if "applicable" not in state_value:
        state_value["applicable"] = False
    state_value.setdefault("checks", [])
    result = evaluate_verifier(
        hardgate=hardgate,
        migration_units=units,
        unit_checks=config.get("migration_unit_checks"),
        p2p=p2p,
        f2p=f2p,
        differential=differential,
        state=state_value,
        weights=config.get("weights", {}),
        applicability=config.get("group_applicability"),
        candidate_root=candidate_root,
        not_run_reason=not_run_reason,
    )
    if "applicability" in state_value:
        result["state_applicability"] = state_value["applicability"]
    result["hardgate_result"] = dict(hardgate)
    result["observations"] = [dict(row) for row in observations]
    result["observation_contract_errors"] = list(observation_errors)
    result["cheating"] = bool(hardgate.get("cheating"))
    result["cheat_level"] = str(hardgate.get("cheat_level", "L0"))
    if hardgate.get("status") != "PASS":
        result["reward_override"] = hardgate.get("reward_override")
    if message:
        result["candidate_execution_error"] = message
    if isolation is not None:
        result["isolation"] = dict(isolation)
    result["receipts"] = {
        "commands": [dict(receipt) for receipt in receipts],
        "harness_returncode": harness_returncode,
    }
    if not_run_reason is not None:
        result["not_run_reason"] = not_run_reason
        result.setdefault("f2p_status", "NOT_RUN")
        result.setdefault("p2p_status", "NOT_RUN")
    if diagnostic is not None:
        result["diagnostic"] = dict(diagnostic)
        result["diagnostic_only"] = bool(diagnostic.get("diagnostic_only", True))
        result["eligible_for_graded"] = False
    else:
        result.setdefault("diagnostic_only", False)
    return result


def _infrastructure_result(
    message: str,
    *,
    candidate: Path,
    config: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]] = (),
    receipts: Sequence[Mapping[str, object]] = (),
    observations: Sequence[Mapping[str, Any]] = (),
    observation_errors: Sequence[str] = (),
    harness_returncode: int | None = None,
    isolation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    hardgate = evaluate_hardgate(candidate, _infrastructure_evidence(message))
    errors = list(observation_errors) or [message]
    return _finish_result(
        config=config,
        units=units,
        hardgate=hardgate,
        receipts=receipts,
        observations=observations,
        observation_errors=errors,
        message=message,
        harness_returncode=harness_returncode,
        isolation=isolation,
    )


def _candidate_evidence_result(
    message: str,
    *,
    candidate: Path,
    config: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    test_baseline: Mapping[str, Any] | None,
    receipts: Sequence[Mapping[str, object]],
    observations: Sequence[Mapping[str, Any]] = (),
    observation_errors: Sequence[str] = (),
    patch_applicable: bool = True,
    isolation: Mapping[str, object] | None = None,
    harness_returncode: int | None = None,
    execution_report: Path | None = None,
    not_run_reason: str | None = None,
    diagnostic: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not observations and not_run_reason is None:
        not_run_reason = (
            message
            or "candidate execution did not produce F2P/P2P observations"
        )
    try:
        evidence = build_hardgate_evidence(
            candidate,
            config=config,
            observations=observations,
            observation_errors=observation_errors,
            receipts=receipts,
            patch_applicable=patch_applicable,
            source_test_baseline=test_baseline,
            isolation=isolation,
            execution_report=execution_report,
        )
    except ConfigurationError as exc:
        return _infrastructure_result(
            str(exc),
            candidate=candidate,
            config=config,
            units=units,
            receipts=receipts,
            observations=observations,
            observation_errors=observation_errors,
            harness_returncode=harness_returncode,
            isolation=isolation,
        )
    hardgate = evaluate_hardgate(candidate, evidence)
    return _finish_result(
        config=config,
        units=units,
        hardgate=hardgate,
        receipts=receipts,
        observations=observations,
        observation_errors=observation_errors,
        message=message,
        harness_returncode=harness_returncode,
        isolation=isolation,
        not_run_reason=not_run_reason,
        diagnostic=diagnostic,
    )


def _asset_preflight(
    assets: Path,
    runtime: Mapping[str, Any],
    timeout: float,
    environment: Mapping[str, str],
) -> dict[str, object]:
    harness = runtime.get("harness_command")
    if not isinstance(harness, Sequence) or isinstance(harness, (str, bytes)) or not harness:
        raise InfrastructureError("target runtime harness command is missing")
    command = [
        str(item).format_map(
            {
                "assets": str(assets),
                "verifier": str(assets / "verifier"),
                "report": "/logs/result.json",
                "repo": str(assets),
            }
        )
        for item in harness
    ]
    command.append("--preflight")
    completed, receipt = _run_with_receipt(
        command,
        kind="environment-build",
        index=1,
        cwd=assets,
        timeout=timeout,
        environment=environment,
    )
    if completed is None:
        raise InfrastructureError("evaluation asset preflight could not be executed")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise InfrastructureError(
            f"evaluation asset preflight failed with exit {completed.returncode}: {detail}"
        )
    return receipt


def _protected_fingerprints(bundle: Path) -> dict[str, str]:
    import hashlib

    values: dict[str, str] = {}
    for relative in _CONFIGURATION_FILES:
        path = bundle / relative
        if path.is_file():
            values[relative] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted((bundle / "verifier").glob("*.py")):
        if path.is_file():
            values[path.relative_to(bundle).as_posix()] = (
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            )
    return values


def _candidate_identity() -> tuple[int, int] | None:
    if os.geteuid() != 0:
        return None
    try:
        uid = int(os.environ.get("MIGRATION_CANDIDATE_UID", "65532"))
        gid = int(os.environ.get("MIGRATION_CANDIDATE_GID", str(uid)))
    except ValueError:
        return None
    return uid, gid


_PUBLIC_ASSET_NAMES = (
    "protocol.mjs",
    "candidate-runner.mjs",
    "replacer-cases.mjs",
)


def _prepare_public_assets(assets: Path, logs: Path) -> Path:
    """Expose only the candidate protocol to the isolated candidate identity.

    Hidden harnesses, fixtures, source/target baselines and assertion logic
    remain inside the evaluator-owned bundle.  The candidate gets only the
    protocol files (including the fixed replacer scenarios that must run in the
    candidate process because callbacks cannot cross the protocol).
    """

    target = Path("/opt/migration/public-assets")
    if not os.access(target.parent, os.W_OK):
        target = logs.parent / "public-assets"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    source = assets / "public"
    if not source.is_dir():
        raise InfrastructureError("the candidate public protocol assets are missing")
    copied: set[str] = set()
    for name in _PUBLIC_ASSET_NAMES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise InfrastructureError(
                f"the candidate public protocol asset {name} is missing or unsafe"
            )
        shutil.copy2(path, target / name)
        copied.add(name)
    unexpected = sorted(
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() or path.is_symlink()
    )
    unexpected = [name for name in unexpected if name not in copied]
    if unexpected:
        raise InfrastructureError(
            "candidate public assets contain unexpected entries: "
            + ", ".join(unexpected[:8])
        )
    subprocess.run(["chmod", "-R", "a+rX", str(target)], check=False)
    return target


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _format_expectations(
    assets: Path,
    config: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Load the frozen target wire-format corpus.

    The source runtime is an older major version: it predates several value
    types and stringifyAsync.  Target format evidence is therefore frozen from
    the authoritative target package, while source payloads are used only to
    prove that the target still consumes the subset the source could emit.
    """

    configured = config.get("assets")
    name = (
        configured.get("format_evidence")
        if isinstance(configured, Mapping)
        else None
    )
    path = assets / str(name or FORMAT_EVIDENCE_ASSET)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BaselineError(f"format evidence asset is missing: {path}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"format evidence asset is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise BaselineError("format evidence asset must be a JSON object")
    if payload.get("schema_version") != FORMAT_EVIDENCE_SCHEMA:
        raise BaselineError(
            "format evidence schema_version must be "
            f"{FORMAT_EVIDENCE_SCHEMA}"
        )
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BaselineError("format evidence cases must be a non-empty array")
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in cases:
        if not isinstance(row, Mapping):
            raise BaselineError("format evidence case must be an object")
        identifier = row.get("observation_id")
        if identifier not in _FORMAT_OBSERVATION_IDS:
            raise BaselineError(
                f"format evidence has an unexpected observation id: {identifier!r}"
            )
        if identifier in by_id:
            raise BaselineError(
                f"format evidence contains duplicate observation id: {identifier}"
            )
        if not isinstance(row.get("case_id"), str) or not row["case_id"]:
            raise BaselineError(
                f"format evidence case {identifier} has no case id"
            )
        kind = row.get("kind")
        if kind == "value":
            for key in ("payload", "parse", "unflatten", "uneval_value"):
                if key not in row:
                    raise BaselineError(
                        f"format evidence case {identifier} is missing {key}"
                    )
            if row.get("async_payload") is not None and "async_parse" not in row:
                raise BaselineError(
                    f"format evidence case {identifier} is missing async_parse"
                )
        elif kind in {"error", "malformed-parse", "malformed-unflatten"}:
            error = row.get("error")
            if not isinstance(error, Mapping) or not {
                "threw",
                "name",
                "message",
            } <= set(error):
                raise BaselineError(
                    f"format evidence case {identifier} lacks error semantics"
                )
        else:
            raise BaselineError(
                f"format evidence case {identifier} has unsupported kind: {kind!r}"
            )
        by_id[identifier] = row
    missing = sorted(set(_FORMAT_OBSERVATION_IDS) - set(by_id))
    if missing:
        raise BaselineError(
            "format evidence is missing required ids: " + ", ".join(missing)
        )
    return by_id


def _source_format_inputs(
    baseline: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Extract the source-era format records from the protected baseline."""

    values: dict[str, Mapping[str, Any]] = {}
    for identifier in _FORMAT_OBSERVATION_IDS:
        row = baseline.get("by_id", {}).get(identifier)
        if not isinstance(row, Mapping):
            raise BaselineError(
                f"source baseline is missing format observation {identifier}"
            )
        envelope = row.get("value")
        content = envelope.get("value") if isinstance(envelope, Mapping) else None
        fmt = content.get("format") if isinstance(content, Mapping) else None
        if not isinstance(fmt, Mapping):
            raise BaselineError(
                f"source baseline observation {identifier} lacks format evidence"
            )
        values[identifier] = fmt
    return values


def _cross_input_payload(
    source_inputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for identifier in _FORMAT_OBSERVATION_IDS:
        fmt = source_inputs[identifier]
        case: dict[str, object] = {
            "id": fmt.get("case_id"),
            "case_id": fmt.get("case_id"),
            "observation_id": identifier,
            "kind": fmt.get("kind"),
        }
        if "payload" in fmt:
            case["payload"] = fmt["payload"]
            if fmt.get("async_payload") is not None:
                case["async_payload"] = fmt["async_payload"]
            if isinstance(fmt.get("uneval_source"), str):
                case["uneval_source"] = fmt["uneval_source"]
        elif "input" in fmt:
            case["input"] = fmt["input"]
            case["wrapped"] = bool(fmt.get("wrapped"))
        elif "parse_payload" in fmt:
            case["parse_payload"] = fmt["parse_payload"]
        elif "unflatten_value" in fmt:
            case["unflatten_value"] = fmt["unflatten_value"]
        else:
            # The source-era runtime cannot express this value shape.  It
            # contributes no backward-compatibility input, so the target never
            # has to consume a payload that never existed.
            continue
        cases.append(case)
    return {
        "schema_version": _CROSS_INPUTS_SCHEMA,
        "digest": _canonical_digest(cases),
        "cases": cases,
    }


def _write_cross_inputs(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _format_projection(
    fmt: Mapping[str, Any],
    *,
    target_evidence: bool = True,
) -> dict[str, Any]:
    """Project a format record onto its observable-contract fields.

    Frozen expectations also carry evaluator bookkeeping such as
    ``observation_id`` and the source-consumption matrix.  Those are never part
    of the candidate's public format record, so the projection is an explicit
    allow-list rather than a copy of every non-presentation field.

    ``target_evidence`` selects the comparison baseline.  The protected 4.3.3
    baseline predates ``stringifyAsync`` and the evaluator cross-inputs, so a
    preserved-contract comparison cannot require those fields from the source
    side.  They stay in the target-evidence comparison, where the frozen 6.0.0
    corpus does enforce them.
    """

    kind = fmt.get("kind")
    if kind in {"malformed-parse", "malformed-unflatten"}:
        # The public contract for malformed payloads is rejection, not one
        # particular exception name or message.
        error = fmt.get("error")
        return {
            "case_id": fmt.get("case_id"),
            "kind": kind,
            "parse_payload": fmt.get("parse_payload"),
            "unflatten_value": fmt.get("unflatten_value"),
            "error_threw": (
                bool(error.get("threw")) if isinstance(error, Mapping) else False
            ),
        }
    if kind == "error":
        error = fmt.get("error") if isinstance(fmt.get("error"), Mapping) else {}
        return {
            "case_id": fmt.get("case_id"),
            "kind": kind,
            "input": fmt.get("input"),
            "wrapped": bool(fmt.get("wrapped")),
            "error": {key: error.get(key) for key in ("threw", "name", "path")},
            "path_matches": bool(fmt.get("path_matches")),
        }
    # `uneval` source text is not a public wire contract: different but
    # equivalent JavaScript is acceptable, and its evaluated value is checked.
    projected = {
        "case_id": fmt.get("case_id"),
        "kind": kind,
        "payload": fmt.get("payload"),
        "async_payload": fmt.get("async_payload"),
        "parse": fmt.get("parse"),
        "unflatten": fmt.get("unflatten"),
        "async_parse": fmt.get("async_parse"),
        "uneval_value": fmt.get("uneval_value"),
        "cross": fmt.get("cross"),
    }
    if not target_evidence:
        for key in ("async_payload", "async_parse", "cross"):
            projected.pop(key, None)
    return projected


def _candidate_format_errors(
    expectations: Mapping[str, Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    *,
    expected_digest: str | None,
) -> tuple[
    list[str], dict[str, str], dict[str, Mapping[str, object]], list[str]
]:
    """Compare candidate wire evidence with the frozen target evidence.

    ``errors`` lists every format defect and invalidates the evidence receipt.
    ``contract_errors`` is the subset that invalidates the candidate as a
    whole.  An incompatible wire format on an F2P observation is an ordinary
    behavioural miss: it fails that observation and lowers the F2P ratio, but
    the new 6.0.0 capability it exercises is the thing being graded, so it must
    not turn the whole run into ``candidate_valid=FAIL``.  The same defect on a
    P2P observation is a public-contract regression and stays a contract error.
    Missing evidence or an unconsumed cross-inputs digest is an evidence
    integrity defect and always invalidates the candidate.
    """

    errors: list[str] = []
    contract_errors: list[str] = []
    errors_by_id: dict[str, str] = {}
    rows = {
        str(row.get("id")): row
        for row in observations
        if isinstance(row, Mapping)
    }
    records: dict[str, Mapping[str, object]] = {}

    def fail(identifier: str, message: str, *, contract: bool = True) -> None:
        errors.append(message)
        if contract:
            contract_errors.append(message)
        errors_by_id.setdefault(identifier, message)

    for identifier in _FORMAT_OBSERVATION_IDS:
        row = rows.get(identifier)
        if row is None:
            fail(identifier, f"format evidence is missing observation {identifier}")
            continue
        envelope = row.get("value")
        content = envelope.get("value") if isinstance(envelope, Mapping) else None
        fmt = content.get("format") if isinstance(content, Mapping) else None
        if not isinstance(fmt, Mapping):
            fail(
                identifier,
                f"observation {identifier} does not carry format evidence",
            )
            continue
        records[identifier] = fmt
        if _format_projection(fmt) != _format_projection(expectations[identifier]):
            fail(
                identifier,
                f"observation {identifier} wire-format evidence differs from "
                "the target package",
                contract=str(row.get("kind") or "") != "F2P",
            )
        evidence = row.get("evidence")
        digest = (
            evidence.get("format_cross_inputs_sha256")
            if isinstance(evidence, Mapping)
            else None
        )
        if digest != expected_digest:
            fail(
                identifier,
                f"observation {identifier} did not consume the evaluator cross-inputs",
            )
    return errors, errors_by_id, records, contract_errors


def _comparison_projection(
    value: object,
    *,
    target_evidence: bool = True,
) -> object:
    """Drop non-contract presentation fields before structural comparison."""

    if not isinstance(value, Mapping):
        return value
    projected = dict(value)
    content = projected.get("value")
    if isinstance(content, Mapping):
        content = dict(content)
        fmt = content.get("format")
        if isinstance(fmt, Mapping):
            content["format"] = _format_projection(
                fmt, target_evidence=target_evidence
            )
        projected["value"] = content
    return projected


def _source_consume_cases(
    expectations: Mapping[str, Mapping[str, Any]],
    records: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for identifier in _FORMAT_OBSERVATION_IDS:
        expected = expectations[identifier]
        # Reverse consumption is meaningful only where the source-era runtime
        # understands the value shape.  Payloads for target-only types have no
        # source consumer to replay them through.
        if expected.get("kind") != "value" or not expected.get("source_consumable"):
            continue
        record = records.get(identifier)
        if not isinstance(record, Mapping) or "payload" not in record:
            continue
        case: dict[str, object] = {
            "case_id": record.get("case_id"),
            "observation_id": identifier,
            "kind": "value",
            "payload": record.get("payload"),
            "async_payload": record.get("async_payload"),
        }
        if isinstance(record.get("uneval_source"), str):
            case["uneval_source"] = record.get("uneval_source")
        cases.append(case)
    return cases


def _source_consume_errors(
    expectations: Mapping[str, Mapping[str, Any]],
    *,
    assets: Path,
    cases: Sequence[Mapping[str, object]],
    source_commit: str,
    timeout: float,
    environment: Mapping[str, str],
    observation_kinds: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str], dict[str, object], list[str]]:
    """Replay candidate payloads through an evaluator-owned source runtime.

    A payload that the source-era runtime cannot restore fails the observation
    whose payload it is.  When that observation exercises a target-only F2P
    capability, the miss lowers the F2P ratio but must not invalidate the whole
    candidate: the new capability is what is being graded.  A P2P payload the
    source runtime can no longer restore is a compatibility regression and
    stays a contract error.  Without a kind map every failure stays a contract
    error, which keeps the default fail-closed.
    """

    kinds = {
        str(identifier): str(kind)
        for identifier, kind in (observation_kinds or {}).items()
    }

    script = assets / "verifier" / "source-consume.mjs"
    source_environment = dict(environment)
    source_environment.setdefault(
        "MIGRATION_EVALUATOR_NODE_MODULES", _EVALUATOR_NODE_MODULES
    )
    errors: list[str] = []
    contract_errors: list[str] = []
    errors_by_id: dict[str, str] = {}

    def fail(identifier: str, message: str) -> None:
        errors.append(message)
        if kinds.get(identifier) != "F2P":
            contract_errors.append(message)
        errors_by_id.setdefault(identifier, message)
    if not script.is_file():
        raise InfrastructureError(
            "evaluator source-consumption runtime is missing"
        )
    with tempfile.TemporaryDirectory(prefix="migration-source-consume-") as raw:
        directory = Path(raw)
        request = directory / "request.json"
        response = directory / "response.json"
        request.write_text(
            json.dumps(
                {
                    "schema_version": "migration-source-consume-request:v1",
                    "source_commit": source_commit,
                    "cases": list(cases),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        completed, receipt = _run_with_receipt(
            ["node", str(script), str(request), str(response)],
            kind="source-consume",
            index=0,
            cwd=assets,
            timeout=timeout,
            environment=source_environment,
        )
        if completed is None or completed.returncode != 0:
            raise InfrastructureError(
                "evaluator source-consumption runtime failed: "
                + ((completed.stderr if completed else "") or "")[-2000:]
            )
        try:
            payload = _load_json(response)
        except (OSError, ValueError) as exc:
            raise InfrastructureError(
                f"evaluator source-consumption output is invalid: {exc}"
            ) from exc
    returned = {
        str(row.get("observation_id")): row
        for row in payload.get("cases", [])
        if isinstance(row, Mapping)
    }
    for identifier in _FORMAT_OBSERVATION_IDS:
        expected_record = expectations[identifier]
        if expected_record.get("kind") != "value" or not expected_record.get(
            "source_consumable"
        ):
            continue
        actual = returned.get(identifier)
        if not isinstance(actual, Mapping):
            fail(
                identifier,
                f"source runtime produced no cross-consumption result for {identifier}",
            )
            continue
        expected_cross = expected_record.get("source_consume")
        expected_cross = (
            expected_cross if isinstance(expected_cross, Mapping) else {}
        )
        for key in ("parse", "unflatten", "async_parse", "uneval"):
            if actual.get(key) != expected_cross.get(key):
                fail(
                    identifier,
                    f"source runtime cannot restore candidate payload for "
                    f"{identifier} ({key})",
                )
    receipt["candidate_cases_sha256"] = _canonical_digest(list(cases))
    receipt["matched"] = not errors
    return errors, errors_by_id, receipt, contract_errors


def _run_as_candidate(command: Sequence[str], *, identity: tuple[int, int]) -> list[str]:
    uid, gid = identity
    return [
        "setpriv",
        f"--reuid={uid}",
        f"--regid={gid}",
        "--clear-groups",
        "--",
        *[str(item) for item in command],
    ]


def _isolation_probe(
    *,
    candidate: Path,
    public_assets: Path,
    identity: tuple[int, int] | None,
    timeout: float,
    environment: Mapping[str, str],
    hidden_root: Path = Path("/tests"),
    verifier_root: Path = Path("/logs/verifier"),
    candidate_logs: Path | None = None,
    candidate_home: Path | None = None,
) -> dict[str, object]:
    if identity is None:
        return {
            "status": "FAIL",
            "source": "candidate-isolation-probe",
            "evidence": [
                "candidate processes do not run under a distinct identity; "
                "the verifier is not root or no candidate uid is configured"
            ],
            "checks": [],
            "violations": [],
            "required_failures": ["distinct-candidate-identity"],
        }
    uid, gid = identity
    evidence: list[str] = []
    checks: list[dict[str, object]] = []
    violations: list[str] = []
    required_failures: list[str] = []
    public_assets_js = json.dumps(str(public_assets))
    hidden_js = json.dumps(str(hidden_root))
    verifier_js = json.dumps(str(verifier_root))
    test_patch_js = json.dumps(str(hidden_root / "verifier" / "test.patch"))
    logs_js = json.dumps(str(candidate_logs or candidate))

    def probe(name: str, script: str, *, allow: bool) -> None:
        node = [
            "node",
            "--permission",
            f"--allow-fs-read={candidate}",
            f"--allow-fs-read={public_assets}",
            f"--allow-fs-read={hidden_root / 'verifier' / 'test.patch'}",
            f"--allow-fs-read={environment.get('MIGRATION_EVALUATOR_NODE_MODULES', _EVALUATOR_NODE_MODULES)}",
            f"--allow-fs-write={candidate}",
            "-e",
            script,
        ]
        if candidate_home is not None:
            node.insert(-2, f"--allow-fs-write={candidate_home}")
        command = _run_as_candidate(node, identity=(uid, gid))
        completed = None
        try:
            completed = subprocess.run(
                command,
                cwd=candidate,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=timeout,
                env={**environment},
            )
            ok = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok = False
            evidence.append(f"{name}: probe execution failed: {exc}")
        record: dict[str, object] = {
            "name": name,
            "returncode": None if completed is None else completed.returncode,
            "expected": "allowed" if allow else "denied",
            "ok": ok if allow else not ok,
        }
        if not record["ok"] and completed is not None:
            record["diagnostic"] = (completed.stderr or completed.stdout or "")[
                -600:
            ]
        checks.append(record)
        if allow and not ok:
            required_failures.append(name)
            evidence.append(f"{name}: required capability is unavailable")
        if not allow and ok:
            violations.append(name)
            evidence.append(f"{name}: forbidden capability succeeded")

    probe(
        "read-candidate-workspace",
        "require('node:fs').readFileSync('./package.json');process.exit(0);",
        allow=True,
    )
    probe(
        "write-candidate-workspace",
        "const fs=require('node:fs');"
        "const path='.candidate-isolation-probe';"
        "fs.writeFileSync(path,'x');"
        "fs.unlinkSync(path);"
        "process.exit(0);",
        allow=True,
    )
    probe(
        "read-public-protocol",
        f"require('node:fs').readFileSync({public_assets_js}+'/candidate-runner.mjs');"
        "process.exit(0);",
        allow=True,
    )
    probe(
        "read-allowed-test-patch",
        f"require('node:fs').readFileSync({test_patch_js});"
        "process.exit(0);",
        allow=True,
    )
    probe(
        "read-public-evaluation-assets",
        f"require('node:fs').readFileSync({public_assets_js}+'/shared/batches/BATCH-P2P-0001/harness.mjs');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "read-hidden-harness",
        f"require('node:fs').readFileSync({hidden_js}+'/assets/harness.mjs');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "read-hidden-shared-harness",
        f"require('node:fs').readFileSync({hidden_js}+'/assets/shared/batches/BATCH-P2P-0001/harness.mjs');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "read-hidden-fixtures",
        f"require('node:fs').readFileSync({hidden_js}+'/assets/fixtures/batches/BATCH-P2P-0001/matrix.mjs');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "read-hidden-source-baseline",
        f"require('node:fs').readFileSync({hidden_js}+'/assets/source-baseline-outputs.json');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "read-hidden-test-evidence",
        f"require('node:fs').readFileSync({hidden_js}+'/assets/source-test-baseline.json');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "read-hidden-source-runtime",
        f"require('node:fs').readFileSync({hidden_js}+'/assets/source-runtime/index.js');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "read-hidden-target-runtime",
        f"require('node:fs').readFileSync({hidden_js}+'/assets/target/run.sh');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "read-hidden-verifier",
        f"require('node:fs').readFileSync({hidden_js}+'/verifier/harbor_entry.py');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "read-hidden-through-symlink",
        f"const fs=require('node:fs');const link='.candidate-isolation-link';"
        "try{fs.unlinkSync(link);}catch{}"
        f"fs.symlinkSync({hidden_js}+'/assets/harness.mjs',link);"
        "try{fs.readFileSync(link);}finally{try{fs.unlinkSync(link);}catch{}}"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "readlink-hidden-path",
        # Resolving a candidate-created symlink exercises the readlink/realpath
        # traversal into evaluator-owned data while keeping the link itself in
        # the candidate's writable workspace.
        f"const fs=require('node:fs');const link='.candidate-readlink-probe';"
        "try{fs.unlinkSync(link);}catch{}"
        f"fs.symlinkSync({hidden_js}+'/assets/harness.mjs',link);"
        "try{fs.realpathSync(link);}finally{try{fs.unlinkSync(link);}catch{}}"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "openat-hidden-path",
        f"require('node:fs').openSync({hidden_js}+'/assets/harness.mjs','r');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "dynamic-import-hidden-harness",
        f"import({json.dumps('file://' + str(hidden_root / 'assets' / 'harness.mjs'))})"
        ".catch((error)=>{console.error(error);process.exit(7);});",
        allow=False,
    )
    probe(
        "create-require-hidden-module",
        f"require('node:module').createRequire({json.dumps(str(hidden_root / 'assets' / 'harness.mjs'))})"
        f"({json.dumps(str(hidden_root / 'assets' / 'harness.mjs'))});process.exit(0);",
        allow=False,
    )
    probe(
        "execve-child-process",
        "require('node:child_process').execFileSync('/bin/true');process.exit(0);",
        allow=False,
    )
    probe(
        "clone-worker-thread",
        "new (require('node:worker_threads').Worker)('process.exit(0)',{eval:true});"
        "setTimeout(()=>process.exit(0),100);",
        allow=False,
    )
    probe(
        "read-hidden-via-dependency-loader",
        f"const Module=require('node:module');"
        f"Module._load({json.dumps(str(hidden_root / 'assets' / 'harness.mjs'))});"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "write-verifier-output",
        f"require('node:fs').writeFileSync({verifier_js}+'/.isolation-probe','x');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "write-final-verdict",
        f"require('node:fs').writeFileSync({verifier_js}+'/verifier-result.json','{{}}');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "write-protected-tests",
        f"require('node:fs').writeFileSync({hidden_js}+'/.isolation-probe','x');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "write-hidden-evaluation-assets",
        f"require('node:fs').writeFileSync({hidden_js}+'/assets/.isolation-probe','x');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "write-candidate-log-report",
        f"require('node:fs').writeFileSync({logs_js}+'/result.json','{{}}');"
        "process.exit(0);",
        allow=False,
    )
    probe(
        "outbound-network",
        "fetch('https://example.com',{signal:AbortSignal.timeout(4000)})"
        ".then(()=>process.exit(0)).catch(()=>process.exit(1));",
        allow=False,
    )
    status = "PASS" if not violations and not required_failures else "FAIL"
    return {
        "status": status,
        "source": "candidate-isolation-probe",
        "identity": {"uid": uid, "gid": gid},
        "checks": checks,
        "evidence": evidence,
        "violations": violations,
        "required_failures": required_failures,
    }


def _dependency_commands(candidate: Path) -> list[tuple[str, list[str]]]:
    del candidate
    pnpm = os.environ.get("MIGRATION_PNPM") or shutil.which("pnpm") or "/usr/local/bin/pnpm"
    store = os.environ.get("MIGRATION_PNPM_STORE", _DEFAULT_PNPM_STORE)
    manifest_probe = (
        "const fs=require('node:fs');"
        "const failures=[];"
        "for (const name of ['package.json','pnpm-lock.yaml']) {"
        "  let stat;"
        "  try { stat=fs.lstatSync(name); }"
        "  catch { failures.push(`${name} is missing`); continue; }"
        "  if (stat.isSymbolicLink()) failures.push(`${name} must not be a symlink`);"
        "  else if (!stat.isFile()) failures.push(`${name} must be a regular file`);"
        "  else if (name==='pnpm-lock.yaml' && stat.size===0)"
        "    failures.push('pnpm-lock.yaml must not be empty');"
        "}"
        "let manifest;"
        "try { manifest=JSON.parse(fs.readFileSync('package.json','utf8')); }"
        "catch (error) { failures.push(`package.json is not valid JSON: ${error.message}`); }"
        "if (manifest!==undefined && "
        "  (!manifest || typeof manifest!=='object' || Array.isArray(manifest)))"
        "  failures.push('package.json must contain a JSON object');"
        "if (failures.length) { console.error(JSON.stringify({failures})); process.exit(1); }"
    )
    return [
        ("dependency-resolution", ["node", "-e", manifest_probe]),
        (
            "dependency-resolution",
            [
                pnpm,
                "install",
                "--offline",
                "--frozen-lockfile",
                "--ignore-scripts",
                "--store-dir",
                store,
                "--package-import-method=copy",
                "--reporter=append-only",
            ],
        ),
        (
            "import-probe",
            [
                "node",
                "--input-type=module",
                "-e",
                (
                    # The probe guards the pre-existing (4.3.3) public contract
                    # and that the entry point loads.  The 6.0.0 additions
                    # (stringifyAsync, the frozen operation maps, DevalueError,
                    # filterArrayIndices) are migration targets: scope units
                    # MU003/MU011 and the F2P observations score them, so a
                    # missing addition stays a partial ``T`` rather than a
                    # hard-gate zero.
                    "const m = await import('./index.js');"
                    "const required = ['uneval','parse','unflatten','stringify'];"
                    "const missing = required.filter((name) => !(name in m));"
                    "const notCallable = required"
                    ".filter((name) => typeof m[name] !== 'function');"
                    "if (missing.length || notCallable.length) {"
                    "  console.error(JSON.stringify({missing, notCallable}));"
                    "  process.exit(1);"
                    "}"
                ),
            ],
        ),
    ]


_ASSERTION_SETUP = """\
import { afterEach, beforeEach, expect } from 'vitest';
import assert from 'node:assert/strict';
import { appendFileSync } from 'node:fs';

let assertCalls = 0;
for (const name of Object.keys(assert)) {
  const original = assert[name];
  if (typeof original !== 'function') continue;
  try {
    assert[name] = function (...args) {
      assertCalls += 1;
      return original.apply(this, args);
    };
  } catch {}
}

beforeEach(() => {
  assertCalls = 0;
});

afterEach(() => {
  const state = expect.getState();
  const row = {
    file: state.testPath,
    name: state.currentTestName,
    expectCalls: state.assertionCalls,
    assertCalls
  };
  appendFileSync(process.env.MIGRATION_ASSERTION_LOG, JSON.stringify(row) + '\\n');
});
"""


def _evaluator_test_includes(assets: Path, config: Mapping[str, Any]) -> list[str]:
    """Return the include list for the evaluator-owned Vitest project.

    The conventional globs cannot be assumed to reach every migrated file: the
    protected equivalence map may name a target file such as ``test/test.js``
    that ``test/**/*.test.js`` does not match.  Every mapped target file is
    therefore added verbatim, so the evaluator executes exactly the files whose
    protected cases it later requires in the execution report.
    """

    includes = ["src/**/*.test.js", "test/**/*.test.js"]
    assets_config = config.get("assets")
    name = (
        assets_config.get("source_test_equivalence")
        if isinstance(assets_config, Mapping)
        else None
    )
    if not isinstance(name, str) or not name:
        return includes
    equivalence = load_equivalence(assets / name)
    for record in equivalence.values():
        if not isinstance(record, Mapping):
            continue
        cases = record.get("cases")
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, Mapping):
                continue
            target = str(case.get("target_file", "")).replace("\\", "/")
            if target and target not in includes:
                includes.append(target)
    return includes


def _assertion_contract_probe(candidate: Path) -> str:
    """Probe the actual imported helper under Vitest, outside candidate writes.

    The protected 4.3.3 suite used uvu's deep equality and strict identity.
    Merely retaining assertion call sites cannot detect a no-op helper or a
    replacement whose equality ignores missing properties.
    """
    imports = assertion_helper_imports(candidate)
    prefix = "import { test, expect as verify } from 'vitest';\n"
    targets = {(candidate / "test" / specifier).resolve() for _, specifier in imports
               if specifier.startswith(".")}
    if len(targets) != 1 or any(not specifier.startswith(".") for _, specifier in imports):
        return prefix + "test('assertions use a single local helper', () => verify(false).toBe(true));\n"
    target = next(iter(targets))
    if not target.is_relative_to(candidate.resolve()) or not target.is_file():
        return prefix + "test('assertion helper exists inside the package', () => verify(false).toBe(true));\n"
    bindings = " ".join(binding for binding, _ in imports)
    prefix += "import * as helper from " + json.dumps(target.as_uri()) + ";\n"
    if re.search(r"\bassert\b", bindings):
        selected = "helper" if re.search(r"\*\s+as\s+assert\b", bindings) else (
            "helper.assert" if "{" in bindings else "helper.default"
        )
        prefix += "const a = " + selected + ";\n"
        prefix += "const equal = (x,y) => a.equal(x,y); const identity = (x,y) => a.is(x,y);\n"
        prefix += "const truthy = x => a.ok(x); const throws = (fn, check) => a.throws(fn, check);\n"
    elif re.search(r"\b(?:equal|deepStrictEqual)\b", bindings):
        # Named imports from the single local helper are just as valid as an
        # ``assert`` namespace import.  Probe the exported primitives directly;
        # the static integrity checker maps their local aliases back to these
        # semantic roles.
        equal_export = "deepStrictEqual" if re.search(r"\bdeepStrictEqual\b", bindings) else "equal"
        identity_export = "strictEqual" if re.search(r"\bstrictEqual\b", bindings) else "is"
        prefix += f"const equal = (x,y) => helper.{equal_export}(x,y);\n"
        prefix += f"const identity = (x,y) => helper.{identity_export}(x,y);\n"
        prefix += "const truthy = x => helper.ok(x); const throws = (fn, check) => helper.throws(fn, check);\n"
    else:
        prefix += "const equal=(x,y)=>helper.expect(x).toEqual(y);\n"
        prefix += "const identity=(x,y)=>helper.expect(x).toBe(y);\n"
        prefix += "const truthy=x=>helper.expect(x).toBeTruthy();\n"
        # The preserved suite also uses assert.throws. If it imports both,
        # the assert branch above exercises it directly.
        prefix += "const throws=(fn,check)=>{let caught=false;try{fn()}catch(e){caught=true;if(check&&!check(e))throw Error('predicate')}if(!caught)throw Error('missing throw')};\n"
    return prefix + r'''
test('assertion equality preserves deep structural semantics', () => {
  verify(() => equal({ a: [1, { b: 2 }] }, { a: [1, { b: 2 }] })).not.toThrow();
  verify(() => equal([, 1], [undefined, 1])).not.toThrow();
  verify(() => equal(NaN, NaN)).not.toThrow();
  for (const [left, right] of [
    [{ a: 1 }, { a: 2 }], [{ a: 1 }, { a: 1, b: undefined }],
    [[1], [1, 2]], [new Date(1), new Date(2)], [/x/g, /x/i],
    [new Uint8Array([1]), new Uint8Array([2])]
  ]) verify(() => equal(left, right)).toThrow();
});
test('assertion identity preserves strict equality', () => {
  const value = {};
  verify(() => identity(value, value)).not.toThrow();
  verify(() => identity(0, -0)).not.toThrow();
  verify(() => identity({}, {})).toThrow();
  verify(() => identity(1, '1')).toThrow();
  verify(() => identity(NaN, NaN)).toThrow();
});
test('truthiness and throws helpers fail when their conditions fail', () => {
  verify(() => truthy({})).not.toThrow();
  for (const value of [false, 0, '', null, undefined, NaN]) verify(() => truthy(value)).toThrow();
  verify(() => throws(() => { throw Error('marker'); }, e => e.message === 'marker')).not.toThrow();
  verify(() => throws(() => {})).toThrow();
  verify(() => throws(() => { throw Error('marker'); }, () => false)).toThrow();
});
'''


def _write_evaluator_vitest_files(
    candidate: Path,
    *,
    includes: Sequence[str] = ("src/**/*.test.js", "test/**/*.test.js"),
) -> tuple[Path, Path]:
    """Create evaluator-owned config/setup files outside candidate writes.

    The files live below the candidate root so that Node resolves ``vitest``
    from the installed dependency graph, but the directory and files are owned
    by the evaluator and made read-only for the candidate identity.

    ``includes`` comes from :func:`_evaluator_test_includes`; the default is
    only a fallback for callers that have no protected asset bundle.
    """

    directory = candidate / ".migration-evaluator"
    if directory.is_symlink() or directory.is_file():
        directory.unlink()
    elif directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    setup = directory / "assertion-setup.mjs"
    config = directory / "vitest.config.mjs"
    setup.write_text(_ASSERTION_SETUP, encoding="utf-8")
    helper_probe = directory / "assertion-contract.test.mjs"
    helper_probe.write_text(_assertion_contract_probe(candidate), encoding="utf-8")
    payload = {
        "root": str(candidate),
        "test": {
            "projects": [
                {
                    "test": {
                        "name": "unit",
                        "environment": "node",
                        "include": [*includes, str(helper_probe)],
                        "testTimeout": 10_000,
                        "setupFiles": [str(setup)],
                    }
                }
            ]
        },
    }
    config.write_text(
        "export default " + json.dumps(payload, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    setup.chmod(0o444)
    config.chmod(0o444)
    helper_probe.chmod(0o444)
    directory.chmod(0o555)
    return config, setup


def _remove_evaluator_vitest_files(candidate: Path) -> None:
    directory = candidate / ".migration-evaluator"
    try:
        if directory.is_symlink() or directory.is_file():
            directory.unlink()
        elif directory.exists():
            directory.chmod(0o755)
            shutil.rmtree(directory)
    except OSError:
        pass


def _test_execution_command(
    report: Path, *, config: Path | None = None
) -> list[str]:
    """Build an evaluator-owned Vitest JSON invocation.

    The candidate's own test script may print a custom report or skip files.
    The verifier therefore runs the declared unit project itself and asks
    Vitest for the machine-readable execution inventory.  Vitest's JSON
    reporter writes to a file rather than stdout, so the file lives in an
    evaluator-owned directory and is copied into the verifier log immediately
    after the run.
    """

    pnpm = os.environ.get("MIGRATION_PNPM") or shutil.which("pnpm") or "/usr/local/bin/pnpm"
    return [
        pnpm,
        "exec",
        "vitest",
        "run",
        *(["--config", str(config)] if config is not None else []),
        "--project",
        "unit",
        "--reporter=json",
        f"--outputFile={report}",
    ]


def _relative_test_file(value: object, candidate: Path) -> str:
    text = str(value).replace("\\", "/")
    path = Path(text)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(candidate.resolve()).as_posix()
        except (OSError, ValueError):
            return path.as_posix()
    return path.as_posix()


def _read_assertion_log(
    source: Path, *, candidate: Path
) -> dict[tuple[str, str], list[int]]:
    """Read per-test assertion counts produced by the evaluator setup file."""

    if source.is_symlink() or not source.is_file():
        raise InfrastructureError(
            "the evaluator assertion log is missing or is not a regular file"
        )
    counts: dict[tuple[str, str], list[int]] = {}
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InfrastructureError(f"the evaluator assertion log is unreadable: {exc}") from exc
    for ordinal, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InfrastructureError(
                f"the evaluator assertion log line {ordinal} is invalid: {exc}"
            ) from exc
        if not isinstance(row, Mapping):
            raise InfrastructureError(
                f"the evaluator assertion log line {ordinal} is not an object"
            )
        name = row.get("name")
        expect_calls = row.get("expectCalls")
        assert_calls = row.get("assertCalls")
        if (
            not isinstance(name, str)
            or not isinstance(row.get("file"), str)
            or not isinstance(expect_calls, int)
            or isinstance(expect_calls, bool)
            or expect_calls < 0
            or not isinstance(assert_calls, int)
            or isinstance(assert_calls, bool)
            or assert_calls < 0
        ):
            raise InfrastructureError(
                f"the evaluator assertion log line {ordinal} is malformed"
            )
        key = (_relative_test_file(row["file"], candidate), name)
        counts.setdefault(key, []).append(expect_calls + assert_calls)
    if not counts:
        raise InfrastructureError("the evaluator assertion log is empty")
    return counts


def _write_test_execution_report(
    source: Path,
    destination: Path,
    *,
    candidate: Path,
    assertion_log: Path,
) -> Path:
    """Validate and persist the Vitest report outside candidate reach."""

    if source.is_symlink() or not source.is_file():
        raise InfrastructureError("Vitest did not produce a regular JSON report")
    try:
        payload = _load_json(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InfrastructureError(str(exc)) from exc
    if not isinstance(payload.get("testResults"), list) or "numTotalTests" not in payload:
        raise InfrastructureError(
            "Vitest produced an execution report without test results"
        )
    counts = _read_assertion_log(assertion_log, candidate=candidate)
    for file_row in payload["testResults"]:
        if not isinstance(file_row, Mapping):
            continue
        relative = _relative_test_file(file_row.get("name", ""), candidate)
        assertions = file_row.get("assertionResults")
        if not isinstance(assertions, list):
            continue
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            status = str(assertion.get("status", "")).casefold()
            if status in {"skipped", "pending", "todo", "disabled"}:
                # Vitest does not run afterEach for a skipped test, so the
                # evaluator assertion log intentionally has no row for it.
                # The report is still valid execution evidence; the skip is
                # classified separately by no_new_skip_or_xfail (candidate
                # owned) or by the protected execution check (protected).
                assertion["assertionCount"] = 0
                continue
            title = assertion.get("title", "")
            suite = assertion.get("ancestorTitles")
            # The evaluator setup file records ``currentTestName`` with the
            # ancestor titles separated by `` > ``.  Vitest's JSON report may
            # expose a different ``fullName`` spelling (space-joined), so build
            # the key from the structured titles instead of trusting it.
            ancestors = (
                [str(item) for item in suite] if isinstance(suite, list) else []
            )
            full_name = " > ".join([*ancestors, str(title)])
            key = (relative, full_name)
            observed = counts.get(key)
            if not observed:
                raise InfrastructureError(
                    "the evaluator assertion log has no result for "
                    f"{relative}: {full_name}"
                )
            assertion["assertionCount"] = observed.pop(0)
    if destination.is_symlink():
        raise InfrastructureError("test execution report path is an unsafe symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    destination.chmod(0o600)
    return destination


def _prepare_candidate_owner(candidate: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    uid, gid = identity
    completed = subprocess.run(
        ["chown", "-R", f"{uid}:{gid}", str(candidate)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise InfrastructureError(
            "cannot assign the candidate workspace to the isolated identity: "
            + (completed.stderr or completed.stdout).strip()[-2000:]
        )


def _clear_candidate_dependencies(candidate: Path) -> None:
    """Remove every preinstalled graph before the evaluator's offline install."""

    target = candidate / "node_modules"
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        target.unlink()
        return
    shutil.rmtree(target)


def _dependency_graph_receipt(
    candidate: Path,
    *,
    phase: str,
    index: int,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Audit the graph that the candidate is actually using at this phase."""

    graph = audit_dependency_graph(candidate)
    errors = graph.get("errors", [])
    errors = list(errors) if isinstance(errors, list) else [str(errors)]
    digest = graph.get("digest")
    if graph.get("status") == "PASS" and not isinstance(digest, str):
        errors.append("dependency audit did not record a canonical graph digest")
    if previous is not None:
        previous_graph = previous.get("resolved_graph", {})
        previous_digest = (
            previous_graph.get("digest")
            if isinstance(previous_graph, Mapping)
            else None
        )
        if digest != previous_digest:
            errors.append(
                "dependency graph changed since the "
                f"{previous.get('phase', 'previous')} audit "
                f"({previous_digest!r} -> {digest!r})"
            )
    valid = graph.get("status") == "PASS" and not errors
    if errors:
        graph["status"] = "FAIL"
        graph["errors"] = errors
    return {
        "schema_version": "migration-execution-receipt:v1",
        "kind": "dependency-graph",
        "phase": phase,
        "index": index,
        "returncode": 0 if valid else 1,
        "timed_out": False,
        "signal": None,
        "failed_phase": None if valid else f"dependency-graph:{phase}",
        "report_present": False,
        "valid": valid,
        "resolved_graph": graph,
    }


def _dependency_failure_message(
    phase: str, receipt: Mapping[str, Any]
) -> str:
    graph = receipt.get("resolved_graph", {})
    details = graph.get("errors", []) if isinstance(graph, Mapping) else []
    if not isinstance(details, list):
        details = [str(details)]
    return (
        f"candidate dependency graph is not valid at the {phase} audit: "
        + "; ".join(str(item) for item in details[:8])
    )


def _diagnostic_group(rows: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in rows]
    if not statuses:
        status = "NOT_RUN"
    elif any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses):
        status = "INCONCLUSIVE"
    elif any(item == "FAIL" for item in statuses):
        status = "FAIL"
    elif all(item == "PASS" for item in statuses):
        status = "PASS"
    else:
        status = "INCONCLUSIVE"
    return {
        "status": status,
        "passed": sum(item == "PASS" for item in statuses),
        "total": len(statuses),
    }


def _run_dependency_diagnostic(
    *,
    candidate: Path,
    bundle: Path,
    assets: Path,
    public_assets: Path,
    logs: Path,
    config: Mapping[str, Any],
    runtime: Mapping[str, Any],
    timeout: float,
    environment: Mapping[str, str],
    dependency_message: str,
) -> dict[str, object]:
    """Run F2P/P2P observations after dependency resolution failed.

    This is deliberately outside the graded evidence path: it uses the
    evaluator's target dependency tree, writes only to the evaluator log
    directory, and never contributes receipts or observations to the formal
    hard gate or score.
    """

    result: dict[str, object] = {
        "schema_version": "migration-diagnostic:v1",
        "diagnostic_only": True,
        "eligible_for_graded": False,
        "dependency_failure": dependency_message,
        "status": "NOT_RUN",
        "f2p_status": "NOT_RUN",
        "p2p_status": "NOT_RUN",
        "summary": {
            "f2p": _diagnostic_group(()),
            "p2p": _diagnostic_group(()),
        },
        "observations": [],
        "contract_errors": [],
        "receipt": None,
    }
    harness = runtime.get("harness_command")
    if (
        not isinstance(harness, Sequence)
        or isinstance(harness, (str, bytes))
        or not harness
    ):
        result["message"] = "target runtime harness command is missing"
        return result
    report = logs / "diagnostic-report.json"
    try:
        if report.exists() or report.is_symlink():
            report.unlink()
    except OSError as exc:
        result["status"] = "FAILED"
        result["message"] = f"cannot clear the diagnostic report path: {exc}"
        return result
    command = [
        str(item).format_map(
            {
                "repo": str(candidate),
                "assets": str(public_assets),
                "verifier": str(assets / "verifier"),
                "report": str(report),
            }
        )
        for item in harness
    ]
    completed, receipt = _run_with_receipt(
        command,
        kind="diagnostic-target-execution",
        index=1,
        cwd=bundle,
        timeout=min(timeout, 300.0),
        environment=environment,
    )
    _annotate_report_presence(receipt, report)
    result["receipt"] = receipt
    returncode = receipt.get("returncode")
    if completed is None or not isinstance(returncode, int):
        result["status"] = "FAILED" if completed is None else "INCOMPLETE"
        result["message"] = "diagnostic harness did not complete"
        return result
    if report.is_symlink() or not report.is_file():
        result["status"] = "FAILED"
        result["message"] = "diagnostic harness produced no report"
        return result
    try:
        payload = _load_json(report)
        payload = normalize_observation_report(
            payload,
            role="candidate",
            returncode=returncode,
            report_path=str(report),
            config=config,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        result["status"] = "FAILED"
        result["message"] = f"diagnostic report is invalid: {exc}"
        return result
    contract_errors = list(
        observation_contract_errors(
            payload,
            config,
            actual_returncode=returncode,
        )
    )
    observations = payload.get("observations", [])
    observations = (
        [dict(row) for row in observations if isinstance(row, Mapping)]
        if isinstance(observations, list)
        else []
    )
    f2p = [row for row in observations if row.get("kind") == "F2P"]
    p2p = [row for row in observations if row.get("kind") == "P2P"]
    result.update(
        {
            "status": (
                "COMPLETED"
                if returncode == 0 and not contract_errors and observations
                else "FAILED"
            ),
            "f2p_status": _diagnostic_group(f2p)["status"],
            "p2p_status": _diagnostic_group(p2p)["status"],
            "summary": {
                "f2p": _diagnostic_group(f2p),
                "p2p": _diagnostic_group(p2p),
            },
            "observations": observations,
            "contract_errors": contract_errors,
        }
    )
    if returncode != 0:
        result["message"] = (
            f"diagnostic harness exited with code {returncode}"
        )
    elif contract_errors:
        result["message"] = "diagnostic observations violated the contract"
    return result


def run_harbor_verifier(
    *,
    candidate_root: Path,
    bundle_root: Path,
    logs_root: Path,
    apply_failed: bool = False,
    patch_message: str = "",
) -> dict[str, object]:
    candidate = candidate_root.resolve()
    bundle = bundle_root.resolve()
    logs = logs_root.resolve()
    if not candidate.is_dir() or candidate.is_symlink():
        raise InfrastructureError("Harbor candidate /app is missing or unsafe")
    try:
        config = _load_json(bundle / "config.json")
    except (OSError, ValueError) as exc:
        return _write_result(
            logs,
            _infrastructure_result(
                f"evaluator config is unreadable: {exc}",
                candidate=candidate,
                config={},
            ),
        )
    harbor = config.get("harbor")
    if isinstance(harbor, Mapping) and harbor.get("reward_mode") != "VERIFIER_ONLY":
        return _write_result(
            logs,
            _infrastructure_result(
                "Harbor verifier only supports VERIFIER_ONLY",
                candidate=candidate,
                config={},
            ),
        )
    assets = bundle / "assets"
    config.setdefault("policy", {})
    target_environment = config.get("target_environment")
    if not isinstance(target_environment, Mapping):
        return _write_result(
            logs,
            _infrastructure_result(
                "target runtime config is missing",
                candidate=candidate,
                config=config,
            ),
        )
    runtime = target_environment.get("runtime")
    if not isinstance(runtime, Mapping):
        return _write_result(
            logs,
            _infrastructure_result(
                "target runtime config is missing",
                candidate=candidate,
                config=config,
            ),
        )
    try:
        timeout = float(runtime.get("timeout_seconds", 1800))
        runtime_environment = runtime.get("environment", {})
        if not isinstance(runtime_environment, Mapping):
            raise ValueError("target runtime environment must be an object")
        units = _load_migration_units(assets, config)
    except (OSError, ValueError, TypeError) as exc:
        return _write_result(
            logs,
            _infrastructure_result(
                f"evaluator runtime or migration units are unreadable: {exc}",
                candidate=candidate,
                config=config,
            ),
        )
    environment = os.environ.copy()
    environment.update(
        {str(key): str(value) for key, value in runtime_environment.items()}
    )
    receipts: list[dict[str, object]] = []

    # Resolve required-state applicability from every declaration before any
    # candidate command runs.  A missing or contradictory declaration is a
    # configuration error, never an implicit "not applicable".
    policy = config.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    try:
        state_declaration = resolve_state_applicability(
            policy=policy,
            group_applicability=config.get("group_applicability"),
            contract=load_migration_contract(config, bundle),
        )
        state_applicability_error = None
    except ApplicabilityError as exc:
        state_declaration = None
        state_applicability_error = str(exc)

    if apply_failed:
        message = patch_message or "model patch could not be applied"
        result = _candidate_evidence_result(
            message,
            candidate=candidate,
            config=config,
            units=units,
            test_baseline=None,
            receipts=receipts,
            patch_applicable=False,
        )
        result["candidate_patch_error"] = message
        return _write_result(logs, result)

    # 1. Evaluator-owned assets and the protected baseline are verified before
    #    any candidate command runs.
    try:
        receipts.append(_asset_preflight(assets, runtime, timeout, environment))
        baseline = load_source_baseline(assets / "source-baseline-outputs.json", config=config)
        test_baseline = _load_protected_test_assets(assets, config)
        format_expectations = _format_expectations(assets, config)
        cross_inputs = _cross_input_payload(_source_format_inputs(baseline))
    except (InfrastructureError, BaselineError) as exc:
        return _write_result(
            logs,
            _infrastructure_result(
                str(exc),
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
            ),
        )
    except (OSError, ValueError) as exc:
        return _write_result(
            logs,
            _infrastructure_result(
                f"protected evaluator asset is invalid: {exc}",
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
            ),
        )

    pre_hashes = _protected_fingerprints(bundle)
    identity = _candidate_identity()
    try:
        _prepare_candidate_owner(candidate, identity)
        public_assets = _prepare_public_assets(assets, logs)
    except (OSError, InfrastructureError) as exc:
        return _write_result(
            logs,
            _infrastructure_result(
                str(exc),
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
            ),
        )

    candidate_logs = logs.parent / "candidate"
    if candidate_logs.exists():
        shutil.rmtree(candidate_logs)
    candidate_logs.mkdir(parents=True, exist_ok=True)
    if identity is not None:
        subprocess.run(
            ["chown", "-R", f"{identity[0]}:{identity[1]}", str(candidate_logs)],
            check=False,
        )
    home = candidate_logs / "home"
    home.mkdir(parents=True, exist_ok=True)
    if identity is not None:
        subprocess.run(["chown", "-R", f"{identity[0]}:{identity[1]}", str(home)], check=False)
    environment["HOME"] = str(home)
    environment["MIGRATION_CANDIDATE_HOME"] = str(home)
    environment["MIGRATION_REPO_ROOT"] = str(candidate)
    environment["MIGRATION_PUBLIC_ASSETS"] = str(public_assets)
    environment["MIGRATION_EVALUATOR_NODE_MODULES"] = environment.get(
        "MIGRATION_EVALUATOR_NODE_MODULES", _EVALUATOR_NODE_MODULES
    )
    environment["MIGRATION_EVAL_SANDBOX"] = str(
        assets / "verifier" / "eval-expression.mjs"
    )
    # The cross-consumption corpus is evaluator-owned and must never live in
    # candidate-writable space.  Only the evaluator process receives its path.
    logs.mkdir(parents=True, exist_ok=True)
    cross_inputs_path = logs / "cross-inputs.json"
    _write_cross_inputs(cross_inputs_path, cross_inputs)
    environment["MIGRATION_CROSS_INPUTS"] = str(cross_inputs_path)
    if identity is not None:
        environment["MIGRATION_CANDIDATE_UID"] = str(identity[0])
        environment["MIGRATION_CANDIDATE_GID"] = str(identity[1])

    def formatted(
        commands: Sequence[Sequence[str]], report: str
    ) -> list[tuple[str, list[str]]]:
        values = {"repo": str(candidate), "assets": str(public_assets), "report": report}
        return [
            (str(kind), [str(item).format_map(values) for item in raw])
            for kind, raw in commands
        ]

    # 2. Toolchain and candidate dependency graph receipts.
    #
    # Version probes only establish that the fixed evaluator toolchain exists.
    # They must run in evaluator-owned space: `pnpm --version` resolves the
    # nearest package.json (Corepack reads its packageManager field), so running
    # it in the candidate workspace lets a corrupted candidate manifest turn an
    # unavailable-toolchain probe into a non-zero exit.  That failure belongs to
    # the candidate (it is a configuration parse error), not to the evaluator,
    # and must be classified as a candidate failure below instead of an
    # infrastructure error.  The probe directory must also be traversable by
    # the unprivileged candidate identity; the hidden assets tree is not.
    toolchain_cwd = logs_root.parent / "toolchain"
    toolchain_cwd.mkdir(parents=True, exist_ok=True)
    toolchain_cwd.chmod(0o755)
    preflight = formatted(
        [("version-probe", raw) for raw in runtime.get("preflight_commands", [])],
        str(candidate_logs / "result.json"),
    )
    dependency = _dependency_commands(candidate)
    preflight_receipts, preflight_failed, preflight_infra = _run_declared_commands(
        preflight,
        cwd=toolchain_cwd,
        timeout=timeout,
        environment=environment,
        start_index=len(receipts) + 1,
        identity=identity,
        candidate_owned=False,
    )
    receipts.extend(preflight_receipts)
    if preflight_failed or preflight_infra:
        return _write_result(
            logs,
            _infrastructure_result(
                "dependency toolchain is unavailable",
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
            ),
        )
    try:
        _clear_candidate_dependencies(candidate)
    except OSError as exc:
        return _write_result(
            logs,
            _infrastructure_result(
                f"cannot clear the candidate's preinstalled dependency graph: {exc}",
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
            ),
        )
    dependency_receipts, dependency_failed, dependency_infra = _run_declared_commands(
        dependency,
        cwd=candidate,
        timeout=timeout,
        environment=environment,
        start_index=len(receipts) + 1,
        identity=identity,
    )
    receipts.extend(dependency_receipts)
    if dependency_infra:
        return _write_result(
            logs,
            _infrastructure_result(
                "dependency toolchain is unavailable",
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
            ),
        )
    if dependency_failed:
        message = _candidate_execution_error(
            "candidate dependency graph or import probe failed", receipts
        )
        try:
            diagnostic = _run_dependency_diagnostic(
                candidate=candidate,
                bundle=bundle,
                assets=assets,
                public_assets=public_assets,
                logs=logs,
                config=config,
                runtime=runtime,
                timeout=timeout,
                environment=environment,
                dependency_message=message,
            )
        except Exception as exc:  # diagnostic evidence must never change the verdict
            diagnostic = {
                "schema_version": "migration-diagnostic:v1",
                "diagnostic_only": True,
                "eligible_for_graded": False,
                "dependency_failure": message,
                "status": "FAILED",
                "f2p_status": "NOT_RUN",
                "p2p_status": "NOT_RUN",
                "summary": {
                    "f2p": _diagnostic_group(()),
                    "p2p": _diagnostic_group(()),
                },
                "observations": [],
                "contract_errors": [],
                "message": f"diagnostic execution failed to start: {exc}",
            }
        return _write_result(
            logs,
            _candidate_evidence_result(
                message,
                candidate=candidate,
                config=config,
                units=units,
                test_baseline=test_baseline,
                receipts=receipts,
                observation_errors=[message],
                not_run_reason=message,
                diagnostic=diagnostic,
            ),
        )

    initial_graph_receipt = _dependency_graph_receipt(
        candidate, phase="initial", index=len(receipts) + 1
    )
    receipts.append(initial_graph_receipt)
    if not initial_graph_receipt["valid"]:
        message = _dependency_failure_message("initial", initial_graph_receipt)
        return _write_result(
            logs,
            _candidate_evidence_result(
                message,
                candidate=candidate,
                config=config,
                units=units,
                test_baseline=test_baseline,
                receipts=receipts,
                observation_errors=[message],
            ),
        )

    # 3. Candidate build and native tests.
    candidate_commands = [
        *(("target-build", raw) for raw in runtime.get("build_commands", [])),
        *(("target-native-test", raw) for raw in runtime.get("native_test_commands", [])),
    ]
    build_receipts, candidate_failed, infrastructure_failed = _run_declared_commands(
        formatted(candidate_commands, str(candidate_logs / "result.json")),
        cwd=candidate,
        timeout=timeout,
        environment=environment,
        start_index=len(receipts) + 1,
        identity=identity,
    )
    receipts.extend(build_receipts)
    post_build_graph_receipt = _dependency_graph_receipt(
        candidate,
        phase="post-build",
        index=len(receipts) + 1,
        previous=initial_graph_receipt,
    )
    receipts.append(post_build_graph_receipt)
    if not post_build_graph_receipt["valid"]:
        message = _dependency_failure_message(
            "post-build", post_build_graph_receipt
        )
        return _write_result(
            logs,
            _candidate_evidence_result(
                message,
                candidate=candidate,
                config=config,
                units=units,
                test_baseline=test_baseline,
                receipts=receipts,
                observation_errors=[message],
            ),
        )
    if infrastructure_failed:
        return _write_result(
            logs,
            _infrastructure_result(
                "candidate toolchain is unavailable",
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
            ),
        )
    if candidate_failed:
        message = _candidate_execution_error(
            "candidate prerequisite command failed", receipts
        )
        return _write_result(
            logs,
            _candidate_evidence_result(
                message,
                candidate=candidate,
                config=config,
                units=units,
                test_baseline=test_baseline,
                receipts=receipts,
                observation_errors=[message],
            ),
        )

    # 3b. The verifier executes the declared unit project itself.  Candidate
    # test reports are evidence to compare, never the source of truth.
    test_execution_report = logs / "test-execution-report.json"
    # Vitest writes the machine-readable report to a file, never to stdout.
    # The raw report lives in a private temporary directory outside the
    # candidate workspace.  Vitest's JSON reporter creates the output
    # directory itself, so the candidate identity must own this one directory
    # for the duration of the evaluator-controlled invocation.  The system
    # temporary directory remains traversable when /logs is deliberately
    # private to the evaluator.  The verified copy is persisted in the
    # evaluator-owned log directory, where the candidate has no write access.
    test_report_directory = Path(
        tempfile.mkdtemp(prefix="migration-test-execution-")
    )
    raw_test_report = test_report_directory / "report.json"
    assertion_log = test_report_directory / "assertions.jsonl"
    raw_test_report.touch()
    assertion_log.touch()
    if identity is not None:
        os.chown(test_report_directory, identity[0], identity[1])
        os.chown(raw_test_report, identity[0], identity[1])
        os.chown(assertion_log, identity[0], identity[1])
        os.chmod(test_report_directory, 0o700)
    else:
        os.chmod(test_report_directory, 0o700)
    test_execution_report_path: Path | None = None
    try:
        try:
            vitest_config, _ = _write_evaluator_vitest_files(
                candidate,
                includes=_evaluator_test_includes(assets, config),
            )
        except (OSError, ValueError) as exc:
            return _write_result(
                logs,
                _infrastructure_result(
                    f"cannot prepare the evaluator test configuration: {exc}",
                    candidate=candidate,
                    config=config,
                    units=units,
                    receipts=receipts,
                ),
            )
        test_environment = {
            **environment,
            "MIGRATION_ASSERTION_LOG": str(assertion_log),
        }
        test_execution_receipts, test_execution_failed, test_execution_infra = (
            _run_declared_commands(
                [
                    (
                        "test-execution",
                        _test_execution_command(
                            raw_test_report, config=vitest_config
                        ),
                    )
                ],
                cwd=candidate,
                timeout=timeout,
                environment=test_environment,
                start_index=len(receipts) + 1,
                identity=identity,
                retry_reset=lambda: (
                    raw_test_report.write_text("", encoding="utf-8"),
                    assertion_log.write_text("", encoding="utf-8"),
                ),
            )
        )
        receipts.extend(test_execution_receipts)
        if test_execution_receipts:
            test_receipt = test_execution_receipts[-1]
            if raw_test_report.is_file() and raw_test_report.stat().st_size > 0:
                try:
                    test_execution_report_path = _write_test_execution_report(
                        raw_test_report,
                        test_execution_report,
                        candidate=candidate,
                        assertion_log=assertion_log,
                    )
                except InfrastructureError as exc:
                    test_receipt["report_error"] = str(exc)
            if test_execution_report_path is None:
                if test_receipt.get("failed_phase") is None:
                    test_receipt["failed_phase"] = (
                        "invalid-report"
                        if test_receipt.get("report_error")
                        else "no-report"
                    )
            test_receipt["report_present"] = (
                test_execution_report_path is not None
            )
    finally:
        _remove_evaluator_vitest_files(candidate)
        shutil.rmtree(test_report_directory, ignore_errors=True)
    if test_execution_infra:
        return _write_result(
            logs,
            _infrastructure_result(
                "candidate test toolchain is unavailable",
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
            ),
        )
    # 4. The evaluator-owned runner imports the hidden harness and drives the
    #    candidate only through the public protocol in an isolated child
    #    process.  The observation report is written outside candidate reach.
    report = logs / "harness-report.json"
    harness = runtime.get("harness_command")
    if not isinstance(harness, Sequence) or isinstance(harness, (str, bytes)) or not harness:
        final_graph_receipt = _dependency_graph_receipt(
            candidate,
            phase="final",
            index=len(receipts) + 1,
            previous=post_build_graph_receipt,
        )
        receipts.append(final_graph_receipt)
        if not final_graph_receipt["valid"]:
            message = _dependency_failure_message(
                "final", final_graph_receipt
            )
            return _write_result(
                logs,
                _candidate_evidence_result(
                    message,
                    candidate=candidate,
                    config=config,
                    units=units,
                    test_baseline=test_baseline,
                    receipts=receipts,
                    observation_errors=[message],
                ),
            )
        return _write_result(
            logs,
            _infrastructure_result(
                "target runtime harness command is missing",
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
            ),
        )
    harness_command = [
        str(item).format_map(
            {
                "repo": str(candidate),
                "assets": str(public_assets),
                "verifier": str(assets / "verifier"),
                "report": str(report),
            }
        )
        for item in harness
    ]
    completed, harness_receipt = _run_with_receipt(
        harness_command,
        kind="target-execution",
        index=len(receipts) + 1,
        cwd=bundle,
        timeout=timeout,
        environment=environment,
    )
    _annotate_report_presence(harness_receipt, report)
    receipts.append(harness_receipt)
    # The harness is the last candidate-controlled execution stage.  Re-audit
    # the dependency graph it actually used before accepting any evidence.
    final_graph_receipt = _dependency_graph_receipt(
        candidate,
        phase="final",
        index=len(receipts) + 1,
        previous=post_build_graph_receipt,
    )
    receipts.append(final_graph_receipt)
    if not final_graph_receipt["valid"]:
        message = _dependency_failure_message("final", final_graph_receipt)
        return _write_result(
            logs,
            _candidate_evidence_result(
                message,
                candidate=candidate,
                config=config,
                units=units,
                test_baseline=test_baseline,
                receipts=receipts,
                observation_errors=[message],
                harness_returncode=harness_receipt.get("returncode"),
            ),
        )
    if completed is None:
        if harness_receipt.get("infrastructure_error") is True:
            return _write_result(
                logs,
                _infrastructure_result(
                    "candidate harness could not be launched",
                    candidate=candidate,
                    config=config,
                    units=units,
                    receipts=receipts,
                    harness_returncode=harness_receipt.get("returncode"),
                ),
            )
        message = _candidate_execution_error(
            "candidate harness did not produce an observations report", receipts
        )
        return _write_result(
            logs,
            _candidate_evidence_result(
                message,
                candidate=candidate,
                config=config,
                units=units,
                test_baseline=test_baseline,
                receipts=receipts,
                observation_errors=[message],
                harness_returncode=harness_receipt.get("returncode"),
            ),
        )
    if completed.returncode == 3:
        return _write_result(
            logs,
            _infrastructure_result(
                "protected evaluation asset integrity check failed: "
                + (completed.stderr or "")[-2000:],
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
                harness_returncode=completed.returncode,
            ),
        )
    if completed.returncode != 0:
        message = _candidate_execution_error(
            "candidate harness exited without usable observation evidence", receipts
        )
        return _write_result(
            logs,
            _candidate_evidence_result(
                message,
                candidate=candidate,
                config=config,
                units=units,
                test_baseline=test_baseline,
                receipts=receipts,
                observation_errors=[message],
                harness_returncode=completed.returncode,
            ),
        )
    if not report.is_file():
        message = "candidate harness produced no observations report"
        return _write_result(
            logs,
            _candidate_evidence_result(
                message,
                candidate=candidate,
                config=config,
                units=units,
                test_baseline=test_baseline,
                receipts=receipts,
                observation_errors=[message],
                harness_returncode=completed.returncode,
            ),
        )

    try:
        payload = _load_json(report)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        message = f"candidate observation report is not valid JSON: {exc}"
        result = _candidate_evidence_result(
            message,
            candidate=candidate,
            config=config,
            units=units,
            test_baseline=test_baseline,
            receipts=receipts,
            observation_errors=[message],
            harness_returncode=completed.returncode,
        )
        return _write_result(logs, result)

    try:
        payload = normalize_observation_report(
            payload,
            role="candidate",
            returncode=completed.returncode,
            report_path=str(report),
            config=config,
        )
    except ValueError as exc:
        return _write_result(
            logs,
            _infrastructure_result(
                f"observation executor config is invalid: {exc}",
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
                harness_returncode=completed.returncode,
            ),
        )

    contract_errors = list(
        observation_contract_errors(
            payload, config, actual_returncode=completed.returncode
        )
    )
    receipts.append(
        {
            "schema_version": "migration-execution-receipt:v1",
            "kind": "report",
            "index": len(receipts) + 1,
            "returncode": 0 if not contract_errors else 1,
            "timed_out": False,
            "signal": None,
            "failed_phase": None if not contract_errors else "report",
            "report_present": True,
            "valid": not contract_errors,
            "contract_errors": contract_errors,
        }
    )
    observations = payload.get("observations", [])
    if not isinstance(observations, list):
        observations = []

    compared: list[dict[str, object]] = []
    for row in observations:
        if not isinstance(row, Mapping):
            continue
        current = dict(row)
        baseline_row = baseline["by_id"].get(str(current.get("id")))
        if current.get("kind") in {"P2P", "DIFFERENTIAL"}:
            if baseline_row is None:
                contract_errors.append(
                    "candidate observation has no protected source baseline: "
                    f"{current.get('id')}"
                )
            else:
                current["source_value"] = baseline_row.get("value")
                current["candidate_value"] = current.get("value")
                comparison_status = compare(
                    _comparison_projection(
                        baseline_row.get("value"), target_evidence=False
                    ),
                    _comparison_projection(
                        current.get("value"), target_evidence=False
                    ),
                    config.get("normalization_policy", {}),
                )["status"]
                current["status"] = merge_comparison_status(
                    str(current.get("status", "INCONCLUSIVE")),
                    str(comparison_status),
                )
        compared.append(current)

    (
        format_errors,
        format_errors_by_id,
        format_records,
        format_contract_errors,
    ) = _candidate_format_errors(
        format_expectations,
        compared,
        expected_digest=str(cross_inputs["digest"]),
    )
    observation_kinds = {
        str(row.get("id")): str(row.get("kind") or "")
        for row in compared
        if isinstance(row, Mapping)
    }
    receipts.append(
        {
            "schema_version": "migration-execution-receipt:v1",
            "kind": "format-evidence",
            "index": len(receipts) + 1,
            "returncode": 0 if not format_errors else 1,
            "timed_out": False,
            "signal": None,
            "failed_phase": None if not format_errors else "format-evidence",
            "report_present": True,
            "valid": not format_errors,
            "errors": format_errors,
            "cross_inputs_sha256": cross_inputs["digest"],
        }
    )
    try:
        (
            source_errors,
            source_errors_by_id,
            source_receipt,
            source_contract_errors,
        ) = _source_consume_errors(
            format_expectations,
            assets=assets,
            cases=_source_consume_cases(format_expectations, format_records),
            source_commit=str(baseline.get("source_commit") or ""),
            timeout=min(timeout, 120.0),
            environment=environment,
            observation_kinds=observation_kinds,
        )
    except InfrastructureError as exc:
        return _write_result(
            logs,
            _infrastructure_result(
                str(exc),
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
                observations=compared,
                observation_errors=list(contract_errors),
                harness_returncode=completed.returncode,
            ),
        )
    source_receipt["index"] = len(receipts) + 1
    receipts.append(source_receipt)
    if format_errors or source_errors:
        # Only defects that break an existing public contract invalidate the
        # candidate.  A miss on a target-only F2P capability still fails its
        # own observation below and lowers the F2P ratio, but it must not turn
        # a partially migrated candidate into an invalid one.
        contract_errors.extend([*format_contract_errors, *source_contract_errors])
        # A source-replay failure is scoped to the observation whose payload
        # could not be restored.  It must not mark unrelated format
        # observations as failed: that cascade turned one real defect into a
        # false FAIL for every P2P/F2P format observation and failed the
        # p2p regression gate for candidates whose own comparisons passed.
        affected = set(format_errors_by_id) | set(source_errors_by_id)
        for row in compared:
            if row.get("id") in affected:
                row["status"] = "FAIL"

    isolation = _isolation_probe(
        candidate=candidate,
        public_assets=public_assets,
        identity=identity,
        timeout=min(timeout, 30.0),
        environment=environment,
        hidden_root=bundle,
        verifier_root=logs,
        candidate_logs=candidate_logs,
        candidate_home=home,
    )
    post_hashes = _protected_fingerprints(bundle)
    if pre_hashes != post_hashes:
        isolation = {
            **isolation,
            "status": "FAIL",
            "violations": [
                *list(isolation.get("violations", [])),
                "protected-verifier-files-changed",
            ],
            "evidence": [
                *list(isolation.get("evidence", [])),
                "protected verifier files changed while candidate code was running",
            ],
        }

    by_kind = {
        kind: [row for row in compared if row.get("kind") == kind]
        for kind in ("P2P", "F2P", "DIFFERENTIAL", "STATE")
    }
    try:
        evidence = build_hardgate_evidence(
            candidate,
            config=config,
            observations=compared,
            observation_errors=contract_errors,
            receipts=receipts,
            patch_applicable=True,
            source_test_baseline=test_baseline,
            isolation=isolation,
            execution_report=test_execution_report_path,
        )
    except ConfigurationError as exc:
        return _write_result(
            logs,
            _infrastructure_result(
                str(exc),
                candidate=candidate,
                config=config,
                units=units,
                receipts=receipts,
                observations=compared,
                observation_errors=contract_errors,
                harness_returncode=completed.returncode,
                isolation=isolation,
            ),
        )
    hardgate = evaluate_hardgate(candidate, evidence)
    policy = config.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    result = _finish_result(
        config=config,
        units=units,
        hardgate=hardgate,
        p2p=by_kind["P2P"],
        f2p=by_kind["F2P"],
        differential=by_kind["DIFFERENTIAL"],
        state={
            "applicable": (
                state_declaration["applicable"]
                if state_declaration is not None
                else None
            ),
            "checks": by_kind["STATE"],
            "applicability": (
                state_declaration["sources"]
                if state_declaration is not None
                else {"error": state_applicability_error}
            ),
        },
        observations=compared,
        observation_errors=contract_errors,
        receipts=receipts,
        harness_returncode=completed.returncode,
        isolation=isolation,
        candidate_root=candidate,
    )
    return _write_result(logs, result)


__all__ = ["InfrastructureError", "run_harbor_verifier"]
