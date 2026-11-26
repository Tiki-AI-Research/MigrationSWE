#!/usr/bin/env python3
import json
import os
import pty
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))
from diff_contract import diff_matches

OBSERVATION_IDS = [
    "F2P-054", "F2P-049", "F2P-050", "F2P-051", "F2P-052", "F2P-053",
    "F2P-055", "F2P-056", "F2P-057", "F2P-058", "F2P-059", "F2P-060",
    "F2P-061", "F2P-062", "F2P-063", "F2P-064", "F2P-065", "F2P-066",
    "F2P-067", "F2P-068", "F2P-069", "F2P-070", "F2P-071", "F2P-072",
    "F2P-073", "F2P-074", "F2P-075", "F2P-076", "F2P-077", "F2P-078",
    "F2P-079", "F2P-080", "F2P-081", "F2P-082", "F2P-083", "F2P-084",
    "F2P-085", "F2P-086", "F2P-087", "F2P-088",
]


class CapabilityMissing(Exception):
    pass


class ProbeFailure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ProbeFailure(message)


def _one_line(value, limit=1200):
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    text = text.replace("\n", "\\n")
    return text[:limit]


class Context:
    def __init__(self, repo, binary=None, report=None):
        self.repo = Path(repo).resolve()
        self.report = report
        self.tmp_root = Path(tempfile.mkdtemp(prefix="f2p0003-"))
        self.bin_dir = self.tmp_root / "bin"
        self.bin_dir.mkdir()
        self._deps = None
        self._buildinfo = None
        self._binary = Path(binary).resolve() if binary else None
        self._min_binary = None
        self._tech = {}

    def cleanup(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def tempdir(self, name="case"):
        path = Path(tempfile.mkdtemp(prefix=name + "-", dir=str(self.tmp_root)))
        return path

    def has_go_module(self):
        manifest = self.repo / "go.mod"
        return manifest.is_file()

    def _normal_env(self):
        env = dict(os.environ)
        env["CGO_ENABLED"] = "1"
        env.setdefault("GOTOOLCHAIN", "local")
        return env

    def _minimal_env(self):
        home = self.tmp_root / "home"
        tmp = self.tmp_root / "tmp"
        home.mkdir(exist_ok=True)
        tmp.mkdir(exist_ok=True)
        return {
            "PATH": str(self.bin_dir),
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    def _command(self, cmd, cwd=None, input_bytes=None, env=None, timeout=180, stdin_devnull=False):
        kwargs = {
            "cwd": str(cwd or self.repo),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env or self._normal_env(),
            "timeout": timeout,
        }
        if stdin_devnull:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_bytes
        return subprocess.run([str(part) for part in cmd], **kwargs)

    def ensure_binary(self):
        if self._binary is not None:
            require(self._binary.is_file(), "configured binary does not exist: %s" % self._binary)
            self._install_minimal_binary()
            return self._binary
        require(self.has_go_module(), "target technology missing: go.mod is absent")
        result = self._command(
            ["go", "build", "-mod=vendor", "-buildvcs=false", "-o", str(self.tmp_root / "rubyfmt"), "./cmd/rubyfmt"],
            timeout=600,
        )
        require(result.returncode == 0, "go build failed: %s" % _one_line(result.stderr))
        self._binary = self.tmp_root / "rubyfmt"
        require(self._binary.is_file(), "go build produced no executable")
        self._install_minimal_binary()
        return self._binary

    def _install_minimal_binary(self):
        if self._min_binary is not None:
            return
        destination = self.bin_dir / "rubyfmt"
        shutil.copy2(str(self._binary), str(destination))
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        self._min_binary = destination

    def run(self, args, input_bytes=None, cwd=None, timeout=120, stdin_devnull=False):
        self.ensure_binary()
        return self._command(
            [str(self._min_binary)] + [str(arg) for arg in args],
            cwd=cwd,
            input_bytes=input_bytes,
            env=self._minimal_env(),
            timeout=timeout,
            stdin_devnull=stdin_devnull,
        )

    def run_ruby(self, args, cwd=None, timeout=120, input_bytes=None):
        return self._command(
            ["ruby"] + [str(arg) for arg in args],
            cwd=cwd,
            input_bytes=input_bytes,
            env=self._normal_env(),
            timeout=timeout,
        )

    def deps(self):
        if self._deps is None:
            require(self.has_go_module(), "target technology missing: go.mod is absent")
            result = self._command(["go", "list", "-deps", "./cmd/rubyfmt"], timeout=180)
            require(result.returncode == 0, "go list -deps failed: %s" % _one_line(result.stderr))
            self._deps = [line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        return list(self._deps)

    def buildinfo(self):
        if self._buildinfo is None:
            binary = self.ensure_binary()
            result = self._command(["go", "version", "-m", str(binary)], timeout=120)
            require(result.returncode == 0, "go version -m failed: %s" % _one_line(result.stderr))
            self._buildinfo = result.stdout.decode("utf-8", "replace")
        return self._buildinfo

    def ldd(self):
        binary = self.ensure_binary()
        result = self._command(["ldd", str(binary)], timeout=60)
        return (result.returncode, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"))

    def require_go_tech(self, prism=True, difflib=False, cli=True, formatter=True):
        key = (prism, difflib, cli, formatter)
        if key in self._tech:
            return self._tech[key]
        deps = self.deps()
        dep_set = set(deps)
        require("rubyfmt/cmd/rubyfmt" in dep_set, "Go runtime entry rubyfmt/cmd/rubyfmt is absent from go list -deps")
        if cli:
            require("rubyfmt/internal/cli" in dep_set, "Go CLI package rubyfmt/internal/cli is absent from go list -deps")
        if formatter:
            require("rubyfmt/internal/formatter" in dep_set, "Go formatter package rubyfmt/internal/formatter is absent from go list -deps")
        if prism:
            require("rubyfmt/internal/prism" in dep_set, "vendored Prism package rubyfmt/internal/prism is absent from go list -deps")
            require("runtime/cgo" in dep_set, "cgo runtime is absent from the target dependency graph")
            build = self.buildinfo()
            require("CGO_ENABLED=1" in build, "binary build metadata does not show CGO_ENABLED=1")
        if difflib:
            require("github.com/pmezard/go-difflib/difflib" in dep_set, "Go difflib package is absent from the target dependency graph")
        forbidden = [dep for dep in deps if any(token in dep for token in ("librubyfmt", "ruby-prism", "rubyfmt-main"))]
        require(not forbidden, "Rust formatter packages remain in dependency graph: %s" % ", ".join(forbidden))
        # A standard-library capability is not evidence of a legacy wrapper.
        # The hard gate scans production call sites for actual Rust fallback;
        # the isolated runtime probes also run without Ruby/Rust on PATH.
        build = self.buildinfo()
        require(re.search(r"(?m)^\s*path\s+rubyfmt/cmd/rubyfmt\s*$", build) is not None, "binary was not built from the Go rubyfmt/cmd/rubyfmt entry point")
        require(re.search(r"(?m)^\s*mod\s+rubyfmt\s+", build) is not None, "binary build metadata does not identify the rubyfmt Go module")
        details = {
            "entry": "rubyfmt/cmd/rubyfmt",
            "deps": [dep for dep in deps if dep.startswith("rubyfmt/") or dep.startswith("github.com/pmezard/") or dep == "runtime/cgo"],
            "cgo": "CGO_ENABLED=1" in build if prism else None,
            "difflib": difflib,
        }
        self._tech[key] = details
        return details

    def assert_format(self, text, expected, args=None, cwd=None):
        result = self.run(args or [], input_bytes=text, cwd=cwd)
        require(result.returncode == 0, "formatting exited %d: %s" % (result.returncode, _one_line(result.stderr)))
        require(result.stdout == expected, "formatting output %r != %r" % (result.stdout, expected))
        return result


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    path.write_bytes(data)


def _ignored_stdin_case(ctx):
    case = ctx.tempdir("ignored")
    _write(case / ".rubyfmtignore", "ignored.rb\n")
    passthrough = ctx.run(["--stdin-filepath", "ignored.rb"], input_bytes=b"a 1,2,3", cwd=case)
    require(passthrough.returncode == 0, "--stdin-filepath ignored.rb exited %d: %s" % (passthrough.returncode, _one_line(passthrough.stderr)))
    require(passthrough.stdout == b"a 1,2,3", "ignored stdin was not passed through unchanged: %r" % passthrough.stdout)
    check = ctx.run(["--check", "--stdin-filepath", "ignored.rb"], input_bytes=b"a 1,2,3", cwd=case)
    require(check.returncode == 0, "ignored --check exited %d: %s" % (check.returncode, _one_line(check.stderr)))
    require(check.stdout == b"", "ignored --check emitted output: %r" % check.stdout)


def _inplace_mtime_case(ctx):
    case = ctx.tempdir("inplace")
    unchanged = case / "unchanged.rb"
    changed = case / "changed.rb"
    _write(unchanged, b"a(1, 2, 3)\n")
    _write(changed, b"a 1,2,3\n")
    old = 1_600_000_000_000_000_000
    os.utime(str(unchanged), ns=(old, old))
    os.utime(str(changed), ns=(old, old))
    before_unchanged = unchanged.stat().st_mtime_ns
    before_changed = changed.stat().st_mtime_ns
    result = ctx.run(["-i", "unchanged.rb", "changed.rb"], cwd=case)
    require(result.returncode == 0, "-i exited %d: %s" % (result.returncode, _one_line(result.stderr)))
    require(unchanged.read_bytes() == b"a(1, 2, 3)\n", "unchanged file contents changed")
    require(changed.read_bytes() == b"a(1, 2, 3)\n", "changed file was not formatted")
    require(unchanged.stat().st_mtime_ns == before_unchanged, "unchanged file mtime moved from %d to %d" % (before_unchanged, unchanged.stat().st_mtime_ns))
    require(changed.stat().st_mtime_ns > before_changed, "changed file mtime did not advance")


def _fail_fast_case(ctx):
    case = ctx.tempdir("failfast")
    _write(case / "bad.rb", b"a 1,2,,\n")
    _write(case / "good.rb", b"a(1, 2)\n")
    plain = ctx.run(["bad.rb", "good.rb"], cwd=case)
    require(plain.returncode == 0, "plain mode exited %d on a named syntax error: %s" % (plain.returncode, _one_line(plain.stderr)))
    require(plain.stdout == b"a(1, 2)\n", "plain mode did not continue to good.rb: %r" % plain.stdout)
    bad = ctx.run(["--fail-fast", "bad.rb"], cwd=case)
    require(bad.returncode == 1, "--fail-fast syntax error exited %d, want 1" % bad.returncode)
    missing = ctx.run(["--fail-fast", "missing.rb"], cwd=case)
    require(missing.returncode == 3, "--fail-fast missing input exited %d, want 3" % missing.returncode)
    missing_list = ctx.run(["--fail-fast", "@missing-list.txt"], cwd=case)
    require(missing_list.returncode == 3, "--fail-fast missing @file exited %d, want 3" % missing_list.returncode)


def _check_diff_case(ctx):
    case = ctx.tempdir("diff")
    _write(case / "test.rb", b"a 1,2,3\n")
    result = ctx.run(["--check", "test.rb"], cwd=case)
    require(result.returncode == 5, "--check exited %d, want 5: %s" % (result.returncode, _one_line(result.stderr)))
    require(diff_matches(result.stdout, [("test.rb", b"a 1,2,3\n", b"a(1, 2, 3)\n")]), "invalid unified diff %r" % result.stdout)


def _stdin_file_case(ctx):
    stdin_result = ctx.assert_format(b"a 1,2,3", b"a(1, 2, 3)\n")
    case = ctx.tempdir("file")
    _write(case / "test.rb", b"a 1,2,3\n")
    file_result = ctx.run(["test.rb"], cwd=case)
    require(file_result.returncode == 0, "file formatting exited %d: %s" % (file_result.returncode, _one_line(file_result.stderr)))
    require(file_result.stdout == b"a(1, 2, 3)\n", "file formatting output %r" % file_result.stdout)
    return stdin_result, file_result


def _prism_case(ctx, expected=b"a(1, 2, 3)\n"):
    ctx.require_go_tech(prism=True)
    result = ctx.run([], input_bytes=b"a 1,2,3")
    require(result.returncode == 0, "elided-ruby formatting exited %d: %s" % (result.returncode, _one_line(result.stderr)))
    require(result.stdout == expected, "elided-ruby formatting output %r != %r" % (result.stdout, expected))
    return result


def _stress_case(ctx):
    ctx.require_go_tech(prism=True)
    for name in ("methods_stress_test.rb", "string_literals_stress_test.rb", "array_literals_stress_test.rb"):
        source = ctx.repo / "ci" / name
        require(source.is_file(), "missing ci stress script: %s" % source)
        case = ctx.tempdir("stress")
        original = case / ("original_" + name)
        formatted = case / ("formatted_" + name)
        _write(original, source.read_bytes())
        before = ctx.run_ruby([str(original)], cwd=case)
        require(before.returncode == 0, "ruby failed before formatting %s: %s" % (name, _one_line(before.stderr)))
        format_result = ctx.run([str(original)], cwd=case)
        require(format_result.returncode == 0, "formatting %s exited %d: %s" % (name, format_result.returncode, _one_line(format_result.stderr)))
        _write(formatted, format_result.stdout)
        after = ctx.run_ruby([str(formatted)], cwd=case)
        require(after.returncode == 0, "ruby failed after formatting %s: %s" % (name, _one_line(after.stderr)))
        require(after.stdout == before.stdout, "stress script %s changed runtime output" % name)


def _directory_file_list_case(ctx):
    ctx.require_go_tech(prism=True)
    case = ctx.tempdir("entrypoints")
    directory = case / "src"
    target = directory / "test.rb"
    _write(target, b"a 1,2,3\n")
    stdin_result = ctx.assert_format(b"a 1,2,3", b"a(1, 2, 3)\n")
    file_result = ctx.run([str(target)], cwd=case)
    require(file_result.returncode == 0 and file_result.stdout == b"a(1, 2, 3)\n", "file entry point failed: %r %s" % (file_result.stdout, _one_line(file_result.stderr)))
    directory_result = ctx.run([str(directory)], cwd=case)
    require(directory_result.returncode == 0 and directory_result.stdout == b"a(1, 2, 3)\n", "directory entry point failed: %r %s" % (directory_result.stdout, _one_line(directory_result.stderr)))
    list_file = case / "list.txt"
    _write(list_file, str(target) + "\n")
    list_result = ctx.run(["@" + str(list_file)], cwd=case)
    require(list_result.returncode == 0 and list_result.stdout == b"a(1, 2, 3)\n", "@file entry point failed: %r %s" % (list_result.stdout, _one_line(list_result.stderr)))
    return stdin_result, file_result, directory_result, list_result


def _library_case(ctx):
    ctx.require_go_tech(prism=True)
    probe = ctx.tmp_root / "probe_formatbuffer_test.go"
    # Assert only the documented public behavior: the library entry point
    # returns formatted bytes, and a syntax error that exposes its documented
    # exit code. Do not pin concrete error-type or helper-symbol names, so an
    # equivalent Go API (e.g. ErrSyntax + errors.Is) is accepted.
    _write(probe, '''package rubyfmt_test

import (
\t"bytes"
\t"testing"

\trubyfmt "rubyfmt"
)

var _ func([]byte) ([]byte, error) = rubyfmt.FormatBuffer

func TestProbeFormatBufferPublicEntryPoint(t *testing.T) {
\tout, err := rubyfmt.FormatBuffer([]byte("a 1,2,3"))
\tif err != nil {
\t\tt.Fatalf("unexpected error: %v", err)
\t}
\tif !bytes.Equal(out, []byte("a(1, 2, 3)\\n")) {
\t\tt.Fatalf("unexpected output: %q", out)
\t}
\t_, syntaxErr := rubyfmt.FormatBuffer([]byte("a 1,2,,"))
\tif syntaxErr == nil {
\t\tt.Fatal("expected a syntax error")
\t}
\tcoded, ok := interface{}(syntaxErr).(interface{ ExitCode() int })
\tif !ok {
\t\tt.Fatalf("syntax error does not expose an exit code: %T", syntaxErr)
\t}
\tif coded.ExitCode() != 1 {
\t\tt.Fatalf("unexpected syntax exit code: %d", coded.ExitCode())
\t}
}
''')
    overlay = ctx.tmp_root / "overlay.json"
    _write(overlay, json.dumps({"Replace": {str(ctx.repo / "probe_formatbuffer_test.go"): str(probe)}}))
    result = ctx._command(
        ["go", "test", "-mod=vendor", "-overlay", str(overlay), "-run", "TestProbeFormatBufferPublicEntryPoint", "."],
        timeout=600,
    )
    require(result.returncode == 0, "Go library FormatBuffer contract failed: %s" % _one_line(result.stdout + result.stderr))
    return result


def _devnull_terminal_case(ctx):
    ctx.require_go_tech(prism=True)
    devnull = ctx.run([], stdin_devnull=True)
    require(devnull.returncode == 0, "stdin=/dev/null exited %d: %s" % (devnull.returncode, _one_line(devnull.stderr)))
    require(b"Usage: rubyfmt" not in devnull.stdout, "stdin=/dev/null printed help")
    require(devnull.stdout in (b"", b"\n"), "stdin=/dev/null produced unexpected bytes: %r" % devnull.stdout)
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [str(ctx._min_binary)],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=ctx._minimal_env(),
        )
        os.close(slave)
        slave = -1
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ProbeFailure("terminal stdin invocation timed out")
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)
    require(process.returncode == 0, "terminal stdin exited %d: %s" % (process.returncode, _one_line(stderr)))
    require(b"Usage: rubyfmt" in stdout, "terminal stdin did not print help: %r" % stdout)
    return stdout


def _named_warning_case(ctx):
    case = ctx.tempdir("warnings")
    _write(case / "bad.rb", b"a 1,2,,\n")
    _write(case / "good.rb", b"a(1, 2)\n")
    result = ctx.run(["bad.rb", "missing.rb", "good.rb"], cwd=case)
    require(result.returncode == 0, "named warnings exited %d: %s" % (result.returncode, _one_line(result.stderr)))
    require(result.stdout == b"a(1, 2)\n", "named warnings did not continue to good.rb: %r" % result.stdout)
    stderr = result.stderr.decode("utf-8", "replace")
    require("bad.rb" in stderr, "syntax-error warning did not name bad.rb: %s" % _one_line(result.stderr))
    require("missing.rb" in stderr, "IO warning did not name missing.rb: %s" % _one_line(result.stderr))


def _stdin_filepath_conflict_case(ctx):
    case = ctx.tempdir("conflict")
    _write(case / ".rubyfmtignore", "ignored.rb\n")
    _write(case / "test.rb", b"a 1,2,3\n")
    with_path = ctx.run(["--stdin-filepath", "ignored.rb", "test.rb"], input_bytes=b"a 1,2,3", cwd=case)
    require(with_path.returncode != 0, "--stdin-filepath with a positional path did not fail")
    require("stdin-filepath" in with_path.stderr.decode("utf-8", "replace"), "conflict error did not name --stdin-filepath: %s" % _one_line(with_path.stderr))
    with_inplace = ctx.run(["--stdin-filepath", "ignored.rb", "-i"], input_bytes=b"a 1,2,3", cwd=case)
    require(with_inplace.returncode != 0, "--stdin-filepath with -i did not fail")
    require("stdin-filepath" in with_inplace.stderr.decode("utf-8", "replace"), "conflict error did not name --stdin-filepath: %s" % _one_line(with_inplace.stderr))


def _stdin_syntax_error_case(ctx):
    result = ctx.run([], input_bytes=b"a 1,2,,")
    require(result.returncode == 1, "stdin syntax error exited %d, want 1" % result.returncode)
    require("syntax error" in result.stderr.decode("utf-8", "replace").lower(), "stdin syntax error text missing: %s" % _one_line(result.stderr))


def _ignore_override_case(ctx):
    case = ctx.tempdir("ignore")
    _write(case / ".rubyfmtignore", "named.rb\n")
    _write(case / "named.rb", b"a 1,2,3\n")
    explicit = ctx.run(["named.rb"], cwd=case)
    require(explicit.returncode == 0 and explicit.stdout == b"a(1, 2, 3)\n", "explicitly named .rubyfmtignore file was not formatted: %r %s" % (explicit.stdout, _one_line(explicit.stderr)))
    git_case = ctx.tempdir("gitignore")
    git_init = ctx._command(["git", "init", "-q"], cwd=git_case, timeout=60)
    require(git_init.returncode == 0, "git init failed: %s" % _one_line(git_init.stderr))
    _write(git_case / ".gitignore", "ignored.rb\n!always.rb\n")
    _write(git_case / ".rubyfmtignore", "always.rb\n")
    _write(git_case / "always.rb", b"a 4,5,6\n")
    _write(git_case / "ignored.rb", b"a 1,2,3\n")
    _write(git_case / "kept.rb", b"a 1,2,3\n")
    without = ctx.run(["."], cwd=git_case)
    require(without.returncode == 0, "plain directory traversal exited %d: %s" % (without.returncode, _one_line(without.stderr)))
    require(without.stdout == b"a(1, 2, 3)\n", "plain traversal formatted a gitignored file: %r" % without.stdout)
    included = ctx.run(["--include-gitignored", "."], cwd=git_case)
    require(included.returncode == 0, "--include-gitignored exited %d: %s" % (included.returncode, _one_line(included.stderr)))
    require(included.stdout.count(b"a(1, 2, 3)\n") == 2, "--include-gitignored did not format both files: %r" % included.stdout)


def _fixture_case(ctx):
    ctx.require_go_tech(prism=True)
    roots = [ctx.repo / "fixtures" / "small", ctx.repo / "fixtures" / "large"]
    checked = 0
    for root in roots:
        require(root.is_dir(), "missing fixture directory: %s" % root)
        for actual in sorted(root.glob("*_actual.rb")):
            expected = actual.with_name(actual.name.replace("_actual.rb", "_expected.rb"))
            require(expected.is_file(), "missing expected fixture for %s" % actual)
            expected_bytes = expected.read_bytes()
            from_actual = ctx.run([str(actual)], cwd=ctx.repo)
            require(from_actual.returncode == 0, "formatting %s exited %d: %s" % (actual.name, from_actual.returncode, _one_line(from_actual.stderr)))
            require(from_actual.stdout == expected_bytes, "formatting %s did not match %s" % (actual.name, expected.name))
            from_expected = ctx.run([str(expected)], cwd=ctx.repo)
            require(from_expected.returncode == 0, "formatting %s exited %d: %s" % (expected.name, from_expected.returncode, _one_line(from_expected.stderr)))
            require(from_expected.stdout == expected_bytes, "formatting %s was not idempotent" % expected.name)
            checked += 1
    require(checked > 0, "no fixture pairs found")


def _test_repository(ctx, label):
    """Exercise migrated harnesses on a disposable copy, never on the candidate."""
    stage = ctx.tempdir(label) / "repo"
    shutil.copytree(
        ctx.repo, stage, symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".cache", "__pycache__"),
    )
    return stage


def _migrated_fixture_harness_case(ctx):
    # The Rust harness enumerated both halves and failed if either was absent.
    # Merely running it on the intact fixture tree cannot detect a Go port that
    # silently filters out incomplete pairs. Check both original assertions.
    stage = _test_repository(ctx, "fixture-harness")
    command = ["go", "test", "-mod=vendor", "-buildvcs=false", "-count=1", "./internal/formatter"]
    baseline = ctx._command(command, cwd=stage, timeout=600)
    require(baseline.returncode == 0, "migrated fixture harness baseline failed: %s" % _one_line(baseline.stdout + baseline.stderr))
    for group in ("small", "large"):
        actuals = sorted((stage / "fixtures" / group).rglob("*_actual.rb"))
        require(actuals, "no %s fixtures available to check migrated harness assertions" % group)
        actual = actuals[0]
        expected = actual.with_name(actual.name[:-len("_actual.rb")] + "_expected.rb")
        require(expected.is_file(), "fixture harness control pair is incomplete")
        for path in (actual, expected):
            saved = ctx.tempdir("removed-fixture") / path.name
            path.rename(saved)
            try:
                result = ctx._command(command, cwd=stage, timeout=600)
                require(result.returncode == 1, "migrated Go tests did not reject a missing fixture half (%s): exit %d; %s" % (
                    path.relative_to(stage), result.returncode, _one_line(result.stdout + result.stderr)))
            finally:
                saved.rename(path)
            restored = ctx._command(command, cwd=stage, timeout=600)
            require(restored.returncode == 0, "restored fixture harness control failed: %s" % _one_line(restored.stdout + restored.stderr))


def _migrated_benchmark_case(ctx):
    # go test ./... only compiles benchmarks. The original benchmarks read and
    # format all three stress scripts, failing on any missing input. Smoke-run
    # them once, then verify each read assertion on an isolated repository.
    stage = _test_repository(ctx, "benchmark-harness")
    command = ["go", "test", "-mod=vendor", "-buildvcs=false", "-count=1",
               "-run=^$", "-bench=.", "-benchtime=1x", "./internal/formatter"]
    baseline = ctx._command(command, cwd=stage, timeout=600)
    require(baseline.returncode == 0, "migrated benchmark baseline failed: %s" % _one_line(baseline.stdout + baseline.stderr))
    require(re.search(rb"(?m)^Benchmark\S+\s+", baseline.stdout) is not None,
            "migrated formatter package contains no executable benchmarks")
    for name in ("methods_stress_test.rb", "array_literals_stress_test.rb", "string_literals_stress_test.rb"):
        path = stage / "ci" / name
        require(path.is_file(), "missing benchmark input: %s" % name)
        saved = ctx.tempdir("removed-benchmark") / name
        path.rename(saved)
        try:
            result = ctx._command(command, cwd=stage, timeout=600)
            require(result.returncode == 1, "migrated benchmarks omitted the stress input %s: exit %d; %s" % (
                name, result.returncode, _one_line(result.stdout + result.stderr)))
        finally:
            saved.rename(path)


def _dependency_policy_case(ctx):
    details = ctx.require_go_tech(prism=True)
    result = ctx.run([], input_bytes=b"a 1,2,3")
    require(result.returncode == 0, "minimal-PATH formatting exited %d: %s" % (result.returncode, _one_line(result.stderr)))
    require(result.stdout == b"a(1, 2, 3)\n", "minimal-PATH formatting output %r" % result.stdout)
    ldd_code, ldd_text = ctx.ldd()
    require(not re.search(r"librubyfmt|ruby-prism|rubyfmt-main", ldd_text), "binary dynamically loads a Rust formatter: %s" % _one_line(ldd_text))
    for name in ("ruby", "cargo", "rustc", "rubyfmt-main"):
        require(shutil.which(name, path=ctx._minimal_env()["PATH"]) is None, "%s unexpectedly present on the isolated PATH" % name)
    return details, result


def _cli_contract_case(ctx):
    ctx.require_go_tech(prism=True, difflib=True)
    version = ctx.run(["-V"])
    require(version.returncode == 0 and version.stdout.startswith(b"rubyfmt "), "-V did not identify rubyfmt: %r" % version.stdout)
    version_long = ctx.run(["--version"])
    require(version_long.returncode == 0 and version_long.stdout.startswith(b"rubyfmt "), "--version did not identify rubyfmt: %r" % version_long.stdout)
    help_short = ctx.run(["-h"])
    require(help_short.returncode == 0 and b"Usage: rubyfmt" in help_short.stdout, "-h did not print help")
    help_long = ctx.run(["--help"])
    require(help_long.returncode == 0 and b"Usage: rubyfmt" in help_long.stdout, "--help did not print help")
    case = ctx.tempdir("cli")
    _write(case / "changed.rb", b"a 1,2,3\n")
    check_result = ctx.run(["-c", "changed.rb"], cwd=case)
    require(check_result.returncode == 5, "-c exited %d, want 5" % check_result.returncode)
    inplace_result = ctx.run(["-i", "changed.rb"], cwd=case)
    require(inplace_result.returncode == 0, "-i exited %d: %s" % (inplace_result.returncode, _one_line(inplace_result.stderr)))
    require((case / "changed.rb").read_bytes() == b"a(1, 2, 3)\n", "-i did not rewrite changed.rb")
    _write(case / "opt_in.rb", b"# rubyfmt: true\na 1,2,3\n")
    opt_in = ctx.run(["--header-opt-in", "opt_in.rb"], cwd=case)
    require(opt_in.returncode == 0 and opt_in.stdout == b"# rubyfmt: true\na(1, 2, 3)\n", "--header-opt-in did not format opted-in input: %r %s" % (opt_in.stdout, _one_line(opt_in.stderr)))
    _write(case / "opt_out.rb", b"# rubyfmt: false\na 1,2,3\n")
    opt_out = ctx.run(["--header-opt-out", "opt_out.rb"], cwd=case)
    require(opt_out.returncode == 0 and opt_out.stdout == b"# rubyfmt: false\na 1,2,3\n", "--header-opt-out did not skip opted-out input: %r %s" % (opt_out.stdout, _one_line(opt_out.stderr)))
    _write(case / ".rubyfmtignore", "ignored.rb\n")
    _write(case / "ignored.rb", b"a 1,2,3\n")
    explicit = ctx.run(["ignored.rb"], cwd=case)
    require(explicit.returncode == 0 and explicit.stdout == b"a(1, 2, 3)\n", "explicitly named ignored file was not formatted: %r %s" % (explicit.stdout, _one_line(explicit.stderr)))
    return version, check_result, inplace_result, opt_in, opt_out, explicit


def _go_list_case(ctx):
    details = ctx.require_go_tech(prism=True)
    deps = ctx.deps()
    require("rubyfmt/internal/cli" in deps, "rubyfmt/internal/cli missing from go list -deps")
    require("rubyfmt/internal/formatter" in deps, "rubyfmt/internal/formatter missing from go list -deps")
    require(not any("librubyfmt" in dep or "ruby-prism" in dep or "rubyfmt-main" in dep for dep in deps), "Rust formatter package appears in go list -deps")
    result = ctx.run([], input_bytes=b"a 1,2,3")
    require(result.returncode == 0 and result.stdout == b"a(1, 2, 3)\n", "isolated formatting failed: %r %s" % (result.stdout, _one_line(result.stderr)))
    return details, deps, result


def _build_dependency_case(ctx):
    binary = ctx.ensure_binary()
    ctx.require_go_tech(prism=True)
    deps = ctx.deps()
    require("rubyfmt/internal/prism" in deps, "rubyfmt/internal/prism missing from go list -deps")
    require(not any("librubyfmt" in dep or "ruby-prism" in dep or "rubyfmt-main" in dep for dep in deps), "Rust formatter package appears in go list -deps")
    result = ctx.run([], input_bytes=b"a 1,2,3")
    require(result.returncode == 0 and result.stdout == b"a(1, 2, 3)\n", "built binary formatting failed: %r %s" % (result.stdout, _one_line(result.stderr)))
    return binary, deps, result


def _check_stdin_diff_case(ctx):
    """Stdin ``--check`` must emit the unified diff for ``stdin`` and exit 5."""
    result = ctx.run(["--check"], input_bytes=b"a 1,2,3\n")
    require(result.returncode == 5, "stdin --check exited %d, want 5: %s" % (result.returncode, _one_line(result.stderr)))
    require(diff_matches(result.stdout, [("stdin", b"a 1,2,3\n", b"a(1, 2, 3)\n")]), "invalid stdin --check diff %r" % result.stdout)


def _check_diff_without_final_newline_case(ctx):
    # A missing newline is part of the original source, including when adding
    # that newline is the only change. A diff must represent it without merging
    # the removed and added lines or claiming an extra source line.
    modes = (
        (["--check"], "stdin"),
        (["--check", "test.rb"], "test.rb"),
        (["--check", "--stdin-filepath", "virtual.rb"], "virtual.rb"),
        (["--check", "inner"], "inner/test.rb"),
        (["--check", "@paths.txt"], "inner/test.rb"),
    )
    for source in (b"a 1,2,3", b"a(1, 2, 3)"):
        for args, name in modes:
            case = ctx.tempdir("check-no-final-newline")
            _write(case / "test.rb", source)
            _write(case / "inner" / "test.rb", source)
            _write(case / "paths.txt", b"inner/test.rb\n")
            result = ctx.run(args, input_bytes=source, cwd=case)
            require(result.returncode == 5, "no-final-newline %s exited %d, want 5: %s" % (args, result.returncode, _one_line(result.stderr)))
            require(diff_matches(result.stdout, [(name, source, b"a(1, 2, 3)\n")]), "invalid no-final-newline %s diff %r" % (args, result.stdout))


def _check_multi_file_case(ctx):
    case = ctx.tempdir("check-multi")
    _write(case / "one.rb", b"a 1,2,3\n")
    _write(case / "two.rb", b"a 4,5,6,7\n")
    result = ctx.run(["--check", "one.rb", "two.rb"], cwd=case)
    require(result.returncode == 5, "--check two files exited %d, want 5: %s" % (result.returncode, _one_line(result.stderr)))
    require(diff_matches(result.stdout, [("one.rb", b"a 1,2,3\n", b"a(1, 2, 3)\n"),
                                         ("two.rb", b"a 4,5,6,7\n", b"a(4, 5, 6, 7)\n")]), "invalid --check two files output %r" % result.stdout)


def _check_directory_case(ctx):
    case = ctx.tempdir("check-directory")
    inner = case / "inner"
    _write(inner / "test.rb", b"a 1,2,3\n")
    result = ctx.run(["--check", "inner"], cwd=case)
    require(result.returncode == 5, "--check directory exited %d, want 5: %s" % (result.returncode, _one_line(result.stderr)))
    require(diff_matches(result.stdout, [("inner/test.rb", b"a 1,2,3\n", b"a(1, 2, 3)\n")]), "invalid --check directory output %r" % result.stdout)


def _check_at_file_case(ctx):
    case = ctx.tempdir("check-at-file")
    inner = case / "inner"
    _write(inner / "test.rb", b"a 1,2,3\n")
    _write(case / "paths.txt", b"inner/test.rb\n")
    result = ctx.run(["--check", "@paths.txt"], cwd=case)
    require(result.returncode == 5, "--check @file exited %d, want 5: %s" % (result.returncode, _one_line(result.stderr)))
    require(diff_matches(result.stdout, [("inner/test.rb", b"a 1,2,3\n", b"a(1, 2, 3)\n")]), "invalid --check @file output %r" % result.stdout)
    # Path-list entries expand into explicitly named inputs, just as in the
    # original CLI. Ignore rules apply to traversal, not these named files.
    _write(case / ".rubyfmtignore", b"inner/\n")
    checked = ctx.run(["--check", "@paths.txt"], cwd=case)
    require(checked.returncode == 5 and diff_matches(checked.stdout, [("inner/test.rb", b"a 1,2,3\n", b"a(1, 2, 3)\n")]),
            "explicit @file entry was ignored in check mode: exit %d, stdout %r" % (checked.returncode, checked.stdout))
    formatted = ctx.run(["@paths.txt"], cwd=case)
    require(formatted.returncode == 0 and formatted.stdout == b"a(1, 2, 3)\n",
            "explicit @file entry was ignored in stdout mode: exit %d, stdout %r" % (formatted.returncode, formatted.stdout))
    inplace = ctx.run(["-i", "@paths.txt"], cwd=case)
    require(inplace.returncode == 0 and inplace.stdout == b"" and (inner / "test.rb").read_bytes() == b"a(1, 2, 3)\n",
            "explicit @file entry was ignored in in-place mode")


def _check_clean_family_case(ctx):
    case = ctx.tempdir("check-clean")
    _write(case / "one.rb", b"a(1, 2, 3)\n")
    _write(case / "two.rb", b"a(4, 5, 6, 7)\n")
    multi = ctx.run(["--check", "one.rb", "two.rb"], cwd=case)
    require(multi.returncode == 0 and multi.stdout == b"", "--check clean files exited %d with %r" % (multi.returncode, multi.stdout))
    inner = case / "inner"
    _write(inner / "three.rb", b"a(1, 2, 3)\n")
    directory = ctx.run(["--check", "."], cwd=case)
    require(directory.returncode == 0 and directory.stdout == b"", "--check clean directory exited %d with %r" % (directory.returncode, directory.stdout))
    _write(case / "paths.txt", b"one.rb\n")
    at_file = ctx.run(["--check", "@paths.txt"], cwd=case)
    require(at_file.returncode == 0 and at_file.stdout == b"", "--check clean @file exited %d with %r" % (at_file.returncode, at_file.stdout))


def _check_header_case(ctx):
    opt_in = ctx.run(["--check", "--header-opt-in"], input_bytes=b"# rubyfmt: true\na 1,2,3\n")
    require(opt_in.returncode == 5, "check --header-opt-in exited %d, want 5: %s" % (opt_in.returncode, _one_line(opt_in.stderr)))
    require(diff_matches(opt_in.stdout, [("stdin", b"# rubyfmt: true\na 1,2,3\n", b"# rubyfmt: true\na(1, 2, 3)\n")]), "invalid check --header-opt-in output %r" % opt_in.stdout)
    opt_out = ctx.run(["--check", "--header-opt-out"], input_bytes=b"# rubyfmt: false\na 1,2,3\n")
    require(opt_out.returncode == 0 and opt_out.stdout == b"", "check --header-opt-out exited %d with %r" % (opt_out.returncode, opt_out.stdout))


def _check_syntax_case(ctx):
    stdin_result = ctx.run(["--check"], input_bytes=b"def (")
    require(stdin_result.returncode == 1, "stdin --check syntax exited %d, want 1" % stdin_result.returncode)
    require("syntax error" in stdin_result.stderr.decode("utf-8", "replace").lower(), "stdin --check syntax text missing: %s" % _one_line(stdin_result.stderr))
    case = ctx.tempdir("check-syntax")
    _write(case / "bad.rb", b"a 1,2,,\n")
    named = ctx.run(["--check", "bad.rb"], cwd=case)
    require(named.returncode == 1, "--check named syntax exited %d, want 1" % named.returncode)


def _stdin_filepath_gitignore_dir_case(ctx):
    case = ctx.tempdir("stdin-gitignore-dir")
    (case / ".git").mkdir()
    _write(case / ".gitignore", b"ignored/\n")
    passthrough = ctx.run(["--stdin-filepath", "ignored/foo.rb"], input_bytes=b"a 1,2,3", cwd=case)
    require(passthrough.returncode == 0 and passthrough.stdout == b"a 1,2,3", "gitignore directory pattern stdin passthrough %r %s" % (passthrough.stdout, _one_line(passthrough.stderr)))
    included = ctx.run(["--stdin-filepath", "ignored/foo.rb", "--include-gitignored"], input_bytes=b"a 1,2,3", cwd=case)
    require(included.returncode == 0 and included.stdout == b"a(1, 2, 3)\n", "--include-gitignored stdin output %r %s" % (included.stdout, _one_line(included.stderr)))


def _stdin_filepath_rubyfmtignore_dir_case(ctx):
    case = ctx.tempdir("stdin-rubyfmtignore-dir")
    _write(case / ".rubyfmtignore", b"ignored/\n")
    passthrough = ctx.run(["--stdin-filepath", "ignored/foo.rb"], input_bytes=b"a 1,2,3", cwd=case)
    require(passthrough.returncode == 0 and passthrough.stdout == b"a 1,2,3", "rubyfmtignore directory pattern stdin passthrough %r %s" % (passthrough.stdout, _one_line(passthrough.stderr)))
    formatted = ctx.run(["--stdin-filepath", "not_ignored/foo.rb"], input_bytes=b"a 1,2,3", cwd=case)
    require(formatted.returncode == 0 and formatted.stdout == b"a(1, 2, 3)\n", "non-ignored stdin filepath output %r %s" % (formatted.stdout, _one_line(formatted.stderr)))


def _ancestor_ignore_case(ctx):
    for ignore_name in (".rubyfmtignore", ".gitignore"):
        for with_git in (True, False):
            case = ctx.tempdir("ancestor-ignore")
            if with_git:
                (case / ".git").mkdir()
            sub = case / "app"
            _write(sub / "ignored.rb", b"a 4,5,6\n")
            _write(sub / "kept.rb", b"a 1,2,3\n")
            _write(case / ignore_name, b"ignored.rb\n")
            result = ctx.run(["-i", "."], cwd=sub)
            label = "%s (with_git=%s)" % (ignore_name, with_git)
            require(result.returncode == 0 and result.stdout == b"", "ancestor %s -i exited %d with %r" % (label, result.returncode, result.stdout))
            require((sub / "ignored.rb").read_bytes() == b"a 4,5,6\n", "ancestor %s was not applied from a subdirectory" % label)
            require((sub / "kept.rb").read_bytes() == b"a(1, 2, 3)\n", "ancestor %s blocked a non-ignored file" % label)
            stdin = ctx.run(["--stdin-filepath", "ignored.rb"], input_bytes=b"a 4,5,6\n", cwd=sub)
            require(stdin.returncode == 0 and stdin.stdout == b"a 4,5,6\n", "ancestor %s did not ignore stdin filepath" % label)
            checked = ctx.run(["--check", "--stdin-filepath", "ignored.rb"], input_bytes=b"a 4,5,6\n", cwd=sub)
            require(checked.returncode == 0 and checked.stdout == b"", "ancestor %s ignored stdin --check was not silent" % label)


def _ignore_pattern_spaces_case(ctx):
    # Gitignore syntax discards unescaped trailing spaces, but an escaped
    # trailing space is part of the name. Use directories so both spellings
    # contain ordinary .rb files and must prune traversal before reading them.
    patterns = (
        (b"blocked   \n", "blocked"),
        (b"blocked\\ \n", "blocked "),
        (b"blocked\\    \n", "blocked "),
    )
    for ignore_name in (".rubyfmtignore", ".gitignore"):
        for pattern, directory in patterns:
            case = ctx.tempdir("ignore-pattern-spaces")
            (case / ".git").mkdir()
            _write(case / ignore_name, pattern)
            relative = directory + "/deep/nested.rb"
            path = case / relative
            original = b"a 4,5,6\n"
            _write(path, original)
            for entry in (".", directory, directory + "/deep"):
                for mode in ("--check", "-i"):
                    result = ctx.run([mode, entry], cwd=case)
                    require(result.returncode == 0 and result.stdout == b"",
                            "%s pattern %r failed to exclude %r in %s: exit %d, stdout %r" % (
                                ignore_name, pattern, entry, mode, result.returncode, result.stdout))
                    require(path.read_bytes() == original,
                            "%s pattern %r allowed an excluded directory to be rewritten" % (ignore_name, pattern))
            for check in (False, True):
                args = ["--stdin-filepath", relative]
                if check:
                    args.insert(0, "--check")
                result = ctx.run(args, input_bytes=original, cwd=case)
                require(result.returncode == 0 and result.stdout == (b"" if check else original),
                        "%s pattern %r did not apply to stdin filepath %r" % (ignore_name, pattern, relative))


def _ignored_directory_prune_case(ctx):
    case = ctx.tempdir("ignored-directory")
    ignored = case / "ignored"
    _write(ignored / "nested.rb", b"a 4,5,6\n")
    _write(case / ".rubyfmtignore", b"ignored/\n!ignored/nested.rb\n")
    _write(ignored / ".rubyfmtignore", b"!nested.rb\n")
    for entry in (".", "ignored"):
        result = ctx.run(["-i", entry], cwd=case)
        require(result.returncode == 0 and result.stdout == b"", "ignored-directory -i %s exited %d with %r" % (entry, result.returncode, result.stdout))
        require((ignored / "nested.rb").read_bytes() == b"a 4,5,6\n", "a negated pattern re-entered an ignored directory through %s" % entry)
    stdin = ctx.run(["--stdin-filepath", "ignored/nested.rb"], input_bytes=b"a 4,5,6\n", cwd=case)
    require(stdin.returncode == 0 and stdin.stdout == b"a 4,5,6\n", "file negation reopened stdin below an excluded directory")
    checked = ctx.run(["--check", "--stdin-filepath", "ignored/nested.rb"], input_bytes=b"a 4,5,6\n", cwd=case)
    require(checked.returncode == 0 and checked.stdout == b"", "excluded stdin under a directory produced a check diff")


def _ignored_nested_repository_case(ctx):
    # The ignore boundary comes from the working directory. A nested .git
    # below an excluded directory must not discard those already-active rules,
    # even when the traversal argument starts inside the nested repository.
    for ignore_name in (".rubyfmtignore", ".gitignore"):
        case = ctx.tempdir("ignored-nested-repository")
        (case / ".git").mkdir()
        (case / "ignored" / ".git").mkdir(parents=True)
        path = case / "ignored" / "deep" / "nested.rb"
        original = b"a 4,5,6\n"
        _write(path, original)
        _write(case / ignore_name, b"ignored/\n!ignored/deep/nested.rb\n")
        _write(case / "ignored" / ignore_name, b"!deep/nested.rb\n")
        for entry in (".", "ignored", "ignored/deep"):
            for mode in ("--check", "-i"):
                result = ctx.run([mode, entry], cwd=case)
                require(result.returncode == 0 and result.stdout == b"",
                        "%s lost the working-directory ignore at nested .git for %s %s: exit %d, stdout %r" % (
                            ignore_name, mode, entry, result.returncode, result.stdout))
                require(path.read_bytes() == original,
                        "%s re-entered an excluded nested repository through %s" % (ignore_name, entry))
        for check in (False, True):
            args = ["--stdin-filepath", "ignored/deep/nested.rb"]
            if check:
                args.insert(0, "--check")
            result = ctx.run(args, input_bytes=original, cwd=case)
            require(result.returncode == 0 and result.stdout == (b"" if check else original),
                    "%s lost the working-directory ignore for stdin below nested .git" % ignore_name)


def _multiple_directory_args_case(ctx):
    for arguments in (["app", "db"], ["db", "app"]):
        case = ctx.tempdir("multi-directory")
        _write(case / "app" / "application.rb", b"a 1, 2, 3\n")
        _write(case / "db" / "schema.rb", b"a 4, 5, 6\n")
        _write(case / ".rubyfmtignore", b"db/schema.rb\n")
        result = ctx.run(["-i", *arguments], cwd=case)
        require(result.returncode == 0 and result.stdout == b"", "multiple directory %s -i exited %d with %r" % (arguments, result.returncode, result.stdout))
        require((case / "app" / "application.rb").read_bytes() == b"a(1, 2, 3)\n", "non-ignored directory argument %s was not formatted" % arguments)
        require((case / "db" / "schema.rb").read_bytes() == b"a 4, 5, 6\n", "ignored file under directory argument %s was formatted" % arguments)


def _missing_at_file_case(ctx):
    case = ctx.tempdir("missing-at-file")
    plain = ctx.run(["@missing-list.txt"], cwd=case)
    require(plain.returncode == 3, "plain missing @file exited %d, want 3" % plain.returncode)
    checked = ctx.run(["--check", "@missing-list.txt"], cwd=case)
    require(checked.returncode == 3, "check missing @file exited %d, want 3" % checked.returncode)


def _inplace_header_case(ctx):
    case = ctx.tempdir("inplace-header")
    _write(case / "opt_in.rb", b"# rubyfmt: true\na 1, 2, 3\n")
    _write(case / "opt_out.rb", b"# rubyfmt: false\na 1, 2, 3\n")
    opted_in = ctx.run(["-i", "--header-opt-in", "opt_in.rb"], cwd=case)
    require(opted_in.returncode == 0 and opted_in.stdout == b"", "-i --header-opt-in exited %d with %r" % (opted_in.returncode, opted_in.stdout))
    opted_out = ctx.run(["-i", "--header-opt-out", "opt_out.rb"], cwd=case)
    require(opted_out.returncode == 0 and opted_out.stdout == b"", "-i --header-opt-out exited %d with %r" % (opted_out.returncode, opted_out.stdout))
    require((case / "opt_in.rb").read_bytes() == b"# rubyfmt: true\na(1, 2, 3)\n", "opted-in file was not formatted in place")
    require((case / "opt_out.rb").read_bytes() == b"# rubyfmt: false\na 1, 2, 3\n", "opted-out file was modified in place")


def _unknown_option_case(ctx):
    result = ctx.run(["--not-a-real-flag"])
    require(result.returncode != 0, "unknown option exited 0")
    stderr = result.stderr.decode("utf-8", "replace")
    stdout = result.stdout.decode("utf-8", "replace")
    require("rubyfmt" in stderr, "unknown option error did not identify rubyfmt: %s" % _one_line(result.stderr))
    require("rubyfmt-main" not in stderr and "rubyfmt-main" not in stdout, "unknown option error leaked the Rust binary name")


def _stdin_brace_syntax_case(ctx):
    result = ctx.run([], input_bytes=b'""}')
    require(result.returncode == 1, "stdin brace syntax exited %d, want 1" % result.returncode)
    require("syntax error" in result.stderr.decode("utf-8", "replace").lower(), "stdin brace syntax text missing: %s" % _one_line(result.stderr))


def check_f2p_049(ctx):
    _named_warning_case(ctx)
    _ignored_stdin_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_050(ctx):
    _inplace_mtime_case(ctx)
    _ignored_stdin_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_051(ctx):
    _fail_fast_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_052(ctx):
    _check_diff_case(ctx)
    _check_diff_without_final_newline_case(ctx)
    _stdin_filepath_conflict_case(ctx)
    ctx.require_go_tech(prism=True, difflib=True)


def check_f2p_053(ctx):
    _inplace_mtime_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_054(ctx):
    _dependency_policy_case(ctx)


def check_f2p_055(ctx):
    _fixture_case(ctx)
    _migrated_fixture_harness_case(ctx)
    _prism_case(ctx)


def check_f2p_056(ctx):
    _stdin_file_case(ctx)
    _prism_case(ctx)


def check_f2p_057(ctx):
    _dependency_policy_case(ctx)


def check_f2p_058(ctx):
    _ignored_stdin_case(ctx)
    _stdin_filepath_conflict_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_059(ctx):
    _stdin_syntax_error_case(ctx)
    _devnull_terminal_case(ctx)


def check_f2p_060(ctx):
    _dependency_policy_case(ctx)
    _library_case(ctx)


def check_f2p_061(ctx):
    _ignore_override_case(ctx)
    _library_case(ctx)


def check_f2p_062(ctx):
    _stress_case(ctx)
    _migrated_benchmark_case(ctx)


def check_f2p_063(ctx):
    _directory_file_list_case(ctx)


def check_f2p_064(ctx):
    _stdin_file_case(ctx)
    _prism_case(ctx)


def check_f2p_065(ctx):
    _fail_fast_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_066(ctx):
    _stdin_file_case(ctx)
    _prism_case(ctx)


def check_f2p_067(ctx):
    _stress_case(ctx)


def check_f2p_068(ctx):
    _cli_contract_case(ctx)


def check_f2p_069(ctx):
    _library_case(ctx)
    _directory_file_list_case(ctx)


def check_f2p_070(ctx):
    _go_list_case(ctx)
    _library_case(ctx)


def check_f2p_071(ctx):
    _build_dependency_case(ctx)


def check_f2p_072(ctx):
    _library_case(ctx)
    _directory_file_list_case(ctx)
    _stdin_file_case(ctx)
    _prism_case(ctx)


def check_f2p_073(ctx):
    _check_stdin_diff_case(ctx)
    ctx.require_go_tech(prism=True, difflib=True)


def check_f2p_074(ctx):
    _check_multi_file_case(ctx)
    ctx.require_go_tech(prism=True, difflib=True)


def check_f2p_075(ctx):
    _check_directory_case(ctx)
    ctx.require_go_tech(prism=True, difflib=True)


def check_f2p_076(ctx):
    _check_at_file_case(ctx)
    ctx.require_go_tech(prism=True, difflib=True)


def check_f2p_077(ctx):
    _check_clean_family_case(ctx)
    ctx.require_go_tech(prism=True, difflib=True)


def check_f2p_078(ctx):
    _check_header_case(ctx)
    ctx.require_go_tech(prism=True, difflib=True)


def check_f2p_079(ctx):
    _check_syntax_case(ctx)
    ctx.require_go_tech(prism=True, difflib=True)


def check_f2p_080(ctx):
    _stdin_filepath_gitignore_dir_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_081(ctx):
    _stdin_filepath_rubyfmtignore_dir_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_082(ctx):
    _ancestor_ignore_case(ctx)
    _ignore_pattern_spaces_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_083(ctx):
    _ignored_directory_prune_case(ctx)
    _ignored_nested_repository_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_084(ctx):
    _multiple_directory_args_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_085(ctx):
    _missing_at_file_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_086(ctx):
    _inplace_header_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_087(ctx):
    _unknown_option_case(ctx)
    ctx.require_go_tech(prism=True)


def check_f2p_088(ctx):
    _stdin_brace_syntax_case(ctx)
    ctx.require_go_tech(prism=True)


CHECKS = {
    "F2P-049": check_f2p_049,
    "F2P-050": check_f2p_050,
    "F2P-051": check_f2p_051,
    "F2P-052": check_f2p_052,
    "F2P-053": check_f2p_053,
    "F2P-054": check_f2p_054,
    "F2P-055": check_f2p_055,
    "F2P-056": check_f2p_056,
    "F2P-057": check_f2p_057,
    "F2P-058": check_f2p_058,
    "F2P-059": check_f2p_059,
    "F2P-060": check_f2p_060,
    "F2P-061": check_f2p_061,
    "F2P-062": check_f2p_062,
    "F2P-063": check_f2p_063,
    "F2P-064": check_f2p_064,
    "F2P-065": check_f2p_065,
    "F2P-066": check_f2p_066,
    "F2P-067": check_f2p_067,
    "F2P-068": check_f2p_068,
    "F2P-069": check_f2p_069,
    "F2P-070": check_f2p_070,
    "F2P-071": check_f2p_071,
    "F2P-072": check_f2p_072,
    "F2P-073": check_f2p_073,
    "F2P-074": check_f2p_074,
    "F2P-075": check_f2p_075,
    "F2P-076": check_f2p_076,
    "F2P-077": check_f2p_077,
    "F2P-078": check_f2p_078,
    "F2P-079": check_f2p_079,
    "F2P-080": check_f2p_080,
    "F2P-081": check_f2p_081,
    "F2P-082": check_f2p_082,
    "F2P-083": check_f2p_083,
    "F2P-084": check_f2p_084,
    "F2P-085": check_f2p_085,
    "F2P-086": check_f2p_086,
    "F2P-087": check_f2p_087,
    "F2P-088": check_f2p_088,
}


def run_probe(repo, binary=None, only=None):
    selected = list(only) if only else list(OBSERVATION_IDS)
    unknown = [oid for oid in selected if oid not in CHECKS]
    if unknown:
        raise SystemExit("unknown observation ids: %s" % ", ".join(unknown))
    ctx = Context(repo, binary=binary)
    results = []
    try:
        for oid in selected:
            started = time.time()
            try:
                CHECKS[oid](ctx)
                status = "PASS"
                reason = "target postcondition observed"
            except CapabilityMissing as exc:
                status = "FAIL"
                reason = str(exc)
            except ProbeFailure as exc:
                status = "FAIL"
                reason = str(exc)
            except subprocess.TimeoutExpired as exc:
                status = "INFRA_ERROR"
                reason = "command timed out: %s" % exc
            except FileNotFoundError as exc:
                status = "INFRA_ERROR"
                reason = "required executable is unavailable: %s" % exc
            except Exception as exc:
                status = "INFRA_ERROR"
                reason = "%s: %s" % (type(exc).__name__, exc)
            results.append({
                "observation_id": oid,
                "status": status,
                "reason": reason,
                "duration_seconds": round(time.time() - started, 3),
            })
    finally:
        ctx.cleanup()
    return results


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--binary")
    parser.add_argument("--observation", action="append", dest="observations")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = run_probe(args.repo, binary=args.binary, only=args.observations)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for item in results:
            print("%s %s %s" % (item["observation_id"], item["status"], item["reason"]))
    return 0 if all(item["status"] == "PASS" for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
