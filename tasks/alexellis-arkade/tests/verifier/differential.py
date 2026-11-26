"""Normalization and comparison helpers for Migration Grader differential checks."""
from __future__ import annotations

import re
from typing import Any, Mapping


_PYTHON_REPR_ADDRESS = re.compile(r"(?<= at )0x[0-9a-fA-F]+(?=>)")
# Generic hexadecimal addresses/ids that vary between runs and machines.
_HEX_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")

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
        return _canonical_cli_error(value)
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


# A command-line error is identified by its category and the offending token,
# not by the help/usage text a framework chooses to print around it:
# ``instruction.md`` explicitly does not freeze usage bytes, while the error
# itself (nonzero exit plus a categorised message) is part of the contract.
_CLI_ERROR_LINE = re.compile(
    r"\b(unknown flag|flag needs an argument|unknown command|unexpected argument"
    r"|invalid argument|required flag[s]?)\b[:\s]*[\"']?(-{1,2}[\w.-]+|[^\s\"',;]+)?"
)


def _canonical_cli_error(value: Any) -> Any:
    """Reduce an error dump to its category and offending token.

    A framework may print a usage/help block before the error itself; the block
    is boilerplate that ``instruction.md`` does not freeze, so a multi-line dump
    is reduced to its last non-empty line (the error) first.  After that, only
    strings that carry exactly one recognisable error line are rewritten, so a
    message that merely mentions the words (or reports several errors) is
    compared verbatim and no real difference is masked.
    """
    if not isinstance(value, str):
        return value
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) > 1:
        value = lines[-1]
    matches = list(_CLI_ERROR_LINE.finditer(value))
    if len(matches) != 1:
        return value
    label = matches[0].group(1)
    token = matches[0].group(2) or ""
    return f"{label} {token}".strip()


# Frameworks render the same *parse* failure differently: Cobra wraps Go's error
# ("invalid argument \"nope\" for \"-p, --parallel\" flag: strconv.ParseInt: ..."),
# Kong surfaces it directly ("--parallel: strconv.ParseInt: ...").  The category
# is the behaviour; the wording is not, so both reduce to ``parse_error`` (the
# token the source side already carries).
_PARSE_ERROR_TEXT = re.compile(
    r"(?i)\b(?:strconv\.parse\w*|invalid syntax|time: invalid duration|"
    r"parse error|cannot parse|invalid (?:bool|int|integer|float|duration|number|argument|value))\b"
)


def _canonical_parse_error(value: Any) -> Any:
    if isinstance(value, str) and _PARSE_ERROR_TEXT.search(value):
        return "parse_error"
    if isinstance(value, list):
        return [_canonical_parse_error(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_parse_error(item) for key, item in value.items()}
    return value


def _canonical_tested_packages(value: Any) -> Any:
    """Drop packages that have no test files from a ``go test`` package map.

    ``go test ./cmd/...`` reports ``?`` (``[no test files]``) for packages that
    contribute nothing to the test signal.  ``instruction.md`` allows the wiring
    to live in new helper packages, so a new ``?`` entry is not a regression;
    an *existing* package that loses its tests turns into ``?`` and is removed
    from one side only, which still fails the comparison.
    """
    if not isinstance(value, dict):
        return value
    packages = value.get("packages")
    if not isinstance(packages, dict):
        return value
    pruned = {key: item for key, item in packages.items() if item != "?"}
    return {**value, "packages": pruned}


# A flag's *type* is a behaviour ("can it be given more than once / does it take
# a value"), but the help text renders it in framework vocabulary: Cobra prints
# ``stringArray``/``strings``/``stringSlice``, Kong prints its own metavar
# (``BUNDLE``).  Only the known vocabulary tokens carry a decision; anything else
# is opaque rendering, and a type must then be compared as undecidable rather
# than assumed to be ``string``.
_KNOWN_FLAG_KINDS = frozenset({"bool", "int", "string", "list"})
_UNDECIDABLE_FLAG_KINDS = frozenset({"opaque"})


def _opaque_flag_names(value: Any) -> set[str]:
    names: set[str] = set()
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            flags = node.get("flags")
            if isinstance(flags, dict):
                for name, entry in flags.items():
                    kind = str(entry.get("kind", "")) if isinstance(entry, Mapping) else ""
                    if kind in _UNDECIDABLE_FLAG_KINDS or kind not in _KNOWN_FLAG_KINDS:
                        names.add(str(name))
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(value)
    return names


def _drop_flag_details(value: Any, names: set[str]) -> Any:
    """Remove the rendering-dependent part of the named flags, on both sides."""
    if isinstance(value, dict):
        flags = value.get("flags")
        if isinstance(flags, dict):
            value = {
                **value,
                "flags": {
                    name: (
                        {k: v for k, v in entry.items() if k not in {"kind", "default"}}
                        if isinstance(entry, Mapping) and str(name) in names
                        else entry
                    )
                    for name, entry in flags.items()
                },
            }
        return {key: _drop_flag_details(item, names) for key, item in value.items()}
    if isinstance(value, list):
        return [_drop_flag_details(item, names) for item in value]
    return value


def _canonical_completion_markers(value: Any) -> Any:
    """Drop generator banner markers from a completion observation.

    ``# bash completion for arkade`` / ``#compdef arkade`` are the script
    generator's own header, and ``instruction.md`` states that generated
    completion script *contents* are not required to match; the required surface
    is the command/alias/flag names, which stay in the observation.
    """
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "markers" and isinstance(item, Mapping):
                result[key] = {
                    name: flag for name, flag in item.items() if not str(name).startswith("#")
                }
                continue
            result[key] = _canonical_completion_markers(item)
        return result
    if isinstance(value, list):
        return [_canonical_completion_markers(item) for item in value]
    return value


def compare(source: Any, target: Any, policy: Mapping[str, Any]) -> dict[str, Any]:
    left, right = normalize(source, policy), normalize(target, policy)
    left, right = _canonical_tally(left), _canonical_tally(right)
    left, right = _canonical_status(left), _canonical_status(right)
    left, right = _canonical_tested_packages(left), _canonical_tested_packages(right)
    # New helper packages may add passing tests. Keep all baseline tested
    # packages, missing packages, failures and the aggregate go_test_exit;
    # successful additional packages are not a behavioral regression.
    if isinstance(left, dict) and isinstance(right, dict):
        baseline_packages, candidate_packages = left.get("packages"), right.get("packages")
        if isinstance(baseline_packages, dict) and isinstance(candidate_packages, dict):
            right = {**right, "packages": {
                name: status for name, status in candidate_packages.items()
                if name in baseline_packages or status not in {"ok", "?"}
            }}
    left, right = _canonical_completion_markers(left), _canonical_completion_markers(right)
    left, right = _canonical_parse_error(left), _canonical_parse_error(right)
    # A flag rendered in an unknown vocabulary on either side is undecidable, so
    # it is dropped from both sides (see _KNOWN_FLAG_KINDS).
    opaque = _opaque_flag_names(left) | _opaque_flag_names(right)
    if opaque:
        left, right = _drop_flag_details(left, opaque), _drop_flag_details(right, opaque)
    return {"status": "PASS" if left == right else "FAIL", "source": left, "target": right}
