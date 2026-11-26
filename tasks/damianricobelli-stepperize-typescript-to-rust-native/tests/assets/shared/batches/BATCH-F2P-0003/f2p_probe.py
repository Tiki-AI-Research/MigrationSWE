#!/usr/bin/env python3
"""Shared F2P probe runner for BATCH-F2P-0003.

The runner implements one semantic predicate per observation. The *same* code
is executed against the source (TypeScript) repository and against the target
(Rust) repository; behaviour is therefore determined by the repository under
evaluation, never by the side label.

Every potentially long-running command is wrapped in an explicit ``timeout``
and no step is allowed to block indefinitely.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.sax.saxutils as sax

HERE = os.path.dirname(os.path.abspath(__file__))
# shared/batches/BATCH-F2P-0003 -> assets root
ASSETS_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
PROBE_FILE = os.path.join(ASSETS_ROOT, "fixtures", "batches", "BATCH-F2P-0003", "f2p_probe.rs")
OBSERVATIONS_FILE = os.path.join(HERE, "observations.json")

# Shared crate discovery: the migration contract does not pin the crate
# location, so locate it instead of assuming crates/stepperize-core.
_SHARED_INTEGRATION = os.path.join(ASSETS_ROOT, "shared", "integration")
if _SHARED_INTEGRATION not in sys.path:
    sys.path.insert(0, _SHARED_INTEGRATION)
try:
    import crate_discovery  # noqa: E402
except ImportError:  # pragma: no cover - assets incomplete
    crate_discovery = None
LOCKED = ["--workspace", "--offline"]
CARGO_TIMEOUT = 1500
GIT_TIMEOUT = 300

PASS = "PASS"
FAIL = "FAIL"
INFRA = "INFRA_ERROR"

DELEGATION_MARKERS = (
    "std::process::Command",
    "Command::new",
    "child_process",
    "napi",
    "neon::",
    "libloading",
)


def _run(cmd, cwd, timeout):
    """Run a command with a hard timeout.

    Returns ``(returncode, output)`` where returncode is an int, the string
    ``"timeout"`` for a permanent block, or ``None`` when the executable cannot
    be started at all (infrastructure error).
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout
    except FileNotFoundError as exc:
        return None, str(exc)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return "timeout", out


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _load_toml(path):
    try:
        import tomllib
    except ImportError:  # pragma: no cover - python < 3.11
        return None
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError):
        return None


def _crate_info(repo):
    if crate_discovery is None:
        return None
    return crate_discovery.discover_crate(repo)


def _crate_dir(repo):
    info = _crate_info(repo)
    if info is not None:
        return info["dir"]
    return os.path.join(repo, "crates", "stepperize-core")


def _crate_manifest(repo):
    return os.path.join(_crate_dir(repo), "Cargo.toml")


def _crate_lib(repo):
    info = _crate_info(repo)
    return info["lib"] if info is not None else "stepperize_core"


def _has_crate(repo):
    return _crate_info(repo) is not None


def _cargo(repo, args):
    return crate_discovery.cargo_command(repo, args)


def _cargo_available():
    return shutil.which("cargo") is not None


def check_dependency_policy(repo):
    """The migrated library runs offline without runtime delegation."""
    if not _has_crate(repo):
        return FAIL, "no stepperize-core crate in repository"
    src_dir = os.path.dirname(_crate_info(repo)["source"])
    for root, _dirs, files in os.walk(src_dir):
        for name in files:
            if not name.endswith(".rs"):
                continue
            text = _read(os.path.join(root, name)) or ""
            for marker in DELEGATION_MARKERS:
                if marker in text:
                    return FAIL, "runtime delegation marker %r found" % marker

    if not _cargo_available():
        return INFRA, "cargo executable unavailable"
    code, out = _run(_cargo(repo, ["test"] + LOCKED), repo, CARGO_TIMEOUT)
    if code is None:
        return INFRA, "cargo could not be started"
    if code == "timeout":
        return INFRA, "cargo test timed out"
    if code != 0:
        return FAIL, "cargo test failed\n" + out[-2000:]
    return PASS, "no delegation; cargo test passed offline"


def check_manifest(repo):
    """Check the actual native library manifest, without prescribing a layout."""
    info = _crate_info(repo)
    if info is None:
        return FAIL, "no stepperize-core crate in repository"
    if _load_toml(info["manifest"]) is None:
        return FAIL, "crate manifest unreadable"
    return PASS, "native library manifest and its declared source exist"


def check_change_scope(repo):
    """R029/F2P-054: only migration scope plus the Rust crate change."""
    if not _has_crate(repo):
        return FAIL, "no stepperize-core crate in repository"
    if not os.path.isfile(_crate_info(repo)["source"]):
        return FAIL, "crate source missing"

    for keep in ("packages/react", "apps/docs"):
        if not os.path.isdir(os.path.join(repo, keep)):
            return FAIL, "%s is missing" % keep

    if shutil.which("git") is None:
        return INFRA, "git unavailable; cannot compare against the base commit"
    if not os.path.isdir(os.path.join(repo, ".git")):
        return FAIL, "repository is not a git checkout; base scope unverifiable"
    code, out = _run(
        ["git", "diff", "--exit-code", "HEAD", "--", "packages/react", "apps/docs"],
        repo,
        GIT_TIMEOUT,
    )
    if code is None:
        return INFRA, "git could not be started"
    if code == "timeout":
        return INFRA, "git diff timed out"
    if code != 0:
        return FAIL, "packages/react or apps/docs differ from the base commit"
    return PASS, "scope limited to the crate; react/docs unchanged"


def check_standalone(repo):
    """R002/F2P-067: the crate builds and tests with the TypeScript sources gone."""
    if not _has_crate(repo):
        return FAIL, "no stepperize-core crate in repository"
    if not _cargo_available():
        return INFRA, "cargo executable unavailable"

    info = _crate_info(repo)
    if info is None:
        return FAIL, "no stepperize-core crate in repository"
    work = tempfile.mkdtemp(prefix="f2p-standalone-")
    try:
        staged = os.path.join(work, "repo")
        shutil.copytree(repo, staged, ignore=shutil.ignore_patterns("target", ".git", "node_modules"))
        # Remove the TypeScript implementation from the isolated tree; mixed
        # layouts keep the Rust sources next to the .ts files.
        for root, _dirs, files in os.walk(staged):
            for name in files:
                if name.endswith(".ts"):
                    try:
                        os.remove(os.path.join(root, name))
                    except OSError:
                        pass
        code, out = _run(_cargo(staged, ["test"] + LOCKED), staged, CARGO_TIMEOUT)
    except OSError as exc:
        return INFRA, "could not stage crate: %s" % exc
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if code is None:
        return INFRA, "cargo could not be started"
    if code == "timeout":
        return INFRA, "cargo test timed out"
    if code != 0:
        return FAIL, "crate does not build or test without packages/core/src\n" + out[-2000:]
    return PASS, "crate builds and tests without the TypeScript sources"


def check_build(repo):
    """R001/F2P-072: cargo build yields the native Rust library crate."""
    if not _has_crate(repo):
        return FAIL, "no stepperize-core crate in repository"
    if not _cargo_available():
        return INFRA, "cargo executable unavailable"
    code, out = _run(
        _cargo(repo, ["build", "--workspace", "--offline"]), repo, CARGO_TIMEOUT
    )
    if code is None:
        return INFRA, "cargo could not be started"
    if code == "timeout":
        return INFRA, "cargo build timed out"
    if code != 0:
        return FAIL, "cargo build failed\n" + out[-2000:]
    return PASS, "cargo build produced the native library crate"


PY_CHECKS = {
    "dependency": check_dependency_policy,
    "manifest": check_manifest,
    "scope": check_change_scope,
    "standalone": check_standalone,
    "build": check_build,
}


def _parse_cargo_tests(output):
    results = {}
    for line in output.splitlines():
        if not line.startswith("test "):
            continue
        parts = line.split(" ... ", 1)
        if len(parts) != 2:
            continue
        name = parts[0][len("test "):].strip()
        status = parts[1].strip()
        if status in ("ok", "FAILED", "ignored"):
            results[name] = status
    return results


def run_rust_checks(repo):
    """Evaluate the crate's migrated behaviour.

    The target Rust public interface is fixed by the contract (see
    instruction.md), so the evaluator-owned postcondition suite MUST compile
    against the candidate crate and its assertions MUST pass.  Neither a binding
    mismatch nor an assertion failure may fall back to the candidate's own
    suite, which cannot prove the contract behaviour.
    """
    if not _has_crate(repo):
        return None, "no stepperize-core crate in repository"
    if not _cargo_available():
        return None, "cargo executable unavailable"

    if not os.path.isfile(PROBE_FILE):
        return None, "no evaluator postcondition suite available"

    tests_dir = os.path.join(_crate_dir(repo), "tests")
    dest = os.path.join(tests_dir, "f2p_probe.rs")
    created = False
    try:
        if not os.path.isdir(tests_dir):
            os.makedirs(tests_dir)
        with open(PROBE_FILE, encoding="utf-8") as handle:
            probe_text = handle.read()
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(probe_text.replace("stepperize_core", _crate_lib(repo)))
        created = True
        code, out = _run(_cargo(repo, ["test"] + LOCKED), repo, CARGO_TIMEOUT)
    except OSError as exc:
        return None, "could not stage probe: %s" % exc
    finally:
        if created:
            try:
                os.remove(dest)
            except OSError:
                pass

    if code is None:
        return None, "cargo could not be started"
    if code == "timeout":
        return None, "cargo test timed out"
    if code == 0:
        return True, out
    # A binding mismatch means the crate does not expose the required public
    # interface; an assertion failure means the behaviour is wrong.  Both FAIL.
    return False, out


def evaluate(repo):
    spec = json.loads(_read(OBSERVATIONS_FILE) or "{}")
    observations = spec.get("observations", spec)
    rust_results, rust_detail = run_rust_checks(repo)
    py_cache = {}

    report = {}
    for observation_id, entry in observations.items():
        statuses = []
        details = []
        for check in entry.get("checks", []):
            if check.startswith("rust:"):
                test_name = check[len("rust:"):].strip()
                if rust_results is None:
                    statuses.append(FAIL)
                    details.append("rust:%s: %s" % (test_name, rust_detail))
                elif rust_results:
                    statuses.append(PASS)
                    details.append("rust:%s: postcondition suite passed" % test_name)
                else:
                    statuses.append(FAIL)
                    details.append("rust:%s: postcondition suite failed" % test_name)
            elif check.startswith("policy:"):
                kind = check[len("policy:"):].strip()
                if kind not in py_cache:
                    fn = PY_CHECKS.get(kind)
                    py_cache[kind] = (
                        fn(repo) if fn else (FAIL, "unknown policy check %r" % kind)
                    )
                status, detail = py_cache[kind]
                statuses.append(status)
                details.append("policy:%s: %s" % (kind, detail))
            else:
                statuses.append(FAIL)
                details.append("unknown check %r" % check)

        if FAIL in statuses:
            status = FAIL
        elif PASS in statuses and all(s == PASS for s in statuses):
            status = PASS
        elif INFRA in statuses:
            status = INFRA
        else:
            status = INFRA
        report[observation_id] = (status, " | ".join(details))
    return report


def _xml_attr(text):
    """Escape a string for use inside a double-quoted XML attribute.

    ``xml.sax.saxutils.escape`` escapes ``&``/``<``/``>`` but *not* the quote
    characters, so a failure detail that contains ``"`` (for example a Rust
    compiler snippet quoting a string literal) would terminate the attribute
    early and make the report unparsable.  Escape quotes as well and drop
    characters that are not valid in XML 1.0 so a genuine FAIL is never
    downgraded to an unparsable/INCONCLUSIVE report.
    """

    cleaned = "".join(
        ch
        for ch in str(text)
        if ch in "\t\n\r" or 0x20 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD
    )
    return sax.escape(cleaned, {'"': "&quot;", "'": "&apos;"})


def write_report(report, path):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    failures = sum(1 for status, _ in report.values() if status == FAIL)
    errors = sum(1 for status, _ in report.values() if status == INFRA)
    lines.append(
        '<testsuites tests="{}" failures="{}" errors="{}">'.format(
            len(report), failures, errors
        )
    )
    lines.append(
        '<testsuite name="BATCH-F2P-0003" tests="{}" failures="{}" errors="{}">'.format(
            len(report), failures, errors
        )
    )
    for observation_id in sorted(report):
        status, detail = report[observation_id]
        lines.append(
            '<testcase classname="BATCH-F2P-0003" name="{}">'.format(
                _xml_attr(observation_id)
            )
        )
        if status == FAIL:
            lines.append(
                '<failure message="{}"/>'.format(_xml_attr(detail[:500]))
            )
        elif status == INFRA:
            lines.append(
                '<error message="{}"/>'.format(_xml_attr(detail[:500]))
            )
        lines.append("</testcase>")
    lines.append("</testsuite></testsuites>")

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv, default_side="target"):
    repo = os.environ.get("MIGRATION_REPO") or os.getcwd()
    report = None
    side = default_side
    args = list(argv)
    for index, arg in enumerate(args):
        if arg == "--repo" and index + 1 < len(args):
            repo = args[index + 1]
        elif arg == "--report" and index + 1 < len(args):
            report = args[index + 1]
        elif arg == "--side" and index + 1 < len(args):
            side = args[index + 1]
    positional = [a for a in args if not a.startswith("--")]
    # Tolerate either "<report>" or "<assets> <report>" invocation styles.
    if report is None:
        if positional:
            report = positional[-1]
        else:
            report = "/logs/result.json"

    report_map = evaluate(repo)
    write_report(report_map, report)
    failed = any(status == FAIL for status, _ in report_map.values())
    sys.stderr.write(
        "BATCH-F2P-0003 %s probe: repo=%s report=%s failed=%s\n"
        % (side, repo, report, failed)
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
