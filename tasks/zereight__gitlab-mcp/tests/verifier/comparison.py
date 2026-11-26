"""Shared monotonic observation comparison semantics."""

from __future__ import annotations


def _semantic_schema(value: object) -> object:
    """Normalize schema annotations without changing accepted inputs."""
    if isinstance(value, list):
        return [_semantic_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"$schema", "description", "title"}:
            continue
        if key in {"properties", "$defs", "definitions", "patternProperties"}:
            # A property called title/description is a field, not an annotation.
            result[key] = {name: _semantic_schema(schema) for name, schema in item.items()}
        else:
            result[key] = _semantic_schema(item)
    if result.get("type") == "object":
        result.setdefault("additionalProperties", True)
        result.setdefault("required", [])
        result["required"] = sorted(result["required"])
    return result


def normalize_omg_projection(value: object) -> object:
    """Canonicalize only order/representation that the OMG contract leaves free.

    Source observations are frozen, so normalize both sides at comparison time.
    Story order, criteria, router factors and every behavioral value stay exact.
    """
    if isinstance(value, list):
        return [normalize_omg_projection(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize_omg_projection(item) for key, item in value.items()}
    if {"allow_stop", "remaining", "message"} <= result.keys() and isinstance(result["remaining"], list):
        result["remaining"] = sorted(result["remaining"], key=repr)
    if "tool_order" in result and isinstance(result["tool_order"], list):
        result["tool_order"] = sorted(result["tool_order"], key=repr)
    if result.get("step") == "tools" and isinstance(result.get("captured"), list):
        result["captured"] = sorted([
            {**tool, "inputSchema": _semantic_schema(tool.get("inputSchema", {}))}
            for tool in result["captured"]
        ], key=lambda tool: str(tool.get("name", "")))
    if result.get("step") == "module" and result.get("module") == "safe_json_parse":
        parsed = result.get("captured")
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            # The public contract requires rejection, not a private helper's
            # unused data/error fields or their representation.
            result["captured"] = {"ok": False, "data": None, "error": "<rejected>"}
    return result


def status_only_projection(value: object) -> bool:
    """Return whether a typed observation value is only a status wrapper."""

    if not isinstance(value, dict) or value.get("type") != "json":
        return False
    payload = value.get("value")
    if not isinstance(payload, dict) or not payload:
        return False
    return all(
        str(key).casefold() in {"result", "outcome", "status"}
        and isinstance(item, str)
        and item.strip().casefold()
        in {
            "pass",
            "passed",
            "success",
            "successes",
            "ok",
            "fail",
            "failed",
            "failure",
            "error",
            "errors",
            "inconclusive",
        }
        for key, item in payload.items()
    )


def merge_comparison_status(original_status: str, comparison_status: str) -> str:
    """Apply a value comparison without ever upgrading an existing failure.

    Comparison is an additional constraint.  It may turn a passing observation
    into a failure, but every non-PASS producer status is authoritative and must
    survive unchanged.
    """

    original = str(original_status)
    return str(comparison_status) if original == "PASS" else original


__all__ = ["merge_comparison_status", "status_only_projection"]
