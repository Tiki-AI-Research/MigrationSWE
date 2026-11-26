from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from harbor_migration_toolkit import ChangedFile, check_protected_paths

_FORBIDDEN_SCRIPT_NAMES = (
    "ctrf",
    "report",
    "fake-report",
    "grader",
    "hardgate",
    "verifier",
)


def evaluate_environment_integrity(
    repo: Path,
    changed_files: Iterable[ChangedFile],
    *,
    protected_files: Iterable[str],
    protected_directories: Iterable[str],
) -> dict[str, Any]:
    """Verify evaluation environment integrity and anti-cheating rules.

    Rules from INTERFACES.md:
    - protected_paths_unchanged via check_protected_paths over reconstructed changed_files
      using the frozen policy (.base_commit, .source_base_commit, .harbor_eval).
    - anti_cheating_pass:
      1. reject patches that create files under .harbor_eval/
      2. reject patches that write under /logs or logs/
      3. reject patches that add a package.json script named to fake reports (e.g. ctrf, report, grader)
    - Violation => policy_violation.
    """
    repo = Path(repo).resolve()
    changed_list = list(changed_files)
    policy_reasons: list[str] = []
    checks: list[dict[str, Any]] = []

    # 1. Protected paths check via toolkit
    prot_result = check_protected_paths(
        changed_list,
        protected_files=protected_files,
        protected_directories=protected_directories,
    )

    if prot_result.status == "configuration_error":
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "evaluation_environment_integrity",
            "applicable": True,
            "success": None,
            "status": "configuration_error",
            "summary": prot_result.reason or "integrity policy configuration error",
            "checks": [],
        }
    if prot_result.status == "infrastructure_error":
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "evaluation_environment_integrity",
            "applicable": True,
            "success": None,
            "status": "infrastructure_error",
            "summary": prot_result.reason or "changed_files data error",
            "checks": [],
        }

    if not prot_result.passed:
        policy_reasons.append("protected_paths_touched")
        checks.append({
            "id": "protected_paths_unchanged",
            "status": "policy_violation",
            "reason": prot_result.reason,
            "violations": [
                {
                    "change": item.change,
                    "changed_path": item.changed_path,
                    "path_role": item.path_role,
                    "protected_path": item.protected_path,
                    "protected_type": item.protected_type,
                }
                for item in prot_result.violations
            ],
        })
    else:
        checks.append({
            "id": "protected_paths_unchanged",
            "status": "passed",
        })

    # 2. Anti-cheating pass
    # A. Check paths for .harbor_eval/**, logs/**, /logs/**
    forbidden_path_hits: list[str] = []
    for item in changed_list:
        for p in (item.path, item.old_path):
            if not p:
                continue
            norm_p = p.replace("\\", "/").strip("/")
            if (
                norm_p == ".harbor_eval"
                or norm_p.startswith(".harbor_eval/")
                or norm_p == "logs"
                or norm_p.startswith("logs/")
            ):
                forbidden_path_hits.append(norm_p)

    if forbidden_path_hits:
        policy_reasons.append("forbidden_directory_write")
        checks.append({
            "id": "anti_cheating_path_pass",
            "status": "policy_violation",
            "hits": forbidden_path_hits,
            "reason": "candidate patch writes to reserved evaluator paths or logs directory",
        })
    else:
        checks.append({
            "id": "anti_cheating_path_pass",
            "status": "passed",
        })

    # B. Check package.json scripts for faking reports
    pkg_path = repo / "package.json"
    fake_scripts: list[str] = []
    if pkg_path.is_file():
        try:
            pkg_data = json.loads(pkg_path.read_text(encoding="utf-8"))
            scripts = pkg_data.get("scripts", {})
            if isinstance(scripts, dict):
                for script_name in scripts.keys():
                    name_lower = script_name.lower()
                    for forbidden in _FORBIDDEN_SCRIPT_NAMES:
                        if forbidden in name_lower:
                            fake_scripts.append(script_name)
                            break
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    if fake_scripts:
        policy_reasons.append("fake_report_script_detected")
        checks.append({
            "id": "anti_cheating_scripts_pass",
            "status": "policy_violation",
            "scripts": fake_scripts,
            "reason": f"candidate package.json defines suspicious report scripts: {fake_scripts}",
        })
    else:
        checks.append({
            "id": "anti_cheating_scripts_pass",
            "status": "passed",
        })

    is_success = len(policy_reasons) == 0
    return {
        "schema_version": "harbor-migration-check/v1",
        "id": "evaluation_environment_integrity",
        "applicable": True,
        "success": is_success,
        "status": "passed" if is_success else "policy_violation",
        "summary": (
            "evaluation environment integrity verified"
            if is_success
            else f"evaluation environment integrity violation: {', '.join(policy_reasons)}"
        ),
        "policy_reasons": policy_reasons,
        "checks": checks,
    }
