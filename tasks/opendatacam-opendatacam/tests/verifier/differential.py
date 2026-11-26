"""Normalization and comparison helpers for Migration Grader differential checks."""
from __future__ import annotations

import re
from typing import Any, Mapping


_PYTHON_REPR_ADDRESS = re.compile(r"(?<= at )0x[0-9a-fA-F]+(?=>)")
# Generic hexadecimal addresses/ids that vary between runs and machines.
_HEX_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")


def _drop_partial_tail(value: str) -> str:
    """Drop an unterminated final line from a captured multi-line stream.

    A streamed/captured body (for example the ``/console`` stdout dump) is read
    up to a byte budget, so its final line is cut at an arbitrary offset that is
    not part of the migrated behaviour.  Only a multi-line value whose last line
    is unterminated is affected; single-line and newline-terminated values are
    left untouched.
    """

    if value and not value.endswith("\n") and "\n" in value:
        return value[: value.rfind("\n") + 1]
    return value

# Role-identity keys are ignored: the source and target run different tooling,
# so an identical behavior projection legitimately reports a different role
# identity.  The deterministic native P2P collector also excludes this metadata
# from its canonical value, and the key is never a behavioral signal.  This is a
# conservative default for known keys; adapters/contracts may extend it through
# ``policy["role_identity_keys"]`` without any language-specific code here.
_DEFAULT_ROLE_IDENTITY_KEYS = frozenset(
    {
        "runner",
        "file",
        "title",
        "detail",
        "matched_tests",
        "native_test_count",
        "projection",
    }
)


def _role_identity_keys(policy: Mapping[str, Any]) -> frozenset[str]:
    value = policy.get("role_identity_keys")
    declared = (
        frozenset(str(item).casefold() for item in value if str(item).strip())
        if isinstance(value, (list, tuple))
        else frozenset()
    )
    return _DEFAULT_ROLE_IDENTITY_KEYS | declared


def _volatile_keys(policy: Mapping[str, Any]) -> frozenset[str]:
    """Keys dropped from structured values before comparison.

    Volatile keys carry environment/time noise (timestamps, pids, cache
    resolution metadata) that is not part of the migrated behavior.  The set is
    explicitly declared by the normalization policy so it stays auditable, and it
    never includes behavioral keys such as ``exitCode``/``status``.
    Role-identity keys are always dropped in addition to the declared set.
    """

    value = policy.get("volatile_keys")
    declared = (
        frozenset(str(item).casefold() for item in value if str(item).strip())
        if isinstance(value, (list, tuple))
        else frozenset()
    )
    return declared | _role_identity_keys(policy)


def _normalize(value: Any, policy: Mapping[str, Any], volatile: frozenset[str]) -> Any:
    if isinstance(value, dict):
        # Framework defaults are not additional task requirements. Keep the
        # status, payload, download disposition and media type, but do not reject
        # a working static/CSV response for optional cache/range headers or the
        # case of the UTF-8 charset token.
        if "headers" in value and "body" in value and "status" in value:
            value = dict(value)
            headers = value.get("headers")
            if isinstance(headers, dict):
                headers = {str(k).lower(): v for k, v in headers.items()
                           if str(k).lower() not in {"accept-ranges", "cache-control"}}
                content_type = headers.get("content-type")
                if isinstance(content_type, str):
                    parts = [part.strip().lower() for part in content_type.split(";")]
                    if parts[0] in {"text/html", "text/plain", "text/csv", "application/json"}:
                        parts = [part for part in parts if part not in {"charset=utf-8", 'charset="utf-8"'}]
                    headers["content-type"] = ";".join(parts)
                value["headers"] = headers
        return {
            str(key): _normalize(item, policy, volatile)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
            if str(key).casefold() not in volatile
        }
    if isinstance(value, list):
        rows = [_normalize(item, policy, volatile) for item in value]
        return sorted(rows, key=repr) if policy.get("ordering") == "unordered" else rows
    if isinstance(value, str):
        if policy.get("platform_paths") == "posix-normalized":
            value = value.replace("\\", "/")
        if policy.get("python_repr_addresses") == "stable-placeholder":
            value = _PYTHON_REPR_ADDRESS.sub("0x<address>", value)
        if policy.get("hex_addresses") == "stable-placeholder":
            value = _HEX_ADDRESS.sub("0x<address>", value)
        if policy.get("truncated_stream_tail") == "drop-partial-line":
            value = _drop_partial_tail(value)
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and isinstance(policy.get("floating_tolerance"), (int, float)):
        tolerance = float(policy["floating_tolerance"])
        if tolerance > 0:
            return round(value / tolerance) * tolerance
    return value


def normalize(value: Any, policy: Mapping[str, Any]) -> Any:
    return _normalize(value, policy, _volatile_keys(policy))


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
    return {"status": "PASS" if left == right else "FAIL", "source": left, "target": right}
