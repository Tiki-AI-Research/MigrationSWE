from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Ensure verifier dir is on sys.path so sibling imports work
_verifier_dir = Path(__file__).resolve().parent
if str(_verifier_dir) not in sys.path:
    sys.path.insert(0, str(_verifier_dir))

from harbor_migration_toolkit import ChangedFile

from asset_integrity import verify_asset_manifest
from behavior_gate import evaluate_behavior_regression
from candidate_valid import evaluate_candidate
from environment_integrity import evaluate_environment_integrity
from legacy_identity import (
    LEGACY_PACKAGES,
    _code_matches,
    _fold_literal_concatenations,
    executable_text,
    is_production_source,
    legacy_loader_evidence,
    legacy_specifier_hits,
    literal_specifiers,
    load_lockfile_identity,
    node14_incompatible_specifiers,
    production_sources,
    unresolved_module_loader_evidence,
)
from sandbox import network_isolated_command, run_candidate_command
from test_integrity import evaluate_smoke_execution, evaluate_test_integrity


def run_command(repo: Path, args: list[str], timeout: int = 600) -> dict[str, Any]:
    if os.name == "nt" and args and args[0] == "npm":
        args = ["npm.cmd", *args[1:]]
    execution_command = network_isolated_command(args)
    try:
        result = subprocess.run(
            execution_command,
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "infrastructure_error",
            "timed_out": True,
            "command": args,
            "execution_command": execution_command,
            "network_isolated": os.name != "nt",
            "error": str(exc),
        }
    except FileNotFoundError as exc:
        return {
            "status": "infrastructure_error",
            "timed_out": False,
            "command": args,
            "execution_command": execution_command,
            "network_isolated": os.name != "nt",
            "error": f"command not found: {exc}",
        }
    except OSError as exc:
        return {
            "status": "infrastructure_error",
            "timed_out": False,
            "command": args,
            "execution_command": execution_command,
            "network_isolated": os.name != "nt",
            "error": str(exc),
        }

    namespace_error = result.returncode != 0 and any(
        marker in (result.stderr or "").lower()
        for marker in ("unshare:", "unshare failed", "operation not permitted", "failed to mount /proc")
    )
    return {
        "status": "passed" if result.returncode == 0 else (
            "infrastructure_error" if namespace_error else "candidate_failure"
        ),
        "command": args,
        "execution_command": execution_command,
        "network_isolated": os.name != "nt",
        "exit_code": result.returncode,
        "timed_out": False,
        "stdout": result.stdout[-4000:] if result.stdout else "",
        "stderr": result.stderr[-4000:] if result.stderr else "",
    }


_OFFLINE_NPM_REGISTRY = "https://registry.npmjs.org"
_NODE14_BINARY = "/opt/node14/bin/node"
_NODE14_VERSION = "v14.18.0"
_NODE14_SEMVER = "14.18.0"
_CHILD_PROCESS_METHOD = r"(?:spawnSync|execFileSync|execSync|spawn|execFile|exec|fork)"


def _legacy_fallback_evidence(text: str, alias_map: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Find process/network bridges to a source implementation, not ordinary residue.

    The child_process bridge fires on a legacy-looking target name
    (``legacy`` / ``express`` / ``old-server`` / ``source-server``) or on a
    script-like literal spawn/exec target (``node`` / ``ts-node`` / ``tsx`` /
    ``npm`` / ``npx`` / ``sharing`` / ``express`` / ``*.js`` / ``*.cjs`` /
    ``*.mjs``), so naming the bridged server ``srv.js`` does not evade it.
    """
    normalized = _fold_literal_concatenations(executable_text(text))
    def code_search(pattern: str, flags: int = 0) -> re.Match[str] | None:
        return next(_code_matches(re.compile(pattern, flags), normalized), None)

    def code_finditer(pattern: str, flags: int = 0):
        return _code_matches(re.compile(pattern, flags), normalized)

    # legacy_identity owns the loader patterns and folds internally; it must
    # receive the raw text so concatenated loaders stay detectable.
    evidence: list[dict[str, str]] = legacy_loader_evidence(text, alias_map)

    module_loader = (
        r"\bmodule\s*(?:\.\s*constructor|\[\s*['\"]constructor['\"]\s*\])"
        r"\s*(?:\.\s*_load\b|\[\s*['\"]_load['\"]\s*\])"
    )
    loader_functions = r"(?:\brequire\b|" + module_loader + r")"
    loader_aliases = {
        match.group(1)
        for match in code_finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            + loader_functions + r"\s*(?=;|,|$)",
        )
    }
    if loader_aliases:
        loader_functions = r"(?:" + loader_functions + r"|\b(?:" + "|".join(
            re.escape(name) for name in sorted(loader_aliases)
        ) + r")\b)"
    child_loader = loader_functions + r"\s*\(\s*['\"](?:node:)?child_process['\"]\s*\)"
    child_process_loaded = bool(code_search(child_loader))
    child_module_bindings = {
        match.group(1)
        for match in code_finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*" + child_loader,
        )
    }
    # Follow destructured child_process bindings, including renamed methods.
    # Only names bound to that module qualify; an unrelated spawn() must not.
    child_bindings: set[str] = set()
    for binding in code_finditer(
        r"\b(?:const|let|var)\s*\{([^{}]+)\}\s*=\s*" + child_loader,
    ):
        for field in binding.group(1).split(","):
            method = re.fullmatch(
                r"\s*(" + _CHILD_PROCESS_METHOD + r")(?:\s*:\s*([A-Za-z_$][\w$]*))?\s*", field
            )
            if method:
                child_bindings.add(method.group(2) or method.group(1))
    for module_name in child_module_bindings:
        for binding in code_finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            + re.escape(module_name)
            + r"\s*(?:\.\s*" + _CHILD_PROCESS_METHOD + r"\b|"
            r"\[\s*['\"]" + _CHILD_PROCESS_METHOD + r"['\"]\s*\])",
        ):
            child_bindings.add(binding.group(1))
    child_callee = r"(?:\.\s*" + _CHILD_PROCESS_METHOD + r"|\[\s*['\"]" + _CHILD_PROCESS_METHOD + r"['\"]\s*\]"
    if child_bindings:
        child_callee += r"|\b(?:" + "|".join(re.escape(name) for name in sorted(child_bindings)) + r")"
    child_callee += r")\s*\("
    child_method = bool(code_search(child_callee))
    legacy_bridge_target = bool(code_search(
        child_callee + r"[^;]*?(?:legacy|express|old[-_]?server|source[-_]?server)", re.I | re.S
    ))
    # B5: a literal script-like first argument is a bridge even when the
    # target name carries no legacy keyword (``spawn('node', ['srv.js'])``,
    # ``execFile('srv.js')``).  Union with the keyword branch keeps aliased or
    # differently named legacy servers matching.
    script_like_spawn_target = bool(
        code_search(
            child_callee + r"\s*['\"`][^'\"`]*"
            r"(?:\b(?:node|ts-node|tsx|npm|npx|sharing|express)\b|\.(?:js|cjs|mjs)\b)",
            re.I,
        )
        or code_search(
            child_callee + r"\s*process\.execPath\s*,\s*\[\s*['\"][^'\"]*"
            r"(?:legacy|express|old[-_]?server|source[-_]?server|\.(?:js|cjs|mjs)\b)",
            re.I,
        )
    )
    env_target_names = {
        name
        for name, key in (
            match.groups() for match in code_finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*process\.env\.([A-Za-z_$][\w$]*)",
            )
        )
        if re.search(r"legacy|express|old|source|server|runner|bridge|upstream|backend", key, re.I)
    }
    env_process_target = bool(
        env_target_names
        and code_search(
            child_callee + r"\s*(?:" + "|".join(re.escape(name) for name in sorted(env_target_names)) + r")\b",
        )
    )
    if child_process_loaded and child_method and (
        legacy_bridge_target or script_like_spawn_target or env_process_target
    ):
        evidence.append({"kind": "subprocess_bridge", "module": "child_process"})

    network_loader = r"\brequire\s*\(\s*['\"](?:node:)?(?:http|https|net|http2)['\"]\s*\)"
    network_module_loaded = bool(code_search(network_loader))
    network_modules = {
        match.group(1)
        for match in code_finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*" + network_loader,
        )
    }
    network_methods = r"(?:request|get|connect|createConnection)"
    network_method_bindings: set[str] = set()
    connect_method_bindings: set[str] = set()
    for binding in code_finditer(
        r"\b(?:const|let|var)\s*\{([^{}]+)\}\s*=\s*" + network_loader,
    ):
        for field in binding.group(1).split(","):
            method = re.fullmatch(
                r"\s*(request|get|connect|createConnection)"
                r"(?:\s*:\s*([A-Za-z_$][\w$]*))?\s*",
                field,
            )
            if method:
                bound_name = method.group(2) or method.group(1)
                network_method_bindings.add(bound_name)
                if method.group(1) in {"connect", "createConnection"}:
                    connect_method_bindings.add(bound_name)
    network_roots = network_loader
    if network_modules:
        network_roots += r"|\b(?:" + "|".join(
            re.escape(name) for name in sorted(network_modules)
        ) + r")\b"
    network_property = (
        r"(?:\.\s*" + network_methods + r"\b|"
        r"\[\s*['\"]" + network_methods + r"['\"]\s*\])"
    )
    for binding in code_finditer(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:" + network_roots + r")\s*" + network_property,
    ):
        network_method_bindings.add(binding.group(1))
    qualified_network_callee = r"(?:" + network_roots + r")\s*" + network_property
    network_callee = qualified_network_callee
    if network_method_bindings:
        network_callee += r"|\b(?:" + "|".join(
            re.escape(name) for name in sorted(network_method_bindings)
        ) + r")\b"
    network_callee = r"(?:" + network_callee + r")\s*\("
    network_call = bool(code_search(network_callee))
    loopback_target = bool(code_search(
        network_callee + r"[^;]*?(?:https?://)?(?:127(?:\.\d{1,3}){3}|0\.0\.0\.0|localhost)(?::\d+)?",
        re.I | re.S,
    ))
    # socketPath on HTTP options and path on net.connect options both select
    # a local Unix socket, even when the path comes from an unlabelled env var.
    socket_path_target = bool(code_search(
        network_callee + r"\s*\{[^{}]*['\"]?socketPath['\"]?\s*:", re.S
    ))
    connect_property = (
        r"(?:\.\s*(?:connect|createConnection)\b|"
        r"\[\s*['\"](?:connect|createConnection)['\"]\s*\])"
    )
    net_connect_callee = r"(?:" + network_roots + r")\s*" + connect_property
    if connect_method_bindings:
        net_connect_callee += r"|\b(?:" + "|".join(
            re.escape(name) for name in sorted(connect_method_bindings)
        ) + r")\b"
    net_connect_callee = r"(?:" + net_connect_callee + r")\s*\("
    ipc_path_target = bool(code_search(
        net_connect_callee + r"\s*\{[^{}]*['\"]?path['\"]?\s*:", re.S
    ))
    # A production HTTP call to a loopback target is itself a source bridge.
    # Do not require a legacy/Express keyword: endpoint names and URLs are
    # attacker-controlled and can be chosen to look like ordinary routes.
    if network_module_loaded and network_call and (loopback_target or socket_path_target or ipc_path_target):
        evidence.append({
            "kind": "loopback_network_bridge" if loopback_target else "unix_socket_network_bridge",
            "module": "http_or_net",
        })
    elif network_module_loaded and network_call and any(
        re.search(r"legacy|server|upstream|backend|bridge|source", name, re.I)
        for name in re.findall(r"process\.env\.([A-Za-z_$][\w$]*)", normalized)
    ):
        evidence.append({"kind": "environment_network_bridge", "module": "http_or_net"})

    rpc_transport = code_search(r"\brequire\s*\(\s*['\"](?:node-ipc|zeromq|grpc|@grpc/grpc-js)['\"]\s*\)")
    if rpc_transport:
        evidence.append({"kind": "rpc_bridge", "module": rpc_transport.group(0)})

    return evidence


def run_offline_npm_ci(repo: Path) -> dict[str, Any]:
    """Verify the candidate lockfile using only the evaluator's frozen npm cache."""
    lockfile_path = repo / "package-lock.json"
    try:
        json.loads(lockfile_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "status": "candidate_failure",
            "command": ["npm", "ci", "--offline"],
            "exit_code": 1,
            "timed_out": False,
            "stdout": "",
            "stderr": f"package-lock.json is unavailable or invalid: {exc}",
        }

    result = run_command(
        repo,
        [
            "npm",
            "ci",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            f"--registry={_OFFLINE_NPM_REGISTRY}",
            "--replace-registry-host=always",
        ],
        timeout=600,
    )

    combined_output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
    if result.get("status") == "candidate_failure" and "ENOTCACHED" in combined_output:
        result["status"] = "infrastructure_error"
        result["error"] = "evaluator npm cache is incomplete"
    return result


def target_build(repo: Path) -> dict[str, Any]:
    """Evaluate target_build_valid gate for sharing-express-to-fastify.

    Per INTERFACES.md:
    - npm ci --ignore-scripts --no-audit --no-fund (dependency_resolved)
    - node --check bin/app.js
    - node --check bin/index.js
    - dependency probe: fastify resolvable and installed version == 4.28.1,
      @fastify/multipart installed version == 8.3.1; lockfile top-level deps match.
    Missing target dependency / syntax error => candidate_failure.
    Tool missing (npm/node not found) or timeout => infrastructure_error.
    """
    checks: list[dict[str, Any]] = []

    runtime = run_command(repo, [_NODE14_BINARY, "--version"], timeout=30)
    runtime["id"] = "minimum_node_runtime"
    runtime["expected_version"] = _NODE14_VERSION
    if runtime["status"] == "passed" and runtime.get("stdout", "").strip() != _NODE14_VERSION:
        runtime["status"] = "infrastructure_error"
        runtime["success"] = None
        runtime["error"] = (
            f"evaluator Node runtime mismatch: expected {_NODE14_VERSION}, "
            f"found {runtime.get('stdout', '').strip()!r}"
        )
    checks.append(runtime)
    if runtime["status"] != "passed":
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": None,
            "status": "infrastructure_error",
            "summary": runtime.get("error", "minimum Node runtime is unavailable"),
            "checks": checks,
        }

    # 1. Dependencies must be prewarmed by the image. Installing from a public
    # registry during scoring makes results network-dependent and violates the
    # offline verifier contract.
    package_modules = repo / "node_modules"
    if not package_modules.is_dir():
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": None,
            "status": "infrastructure_error",
            "summary": "prewarmed node_modules is missing; runtime dependency installation is disabled",
            "checks": [{"id": "dependency_prewarm", "status": "infrastructure_error", "reason": "node_modules missing"}],
        }

    # Reject a lockfile that cannot run on the declared minimum Node before
    # npm consults the offline cache. Otherwise an incompatible candidate pin
    # can be mislabeled as an evaluator cache outage (ENOTCACHED).
    lock_engine_script = (
        "const fs = require('fs');\n"
        "const semver = require('semver');\n"
        "const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8'));\n"
        "const incompatible = [];\n"
        "for (const [packagePath, entry] of Object.entries(lock.packages || {})) {\n"
        "  const range = entry.engines && entry.engines.node;\n"
        "  if (!range) continue;\n"
        "  try {\n"
        f"    if (!semver.satisfies('{_NODE14_SEMVER}', range, {{includePrerelease: true}})) "
        "incompatible.push({packagePath, version: entry.version || null, range});\n"
        "  } catch (error) { incompatible.push({packagePath, version: entry.version || null, range, error: error.message}); }\n"
        "}\n"
        "process.stdout.write(JSON.stringify(incompatible));\n"
    )
    engine_preflight = run_command(repo, [_NODE14_BINARY, "-e", lock_engine_script], timeout=30)
    engine_preflight["id"] = "lockfile_engine_preflight"
    checks.append(engine_preflight)
    if engine_preflight["status"] != "passed":
        preflight_status = engine_preflight["status"]
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False if preflight_status == "candidate_failure" else None,
            "status": preflight_status,
            "summary": (
                "candidate configuration prevented locked dependency engine audit"
                if preflight_status == "candidate_failure"
                else "unable to audit locked dependency engines before offline install"
            ),
            "checks": checks,
        }
    try:
        incompatible_preflight = json.loads(engine_preflight.get("stdout", ""))
    except (TypeError, json.JSONDecodeError):
        incompatible_preflight = None
    engine_preflight["incompatible_packages"] = incompatible_preflight
    if not isinstance(incompatible_preflight, list):
        engine_preflight["status"] = "infrastructure_error"
        engine_preflight["error"] = "lockfile engine preflight returned malformed evidence"
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": None,
            "status": "infrastructure_error",
            "summary": engine_preflight["error"],
            "checks": checks,
        }
    if incompatible_preflight:
        engine_preflight["status"] = "candidate_failure"
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": f"locked dependency engines do not accept Node {_NODE14_SEMVER}",
            "checks": checks,
        }

    # 2. npm ci --offline verifies lockfile consistency without package-registry access.
    install = run_offline_npm_ci(repo)
    install["id"] = "dependency_resolved"
    checks.append(install)
    if install["status"] != "passed":
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False if install["status"] == "candidate_failure" else None,
            "status": install["status"],
            "summary": f"npm ci failed: exit_code={install.get('exit_code')}",
            "checks": checks,
        }

    # 2. Syntax check bin/app.js
    check_app = run_command(repo, [_NODE14_BINARY, "--check", "bin/app.js"], timeout=30)
    check_app["id"] = "syntax_check_bin_app"
    checks.append(check_app)
    if check_app["status"] != "passed":
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False if check_app["status"] == "candidate_failure" else None,
            "status": check_app["status"],
            "summary": f"syntax error in bin/app.js: {check_app.get('stderr', '').strip()}",
            "checks": checks,
        }

    # 3. Syntax check bin/index.js
    check_index = run_command(repo, [_NODE14_BINARY, "--check", "bin/index.js"], timeout=30)
    check_index["id"] = "syntax_check_bin_index"
    checks.append(check_index)
    if check_index["status"] != "passed":
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False if check_index["status"] == "candidate_failure" else None,
            "status": check_index["status"],
            "summary": f"syntax error in bin/index.js: {check_index.get('stderr', '').strip()}",
            "checks": checks,
        }

    # 4. Load the target framework on the declared minimum runtime, verify exact
    # top-level versions, and reject locked packages that declare an incompatible
    # Node engine range.
    probe_script = (
        "const fs = require('fs');\n"
        "const semver = require('semver');\n"
        "const manifest = JSON.parse(fs.readFileSync('package.json', 'utf8'));\n"
        "const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8'));\n"
        "const fastifyPkg = require('fastify/package.json');\n"
        "const multipartPkg = require('@fastify/multipart/package.json');\n"
        "require('fastify');\n"
        "const incompatibleEngines = [];\n"
        "for (const [packagePath, entry] of Object.entries(lock.packages || {})) {\n"
        "  const range = entry.engines && entry.engines.node;\n"
        "  if (!range) continue;\n"
        "  try {\n"
        f"    if (!semver.satisfies('{_NODE14_SEMVER}', range, {{includePrerelease: true}})) "
        "incompatibleEngines.push({packagePath, version: entry.version || null, range});\n"
        "  } catch (error) {\n"
        "    incompatibleEngines.push({packagePath, version: entry.version || null, range, error: error.message});\n"
        "  }\n"
        "}\n"
        "process.stdout.write(JSON.stringify({"
        "fastify: fastifyPkg.version, multipart: multipartPkg.version, "
        "manifestFastify: manifest.dependencies && manifest.dependencies.fastify, "
        "manifestMultipart: manifest.dependencies && manifest.dependencies['@fastify/multipart'], "
        "incompatibleEngines}));\n"
    )
    probe = run_command(repo, [_NODE14_BINARY, "-e", probe_script], timeout=30)
    probe["id"] = "target_dependency_probe"
    if probe["status"] != "passed":
        checks.append(probe)
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False if probe["status"] == "candidate_failure" else None,
            "status": probe["status"],
            "summary": f"required target dependency resolution failed: {probe.get('stderr', '').strip()}",
            "checks": checks,
        }

    try:
        versions = json.loads(probe.get("stdout", "{}"))
        fastify_ver = versions.get("fastify")
        multipart_ver = versions.get("multipart")
        manifest_fastify = versions.get("manifestFastify")
        manifest_multipart = versions.get("manifestMultipart")
        if (
            fastify_ver != "4.28.1"
            or multipart_ver != "8.3.1"
            or manifest_fastify != "4.28.1"
            or manifest_multipart != "8.3.1"
        ):
            probe["status"] = "candidate_failure"
            probe["error"] = (
                "version mismatch: "
                f"installed fastify={fastify_ver}, manifest fastify={manifest_fastify} "
                "(expected 4.28.1); "
                f"installed @fastify/multipart={multipart_ver}, "
                f"manifest @fastify/multipart={manifest_multipart} (expected 8.3.1)"
            )
            checks.append(probe)
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "target_build_valid",
                "applicable": True,
                "success": False,
                "status": "candidate_failure",
                "summary": probe["error"],
                "checks": checks,
            }
    except json.JSONDecodeError as exc:
        probe["status"] = "infrastructure_error"
        probe["error"] = f"unparseable version probe output: {exc}"
        checks.append(probe)
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": None,
            "status": "infrastructure_error",
            "summary": probe["error"],
            "checks": checks,
        }

    checks.append(probe)

    incompatible_engines = versions.get("incompatibleEngines")
    engine_check = {
        "id": "dependency_engine_compatibility",
        "runtime": _NODE14_SEMVER,
        "incompatible_packages": incompatible_engines,
    }
    if not isinstance(incompatible_engines, list):
        engine_check["status"] = "candidate_failure"
        engine_check["reason"] = "dependency engine probe did not return a package list"
        checks.append(engine_check)
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": engine_check["reason"],
            "checks": checks,
        }
    if incompatible_engines:
        engine_check["status"] = "candidate_failure"
        engine_check["reason"] = (
            f"locked dependency engines do not accept Node {_NODE14_SEMVER}"
        )
        checks.append(engine_check)
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": engine_check["reason"],
            "checks": checks,
        }
    engine_check["status"] = "passed"
    checks.append(engine_check)

    # 5. Lockfile top-level deps match
    lockfile_path = repo / "package-lock.json"
    if not lockfile_path.is_file():
        checks.append({
            "id": "lockfile_present",
            "status": "candidate_failure",
            "reason": "package-lock.json is missing",
        })
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": "package-lock.json is missing",
            "checks": checks,
        }

    try:
        lock_data = json.loads(lockfile_path.read_text(encoding="utf-8"))
        has_fastify_lock = False
        has_multipart_lock = False

        packages = lock_data.get("packages", {})
        if "node_modules/fastify" in packages and "node_modules/@fastify/multipart" in packages:
            has_fastify_lock = True
            has_multipart_lock = True
        else:
            deps = lock_data.get("dependencies", {})
            if "fastify" in deps and "@fastify/multipart" in deps:
                has_fastify_lock = True
                has_multipart_lock = True

        if not (has_fastify_lock and has_multipart_lock):
            checks.append({
                "id": "lockfile_target_deps",
                "status": "candidate_failure",
                "reason": "package-lock.json does not record fastify and @fastify/multipart",
            })
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "target_build_valid",
                "applicable": True,
                "success": False,
                "status": "candidate_failure",
                "summary": "package-lock.json does not record fastify and @fastify/multipart",
                "checks": checks,
            }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        checks.append({
            "id": "lockfile_valid_json",
            "status": "candidate_failure",
            "reason": str(exc),
        })
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "target_build_valid",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": f"package-lock.json is invalid JSON: {exc}",
            "checks": checks,
        }

    checks.append({
        "id": "lockfile_target_deps",
        "status": "passed",
    })

    return {
        "schema_version": "harbor-migration-check/v1",
        "id": "target_build_valid",
        "applicable": True,
        "success": True,
        "status": "passed",
        "summary": "target dependencies resolved, syntax valid, and target fastify installed",
        "checks": checks,
    }


def migration_integrity(
    repo: Path,
    base_dir: Path | None = None,
    *,
    base_commit: str | None = None,
    runtime_ready: bool = True,
) -> dict[str, Any]:
    """Evaluate migration_integrity_preserved gate for sharing-express-to-fastify.

    Per INTERFACES.md:
    1. no_deceptive_source_fallback:
       - no child_process/RPC/HTTP bridge to a source implementation in bin/**
       - a legacy package installed under a different package name (npm alias /
         masquerade) or reachable only through another package's dependency
         chain (undeclared at the root) is detected from package-lock.json
         identity, both in the dependency tree and in literal bin/** require()/import() specifiers
       Bridge or aliased/undeclared legacy identity in the production path =>
       policy_violation. (Don't flag test/**)
       - a plain literal legacy require() in bin/** (for example require('express'))
         is recorded as migration-scope residue without failing this gate.
    2. public_contract_preserved:
       - package.json name 'easy-sharing', main 'bin/index.js', bin.sharing / bin.easy-sharing unchanged,
         engines.node '>=14.18.0'
       - bin/app.js still exports 'start' and 'buildApp'
       - CLI routes / status codes not redefined in bin/index.js (byte-identical to base bin/index.js)
       Mismatch => candidate_failure.
    3. critical_data_preserved:
       - {status: "not_applicable", reason: "sharing migration does not touch persistent state"}
    """
    package_path = repo / "package.json"
    if not package_path.is_file():
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "migration_integrity_preserved",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": "package.json is missing",
            "checks": [{"id": "package_json_present", "status": "candidate_failure", "reason": "missing package.json"}],
        }

    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "migration_integrity_preserved",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": f"package.json is unparseable: {exc}",
            "checks": [{"id": "package_json_valid", "status": "candidate_failure", "reason": str(exc)}],
        }

    policy_reasons: list[str] = []
    checks: list[dict[str, Any]] = []

    # Ordinary legacy dependency residue is incomplete migration scope, not L2
    # cheating. Keep the evidence visible without terminating the hard gate.
    forbidden_deps = ("express", "express-fileupload", "express-basic-auth")
    deps = package.get("dependencies", {})
    legacy_deps_found = [d for d in forbidden_deps if d in deps]
    checks.append({
        "id": "legacy_dependency_residue_classification",
        "status": "passed",
        "legacy_dependencies": legacy_deps_found,
        "classification": "scope_incomplete" if legacy_deps_found else "none",
    })

    # Check production code (bin/**) for deceptive fallback and Node 14 incompatibilities
    bin_dir = repo / "bin"
    fallback_hits: list[dict[str, Any]] = []
    legacy_require_hits: list[dict[str, Any]] = []
    node14_incompat_hits: list[str] = []
    unresolved_loader_hits: list[dict[str, Any]] = []
    child_server_hits: list[str] = []
    compatibility_failures: list[str] = []

    # Security boundary: key matching cannot see a legacy runtime installed
    # under an aliased name. Plain legacy installs stay scope residue.
    identity_findings: list[dict[str, Any]] = []
    alias_map: dict[str, str] = {}
    try:
        identity_findings, alias_map = load_lockfile_identity(repo)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        compatibility_failures.append(f"package-lock.json identity unreadable: {exc}")
        checks.append({
            "id": "no_legacy_dependency_identity",
            "status": "candidate_failure",
            "reason": f"package-lock.json is unavailable or invalid: {exc}",
        })
    else:
        deception_findings = [
            item for item in identity_findings
            if item.get("kind") in {"aliased_install", "alias_spec", "undeclared_legacy_install"}
        ]
        if deception_findings:
            deception_kinds = {item.get("kind") for item in deception_findings}
            if (
                deception_kinds & {"aliased_install", "alias_spec"}
                and "legacy_dependency_identity_detected" not in policy_reasons
            ):
                policy_reasons.append("legacy_dependency_identity_detected")
            if (
                "undeclared_legacy_install" in deception_kinds
                and "undeclared_legacy_dependency_detected" not in policy_reasons
            ):
                policy_reasons.append("undeclared_legacy_dependency_detected")
            checks.append({
                "id": "no_legacy_dependency_identity",
                "status": "policy_violation",
                "hits": deception_findings,
                "reason": (
                    "legacy runtime installed under an aliased identity: "
                    f"{deception_findings}"
                ),
            })
        else:
            checks.append({
                "id": "no_legacy_dependency_identity",
                "status": "passed",
            })

    if bin_dir.is_dir():
        for path in production_sources(repo):
            if is_production_source(path):
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue

                rel_p = path.relative_to(repo).as_posix()
                detected_fallbacks = _legacy_fallback_evidence(text, alias_map)
                for hit in legacy_specifier_hits(text, alias_map):
                    if hit["aliased"]:
                        detected_fallbacks.append({
                            "kind": "aliased_legacy_require",
                            "specifier": hit["specifier"],
                            "resolves_to": hit["resolves_to"],
                        })
                    elif path.suffix.lower() not in {".js", ".cjs", ".mjs", ""}:
                        detected_fallbacks.append({"kind": "concealed_legacy_require", **hit})
                    else:
                        legacy_require_hits.append({"path": rel_p, **hit})
                if detected_fallbacks:
                    fallback_hits.append({"path": rel_p, "evidence": detected_fallbacks})
                if node14_incompatible_specifiers(text):
                    node14_incompat_hits.append(rel_p)
                unresolved = unresolved_module_loader_evidence(text)
                if unresolved:
                    unresolved_loader_hits.append({"path": rel_p, "evidence": unresolved})

                # Check child_process server launching
                # Note: bin/index.js in base legitimately uses spawn for openBrowser ('open' / 'xdg-open' / 'cmd /c start')
                # Any child_process usage in other production files (like bin/app.js) or any server launching is forbidden.
                if rel_p != "bin/index.js":
                    if any(item["kind"] == "subprocess_bridge" for item in detected_fallbacks):
                        child_server_hits.append(rel_p)
                else:
                    # In bin/index.js, check if spawn/exec is used to run node/server/express
                    if re.search(_CHILD_PROCESS_METHOD + r"\s*\(\s*['\"`]?\s*(?:node|express|sharing|npm|npx)", text, re.I):
                        child_server_hits.append(rel_p)

    if fallback_hits:
        if "fallback_implementation_detected" not in policy_reasons:
            policy_reasons.append("fallback_implementation_detected")
        if (
            any(
                item.get("kind") == "aliased_legacy_require"
                for hit in fallback_hits
                for item in hit.get("evidence", [])
            )
            and "aliased_legacy_runtime_detected" not in policy_reasons
        ):
            policy_reasons.append("aliased_legacy_runtime_detected")
        checks.append({
            "id": "no_source_implementation_bridge",
            "status": "policy_violation",
            "hits": fallback_hits,
            "reason": f"source implementation bridge found in production files: {fallback_hits}",
        })
    else:
        checks.append({
            "id": "no_source_implementation_bridge",
            "status": "passed",
        })

    checks.append({
        "id": "no_legacy_require_in_production",
        "status": "passed",
        "hits": legacy_require_hits,
        "classification": "scope_incomplete" if legacy_require_hits else "none",
    })
    checks.append({
        "id": "unresolved_module_loader",
        "status": "passed",
        "classification": "unresolved" if unresolved_loader_hits else "none",
        "hits": unresolved_loader_hits,
    })

    if node14_incompat_hits:
        compatibility_failures.append(
            f"Node 14 incompatible stream/promises module load found: {node14_incompat_hits}"
        )
        checks.append({
            "id": "no_stream_promises",
            "status": "candidate_failure",
            "hits": node14_incompat_hits,
            "reason": f"Node 14 incompatible stream/promises module load found: {node14_incompat_hits}",
        })
    else:
        checks.append({
            "id": "no_stream_promises",
            "status": "passed",
        })

    if child_server_hits:
        if "child_server_spawning_detected" not in policy_reasons:
            policy_reasons.append("child_server_spawning_detected")
        checks.append({
            "id": "no_child_server_spawning",
            "status": "policy_violation",
            "hits": child_server_hits,
            "reason": f"child_process server spawning detected: {child_server_hits}",
        })
    else:
        checks.append({
            "id": "no_child_server_spawning",
            "status": "passed",
        })

    # If policy violations exist, return policy_violation
    if policy_reasons:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "migration_integrity_preserved",
            "applicable": True,
            "success": False,
            "status": "policy_violation",
            "summary": f"migration integrity violation: {', '.join(policy_reasons)}",
            "policy_reasons": policy_reasons,
            "checks": checks,
            "critical_data_preserved": {
                "status": "na",
                "reason": "sharing migration does not touch persistent state",
            },
        }

    # 2. Public contract preserved checks (candidate_failure on mismatch)
    pkg_name = package.get("name")
    pkg_main = package.get("main")
    pkg_bin = package.get("bin", {})
    pkg_engines = package.get("engines", {})

    contract_failures: list[str] = list(compatibility_failures)
    if pkg_name != "easy-sharing":
        contract_failures.append(f"package name changed to {pkg_name!r}")
    if pkg_main != "bin/index.js":
        contract_failures.append(f"package main changed to {pkg_main!r}")
    if not isinstance(pkg_bin, dict) or pkg_bin.get("sharing") != "./bin/index.js" or pkg_bin.get("easy-sharing") != "./bin/index.js":
        contract_failures.append(f"bin mapping changed: {pkg_bin!r}")
    if not isinstance(pkg_engines, dict) or pkg_engines.get("node") != ">=14.18.0":
        contract_failures.append(f"engines.node changed: {pkg_engines!r}")

    # Check bin/app.js exports start and buildApp
    app_exports_script = (
        "const app = require('./bin/app');\n"
        "const hasStart = typeof app.start === 'function';\n"
        "const hasBuildApp = typeof app.buildApp === 'function';\n"
        "const capabilities = {inject: null, ready: null, close: null, version: null};\n"
        "let buildError = null;\n"
        "if (hasBuildApp) {\n"
        "  try {\n"
        "    const instance = app.buildApp({});\n"
        "    capabilities.inject = typeof instance.inject;\n"
        "    capabilities.ready = typeof instance.ready;\n"
        "    capabilities.close = typeof instance.close;\n"
        "    capabilities.version = instance && typeof instance.version === 'string' ? instance.version : null;\n"
        "  } catch (error) {\n"
        "    buildError = String((error && error.message) || error);\n"
        "  }\n"
        "}\n"
        "let fastifyVersion = null;\n"
        "try {\n"
        "  fastifyVersion = require('fastify/package.json').version;\n"
        "} catch (error) {\n"
        "  fastifyVersion = null;\n"
        "}\n"
        "process.stdout.write('HARBOR_EXPORTS=' + JSON.stringify({start: hasStart, buildApp: hasBuildApp, capabilities: capabilities, buildError: buildError, fastifyVersion: fastifyVersion}) + '\\n');\n"
    )
    if runtime_ready:
        app_exports_probe = run_candidate_command(
            repo,
            ["node", "-e", app_exports_script],
            timeout=30,
        )
        app_exports_probe["id"] = "sandboxed_app_exports_probe"
    else:
        app_exports_probe = {
            "id": "sandboxed_app_exports_probe",
            "status": "blocked",
            "success": None,
            "reason": "target_build_valid did not pass; runtime dependencies are not verified",
        }
    checks.append(app_exports_probe)
    if app_exports_probe["status"] == "infrastructure_error":
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "migration_integrity_preserved",
            "applicable": True,
            "success": None,
            "status": "infrastructure_error",
            "summary": app_exports_probe.get("error", "candidate sandbox unavailable"),
            "checks": checks,
            "critical_data_preserved": {
                "status": "na",
                "reason": "sharing migration does not touch persistent state",
            },
        }
    if app_exports_probe["status"] not in {"passed", "blocked"}:
        contract_failures.append(f"failed to probe bin/app exports: {app_exports_probe.get('stderr', '').strip()}")
    elif app_exports_probe["status"] == "passed":
        try:
            export_lines = [
                line.removeprefix("HARBOR_EXPORTS=")
                for line in app_exports_probe.get("stdout", "").splitlines()
                if line.startswith("HARBOR_EXPORTS=")
            ]
            if len(export_lines) != 1:
                raise json.JSONDecodeError("expected one authenticated export line", "", 0)
            exp_data = json.loads(export_lines[0])
            if not exp_data.get("start") or not exp_data.get("buildApp"):
                contract_failures.append(f"bin/app does not export both start and buildApp: {exp_data}")
            if exp_data.get("buildApp"):
                build_error = exp_data.get("buildError")
                if build_error:
                    contract_failures.append(f"buildApp({{}}) failed: {build_error}")
                capabilities = exp_data.get("capabilities")
                if not isinstance(capabilities, dict):
                    capabilities = {}
                missing_lifecycle = [
                    method
                    for method in ("inject", "ready", "close")
                    if capabilities.get(method) != "function"
                ]
                if missing_lifecycle:
                    contract_failures.append(
                        "buildApp() does not return a Fastify instance: "
                        f"missing lifecycle method(s) {missing_lifecycle}"
                    )
                instance_version = capabilities.get("version")
                if not isinstance(instance_version, str) or not instance_version:
                    contract_failures.append(
                        "buildApp() instance version is not a non-empty string: "
                        f"{instance_version!r}"
                    )
                fastify_version = exp_data.get("fastifyVersion")
                if (
                    isinstance(fastify_version, str)
                    and fastify_version
                    and fastify_version != instance_version
                ):
                    contract_failures.append(
                        "buildApp() instance version does not match installed fastify: "
                        f"{instance_version!r} != {fastify_version!r}"
                    )
        except json.JSONDecodeError as exc:
            contract_failures.append(f"invalid bin/app export probe output: {exc}")

    # Check bin/index.js byte-identical to base bin/index.js
    candidate_index_path = repo / "bin" / "index.js"
    base_index_content: bytes | None = None

    if base_dir and (base_dir / "bin" / "index.js").is_file():
        base_index_content = (base_dir / "bin" / "index.js").read_bytes()
    else:
        if base_commit:
            base_c = base_commit
            git_base = subprocess.run(
                ["git", "show", f"{base_c}:bin/index.js"],
                cwd=repo,
                capture_output=True,
                check=False,
            )
            if git_base.returncode == 0:
                base_index_content = git_base.stdout

    if base_index_content is not None and candidate_index_path.is_file():
        candidate_index_bytes = candidate_index_path.read_bytes()
        base_norm = base_index_content.replace(b"\r\n", b"\n")
        cand_norm = candidate_index_bytes.replace(b"\r\n", b"\n")
        if base_norm != cand_norm:
            contract_failures.append("bin/index.js has been modified (must be byte-identical to base)")
    elif not candidate_index_path.is_file():
        contract_failures.append("bin/index.js is missing")

    if contract_failures:
        checks.append({
            "id": "public_contract_preserved",
            "status": "candidate_failure",
            "failures": contract_failures,
            "reason": f"public contract mismatches: {'; '.join(contract_failures)}",
        })
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "migration_integrity_preserved",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": f"public contract violated: {contract_failures[0]}",
            "checks": checks,
            "critical_data_preserved": {
                "status": "na",
                "reason": "sharing migration does not touch persistent state",
            },
        }

    if not runtime_ready:
        checks.append({
            "id": "public_contract_preserved",
            "status": "blocked",
            "reason": "runtime export validation requires a successful target build",
        })
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "migration_integrity_preserved",
            "applicable": True,
            "success": None,
            "status": "blocked",
            "summary": "static migration checks passed; runtime export validation is blocked by target_build_valid",
            "checks": checks,
            "critical_data_preserved": {
                "status": "na",
                "reason": "sharing migration does not touch persistent state",
            },
        }

    checks.append({
        "id": "public_contract_preserved",
        "status": "passed",
    })

    return {
        "schema_version": "harbor-migration-check/v1",
        "id": "migration_integrity_preserved",
        "applicable": True,
        "success": True,
        "status": "passed",
        "summary": "migration integrity and public contract verified",
        "checks": checks,
        "critical_data_preserved": {
            "status": "na",
            "reason": "sharing migration does not touch persistent state",
        },
    }


def load_policy(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("status") != "ready":
        raise ValueError("integrity policy is not ready")
    files = tuple(item["path"] for item in value.get("protected_files", []))
    directories = tuple(item["path"] for item in value.get("protected_directories", []))
    return files, directories


def _blocked_gate(gate_id: str) -> dict[str, Any]:
    return {
        "schema_version": "harbor-migration-check/v1",
        "id": gate_id,
        "applicable": True,
        "success": None,
        "status": "blocked",
        "summary": "gate blocked due to earlier failure",
        "checks": [],
    }


# Integrity gates must run even after an earlier failure so that cheating
# evidence (test tampering, protected-path writes, source-implementation
# bridges) always determines the terminal -1 reward instead of being hidden
# behind an unrelated build failure.
_ALWAYS_RUN_AFTER_FAILURE = frozenset(
    {
        "migration_integrity_preserved",
        "test_integrity",
        "evaluation_environment_integrity",
    }
)
# When the evaluator itself failed, later signals are not attributable to the
# candidate; keep the full short-circuit in that case.
_EVALUATOR_FAILURE_STATUSES = frozenset({"infrastructure_error", "configuration_error"})


def evaluate(repo: Path, patch: Path, config_path: Path) -> dict[str, Any]:
    """Execute the composite hard gate in configured order with short-circuiting."""
    repo = Path(repo).resolve()
    patch = Path(patch).resolve()
    config_path = Path(config_path).resolve()

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "hard_gate",
            "hard_gate_passed": False,
            "status": "configuration_error",
            "summary": f"unable to read config.json: {exc}",
            "gates": {},
            "evidence": {"changed_files": [], "policy_reasons": []},
        }

    assets_dir = config_path.parent / "assets"
    asset_report = verify_asset_manifest(assets_dir)
    if asset_report.get("success") is not True:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "hard_gate",
            "hard_gate_passed": False,
            "status": "configuration_error",
            "summary": asset_report.get("summary", "evaluator asset manifest validation failed"),
            "gates": {},
            "asset_integrity": asset_report,
            "evidence": {"changed_files": [], "policy_reasons": []},
        }

    gate_order: list[str] = config.get(
        "hard_gate_order",
        [
            "candidate_valid",
            "test_integrity",
            "evaluation_environment_integrity",
            "target_build_valid",
            "migration_integrity_preserved",
            "behavior_regression",
        ],
    )

    # Base identity is evaluator-owned. The marker lives in the separate
    # evaluator reference checkout, never in the candidate repository; it is
    # used for the operational commit while config.json fixes its tree.
    configured_base = str(config.get("base_commit", "")).strip()
    evaluator_marker = Path("/opt/sharing-base/.base_commit")
    if evaluator_marker.is_file():
        try:
            base_commit = evaluator_marker.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            base_commit = ""
            base_identity_error = f"unable to read evaluator base marker: {exc}"
        else:
            base_identity_error = None
    else:
        # Local selftests have no evaluator image; use the configured commit,
        # never a marker supplied by the candidate repository.
        base_commit = configured_base
        base_identity_error = None

    if not base_commit or base_identity_error:
        err_gate = {
            "schema_version": "harbor-migration-check/v1",
            "id": "candidate_valid",
            "applicable": True,
            "success": None,
            "status": "configuration_error",
            "summary": base_identity_error or "evaluator base commit and config.base_commit are missing",
            "checks": [],
        }
        gates_map: dict[str, Any] = {gate_order[0]: err_gate}
        for g in gate_order[1:]:
            gates_map[g] = _blocked_gate(g)
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "hard_gate",
            "hard_gate_passed": False,
            "status": "configuration_error",
            "gates": gates_map,
            "evidence": {"changed_files": [], "policy_reasons": []},
        }

    # The operational commit may have a generated hash, but its tree must equal
    # the frozen config tree. This detects a changed archive or a forged marker.
    expected_tree = str(config.get("base_tree_hash", "")).strip()
    tree_probe = subprocess.run(
        ["git", "rev-parse", f"{base_commit}^{{tree}}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    actual_tree = tree_probe.stdout.strip() if tree_probe.returncode == 0 else ""
    if expected_tree and actual_tree != expected_tree:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "hard_gate",
            "hard_gate_passed": False,
            "status": "configuration_error",
            "summary": "evaluator baseline tree does not match config.json",
            "gates": {gate_order[0]: {
                "schema_version": "harbor-migration-check/v1",
                "id": gate_order[0],
                "applicable": True,
                "success": None,
                "status": "configuration_error",
                "summary": "base tree hash mismatch",
                "checks": [{"id": "base_tree_hash", "status": "configuration_error", "expected": expected_tree, "actual": actual_tree}],
            }},
            "evidence": {"changed_files": [], "policy_reasons": [], "base_commit": base_commit, "configured_base_commit": configured_base, "base_tree_hash": {"expected": expected_tree, "actual": actual_tree}},
        }

    git_audit: dict[str, Any] = {"status": "not_available"}
    audit_root = Path("/opt/sharing-base")
    if audit_root.is_dir():
        audit_files = {
            "fsck_unreachable": audit_root / ".git-fsck-unreachable",
            "object_inventory": audit_root / ".git-object-inventory",
            "reflog": audit_root / ".git-reflog",
        }
        missing_audit = [name for name, path in audit_files.items() if not path.is_file()]
        if missing_audit:
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "hard_gate",
                "hard_gate_passed": False,
                "status": "configuration_error",
                "summary": f"baseline Git audit artifacts are missing: {missing_audit}",
                "gates": {},
                "evidence": {"changed_files": [], "policy_reasons": [], "git_audit": {"status": "configuration_error", "missing": missing_audit}},
            }
        fsck_text = audit_files["fsck_unreachable"].read_text(encoding="utf-8", errors="replace")
        git_audit = {
            "status": "passed" if "unreachable" not in fsck_text.lower() else "configuration_error",
            "fsck_unreachable": fsck_text[-4000:],
            "object_inventory_sha256": hashlib.sha256(audit_files["object_inventory"].read_bytes()).hexdigest(),
            "reflog_sha256": hashlib.sha256(audit_files["reflog"].read_bytes()).hexdigest(),
        }
        if git_audit["status"] != "passed":
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "hard_gate",
                "hard_gate_passed": False,
                "status": "configuration_error",
                "summary": "baseline Git audit reports unreachable objects",
                "gates": {},
                "evidence": {"changed_files": [], "policy_reasons": [], "git_audit": git_audit},
            }

    gates_map = {}
    policy_reasons: list[str] = []
    changed_files: tuple[ChangedFile, ...] = ()

    # Base reference directory and archive for test / migration integrity
    base_dir_candidate = Path("/opt/sharing-base")
    base_dir = base_dir_candidate if base_dir_candidate.is_dir() else None
    archive_path = Path("/tests/assets/base-repo.tar.gz")
    if not archive_path.is_file():
        local_archive = config_path.parent / "assets" / "base-repo.tar.gz"
        if local_archive.is_file():
            archive_path = local_archive

    short_circuit = False
    failing_status: str | None = None
    failing_gate: str | None = None

    for gate_id in gate_order:
        # A failed candidate patch means there is no trustworthy candidate
        # tree to audit, so the remaining gates stay blocked.
        if short_circuit and (
            failing_status in _EVALUATOR_FAILURE_STATUSES
            or failing_gate == "candidate_valid"
            or gate_id not in _ALWAYS_RUN_AFTER_FAILURE
        ):
            gates_map[gate_id] = _blocked_gate(gate_id)
            continue

        gate_result: dict[str, Any]

        if gate_id == "candidate_valid":
            configured_allowlist = config.get("allowed_change_paths")
            if not isinstance(configured_allowlist, list) or not all(isinstance(item, str) and item for item in configured_allowlist):
                gate_result = {
                    "schema_version": "harbor-migration-check/v1",
                    "id": "candidate_valid",
                    "applicable": True,
                    "success": None,
                    "status": "configuration_error",
                    "summary": "config.json allowed_change_paths must be a non-empty string array",
                    "checks": [],
                }
                changed_files = ()
            else:
                gate_result, changed_files = evaluate_candidate(
                    repo,
                    patch,
                    base_commit,
                    allow_empty=False,
                    allowed_paths=tuple(configured_allowlist),
                )
        elif gate_id == "target_build_valid":
            gate_result = target_build(repo)
        elif gate_id == "migration_integrity_preserved":
            gate_result = migration_integrity(
                repo,
                base_dir=base_dir,
                base_commit=base_commit,
                runtime_ready=gates_map.get("target_build_valid", {}).get("success") is True,
            )
        elif gate_id == "test_integrity":
            gate_result = evaluate_test_integrity(
                repo,
                base_dir=base_dir,
                archive_path=archive_path,
            )
        elif gate_id == "evaluation_environment_integrity":
            policy_file = Path(config.get("integrity_policy", "/tests/assets/evaluation-integrity-policy.json"))
            if not policy_file.is_file():
                local_policy = config_path.parent / "assets" / "evaluation-integrity-policy.json"
                if local_policy.is_file():
                    policy_file = local_policy
            try:
                protected_files, protected_directories = load_policy(policy_file)
            except Exception as exc:
                gate_result = {
                    "schema_version": "harbor-migration-check/v1",
                    "id": "evaluation_environment_integrity",
                    "applicable": True,
                    "success": None,
                    "status": "configuration_error",
                    "summary": f"failed to load integrity policy: {exc}",
                    "checks": [],
                }
            else:
                gate_result = evaluate_environment_integrity(
                    repo,
                    changed_files,
                    protected_files=protected_files,
                    protected_directories=protected_directories,
                )
        elif gate_id == "behavior_regression":
            # Fail-closed: behavior_gate sets success=True only when every
            # configured behavior node passed, so any other result reaches the
            # short-circuit below and forces hard_gate_passed to False.
            gate_result = evaluate_behavior_regression(
                repo,
                config,
                config_path=config_path,
            )
        else:
            gate_result = {
                "schema_version": "harbor-migration-check/v1",
                "id": gate_id,
                "applicable": True,
                "success": None,
                "status": "configuration_error",
                "summary": f"unknown gate in hard_gate_order: {gate_id}",
                "checks": [],
            }

        gates_map[gate_id] = gate_result

        # Runtime smoke validation needs target dependencies and must follow
        # the static migration bridge checks. Keep its verdict in test_integrity
        # and stop before behavior/scoring when the smoke is a stub or a no-op.
        if gate_id == "migration_integrity_preserved" and gate_result.get("success") is True:
            integrity = gates_map.get("test_integrity")
            if integrity is not None and integrity.get("success") is True:
                smoke_execution = evaluate_smoke_execution(repo)
                integrity["checks"].append(smoke_execution)
                for check in integrity["checks"]:
                    if check["id"] == "fastify_smoke_test":
                        check["execution_check"] = smoke_execution["id"]
                        check["status"] = smoke_execution["status"]
                if smoke_execution["success"] is not True:
                    integrity.update(success=smoke_execution["success"], status=smoke_execution["status"],
                                     summary=smoke_execution.get("reason", "smoke execution unavailable"))
                    integrity.setdefault("reasons", []).append("required_fastify_smoke_incomplete")
                    short_circuit = True
                    failing_status = smoke_execution["status"]
                    failing_gate = "test_integrity"

        if gate_result.get("policy_reasons"):
            policy_reasons.extend(gate_result["policy_reasons"])

        g_status = gate_result.get("status")
        g_success = gate_result.get("success")

        if g_success is not True:
            short_circuit = True
            if failing_status is None or g_status in _EVALUATOR_FAILURE_STATUSES:
                failing_status = g_status
                failing_gate = gate_id

    if any(g.get("status") == "policy_violation" for g in gates_map.values()):
        top_status = "policy_violation"
        passed = False
    elif failing_status is not None:
        top_status = failing_status
        passed = False
    else:
        top_status = "passed"
        passed = True

    return {
        "schema_version": "harbor-migration-check/v1",
        "id": "hard_gate",
        "hard_gate_passed": passed,
        "status": top_status,
        "gates": gates_map,
        "evidence": {
            "changed_files": [item.__dict__ for item in changed_files],
            "policy_reasons": policy_reasons,
            "base_commit": base_commit,
            "configured_base_commit": configured_base,
            "base_tree_hash": expected_tree,
            "git_audit": git_audit,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Harbor Hard Gate Verifier for sharing-express-to-fastify")
    parser.add_argument("--repo", type=Path, required=True, help="Path to repository worktree")
    parser.add_argument("--patch", type=Path, required=True, help="Path to candidate patch")
    parser.add_argument("--config", type=Path, required=True, help="Path to config.json")
    parser.add_argument("--out", type=Path, required=True, help="Output JSON path")
    args = parser.parse_args()

    try:
        report = evaluate(args.repo.resolve(), args.patch.resolve(), args.config.resolve())
    except Exception as exc:
        report = {
            "schema_version": "harbor-migration-check/v1",
            "id": "hard_gate",
            "hard_gate_passed": False,
            "status": "configuration_error",
            "summary": f"unhandled verifier exception: {exc}",
            "gates": {},
            "evidence": {"changed_files": [], "policy_reasons": []},
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report.get("hard_gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
