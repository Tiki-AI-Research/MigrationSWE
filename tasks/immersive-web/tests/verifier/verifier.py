#!/usr/bin/env python3
"""Execute verified gates, clean builds, and evaluator-owned tests with typed evidence."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


CANDIDATE_USER = "candidate"
EVALUATOR_USER = "evaluator"


class EvaluationInfrastructureError(RuntimeError):
    pass


class EvaluationTaskError(EvaluationInfrastructureError):
    def __init__(
        self,
        message: str,
        *,
        error_class: str = "evaluator_contract_error",
    ):
        super().__init__(message)
        self.error_class = error_class


class CandidateExecutionError(RuntimeError):
    def __init__(self, message: str, *, error_type: str = "candidate_failure", evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.evidence = evidence or {}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as error:
        raise EvaluationInfrastructureError(f"cannot write report {path}: {error}") from error


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise EvaluationInfrastructureError(f"invalid {label} at {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvaluationInfrastructureError(f"{label} must be a JSON object")
    return value


def load_hardgate_module() -> Any:
    path = Path(__file__).with_name("hardgate.py")
    spec = importlib.util.spec_from_file_location("webvr_hardgate_runtime", path)
    if spec is None or spec.loader is None:
        raise EvaluationInfrastructureError("cannot load hard gate module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def restricted_env(repo: Path, user: str = CANDIDATE_USER) -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "NPM_CONFIG_CACHE",
        "NPM_CONFIG_PREFER_OFFLINE",
        "NPM_CONFIG_AUDIT",
        "NPM_CONFIG_FUND",
        "TMPDIR",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(
        {
            "HOME": f"/home/{user}",
            "CI": "1",
            "NO_COLOR": "1",
            "NPM_CONFIG_OFFLINE": "true",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "npm_config_offline": "true",
            "npm_config_ignore_scripts": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "PWD": str(repo),
        }
    )
    if user == EVALUATOR_USER:
        # The evaluator-owned suite must never rely on the candidate npm cache and
        # needs a private writable temp/cache location it owns.
        env["TMPDIR"] = f"/home/{user}/tmp"
        env["VITEST_TMP_DIR"] = f"/home/{user}/tmp"
        return env
    if os.geteuid() == 0:
        env["NPM_CONFIG_CACHE"] = "/opt/npm-cache-candidate"
        env["npm_config_cache"] = "/opt/npm-cache-candidate"
    return env


def user_prefix(user: str) -> list[str]:
    if os.geteuid() != 0:
        return []
    if not shutil.which("runuser") or subprocess.run(
        ["id", user], capture_output=True, check=False
    ).returncode != 0:
        raise EvaluationInfrastructureError(f"{user} execution user is unavailable")
    return ["runuser", "--user", user, "--"]


def run_command(
    label: str,
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    user: str | None = CANDIDATE_USER,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    full_argv = [*user_prefix(user), *argv] if user else argv
    started = time.time_ns()
    run_env = restricted_env(cwd, user) if user else None
    if extra_env:
        run_env = {**(run_env if run_env is not None else os.environ), **extra_env}
    try:
        result = subprocess.run(
            full_argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env=run_env,
        )
        timed_out = False
        launch_error = ""
    except subprocess.TimeoutExpired as error:
        result = None
        timed_out = True
        launch_error = str(error)
    except OSError as error:
        raise EvaluationInfrastructureError(f"cannot launch {label}: {error}") from error
    stopped = time.time_ns()
    stdout = result.stdout if result else ""
    stderr = result.stderr if result else launch_error
    return {
        "label": label,
        "argv": argv,
        "effective_argv": full_argv,
        "cwd": str(cwd),
        "started_ns": started,
        "stopped_ns": stopped,
        "duration_ms": (stopped - started) // 1_000_000,
        "exit_code": result.returncode if result else None,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
    }


def summarize_command(evidence: dict[str, Any]) -> dict[str, Any]:
    return dict(evidence)


def run_hardgate(config: Path, repo: Path, output: Path, apply_failed: int) -> dict[str, Any]:
    evidence = run_command(
        "hard_gate",
        [
            "python3",
            str(Path(__file__).parent / "hardgate.py"),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--output",
            str(output),
            "--apply-failed",
            str(apply_failed),
        ],
        cwd=repo,
        timeout=1800,
        user=None,
    )
    if not output.is_file():
        raise EvaluationInfrastructureError(
            f"hard gate did not produce a report (exit {evidence['exit_code']}): {evidence['stderr'][-2000:]}"
        )
    result = load_object(output, "hard-gate report")
    if evidence["exit_code"] == 2 or result.get("infrastructure_error") is True:
        raise EvaluationInfrastructureError(result.get("message", "hard gate infrastructure failure"))
    if evidence["exit_code"] != 0:
        raise EvaluationInfrastructureError(
            f"hard gate crashed with exit {evidence['exit_code']}: {evidence['stderr'][-2000:]}"
        )
    return result


def _legacy_source_residue(repo: Path, paths: list[str]) -> dict[str, list[str]]:
    """Find ordinary legacy source-framework calls / deprecated constructs.

    This is a completeness signal for ``migration_scope_complete``: it detects
    the target build still importing or invoking the source toolchain (Rollup /
    Babel / Mocha / equivalent) so that a "surface replacement" does not count
    as a completed migration. Obfuscated or hidden fallbacks remain the
    anti-cheating scanner's responsibility, so the two gates do not collide.
    """
    legacy_import = re.compile(
        r"(?:from\s*|\brequire\s*\(\s*|\bimport\s*\(\s*|\bimport\s+)['\"]"
        r"(?:rollup(?:[-/][^'\"]*)?|babel|@babel/[^'\"]+|babel-core|babel-register|"
        r"mocha|browserify|gulp|grunt|rollup-plugin[^'\"]*)['\"]",
        re.IGNORECASE,
    )
    # Only a real invocation is a runtime dependency. A bare ``mocha`` in a log
    # line, a changelog note, or a test title, and a text mention of
    # ``rollup.config``/``.babelrc``, are not: those are handled as ordinary
    # files by the ``removed_paths`` evidence instead.
    legacy_usage = re.compile(
        r"(?:\brollup\s*\(|\bbabel\s*\(|\bbabel-node\b|"
        r"\b(?:spawn|exec|execFile|execFileSync|execSync|spawnSync)\s*\(\s*['\"]"
        r"(?:npx\s+)?(?:mocha|babel-node|rollup)\b)",
        re.IGNORECASE,
    )
    deprecated = re.compile(r"(?:new\s+Buffer\s*\(|process\.binding\s*\()")
    residue: dict[str, list[str]] = {}
    for relative in paths:
        path = repo / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        findings = sorted(
            {
                *[match.group(0) for match in legacy_import.finditer(text)],
                *[match.group(0) for match in legacy_usage.finditer(text)],
                *[match.group(0) for match in deprecated.finditer(text)],
            }
        )
        if findings:
            residue[relative] = findings
    return residue


def migration_scope_complete(repo: Path, contract: dict[str, Any]) -> dict[str, Any]:
    package = load_object(repo / "package.json", "candidate package")
    lock = load_object(repo / "package-lock.json", "candidate lockfile")
    units: list[dict[str, Any]] = []
    for unit in contract["migration_units"]:
        kind = unit["kind"]
        evidence: Any = None
        passed = False
        if kind == "package_scripts":
            scripts = package.get("scripts", {})
            evidence = {name: scripts.get(name) for name in unit["required"]}
            # Compare the exact script bodies declared in the contract, not just
            # the presence of the names: the hard gate applies the same values,
            # so a wrong command must not pass here while failing preflight.
            passed = all(name in scripts for name in unit["required"])
            for name, value in unit.get("expected_scripts", {}).items():
                if scripts.get(name) != value:
                    passed = False
            template = unit.get("test_script_template")
            if template and "test" in unit["required"]:
                config_name = next(
                    (
                        name
                        for name in unit.get("test_configs", [])
                        if (repo / name).is_file()
                    ),
                    None,
                )
                expected_test = template.format(config=config_name) if config_name else None
                if scripts.get("test") != expected_test:
                    passed = False
        elif kind == "direct_dependencies":
            evidence = sorted(package.get("devDependencies", {}))
            passed = set(evidence) == set(unit["required"])
        elif kind == "lockfile_v3":
            evidence = lock.get("lockfileVersion")
            passed = evidence == 3 and bool(lock.get("packages"))
        elif kind == "target_config":
            evidence = {path: (repo / path).is_file() for path in unit["required"]}
            alternatives = unit.get("alternatives", [])
            if alternatives:
                selected = next((path for path in alternatives if (repo / path).is_file()), None)
                evidence["selected"] = selected
                passed = selected is not None and all(evidence.values())
            else:
                passed = all(evidence.values())
        elif kind == "removed_paths":
            evidence = {path: not (repo / path).exists() for path in unit["required"]}
            # Legacy tooling must also be gone from the *migrated execution
            # path*: an ordinary `import { rollup } from 'rollup'` in the target
            # build driver is an incomplete migration even though the legacy
            # config files were deleted. Hidden/obfuscated fallbacks remain a
            # `no_deceptive_source_fallback` concern.
            residue = _legacy_source_residue(repo, unit.get("source_scan", []))
            if residue:
                evidence["legacy_source_residue"] = residue
            passed = all(evidence.values()) and not residue
        elif kind == "node_runtime":
            candidates = [repo / ".travis.yml", repo / ".github/workflows/ci.yml"]
            existing = [path for path in candidates if path.is_file()]
            evidence = {str(path.relative_to(repo)): path.read_text(encoding="utf-8") for path in existing}
            passed = any(re.search(r"node_js:\s*\n\s*-\s*['\"]?20['\"]?", text) for text in evidence.values()) or any("node-version: '20'" in text or 'node-version: "20"' in text for text in evidence.values())
        elif kind == "artifacts":
            evidence = {path: (repo / path).is_file() for path in unit["required"]}
            passed = all(evidence.values())
        elif kind == "package_boundary":
            npmignore = (
                (repo / ".npmignore").read_text(encoding="utf-8")
                if (repo / ".npmignore").is_file()
                else ""
            )
            # Ignore entries may be anchored to the package root (`/scripts`,
            # `./src`) or suffixed with a slash. Those are equivalent to the
            # unanchored forms for npm's purpose; normalize them before the
            # contract comparison so a correct anchored boundary is not
            # misreported as incomplete.
            def _ignore_entry(line: str) -> str:
                value = line.strip()
                while value.startswith("./") or value.startswith("/"):
                    value = value[2:] if value.startswith("./") else value[1:]
                return value.rstrip("/")

            ignore_entries = {
                _ignore_entry(line)
                for line in npmignore.splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
            config_files = [
                name
                for name in (
                    "vite.config.mjs",
                    "vite.config.js",
                    "vitest.config.mjs",
                    "vitest.config.js",
                )
                if (repo / name).is_file()
            ]

            def ignored(name: str) -> bool:
                return any(fnmatch.fnmatch(name, entry) for entry in ignore_entries)

            ignore_ok = all(
                ignored(name) for name in ("scripts", "src", "test", *config_files)
            )
            # `.npmignore` may also use the "ignore everything, then re-include"
            # allowlist style (`*` followed by `!` negations). That is an equally
            # valid publication boundary.
            lines = [
                line.strip()
                for line in npmignore.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            allowlist_ok = "*" in lines and any(line.startswith("!") for line in lines)
            # A package.json `files` allowlist is an equally valid publication
            # boundary. Accept it and let the `npm pack` contract test validate
            # the resulting file set.
            files_field = package.get("files")
            files_ok = isinstance(files_field, list) and any(
                isinstance(entry, str) and entry.strip().lstrip("./").startswith("build")
                for entry in files_field
            )
            evidence = {"npmignore": npmignore, "files": files_field}
            passed = ignore_ok or allowlist_ok or files_ok
        units.append({
            "id": unit["id"],
            "kind": kind,
            "outcome": "passed" if passed else "failed",
            "evidence": evidence,
        })
    passed_units = sum(unit["outcome"] == "passed" for unit in units)
    complete = passed_units == len(units) and len(units) > 0
    score = (passed_units / len(units)) if units else 0.0
    return {
        "outcome": "passed" if complete else "failed",
        "applicable": True,
        "complete": complete,
        "passed_units": passed_units,
        "total_units": len(units),
        "score": score,
        "units": units,
    }


def unmeasured_scope(contract: dict[str, Any], reason: str) -> dict[str, Any]:
    """A migration-scope result for a candidate whose package/lock is invalid."""
    return {
        "outcome": "not_run",
        "applicable": True,
        "complete": False,
        "passed_units": 0,
        "total_units": len(contract["migration_units"]),
        "score": 0.0,
        "units": [],
        "reason": reason,
    }


def measure_scope(repo: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """Measure the declared migration scope, tolerating an unreadable candidate.

    The measurement is independent of the evaluator and of the hard-gate
    verdict, so it is taken on every outcome -- including a rejected candidate
    -- to report which migration units actually completed instead of a synthetic
    0. An invalid candidate package/lock is reported as an unmeasured scope
    (``not_run``) rather than being promoted to a platform infrastructure error,
    because the package/lock is candidate-controlled input.
    """
    try:
        return migration_scope_complete(repo, contract)
    except EvaluationInfrastructureError as error:
        return unmeasured_scope(
            contract, f"candidate package/lock could not be read: {error}"
        )


def extract_json(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "testResults" in value:
            return value
    raise ValueError("Vitest stdout did not contain a JSON report")


def run_evaluator(
    repo: Path,
    config: dict[str, Any],
    *,
    repo_root: Path | None = None,
    user: str | None = EVALUATOR_USER,
) -> tuple[dict[str, Any], dict[str, Any]]:
    vitest = Path(config["hard_gate"]["evaluator_vitest"])
    if not vitest.is_file():
        raise EvaluationInfrastructureError("evaluator-owned Vitest executable is missing")
    source_config = Path("/tests/evaluator/vitest.config.mjs")
    effective_root = repo_root or repo
    try:
        with tempfile.TemporaryDirectory(prefix="evaluator-config-") as directory:
            runtime_config = Path(directory) / "vitest.config.mjs"
            output_file = Path(directory) / "vitest-report.json"
            shutil.copyfile(source_config, runtime_config)
            if os.geteuid() == 0 and user is not None:
                shutil.chown(directory, user=user, group=user)
                shutil.chown(runtime_config, user=user, group=user)
            elif os.geteuid() == 0:
                os.chmod(directory, 0o755)
                os.chmod(runtime_config, 0o644)
            run = run_command(
                "evaluator_owned_tests",
                [str(vitest), "run", "--config", str(runtime_config), "--reporter=json", "--outputFile", str(output_file)],
                cwd=repo,
                # The suite runs several real builds plus four live watcher
                # scenarios, so a candidate that fails several watch nodes can
                # legitimately spend many minutes before the run finishes.
                # Individual nodes carry their own shorter timeouts; this outer
                # bound only exists to catch a wedged run, and a timeout here is
                # an infrastructure error rather than a candidate result. Keep
                # enough headroom that ordinary host contention is not
                # misclassified.
                timeout=1800,
                user=user,
                extra_env={
                    "EVAL_REPO_ROOT": str(effective_root),
                    "EVALUATOR_NODE_MODULES": str(
                        config["hard_gate"]["evaluator_node_modules"]
                    ),
                },
            )
            if run["timed_out"]:
                raise EvaluationInfrastructureError("evaluator-owned suite timed out")
            if not output_file.is_file():
                if run["exit_code"] == 0:
                    raise EvaluationTaskError(
                        "Vitest did not create its evaluator-owned JSON report",
                        error_class="evaluator_report_error",
                    )
                raise EvaluationTaskError(
                    "evaluator suite failed before producing structured results: "
                    f"{(run['stderr'] or run['stdout'])[-4000:]}",
                    error_class="evaluator_startup_error",
                )
            result = load_object(output_file, "evaluator-owned Vitest report")
            if run["exit_code"] not in {0, 1}:
                raise EvaluationTaskError(
                    f"evaluator suite exited unexpectedly with {run['exit_code']}",
                    error_class="evaluator_startup_error",
                )
    except (LookupError, OSError) as error:
        raise EvaluationInfrastructureError(
            f"cannot prepare writable evaluator config copy: {error}"
        ) from error
    return result, summarize_command(run)


def extract_statuses(vitest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for suite in vitest.get("testResults", []):
        for assertion in suite.get("assertionResults", []):
            title = str(assertion.get("title", ""))
            node_id = title.split()[-1]
            if not node_id.startswith(("f2p.", "p2p.")):
                continue
            if node_id in statuses:
                raise EvaluationTaskError(f"duplicate evaluator node: {node_id}")
            status = assertion.get("status")
            statuses[node_id] = {
                "passed": status == "passed",
                "vitest_status": status,
                "duration_ms": int(assertion.get("duration") or 0),
                "message": "\n".join(assertion.get("failureMessages") or []),
                "ancestor_titles": assertion.get("ancestorTitles") or [],
            }
    return statuses


def validate_statuses(config: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> None:
    expected = {str(node) for group in ("f2p", "p2p") for node in config[group]}
    actual = set(statuses)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise EvaluationTaskError(
            "evaluator node identity mismatch: "
            f"missing={missing}, unexpected={unexpected}"
        )
    invalid = sorted(
        node_id
        for node_id, measured in statuses.items()
        if measured["vitest_status"] not in {"passed", "failed"}
    )
    if invalid:
        raise EvaluationTaskError(
            f"evaluator nodes have non-terminal status: {invalid}"
        )


def validate_configuration(config: dict[str, Any]) -> None:
    """Require every declared required node to belong to exactly one scored set.

    A node that is declared as P2P but fails the source baseline, or as F2P but
    already passes it, must not silently fall out of both sets: that would make
    a required migration target unmeasured while still allowing reward 1.
    """
    required = [str(node) for node in config.get("required_nodes", [])]
    configured = {
        str(node) for group in ("f2p", "p2p") for node in config[group]
    }
    overlap = {str(node) for node in config["f2p"]} & {
        str(node) for node in config["p2p"]
    }
    if overlap:
        raise EvaluationTaskError(
            "verifier config F2P and P2P sets overlap: " f"{sorted(overlap)}",
            error_class="evaluator_contract_error",
        )
    if not required:
        raise EvaluationTaskError(
            "verifier config must declare required_nodes",
            error_class="evaluator_contract_error",
        )
    if len(required) != len(set(required)):
        raise EvaluationTaskError(
            "verifier config required_nodes contains duplicates",
            error_class="evaluator_contract_error",
        )
    if set(required) != configured:
        raise EvaluationTaskError(
            "verifier config required_nodes must equal the F2P/P2P union: "
            f"missing_from_groups={sorted(set(required) - configured)}, "
            f"undeclared_groups={sorted(configured - set(required))}",
            error_class="evaluator_contract_error",
        )


def identity(config: dict[str, Any], hardgate: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": config["task_id"],
        "suite_id": config["suite_id"],
        "declared_upstream_commit": config["provenance"]["declared_upstream_commit"],
        "upstream_verification": config["provenance"]["upstream_verification"],
        "replay_commit": config["provenance"]["replay_commit"],
        "replay_tree": config["provenance"]["replay_tree"],
        "candidate_patch_sha256": hardgate.get("candidate_identity", {}).get("patch_sha256"),
        "evaluator_assets": hardgate.get("asset_identity", {}),
        "runtime": hardgate.get("runtime", {}),
    }


def report(
    config: dict[str, Any],
    hardgate: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
    blocker: dict[str, Any] | None,
    started_ms: int,
    stopped_ms: int,
    evaluator_command: dict[str, Any] | None,
) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    for group in ("f2p", "p2p"):
        for node_id in config[group]:
            measured = statuses.get(node_id)
            if measured is not None:
                passed = measured["passed"]
                extra = {
                    "deepswe": {
                        "group": group,
                        "execution_status": "completed",
                        "measurement_status": "measured",
                        "responsibility": "candidate" if not passed else "none",
                        "outcome": "passed" if passed else "failed",
                        "validity_status": "valid",
                        "blocked_by": [],
                    }
                }
                entry: dict[str, Any] = {
                    "name": node_id,
                    "testId": node_id,
                    "status": "passed" if passed else "failed",
                    "duration": measured["duration_ms"],
                    "extra": extra,
                }
                if measured["message"]:
                    entry["message"] = measured["message"]
            else:
                blocked_by = [blocker["id"]] if blocker else ["verifier.missing-measurement"]
                message = blocker["message"] if blocker else "expected evaluator node was not measured"
                entry = {
                    "name": node_id,
                    "testId": node_id,
                    "status": "failed",
                    "duration": 0,
                    "message": message,
                    "extra": {
                        "deepswe": {
                            "group": group,
                            "execution_status": "not_run" if blocker else "completed",
                            "measurement_status": "not_measured",
                            "responsibility": blocker.get("responsibility", "evaluator") if blocker else "evaluator",
                            "outcome": "blocked" if blocker else "failed",
                            "validity_status": (
                                "valid"
                                if blocker
                                and blocker.get("responsibility") in {"candidate", "policy"}
                                else "invalid"
                            ),
                            "blocked_by": blocked_by,
                        }
                    },
                }
            tests.append(entry)
    passed_count = sum(item["status"] == "passed" for item in tests)
    return {
        "reportFormat": "CTRF",
        "specVersion": "0.0.1",
        "identity": identity(config, hardgate),
        "evaluation": {
            "valid": all(
                ((item.get("extra") or {}).get("deepswe") or {}).get("validity_status")
                == "valid"
                for item in tests
            ),
            "candidate_outcome": "passed" if passed_count == len(tests) else "failed",
            "evaluator_command": evaluator_command,
        },
        "results": {
            "tool": {"name": "deepswe-evaluator-owned-vitest", "version": "3"},
            "summary": {
                "tests": len(tests),
                "passed": passed_count,
                "failed": len(tests) - passed_count,
                "skipped": 0,
                "pending": 0,
                "other": 0,
                "start": started_ms,
                "stop": stopped_ms,
            },
            "tests": tests,
        },
    }


def f2p_component(
    config: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
    expected: list[str] | None = None,
) -> dict[str, Any]:
    nodes = [str(node) for node in (expected if expected is not None else config["f2p"])]
    measured = [node for node in nodes if node in statuses]
    passed = [node for node in measured if statuses[node]["passed"]]
    total = len(nodes)
    score = (len(passed) / total) if total else 0.0
    return {
        "outcome": "passed" if len(passed) == total and total > 0 else "failed",
        "applicable": True,
        "passed": len(passed),
        "total": total,
        "measured": len(measured),
        "score": score,
        "failed_node_ids": [node for node in measured if not statuses[node]["passed"]],
    }


def state_component(contract: dict[str, Any]) -> dict[str, Any]:
    """Report the applicability of the conditional state-preservation check.

    ``na`` is only ever produced from a *positive*, contract-backed claim that
    the check does not apply (``persistent_state.involved`` is exactly
    ``False``). An undetermined or missing applicability is reported as
    ``unknown`` with ``applicable: null`` so no consumer can mistake it for
    ``na``, and an applicable-but-unexecuted check is reported as ``not_run``.
    """
    persistent = contract.get("persistent_state")
    reason = persistent.get("reason") if isinstance(persistent, dict) else None
    if not isinstance(persistent, dict):
        return {
            "outcome": "unknown",
            "applicable": None,
            "reason": "contract does not declare persistent_state; applicability is undetermined",
        }
    involved = persistent.get("involved")
    if involved is False:
        return {
            "outcome": "na",
            "applicable": False,
            "reason": reason
            or "no persistent state or serialization migration is involved",
        }
    if involved is True:
        return {
            "outcome": "not_run",
            "applicable": True,
            "reason": reason or "persistent state migration is involved",
        }
    return {
        "outcome": "unknown",
        "applicable": None,
        "reason": reason
        or "persistent_state.involved is not a boolean; applicability is undetermined",
    }


def create_baseline_tree(repo: Path, directory: Path) -> Path:
    """Export the immutable replay commit (``HEAD``) into an isolated worktree.

    The tree is materialized from ``git archive`` rather than the working tree,
    so a candidate cannot influence the "before" state. Dependencies are shared
    read-only through a symlink to the installed node_modules.
    """
    tree = directory / "baseline"
    tree.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", "HEAD"],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise EvaluationInfrastructureError(
            "cannot export the source baseline tree: "
            f"{archive.stderr.decode('utf-8', 'replace')[-2000:]}"
        )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(tree)],
        input=archive.stdout,
        capture_output=True,
        check=False,
    )
    if extract.returncode != 0:
        raise EvaluationInfrastructureError(
            "cannot extract the source baseline tree: "
            f"{extract.stderr.decode('utf-8', 'replace')[-2000:]}"
        )
    node_modules = repo / "node_modules"
    link = tree / "node_modules"
    if node_modules.exists() and not link.exists():
        try:
            link.symlink_to(node_modules, target_is_directory=True)
        except OSError as error:
            raise EvaluationInfrastructureError(
                f"cannot share dependencies with the source baseline: {error}"
            ) from error
    for root_dir, _directories, files in os.walk(tree):
        try:
            os.chmod(root_dir, 0o755)
        except OSError:
            pass
        for name in files:
            candidate = Path(root_dir) / name
            if candidate.is_symlink():
                continue
            try:
                os.chmod(candidate, 0o644)
            except OSError:
                pass
    return tree


def measure_source_baseline(repo: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Run the evaluator suite against the frozen source baseline.

    This provides the measured "before" state used to validate F2P / P2P
    labeling: a fail-to-pass node must fail before the migration and a
    pass-to-pass node must pass before it.
    """
    with tempfile.TemporaryDirectory(prefix="baseline-tree-") as directory:
        tree = create_baseline_tree(repo, Path(directory))
        vitest, command_evidence = run_evaluator(repo, config, repo_root=tree, user=None)
        statuses = extract_statuses(vitest)
    return {"statuses": statuses, "command": command_evidence, "source": "git-archive:HEAD"}


def blocker_from_hard_gate(hardgate: dict[str, Any]) -> dict[str, Any]:
    """Describe the actual hard-gate failure reported by the gate result.

    Late failures (for example a P2P regression discovered after evaluation) must
    keep their own node id, responsibility and error type. Fabricating a
    frozen-source blocker would mislabel an ordinary regression as source
    tampering.
    """
    failed_ids = [str(node) for node in (hardgate.get("failed_nodes") or [])]
    nodes_by_id = {
        str(node.get("id")): node
        for node in hardgate.get("nodes", [])
        if isinstance(node, dict) and node.get("id") is not None
    }
    first_failure = nodes_by_id.get(failed_ids[0], {}) if failed_ids else {}
    return {
        "id": failed_ids[0] if failed_ids else "gate.unknown",
        "message": first_failure.get(
            "message", "candidate failed deterministic hard gate"
        ),
        "responsibility": first_failure.get("responsibility", "candidate"),
        "error_type": first_failure.get(
            "error_type", "candidate_hard_gate_failure"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--ctrf-output", type=Path, required=True)
    parser.add_argument("--hard-gate-output", type=Path, required=True)
    parser.add_argument("--apply-failed", type=int, choices=(0, 1), default=0)
    args = parser.parse_args()
    started_ms = int(time.time() * 1000)
    config = load_object(args.config, "verifier config")
    contract = load_object(Path(config["contract_path"]), "migration contract")
    validate_configuration(config)
    hardgate_module = load_hardgate_module()

    try:
        hardgate = run_hardgate(args.config, args.repo, args.hard_gate_output, args.apply_failed)
        statuses: dict[str, dict[str, Any]] = {}
        blocker: dict[str, Any] | None = None
        evaluator_command: dict[str, Any] | None = None
        baseline_evidence: dict[str, Any] = {"measured_from": "not_run"}
        measurement_setup: dict[str, Any] = {
            "skipped": True,
            "reason": "not_run",
        }
        scope: dict[str, Any] = {
            "outcome": "not_run",
            "applicable": True,
            "complete": False,
            "passed_units": 0,
            "total_units": len(contract["migration_units"]),
            "score": 0.0,
            "units": [],
        }
        f2p: dict[str, Any] = {
            "outcome": "not_run",
            "applicable": True,
            "passed": 0,
            "total": len(config["f2p"]),
            "measured": 0,
            "score": 0.0,
            "failed_node_ids": [],
        }
        state = state_component(contract)
        hard_gate_passed = hardgate.get("passed") is True
        # The hard-gate verdict and the measured score are deliberately
        # decoupled: the evaluator always runs so the real F2P / P2P result is
        # observed and reported even when the gate fails, while the verdict is
        # anchored first and still governs the final reward (grader.py maps
        # cheating to -1 and any other gate failure to 0), so measuring a
        # rejected candidate can never turn it into a pass. Measurement is
        # best-effort: if the evaluator cannot run at all, the gate verdict is
        # kept and the score stays unmeasured instead of being promoted to a
        # platform infrastructure error.
        gate_failed = not hard_gate_passed
        if gate_failed:
            blocker = blocker_from_hard_gate(hardgate)
            # The gate stopped before the target-build phase on this path, so
            # the workspace may lack installed dependencies and freshly built
            # artifacts. Prepare it best-effort so the evaluator reports the
            # candidate's real behaviour instead of missing-artifact failures.
            # This cannot change the gate verdict or the reward.
            measurement_setup = hardgate_module.prepare_measurement(args.repo, config)
            # Measure the declared migration scope too, so a rejected candidate
            # still shows which migration units it completed instead of a
            # synthetic 0. The measurement is independent of the evaluator and
            # never overrides the gate verdict or the reward: an invalid
            # candidate package/lock is reported as an unmeasured scope rather
            # than promoted to a platform infrastructure error.
            scope = measure_scope(args.repo, contract)
        try:
            vitest, evaluator_command = run_evaluator(args.repo, config)
            statuses = extract_statuses(vitest)
            validate_statuses(config, statuses)
            # Measure the source baseline so F2P / P2P membership is a fact,
            # not an assumption: P2P counts only nodes that passed before,
            # and a node that already passed before is not fail-to-pass.
            baseline = measure_source_baseline(args.repo, config)
            before_statuses = baseline["statuses"]
            before_passed = {
                node for node, measured in before_statuses.items() if measured.get("passed")
            }
            configured_p2p = [str(node) for node in config["p2p"]]
            configured_f2p = [str(node) for node in config["f2p"]]
            p2p_nodes = [node for node in configured_p2p if node in before_passed]
            p2p_baseline_excluded = [
                node for node in configured_p2p if node not in before_passed
            ]
            f2p_nodes = [node for node in configured_f2p if node not in before_passed]
            f2p_baseline_excluded = [
                node for node in configured_f2p if node in before_passed
            ]
            uncovered = sorted(
                set(str(node) for node in config["required_nodes"])
                - set(p2p_nodes)
                - set(f2p_nodes)
            )
            if uncovered:
                raise EvaluationTaskError(
                    "source baseline leaves required evaluator nodes unscored: "
                    f"{uncovered}; a required node must be measured as P2P "
                    "(baseline passed) or F2P (baseline failed)",
                    error_class="evaluator_contract_error",
                )
            # P2P is a hard gate, not a verifier score component. Only the
            # nodes that already passed on the source baseline may regress.
            p2p_failed = [
                node
                for node in p2p_nodes
                if not (node in statuses and statuses[node]["passed"])
            ]
            p2p_passed = not p2p_failed
            hardgate = hardgate_module.apply_p2p_result(
                hardgate, p2p_passed, len(p2p_nodes), p2p_failed
            )
            hardgate["atomic_checks"]["p2p_no_regression"] = (
                "passed" if p2p_passed else "failed"
            )
            hardgate["p2p_baseline"] = {
                "measured_from": baseline["source"],
                "before_passed_node_ids": sorted(before_passed),
                "configured_p2p_excluded_not_passing_before": p2p_baseline_excluded,
                "configured_f2p_excluded_passing_before": f2p_baseline_excluded,
            }
            baseline["p2p_baseline_excluded"] = p2p_baseline_excluded
            baseline["f2p_baseline_excluded"] = f2p_baseline_excluded
            baseline_evidence = {
                "measured_from": baseline["source"],
                "before_passed_node_ids": sorted(before_passed),
                "configured_p2p_excluded_not_passing_before": p2p_baseline_excluded,
                "configured_f2p_excluded_passing_before": f2p_baseline_excluded,
            }
            # F2P belongs to the verifier, not the hard gate: its pass rate
            # is a scored component of ``T`` and a fully green fail-to-pass
            # set is a conjunction condition for verifier success. A
            # fail-to-pass node that did not turn green keeps the verifier
            # failed and yields ``reward = T < 1`` (never a hard-gate 0).
            f2p = f2p_component(config, statuses, expected=f2p_nodes)
            write_json(args.hard_gate_output, hardgate)
            hard_gate_passed = hardgate.get("passed") is True
            if hard_gate_passed:
                scope = measure_scope(args.repo, contract)
                if not scope["complete"]:
                    blocker = {
                        "id": "verifier.migration-scope-complete",
                        "message": "one or more declared migration units remain incomplete",
                        "responsibility": "candidate",
                        "error_type": "migration_incomplete",
                    }
        except CandidateExecutionError as error:
            blocker = {
                "id": f"execution.{error.error_type}",
                "message": str(error),
                "responsibility": "candidate",
                "evidence": error.evidence,
            }
        except EvaluationInfrastructureError:
            # Best-effort measurement only. When the gate has already failed,
            # an evaluator that cannot run (a candidate that broke the build,
            # a suite startup failure, ...) must not become a platform
            # infrastructure error: keep the gate verdict, leave the score
            # unmeasured, and let the reward contract reject the candidate as
            # before. A gate that passed still surfaces its own blocker.
            if not gate_failed:
                raise
            statuses = {}
            evaluator_command = None
        # The evaluator has now imported and executed candidate modules. Re-verify
        # the frozen sources so a build- or import-time side effect cannot rewrite
        # the behavior that was just measured.
        try:
            hardgate = hardgate_module.fail_post_evaluation(hardgate, args.repo, contract)
        except hardgate_module.InfrastructureError as error:
            raise EvaluationInfrastructureError(str(error)) from error
        if hardgate.get("passed") is not True and blocker is None:
            blocker = blocker_from_hard_gate(hardgate)
        # A hard-gate verdict can turn red *after* the evaluator ran -- a P2P
        # regression, or a post-evaluation frozen-source check -- and a
        # candidate execution error can also skip the success-path measurement.
        # In those cases the scope is still unmeasured, so take it now: a
        # rejected candidate must report its real migration scope, never a
        # synthetic 0 stand-in for the blocker.
        if scope["outcome"] == "not_run":
            scope = measure_scope(args.repo, contract)
        write_json(args.hard_gate_output, hardgate)
        verifier_components = {
            "migration_scope_complete": scope,
            # Spec name is "F2P regression"; keep `f2p` as an alias for the
            # existing grader/report consumers.
            "f2p_regression": f2p,
            "f2p": f2p,
            "required_state_preservation_complete": state,
        }
        stopped_ms = int(time.time() * 1000)
        ctrf = report(config, hardgate, statuses, blocker, started_ms, stopped_ms, evaluator_command)
        write_json(args.ctrf_output, ctrf)
        diagnostics = config["diagnostics"]
        write_json(
            Path(diagnostics["node_report"]),
            {"identity": ctrf["identity"], "tests": ctrf["results"]["tests"], "summary": ctrf["results"]["summary"]},
        )
        write_json(
            Path(diagnostics["verifier"]),
            {
                "schema_version": "webvr-verifier-result/v3",
                "identity": ctrf["identity"],
                "evaluation_valid": ctrf["evaluation"]["valid"],
                "candidate_outcome": ctrf["evaluation"]["candidate_outcome"],
                "hard_gate_passed": hardgate.get("passed") is True,
                "blocker": blocker,
                "components": verifier_components,
                "source_baseline": baseline_evidence,
                "measurement_setup": measurement_setup,
                "summary": ctrf["results"]["summary"],
            },
        )
        return 0
    except EvaluationInfrastructureError as error:
        stopped_ms = int(time.time() * 1000)
        if isinstance(error, EvaluationTaskError):
            error_class = error.error_class
            responsibility = "task_or_evaluator"
            retryable = False
        else:
            error_class = "evaluation_environment_or_verifier_error"
            responsibility = "evaluator_or_platform"
            retryable = True
        payload = {
            "schema_version": "evaluation-infrastructure-error/v1",
            "error_class": error_class,
            "responsibility": responsibility,
            "evaluation_valid": False,
            "retryable": retryable,
            "phase": "verifier",
            "message": str(error),
            "start": started_ms,
            "stop": stopped_ms,
        }
        try:
            write_json(Path(config["diagnostics"]["infrastructure_error"]), payload)
        except EvaluationInfrastructureError:
            pass
        print(f"[FATAL] {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
