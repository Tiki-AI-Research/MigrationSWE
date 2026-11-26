"""Validate container observations against the frozen test manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence


_KINDS = {
    "p2p": "P2P",
    "f2p": "F2P",
    "differential": "DIFFERENTIAL",
    "state": "STATE",
    "contract": "CONTRACT",
}
_STATUSES = {"PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT"}
_ROLES = {"source", "target", "candidate", "mutation"}
OBSERVATION_SCHEMA = "migration-observations:v6"


def _executor_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    identifier = value.get("id")
    version = value.get("version")
    if (
        not isinstance(identifier, str)
        or not identifier
        or not isinstance(version, str)
        or not version
    ):
        return None
    return identifier, version


def normalize_observation_report(
    payload: Mapping[str, Any],
    *,
    role: str,
    returncode: int,
    report_path: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in _ROLES:
        raise ValueError("observation role is invalid")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ValueError("observation returncode is invalid")
    if not report_path.startswith("/logs/"):
        raise ValueError("observation report path is invalid")
    normalized = dict(payload)
    expected_kinds = _expected(config)[0] if config is not None else {}
    rows = payload.get("observations")
    if isinstance(rows, list):
        normalized_rows: list[object] = []
        for row in rows:
            if not isinstance(row, Mapping):
                normalized_rows.append(row)
                continue
            current = dict(row)
            identifier = current.get("id")
            if (
                "kind" not in current
                and isinstance(identifier, str)
                and identifier in expected_kinds
            ):
                current["kind"] = expected_kinds[identifier]
            status = current.get("status")
            if isinstance(status, str):
                current["status"] = status.upper()
            if "value" in current:
                value = current["value"]
                if not (
                    isinstance(value, Mapping)
                    and value.get("type") == "json"
                    and "value" in value
                ):
                    current["value"] = {"type": "json", "value": value}
            normalized_rows.append(current)
        normalized["observations"] = normalized_rows
    normalized.setdefault("role", role)
    normalized.setdefault(
        "receipt",
        {
            "returncode": returncode,
            "report_path": report_path,
        },
    )
    if config is not None and "executor" not in normalized:
        identity = _executor_identity(config.get("executor"))
        if identity is not None:
            normalized["executor"] = {
                "id": identity[0],
                "version": identity[1],
            }
    if config is not None:
        normalized["runtime_sha256"] = config.get(
            "source_runtime_sha256" if role == "source" else "target_runtime_sha256"
        )
    return normalized


def _expected(config: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    tests = config.get("tests")
    if not isinstance(tests, Mapping):
        return {}, ["runtime config tests mapping is missing"]
    expected: dict[str, str] = {}
    errors: list[str] = []
    for group, kind in _KINDS.items():
        rows = tests.get(group, [])
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            errors.append(f"runtime config tests.{group} must be an array")
            continue
        for row in rows:
            identifier = row.get("id") if isinstance(row, Mapping) else None
            if not isinstance(identifier, str) or not identifier:
                errors.append(f"runtime config tests.{group} contains an invalid id")
            elif identifier in expected:
                errors.append(f"runtime config contains duplicate observation id: {identifier}")
            else:
                expected[identifier] = kind
    return expected, errors


def _row_errors(
    observations: object,
    expected: Mapping[str, str],
    *,
    typed_values: bool,
) -> list[str]:
    if not isinstance(observations, list):
        return ["container observations must be an array"]
    errors: list[str] = []
    actual: set[str] = set()
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping):
            errors.append(f"observations[{index}] must be an object")
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        status = row.get("status")
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"observations[{index}] has an invalid id")
            continue
        if identifier in actual:
            errors.append(f"container returned duplicate observation id: {identifier}")
            continue
        actual.add(identifier)
        if not isinstance(kind, str):
            errors.append(f"observation {identifier} has an invalid kind")
            continue
        if status not in _STATUSES:
            errors.append(f"observation {identifier} has an invalid status: {status}")
        expected_kind = expected.get(identifier)
        if expected_kind is not None and kind != expected_kind:
            errors.append(
                f"observation {identifier} kind mismatch: expected {expected_kind}, got {kind}"
            )
        value = row.get("value")
        if typed_values:
            if (
                not isinstance(value, Mapping)
                or value.get("type") != "json"
                or "value" not in value
            ):
                raise ValueError(
                    f"observation {identifier} value must be a typed JSON envelope"
                )
            value = value["value"]
        try:
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if "evidence" in row:
                json.dumps(
                    row["evidence"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
        except (TypeError, ValueError):
            raise ValueError(
                f"observation {identifier} value must be canonical JSON"
            )
    for identifier in sorted(set(expected) - set(actual)):
        errors.append(f"missing observation: {identifier}")
    for identifier in sorted(set(actual) - set(expected)):
        errors.append(f"unexpected observation: {identifier}")
    return errors


def validate_observation_set(
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    actual_returncode: int | None = None,
) -> tuple[str, ...]:
    if payload.get("schema_version") != OBSERVATION_SCHEMA:
        raise ValueError(f"observation schema_version must be {OBSERVATION_SCHEMA}")
    if "adapter" in payload:
        raise ValueError("observation adapter field is no longer accepted")
    role = payload.get("role")
    if role not in _ROLES:
        raise ValueError("observation role is invalid")
    expected_identity = _executor_identity(config.get("executor"))
    actual_identity = _executor_identity(payload.get("executor"))
    if expected_identity is None or actual_identity is None:
        raise ValueError("observation executor descriptor is required")
    if expected_identity != actual_identity:
        raise ValueError("observation executor descriptor does not match config")
    expected_runtime = config.get(
        "source_runtime_sha256" if role == "source" else "target_runtime_sha256"
    )
    if payload.get("runtime_sha256") != expected_runtime:
        raise ValueError("observation runtime digest does not match config")
    receipt = payload.get("receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("observation receipt is required")
    receipt_code = receipt.get("returncode")
    report_path = receipt.get("report_path")
    if (
        not isinstance(receipt_code, int)
        or isinstance(receipt_code, bool)
        or not isinstance(report_path, str)
        or not report_path.startswith("/logs/")
    ):
        raise ValueError("observation receipt is invalid")
    if actual_returncode is not None and receipt_code != actual_returncode:
        raise ValueError(
            "observation receipt returncode does not match container returncode"
        )
    expected, errors = _expected(config)
    errors.extend(
        _row_errors(payload.get("observations"), expected, typed_values=True)
    )
    return tuple(errors)


def load_report_selection(assets_root: Path) -> Mapping[str, object] | None:
    """Load the frozen observation projection selection, if the Case has one.

    The file is produced only when the authoring smoke controls drop
    observations.  A missing or malformed selection is treated as "no
    projection" so legacy bundles keep their previous behaviour.
    """

    path = Path(assets_root) / "shared" / "validated-observations" / "selection.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    original = payload.get("original_ids")
    accepted = payload.get("accepted_ids")
    if not isinstance(original, list) or not isinstance(accepted, list):
        return None
    if any(not isinstance(item, str) or not item for item in [*original, *accepted]):
        return None
    return {"original_ids": list(original), "accepted_ids": list(accepted)}


def project_report(
    payload: Mapping[str, Any],
    selection: Mapping[str, object] | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Project a raw harness report onto the frozen accepted observation set.

    The raw report must still contain exactly the frozen ``original_ids`` (no
    missing, extra, or duplicate rows); only then are the frozen extras dropped.
    This keeps the anti-cheat guarantee that the harness cannot silently alter
    the observation inventory.  ``selection is None`` disables projection.
    """

    projected = dict(payload)
    if selection is None:
        return projected, ()
    rows = payload.get("observations")
    if not isinstance(rows, list):
        return projected, ("original observation report is incomplete or invalid",)
    if any(not isinstance(row, Mapping) for row in rows):
        return projected, ("original observation report contains invalid rows",)
    ids = [str(row.get("id")) for row in rows]
    if (
        len(ids) != len(set(ids))
        or set(ids) != {str(item) for item in selection["original_ids"]}
    ):
        return projected, ("original observation report is incomplete or invalid",)
    accepted = {str(item) for item in selection["accepted_ids"]}
    projected["observations"] = [dict(row) for row in rows if str(row.get("id")) in accepted]
    return projected, ()


def projected_returncode(payload: Mapping[str, Any]) -> int:
    """Derive the observation-level exit signal from a projected report.

    The authoring runtime used to encode "all accepted observations PASS" as a
    zero exit and any failure as a non-zero exit.  That signal now lives in the
    verifier so it does not depend on the candidate runtime language or tools.
    """

    rows = payload.get("observations")
    if not isinstance(rows, list) or not rows:
        return 2
    statuses = [str(row.get("status")) for row in rows if isinstance(row, Mapping)]
    if len(statuses) != len(rows):
        return 2
    return 0 if all(status == "PASS" for status in statuses) else 1


def observation_contract_errors(
    observations: object,
    config: Mapping[str, Any],
    *,
    actual_returncode: int | None = None,
) -> tuple[str, ...]:
    if isinstance(observations, Mapping):
        try:
            return validate_observation_set(
                observations,
                config,
                actual_returncode=actual_returncode,
            )
        except ValueError as exc:
            return (str(exc),)
    expected, errors = _expected(config)
    errors.extend(_row_errors(observations, expected, typed_values=False))
    return tuple(errors)


__all__ = [
    "OBSERVATION_SCHEMA",
    "load_report_selection",
    "normalize_observation_report",
    "observation_contract_errors",
    "project_report",
    "projected_returncode",
    "validate_observation_set",
]
