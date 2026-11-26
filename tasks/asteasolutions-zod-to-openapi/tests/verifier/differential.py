"""Normalization and comparison helpers for Migration Grader differential checks."""
from __future__ import annotations

import re
from typing import Any, Mapping


_PYTHON_REPR_ADDRESS = re.compile(r"(?<= at )0x[0-9a-fA-F]+(?=>)")
# Generic hexadecimal addresses/ids that vary between runs and machines.
_HEX_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")


def _pointer_paths(
    policy: Mapping[str, Any], name: str, *, allow_root: bool = False,
) -> frozenset[tuple[str, ...]]:
    """Read exact, case-sensitive JSON Pointers; never infer fields by name."""

    pointers = policy.get(name, [])
    if not isinstance(pointers, (list, tuple)):
        raise ValueError(f"{name} must be an array of JSON Pointers")
    paths: set[tuple[str, ...]] = set()
    for pointer in pointers:
        if allow_root and pointer == "":
            paths.add(())
            continue
        if not isinstance(pointer, str) or not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
            raise ValueError(f"{name} entries must be {'valid' if allow_root else 'non-root'} JSON Pointers")
        paths.add(tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")))
    return frozenset(paths)


def _excluded_paths(policy: Mapping[str, Any]) -> frozenset[tuple[str, ...]]:
    """Only discard metadata explicitly identified by its JSON Pointer.

    Legacy ``volatile_keys``/``role_identity_keys`` declarations refer to root
    keys only. No field name is implicitly ignored, particularly inside an
    OpenAPI document or a user-defined schema's properties.
    """

    paths: set[tuple[str, ...]] = set()
    for name in ("volatile_keys", "role_identity_keys"):
        keys = policy.get(name, [])
        if not isinstance(keys, (list, tuple)) or any(not isinstance(key, str) or not key for key in keys):
            raise ValueError(f"{name} must be an array of non-empty root keys")
        paths.update((key,) for key in keys)
    paths.update(_pointer_paths(policy, "volatile_paths"))
    return frozenset(paths)


def _normalize(
    value: Any,
    policy: Mapping[str, Any],
    excluded: frozenset[tuple[str, ...]],
    path_fields: frozenset[tuple[str, ...]],
    path: tuple[str, ...] = (),
) -> Any:
    if isinstance(value, dict):
        items = value.items()
        if policy.get("ordering") == "unordered":
            items = sorted(items, key=lambda row: str(row[0]))
        return {
            str(key): _normalize(item, policy, excluded, path_fields, (*path, str(key)))
            for key, item in items
            if (*path, str(key)) not in excluded
        }
    if isinstance(value, list):
        rows = [_normalize(item, policy, excluded, path_fields, (*path, str(index))) for index, item in enumerate(value)]
        return sorted(rows, key=repr) if policy.get("ordering") == "unordered" else rows
    if isinstance(value, str):
        if policy.get("platform_paths") == "posix-normalized" and path in path_fields:
            value = value.replace("\\", "/")
        if policy.get("python_repr_addresses") == "stable-placeholder":
            value = _PYTHON_REPR_ADDRESS.sub("0x<address>", value)
        if policy.get("hex_addresses") == "stable-placeholder":
            value = _HEX_ADDRESS.sub("0x<address>", value)
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and isinstance(policy.get("floating_tolerance"), (int, float)):
        tolerance = float(policy["floating_tolerance"])
        if tolerance > 0:
            return round(value / tolerance) * tolerance
    return value


def normalize(value: Any, policy: Mapping[str, Any]) -> Any:
    """Normalize paths only at explicitly declared ``platform_path_fields``.

    Entries are JSON Pointers relative to the observation's unwrapped JSON
    value. An empty list (the default) disables path normalization; the
    empty pointer explicitly selects a value that is itself a path string.
    """

    path_fields = _pointer_paths(policy, "platform_path_fields", allow_root=True)
    return _normalize(value, policy, _excluded_paths(policy), path_fields)


def _equal(left: Any, right: Any, *, ordered: bool) -> bool:
    """Compare JSON booleans separately from numbers and respect field order."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        keys_match = list(left) == list(right) if ordered else left.keys() == right.keys()
        return keys_match and all(_equal(left[key], right[key], ordered=ordered) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b, ordered=ordered) for a, b in zip(left, right))
    return left == right


# Test runners report the same outcome tally under different key names
# (mocha: passes/failures/pending, vitest: passed/failed, pytest: passed/failed
# /skipped).  These are role/tool vocabulary, not behavior.  When a mapping is
# entirely made of recognized non-negative integer tally keys it is reduced to a
# canonical ``passed``/``failed``/``pending`` tally (zero buckets omitted) so the
# two role harnesses compare equal.  Unknown keys are left untouched, so this
# never masks a real value.
_TALLY_BUCKETS = {
    "passed": frozenset({"pass", "passes", "passed", "success", "successes", "ok"}),
    "failed": frozenset({"fail", "fails", "failures", "failed", "failure", "error", "errors"}),
    "pending": frozenset({"pending", "skipped", "skip", "todo", "disabled", "incomplete"}),
}


_STATUS_VALUE_KEYS = frozenset({"result", "outcome", "status"})
_STATUS_VALUES = {
    "pass": "PASS",
    "passed": "PASS",
    "success": "PASS",
    "successes": "PASS",
    "ok": "PASS",
    "fail": "FAIL",
    "failed": "FAIL",
    "failure": "FAIL",
    "failures": "FAIL",
    "error": "FAIL",
    "errors": "FAIL",
    "inconclusive": "INCONCLUSIVE",
    "skip": "SKIP",
    "skipped": "SKIP",
    "pending": "PENDING",
    "timeout": "TIMEOUT",
}


def _canonical_status(value: Any) -> Any:
    """Canonicalize a single-key status projection.

    Harnesses in different languages commonly wrap the same outcome under
    ``result`` or ``outcome``.  Only a one-key mapping whose value is a known
    status word is rewritten, so multi-field behavior projections are never
    masked by this normalization.
    """

    if not isinstance(value, dict) or len(value) != 1:
        return value
    key, item = next(iter(value.items()))
    if str(key).casefold() not in _STATUS_VALUE_KEYS or not isinstance(item, str):
        return value
    canonical = _STATUS_VALUES.get(item.strip().casefold())
    return {"status": canonical} if canonical is not None else value


def _canonical_tally(value: Any) -> Any:
    if not isinstance(value, dict) or not value:
        return value
    buckets = {name: 0 for name in _TALLY_BUCKETS}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return value
        bucket = next(
            (name for name, names in _TALLY_BUCKETS.items() if str(key).casefold() in names),
            None,
        )
        if bucket is None:
            return value
        buckets[bucket] += item
    return {name: total for name, total in buckets.items() if total}


def compare(source: Any, target: Any, policy: Mapping[str, Any]) -> dict[str, Any]:
    left, right = normalize(source, policy), normalize(target, policy)
    left, right = _canonical_tally(left), _canonical_tally(right)
    left, right = _canonical_status(left), _canonical_status(right)
    equal = _equal(left, right, ordered=policy.get("ordering") == "declared-order")
    return {"status": "PASS" if equal else "FAIL", "source": left, "target": right}
