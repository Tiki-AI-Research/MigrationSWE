"""Strict loading of the protected source baseline.

The baseline is an evaluator asset.  A missing, stale, empty or internally
inconsistent baseline is an infrastructure failure, never candidate evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


BASELINE_SCHEMA = "migration-source-baseline:v6"

# Very large, highly repetitive source-era payloads are stored as an exact
# run-length encoding so the protected baseline stays reviewable.  A marker is
# self-certifying: the digest covers the fully expanded JSON value, so a
# decoder can never silently substitute a different payload.
COMPACT_RLE_TAG = "$rle"
_COMPACT_FIELDS = frozenset({"$rle", "$count", "$sha256"})

# These observations carry evaluator-owned wire-format evidence.  A baseline
# without the actual payloads and revival values cannot prove public format
# compatibility and must be rejected as stale.
_REQUIRED_FORMAT_IDS = frozenset(
    {
        "P2P-007",
        "P2P-008",
        "P2P-011",
        "P2P-014",
        "P2P-021",
        "P2P-022",
        "P2P-038",
        "P2P-044",
        "P2P-051",
    }
)


def _validate_format_evidence(identifier: str, value: object) -> None:
    if not isinstance(value, Mapping):
        raise BaselineError(
            f"source baseline observation {identifier} lacks a typed JSON value"
        )
    content = value.get("value")
    if not isinstance(content, Mapping):
        raise BaselineError(
            f"source baseline observation {identifier} value is not an object"
        )
    if identifier not in _REQUIRED_FORMAT_IDS:
        return
    fmt = content.get("format")
    if not isinstance(fmt, Mapping):
        raise BaselineError(
            f"source baseline observation {identifier} lacks format evidence"
        )
    if not isinstance(fmt.get("case_id"), str) or not fmt["case_id"]:
        raise BaselineError(
            f"source baseline observation {identifier} has no format case id"
        )
    kind = fmt.get("kind")

    def typed_error() -> bool:
        error = fmt.get("error")
        if not isinstance(error, Mapping):
            return False
        # A source-era runtime may accept a value the target rejects.  That is
        # a complete observation (`threw: false`) rather than missing evidence.
        if error.get("threw") is False:
            return True
        return {"threw", "name", "message"} <= set(error)

    if "payload" in fmt:
        for key in ("payload", "parse", "unflatten", "uneval_value", "cross"):
            if key not in fmt:
                raise BaselineError(
                    f"source baseline observation {identifier} format evidence "
                    f"is missing {key}"
                )
        cross = fmt.get("cross")
        # A source run without evaluator cross-inputs has no source-to-source
        # comparison, so a null cross record is valid here.
        if cross is not None and (
            not isinstance(cross, Mapping)
            or not all(key in cross for key in ("parse", "unflatten", "uneval"))
        ):
            raise BaselineError(
                f"source baseline observation {identifier} lacks cross-consumption "
                "evidence"
            )
        return
    # A source-era runtime may reject a value shape that the target supports.
    # That is recorded as an ordinary typed rejection, never as silent absence.
    if kind == "error" or "input" in fmt or "parse_payload" in fmt or "unflatten_value" in fmt:
        if not typed_error():
            raise BaselineError(
                f"source baseline observation {identifier} lacks error semantics"
            )
        return
    if typed_error():
        return
    raise BaselineError(
        f"source baseline observation {identifier} has unsupported format evidence"
    )


class BaselineError(ValueError):
    """The protected source baseline cannot be used for comparison."""


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _expand_rle_marker(identifier: str, marker: Mapping[str, Any]) -> list[Any]:
    runs = marker.get(COMPACT_RLE_TAG)
    count = marker.get("$count")
    digest = marker.get("$sha256")
    if not isinstance(runs, list) or not runs:
        raise BaselineError(
            f"source baseline observation {identifier} has an empty RLE payload"
        )
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise BaselineError(
            f"source baseline observation {identifier} has an invalid RLE length"
        )
    expanded: list[Any] = []
    for run in runs:
        if (
            not isinstance(run, list)
            or len(run) != 2
            or not isinstance(run[1], int)
            or isinstance(run[1], bool)
            or run[1] <= 0
        ):
            raise BaselineError(
                f"source baseline observation {identifier} has an invalid RLE run"
            )
        expanded.extend([run[0]] * run[1])
    if len(expanded) != count:
        raise BaselineError(
            f"source baseline observation {identifier} RLE length is inconsistent"
        )
    if not isinstance(digest, str) or _canonical_digest(expanded) != digest:
        raise BaselineError(
            f"source baseline observation {identifier} RLE payload digest mismatch"
        )
    return expanded


def expand_compact_value(identifier: str, value: object) -> object:
    """Expand an exact run-length marker, recursing through the payload.

    Only objects whose field set is exactly the marker contract are treated as
    encoded arrays.  Every other object is ordinary evidence and is preserved
    verbatim.
    """

    if isinstance(value, Mapping):
        if set(value) == _COMPACT_FIELDS:
            return _expand_rle_marker(identifier, value)
        return {
            key: expand_compact_value(identifier, item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [expand_compact_value(identifier, item) for item in value]
    return value


def _config_fingerprint(config: Mapping[str, Any]) -> str:
    tests = config.get("tests", {})
    tests = tests if isinstance(tests, Mapping) else {}
    identifiers = {
        group: sorted(
            str(row.get("id"))
            for row in tests.get(group, [])
            if isinstance(row, Mapping) and row.get("id")
        )
        for group in ("p2p", "f2p", "differential", "state")
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(identifiers, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_source_baseline(
    path: Path,
    *,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Load and fully validate the baseline before any candidate admission."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BaselineError(f"source baseline is missing: {path}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"source baseline is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise BaselineError("source baseline must be a JSON object")
    if payload.get("schema_version") != BASELINE_SCHEMA:
        raise BaselineError(
            f"source baseline schema_version must be {BASELINE_SCHEMA}"
        )
    if payload.get("status") != "VALID":
        raise BaselineError(
            f"source baseline status is not VALID: {payload.get('status')!r}"
        )
    declared = str(payload.get("declared_base_commit") or "")
    if declared != str(config.get("base_commit") or ""):
        raise BaselineError(
            "source baseline declared_base_commit does not match config.base_commit"
        )
    if not str(payload.get("source_commit") or ""):
        raise BaselineError("source baseline does not record its source commit")
    expected_runtime = config.get("source_runtime_sha256")
    if payload.get("source_runtime_sha256") != expected_runtime:
        raise BaselineError(
            "source baseline runtime digest does not match config.source_runtime_sha256"
        )
    if payload.get("config_fingerprint") != _config_fingerprint(config):
        raise BaselineError(
            "source baseline config fingerprint does not match the required test ids"
        )

    tests = config.get("tests", {})
    tests = tests if isinstance(tests, Mapping) else {}
    required: dict[str, str] = {}
    for group, kind in (("p2p", "P2P"), ("f2p", "F2P")):
        rows = tests.get(group, [])
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise BaselineError(f"config.tests.{group} must be an array")
        for row in rows:
            identifier = row.get("id") if isinstance(row, Mapping) else None
            if not isinstance(identifier, str) or not identifier:
                raise BaselineError(f"config.tests.{group} contains an invalid id")
            if identifier in required:
                raise BaselineError(f"config contains duplicate observation id: {identifier}")
            required[identifier] = kind

    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise BaselineError("source baseline observations must be a non-empty array")
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping):
            raise BaselineError(f"source baseline observations[{index}] must be an object")
        identifier = row.get("id")
        kind = row.get("kind")
        status = row.get("status")
        if not isinstance(identifier, str) or not identifier:
            raise BaselineError(
                f"source baseline observations[{index}] has an invalid id"
            )
        if identifier in by_id:
            raise BaselineError(f"source baseline contains duplicate observation id: {identifier}")
        if kind not in {"P2P", "F2P"}:
            raise BaselineError(
                f"source baseline observation {identifier} has unsupported kind: {kind!r}"
            )
        if required.get(identifier) != kind:
            raise BaselineError(
                f"source baseline observation {identifier} is not a required {required.get(identifier)} id"
            )
        if status not in {"PASS", "FAIL"}:
            raise BaselineError(
                f"source baseline observation {identifier} has invalid status: {status!r}"
            )
        value = row.get("value")
        if (
            not isinstance(value, Mapping)
            or value.get("type") != "json"
            or "value" not in value
        ):
            raise BaselineError(
                f"source baseline observation {identifier} lacks a typed JSON value"
            )
        try:
            json.dumps(value["value"], allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise BaselineError(
                f"source baseline observation {identifier} value is not canonical JSON"
            ) from exc
        try:
            expanded = expand_compact_value(identifier, value["value"])
            json.dumps(expanded, allow_nan=False)
        except BaselineError:
            raise
        except (TypeError, ValueError) as exc:
            raise BaselineError(
                f"source baseline observation {identifier} value is not canonical JSON"
            ) from exc
        if expanded is not value["value"]:
            value = dict(value)
            value["value"] = expanded
            row = dict(row)
            row["value"] = value
        _validate_format_evidence(identifier, value)
        by_id[identifier] = row

    missing = sorted(set(required) - set(by_id))
    unexpected = sorted(set(by_id) - set(required))
    if missing:
        raise BaselineError(f"source baseline is missing required ids: {', '.join(missing)}")
    if unexpected:
        raise BaselineError(f"source baseline has unexpected ids: {', '.join(unexpected)}")

    p2p = [row for row in by_id.values() if row["kind"] == "P2P"]
    f2p = [row for row in by_id.values() if row["kind"] == "F2P"]
    if any(row["status"] != "PASS" for row in p2p):
        failed = sorted(str(row["id"]) for row in p2p if row["status"] != "PASS")
        raise BaselineError(
            f"source baseline P2P must all pass; failed: {', '.join(failed)}"
        )
    if any(row["status"] != "FAIL" for row in f2p):
        passed = sorted(str(row["id"]) for row in f2p if row["status"] != "FAIL")
        raise BaselineError(
            f"source baseline F2P must all be rejected; unexpectedly passed: {', '.join(passed)}"
        )
    if payload.get("p2p_all_pass") is not True or payload.get("f2p_rejected") is not True:
        raise BaselineError(
            "source baseline calibration flags do not match its observations"
        )
    return {
        "schema_version": BASELINE_SCHEMA,
        "status": "VALID",
        "source_commit": str(payload["source_commit"]),
        "declared_base_commit": declared,
        "source_runtime_sha256": expected_runtime,
        "config_fingerprint": payload["config_fingerprint"],
        "observations": [dict(by_id[identifier]) for identifier in required],
        "by_id": by_id,
        "path": str(path),
    }


__all__ = [
    "BASELINE_SCHEMA",
    "BaselineError",
    "expand_compact_value",
    "load_source_baseline",
]
