"""Small self-contained integrity gate shipped with the public verifier."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Mapping


_FORBIDDEN_GRADING_BASENAMES = frozenset({
    "objective-baseline.json", "rubric-asset.json", "judge-prompt.md",
    "judge-instruction.md", "judge-gateway.json", "objective.json", "rubric.json",
    "verifier-component.json", "component_result.py",
})


def _forbidden_grading_path(path: str) -> bool:
    """Reject objective/rubric grading locations, not arbitrary asset substrings.

    An authored fixture such as ``assert-components.ts`` is a legitimate
    candidate asset even though it contains ``component``.  These names can only
    belong to a second (objective/rubric) grading stage, which this verifier no
    longer has, so they must never ship in the bundle.
    """

    parts = str(path).casefold().split("/")
    if len(parts) >= 2 and parts[0] == "verifier" and parts[1] == "graded":
        return True
    return parts[-1] in _FORBIDDEN_GRADING_BASENAMES


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate_public_integrity(root: Path) -> None:
    """Fail closed when a public asset is missing, replaced, or symlinked."""

    manifest_path = root / "assets" / "assets-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("public integrity manifest is missing")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("public integrity manifest is invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != "migration-public-assets:v1":
        raise ValueError("public integrity manifest schema is invalid")
    rows = payload.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("public integrity manifest files are missing")
    declared: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("public integrity manifest entry is invalid")
        relative = row.get("path")
        if not isinstance(relative, str):
            raise ValueError("public integrity manifest path is invalid")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            raise ValueError("public integrity manifest path escapes the bundle")
        if relative in declared or relative == "assets/assets-manifest.json":
            raise ValueError("public integrity manifest contains a duplicate or manifest entry")
        declared.add(relative)
        target = root / Path(*path.parts)
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"public integrity asset is missing: {relative}")
        if row.get("sha256") != _digest(target) or row.get("size") != target.stat().st_size:
            raise ValueError(f"public integrity asset changed: {relative}")
    actual: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative == "adversarial-patches" or relative.startswith("adversarial-patches/"):
            continue
        if path.is_symlink():
            raise ValueError(f"public integrity asset is symlinked: {relative}")
        if (
            path.is_file()
            and "__pycache__" not in path.relative_to(root).parts
            and relative != "assets/assets-manifest.json"
        ):
            actual.add(relative)
    if actual != declared:
        raise ValueError("public integrity asset inventory changed")


def validate_public_projection_contract(root: Path, contract: Mapping[str, object]) -> None:
    """Validate the explicit projection inventory shipped in ``config.json``."""

    allowed = contract.get("allowed_files")
    required = contract.get("required_files")
    roots = contract.get("allowed_asset_roots")
    if not isinstance(allowed, list) or any(not isinstance(item, str) or not item for item in allowed):
        raise ValueError("public projection allowed_files is invalid")
    if allowed != sorted(set(allowed)):
        raise ValueError("public projection allowed_files are not canonical")
    if any(_forbidden_grading_path(path) for path in allowed):
        raise ValueError("public projection contains forbidden grading files")
    if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
        raise ValueError("public projection required_files is invalid")
    if not isinstance(roots, list) or any(not isinstance(item, str) or not item for item in roots):
        raise ValueError("public projection allowed_asset_roots is invalid")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and "__pycache__" not in path.relative_to(root).parts
        and path.relative_to(root).parts[0] != "adversarial-patches"
    }
    if actual != set(allowed):
        raise ValueError("public projection file inventory changed")
    if not set(required).issubset(actual):
        raise ValueError("public projection required files are missing")
    if contract.get("validated") is not True or contract.get("usable_for_scoring") is not True:
        return
    if roots != ["fixtures", "shared", "source", "target"]:
        raise ValueError("usable public projection asset roots are invalid")


__all__ = ["validate_public_integrity", "validate_public_projection_contract"]
