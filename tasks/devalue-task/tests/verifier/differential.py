"""Normalization and comparison helpers for Migration Grader differential checks."""
from __future__ import annotations

import re
from typing import Any, Mapping


_PYTHON_REPR_ADDRESS = re.compile(r"(?<= at )0x[0-9a-fA-F]+(?=>)")


def normalize(value: Any, policy: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        return {str(key): normalize(item, policy) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, list):
        rows = [normalize(item, policy) for item in value]
        return sorted(rows, key=repr) if policy.get("ordering") == "unordered" else rows
    if (
        isinstance(value, str)
        and policy.get("python_repr_addresses") == "stable-placeholder"
    ):
        return _PYTHON_REPR_ADDRESS.sub("0x<address>", value)
    if isinstance(value, float) and isinstance(policy.get("floating_tolerance"), (int, float)):
        tolerance = float(policy["floating_tolerance"])
        if tolerance > 0:
            return round(value / tolerance) * tolerance
    return value


def compare(source: Any, target: Any, policy: Mapping[str, Any]) -> dict[str, Any]:
    left, right = normalize(source, policy), normalize(target, policy)
    return {"status": "PASS" if left == right else "FAIL", "source": left, "target": right}
