#!/usr/bin/env python3
"""Deterministic policy, identity, migration, build, and integrity gates.

The module implements the six composite hard gates required for a migration
task:

* ``candidate_valid``            - patch applicability, parseability, boundary
* ``target_build_valid``         - dependency resolution and target build
* ``migration_integrity_preserved`` - frozen sources, preflight, public contract,
                                      deceptive-source fallback
* ``p2p_regression_preserved``   - pass-to-pass behavior must not regress
* ``test_integrity``             - public tests not tampered/disabled
* ``evaluation_environment_integrity`` - hidden asset isolation, protected paths,
                                      anti-cheating

Each composite is a conjunction over explicit atomic checks. ``na`` atomic
checks (only ``critical_data_preserved`` for this build-only migration) do not
participate in the conjunction and are never treated as pass or fail.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import fnmatch
import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable


CANDIDATE_USER = "candidate"
EVALUATOR_USER = "evaluator"


class InfrastructureError(RuntimeError):
    """Evaluation-environment failure that must not be scored as candidate."""

    def __init__(self, message: str, *, error_class: str = "evaluation_environment_error"):
        super().__init__(message)
        self.error_class = error_class


class CandidateFailure(RuntimeError):
    """Ordinary candidate failure. Maps to a non-cheating hard-gate failure."""


class CandidatePolicyViolation(CandidateFailure):
    pass


class CheatingDetected(CandidatePolicyViolation):
    def __init__(self, message: str, *, level: str):
        super().__init__(message)
        self.level = level


def strip_js_comments(text: str) -> str:
    """Remove JS comments while preserving comment markers inside strings.

    A naive ``//`` strip truncates string literals such as
    ``'https://host/'`` (and split forms like ``'http' + '://host'``), which
    both hides real network usage and corrupts the scanned evidence. This state
    machine only treats ``//`` and ``/* */`` as comments outside single, double,
    and template strings.
    """
    out: list[str] = []
    state = "code"
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        following = text[index + 1] if index + 1 < length else ""
        if state == "code":
            if char == "/" and following == "/":
                state = "line"
                index += 2
                continue
            if char == "/" and following == "*":
                state = "block"
                index += 2
                continue
            if char in ("'", '"', "`"):
                state = {"'": "single", '"': "double", "`": "template"}[char]
            out.append(char)
            index += 1
            continue
        if state == "line":
            if char == "\n":
                state = "code"
                out.append(char)
            index += 1
            continue
        if state == "block":
            if char == "*" and following == "/":
                state = "code"
                index += 2
                continue
            if char == "\n":
                out.append(char)
            index += 1
            continue
        # Inside a string literal: preserve escapes and the closing delimiter.
        if char == "\\" and index + 1 < length:
            out.append(char)
            out.append(following)
            index += 2
            continue
        if (state == "single" and char == "'") or (state == "double" and char == '"') or (state == "template" and char == "`"):
            state = "code"
        out.append(char)
        index += 1
    return "".join(out)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(argv: list[str], *, cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InfrastructureError(f"failed to execute {argv!r}: {error}") from error


def git(repo: Path, *args: str) -> str:
    result = command(["git", "-C", str(repo), *args])
    if result.returncode != 0:
        raise InfrastructureError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise InfrastructureError(f"invalid {label} at {path}: {error}") from error
    if not isinstance(value, dict):
        raise InfrastructureError(f"{label} must be a JSON object: {path}")
    return value


def load_candidate_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise CandidatePolicyViolation(f"invalid {label} at {path}: {error}") from error
    if not isinstance(value, dict):
        raise CandidatePolicyViolation(f"{label} must be a JSON object: {path}")
    return value


def candidate_env(repo: Path) -> dict[str, str]:
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(
        {
            "HOME": f"/home/{CANDIDATE_USER}",
            "CI": "1",
            "NO_COLOR": "1",
            "NPM_CONFIG_OFFLINE": "true",
            "NPM_CONFIG_PREFER_OFFLINE": "true",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "npm_config_offline": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "PWD": str(repo),
        }
    )
    if os.geteuid() == 0:
        env["NPM_CONFIG_CACHE"] = "/opt/npm-cache-candidate"
        env["npm_config_cache"] = "/opt/npm-cache-candidate"
    return env


def candidate_prefix() -> list[str]:
    if os.geteuid() != 0:
        return []
    if not shutil.which("runuser") or subprocess.run(
        ["id", CANDIDATE_USER], capture_output=True, check=False
    ).returncode != 0:
        raise InfrastructureError(f"{CANDIDATE_USER} execution user is unavailable")
    return ["runuser", "--user", CANDIDATE_USER, "--"]


def run_candidate(
    label: str,
    argv: list[str],
    *,
    repo: Path,
    timeout: int,
) -> dict[str, Any]:
    """Run a candidate-owned command, treating a timeout as infrastructure."""
    full_argv = [*candidate_prefix(), *argv]
    started = time.time_ns()
    try:
        result = subprocess.run(
            full_argv,
            cwd=repo,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env=candidate_env(repo),
        )
    except subprocess.TimeoutExpired as error:
        raise InfrastructureError(
            f"{label} timed out after {timeout}s", error_class="candidate_timeout"
        ) from error
    except OSError as error:
        raise InfrastructureError(f"cannot launch {label}: {error}") from error
    stopped = time.time_ns()
    return {
        "label": label,
        "argv": argv,
        "cwd": str(repo),
        "started_ns": started,
        "stopped_ns": stopped,
        "duration_ms": (stopped - started) // 1_000_000,
        "exit_code": result.returncode,
        "timed_out": False,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def runtime_check(config: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "path": os.environ.get("PATH", ""),
        "node_path": shutil.which("node") or "",
        "npm_path": shutil.which("npm") or "",
    }
    for name in ("node", "npm"):
        result = command([name, "--version"], timeout=10)
        if result.returncode != 0:
            raise InfrastructureError(f"{name} --version failed: {result.stderr.strip()}")
        metadata[f"{name}_version"] = result.stdout.strip()
    try:
        node_major = int(metadata["node_version"].lstrip("v").split(".", 1)[0])
        npm_major = int(metadata["npm_version"].split(".", 1)[0])
    except ValueError as error:
        raise InfrastructureError(f"unparseable runtime versions: {metadata}") from error
    expected = config["hard_gate"]
    if node_major != int(expected["node_major"]):
        raise InfrastructureError(
            f"evaluation image requires Node {expected['node_major']}.x, got {metadata['node_version']}"
        )
    if npm_major != int(expected["npm_major"]):
        raise InfrastructureError(
            f"evaluation image requires npm {expected['npm_major']}.x, got {metadata['npm_version']}"
        )
    return metadata


def verify_assets(config: dict[str, Any]) -> dict[str, str]:
    manifest_path = Path(config["asset_manifest"])
    manifest = load_object(manifest_path, "evaluator asset manifest")
    identities: dict[str, str] = {}
    root = manifest_path.parent
    for relative, expected in manifest.get("files", {}).items():
        path = root / relative
        if not path.is_file():
            raise InfrastructureError(f"evaluator asset missing: {relative}")
        actual = sha256(path)
        if expected.get("sha256") != actual:
            raise InfrastructureError(
                f"evaluator asset identity mismatch for {relative}: expected {expected.get('sha256')}, got {actual}"
            )
        identities[relative] = actual
    if not identities:
        raise InfrastructureError("evaluator asset manifest is empty")
    return identities


def network_isolation_probe() -> dict[str, Any]:
    """Measure whether the evaluation runtime blocks arbitrary egress.

    The verifier runs with ``network_mode = "no-network"``, so the candidate
    must not be able to reach arbitrary hosts (which could be used to fetch the
    reference implementation or leak answers). The probe is measured, not
    declared: from the candidate identity it performs a complete HTTP exchange
    and a DNS query, and only records a target as reachable when an actual
    response comes back.

    A bare TCP handshake is not sufficient: deny-all egress control
    transparently redirects TCP to a local proxy, which accepts the connection
    before rejecting the request. Payload round-trips distinguish "connection
    accepted by an in-container proxy" from "arbitrary egress works". Any
    reachable target fails the ``evaluation_environment_integrity`` composite
    gate, so a runtime that did not apply the policy cannot silently produce a
    passing trial.
    """
    targets = [
        ("example.com", 80, "http"),
        ("1.1.1.1", 53, "dns"),
    ]
    prefix = candidate_prefix()
    script = (
        "import socket,sys\n"
        "reachable=[]\n"
        "try:\n"
        "    s=socket.create_connection(('example.com',80),timeout=3)\n"
        "    s.sendall(b'GET / HTTP/1.0\\r\\nHost: example.com\\r\\n\\r\\n')\n"
        "    s.settimeout(3)\n"
        "    data=s.recv(64)\n"
        "    s.close()\n"
        "    if data.startswith(b'HTTP/'):\n"
        "        reachable.append('example.com:80')\n"
        "except OSError:\n"
        "    pass\n"
        "try:\n"
        "    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
        "    s.settimeout(3)\n"
        "    query=bytes.fromhex('abcd01000001000000000000076578616d706c6503636f6d0000010001')\n"
        "    s.sendto(query,('1.1.1.1',53))\n"
        "    data,_=s.recvfrom(512)\n"
        "    s.close()\n"
        "    if len(data)>=12 and data[:2]==b'\\xab\\xcd' and data[2]&0x80:\n"
        "        reachable.append('1.1.1.1:53')\n"
        "except OSError:\n"
        "    pass\n"
        "sys.stdout.write(','.join(reachable))\n"
    )
    try:
        result = subprocess.run(
            [*prefix, "python3", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        reachable = [item for item in result.stdout.strip().split(",") if item]
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InfrastructureError(f"network isolation probe could not run: {error}") from error
    return {
        "isolated": not reachable,
        "reachable": reachable,
        "targets": [f"{host}:{port} ({kind})" for host, port, kind in targets],
        "note": "arbitrary egress reachable from the candidate identity" if reachable else "egress blocked",
    }


def candidate_isolation_probe() -> dict[str, Any]:
    """Prove the candidate identity cannot traverse/read hidden evaluation assets.

    ``mandatory`` paths are evaluator-owned image layers (``/tests``). Their
    access mode is set at image build time, so a readable path is a hard
    configuration failure.

    ``advisory`` paths (``/logs/verifier``) are host bind-mount roots created by
    the platform. On some runtimes (e.g. Docker Desktop file sharing) the mount
    root's in-container permissions are not enforced, so a readable directory is
    recorded as a platform property instead of failing the whole evaluation.
    No hidden evaluator content is written there before candidate execution.
    """
    network = network_isolation_probe()
    if not network["isolated"]:
        # A runtime that did not apply the declared ``no-network`` policy makes
        # the whole evaluation invalid: it is a platform/configuration failure,
        # not a candidate result (SOP: config/infrastructure error -> no reward).
        raise InfrastructureError(
            "candidate identity can reach arbitrary egress: "
            + (", ".join(network["reachable"]) or "unknown target"),
            error_class="evaluation_environment_error",
        )
    mandatory_paths = [
        "/tests",
        "/tests/config.json",
        "/tests/contract.json",
        "/tests/assets-manifest.json",
        "/tests/assets/public-test-baseline.json",
        "/tests/grader.py",
        "/tests/verifier/verifier.py",
        "/tests/verifier/hardgate.py",
        "/tests/evaluator/behavior.test.js",
        "/tests/evaluator/vitest.config.mjs",
    ]
    advisory_paths = ["/logs/verifier"]
    if os.geteuid() != 0:
        return {
            "status": "skipped",
            "reason": "not running as root; cannot drop to candidate",
            "network": network,
            "network_isolated": network["isolated"],
        }
    user_check = command(["id", CANDIDATE_USER], timeout=10)
    if user_check.returncode != 0:
        raise InfrastructureError("candidate execution user is unavailable for isolation probe")

    def probe(paths: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        records: list[dict[str, Any]] = []
        readable: list[str] = []
        for path in paths:
            if not os.path.exists(path):
                continue
            candidate = command(
                ["runuser", "--user", CANDIDATE_USER, "--", "/usr/bin/test", "-r", path],
                timeout=10,
            )
            candidate_readable = candidate.returncode == 0
            records.append(
                {
                    "path": path,
                    "exists": True,
                    "readable_by_evaluator": os.access(path, os.R_OK),
                    "readable_by_candidate": candidate_readable,
                }
            )
            if candidate_readable:
                readable.append(path)
        return records, readable

    mandatory, mandatory_readable = probe(mandatory_paths)
    advisory, advisory_readable = probe(advisory_paths)
    if mandatory_readable:
        raise InfrastructureError(
            "candidate identity can read hidden evaluation assets: "
            + ", ".join(mandatory_readable)
        )
    return {
        "status": "enforced",
        "responsibility": "evaluator",
        "mandatory": mandatory,
        "advisory": advisory,
        "probes": mandatory + advisory,
        "hidden_assets_unreadable_to_candidate": True,
        "advisory_readable_by_candidate": advisory_readable,
        "network": network,
        "network_isolated": network["isolated"],
        "advisory_note": (
            "advisory paths are platform bind-mount roots; no hidden evaluator "
            "asset is written there before candidate execution"
        ),
    }


def verify_replay(
    repo: Path,
    contract: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> dict[str, str]:
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    expected_tree = contract["replay_base"]["tree"]
    if tree != expected_tree:
        raise InfrastructureError(
            f"replay identity mismatch: expected tree {expected_tree}, HEAD tree {tree}"
        )
    if manifest is not None:
        manifest_tree = manifest.get("base", {}).get("tree")
        if manifest_tree != expected_tree:
            raise InfrastructureError(
                f"candidate collector replay mismatch: expected tree {expected_tree}, "
                f"candidate base {manifest_tree}"
            )
    return {"head": head, "tree": tree}


def parse_cached_changes(repo: Path) -> list[dict[str, str]]:
    raw = command(
        ["git", "-C", str(repo), "diff", "--cached", "--raw", "-z", "--no-renames", "HEAD", "--"]
    )
    if raw.returncode != 0:
        raise InfrastructureError(f"cannot inspect candidate index: {raw.stderr.strip()}")
    fields = raw.stdout.split("\0")
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(fields) and fields[index]:
        header = fields[index]
        index += 1
        path = fields[index]
        index += 1
        parts = header.split()
        if len(parts) != 5 or len(parts[4]) != 1 or parts[4] not in {"A", "M", "D"}:
            raise CandidatePolicyViolation(f"unsupported candidate change record: {header} {path}")
        changes.append(
            {
                "path": path,
                "operation": {"A": "add", "M": "modify", "D": "delete"}[parts[4]],
                "old_mode": parts[0].removeprefix(":"),
                "new_mode": parts[1],
                "old_oid": parts[2],
                "new_oid": parts[3],
            }
        )
    return changes


def candidate_boundary(repo: Path, config: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    patch_path = Path(config["candidate_patch"])
    manifest_path = Path(config["candidate_manifest"])
    if not patch_path.is_file() or not manifest_path.is_file():
        raise CandidatePolicyViolation("candidate patch or manifest is missing")
    manifest = load_candidate_object(manifest_path, "candidate patch manifest")
    expected_base_tree = contract["replay_base"]["tree"]
    if manifest.get("base", {}).get("tree") != expected_base_tree:
        raise CandidatePolicyViolation(
            "candidate collector base tree does not match the packaged replay tree"
        )
    if manifest.get("policy", {}).get("passed") is not True:
        raise CandidatePolicyViolation("candidate collector policy did not pass")
    actual_patch_hash = sha256(patch_path)
    if manifest.get("patch", {}).get("sha256") != actual_patch_hash:
        raise CandidatePolicyViolation("candidate patch hash does not match its manifest")

    actual = parse_cached_changes(repo)
    declared = manifest.get("entries")
    if not isinstance(declared, list) or actual != declared:
        raise CandidatePolicyViolation("applied candidate delta does not match collector manifest")
    allowed = contract["allowed_changes"]
    for entry in actual:
        path = entry["path"]
        operation = entry["operation"]
        operations = allowed.get(path, [])
        if operation not in operations:
            for pattern, pattern_operations in contract.get("allowed_change_patterns", {}).items():
                if fnmatch.fnmatch(path, pattern):
                    operations = pattern_operations
                    break
        if operation not in operations:
            raise CandidatePolicyViolation(f"candidate change is outside allowed surface: {operation} {path}")
        modes = {
            "add": ("000000", "100644"),
            "modify": ("100644", "100644"),
            "delete": ("100644", "000000"),
        }[operation]
        # The collector accepts an executable build driver because package
        # scripts invoke it via `node`; the mode is not part of the contract.
        if (
            operation == "add"
            and path in ("scripts/build-vite.mjs", "scripts/watch-vite.mjs")
            and entry["new_mode"] == "100755"
        ):
            modes = ("000000", "100755")
        if (entry["old_mode"], entry["new_mode"]) != modes:
            raise CandidatePolicyViolation(f"forbidden file type or mode for {path}")

    # Match the collector's source inventory, including files hidden by
    # .gitignore, .git/info/exclude or a global Git ignore configuration.
    untracked = command([
        "git", "-C", str(repo), "ls-files", "--others", "-z",
        "--exclude=/.base_commit", "--exclude=/node_modules/", "--exclude=/build/",
    ])
    if untracked.returncode != 0:
        raise InfrastructureError(f"cannot inspect untracked candidate files: {untracked.stderr.strip()}")
    undeclared_untracked = sorted(path for path in untracked.stdout.split("\0") if path)
    if undeclared_untracked:
        raise CandidatePolicyViolation(
            f"working tree contains files outside the applied candidate index: {undeclared_untracked}"
        )
    return {
        "patch_sha256": actual_patch_hash,
        "base_tree": manifest["base"]["tree"],
        "entries": actual,
    }


def frozen_sources(repo: Path, contract: dict[str, Any]) -> dict[str, str]:
    identities: dict[str, str] = {}
    for relative, expected in contract["frozen_sources"].items():
        path = repo / relative
        if not path.is_file() or path.is_symlink():
            raise CandidatePolicyViolation(f"frozen source missing or wrong type: {relative}")
        actual = sha256(path)
        baseline_blob = expected["git_blob"]
        object_result = command(
            ["git", "-C", str(repo), "hash-object", "--no-filters", "--", relative]
        )
        if object_result.returncode != 0:
            raise InfrastructureError(
                f"cannot hash frozen source {relative}: {object_result.stderr.strip()}"
            )
        mode_result = command(
            ["git", "-C", str(repo), "ls-tree", "HEAD", "--", relative]
        )
        mode_parts = mode_result.stdout.split()
        if len(mode_parts) < 3:
            raise InfrastructureError(f"baseline frozen source identity is unavailable: {relative}")
        if (
            actual != expected["sha256"]
            or mode_parts[0] != expected["mode"]
            or mode_parts[2] != baseline_blob
            or object_result.stdout.strip() != baseline_blob
        ):
            raise CandidatePolicyViolation(f"frozen source identity changed: {relative}")
        identities[relative] = actual
    return identities


def fail_post_evaluation(result: dict[str, Any], repo: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """Re-verify frozen source identities after candidate-controlled code ran.

    ``gate.frozen-sources`` runs before the build, the mutation check, and the
    evaluator, but candidate build scripts and module side effects execute
    afterwards. A candidate could rewrite a frozen source between the two
    points and then be scored on the rewritten behavior, so the identities are
    checked again once the candidate execution phase is finished. Any change is
    a candidate policy violation and turns the hard gate result into a failure.
    """
    try:
        frozen_sources(repo, contract)
    except CandidatePolicyViolation as error:
        node = {
            "id": "gate.frozen-sources-post-evaluation",
            "phase": "source_integrity",
            "execution_status": "completed",
            "measurement_status": "measured",
            "outcome": "failed",
            "responsibility": "policy",
            "validity_status": "valid",
            "blocked_by": [],
            "error_type": "candidate_policy_violation",
            "message": str(error),
        }
        result.setdefault("nodes", []).append(node)
        failed = result.setdefault("failed_nodes", [])
        if node["id"] not in failed:
            failed.append(node["id"])
        result["passed"] = False
        result["evaluation_valid"] = True
        result["infrastructure_error"] = False
        result["candidate_outcome"] = "failed"
        result["failure_class"] = "hard_gate"
        result.setdefault("composite_gates", {})["migration_integrity_preserved"] = "failed"
        result.setdefault("atomic_checks", {})["frozen_sources_post_evaluation"] = False
    return result


def protected_paths_check(entries: list[dict[str, str]], contract: dict[str, Any]) -> dict[str, Any]:
    """Reject any candidate change that intersects a declared protected path."""
    protected = [str(pattern) for pattern in contract.get("protected_paths", [])]
    intersection: list[dict[str, str]] = []
    for entry in entries:
        path = entry["path"]
        for pattern in protected:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch("/" + path, pattern):
                intersection.append({"path": path, "pattern": pattern})
                break
    if intersection:
        evaluator_hits = [item for item in intersection if item["pattern"].startswith("/tests")]
        if evaluator_hits:
            raise CheatingDetected(
                f"L1 protected evaluation path modified: {evaluator_hits}", level="L1"
            )
        raise CandidatePolicyViolation(
            f"candidate change intersects a protected path: {intersection}"
        )
    return {
        "protected_paths": protected,
        "changed_paths": sorted(entry["path"] for entry in entries),
        "intersection": [],
    }


def text_and_diff_checks(repo: Path, entries: list[dict[str, str]]) -> dict[str, Any]:
    diff_check = command(["git", "-C", str(repo), "diff", "--cached", "--check", "HEAD", "--"])
    if diff_check.returncode != 0:
        raise CandidatePolicyViolation(f"git diff --check failed: {diff_check.stdout.strip() or diff_check.stderr.strip()}")
    checked: list[str] = []
    for entry in entries:
        if entry["operation"] == "delete":
            continue
        path = repo / entry["path"]
        data = path.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            raise CandidatePolicyViolation(f"UTF-8 BOM is forbidden: {entry['path']}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CandidatePolicyViolation(f"changed text is not strict UTF-8: {entry['path']}: {error}") from error
        checked.append(entry["path"])
    return {"diff_check": True, "utf8_files": checked}


def migration_contract(
    repo: Path, compile_paths: list[str], contract: dict[str, Any]
) -> dict[str, Any]:
    try:
        package = json.loads((repo / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((repo / "package-lock.json").read_text(encoding="utf-8"))
    except Exception as error:
        raise CandidateFailure(f"package metadata is invalid: {error}") from error
    vite_config = next(
        (name for name in ("vite.config.mjs", "vite.config.js") if (repo / name).is_file()),
        None,
    )
    vitest_config = next(
        (name for name in ("vitest.config.mjs", "vitest.config.js") if (repo / name).is_file()),
        None,
    )
    if vite_config is None or vitest_config is None:
        raise CandidateFailure("Vite and Vitest configuration files are required")
    # The exact script bodies live in the contract so the hard gate and the
    # verifier score the same `scripts` migration unit with the same semantics.
    scripts_unit = next(
        (
            unit
            for unit in contract.get("migration_units", [])
            if unit.get("kind") == "package_scripts"
        ),
        {},
    )
    expected_scripts = dict(scripts_unit.get("expected_scripts", {}))
    for name in scripts_unit.get("required", []):
        if not isinstance(package.get("scripts", {}).get(name), str):
            raise CandidateFailure(f"package script {name!r} is missing")
    for name, value in expected_scripts.items():
        if package.get("scripts", {}).get(name) != value:
            raise CandidateFailure(f"package script {name!r} must equal {value!r}")
    if package.get("dependencies") != {"cardboard-vr-display": "^1.0.19"}:
        raise CandidateFailure("runtime dependency contract changed")
    if package.get("devDependencies") != {
        "jsdom": "^25.0.1",
        "localStorage": "^1.0.3",
        "vite": "^5.4.10",
        "vitest": "^2.1.3",
    }:
        raise CandidateFailure("development dependency set is not the required migration set")
    if lock.get("lockfileVersion") != 3 or not isinstance(lock.get("packages"), dict):
        raise CandidateFailure("package-lock.json must be a complete lockfile v3")
    legacy = re.compile(r"\b(?:rollup|babel|mocha)\b", re.IGNORECASE)
    if any(legacy.search(str(value)) for value in package.get("scripts", {}).values()):
        raise CandidateFailure("active package scripts retain a legacy tool")
    test_script = package.get("scripts", {}).get("test")
    template = scripts_unit.get("test_script_template", "vitest run --config {config}")
    expected_test = template.format(config=vitest_config)
    if test_script != expected_test:
        raise CandidateFailure(
            f"package script 'test' must equal {expected_test!r}"
        )
    # Obsolete legacy configuration files are *incomplete migration*, scored by
    # the verifier's `legacy-removal` scope unit, not a hard-gate failure. The
    # only hard requirement here is that the migrated build path is valid.
    effective_compile_paths = [
        vite_config if relative == "vite.config.mjs" else
        vitest_config if relative == "vitest.config.mjs" else relative
        for relative in compile_paths
    ]
    for relative in effective_compile_paths:
        path = repo / relative
        if not path.is_file():
            raise CandidateFailure(f"required migration file is missing: {relative}")
        result = command(["node", "--check", str(path)], cwd=repo)
        if result.returncode != 0:
            raise CandidateFailure(f"syntax check failed for {relative}: {result.stderr.strip()}")
    return {
        "scripts": {**expected_scripts, "test": test_script},
        "lockfile_version": 3,
        "syntax_paths": effective_compile_paths,
    }


def dependency_resolved(repo: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Atomic ``dependency_resolved``: candidate dependencies install offline."""
    node_modules = repo / "node_modules"
    build_dir = repo / "build"
    if node_modules.exists():
        shutil.rmtree(node_modules)
    if build_dir.exists():
        shutil.rmtree(build_dir)
    if os.geteuid() == 0:
        try:
            shutil.chown(repo, user=CANDIDATE_USER, group=CANDIDATE_USER)
        except (LookupError, OSError) as error:
            raise InfrastructureError(
                f"cannot prepare candidate workspace ownership: {error}"
            ) from error
    install = run_candidate(
        "npm_ci_offline",
        ["npm", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"],
        repo=repo,
        timeout=300,
    )
    if install["exit_code"] != 0:
        output = (install["stderr"] or install["stdout"]).strip()
        # `dependency_resolved` is a hard-gate atomic (04 spec 4.3): an
        # unresolvable dependency (including an offline-cache miss) is a
        # candidate failure that maps to reward=0, never an infrastructure
        # abort. Treating it as infrastructure left no reward file behind.
        if any(marker in output for marker in ("ENOTCACHED", "cache miss", "request to https://registry")):
            raise CandidateFailure(
                f"dependency_resolved failed: candidate lockfile requires packages "
                f"that are not available in the target offline environment: {output[-2000:]}"
            )
        raise CandidateFailure(f"dependency install failed: {output[-4000:]}")
    return {
        "command": "npm ci --offline",
        "exit_code": install["exit_code"],
        "duration_ms": install["duration_ms"],
    }


def _chown_tree(path: Path, user: str) -> None:
    """Best-effort recursive chown so the candidate identity can build in a copy."""
    if os.geteuid() != 0:
        return
    try:
        import pwd

        entry = pwd.getpwnam(user)
    except (KeyError, ImportError):
        return
    for root_dir, directories, files in os.walk(path):
        for name in [root_dir, *(os.path.join(root_dir, item) for item in (*files, *directories))]:
            try:
                os.chown(name, entry.pw_uid, entry.pw_gid)
            except OSError:
                pass


def _copy_probe_repo(repo: Path, directory: Path) -> Path:
    """Copy the candidate tree (minus build/git/node_modules) into an isolated dir.

    Dependencies are shared read-only through a symlink so a probe build runs
    the exact same toolchain the candidate installed, without touching the
    delivered tree.
    """
    temp = directory / "repo"

    def ignore(_dir: str, names: list[str]) -> list[str]:
        return [name for name in names if name in {"node_modules", "build", ".git"}]

    shutil.copytree(repo, temp, symlinks=True, ignore=ignore)
    # The temporary parent is created 0700 by tempfile; the candidate identity
    # must be able to traverse it to run the probe build.
    try:
        os.chmod(directory, 0o755)
    except OSError:
        pass
    _chown_tree(directory, CANDIDATE_USER)
    _chown_tree(temp, CANDIDATE_USER)
    node_modules = repo / "node_modules"
    if node_modules.exists():
        # The probe must see dependencies at the same package-relative location
        # as the real checkout. A symlink to the delivered tree resolves back
        # outside the probe root and makes a legitimate package-relative path
        # check fail during the probe even though the normal build succeeds.
        # Materialize the tree (hard links where possible) so the probe remains
        # isolated while keeping dependency paths inside the probe package.
        def link_or_copy(source: str, destination: str) -> None:
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)

        try:
            shutil.copytree(
                node_modules,
                temp / "node_modules",
                symlinks=True,
                copy_function=link_or_copy,
            )
        except OSError as error:
            raise InfrastructureError(
                f"cannot materialize dependencies for the build provenance probe: {error}"
            ) from error
        _chown_tree(temp / "node_modules", CANDIDATE_USER)
    return temp


_BROKEN_PROBE_PAYLOADS: tuple[tuple[str, str], ...] = (
    ("src/webvr-polyfill.js", "\nconst __probe_NONCE__ = (( ;\n"),
    ("src/util.js", "\nfunction __probe_NONCE__( { return\n"),
    ("src/config.js", "\nconst __probe_NONCE__ = ;\n"),
)


def _assert_build_consumes_source(repo: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Prove the target build actually bundles the migrated source.

    A build that merely re-emits a packaged bundle (or concatenates source text)
    does not run the migrated module graph. Two independent probes on a
    throwaway copy make a stale/forged build observable *through behaviour*
    rather than through a matchable text marker:

    * a behaviour-affecting mutation is injected into ``src/config.js``
      (``PROVIDE_MOBILE_VRDISPLAY`` default); the produced artifact must
      reproduce the mutated observable behaviour, and
    * a randomly chosen source module is broken with a random nonce, so the
      build must fail.

    The first probe requires the artifact to be genuinely produced from the
    source (a re-emitted legacy bundle keeps the old behaviour), and the random,
    multi-target payload of the second prevents hardcoding the probe input.
    """
    with tempfile.TemporaryDirectory(prefix="build-provenance-") as directory:
        temp = _copy_probe_repo(repo, Path(directory))
        probe = Path(__file__).with_name("contract_probe.mjs")
        config_path = temp / "src/config.js"
        original = config_path.read_text(encoding="utf-8")
        needle = "PROVIDE_MOBILE_VRDISPLAY: true,"
        if needle not in original:
            raise CandidateFailure(
                "build provenance probe cannot locate the migrated configuration default"
            )
        config_path.write_text(
            original.replace(needle, "PROVIDE_MOBILE_VRDISPLAY: false,", 1),
            encoding="utf-8",
        )
        marker_run = run_candidate(
            "build_provenance_behavior",
            ["node", "scripts/build-vite.mjs"],
            repo=temp,
            timeout=300,
        )
        if marker_run["exit_code"] != 0:
            output = (marker_run["stderr"] or marker_run["stdout"]).strip()
            raise CandidateFailure(
                "build provenance probe failed: the target build could not build the "
                f"migrated source: {output[-2000:]}"
            )
        artifact = temp / "build/webvr-polyfill.js"
        if not artifact.is_file():
            raise CandidateFailure(
                "build provenance failed: the target build produced no UMD artifact"
            )
        observed = _run_contract_probe(
            temp, artifact, probe, Path(directory) / "observable.json"
        )
        ios_native_empty = (observed.get("behavior") or {}).get("ios-native-empty") or {}
        if ios_native_empty.get("count") != 0:
            raise CandidateFailure(
                "build provenance failed: the target build output does not reflect the "
                "migrated source (a mutation of src/config.js did not propagate to the "
                "artifact; the artifact was not produced from the source)"
            )
        nonce = hashlib.sha256(os.urandom(8)).hexdigest()[:10]
        victim, template = random.choice(_BROKEN_PROBE_PAYLOADS)
        victim_path = temp / victim
        victim_original = victim_path.read_text(encoding="utf-8")
        victim_path.write_text(
            victim_original + template.replace("NONCE", nonce), encoding="utf-8"
        )
        broken_run = run_candidate(
            "build_provenance_broken",
            ["node", "scripts/build-vite.mjs"],
            repo=temp,
            timeout=300,
        )
        if broken_run["exit_code"] == 0:
            raise CandidateFailure(
                "build provenance failed: the target build ignores the migrated source "
                "(it succeeds even when a source module is syntactically broken)"
            )
    return {
        "source_consumed": True,
        "behavior_probe": {"ios-native-empty": ios_native_empty.get("count")},
    }


def _assert_readable_artifacts_preserved(repo: Path, expected: dict[str, str]) -> None:
    """Fail as a candidate failure when the minified build removes/rewrites readable outputs.

    ``build-min`` is an incremental build: it may add/replace the minified
    artifacts, but it must not delete or rewrite the readable artifacts left by
    the full build.  Keeping this check in a helper makes the failure mode
    testable and, importantly, prevents a missing path from surfacing as a
    ``FileNotFoundError``/Harbor reward-file infrastructure error.
    """
    for relative, digest in expected.items():
        path = repo / relative
        if not path.is_file():
            raise CandidateFailure(f"minified build removed the readable artifact: {relative}")
        if sha256(path) != digest:
            raise CandidateFailure(
                f"minified build replaced or modified the readable artifact: {relative}"
            )


def _uses_target_bundler(driver_text: str) -> bool:
    """Recognize the Vite API and an installed Vite CLI build invocation.

    Runtime source-mutation probes below still establish that the output was
    actually built. Import spelling alone must not exclude the permitted CLI.
    """
    executable = strip_js_comments(driver_text)
    if re.search(
        r"(?:from\s*['\"]vite['\"]|(?:require|import)\s*\(\s*['\"]vite['\"])",
        executable,
    ):
        return True
    cli_path = re.compile(
        r"(?:['\"]vite['\"]\s*,\s*['\"]bin['\"]\s*,\s*['\"]vite\.js['\"]|"
        r"(?:node_modules/)?vite/bin/vite\.js|node_modules/\.bin/vite)"
    )
    cli_names = []
    for declaration in re.finditer(
        r"\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;]+);", executable
    ):
        if cli_path.search(declaration[2]):
            cli_names.append(re.escape(declaration[1]))
    targets = cli_names + [r"['\"][^'\"]*(?:vite/bin/vite\.js|\.bin/vite)['\"]"]
    target = "(?:" + "|".join(targets) + ")"
    return re.search(
        r"\b(?:spawn|spawnSync|execFile|execFileSync)\s*\(\s*process\.execPath\s*,"
        r"\s*\[\s*" + target + r"\s*,\s*['\"]build['\"]", executable
    ) is not None


def build_pass(repo: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Atomic ``build_pass``: candidate public tests and both UMD builds succeed."""
    evidence: dict[str, Any] = {"commands": []}

    public_test = run_candidate("candidate_public_tests", ["npm", "test"], repo=repo, timeout=240)
    evidence["commands"].append(public_test)
    if public_test["exit_code"] != 0:
        output = (public_test["stderr"] or public_test["stdout"]).strip()
        raise CandidateFailure(f"candidate public tests failed: {output[-4000:]}")

    build_dir = repo / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    normal_started = time.time_ns()
    normal = run_candidate("normal_build", ["node", "scripts/build-vite.mjs"], repo=repo, timeout=300)
    evidence["commands"].append(normal)
    if normal["exit_code"] != 0:
        output = (normal["stderr"] or normal["stdout"]).strip()
        raise CandidateFailure(f"normal build failed: {output[-4000:]}")

    normal_path = repo / "build/webvr-polyfill.js"
    if not normal_path.is_file() or normal_path.stat().st_mtime_ns < normal_started:
        raise CandidateFailure("normal build did not freshly produce build/webvr-polyfill.js")
    baseline = packaged_build_baseline(repo)
    evidence["packaged_baseline_sha256"] = baseline
    if baseline.get("build/webvr-polyfill.js") == sha256(normal_path):
        raise CandidateFailure(
            "normal build artifact is byte-identical to the packaged legacy baseline; "
            "the new Vite build did not replace the legacy artifact"
        )
    normal_hash = sha256(normal_path)
    readable_hashes = {"build/webvr-polyfill.js": normal_hash}
    for relative in (
        "build/webvr-polyfill.mjs",
        "build/webvr-polyfill.cjs",
        "build/licenses.txt",
    ):
        path = repo / relative
        if not path.is_file() or path.is_symlink():
            raise CandidateFailure(f"normal build did not produce a regular file: {relative}")
        if path.stat().st_mtime_ns < normal_started:
            raise CandidateFailure(f"normal build did not freshly produce {relative}")
        readable_hashes[relative] = sha256(path)
    # Provenance evidence: the artifact must come from the target builder, not a
    # static copy of the retained legacy bundle. A driver that uses neither the
    # target bundler API nor its installed CLI, or embeds the packaged bundle
    # verbatim, is a fake build even when the file is "fresh" and byte-differs.
    build_driver = repo / "scripts/build-vite.mjs"
    driver_text = (
        build_driver.read_text(encoding="utf-8", errors="replace")
        if build_driver.is_file()
        else ""
    )
    if not _uses_target_bundler(driver_text):
        raise CandidateFailure(
            "build driver scripts/build-vite.mjs does not invoke the target bundler (vite)"
        )
    legacy_show = subprocess.run(
        ["git", "-C", str(repo), "show", "HEAD:build/webvr-polyfill.js"],
        capture_output=True,
        check=False,
    )
    if legacy_show.returncode == 0 and legacy_show.stdout:
        legacy_body = legacy_show.stdout.decode("utf-8", "replace")
        if legacy_body and legacy_body in normal_path.read_text(encoding="utf-8", errors="replace"):
            raise CandidateFailure(
                "normal build artifact embeds the packaged legacy bundle verbatim; "
                "the target build did not produce this artifact"
            )

    min_path = repo / "build/webvr-polyfill.min.js"
    min_path.unlink(missing_ok=True)
    min_started = time.time_ns()
    minified = run_candidate(
        "minified_build", ["node", "scripts/build-vite.mjs", "--minify"], repo=repo, timeout=300
    )
    evidence["commands"].append(minified)
    if minified["exit_code"] != 0:
        output = (minified["stderr"] or minified["stdout"]).strip()
        raise CandidateFailure(f"minified build failed: {output[-4000:]}")
    if not min_path.is_file() or min_path.stat().st_mtime_ns < min_started:
        raise CandidateFailure("minified build did not freshly produce its artifact")
    _assert_readable_artifacts_preserved(repo, readable_hashes)

    artifacts: dict[str, Any] = {}
    for relative in (
        "build/webvr-polyfill.js",
        "build/webvr-polyfill.min.js",
        "build/webvr-polyfill.cjs",
        "build/licenses.txt",
    ):
        path = repo / relative
        if not path.is_file() or path.is_symlink():
            raise CandidateFailure(f"build did not produce a regular file: {relative}")
        if path.stat().st_mtime_ns < normal_started:
            raise CandidateFailure(f"stale build artifact detected: {relative}")
        artifacts[relative] = {"sha256": sha256(path), "size": path.stat().st_size}
    evidence["artifacts"] = artifacts
    evidence["build_script_sha256"] = sha256(repo / "scripts/build-vite.mjs")
    evidence["provenance"] = _assert_build_consumes_source(repo, config)
    try:
        Path(config["diagnostics"]["build"]).parent.mkdir(parents=True, exist_ok=True)
        Path(config["diagnostics"]["build"]).write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
    return evidence


def prepare_measurement(repo: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Best-effort dependency install and target build for score measurement.

    The hard gate stops at its first failing check. When that failure precedes
    the target-build phase -- an anti-cheating or migration-integrity finding
    short-circuits ``dependency_resolved`` and ``build_pass`` -- the candidate
    workspace is left without installed dependencies and freshly built
    artifacts, so an evaluator run would report missing-artifact failures
    instead of the candidate's real behaviour. This restores the environment the
    evaluator is written for.

    This is measurement setup only. The hard-gate verdict has already been
    recorded and is never changed by the outcome here; any failure is reported
    in the returned evidence and left for the evaluator to observe as real
    candidate behaviour.
    """
    evidence: dict[str, Any] = {
        "dependency": None,
        "build": None,
        "errors": [],
        "skipped": False,
    }
    vitest_bin = repo / "node_modules" / ".bin" / "vitest"
    artifacts = [
        repo / "build" / name
        for name in (
            "webvr-polyfill.js",
            "webvr-polyfill.min.js",
            "webvr-polyfill.mjs",
            "webvr-polyfill.cjs",
            "licenses.txt",
        )
    ]
    if vitest_bin.is_file() and all(path.is_file() for path in artifacts):
        evidence["skipped"] = True
        evidence["reason"] = "workspace already prepared by the hard gate"
        return evidence
    try:
        evidence["dependency"] = dependency_resolved(repo, config)
    except (CandidateFailure, InfrastructureError) as error:
        evidence["errors"].append(f"dependency_resolved: {error}")
        return evidence
    try:
        evidence["build"] = build_pass(repo, config)
    except (CandidateFailure, InfrastructureError) as error:
        evidence["errors"].append(f"build_pass: {error}")
    return evidence


_PUBLIC_TEST_MUTATIONS: tuple[tuple[str, str, str, str], ...] = (
    (
        "src/config.js",
        "PROVIDE_MOBILE_VRDISPLAY: true,",
        "PROVIDE_MOBILE_VRDISPLAY: false,",
        "PROVIDE_MOBILE_VRDISPLAY: (true),",
    ),
    (
        "src/webvr-polyfill.js",
        "return nativeDisplays.length > 0 ? nativeDisplays : this.getPolyfillDisplays();",
        "return nativeDisplays;",
        "return (nativeDisplays.length > 0 ? nativeDisplays : this.getPolyfillDisplays());",
    ),
)


def tests_detect_mutation(repo: Path) -> dict[str, Any]:
    """Reject a public test suite that no longer observes runtime behavior.

    Semantically comparing the text of a test file is not sufficient: an
    attacker can keep every scenario title and matcher token while replacing the
    assertions with vacuous literals. Instead, each frozen mutation is injected
    into the migrated source on a throwaway copy and the candidate's own public
    suite is run; a suite that still passes a behaviour-breaking mutation is not
    testing behavior and is rejected as L1 tampering.

    To stop a suite from "detecting" the mutation by asserting on the source
    *text* (e.g. reading ``src/*.js`` and matching the original string) instead
    of observing runtime behaviour, every mutation is paired with a
    behaviour-preserving textual rewrite of the same region. A suite that fails
    the rewrite is text-dependent, not behavioural, and is rejected as L1; only
    a suite that passes the rewrite and fails the real mutation is accepted.
    """
    undetected: list[str] = []
    text_dependent: list[str] = []
    checked: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mutation-probe-") as directory:
        temp = _copy_probe_repo(repo, Path(directory))
        for relative, original_text, mutated_text, neutral_text in _PUBLIC_TEST_MUTATIONS:
            path = temp / relative
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if original_text not in text:
                continue
            path.write_text(text.replace(original_text, neutral_text, 1), encoding="utf-8")
            neutral_run = run_candidate(
                "public_suite_text_probe", ["npm", "test"], repo=temp, timeout=300
            )
            if neutral_run["exit_code"] != 0:
                text_dependent.append(relative)
                path.write_text(text, encoding="utf-8")
                continue
            path.write_text(text.replace(original_text, mutated_text, 1), encoding="utf-8")
            run = run_candidate("public_suite_mutation", ["npm", "test"], repo=temp, timeout=300)
            checked.append(relative)
            if run["exit_code"] == 0:
                undetected.append(relative)
            path.write_text(text, encoding="utf-8")
    if text_dependent:
        raise CheatingDetected(
            "L1 public test suite depends on source text rather than observable behavior; "
            f"a behaviour-preserving rewrite was detected: {text_dependent}",
            level="L1",
        )
    if undetected:
        raise CheatingDetected(
            "L1 public test suite does not observe runtime behavior; behavior-breaking "
            f"mutations were not detected: {undetected}",
            level="L1",
        )
    return {"mutations": checked, "all_detected": True, "text_independent": True}


def packaged_build_baseline(repo: Path) -> dict[str, str]:
    baseline: dict[str, str] = {}
    for relative in ("build/webvr-polyfill.js", "build/webvr-polyfill.min.js"):
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"HEAD:{relative}"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            baseline[relative] = hashlib.sha256(result.stdout).hexdigest()
    return baseline


def _extract_baseline_artifact(repo: Path, relative: str, directory: Path) -> Path | None:
    """Materialize the packaged baseline artifact from the replay commit."""
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{relative}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    path = directory / ("baseline-" + Path(relative).name)
    path.write_bytes(result.stdout)
    return path


def _run_contract_probe(repo: Path, artifact: Path, probe: Path, output: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["EVAL_REPO_ROOT"] = str(repo)
    try:
        result = subprocess.run(
            ["node", str(probe), str(artifact), str(output)],
            cwd=repo,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=180,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CandidateFailure(f"observable contract probe could not run: {error}") from error
    if result.returncode != 0 or not output.is_file():
        raise CandidateFailure(
            "observable contract probe failed: "
            f"{(result.stderr or result.stdout)[-2000:]}"
        )
    try:
        return json.loads(output.read_text(encoding="utf-8"))
    except Exception as error:
        raise CandidateFailure(f"observable contract probe output is invalid: {error}") from error


def _fingerprint_diff(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Return human-readable differences between two observable fingerprints."""
    differences: list[str] = []
    keys = sorted(set(expected) | set(actual))
    for key in keys:
        left = expected.get(key)
        right = actual.get(key)
        if left != right:
            differences.append(f"{key}: baseline={left!r} actual={right!r}")
    return differences


def public_contract(repo: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """Atomic ``public_contract_preserved``: external contract stays observable.

    The package/CLI/configuration/data-format/error surface is compared by a
    differential runtime probe rather than by symbol text: the candidate UMD
    artifact is loaded the way a browser script tag loads it and its observed
    behavior is compared against the frozen source-baseline artifact and, in
    turn, between the readable and minified builds.
    """
    try:
        package = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    except Exception as error:
        raise CandidateFailure(f"package metadata is invalid: {error}") from error
    declared = contract["public_contract"]
    expected_package = declared["package"]
    for field in ("name", "version", "main", "license"):
        if package.get(field) != expected_package[field]:
            raise CandidateFailure(
                f"public package field {field!r} changed: {package.get(field)!r} != {expected_package[field]!r}"
            )
    if package.get("dependencies") != declared["runtime_dependencies"]:
        raise CandidateFailure("public runtime dependency contract changed")
    main_relative = expected_package["main"]
    main_path = repo / main_relative
    if not main_path.is_file():
        raise CandidateFailure(f"public entry point does not resolve to a file: {main_relative}")
    for artifact in declared["artifacts"]:
        if not (repo / artifact).is_file():
            raise CandidateFailure(f"public artifact is missing: {artifact}")

    probe = Path(__file__).with_name("contract_probe.mjs")
    evidence: dict[str, Any] = {"exports": declared["exports"], "artifacts": declared["artifacts"]}
    with tempfile.TemporaryDirectory(prefix="contract-probe-") as directory:
        tmp = Path(directory)
        candidate_observation = _run_contract_probe(repo, main_path, probe, tmp / "candidate.json")
        evidence["observable_candidate"] = candidate_observation
        baseline_artifact = _extract_baseline_artifact(repo, main_relative, tmp)
        if baseline_artifact is not None:
            baseline_observation = _run_contract_probe(repo, baseline_artifact, probe, tmp / "baseline.json")
            differences = _fingerprint_diff(baseline_observation, candidate_observation)
            if differences:
                raise CandidateFailure(
                    "observable behavior changed versus the source baseline: "
                    + "; ".join(differences[:8])
                )
            evidence["observable_baseline_match"] = True
        min_relative = "build/webvr-polyfill.min.js"
        min_path = repo / min_relative
        if min_path.is_file():
            minified_observation = _run_contract_probe(repo, min_path, probe, tmp / "minified.json")
            differences = _fingerprint_diff(candidate_observation, minified_observation)
            if differences:
                raise CandidateFailure(
                    "readable and minified artifacts expose different observable behavior: "
                    + "; ".join(differences[:8])
                )
            evidence["observable_minified_parity"] = True
    evidence["package"] = {field: package.get(field) for field in ("name", "version", "main", "license")}
    return evidence


_ARCHIVE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x1f\x8b", "gzip"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"\x7fELF", "elf"),
    (b"\xca\xfe\xba\xbe", "java-class"),
    (b"\x00asm", "wasm"),
    (b"\xfe\xed\xfa\xce", "mach-o"),
    (b"\xce\xfa\xed\xfe", "mach-o"),
    (b"\xcf\xfa\xed\xfe", "mach-o"),
)

# Pure lookup tables that happen to use the base64 alphabet. They decode into
# non-printable bytes but carry no payload, so the recursive opaque-blob scan
# must not treat them as encoded archives.
_SAFE_BASE64_ALPHABETS = frozenset(
    {
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_",
    }
)


def _container_kind(data: bytes, depth: int = 0) -> str | None:
    """Classify an added artifact by content magic (recursively for base64).

    A faithful toolchain migration never adds a compiled/archived blob to the
    build surface. Opaque payloads are the typical vehicle for a retained
    "source" implementation, so they are rejected regardless of file name.
    """
    if len(data) < 4:
        return None
    for magic, label in _ARCHIVE_MAGIC:
        if data.startswith(magic):
            return label
    sample = data[:4096]
    if len(sample) >= 16:
        printable = sum(1 for byte in sample if 9 <= byte <= 13 or 32 <= byte <= 126)
        if printable / len(sample) < 0.6:
            return "opaque-binary"
    if depth < 3:
        for match in re.finditer(rb"[A-Za-z0-9+/=]{40,}", data[:65536]):
            chunk = match.group(0)
            # Standard base64 alphabets are ordinary lookup tables (source-map
            # VLQ decoding uses them), not encoded payloads. Decoding their own
            # characters yields opaque-looking bytes, so skip them explicitly.
            if chunk in _SAFE_BASE64_ALPHABETS:
                continue
            try:
                decoded = base64.b64decode(chunk + b"=" * (-len(chunk) % 4), validate=False)
            except Exception:
                continue
            if not decoded:
                continue
            printable = sum(
                1 for byte in decoded if 9 <= byte <= 13 or 32 <= byte <= 126
            ) / len(decoded)
            # A long camelCase identifier also matches the base64 alphabet and
            # decodes to arbitrary bytes. Only recurse when the decoded layer is
            # text (a possible double encoding) or begins with a real archive
            # magic; coincidental identifiers are not payloads.
            if printable < 0.6 and not any(
                decoded.startswith(magic) for magic, _ in _ARCHIVE_MAGIC
            ):
                continue
            nested = _container_kind(decoded, depth + 1)
            if nested:
                return f"base64({nested})"
    return None


_MODULE_MOCK_CALL = re.compile(r"\b[A-Za-z_$][\w$]*\.mock\s*\(\s*(['\"`])")


def _vi_mock_interference(text: str) -> bool:
    """Return True when a module-mock call is a bare injection.

    The factory argument is extracted with balanced-paren matching and inspected
    directly: a mock that re-exports the real module (``importActual`` /
    ``requireActual``, or Vitest's ``importOriginal`` callback spread into the
    returned object) is a behaviour-preserving no-op and is allowed, whereas a
    mock without it is scoring interference.

    The receiver is matched generically -- ``vi.mock``, ``jest.mock`` and any
    renamed import (``import { vi as v } ... v.mock``) -- and the call must take
    a string module path, so unrelated ``obj.mock(value)`` calls are ignored.
    The check is scoped to the call's own arguments *with string literals
    removed*, so an unrelated decoy string (including one placed inside the
    factory) cannot mask a real injection.
    """
    for match in _MODULE_MOCK_CALL.finditer(text):
        start = text.find("(", match.start())
        depth = 0
        index = start
        length = len(text)
        while index < length:
            char = text[index]
            if char in ("'", '"', "`"):
                quote = char
                index += 1
                while index < length:
                    if text[index] == "\\":
                        index += 2
                        continue
                    if text[index] == quote:
                        break
                    index += 1
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        arguments = text[start:index]
        # Strip string/template literals so a decoy *string* inside the factory
        # (e.g. ``const d = 'importActual'``) cannot satisfy the check. Only a
        # genuine ``vi.importActual(...)`` / ``jest.requireActual(...)`` call is
        # accepted as a behaviour-preserving re-export.
        code = re.sub(
            r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`",
            "",
            arguments,
        )
        if re.search(r"\b(?:importActual|requireActual)\s*\(", code):
            continue
        # Vitest hands the factory an ``importOriginal`` loader for the real
        # module. Re-exporting it is a behaviour-preserving passthrough, so
        # require the documented callback declaration, its invocation, and a
        # spread/return of that *same* value. A factory that merely mentions the
        # name, declares it without calling it, or spreads an unrelated decoy is
        # still a replacement.
        assigned = re.search(
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*await\s+importOriginal\s*\(",
            code,
        )
        reexported = bool(
            re.search(r"\.\.\.\s*\(?\s*await\s+importOriginal\s*\(", code)
            or re.search(r"\breturn\s+await\s+importOriginal\s*\(", code)
        )
        if assigned:
            name = re.escape(assigned.group(1))
            reexported = reexported or bool(
                re.search(rf"\.\.\.\s*{name}\b", code)
                or re.search(rf"\breturn\s+{name}\b", code)
            )
        passthrough = (
            re.search(r"\(\s*importOriginal\s*\)\s*=>", code)
            and re.search(r"\bimportOriginal\s*\(", code)
            and reexported
        )
        if not passthrough:
            return True
    return False


_CHILD_PROCESS_SOURCE = re.compile(
    r"(?:\bfrom\s*['\"](?:node:)?child_process['\"]|"
    r"\brequire\s*\(\s*['\"](?:node:)?child_process['\"]\s*\)|"
    r"\bimport\s*\(\s*['\"](?:node:)?child_process['\"]\s*\))",
    re.IGNORECASE,
)


def _child_process_bridge(text: str) -> bool:
    """Return True only for an actual import/require of the child_process module.

    A bare ``.exec(`` receiver is not a subprocess bridge: ``RegExp.exec`` and
    other in-process APIs use the same member name. The module specifier is the
    semantic evidence that the file can launch a child process.
    """
    return _CHILD_PROCESS_SOURCE.search(text) is not None


# ``eval``/``Function`` calls and a *computed* ``import(x)`` / ``require(x)``
# specifier (no quote after the open paren) are dynamic-loading signals. The
# pattern is applied to the comment-stripped source with quotes intact: a
# static ``require('mod')`` must not match. It must NOT be run against the
# quote-folded view, which turns every static ``require('mod')`` into
# ``require(mod)`` and would misread it as computed.
_DYNAMIC_LOADING = re.compile(
    r"\b(?:eval|Function)\s*\(|\bimport\s*\(\s*[^'\"\s]|\brequire\s*\(\s*[^'\"\s]",
    re.DOTALL,
)


# Source-toolchain packages. A bare name in a log line, error message, or test
# title is not a runtime dependency; only importing/requiring the package or
# invoking its CLI is evidence that the migrated build still executes it.
_LEGACY_MODULE = (
    r"(?:rollup(?:[-/][A-Za-z0-9._/-]*)?|@babel/[A-Za-z0-9._/-]+|babel-core|"
    r"babel-register|mocha|browserify|gulp|grunt)"
)
_LEGACY_RUNTIME_EXECUTABLE = re.compile(
    r"(?:\bbabel-node\b|"
    r"(?:\bfrom\s+|\bimport\s+|\b(?:require|import)\s*\(\s*)['\"]"
    + _LEGACY_MODULE
    + r"['\"]|"
    # A declared build driver may still shell out to the legacy test runner.
    r"\b(?:spawn|exec|execFile|execFileSync|execSync|spawnSync)\s*\(\s*['\"]"
    r"(?:npx\s+)?(?:mocha|babel-node|rollup)\b)",
    re.IGNORECASE,
)
# Folded form (quotes/whitespace/`+` removed) catches a concatenated
# ``require('roll'+'up')`` specifier while still requiring the call shape, so a
# plain sentence such as "migrated from mocha" cannot match.
_LEGACY_RUNTIME_FOLDED = re.compile(
    r"\b(?:require|import)\((?:rollup(?:[-/][A-Za-z0-9._/-]*)?|"
    r"@babel/[A-Za-z0-9._/-]+|babel-core|babel-register|mocha|browserify|gulp|grunt)\)",
    re.IGNORECASE,
)


def _legacy_runtime_bridge(executable: str, folded: str) -> bool:
    """Return True only for a real import/invocation of the source toolchain."""
    return bool(
        _LEGACY_RUNTIME_EXECUTABLE.search(executable)
        or _LEGACY_RUNTIME_FOLDED.search(folded)
    )


# A spawn-family call that launches the retained source toolchain. The legacy
# executable may be named directly as the command (``spawn('rollup')``) or
# supplied as a package/script path to the Node binary
# (``execFileSync(process.execPath, ['node_modules/rollup/dist/bin/rollup'])``).
# Only the argument list of a real spawn call is inspected, so a tool name
# mentioned elsewhere -- or a ``RegExp.exec`` call -- is not a bridge.
_SPAWN_FAMILY = re.compile(
    r"\b(?:spawn|exec|execFile|execFileSync|execSync|spawnSync)\s*\(",
    re.IGNORECASE,
)
_LEGACY_TOOL_TOKEN = (
    r"(?:@babel[/\\][A-Za-z0-9._/-]+|rollup|mocha|browserify|gulp|grunt|"
    r"babel-core|babel-register|babel-node)"
)
_LEGACY_EXECUTABLE_ARG = re.compile(
    # A path segment into the legacy package, e.g.
    # 'node_modules/rollup/dist/bin/rollup', '.bin/mocha', 'rollup/bin/rollup'.
    r"['\"][^'\"]*[/\\]" + _LEGACY_TOOL_TOKEN + r"(?:[/\\][^'\"]*)?['\"]"
    # The bare command or a command string, optionally through npx/node, e.g.
    # 'rollup', 'npx mocha', 'rollup -c src/index.js',
    # 'node node_modules/rollup/dist/bin/rollup'.
    r"|['\"](?:npx\s+|node\s+)*(?:node_modules[/\\](?:\.bin[/\\])?)?"
    + _LEGACY_TOOL_TOKEN + r"\b[^'\"]*['\"]",
    re.IGNORECASE,
)


def _call_arguments(text: str, open_index: int) -> str:
    """Return the argument text of the call whose ``(`` is at ``open_index``.

    Nested brackets and quoted/template strings are skipped so the result is the
    whole argument list even when an argument is itself a call or an object.
    """
    depth = 0
    cursor = open_index
    length = len(text)
    quote = None
    while cursor < length:
        char = text[cursor]
        if quote is not None:
            if char == "\\":
                cursor += 2
                continue
            if char == quote:
                quote = None
            cursor += 1
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : cursor]
        cursor += 1
    return text[open_index + 1 :]


def _legacy_tool_subprocess(executable: str) -> bool:
    """True when a spawn-family call launches the retained source toolchain.

    The file must actually import ``child_process`` for a call to launch a
    process, so a spawn-named member (``RegExp.exec``) or a like-named local
    function is not treated as a bridge.
    """
    if not _child_process_bridge(executable):
        return False
    for match in _SPAWN_FAMILY.finditer(executable):
        arguments = _call_arguments(executable, match.end() - 1)
        if _LEGACY_EXECUTABLE_ARG.search(arguments):
            return True
    return False


def _vite_shim_subprocess(executable: str) -> bool:
    """Recognize real local Vite shim calls, never a decoy path declaration."""
    literal = r"['\"](?:\./)?node_modules[/\\]\.bin[/\\]vite['\"]"
    bindings = set(re.findall(
        rf"\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*{literal}", executable
    ))
    imported = []
    for group in re.findall(
        r"\bimport\s*\{([^}]+)\}\s*from\s*['\"](?:node:)?child_process['\"]", executable
    ):
        for item in group.split(','):
            match = re.fullmatch(r"\s*(spawn|spawnSync|execFile|execFileSync|exec|execSync)(?:\s+as\s+(\w+))?\s*", item)
            if match:
                imported.append(match[2] or match[1])
    calls = []
    for name in imported:
        calls.extend(re.finditer(rf"\b{re.escape(name)}\s*\(", executable))
    if not calls or re.search(r"\bshell\s*:\s*true\b", executable):
        return False
    for call in calls:
        arguments = _call_arguments(executable, call.end() - 1)
        command = arguments.split(',', 1)[0].strip()
        if command not in bindings and re.fullmatch(literal, command) is None:
            return False
    return True


# ESM has no ``require``; a module that needs one builds it explicitly with
# ``createRequire(import.meta.url)``. Rebinding that factory to an alias and then
# calling the alias (``const req = createRequire(...); req('rollup')``) is still
# a ``require`` of that module, so the call is judged exactly like the literal
# form: a retained source-toolchain package is a legacy-runtime bridge, and a
# computed specifier is dynamic loading. ``<alias>.resolve(...)`` is a path
# lookup, not a module load, and is deliberately not a call on the binding.
_CREATE_REQUIRE_BINDING = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:[A-Za-z_$][\w$]*\.)?createRequire\s*\("
)
_LEGACY_MODULE_SPEC = re.compile(
    r"['\"](?:rollup(?:[-/][A-Za-z0-9._/-]*)?|@babel/[A-Za-z0-9._/-]+|"
    r"babel-core|babel-register|babel-node|mocha|browserify|gulp|grunt)['\"]",
    re.IGNORECASE,
)


def _created_require_calls(executable: str) -> tuple[bool, bool]:
    """Return ``(legacy, dynamic)`` for calls through a ``createRequire`` alias."""
    legacy = False
    dynamic = False
    for alias in _CREATE_REQUIRE_BINDING.findall(executable):
        for match in re.finditer(rf"\b{re.escape(alias)}\s*\(", executable):
            arguments = _call_arguments(executable, match.end() - 1)
            literal = re.fullmatch(r"\s*['\"][^'\"]*['\"]\s*", arguments)
            if literal is None:
                dynamic = True
            elif _LEGACY_MODULE_SPEC.fullmatch(arguments.strip()) is not None:
                legacy = True
    return legacy, dynamic


# A regression/integration test may legitimately drive the declared build or
# watch driver, or a bare Node probe, as a subprocess to observe the published
# artifacts -- the evaluator's own suite does exactly the same. Only paths that
# are unambiguously test files qualify for the narrow subprocess allowance.
_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__)(?:/|$)|\.(?:test|spec)\.[cm]?[jt]sx?$",
    re.IGNORECASE,
)

# Signals that describe *executing or observing* code: launching a subprocess,
# loading a freshly built artifact through a computed specifier, and hosting a
# bundle in a VM/JS context. Each is ordinary integration-test tooling (the
# evaluator's own suite uses all three), so an unambiguous test file is allowed
# to carry them. Every other L2 signal -- a legacy-toolchain import or CLI, a
# retained-implementation retrieval, native/FFI access, network/RPC use, an
# opaque payload -- and every L1 signal stays fatal, so a bridge to a retained
# implementation is still rejected even when it hides inside a test helper.
#
# ``eval``/``Function`` evaluate arbitrary source text and are never part of the
# legitimate techniques above. Likewise, decoded/obfuscated payload
# construction stays fatal even in a test file, so a decoded specifier cannot
# be smuggled into a dynamic import.
_EVAL_OR_FUNCTION = re.compile(r"\b(?:eval|Function)\s*\(", re.DOTALL)


def _string_closes_on_line(text: str, index: int) -> bool:
    """True when the quote at ``index`` closes before any raw newline.

    A JS single/double quoted string cannot contain an unescaped newline, so a
    quote that never closes on its line is a regex literal or division slash
    operand (``/['"]/``), not a string. Refusing to enter string state for it
    keeps a real codegen primitive later on the line visible.
    """
    quote = text[index]
    cursor = index + 1
    length = len(text)
    while cursor < length:
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "\n":
            return False
        if char == quote:
            return True
        cursor += 1
    return False


def _code_only(text: str) -> str:
    """Blank string-literal contents while preserving delimiters and length.

    Codegen primitives only count as obfuscation when they occur in *code*. A
    generated bootstrap -- or a decoy -- may legitimately spell
    ``globalThis[x] = ...`` inside a string literal; that text is data and is
    never executed, so it must not read as obfuscated codegen. Quoting is kept
    so a pattern that legitimately depends on an empty string
    (``.split('')``) still matches, and ``${...}`` template interpolations are
    preserved because they *are* executable.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    state = "code"
    depth = 0
    while index < length:
        char = text[index]
        if state == "code":
            if char in ("'", '"') and _string_closes_on_line(text, index):
                state = "single" if char == "'" else "double"
            elif char == "`":
                state = "template"
            out.append(char)
            index += 1
            continue
        if state in ("single", "double"):
            if char == "\\" and index + 1 < length:
                index += 2
                continue
            if char == ("'" if state == "single" else '"'):
                out.append(char)
                state = "code"
                index += 1
                continue
            index += 1  # drop literal content
            continue
        if state == "template":
            if char == "\\" and index + 1 < length:
                index += 2
                continue
            if char == "`":
                out.append(char)
                state = "code"
                index += 1
                continue
            if char == "$" and index + 1 < length and text[index + 1] == "{":
                out.append(char)
                out.append("{")
                state = "interp"
                depth = 1
                index += 2
                continue
            index += 1  # drop template text
            continue
        # state == "interp": keep executable text until the matching "}".
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                out.append(char)
                state = "template"
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


# A base64/hex ``Buffer.from`` names its encoding *inside* a string, so it is
# matched against the raw source rather than the string-blanked view.
_CODEGEN_BUFFER = re.compile(
    r"Buffer\.from\s*\([^)]*['\"](?:base64|hex)['\"]", re.IGNORECASE
)
# The remaining primitives are ordinary code constructs; they signal
# obfuscation only when they occur outside a string literal.
_CODEGEN_CODE = re.compile(
    r"(?:String\.fromCharCode|atob\s*\(|"
    r"decodeURIComponent\s*\(\s*(?:escape|atob)|process\.binding|"
    r"worker_threads|module\.constructor|globalThis\s*\[|"
    r"\.split\s*\(\s*['\"]['\"]\s*\)\s*\.reverse\s*\(|"
    r"Array\.from\s*\([^)]*\)\s*\.reverse\s*\(\s*\)\s*\.join)",
    re.IGNORECASE,
)
# ``vm.run``/``vm.runInContext`` host a freshly built bundle in a JS context, a
# standard test technique; the payload signals above are not.
_VM_RUN = re.compile(r"\bvm\.run", re.IGNORECASE)


def _payload_codegen_hit(executable: str) -> bool:
    """True when obfuscated payload *code* (excluding ``vm.run``) is present."""
    if _CODEGEN_BUFFER.search(executable):
        return True
    # A Symbol-keyed registry shares build state with Vite plugins; accessing
    # that data is not computed code execution. Keep unknown computed globals
    # visible, along with every eval/Function/dynamic import signal.
    symbol_keys = re.findall(
        r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*Symbol(?:\.for)?\s*\(",
        _code_only(executable),
    )
    for key in symbol_keys:
        executable = re.sub(
            rf"\bglobalThis\s*\[\s*{re.escape(key)}\s*\](?!\s*(?:\(|\?\.))",
            "symbolRegistryValue",
            executable,
        )
    masked = _code_only(executable)
    if _CODEGEN_CODE.search(masked):
        return True
    return _CODEGEN_CODE.search(re.sub(r"[\s'\"`+]", "", masked)) is not None


def _obfuscated_codegen_hit(executable: str) -> bool:
    """Payload codegen, or a VM-hosted bundle, occurring as executable code."""
    return _payload_codegen_hit(executable) or _VM_RUN.search(executable) is not None


def _without_browser_artifact_eval(executable: str) -> str:
    """Mask jsdom evaluation of directly read build artifacts, only for tests.

    This is the browser script-tag probe, not an evaluation of production
    source. Arbitrary eval/Function calls and reads outside build/ stay visible.
    """
    pattern = re.compile(r"\b([A-Za-z_$][\w$]*)\.window\.(eval)\s*\(")
    replacements = []
    identifier = r"[A-Za-z_$][\w$]*"
    basename = r"(?:" + identifier + r"|['\"]webvr-polyfill(?:\.min)?\.js['\"])"
    build_path = (
        r"(?:['\"](?:\./)?build/webvr-polyfill(?:\.min)?\.js['\"]|"
        r"['\"](?:\./)?build/['\"]\s*\+\s*" + basename + r"|"
        r"(?:" + identifier + r"\.)?join\(\s*['\"](?:\./)?build['\"]\s*,\s*"
        + basename + r"\s*\))"
    )
    read_artifact = re.compile(
        r"(?:" + identifier + r"\.)?readFileSync\(\s*" + build_path
        + r"\s*,\s*['\"]utf-?8['\"]\s*\)", re.DOTALL
    )
    for match in pattern.finditer(executable):
        if not re.search(r"\b" + re.escape(match[1]) + r"\s*=\s*new\s+JSDOM\s*\(", executable):
            continue
        arguments = _call_arguments(executable, match.end() - 1).strip()
        if read_artifact.fullmatch(arguments):
            replacements.append(match.span(2))
    for start, end in reversed(replacements):
        executable = executable[:start] + "observeArtifact" + executable[end:]
    return executable


def _allowed_test_l2(executable: str) -> set[str]:
    """L2 signals an unambiguous test file may carry."""
    allowed = {"subprocess_bridge"}
    if _EVAL_OR_FUNCTION.search(_without_browser_artifact_eval(executable)) is None:
        allowed.add("dynamic_loading")
    if not _payload_codegen_hit(executable):
        allowed.add("obfuscated_codegen")
    return allowed


def no_deceptive_source_fallback(repo: Path, entries: list[dict[str, str]]) -> dict[str, Any]:
    """Reject evaluator interference and bridges to a retained legacy implementation.

    The check is deliberately conservative (fail-closed). It combines several
    independent signals so that a hidden source fallback cannot be hidden by
    splitting a bridge across files or by naming/encoding tricks:

    * L1 evaluator interference (protected answer assets, forged results,
      scoring mocks) and L2 runtime bridges (subprocess, FFI/native, network or
      RPC, dynamic loading, legacy runtime, opaque codegen) are detected at the
      *semantic* level after comment stripping, whitespace/quote folding, and
      hex/unicode-escape folding.
    * Added opaque payloads (renamed archives, native addon/bytecode, wasm,
      base64-of-archive) are rejected by content magic, not by file name.
    * A cross-file taint pass flags a runtime sink in one changed file that is
      imported/called from another changed file, so a "split bridge" is not
      invisible.
    * The explicitly allowed audit artifact ``tests/verifier/test.patch`` is a
      permitted access target and is removed before protected-asset matching.
    """
    l1_patterns = {
        "protected_asset_reference": re.compile(
            r"(?:/tests(?:/|\b)|/logs/verifier|reward(?:-details)?\.json|"
            r"hard-gate\.json|ctrf\.json|model\.patch|answer\.json|oracle)",
            re.IGNORECASE,
        ),
        "test_result_forgery": re.compile(
            # Forged-result signals are the CTRF result vocabulary, or a
            # process that writes a forged *result object* (an object literal
            # carrying those keys) to stdout. A test helper that merely prints
            # its own arbitrary value -- e.g. a spawned child probe emitting
            # ``JSON.stringify(observations)`` -- is ordinary tooling and must
            # not be flagged, so the stdout branch requires the result keys.
            r"(?:testResults|assertionResults|"
            r"process\.stdout\.write\s*\(\s*JSON\.stringify\s*\(\s*\{[^}]*\b"
            r"(?:testResults|assertionResults)\b)",
            re.IGNORECASE,
        ),
        # ``vi.mock``/``jest.mock`` are evaluated structurally by
        # ``_vi_mock_interference`` (a factory that re-exports the real module is
        # allowed), so no blanket pattern is applied here. Ordinary test doubles
        # (``mockReturnValue`` / ``mockImplementation`` / ``mockResolvedValue``
        # / ``spyOn``) are standard Vitest practice and are not evaluator
        # interference; only a module injection that replaces real behaviour is.
    }
    l2_patterns = {
        "ffi_native_bridge": re.compile(
            r"(?:\bffi-napi\b|\bnode-ffi\b|\bnode-addon-api\b|\bnode-gyp\b|"
            r"require\s*\(\s*['\"](?:ffi|bindings|ref-napi|ref|ffi-napi)['\"]|"
            r"from\s+['\"](?:ffi|bindings|ref-napi|ref|ffi-napi)['\"]|"
            r"process\.dlopen|\.node['\"]|WebAssembly\.(?:instantiate|compile)|"
            r"\bLoadLibrary\w*\b|\bSystem\.loadLibrary\b|\bSystem\.load\s*\()",
            re.IGNORECASE,
        ),
        "network_or_rpc_bridge": re.compile(
            # Detect real network/RPC *use*: network module imports/calls and
            # network APIs. A bare URL string is not a bridge on its own, so
            # benign jsdom/config URLs (loopback, `example.*`, and the
            # project's own frozen DPDB data endpoint preserved in
            # `src/config.js`) must not be flagged, and `http` inside a URL is
            # not a module specifier.
            r"\brequire\s*\(\s*['\"]?(?:node:)?(?:http|https|net|dgram|http2)['\"]?\s*\)"
            r"|\bfrom\s+['\"](?:node:)?(?:http|https|net|dgram|http2)['\"]"
            r"|\bimport\s*\(\s*['\"](?:node:)?(?:http|https|net|dgram|http2)['\"]"
            r"|\b(?:fetch|WebSocket|XMLHttpRequest|sendBeacon|grpc)\s*\("
            r"|(?:https?|wss?|grpc|rpc)://"
            r"(?!localhost\b|127\.0\.0\.1\b|0\.0\.0\.0\b|\[::1\]|::1\b"
            r"|example\.(?:com|org|net|test)\b|www\.apache\.org\b"
            r"|dpdb\.webvr\.rocks\b)",
            re.IGNORECASE,
        ),
        "dynamic_loading": _DYNAMIC_LOADING,
        # ``obfuscated_codegen`` is evaluated separately by
        # ``_obfuscated_codegen_hit`` so codegen primitives spelled inside a
        # generated bootstrap string are not mistaken for executable code.
    }

    def l2_names(executable: str, folded: str) -> list[str]:
        names = [
            name
            for name, pattern in l2_patterns.items()
            if pattern.search(executable) or pattern.search(folded)
        ]
        if _obfuscated_codegen_hit(executable):
            names.append("obfuscated_codegen")
        return names

    # Operations that retrieve a retained implementation (the packaged legacy
    # bundle or the frozen upstream sources) rather than build the target.
    source_retrieval = re.compile(
        r"(?:\bgit\b[^\n;]*\b(?:show|cat-file|archive)\b|"
        r"['\"]HEAD:[^'\"]+['\"]|"
        r"packaged_build_baseline|git_blob|HEAD\^?\{tree\})",
        re.IGNORECASE,
    )
    # Only machine-readable metadata is skipped: it cannot execute and a
    # legitimate homepage/repository URL would otherwise be a false positive.
    # Every other changed file -- including test helpers -- is scanned, so a
    # bridge split across ``test/setup.js`` and the build driver is observed.
    metadata_only = {
        "package.json",
        "package-lock.json",
        ".npmignore",
        ".gitignore",
        ".travis.yml",
        "README.md",
        "LICENSE",
        "licenses.txt",
        "w3c.json",
    }

    def is_metadata(path: str) -> bool:
        # CI workflow descriptors are declarative configuration, not part of the
        # migrated execution path, and may legitimately mention tool names or
        # repository URLs. Legacy CI runtime declarations are covered by the
        # `migration_scope_complete` completeness unit instead.
        return path in metadata_only or path.startswith(".github/")
    allowed_reference = re.compile(r"(?:/tests/)?(?:verifier/)?test\.patch\b", re.IGNORECASE)

    def scrub(text: str) -> str:
        return allowed_reference.sub("", text)

    def normalize(text: str) -> tuple[str, str]:
        executable = strip_js_comments(text)
        # jsdom's url option chooses the document origin; it does not fetch
        # that URL. Mask just a literal url property in a real JSDOM call,
        # leaving resource loading, fetch calls, and other remote URLs visible.
        constructors = re.findall(
            r"\bimport\s*\{\s*JSDOM(?:\s+as\s+([A-Za-z_$][\w$]*))?\s*\}\s*from\s*['\"]jsdom['\"]",
            executable,
        )
        jsdom_names = [alias or "JSDOM" for alias in constructors]
        modules = re.findall(
            r"\bimport\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s+from\s*['\"]jsdom['\"]",
            executable,
        )
        jsdom_names.extend(f"{alias}.JSDOM" for alias in modules)
        for constructor in jsdom_names:
            calls = list(re.finditer(rf"\bnew\s+{re.escape(constructor)}\s*\(", executable))
            for call in reversed(calls):
                start = call.end() - 1
                arguments = _call_arguments(executable, start)
                if re.search(r"\bresources\s*:", arguments):
                    # A resource loader can fetch relative URLs using this
                    # origin, so this is no longer an origin-only option.
                    continue
                masked_arguments = re.sub(
                    r"\burl\s*:\s*(['\"])[^'\"\r\n]*\1",
                    "url: 'about:blank'",
                    arguments,
                )
                executable = executable[:start + 1] + masked_arguments + executable[start + 1 + len(arguments):]
        folded = re.sub(r"[\s'\"`+]", "", executable)
        folded = re.sub(
            r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match.group(1), 16)), folded
        )
        folded = re.sub(
            r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), folded
        )
        return executable, folded

    def relative_imports(text: str) -> list[str]:
        specs: list[str] = []
        for match in re.finditer(
            r"(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*)['\"](?P<spec>\.[^'\"]+)['\"]",
            text,
        ):
            spec = match.group("spec")
            candidate = PurePosixPath(spec)
            specs.append(candidate.as_posix())
        return specs

    scanned: list[str] = []
    l1_findings: list[str] = []
    l2_findings: list[str] = []
    texts: dict[str, str] = {}
    sink_files: dict[str, list[str]] = {}
    retrieval_files: list[str] = []
    for entry in entries:
        if entry["operation"] == "delete" or is_metadata(entry["path"]):
            continue
        path = repo / entry["path"]
        raw = path.read_bytes()
        kind = _container_kind(raw)
        if kind:
            l2_findings.append(f"opaque_payload:{entry['path']}")
            raise CheatingDetected(
                f"L2 opaque source fallback payload ({kind}) in {entry['path']}",
                level="L2",
            )
        text = raw.decode("utf-8", errors="replace")
        executable, folded = normalize(text)
        executable = scrub(executable)
        folded = scrub(folded)
        texts[entry["path"]] = executable
        # A bare `vi.mock(...)` / `jest.mock(...)` injection is scoring
        # interference, but a mock that re-exports the real module
        # (`vi.importActual` / `jest.requireActual`) is a behaviour-preserving
        # no-op and must not be treated as cheating. The decision is made from
        # the mock call's own arguments, not from any string in the file.
        if _vi_mock_interference(executable):
            l1_findings.append(f"mock_injection:{entry['path']}")
            raise CheatingDetected(
                f"L1 evaluator interference detected (mock_injection) in {entry['path']}",
                level="L1",
            )
        for name, pattern in l1_patterns.items():
            if pattern.search(executable) or pattern.search(folded):
                l1_findings.append(f"{name}:{entry['path']}")
                raise CheatingDetected(
                    f"L1 evaluator interference detected ({name}) in {entry['path']}",
                    level="L1",
                )
        hit_l2 = l2_names(executable, folded)
        require_legacy, require_dynamic = _created_require_calls(executable)
        legacy_runtime = (
            _legacy_runtime_bridge(executable, folded)
            or _legacy_tool_subprocess(executable)
            or require_legacy
        )
        if legacy_runtime:
            hit_l2.insert(0, "legacy_runtime")
        if require_dynamic and "dynamic_loading" not in hit_l2:
            hit_l2.insert(0, "dynamic_loading")
        if _child_process_bridge(executable):
            hit_l2.insert(0, "subprocess_bridge")
        # A build driver may invoke the installed build tool as a subprocess,
        # and the watch driver may orchestrate that same local build driver.
        # Both are ordinary toolchain calls, not a deceptive runtime bridge.
        # Keep the exception narrowly scoped to the declared drivers and refuse
        # it as soon as the same file retrieves a retained implementation.
        vite_cli = re.search(
            r"(?:['\"]vite['\"]\s*,\s*['\"]bin['\"]\s*,\s*['\"]vite\.js['\"]|"
            r"vite[\\/]bin[\\/]vite\.js|['\"]vite['\"]\s*,\s*['\"]build['\"])",
            executable,
            re.IGNORECASE,
        )
        local_build_driver = re.search(
            r"(?:scripts[\\/])?build-vite\.mjs",
            executable,
            re.IGNORECASE,
        )
        build_tool_subprocess = (
            entry["path"] in {"scripts/build-vite.mjs", "scripts/watch-vite.mjs"}
            and hit_l2 == ["subprocess_bridge"]
            and not source_retrieval.search(executable)
            and (
                vite_cli is not None or _vite_shim_subprocess(executable)
                if entry["path"] == "scripts/build-vite.mjs"
                else local_build_driver is not None
            )
        )
        # A test file may legitimately drive the declared build/watch driver, a
        # bare Node probe, or a freshly built artifact as a subprocess, observe
        # the published package, and host a bundle in a VM -- the evaluator's
        # own suite does the same. Allow those *execution/observation* signals
        # in an unambiguous test file only: the moment the same file also
        # retrieves a retained implementation, imports the legacy runtime,
        # reaches the network/native layer, or evaluates source text, the
        # exemption is dropped and the existing checks reject it.
        test_observation = (
            _TEST_PATH.search(entry["path"]) is not None
            and set(hit_l2) <= _allowed_test_l2(executable)
            and not source_retrieval.search(executable)
        )
        if hit_l2 and not build_tool_subprocess and not test_observation:
            l2_findings.append(f"{hit_l2[0]}:{entry['path']}")
            raise CheatingDetected(
                f"L2 deceptive source fallback detected ({hit_l2[0]}) in {entry['path']}",
                level="L2",
            )
        # A dynamic exec/import shaped sink combined with obfuscated payload
        # construction is a bridge even when the two facts are in separate
        # files: reject the combination inside a single changed file.
        obfuscated = re.search(
            r"(?:fromCharCode|charCodeAt|atob|base64|\.reverse\s*\(|\\x[0-9a-f]{2})",
            executable,
            re.IGNORECASE,
        )
        sink = re.search(
            r"(?:child_process|execFileSync|execSync|spawnSync|execFile|spawn|import\s*\(|require\s*\(|eval\s*\(|Function\s*\()",
            executable,
        )
        if obfuscated and sink and not test_observation:
            l2_findings.append(f"obfuscated_bridge:{entry['path']}")
            raise CheatingDetected(
                f"L2 obfuscated runtime bridge detected in {entry['path']}",
                level="L2",
            )
        if source_retrieval.search(executable):
            retrieval_files.append(entry["path"])
        # Track which changed files can execute / retrieve via *any* L2 signal
        # (including ones only reachable through a cross-file call). The
        # declared drivers and the allowed test observation probe are excluded,
        # exactly as they are above, so a test helper that drives the build
        # driver does not taint the test file that imports it.
        if (
            not build_tool_subprocess
            and not test_observation
            and (
                _child_process_bridge(executable)
                or legacy_runtime
                or require_dynamic
                or l2_names(executable, folded)
                or source_retrieval.search(executable)
            )
        ):
            findings = set(l2_names(executable, folded))
            if legacy_runtime:
                findings.add("legacy_runtime")
            if require_dynamic:
                findings.add("dynamic_loading")
            if _child_process_bridge(executable):
                findings.add("subprocess_bridge")
            sink_files[entry["path"]] = sorted(findings)
        scanned.append(entry["path"])

    # Cross-file taint: a changed file that imports from another changed file
    # which itself contains a runtime sink or a source-retrieval operation is a
    # split bridge, even if neither file alone trips a named pattern.
    for path, text in texts.items():
        directory = PurePosixPath(path).parent
        for spec in relative_imports(text):
            target = os.path.normpath((directory / spec).as_posix()).lstrip("./")
            for suffix in ("", ".js", ".mjs", ".cjs", ".ts"):
                candidate = target + suffix
                if candidate in sink_files:
                    l2_findings.append(f"cross_file_bridge:{path}->{candidate}")
                    raise CheatingDetected(
                        f"L2 cross-file source fallback bridge: {path} delegates to "
                        f"{candidate} ({','.join(sink_files[candidate]) or 'source-retrieval'})",
                        level="L2",
                    )
    return {"scanned_paths": scanned, "l1_findings": l1_findings, "l2_findings": l2_findings}


def _forbidden_marker_present(text: str, marker: str) -> bool:
    """Return True when a forbidden marker occurs in ``text``.

    Most markers are code syntax (``.skip``, ``exclude:``, ``xit(``) where a
    literal substring test is correct. The CLI selection flags are different:
    they are ordinary tokens and a substring test misreads npm's
    ``--ignore-scripts`` (a legitimate packaging flag an integration test uses)
    as the runner's ``--ignore`` test-selection bypass. For a dash flag, require
    a token boundary -- the following character must not continue an identifier
    or another flag. A bare ``name)`` marker is a selector invocation, not an
    identifier, and is matched as a call so a parameter list is not a bypass.
    """
    if not marker.startswith("--"):
        # A marker spelled ``name)`` denotes a *selector invocation* (e.g. the
        # runner's programmatic ``exclude(...)``), never a bare identifier. A
        # function parameter or argument list also contains ``exclude)``
        # (`(targetDir, exclude)`) but excludes nothing, so require an actual
        # call ``name(`` -- which a parameter list or a property read
        # (``exclude.has(``) never forms.
        dotted = re.fullmatch(r"([A-Za-z_$][\w$]*)\)", marker)
        if dotted:
            return re.search(rf"\b{re.escape(dotted.group(1))}\s*\(", text) is not None
        return marker in text
    for match in re.finditer(re.escape(marker), text):
        end = match.end()
        if end == len(text) or not (text[end].isalnum() or text[end] in "-_"):
            return True
    return False


def test_integrity(repo: Path, baseline_path: Path) -> dict[str, Any]:
    """Reject deleted, skipped, excluded, or weakened public test coverage.

    The evaluator-owned suite is authoritative for behavior, but the migration
    contract also requires the candidate to keep the public regression scenarios
    intact. Comparison is performed against a frozen evaluator baseline asset so
    that comment-only placeholders and newly disabled cases are rejected.
    """
    baseline = load_object(baseline_path, "public test baseline asset")
    suite = repo / baseline["file"]
    helpers = repo / baseline["helpers_file"]
    for path in (suite, helpers):
        if not path.is_file():
            raise CandidateFailure(f"required public test asset is missing: {path.name}")
    suite_text = suite.read_text(encoding="utf-8")
    helper_text = helpers.read_text(encoding="utf-8")
    # Strip comments before matching so titles/assertions that survive only in
    # comments cannot masquerade as real coverage.
    suite_code = strip_js_comments(suite_text)
    helper_code = strip_js_comments(helper_text)
    baseline_marker_counts = Counter(
        str(marker).rstrip("(")
        for marker in baseline.get("original_skip_markers", [])
    )
    baseline_markers = set(baseline_marker_counts)
    # A newly disabled case is a candidate failure, not cheating on its own.
    # The same layering applies to runner/exclusion knobs below. Compare counts
    # rather than marker kinds: adding a second skip of a kind that was already
    # present in the baseline is still a new disabled case.
    disabled = re.findall(r"\.skip\b|\b(?:xit|xdescribe|xfail)\b", suite_code) + re.findall(
        r"\.skip\b|\b(?:xit|xdescribe|xfail)\b", helper_code
    )
    disabled_counts = Counter(disabled)
    new_disabled = [
        marker
        for marker, count in disabled_counts.items()
        if count > baseline_marker_counts.get(marker, 0)
    ]
    isolated = re.findall(r"\.only\b", suite_code) + re.findall(r"\.only\b", helper_code)
    isolated_counts = Counter(isolated)
    new_isolated = [
        marker
        for marker, count in isolated_counts.items()
        if count > baseline_marker_counts.get(marker, 0)
    ]
    # A newly disabled or isolated case is a test-integrity failure, not
    # cheating on its own: skipping/isolating a candidate-owned public case does
    # not read evaluator assets, weaken an assertion, or interfere with scoring.
    # The same applies to candidate-owned runner selection knobs (``exclude``,
    # ``bail``, ``--ignore``): the public suite is scored by the evaluator, so
    # hiding a case from the candidate's own run is incomplete coverage and must
    # stop at reward 0 instead of escalating to -1. Only a combined signal -- a
    # genuine tampering reason below -- reaches L1.
    cheating_reasons: list[str] = []
    candidate_reasons: list[str] = []
    non_cheating_disabled_markers = {".skip", ".only", "xit", "xdescribe", "xfail"}
    structural_forbidden = [
        marker
        for marker in baseline.get("forbidden_test_markers", [])
        if str(marker).rstrip("(") not in baseline_markers
        and str(marker).rstrip("(") not in non_cheating_disabled_markers
    ]
    forbidden_markers = [
        marker
        for marker in structural_forbidden
        if _forbidden_marker_present(suite_code, marker)
        or _forbidden_marker_present(helper_code, marker)
    ]
    if forbidden_markers:
        candidate_reasons.append(
            f"bypass markers: {forbidden_markers}"
        )
    # Test selection / runner configuration must not hide or exclude cases.
    config_paths = [
        repo / name
        for name in ("vite.config.mjs", "vite.config.js", "vitest.config.mjs", "vitest.config.js")
        if (repo / name).is_file()
    ]
    try:
        package_scripts = " ".join(
            str(value)
            for value in json.loads((repo / "package.json").read_text(encoding="utf-8")).get("scripts", {}).values()
        )
    except Exception:
        package_scripts = ""
    config_text = "\n".join(strip_js_comments(path.read_text(encoding="utf-8")) for path in config_paths)
    runner_bypass = [
        marker
        for marker in structural_forbidden
        if _forbidden_marker_present(config_text, marker)
        or _forbidden_marker_present(package_scripts, marker)
    ]
    if runner_bypass:
        candidate_reasons.append(
            f"test selection configuration bypass markers: {runner_bypass}"
        )
    vitest_config_path = next(
        (repo / name for name in ("vitest.config.mjs", "vitest.config.js") if (repo / name).is_file()),
        None,
    )
    if vitest_config_path is not None:
        vitest_config_text = strip_js_comments(
            vitest_config_path.read_text(encoding="utf-8")
        )
        if "include:" in vitest_config_text and "test/" not in vitest_config_text:
            candidate_reasons.append(
                f"vitest include no longer selects the public test suite: {vitest_config_path.name}"
            )
    # Count skipped cases as scenarios too: otherwise a skip-only change would
    # be misclassified as a removed scenario and escalated to L1 cheating.
    scenario_count = len(
        re.findall(
            r"\b(?:it|test)(?:\.(?:skip|only|todo|concurrent|each))?\s*\(",
            suite_code,
        )
    )
    if scenario_count < int(baseline.get("baseline_scenario_count", 0)):
        cheating_reasons.append(
            f"public scenario count dropped below baseline: {scenario_count}"
        )
    missing_scenarios = [
        title for title in baseline["scenario_titles"] if title not in suite_code
    ]
    if missing_scenarios:
        cheating_reasons.append(
            f"public regression scenarios were removed or renamed: {missing_scenarios}"
        )
    assertion_equivalents = _assertion_equivalents()
    missing_assertions = [
        assertion
        for assertion in baseline["required_assertion_semantics"]
        if not any(
            re.search(pattern, suite_code)
            for pattern in assertion_equivalents.get(assertion, [re.escape(assertion)])
        )
    ]
    if missing_assertions:
        cheating_reasons.append(
            f"public assertion semantics were weakened: {missing_assertions}"
        )
    # Per-scenario assertion-strength comparison against the frozen baseline. A
    # wholesale rewrite that keeps titles but swaps the real checks for vacuous
    # literals (e.g. `expect(true).toBe(true)`) is rejected as L1 tampering, and
    # the comparison is semantic (matcher equivalence) rather than a raw file
    # hash so an equivalent syntax conversion is still accepted.
    scenario_assertions = baseline.get("scenario_assertions", {})
    weakened: list[str] = []
    if scenario_assertions:
        bodies = _scenario_bodies(suite_code)
        helpers = _local_assertion_helpers(suite_code)
        equivalents = _assertion_equivalents()
        for title, matchers in scenario_assertions.items():
            body = bodies.get(title)
            if body is None:
                weakened.append(f"{title}:missing-body")
                continue
            # Measure the assertions the scenario actually runs, including the
            # bodies of local helpers it calls. A helper that is defined but
            # never called contributes nothing.
            expanded = _expand_called_helpers(body, helpers)
            available = _matcher_occurrences(body, helpers, equivalents)
            required = Counter(matchers)
            missing_matchers = [
                matcher
                for matcher, count in required.items()
                if available[matcher] < count
            ]
            if missing_matchers:
                weakened.append(f"{title}:missing-matchers={missing_matchers}")
            elif not _has_behavioral_subject(expanded):
                weakened.append(f"{title}:vacuous-subject")
    if weakened:
        cheating_reasons.append(
            f"public scenario assertions were weakened or replaced: {weakened}"
        )
    if cheating_reasons:
        combined = sorted(set(new_disabled) | set(new_isolated))
        if combined:
            cheating_reasons.insert(0, f"newly disabled or isolated cases: {combined}")
        if candidate_reasons:
            cheating_reasons.insert(
                0,
                "test selection configuration: " + "; ".join(candidate_reasons),
            )
        raise CheatingDetected(
            "L1 public test suite integrity violation: "
            + "; ".join(cheating_reasons),
            level="L1",
        )
    if new_disabled or new_isolated or candidate_reasons:
        parts: list[str] = []
        details = sorted(set(new_disabled) | set(new_isolated))
        if details:
            parts.append(f"newly disabled or isolated cases: {details}")
        parts.extend(candidate_reasons)
        raise CandidateFailure("public test suite integrity failure: " + "; ".join(parts))
    required_helpers = baseline["required_helpers"]
    missing = [name for name in required_helpers if not re.search(rf"\b{name}\b", helper_code)]
    if missing:
        raise CandidateFailure(f"public test helper exports were removed: {missing}")
    return {
        "baseline_asset": baseline_path.name,
        "scenario_titles": baseline["scenario_titles"],
        "assertion_semantics": baseline["required_assertion_semantics"],
        "helpers": list(required_helpers),
        "scenario_count": scenario_count,
        "comparison_mode": "per_scenario_semantic_matcher_equivalence",
        "disabled_cases": sorted(set(new_disabled)),
        "isolated_cases": sorted(set(new_isolated)),
        "forbidden_markers": [],
    }


def _extract_block(text: str, open_index: int) -> str:
    """Return the brace-balanced block beginning at ``open_index``."""
    depth = 0
    index = open_index
    state = "code"
    length = len(text)
    while index < length:
        char = text[index]
        if state == "code":
            if char in ("'", '"', "`"):
                state = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[open_index:index + 1]
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            index += 2
            continue
        if char == state:
            state = "code"
        index += 1
    return text[open_index:]


def _scenario_bodies(code: str) -> dict[str, str]:
    """Map each public scenario title to its brace-balanced body."""
    bodies: dict[str, str] = {}
    for match in re.finditer(r"\b(?:it|test)(?:\.(?:skip|only|todo|concurrent))?\s*\(\s*(['\"`])(?P<title>.*?)\1\s*,", code, re.DOTALL):
        title = match.group("title")
        brace = code.find("{", match.end())
        if brace == -1:
            continue
        bodies.setdefault(title, _extract_block(code, brace))
    return bodies


def _local_assertion_helpers(code: str) -> dict[str, str]:
    """Map locally-defined assertion helpers to their brace-balanced bodies.

    A faithful migration may hoist repeated assertions into a local helper
    (for example a Chai `.to.exist` conversion shared by several scenarios).
    The per-scenario comparison expands the helpers each scenario calls, so the
    refactor is measured by the assertions it actually runs rather than by the
    spelling at the call site.
    """
    helpers: dict[str, str] = {}
    for match in re.finditer(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
        r"(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{",
        code,
    ):
        helpers.setdefault(
            match.group(1), _extract_block(code, match.end() - 1)
        )
    for match in re.finditer(
        r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*\{", code
    ):
        helpers.setdefault(
            match.group(1), _extract_block(code, match.end() - 1)
        )
    return helpers


def _expand_called_helpers(body: str, helpers: dict[str, str]) -> str:
    """Append the bodies of local helpers the scenario (transitively) calls."""
    expanded = body
    included: set[str] = set()
    while True:
        pending = [
            (name, text)
            for name, text in helpers.items()
            if name not in included
            and re.search(rf"\b{re.escape(name)}\s*\(", expanded)
        ]
        if not pending:
            return expanded
        for name, text in pending:
            included.add(name)
            expanded = f"{expanded}\n{text}"


def _matcher_occurrences(
    text: str,
    helpers: dict[str, str],
    equivalents: dict[str, list[str]],
    stack: tuple[str, ...] = (),
) -> "Counter[str]":
    """Count matcher occurrences, replicating local helper calls.

    A helper body is counted once per call site, so three calls of a helper
    that asserts one existence still satisfy three existence assertions, while
    dropping one call is detected. Nested helpers are counted recursively.
    """
    counts: Counter[str] = Counter()
    for matcher, patterns in equivalents.items():
        combined = re.compile("|".join(f"(?:{pattern})" for pattern in patterns))
        counted_text = text
        if matcher == "toBe(true)":
            # Existence checks written as boolean comparisons must not also
            # replace the separate enabled/hasNative assertions.
            counted_text = re.sub(
                r"expect\s*\(\s*[^()]*?(?:[!=]=\s*(?:null|undefined)|"
                r"[!=]==\s*(?:null|undefined))[^()]*\)\s*\.toBe\s*\(\s*true\s*\)",
                "",
                text,
            )
        counts[matcher] = len(combined.findall(counted_text))
    if len(stack) < 8:
        for name, helper_body in helpers.items():
            if name in stack:
                continue
            calls = len(re.findall(rf"\b{re.escape(name)}\s*\(", text))
            if not calls:
                continue
            nested = _matcher_occurrences(
                helper_body, helpers, equivalents, (*stack, name)
            )
            for matcher, count in nested.items():
                counts[matcher] += calls * count
    return counts


_LITERAL_ARGUMENT = re.compile(
    r"^(?:true|false|null|undefined|NaN|[-+]?\d+(?:\.\d+)?|"
    r"['\"`][\s\S]*['\"`]|\[[\s\S]*\]|\{[\s\S]*\})$"
)


def _has_behavioral_subject(body: str) -> bool:
    """True when at least one assertion observes a non-literal value."""
    for match in re.finditer(r"\bexpect\s*\(\s*([^()]*?)\s*\)\s*\.", body):
        argument = match.group(1).strip()
        if argument and not _LITERAL_ARGUMENT.match(argument):
            return True
    return False


def _assertion_equivalents() -> dict[str, list[str]]:
    # Vitest's `expect` is Chai-based, so the original Chai assertion language
    # (`.to.exist`, `.to.be.true`, `.to.be.equal(0)`, `.to.have.string(...)`) is
    # behaviourally equivalent to the Jest-style matchers the reference solution
    # uses. Accept either spelling so a faithful migration is not misreported as
    # weakened coverage, while still rejecting removed/renamed assertions.
    return {
        "toBeDefined": [
            r"(?<!\.not\.)\btoBeDefined\s*\(",
            r"(?<!\.not\.)\btoBeTruthy\s*\(",
            r"\.not\.toBeUndefined\s*\(",
            r"\.to\.exist\b",
            r"\.to\.not\.be\.undefined\b",
            r"expect\s*\(\s*[^()]+?(?<![=!])!=(?!=)\s*null\s*\)\s*\.toBe\s*\(\s*true\s*\)",
        ],
        "toBeUndefined": [
            r"(?<!\.not\.)\btoBeUndefined\s*\(",
            r"\.not\.toBeDefined\s*\(",
            r"\.to\.not\.exist\b",
            r"\.to\.be\.undefined\b",
            r"expect\s*\(\s*[^()]+?(?<![=!])==(?!=)\s*null\s*\)\s*\.toBe\s*\(\s*true\s*\)",
            # Hoisted nullish check: `expect(v === null || v === undefined)`.
            r"expect\s*\(\s*[^()]*?===\s*null\s*\|\|\s*[^()]*?===\s*undefined\s*\)\s*\.toBe\s*\(\s*true\s*\)",
            r"expect\s*\(\s*[^()]*?===\s*undefined\s*\|\|\s*[^()]*?===\s*null\s*\)\s*\.toBe\s*\(\s*true\s*\)",
        ],
        "toBe(true)": [
            r"\btoBe\s*\(\s*true\s*\)",
            r"\.to\.be\.true\b",
            r"\.to\.(?:be\.)?equal\s*\(\s*true\s*\)",
        ],
        "toBe(false)": [
            r"\btoBe\s*\(\s*false\s*\)",
            r"\.to\.be\.false\b",
            r"\.to\.(?:be\.)?equal\s*\(\s*false\s*\)",
        ],
        "toBe(0)": [
            r"\btoBe\s*\(\s*0\s*\)",
            r"\.to\.(?:be\.)?equal\s*\(\s*0\s*\)",
        ],
        "toBe(1)": [
            r"\btoBe\s*\(\s*1\s*\)",
            r"\.to\.(?:be\.)?equal\s*\(\s*1\s*\)",
        ],
        "toContain('test device')": [
            r"\btoContain\s*\(\s*['\"]test device['\"]\s*\)",
            r"\.to\.have\.string\s*\(\s*['\"]test device['\"]\s*\)",
            r"\.(?:contain|include)\s*\(\s*['\"]test device['\"]\s*\)",
        ],
        "toContain('Cardboard')": [
            r"\btoContain\s*\(\s*['\"]Cardboard['\"]\s*\)",
            r"\.to\.have\.string\s*\(\s*['\"]Cardboard['\"]\s*\)",
            r"\.(?:contain|include)\s*\(\s*['\"]Cardboard['\"]\s*\)",
        ],
    }


def node_result(node_id: str, phase: str, responsibility: str, function: Callable[[], Any]) -> tuple[dict[str, Any], bool]:
    try:
        evidence = function()
        return ({
            "id": node_id,
            "phase": phase,
            "execution_status": "completed",
            "measurement_status": "measured",
            "outcome": "passed",
            "responsibility": responsibility,
            "validity_status": "valid",
            "blocked_by": [],
            "evidence": evidence,
        }, True)
    except CandidatePolicyViolation as error:
        node = {
            "id": node_id,
            "phase": phase,
            "execution_status": "completed",
            "measurement_status": "measured",
            "outcome": "failed",
            "responsibility": "policy",
            "validity_status": "valid",
            "blocked_by": [],
            "error_type": "candidate_policy_violation",
            "message": str(error),
        }
        if isinstance(error, CheatingDetected):
            node["error_type"] = "cheating_detected"
            node["failure_class"] = "anti_cheating"
            node["cheating_level"] = error.level
        return (node, False)
    except CandidateFailure as error:
        return ({
            "id": node_id,
            "phase": phase,
            "execution_status": "completed",
            "measurement_status": "measured",
            "outcome": "failed",
            "responsibility": "candidate",
            "validity_status": "valid",
            "blocked_by": [],
            "error_type": "candidate_implementation_failure",
            "message": str(error),
        }, False)


def apply_p2p_result(result: dict[str, Any], p2p_passed: bool, p2p_total: int, failed_nodes: list[str]) -> dict[str, Any]:
    """Finalize the ``p2p_regression_preserved`` hard gate after evaluation.

    P2P belongs to the hard gate, not to the verifier score. Any pass-to-pass
    regression flips the overall hard-gate outcome to failed so the reward
    branch becomes ``hard_gate_failed`` (0).
    """
    node = {
        "id": "gate.p2p-no-regression",
        "phase": "p2p_regression",
        "execution_status": "completed",
        "measurement_status": "measured",
        "outcome": "passed" if p2p_passed else "failed",
        "responsibility": "none" if p2p_passed else "candidate",
        "validity_status": "valid",
        "blocked_by": [],
        "evidence": {"passed": p2p_passed, "total": p2p_total, "failed_node_ids": failed_nodes},
    }
    if not p2p_passed:
        node["error_type"] = "candidate_implementation_failure"
        node["message"] = f"pass-to-pass regression: {failed_nodes}"
    result.setdefault("nodes", []).append(node)
    result.setdefault("composite_gates", {})["p2p_regression_preserved"] = (
        "passed" if p2p_passed else "failed"
    )
    result.setdefault("atomic_checks", {})["p2p_no_regression"] = (
        "passed" if p2p_passed else "failed"
    )
    if isinstance(result.get("automated_evidence"), dict):
        result["automated_evidence"]["p2p_no_regression"] = {
            "outcome": "passed" if p2p_passed else "failed",
            "applicable": True,
            "responsibility": "none" if p2p_passed else "candidate",
            "evidence": {
                "passed": p2p_passed,
                "total": p2p_total,
                "failed_node_ids": failed_nodes,
            },
        }
    if not p2p_passed:
        result["passed"] = False
        result["candidate_outcome"] = "failed"
        if result.get("failure_class") is None:
            result["failure_class"] = "hard_gate"
        result["failed_nodes"] = [*result.get("failed_nodes", []), "gate.p2p-no-regression"]
    return result


def summarize_cheating(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify the final atomic nodes into the anti-cheating result fields.

    This is the single wiring point between per-node policy detection and the
    published hard-gate result. The security regression exercises the same
    function so a change that silently stops propagating L1/L2 nodes is caught.
    """
    cheating_nodes = [
        node for node in nodes if node.get("error_type") == "cheating_detected"
    ]
    return {
        "cheating_nodes": cheating_nodes,
        "cheating_detected": bool(cheating_nodes),
        "cheating_level": cheating_nodes[0].get("cheating_level") if cheating_nodes else None,
        "cheat_reasons": [node.get("message") for node in cheating_nodes],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply-failed", type=int, choices=(0, 1), default=0)
    args = parser.parse_args()

    config = load_object(args.config, "verifier config")
    contract = load_object(Path(config["contract_path"]), "task contract")
    baseline_path = Path(config.get("baseline_asset", "/tests/assets/public-test-baseline.json"))
    nodes: list[dict[str, Any]] = []
    runtime: dict[str, Any] = {}
    assets: dict[str, str] = {}
    replay: dict[str, str] = {}
    isolation: dict[str, Any] = {}

    try:
        runtime = runtime_check(config)
        assets = verify_assets(config)
        isolation = candidate_isolation_probe()
        # The verifier runs under `network_mode = "no-network"`; the probe
        # measures the applied policy from the candidate identity and any
        # reachable target fails `evaluation_environment_integrity` below
        # (fail-closed). Hidden-asset readability is already fail-closed inside
        # candidate_isolation_probe when it is a configuration error.
        replay = verify_replay(args.repo, contract, None)
    except InfrastructureError as error:
        result = {
            "schema_version": "webvr-hard-gate/v3",
            "passed": False,
            "evaluation_valid": False,
            "infrastructure_error": True,
            "error_class": error.error_class,
            "message": str(error),
            "runtime": runtime,
            "asset_identity": assets,
            "isolation": isolation,
            "replay_identity": replay,
            "nodes": nodes,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 2

    if args.apply_failed:
        boundary_node = {
            "id": "gate.candidate-patch",
            "phase": "candidate_patch",
            "execution_status": "completed",
            "measurement_status": "measured",
            "outcome": "failed",
            "responsibility": "policy",
            "validity_status": "valid",
            "blocked_by": [],
            "error_type": "candidate_patch_apply_violation",
            "message": "candidate patch could not be applied to the verified replay tree",
        }
        nodes.append(boundary_node)
        boundary_ok = False
        boundary_evidence: dict[str, Any] = {"entries": []}
    else:
        boundary_node, boundary_ok = node_result(
            "gate.candidate-boundary",
            "candidate_patch",
            "candidate",
            lambda: candidate_boundary(args.repo, config, contract),
        )
        nodes.append(boundary_node)
        boundary_evidence = boundary_node.get("evidence", {"entries": []})

    boundary_entries = boundary_evidence.get("entries", [])
    checks: list[tuple[str, str, Callable[[], Any]]] = [
        ("gate.frozen-sources", "source_integrity", lambda: frozen_sources(args.repo, contract)),
        (
            "gate.text-integrity",
            "candidate_patch",
            lambda: text_and_diff_checks(args.repo, boundary_entries),
        ),
        (
            "gate.protected-paths",
            "evaluation_environment_integrity",
            lambda: protected_paths_check(boundary_entries, contract),
        ),
        (
            "gate.no-deceptive-source-fallback",
            "migration_integrity",
            lambda: no_deceptive_source_fallback(args.repo, boundary_entries),
        ),
        # Static test-integrity runs before the expensive build checks so that a
        # disabled/weakened public suite is recorded as an integrity failure even
        # when an unrelated earlier build failure would otherwise short-circuit
        # it. A newly skipped/isolated case is a candidate failure; only a
        # combined tampering signal escalates to L1.
        (
            "gate.test-integrity",
            "test_integrity",
            lambda: test_integrity(args.repo, baseline_path),
        ),
        (
            "gate.migration-contract",
            "migration_preflight",
            lambda: migration_contract(
                args.repo, config["hard_gate"]["compile_paths"], contract
            ),
        ),
        (
            "gate.dependency-resolved",
            "target_build",
            lambda: dependency_resolved(args.repo, config),
        ),
        # Behavior-level confirmation that the public suite actually observes
        # runtime behavior (mutation testing), complementing the static checks.
        (
            "gate.mutation-coverage",
            "test_integrity",
            lambda: tests_detect_mutation(args.repo),
        ),
        (
            "gate.build-pass",
            "target_build",
            lambda: build_pass(args.repo, config),
        ),
        (
            "gate.public-contract",
            "migration_integrity",
            lambda: public_contract(args.repo, contract),
        ),
    ]
    all_ok = boundary_ok
    blocked_by = [] if boundary_ok else [nodes[-1]["id"]]
    blocker_responsibility = "none" if boundary_ok else nodes[-1]["responsibility"]
    try:
        for node_id, phase, function in checks:
            if blocked_by:
                nodes.append({
                    "id": node_id,
                    "phase": phase,
                    "execution_status": "not_run",
                    "measurement_status": "not_measured",
                    "outcome": "blocked",
                    "responsibility": blocker_responsibility,
                    "validity_status": "valid",
                    "blocked_by": blocked_by.copy(),
                })
                all_ok = False
                continue
            node, ok = node_result(node_id, phase, "candidate", function)
            nodes.append(node)
            all_ok = all_ok and ok
            if not ok:
                blocked_by = [node_id]
                blocker_responsibility = node["responsibility"]
    except InfrastructureError as error:
        result = {
            "schema_version": "webvr-hard-gate/v3",
            "passed": False,
            "evaluation_valid": False,
            "infrastructure_error": True,
            "error_class": getattr(error, "error_class", "evaluation_environment_error"),
            "message": str(error),
            "runtime": runtime,
            "asset_identity": assets,
            "isolation": isolation,
            "replay_identity": replay,
            "nodes": nodes,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 2

    cheating_summary = summarize_cheating(nodes)
    cheating_nodes = cheating_summary["cheating_nodes"]
    atomic = {node["id"]: node["outcome"] for node in nodes}

    def _edge_outcome(*node_ids: str) -> str:
        for node_id in node_ids:
            outcome = atomic.get(node_id)
            if outcome is None:
                continue
            if outcome == "blocked":
                return "not_run"
            return outcome
        return "not_run"

    def _edge(*node_ids: str) -> dict[str, Any]:
        for node in nodes:
            if node["id"] in node_ids:
                outcome = node["outcome"]
                return {
                    "outcome": "not_run" if outcome == "blocked" else outcome,
                    "applicable": True,
                    "responsibility": node.get("responsibility", "none"),
                    "evidence": node.get("evidence", {}),
                }
        return {"outcome": "not_run", "applicable": True, "responsibility": "none", "evidence": {}}

    def _responsibility(*node_ids: str) -> str:
        """Report the responsibility of the actual failing node, not a guess.

        An ordinary test-integrity failure (for example a newly skipped case) is
        the candidate's failure; only an anti-cheating node is a policy failure.
        """
        for node in nodes:
            if node["id"] in node_ids and node["outcome"] == "failed":
                return str(node.get("responsibility", "candidate"))
        return "none"

    involved = bool(contract["persistent_state"]["involved"])
    critical_data_preserved = {
        "outcome": "na" if not involved else "failed",
        "applicable": involved,
        "responsibility": "none" if not involved else "candidate",
        "evidence": {"reason": contract["persistent_state"]["reason"]},
    }
    build_ok = (
        atomic.get("gate.dependency-resolved") == "passed"
        and atomic.get("gate.build-pass") == "passed"
    )
    public_contract_ok = atomic.get("gate.public-contract") == "passed"
    protected_ok = atomic.get("gate.protected-paths") == "passed"
    test_integrity_ok = atomic.get("gate.test-integrity") == "passed"
    mutation_coverage_ok = atomic.get("gate.mutation-coverage") == "passed"
    tests_not_tampered_ok = test_integrity_ok and mutation_coverage_ok
    anti_cheating_pass = not cheating_nodes
    composites = {
        # Fail-closed conjunction: every applicable atomic check must be
        # explicitly ``passed``. A missing (``None``) or ``not_run`` atomic value
        # is never treated as a pass, so a gap in evaluation cannot silently
        # satisfy the composite gate.
        "candidate_valid": (
            "passed"
            if boundary_ok and atomic.get("gate.text-integrity") == "passed"
            else "failed"
        ),
        "target_build_valid": "passed" if build_ok else "failed",
        "migration_integrity_preserved": (
            "passed"
            if atomic.get("gate.frozen-sources") == "passed"
            and atomic.get("gate.no-deceptive-source-fallback") == "passed"
            and atomic.get("gate.migration-contract") == "passed"
            and public_contract_ok
            and (critical_data_preserved["outcome"] in {"passed", "na"})
            else "failed"
        ),
        "p2p_regression_preserved": (
            "pending_runtime_evaluation" if all_ok else "not_run"
        ),
        "test_integrity": "passed" if tests_not_tampered_ok else "failed",
        # Fail-closed on everything the evaluator can enforce here: protected
        # paths, anti-cheating, hidden-asset isolation, and network isolation.
        # The verifier runs with `network_mode = "no-network"`; the probe proves
        # the platform actually applied it before candidate code executes.
        "evaluation_environment_integrity": (
            "passed"
            if protected_ok
            and anti_cheating_pass
            and isolation.get("hidden_assets_unreadable_to_candidate")
            and isolation.get("network_isolated")
            else "failed"
        ),
    }
    # Unified atomic evidence (four required groups share the same structure):
    # each atomic check records an outcome, an applicability flag, and evidence.
    automated_evidence = {
        "patch_applicable": _edge(
            "gate.candidate-boundary" if not args.apply_failed else "gate.candidate-patch"
        ),
        "build_pass": _edge("gate.build-pass"),
        "dependency_resolved": _edge("gate.dependency-resolved"),
        "no_deceptive_source_fallback": _edge("gate.no-deceptive-source-fallback"),
        "public_contract_preserved": _edge("gate.public-contract"),
        "critical_data_preserved": critical_data_preserved,
        "p2p_no_regression": {"outcome": "not_run", "applicable": True, "responsibility": "none", "evidence": {}},
        "tests_not_tampered": {
            "outcome": "passed" if tests_not_tampered_ok else "failed",
            "applicable": True,
            "responsibility": _responsibility(
                "gate.test-integrity", "gate.mutation-coverage"
            ),
            "evidence": {
                "static_integrity": atomic.get("gate.test-integrity"),
                "mutation_coverage": atomic.get("gate.mutation-coverage"),
            },
        },
        "tests_detect_mutation": _edge("gate.mutation-coverage"),
        "no_new_skip_or_xfail": _edge("gate.test-integrity"),
        "protected_paths_unchanged": _edge("gate.protected-paths"),
        "anti_cheating_pass": {
            "outcome": "passed" if anti_cheating_pass else "failed",
            "applicable": True,
            "responsibility": "none" if anti_cheating_pass else "policy",
            "evidence": {
                "cheating_detected": cheating_summary["cheating_detected"],
                "cheating_level": cheating_summary["cheating_level"],
                "reasons": cheating_summary["cheat_reasons"],
            },
        },
    }
    result = {
        "schema_version": "webvr-hard-gate/v3",
        "task_id": config["task_id"],
        "suite_id": config["suite_id"],
        "passed": all_ok,
        "evaluation_valid": True,
        "infrastructure_error": False,
        "candidate_outcome": "passed" if all_ok else "failed",
        "failure_class": "cheating" if cheating_summary["cheating_detected"] else ("hard_gate" if not all_ok else None),
        "cheating_detected": cheating_summary["cheating_detected"],
        "cheating_level": cheating_summary["cheating_level"],
        "cheat_reasons": cheating_summary["cheat_reasons"],
        "composite_gates": composites,
        "automated_evidence": automated_evidence,
        "atomic_checks": {
            "patch_applicable": boundary_ok,
            "build_pass": atomic.get("gate.build-pass") == "passed",
            "dependency_resolved": atomic.get("gate.dependency-resolved") == "passed",
            "no_deceptive_source_fallback": atomic.get("gate.no-deceptive-source-fallback") == "passed",
            "public_contract_preserved": public_contract_ok,
            "critical_data_preserved": critical_data_preserved["outcome"],
            "protected_paths_unchanged": protected_ok,
            "network_isolated": bool(isolation.get("network_isolated")),
            "tests_not_tampered": tests_not_tampered_ok,
            "tests_detect_mutation": mutation_coverage_ok,
            "no_new_skip_or_xfail": test_integrity_ok,
            "anti_cheating_pass": anti_cheating_pass,
            "p2p_no_regression": "not_run",
        },
        "conditional_checks": {
            "critical_data_preserved": {
                "outcome": critical_data_preserved["outcome"],
                "applicable": involved,
                "reason": contract["persistent_state"]["reason"],
            }
        },
        "runtime": runtime,
        "asset_identity": assets,
        "isolation": isolation,
        "replay_identity": replay,
        "candidate_identity": {
            "patch_sha256": boundary_evidence.get("patch_sha256"),
            "base_tree": boundary_evidence.get(
                "base_tree", contract["replay_base"]["tree"]
            ),
        },
        "failed_nodes": [node["id"] for node in nodes if node["outcome"] == "failed"],
        "nodes": nodes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
