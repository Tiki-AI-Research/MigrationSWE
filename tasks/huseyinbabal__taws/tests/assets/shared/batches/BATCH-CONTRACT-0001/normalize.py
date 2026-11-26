#!/usr/bin/env python3
"""Shared normalization helpers for the BATCH-CONTRACT-0001 public-contract probes.

Both the source and the target probe consume this module so that the two sides
compare *normalized observable values* rather than raw, implementation-specific
bytes. Only process-observable facts are normalized:

  * process exit codes,
  * structural stdout (one entry per line, sorted/order preserved where the
    contract fixes an order),
  * presence/absence of a capability,
  * booleans and numeric values.

Human readable message/description/detail text is never compared verbatim.
Error behavior is reduced to an exit code (or, for library probes, the
normalized test verdict PASS/FAIL).

Every potentially long-running command is executed through `run`, which always
passes an explicit timeout to `subprocess.run` and therefore never waits
forever. The probe entrypoints additionally wrap the cargo/go commands in the
coreutils `timeout` binary.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

TIMEOUT = int(os.environ.get("CONTRACT_PROBE_TIMEOUT", "600"))


def run(argv, cwd=None, env=None, timeout=TIMEOUT, stdin=None):
    """Run a command with an explicit timeout; never wait forever."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        kwargs = {}
        if stdin is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = stdin
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=merged,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            **kwargs,
        )
        return {
            "exit": proc.returncode,
            "stdout": proc.stdout.decode("utf-8", "replace"),
            "stderr": proc.stderr.decode("utf-8", "replace"),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {"exit": None, "stdout": "", "stderr": "", "timed_out": True}
    except (OSError, ValueError):
        return {"exit": None, "stdout": "", "stderr": "", "timed_out": False}


def writable_copy(repo, prefix):
    """Return a writable working tree for `repo`.

    The authoring/evaluation containers may mount the repository read-only, so a
    build (`cargo build`, `go test`) cannot write its target/build directory in
    place. When the repository is not writable we copy it into a temporary
    directory and run there. A writable repository is used as-is.
    """
    if os.access(repo, os.W_OK):
        return repo
    dest = tempfile.mkdtemp(prefix=prefix)
    work = os.path.join(dest, "repo")
    shutil.copytree(repo, work, symlinks=True)
    return work


def lines(text):
    return [ln for ln in text.replace("\r\n", "\n").split("\n")]


def nonempty_lines(text):
    return [ln for ln in lines(text) if ln.strip() != ""]


def normalize_cli(capture, result):
    if result["timed_out"]:
        return {"status": "timeout"}
    code = result["exit"]
    if capture == "exit_only":
        return {"exit": code}
    if capture == "exit_and_stdout_nonempty":
        return {"exit": code, "stdout_nonempty": len(result["stdout"]) > 0}
    if capture == "line_count":
        return {"exit": code, "line_count_nonzero": len(nonempty_lines(result["stdout"])) > 0}
    if capture == "sorted_lines":
        return {"exit": code, "lines": sorted(nonempty_lines(result["stdout"]))}
    if capture == "first_lines":
        return {"exit": code, "lines": nonempty_lines(result["stdout"])}
    if capture == "stdout":
        return {"exit": code, "stdout": result["stdout"].strip()}
    return {"exit": code}


def verdict(passed):
    return "PASS" if passed else "FAIL"


def entry(passed, value):
    return {"status": verdict(passed), "value": value}


def cli_value(oid, values, expected):
    """Reduce raw CLI observations to the same normalized value on both sides.

    The returned dict only contains booleans/exit codes, so the source (Rust)
    and target (Go) projections are directly comparable by
    `equal_after_normalization`.
    """
    if oid == "CONTRACT-001":
        return {
            "help_exit_zero": len(values) > 0 and values[0].get("exit") == 0,
            "unknown_flag_rejected": len(values) > 1 and values[1].get("exit") not in (0, None),
            "invalid_log_level_rejected": len(values) > 2 and values[2].get("exit") not in (0, None),
        }
    if oid == "CONTRACT-002":
        shells_ok = len(values) >= 4 and all(v.get("exit") == 0 for v in values[:4])
        fallback_ok = (
            len(values) >= 5
            and values[4].get("exit") == 0
            and bool(values[4].get("stdout_nonempty"))
        )
        regions_ok = len(values) >= 6 and bool(values[5].get("line_count_nonzero"))
        profiles_ok = len(values) >= 7 and bool(values[6].get("line_count_nonzero"))
        return {
            "completion_shells_ok": shells_ok,
            "completion_fallback_ok": fallback_ok,
            "list_regions_nonempty": regions_ok,
            "list_profiles_nonempty": profiles_ok,
        }
    if oid == "CONTRACT-012":
        profiles = values[0].get("lines") or [] if values else []
        regions = values[1].get("lines") or [] if len(values) > 1 else []
        wanted = expected.get("list_regions_first", []) if isinstance(expected, dict) else []
        return {
            "profiles_include_default": "default" in profiles,
            "regions_prefix_ok": regions[: len(wanted)] == wanted,
        }
    if oid == "CONTRACT-020":
        version_ok = bool(values) and values[0].get("stdout") == "taws 1.3.0-rc.8"
        completion_ok = len(values) > 1 and values[1].get("exit") == 0
        return {"version_ok": version_ok, "completion_ok": completion_ok}
    return {"behaviour_ok": bool(values)}


def emit(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True, indent=2))
    sys.stdout.write("\n")
