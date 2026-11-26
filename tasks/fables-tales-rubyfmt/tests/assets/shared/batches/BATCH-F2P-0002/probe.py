#!/usr/bin/env python3
"""Shared F2P observation probes for BATCH-F2P-0002.

Each probe uses the same semantic predicate on the source and target
repositories. A target repository must expose the Go 1.24 formatter / CLI
capability described by the frozen contract; a source repository cannot
satisfy the target postcondition.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))
from diff_contract import diff_matches


OBSERVATION_CHECKS = {
    "F2P-032": "dependency_graph",
    "F2P-033": "dependency_graph",
    "F2P-039": "path_only_execution",
    "F2P-040": "path_only_execution",
    "F2P-025": "stdin_devnull_and_tty",
    "F2P-045": "stdin_devnull_and_tty",
    "F2P-026": "vendored_prism_runtime",
    "F2P-036": "vendored_prism_runtime",
    "F2P-044": "vendored_prism_runtime",
    "F2P-027": "named_input_errors",
    "F2P-029": "named_input_errors",
    "F2P-030": "named_input_errors",
    "F2P-028": "check_diff",
    "F2P-046": "check_diff",
    "F2P-031": "inplace_mtime",
    "F2P-042": "inplace_mtime",
    "F2P-047": "inplace_mtime",
    "F2P-048": "inplace_mtime",
    "F2P-034": "extension_filtering",
    "F2P-038": "extension_filtering",
    "F2P-035": "ignored_stdin_filepath",
    "F2P-037": "ignored_stdin_filepath",
    "F2P-041": "ignored_stdin_filepath",
    "F2P-043": "public_go_library",
}


def _run(argv, cwd, stdin=None, env=None, timeout=120):
    kwargs = {}
    if isinstance(stdin, int):
        kwargs["stdin"] = stdin
    else:
        kwargs["input"] = stdin
    return subprocess.run(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
        **kwargs,
    )


def _build(root):
    binary = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-probe-")) / "rubyfmt"
    if not (pathlib.Path(root) / "go.mod").is_file():
        return None, "go.mod missing"
    proc = _run(
        ["go", "build", "-mod=vendor", "-buildvcs=false", "-o", str(binary), "./cmd/rubyfmt"],
        root,
        timeout=600,
    )
    if proc.returncode != 0:
        return None, "go build failed: " + proc.stderr.decode("utf-8", "replace")[-600:]
    return binary, ""


def _go_packages(root):
    proc = _run(["go", "list", "-deps", "./cmd/rubyfmt"], root)
    if proc.returncode != 0:
        return None, "go list failed: " + proc.stderr.decode("utf-8", "replace")[-400:]
    return set(proc.stdout.decode("utf-8", "replace").split()), ""


def _check_dependency_graph(root, binary):
    packages, err = _go_packages(root)
    if packages is None:
        return False, err
    required = {"rubyfmt/internal/cli", "rubyfmt/internal/formatter", "rubyfmt/internal/prism"}
    forbidden = sorted(
        p for p in packages
        if "rubyfmt-main" in p or "librubyfmt" in p or "ruby_prism" in p or "ruby-prism" in p
    )
    env = dict(os.environ)
    env["PATH"] = "/nonexistent"
    if binary is None:
        return False, "no built executable"
    proc = _run([str(binary)], root, stdin=b"a 1,2,3", env=env)
    ok = (
        required <= packages
        and not forbidden
        and proc.returncode == 0
        and proc.stdout == b"a(1, 2, 3)\n"
    )
    return ok, json.dumps({
        "missing_packages": sorted(required - packages),
        "forbidden_packages": forbidden,
        "exit": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
    })


def _check_path_only_execution(root, binary):
    if binary is None:
        return False, "no built executable"
    path_dir = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-path-"))
    local = path_dir / "rubyfmt"
    shutil.copy2(binary, local)
    local.chmod(0o755)
    env = {"PATH": str(path_dir), "HOME": str(path_dir), "GOCACHE": str(path_dir / "cache")}
    proc = _run([str(local)], root, stdin=b"a 1,2,3", env=env)
    return (
        proc.returncode == 0 and proc.stdout == b"a(1, 2, 3)\n",
        json.dumps({
            "path": str(path_dir),
            "exit": proc.returncode,
            "stdout": proc.stdout.decode("utf-8", "replace"),
            "stderr": proc.stderr.decode("utf-8", "replace")[:300],
        }),
    )


def _check_stdin_devnull_and_tty(root, binary):
    if binary is None:
        return False, "no built executable"
    proc = _run([str(binary)], root, stdin=subprocess.DEVNULL)
    ok_devnull = proc.returncode == 0 and b"Usage: rubyfmt" not in proc.stdout
    import pty

    master, slave = pty.openpty()
    tty_proc = subprocess.Popen(
        [str(binary)],
        cwd=str(root),
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(slave)
    try:
        tty_out, tty_err = tty_proc.communicate(timeout=30)
    finally:
        os.close(master)
    ok_tty = b"Usage: rubyfmt" in tty_out or b"Usage: rubyfmt" in tty_err
    return ok_devnull and ok_tty, json.dumps({
        "devnull_exit": proc.returncode,
        "devnull_stdout": proc.stdout.decode("utf-8", "replace")[:120],
        "tty_exit": tty_proc.returncode,
        "tty_stdout": tty_out.decode("utf-8", "replace")[:160],
    })


def _check_vendored_prism_runtime(root, binary):
    packages, err = _go_packages(root)
    if packages is None:
        return False, err
    if binary is None:
        return False, "no built executable"
    data = binary.read_bytes()
    has_prism_package = "rubyfmt/internal/prism" in packages
    has_rust_prism = b"ruby-prism" in data or b"ruby_prism" in data
    env = dict(os.environ)
    env["PATH"] = "/nonexistent"
    proc = _run([str(binary)], root, stdin=b"a 1,2,3", env=env)
    ok = (
        has_prism_package
        and not has_rust_prism
        and proc.returncode == 0
        and proc.stdout == b"a(1, 2, 3)\n"
    )
    return ok, json.dumps({
        "prism_package": has_prism_package,
        "rust_prism_reference": has_rust_prism,
        "PATH": env["PATH"],
        "exit": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
    })


def _check_named_input_errors(root, binary):
    if binary is None:
        return False, "no built executable"
    work = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-errors-"))
    (work / "bad.rb").write_bytes(b"a 1,2,,\n")
    (work / "good.rb").write_bytes(b"a(1, 2)\n")
    plain = _run([str(binary), "bad.rb", "good.rb"], work)
    fail_fast = _run([str(binary), "--fail-fast", "bad.rb", "good.rb"], work)
    missing = _run([str(binary), "--fail-fast", "missing.rb"], work)
    ok = (
        plain.returncode == 0
        and plain.stdout == b"a(1, 2)\n"
        and fail_fast.returncode == 1
        and missing.returncode == 3
    )
    return ok, json.dumps({
        "plain_exit": plain.returncode,
        "plain_stdout": plain.stdout.decode("utf-8", "replace"),
        "fail_fast_exit": fail_fast.returncode,
        "missing_exit": missing.returncode,
    })


def _check_check_diff(root, binary):
    if binary is None:
        return False, "no built executable"
    work = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-check-"))
    target = work / "test.rb"
    target.write_bytes(b"a 1,2,3\n")
    proc = _run([str(binary), "--check", "test.rb"], work)
    expected = (
        b"--- test.rb\n"
        b"+++ test.rb\n"
        b"@@ -1 +1 @@\n"
        b"-a 1,2,3\n"
        b"+a(1, 2, 3)\n"
    )
    return proc.returncode == 5 and diff_matches(proc.stdout, [("test.rb", b"a 1,2,3\n", b"a(1, 2, 3)\n")]), json.dumps({
        "exit": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
        "expected_stdout": expected.decode("utf-8", "replace"),
    })


def _check_inplace_mtime(root, binary):
    if binary is None:
        return False, "no built executable"
    work = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-mtime-"))
    unchanged = work / "unchanged.rb"
    changed = work / "changed.rb"
    unchanged.write_bytes(b"a(1, 2, 3)\n")
    changed.write_bytes(b"a 1,2,3\n")
    old_time = time.time() - 3600
    os.utime(unchanged, (old_time, old_time))
    unchanged_mtime = unchanged.stat().st_mtime_ns
    changed_mtime = changed.stat().st_mtime_ns
    proc = _run([str(binary), "-i", "unchanged.rb", "changed.rb"], work)
    ok = (
        proc.returncode == 0
        and unchanged.stat().st_mtime_ns == unchanged_mtime
        and unchanged.read_bytes() == b"a(1, 2, 3)\n"
        and changed.read_bytes() == b"a(1, 2, 3)\n"
        and changed.stat().st_mtime_ns > changed_mtime
    )
    return ok, json.dumps({
        "exit": proc.returncode,
        "unchanged_mtime_preserved": unchanged.stat().st_mtime_ns == unchanged_mtime,
        "changed_mtime_advanced": changed.stat().st_mtime_ns > changed_mtime,
        "changed_bytes": changed.read_bytes().decode("utf-8", "replace"),
    })


def _check_extension_filtering(root, binary):
    if binary is None:
        return False, "no built executable"
    explicit = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-explicit-"))
    (explicit / "test.rake").write_bytes(b"a 1,2,3")
    tree = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-tree-"))
    (tree / "test.rb").write_bytes(b"a 1,2,3")
    (tree / "test.txt").write_bytes(b"a 1,2,3")
    r1 = _run([str(binary), "-i", "test.rake"], explicit)
    r2 = _run([str(binary), "-i", "."], tree)
    ok = (
        r1.returncode == 0
        and (explicit / "test.rake").read_bytes() == b"a(1, 2, 3)\n"
        and r2.returncode == 0
        and (tree / "test.rb").read_bytes() == b"a(1, 2, 3)\n"
        and (tree / "test.txt").read_bytes() == b"a 1,2,3"
    )
    return ok, json.dumps({
        "explicit_exit": r1.returncode,
        "explicit_bytes": (explicit / "test.rake").read_bytes().decode("utf-8", "replace"),
        "tree_exit": r2.returncode,
        "rb_bytes": (tree / "test.rb").read_bytes().decode("utf-8", "replace"),
        "txt_bytes": (tree / "test.txt").read_bytes().decode("utf-8", "replace"),
    })


def _check_ignored_stdin_filepath(root, binary):
    if binary is None:
        return False, "no built executable"
    work = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-ignore-"))
    (work / ".rubyfmtignore").write_text("ignored.rb\n", encoding="utf-8")
    normal = _run([str(binary), "--stdin-filepath", "ignored.rb"], work, stdin=b"a 1,2,3")
    check = _run(
        [str(binary), "--check", "--stdin-filepath", "ignored.rb"],
        work,
        stdin=b"a 1,2,3",
    )
    ok = (
        normal.returncode == 0
        and normal.stdout == b"a 1,2,3"
        and check.returncode == 0
        and check.stdout == b""
    )
    return ok, json.dumps({
        "normal_exit": normal.returncode,
        "normal_stdout": normal.stdout.decode("utf-8", "replace"),
        "check_exit": check.returncode,
        "check_stdout": check.stdout.decode("utf-8", "replace"),
    })


def _check_public_go_library(root, binary):
    if not (pathlib.Path(root) / "go.mod").is_file():
        return False, "go.mod missing"
    proc = _run(["go", "test", "."], root, timeout=600)
    return proc.returncode == 0, json.dumps({
        "exit": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace")[-400:],
        "stderr": proc.stderr.decode("utf-8", "replace")[-600:],
    })

CHECKS = {
    "dependency_graph": _check_dependency_graph,
    "path_only_execution": _check_path_only_execution,
    "stdin_devnull_and_tty": _check_stdin_devnull_and_tty,
    "vendored_prism_runtime": _check_vendored_prism_runtime,
    "named_input_errors": _check_named_input_errors,
    "check_diff": _check_check_diff,
    "inplace_mtime": _check_inplace_mtime,
    "extension_filtering": _check_extension_filtering,
    "ignored_stdin_filepath": _check_ignored_stdin_filepath,
    "public_go_library": _check_public_go_library,
}


def main(argv):
    if len(argv) < 2:
        print("usage: probe.py <repo-root> [results.json]", file=sys.stderr)
        return 2
    root = pathlib.Path(argv[1]).resolve()
    output_path = pathlib.Path(argv[2]) if len(argv) > 2 else None
    binary, build_error = _build(root)
    cache = {}
    observations = {}
    for observation_id, check_name in OBSERVATION_CHECKS.items():
        if check_name not in cache:
            cache[check_name] = CHECKS[check_name](root, binary)
        passed, detail = cache[check_name]
        observations[observation_id] = {
            "check": check_name,
            "status": "PASS" if passed else "FAIL",
            "detail": detail,
        }
    report = {
        "batch_id": "BATCH-F2P-0002",
        "repo_root": str(root),
        "binary": str(binary) if binary else None,
        "build_error": build_error,
        "observations": observations,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if output_path is not None:
        output_path.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
