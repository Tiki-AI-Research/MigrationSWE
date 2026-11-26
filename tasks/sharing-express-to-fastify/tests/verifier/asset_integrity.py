from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REQUIRED_ASSETS = {
    "evaluation_integrity_policy": "evaluation-integrity-policy.json",
    "migration_contract": "migration-contract.json",
    "base_repo_archive": "base-repo.tar.gz",
    "harbor_migration_toolkit_wheel": "wheels/harbor_migration_toolkit-0.1.0-py3-none-any.whl",
    "baseline_node_results": "baseline-node-results.json",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_asset_manifest(assets_dir: Path) -> dict[str, Any]:
    assets_dir = Path(assets_dir).resolve()
    manifest_path = assets_dir / "assets-manifest.json"
    checks: list[dict[str, Any]] = []
    missing_assets: list[str] = []
    mismatched_assets: list[str] = []
    invalid_entries: list[str] = []
    undeclared_required_assets: list[str] = []

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "asset_integrity",
            "success": False,
            "status": "configuration_error",
            "summary": f"unable to load assets-manifest.json: {exc}",
            "checks": checks,
            "missing_assets": missing_assets,
            "mismatched_assets": mismatched_assets,
            "invalid_entries": ["assets-manifest.json"],
            "undeclared_required_assets": undeclared_required_assets,
        }

    declared = manifest.get("assets") if isinstance(manifest, dict) else None
    if manifest.get("schema_version") != "1.0" or not isinstance(declared, dict):
        invalid_entries.append("assets-manifest.json")
        declared = declared if isinstance(declared, dict) else {}

    for asset_id, required_path in REQUIRED_ASSETS.items():
        entry = declared.get(asset_id)
        if not isinstance(entry, dict) or entry.get("path") != required_path:
            undeclared_required_assets.append(asset_id)

    for asset_id, entry in sorted(declared.items()):
        if not isinstance(asset_id, str) or not isinstance(entry, dict):
            invalid_entries.append(str(asset_id))
            continue
        relative = entry.get("path")
        expected = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or not isinstance(expected, str)
            or len(expected) != 64
            or any(ch not in "0123456789abcdef" for ch in expected)
        ):
            invalid_entries.append(asset_id)
            continue
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or "\\" in relative:
            invalid_entries.append(asset_id)
            continue
        path = assets_dir / relative_path
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(assets_dir)
        except (OSError, ValueError):
            missing_assets.append(asset_id)
            continue
        if not resolved.is_file():
            missing_assets.append(asset_id)
            continue
        actual = _sha256(resolved)
        if actual != expected:
            mismatched_assets.append(asset_id)
            checks.append({
                "id": asset_id,
                "status": "configuration_error",
                "path": relative,
                "expected_sha256": expected,
                "actual_sha256": actual,
            })
        else:
            checks.append({
                "id": asset_id,
                "status": "passed",
                "path": relative,
                "sha256": actual,
            })

    success = not any(
        (missing_assets, mismatched_assets, invalid_entries, undeclared_required_assets)
    )
    return {
        "schema_version": "harbor-migration-check/v1",
        "id": "asset_integrity",
        "success": success,
        "status": "passed" if success else "configuration_error",
        "summary": "evaluator asset manifest verified" if success else "evaluator asset manifest validation failed",
        "checks": checks,
        "missing_assets": missing_assets,
        "mismatched_assets": mismatched_assets,
        "invalid_entries": invalid_entries,
        "undeclared_required_assets": undeclared_required_assets,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify frozen evaluator asset hashes")
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = verify_asset_manifest(args.assets)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
