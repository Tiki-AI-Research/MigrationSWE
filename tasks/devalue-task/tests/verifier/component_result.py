"""Project detailed Migration verifier output into a stable Harbor handoff."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

COMPONENT_SCHEMA = "migration-verifier-component:v1"
_GROUP_STATUSES = {"PASS", "FAIL", "INFRA_ERROR", "INCONCLUSIVE", "NA", "NOT_RUN"}
_UNEVALUATED = {"INFRA_ERROR", "INCONCLUSIVE", "NA", "NOT_RUN"}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _digest_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError(f"component result path is unsafe: {path}")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _group(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        value = {}
    status = str(value.get("status", "INCONCLUSIVE"))
    if status not in _GROUP_STATUSES:
        raise ValueError(f"{label} status is invalid: {status}")
    passed = value.get("passed", 0)
    total = value.get("total", 0)
    if (
        isinstance(passed, bool)
        or not isinstance(passed, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or passed < 0
        or total < 0
        or passed > total
    ):
        raise ValueError(f"{label} counts are invalid")
    ratio = passed / total if total > 0 and status not in _UNEVALUATED else None
    return {
        "status": status,
        "passed": passed,
        "total": total,
        "pass_ratio": ratio,
    }


def _component_status(verifier_status: object, infrastructure_error: bool) -> str:
    if infrastructure_error:
        return "infrastructure_error"
    return {
        "success": "passed",
        "failed": "candidate_failed",
        "infra_error": "infrastructure_error",
    }.get(str(verifier_status), "inconclusive")


def build_verifier_component(
    verifier_result: Mapping[str, object],
    *,
    verifier_result_path: Path,
) -> dict[str, object]:
    """Build the closed component result consumed by Harbor's outer assembler."""

    if verifier_result_path.is_symlink() or not verifier_result_path.is_file():
        raise ValueError("verifier result path is missing or unsafe")
    components = verifier_result.get("components")
    components = components if isinstance(components, Mapping) else {}
    regression = components.get("regression_test")
    regression = regression if isinstance(regression, Mapping) else {}
    p2p = _group(regression.get("p2p"), "P2P")
    f2p = _group(regression.get("f2p"), "F2P")
    verifier_status = str(verifier_result.get("status", "inconclusive"))
    infrastructure_error = verifier_status == "infra_error" or any(
        group["status"] == "INFRA_ERROR" for group in (p2p, f2p)
    )
    sha256 = _digest_file(verifier_result_path)
    if not _DIGEST.fullmatch(sha256):
        raise ValueError("verifier result digest is invalid")
    return {
        "schema_version": COMPONENT_SCHEMA,
        "status": _component_status(verifier_status, infrastructure_error),
        "cheating_detected": bool(verifier_result.get("cheating")),
        "p2p_pass_ratio": p2p["pass_ratio"],
        "f2p_pass_ratio": f2p["pass_ratio"],
        "groups": {
            "p2p": {
                key: p2p[key] for key in ("status", "passed", "total")
            },
            "f2p": {
                key: f2p[key] for key in ("status", "passed", "total")
            },
        },
        "errors": {"infrastructure_error": infrastructure_error},
        "verifier_result": {
            "path": verifier_result_path.name,
            "sha256": sha256,
        },
    }


def write_verifier_component(
    verifier_result: Mapping[str, object],
    *,
    verifier_result_path: Path,
    output_path: Path,
) -> dict[str, object]:
    """Atomically write and return one Harbor Verifier component result."""

    payload = build_verifier_component(
        verifier_result,
        verifier_result_path=verifier_result_path,
    )
    _write_json(output_path, payload)
    return payload


__all__ = [
    "COMPONENT_SCHEMA",
    "build_verifier_component",
    "write_verifier_component",
]
