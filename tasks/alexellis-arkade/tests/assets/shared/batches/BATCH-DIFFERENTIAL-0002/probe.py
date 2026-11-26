#!/usr/bin/env python3
"""Differential probe for BATCH-DIFFERENTIAL-0002.

The probe executes the shared case corpus against a repository and writes a
normalized JSON observation document.  It is deterministic: the source and the
reference target must produce the same normalized document for every
preserved-behaviour observation in this batch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile

BATCH_ID = "BATCH-DIFFERENTIAL-0002"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
FLAG_RE = re.compile(r"--[A-Za-z0-9][A-Za-z0-9_-]*")
TEST_RE = re.compile(r"^(ok|FAIL|\?)\s+(\S+)")

SYSTEM_SUBCOMMANDS = [
    "go",
    "firecracker",
    "prometheus",
    "cni",
    "containerd",
    "actions-runner",
    "node",
    "tc-redirect-tap",
    "registry",
    "gitlab-runner",
    "buildkitd",
    "pwsh",
    "caddy",
    "zvol-snapshotter",
    "node_exporter",
]

TOP_LEVEL_TOKENS = [
    "install",
    "i",
    "version",
    "v",
    "info",
    "f",
    "update",
    "u",
    "get",
    "g",
    "d",
    "download",
    "uninstall",
    "delete",
    "completion",
    "release",
    "rel",
    "r",
    "chart",
    "c",
    "docker",
    "fstail",
    "ft",
    "gha",
    "system",
    "s",
    "oci",
    "o",
    "search",
]

OUT_OF_SCOPE_COMMANDS = [
    ("chart", "verify"),
    ("oci", "publish"),
    ("system", "install", "go"),
    ("chart", "upgrade"),
    ("docker", "gen"),
    ("gha", "bump"),
    ("fstail",),
]

PACKAGE_TREES = ["pkg/get", "pkg/archive", "pkg/helm", "pkg/k8s"]


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def run(cmd, cwd, env, stdin=None, timeout=600):
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def invoke(binary: str, args, env, stdin=None, timeout=180):
    return run([binary] + list(args), None, env, stdin=stdin, timeout=timeout)


def extract_flags(text: str):
    return sorted(set(FLAG_RE.findall(strip_ansi(text))))


def digest_tree(repo: str, relative: str) -> str:
    base = os.path.join(repo, relative)
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        filenames.sort()
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            relpath = os.path.relpath(path, repo).replace(os.sep, "/")
            digest.update(relpath.encode("utf-8"))
            digest.update(b"\0")
            with open(path, "rb") as handle:
                digest.update(handle.read())
            digest.update(b"\0")
    return digest.hexdigest()


def build_binary(repo: str, tmpdir: str, env) -> str:
    binary = os.path.join(tmpdir, "arkade")
    proc = run(
        ["go", "build", "-mod=vendor", "-o", binary, "."],
        repo,
        env,
        timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "go build failed with exit %d\nstdout:\n%s\nstderr:\n%s"
            % (proc.returncode, proc.stdout, proc.stderr)
        )
    return binary


def observe_oci_login(binary: str, env, tmpdir: str):
    observed = {}

    stdin_dir = tempfile.mkdtemp(prefix="docker-config-stdin-", dir=tmpdir)
    stdin_env = dict(env)
    stdin_env["DOCKER_CONFIG"] = stdin_dir
    proc = invoke(
        binary,
        ["oci", "login", "reg.example.com", "-u", "alice", "--password-stdin"],
        stdin_env,
        stdin="s3cret\n",
    )
    config_path = os.path.join(stdin_dir, "config.json")
    config_written = os.path.isfile(config_path)
    auth = None
    if config_written:
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                auth = json.load(handle)["auths"]["reg.example.com"]["auth"]
        except Exception:
            auth = None
    observed["password_stdin"] = {
        "exit": proc.returncode,
        "config_written": config_written,
        "auth": auth,
    }

    flag_dir = tempfile.mkdtemp(prefix="docker-config-flag-", dir=tmpdir)
    flag_env = dict(env)
    flag_env["DOCKER_CONFIG"] = flag_dir
    proc = invoke(
        binary,
        ["oci", "login", "reg.example.com", "-u", "alice", "-p", "direct"],
        flag_env,
    )
    config_path = os.path.join(flag_dir, "config.json")
    config_written = os.path.isfile(config_path)
    auth = None
    if config_written:
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                auth = json.load(handle)["auths"]["reg.example.com"]["auth"]
        except Exception:
            auth = None
    observed["password_flag"] = {
        "exit": proc.returncode,
        "config_written": config_written,
        "auth": auth,
    }

    missing_dir = tempfile.mkdtemp(prefix="docker-config-missing-", dir=tmpdir)
    missing_env = dict(env)
    missing_env["DOCKER_CONFIG"] = missing_dir
    proc = invoke(binary, ["oci", "login", "reg.example.com"], missing_env)
    combined = (proc.stdout + proc.stderr).lower()
    observed["missing_credentials"] = {
        "exit_nonzero": proc.returncode != 0,
        "reports_required": "username and password required" in combined,
    }

    return observed


def observe_system_install(binary: str, env):
    observed = {}
    for subcommand in SYSTEM_SUBCOMMANDS:
        proc = invoke(binary, ["system", "install", subcommand, "--help"], env)
        observed[subcommand] = {
            "exit": proc.returncode,
            "flags": extract_flags(proc.stdout + proc.stderr),
        }
    return observed


def observe_signal_handling(repo: str):
    source = read_text(os.path.join(repo, "cmd", "get.go"))
    return {
        "registers_interrupt_and_sigterm": "signal.Notify(signalChan, os.Interrupt, syscall.SIGTERM)"
        in source,
        "has_leave_alt_screen": "leaveAltScreen()" in source,
        "restores_cursor": "[?25h" in source,
        "leaves_alternate_screen": "[?1049l" in source,
        "exits_nonzero_on_signal": "os.Exit(2)" in source,
    }


def observe_public_surface(binary: str, env):
    accepted = {}
    for token in TOP_LEVEL_TOKENS:
        proc = invoke(binary, [token, "--help"], env)
        accepted[token] = proc.returncode == 0

    root_help = strip_ansi(invoke(binary, ["--help"], env).stdout)
    alias_proc = invoke(binary, ["d", "--format", "list"], env)
    alias_tools = [
        line.strip()
        for line in strip_ansi(alias_proc.stdout).splitlines()
        if line.strip()
    ]

    return {
        "accepted_tokens": accepted,
        "release_hidden_from_root_help": "release" not in root_help,
        "d_resolves_to_get": alias_proc.returncode == 0
        and "helm" in alias_tools
        and "kubectl" in alias_tools,
    }


def observe_out_of_scope(binary: str, env):
    observed = {}
    for command in OUT_OF_SCOPE_COMMANDS:
        key = " ".join(command)
        proc = invoke(binary, list(command) + ["--help"], env)
        observed[key] = {
            "exit": proc.returncode,
            "flags": extract_flags(proc.stdout + proc.stderr),
        }
    return observed


def observe_catalogs(binary: str, env):
    tools_proc = invoke(binary, ["get", "--format", "list"], env)
    tools = [
        line.strip()
        for line in strip_ansi(tools_proc.stdout).splitlines()
        if line.strip()
    ]

    apps_proc = invoke(binary, ["install", "--print-table"], env)
    apps_text = strip_ansi(apps_proc.stdout)
    normalized_apps = "\n".join(line.rstrip() for line in apps_text.splitlines()).strip() + "\n"
    count_match = re.search(r"There are (\d+) apps", apps_text)

    get_help = strip_ansi(invoke(binary, ["get", "--help"], env).stdout)
    arch_line = ""
    os_line = ""
    for line in get_help.splitlines():
        stripped = line.strip()
        if not arch_line and re.match(r"^(-[A-Za-z],\s*)?--arch(?:[= ]|$)", stripped):
            arch_line = stripped
        if not os_line and re.match(r"^(-[A-Za-z],\s*)?--os(?:[= ]|$)", stripped):
            os_line = stripped

    return {
        "tool_count": len(tools),
        "tool_catalog_sha256": sha256_text("\n".join(tools) + "\n"),
        "app_count": int(count_match.group(1)) if count_match else None,
        "app_table_sha256": sha256_text(normalized_apps),
        "arch_default_x86_64": "x86_64" in arch_line,
        "os_default_linux": "Linux" in os_line,
    }


def observe_handler_trees(repo: str):
    return {relative: digest_tree(repo, relative) for relative in PACKAGE_TREES}


def observe_existing_tests(repo: str, env):
    proc = run(["go", "test", "-mod=vendor", "./cmd/..."], repo, env, timeout=900)
    packages = {}
    for line in strip_ansi(proc.stdout + proc.stderr).splitlines():
        match = TEST_RE.match(line)
        if match:
            packages[match.group(2)] = match.group(1)

    get_test = read_text(os.path.join(repo, "cmd", "get_test.go"))
    install_test = read_text(os.path.join(repo, "cmd", "install_test.go"))

    return {
        "go_test_exit": proc.returncode,
        "packages": dict(sorted(packages.items())),
        "invalid_arch_assertion_present": "Test_GetCommandWithInvalidArch" in get_test
        and "not supported" in get_test,
        "app_table_assertion_present": "TestRenderTable" in install_test,
        "tool_lookup_assertion_present": "TestCheckForTool" in install_test,
        "app_lookup_assertion_present": "TestFindAppByName" in install_test,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    repo = os.path.abspath(args.repo)
    out = os.path.abspath(args.out)

    with tempfile.TemporaryDirectory(prefix="arkade-differential-") as tmpdir:
        home = os.path.join(tmpdir, "home")
        os.makedirs(home, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "HOME": home,
                "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                "XDG_CACHE_HOME": os.path.join(home, ".cache"),
                "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
                "GOFLAGS": "-mod=vendor",
                "CGO_ENABLED": "0",
            }
        )

        binary = build_binary(repo, tmpdir, env)

        observations = {
            "DIFF-033": observe_oci_login(binary, env, tmpdir),
            "DIFF-034": observe_system_install(binary, env),
            "DIFF-035": observe_signal_handling(repo),
            "DIFF-036": observe_public_surface(binary, env),
            "DIFF-037": observe_out_of_scope(binary, env),
            "DIFF-038": observe_catalogs(binary, env),
            "DIFF-039": observe_handler_trees(repo),
            "DIFF-040": observe_existing_tests(repo, env),
        }

    document = {
        "schema_version": "migration-differential-observation:v1",
        "batch_id": BATCH_ID,
        "family": "shared-case-corpus",
        "observations": observations,
    }

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
