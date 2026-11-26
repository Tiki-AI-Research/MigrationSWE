from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from harbor_migration_toolkit import (
    ChangedFile,
    check_patch_applicable,
    get_changed_files,
    patch_is_empty,
    read_patch,
)

_TRAVERSAL_RE = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MARKER_PATTERNS = (
    ".base_commit",
    ".base_tree_hash",
    ".source_base_commit",
    ".harbor_eval",
)
# The evaluator test suite creates its scratch directory inside test/ when the
# candidate runs `npm test`; its residue is not a candidate change.
_GENERATED_TEST_RESIDUE_RE = re.compile(r"^test/\.tmp[^/]*(?:/|$)")

def _normalize_changed_path(raw_path: str) -> str:
    normalized = str(raw_path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _matches_allowed_pattern(path: str, pattern: str) -> bool:
    """Match one allowlist entry, treating a trailing '/**' as a whole subtree.

    PurePosixPath.match() treats '**' as a single path segment, so 'bin/**'
    would not cover nested files such as 'bin/lib/helper.js'.
    """
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    if not any(char in pattern for char in "*?[]"):
        return path == pattern
    return PurePosixPath(path).match(pattern)


def _check_raw_patch_safety(patch_text: str) -> tuple[bool, str, str | None]:
    """Inspect raw patch header lines for path traversal, absolute paths, or evaluator markers.

    Returns: (is_safe, status, reason)
    - Marker file/directory in patch headers => policy_violation
    - Path traversal or absolute path => policy_violation
    """
    for line in patch_text.splitlines():
        # Check diff header lines: "diff --git a/... b/...", "--- a/...", "+++ b/...", "rename to ...", "copy to ..."
        path_candidates: list[str] = []
        if line.startswith("diff --git "):
            parts = line[len("diff --git ") :].split()
            if len(parts) >= 2:
                path_candidates.extend([parts[0], parts[1]])
        elif line.startswith("--- ") or line.startswith("+++ "):
            raw = line[4:].strip()
            if raw and raw != "/dev/null":
                path_candidates.append(raw.split("\t")[0])
        elif line.startswith("rename from ") or line.startswith("rename to "):
            path_candidates.append(line.split(maxsplit=2)[-1].strip())
        elif line.startswith("copy from ") or line.startswith("copy to "):
            path_candidates.append(line.split(maxsplit=2)[-1].strip())

        for raw_path in path_candidates:
            # Strip standard git prefixes a/ or b/
            norm = raw_path
            if norm.startswith("a/") or norm.startswith("b/"):
                norm = norm[2:]
            norm = norm.strip("\"'")

            # Check markers
            for marker in _MARKER_PATTERNS:
                if norm == marker or norm.startswith(f"{marker}/") or f"/{marker}/" in norm or norm.endswith(f"/{marker}"):
                    return False, "policy_violation", f"patch contains forbidden evaluator marker: {marker}"

            # Check path traversal
            if _TRAVERSAL_RE.search(norm):
                return False, "policy_violation", f"patch contains path traversal: {norm}"

            # Check absolute path
            if norm.startswith("/") or norm.startswith("\\") or _WINDOWS_DRIVE_RE.match(norm):
                return False, "policy_violation", f"patch contains absolute path: {norm}"

    return True, "passed", None


def evaluate_candidate(
    repo: Path,
    patch: Path,
    expected_base: str,
    *,
    allow_empty: bool = False,
    allowed_paths: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], tuple[ChangedFile, ...]]:
    """Validate candidate patch readability, non-emptiness, safety markers, and applicability to base."""
    repo = Path(repo).resolve()
    patch = Path(patch).resolve()

    try:
        patch_bytes = read_patch(patch)
    except (OSError, TypeError, UnicodeError) as exc:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "candidate_valid",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": f"unable to read model patch: {exc}",
            "patch_applicable": False,
            "checks": [],
        }, ()

    patch_sha = hashlib.sha256(patch_bytes).hexdigest()

    # Try decode for raw safety inspection
    try:
        patch_text = patch_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            patch_text = patch_bytes.decode("latin-1")
        except Exception as exc:
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "candidate_valid",
                "applicable": True,
                "success": False,
                "status": "candidate_failure",
                "summary": f"patch is not decode-safe: {exc}",
                "patch_applicable": False,
                "patch_sha256": patch_sha,
                "checks": [],
            }, ()

    # Raw patch marker / traversal / absolute path check
    is_safe, safety_status, safety_reason = _check_raw_patch_safety(patch_text)
    if not is_safe:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "candidate_valid",
            "applicable": True,
            "success": False,
            "status": safety_status,
            "summary": safety_reason,
            "policy_reasons": [safety_reason],
            "patch_applicable": False,
            "patch_sha256": patch_sha,
            "checks": [
                {
                    "id": "raw_patch_safety",
                    "status": safety_status,
                    "reason": safety_reason,
                }
            ],
        }, ()

    if patch_is_empty(patch_bytes) and not allow_empty:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "candidate_valid",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": "empty candidate patch is not allowed",
            "patch_applicable": False,
            "patch_sha256": patch_sha,
            "checks": [
                {
                    "id": "patch_non_empty",
                    "status": "candidate_failure",
                    "reason": "empty candidate patch is not allowed",
                }
            ],
        }, ()

    check = check_patch_applicable(repo, expected_base, patch, allow_empty=allow_empty)
    if not check.passed:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "candidate_valid",
            "applicable": True,
            "success": False,
            "status": check.status,
            "summary": check.reason or "patch does not apply cleanly to base commit",
            "patch_applicable": False,
            "patch_sha256": patch_sha,
            "checks": [
                {
                    "id": "patch_applies_cleanly",
                    "status": check.status,
                    "command": list(check.command),
                    "exit_code": check.exit_code,
                    "reason": check.reason,
                    "stdout": check.stdout[-1000:] if check.stdout else "",
                    "stderr": check.stderr[-1000:] if check.stderr else "",
                }
            ],
        }, ()

    try:
        changed = get_changed_files(
            repo,
            expected_base,
            exclude_paths=(".base_commit", ".base_tree_hash", ".source_base_commit"),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "candidate_valid",
            "applicable": True,
            "success": None,
            "status": "infrastructure_error",
            "summary": f"unable to enumerate changed files: {exc}",
            "patch_applicable": True,
            "patch_sha256": patch_sha,
            "checks": [],
        }, ()

    if allowed_paths is not None:
        if not allowed_paths:
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "candidate_valid",
                "applicable": True,
                "success": None,
                "status": "configuration_error",
                "summary": "candidate allowlist is empty",
                "patch_applicable": True,
                "patch_sha256": patch_sha,
                "checks": [],
            }, changed

        disallowed: list[str] = []
        for item in changed:
            paths = [item.path]
            if item.old_path:
                paths.append(item.old_path)
            for raw_path in paths:
                normalized = _normalize_changed_path(raw_path)
                if _GENERATED_TEST_RESIDUE_RE.match(normalized):
                    continue
                if not any(_matches_allowed_pattern(normalized, pattern) for pattern in allowed_paths):
                    disallowed.append(normalized)
        if disallowed:
            unique = sorted(set(disallowed))
            reason = f"candidate changes outside configured allowlist: {unique}"
            checks: list[dict[str, Any]] = [
                {
                    "id": "changed_path_allowlist",
                    "status": "policy_violation",
                    "allowed_paths": list(allowed_paths),
                    "disallowed_paths": unique,
                }
            ]
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "candidate_valid",
                "applicable": True,
                "success": False,
                "status": "policy_violation",
                "summary": reason,
                "policy_reasons": ["changed_path_outside_allowlist"],
                "patch_applicable": True,
                "patch_sha256": patch_sha,
                "changed_files": [
                    {"status": item.status, "path": item.path, "old_path": item.old_path}
                    for item in changed
                ],
                "checks": checks,
            }, changed

    pass_checks: list[dict[str, Any]] = [
        {
            "id": "raw_patch_safety",
            "status": "passed",
        },
        {
            "id": "patch_non_empty",
            "status": "passed",
        },
        {
            "id": "patch_applies_cleanly",
            "status": "passed",
            "command": list(check.command),
            "exit_code": check.exit_code,
        },
        {
            "id": "enumerate_changed_files",
            "status": "passed",
            "count": len(changed),
        },
    ]
    # A cleanly applied patch is still invalid when a changed configuration
    # file cannot be parsed. Check the applied candidate, not the diff text.
    for item in changed:
        if item.status == "D" or not item.path.lower().endswith(".json"):
            continue
        json_path = repo / item.path
        try:
            if json_path.is_symlink():
                raise ValueError("JSON path is a symbolic link")
            json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            reason = f"changed JSON file cannot be parsed: {item.path}: {exc}"
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "candidate_valid",
                "applicable": True,
                "success": False,
                "status": "candidate_failure",
                "summary": reason,
                "patch_applicable": True,
                "patch_sha256": patch_sha,
                "changed_files": [
                    {"status": entry.status, "path": entry.path, "old_path": entry.old_path}
                    for entry in changed
                ],
                "checks": [
                    *pass_checks,
                    {"id": "changed_json_parse", "status": "candidate_failure", "path": item.path, "reason": str(exc)},
                ],
            }, changed
        pass_checks.append({"id": "changed_json_parse", "status": "passed", "path": item.path})

    return {
        "schema_version": "harbor-migration-check/v1",
        "id": "candidate_valid",
        "applicable": True,
        "success": True,
        "status": "passed",
        "summary": f"candidate patch is valid and applies cleanly ({len(changed)} changed files)",
        "patch_applicable": True,
        "patch_sha256": patch_sha,
        "changed_files": [
            {"status": item.status, "path": item.path, "old_path": item.old_path}
            for item in changed
        ],
        "checks": pass_checks,
    }, changed
