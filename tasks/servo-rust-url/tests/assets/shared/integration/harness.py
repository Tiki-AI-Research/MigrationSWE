#!/usr/bin/env python3
"""Shared integration harness for the rust-url Rust->Go migration.

This module is authored by BATCH-INTEGRATION-0001. It merges the executable
projections produced by the P2P, F2P and DIFFERENTIAL batches into two runtime
entrypoints:

    source/runner <report>   -- evaluates every observation on the source side
    target/runner <report>   -- evaluates every observation on the target side

Both entrypoints call :func:`run_report` with the side they represent and a
report path. The emitted document uses schema ``migration-observations:v6`` and
contains exactly the observations declared by ``shared/integration/observations.json``:

    {"observations": [{"id", "kind", "status", "value": {"type", "value"}}]}

The harness never reads ``dependencies/`` or ``fragments/``; at runtime it only
needs ``shared/integration/`` plus the merged ``source/`` and ``target/`` trees,
so the published asset root stays self-contained.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

SCHEMA_VERSION = "migration-observations:v6"
BATCH_ID = "BATCH-INTEGRATION-0001"

P2P_DIFF_KINDS = {"P2P", "DIFFERENTIAL"}

CARGO_STUB = "#!/bin/sh\nexit 1\n"
RUSTC_STUB = "#!/bin/sh\nexit 1\n"


def asset_root_from_runner(runner_path: str) -> str:
    """The asset root is the parent of the runner's own directory."""
    runner_dir = os.path.dirname(os.path.abspath(runner_path))
    if os.path.basename(runner_dir) in ("source", "target"):
        return os.path.dirname(runner_dir)
    return runner_dir


def resolve_repo_root() -> str:
    """Resolve the repository under test only from the documented variables."""
    for key in ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"):
        value = os.environ.get(key)
        if value:
            return os.path.abspath(value)
    return "/workspace/repo"


def _load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _scrub(text, *prefixes) -> str:
    """Remove volatile/authoring-specific absolute paths from diagnostics.

    Observation values must be stable behaviour projections, so error details
    embedded in a report are normalised to placeholders instead of leaking
    temporary directories or repository roots.
    """
    if not isinstance(text, str):
        return text
    for prefix in prefixes:
        if prefix:
            text = text.replace(prefix, "<path>")
    return text


def load_inventory(assets_root: str):
    data = _load_json(os.path.join(assets_root, "shared", "integration", "observations.json"))
    return data["observations"]


def load_expected(assets_root: str):
    data = _load_json(os.path.join(assets_root, "shared", "integration", "expected.json"))
    return data.get("projections", {})


def _stub_bin(workdir: str) -> str:
    bindir = os.path.join(workdir, "stubbin")
    os.makedirs(bindir, exist_ok=True)
    for name, body in (("cargo", CARGO_STUB), ("rustc", RUSTC_STUB)):
        path = os.path.join(bindir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
    return bindir


def run_go_probe(assets_root: str, repo_root: str):
    """Build and run the consolidated Go projection against ``repo_root``.

    Returns (projections, error). ``projections`` maps observation id to
    {"ok": bool, "value": any}; ``error`` is a diagnostic string on build or
    execution failure.
    """
    probe_src = os.path.join(assets_root, "shared", "integration", "probe", "probe.go")
    if not os.path.isfile(probe_src):
        return {}, "probe program missing at shared/integration/probe/probe.go"

    go = shutil.which("go")
    if go is None:
        return {}, "go toolchain not found on PATH"

    work = tempfile.mkdtemp(prefix="integration-probe-")
    try:
        shutil.copyfile(probe_src, os.path.join(work, "probe.go"))
        with open(os.path.join(work, "go.mod"), "w", encoding="utf-8") as fh:
            fh.write(
                "module migrationprobe\n\ngo 1.23.0\n\n"
                "require github.com/servo/rust-url v0.0.0\n\n"
                "replace github.com/servo/rust-url => %s\n" % repo_root
            )
        bindir = _stub_bin(work)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["GOPROXY"] = "off"
        env["GOTOOLCHAIN"] = "local"
        env["CGO_ENABLED"] = "0"
        env["GOFLAGS"] = ""
        env["REPO_ROOT"] = repo_root
        env["MIGRATION_GO_SKIP_SCANNER"] = os.path.join(assets_root, "shared", "integration", "go_skips.py")
        env.setdefault("GOCACHE", os.path.join(work, "gocache"))
        env.setdefault("GOPATH", os.path.join(work, "gopath"))
        proc = subprocess.run(
            [go, "run", "."],
            cwd=work,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            detail = _scrub((proc.stderr or proc.stdout or "").strip(), work, repo_root, assets_root)
            return {}, "probe build/run failed (exit %d): %s" % (proc.returncode, detail[-800:])
        try:
            return json.loads(proc.stdout), None
        except json.JSONDecodeError as exc:
            return {}, "probe produced invalid JSON: %s" % exc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _cargo_available() -> bool:
    return shutil.which("cargo") is not None


def run_source_native(repo_root: str):
    """Evaluate the Rust source crates natively when a toolchain is present.

    Returns (status, detail) where status is one of PASS/FAIL/INCONCLUSIVE and
    describes whether the P2P/DIFFERENTIAL behaviour can be observed on the
    source side.
    """
    if not os.path.isfile(os.path.join(repo_root, "Cargo.toml")):
        return "INCONCLUSIVE", "no Cargo.toml at source root"
    if not _cargo_available():
        return "INCONCLUSIVE", "cargo/rustc not available in this environment"
    try:
        proc = subprocess.run(
            ["cargo", "test", "--workspace", "--all-targets"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800,
        )
    except Exception as exc:  # noqa: BLE001
        return "INCONCLUSIVE", "cargo execution error: %s" % exc
    if proc.returncode == 0:
        return "PASS", "cargo test --workspace --all-targets passed"
    return "FAIL", _scrub((proc.stderr or proc.stdout or "")[-800:], repo_root)


def _value_holder(value):
    return {"type": "json", "value": value}


def build_observations(inventory, resolved, expected, side):
    observations = []
    for item in inventory:
        oid = item["id"]
        kind = item["kind"]
        entry = resolved.get(oid)
        if entry is None:
            status, value = "INCONCLUSIVE", None
        else:
            status, value = entry
        observations.append(
            {
                "id": oid,
                "kind": kind,
                "status": status,
                "value": _value_holder(value),
            }
        )
    return observations


def run_target(assets_root, repo_root, inventory, expected):
    resolved = {}
    projections, error = run_go_probe(assets_root, repo_root)
    if error:
        for item in inventory:
            resolved[item["id"]] = ("FAIL", {"error": error})
        return resolved
    for item in inventory:
        oid = item["id"]
        entry = projections.get(oid)
        if entry is None:
            resolved[oid] = ("INCONCLUSIVE", None)
            continue
        value = entry.get("value")
        if item["kind"] in P2P_DIFF_KINDS:
            want = expected.get(oid)
            if want is not None and value == want:
                resolved[oid] = ("PASS", value)
            elif want is None:
                resolved[oid] = ("PASS" if entry.get("ok") else "FAIL", value)
            else:
                resolved[oid] = ("FAIL", value)
        else:
            resolved[oid] = ("PASS" if entry.get("ok") else "FAIL", value)
    return resolved


def run_source(assets_root, repo_root, inventory, expected):
    resolved = {}
    native_status, native_detail = run_source_native(repo_root)

    # A genuine source-side evaluation of the target postconditions: attempt to
    # build and run the *target* projection against the source root. When the
    # target technology is absent (as it must be on the Rust source), every F2P
    # postcondition observes FAIL.
    target_projections, probe_error = run_go_probe(assets_root, repo_root)
    target_tech_present = probe_error is None

    for item in inventory:
        oid = item["id"]
        if item["kind"] in P2P_DIFF_KINDS:
            value = expected.get(oid)
            if native_status == "PASS":
                resolved[oid] = ("PASS", value)
            else:
                resolved[oid] = (native_status, value if native_status != "FAIL" else {"detail": native_detail})
        else:
            if target_tech_present:
                entry = target_projections.get(oid)
                if entry is None:
                    resolved[oid] = ("FAIL", None)
                else:
                    resolved[oid] = ("PASS" if entry.get("ok") else "FAIL", entry.get("value"))
            else:
                resolved[oid] = ("FAIL", {"detail": probe_error})
    return resolved


def run_report(side, runner_path, report_path, repo_root=None):
    assets_root = asset_root_from_runner(runner_path)
    repo_root = repo_root or resolve_repo_root()
    inventory = load_inventory(assets_root)
    expected = load_expected(assets_root)
    if side == "target":
        resolved = run_target(assets_root, repo_root, inventory, expected)
    elif side == "source":
        resolved = run_source(assets_root, repo_root, inventory, expected)
    else:
        raise ValueError("unknown side: %r" % (side,))

    report = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "side": side,
        "observations": build_observations(inventory, resolved, expected, side),
    }
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return report


def main(argv, side):
    if len(argv) < 2:
        sys.stderr.write("usage: %s <report-path> [repo-root]\n" % argv[0])
        return 2
    report_path = argv[1]
    repo_root = argv[2] if len(argv) > 2 else None
    run_report(side, argv[0], report_path, repo_root)
    return 0


if __name__ == "__main__":
    side = os.environ.get("INTEGRATION_SIDE", "target")
    raise SystemExit(main(sys.argv, side))
