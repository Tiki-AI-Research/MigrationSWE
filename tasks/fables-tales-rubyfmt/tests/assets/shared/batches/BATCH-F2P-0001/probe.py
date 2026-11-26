#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))
from diff_contract import diff_matches

BATCH_ID = "BATCH-F2P-0001"

OBSERVATIONS = [
    "F2P-001", "F2P-002", "F2P-003", "F2P-004", "F2P-005", "F2P-007",
    "F2P-009", "F2P-013", "F2P-016", "F2P-019", "F2P-020", "F2P-021",
    "F2P-024", "F2P-006", "F2P-008", "F2P-010", "F2P-011", "F2P-012",
    "F2P-014", "F2P-015", "F2P-017", "F2P-018", "F2P-022", "F2P-023",
]

CHECK_FOR_OBSERVATION = {
    "F2P-001": "missing_atfile_and_named_warning",
    "F2P-002": "inplace_explicit_and_traversal",
    "F2P-003": "named_error_failfast",
    "F2P-004": "stdin_devnull_and_terminal",
    "F2P-005": "check_diff_exact",
    "F2P-007": "library_contract",
    "F2P-009": "ignored_stdin_filepath",
    "F2P-013": "version_help_identity",
    "F2P-016": "inplace_mtime",
    "F2P-019": "go_build_and_dependency_list",
    "F2P-020": "cgo_prism_without_ruby_or_rust",
    "F2P-021": "header_opt_in_window",
    "F2P-024": "ignore_negation_traversal",
    "F2P-006": "check_diff_exact",
    "F2P-008": "missing_atfile_and_named_warning",
    "F2P-010": "missing_atfile_and_named_warning",
    "F2P-011": "missing_atfile_and_named_warning",
    "F2P-012": "missing_atfile_and_named_warning",
    "F2P-014": "version_help_identity",
    "F2P-015": "check_diff_exact",
    "F2P-017": "library_contract",
    "F2P-018": "inplace_explicit_and_traversal",
    "F2P-022": "inplace_mtime",
    "F2P-023": "ignored_stdin_filepath",
}


class ProbeFailure(Exception):
    pass


class Result:
    def __init__(self, code, stdout, stderr):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr


class TargetProbe:
    def __init__(self, repo, binary=None):
        self.repo = pathlib.Path(repo).resolve()
        self.explicit_binary = pathlib.Path(binary).resolve() if binary else None
        self.binary = None
        self.tempdirs = []

    def tempdir(self):
        path = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-f2p-"))
        self.tempdirs.append(path)
        return path

    def cleanup(self):
        for path in self.tempdirs:
            shutil.rmtree(path, ignore_errors=True)

    def run(self, argv, cwd=None, data=None, stdin=None, env=None, timeout=120):
        kwargs = {
            "cwd": str(cwd or self.repo),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "timeout": timeout,
        }
        if env is not None:
            kwargs["env"] = env
        if stdin is not None:
            kwargs["stdin"] = stdin
        elif data is not None:
            kwargs["input"] = data
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        completed = subprocess.run(argv, **kwargs)
        return Result(
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )

    def require_go_module(self):
        go_mod = self.repo / "go.mod"
        if not go_mod.is_file():
            raise ProbeFailure("target technology missing: go.mod is absent")
        text = go_mod.read_text(encoding="utf-8", errors="replace")
        if "module rubyfmt" not in text:
            raise ProbeFailure("target technology missing: go.mod does not declare module rubyfmt")
        if "go 1.24" not in text:
            raise ProbeFailure("target technology version mismatch: go.mod does not require Go 1.24")
        go = shutil.which("go")
        if go is None:
            raise ProbeFailure("target technology missing: go toolchain is unavailable")
        version = self.run([go, "version"]).stdout
        if "go1.24" not in version:
            raise ProbeFailure("target technology version mismatch: " + version.strip())
        return go

    def build(self):
        self.require_go_module()
        if self.explicit_binary is not None:
            if not self.explicit_binary.is_file():
                raise ProbeFailure("explicit target binary does not exist")
            self.binary = self.explicit_binary
        else:
            out = self.tempdir() / "rubyfmt"
            go = shutil.which("go")
            result = self.run(
                [go, "build", "-mod=vendor", "-buildvcs=false", "-o", str(out), "./cmd/rubyfmt"],
                timeout=300,
            )
            if result.code != 0:
                raise ProbeFailure("go build failed: " + (result.stderr or result.stdout).strip())
            self.binary = out
        self.require_go_binary()
        return self.binary

    def require_go_binary(self):
        go = shutil.which("go")
        result = self.run([go, "version", "-m", str(self.binary)])
        if result.code != 0:
            raise ProbeFailure("cannot inspect binary build metadata: " + result.stderr.strip())
        if "go1.24" not in result.stdout:
            raise ProbeFailure("binary was not built by Go 1.24: " + result.stdout.strip())
        if "path\trubyfmt/cmd/rubyfmt" not in result.stdout:
            raise ProbeFailure("binary is not the Go rubyfmt command: " + result.stdout.strip())

    def run_binary(self, args, cwd=None, data=None, stdin=None, env=None):
        return self.run([str(self.binary)] + list(args), cwd=cwd, data=data, stdin=stdin, env=env)

    def write(self, directory, name, content):
        path = pathlib.Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def check_missing_atfile_and_named_warning(self):
        directory = self.tempdir()
        missing_at = self.run_binary(["@missing.txt"], cwd=directory)
        if missing_at.code != 3:
            raise ProbeFailure("rubyfmt @missing.txt exited %d, want 3" % missing_at.code)
        missing_at_check = self.run_binary(["--check", "@missing.txt"], cwd=directory)
        if missing_at_check.code != 3:
            raise ProbeFailure("rubyfmt --check @missing.txt exited %d, want 3" % missing_at_check.code)
        good = self.write(directory, "good.rb", "a 1,2,3\n")
        ordinary = self.run_binary([str(good), "missing.rb"], cwd=directory)
        if ordinary.code != 0:
            raise ProbeFailure("missing ordinary file was not a warning: exit %d" % ordinary.code)
        if ordinary.stdout != "a(1, 2, 3)\n":
            raise ProbeFailure("processing did not continue after missing ordinary file: %r" % ordinary.stdout)
        if "missing.rb" not in ordinary.stderr:
            raise ProbeFailure("missing ordinary file warning did not identify the path")
        return "missing @file exits 3 and missing ordinary file is a non-fatal warning"

    def check_inplace_explicit_and_traversal(self):
        directory = self.tempdir()
        rake = self.write(directory, "test.rake", "a 1,2,3\n")
        explicit = self.run_binary(["-i", str(rake)], cwd=directory)
        if explicit.code != 0 or explicit.stdout != "":
            raise ProbeFailure("explicit -i test.rake failed: code=%d stdout=%r" % (explicit.code, explicit.stdout))
        if rake.read_text(encoding="utf-8") != "a(1, 2, 3)\n":
            raise ProbeFailure("explicit .rake file was not formatted")

        traversal = self.tempdir()
        rb = self.write(traversal, "test.rb", "a 1,2,3\n")
        txt = self.write(traversal, "test.txt", "a 1,2,3\n")
        walked = self.run_binary(["-i", "."], cwd=traversal)
        if walked.code != 0:
            raise ProbeFailure("directory traversal exited %d" % walked.code)
        if rb.read_text(encoding="utf-8") != "a(1, 2, 3)\n":
            raise ProbeFailure("directory traversal did not format test.rb")
        if txt.read_text(encoding="utf-8") != "a 1,2,3\n":
            raise ProbeFailure("directory traversal formatted non-.rb file test.txt")
        return "explicit non-.rb formatting and .rb-only traversal are preserved"

    def check_named_error_failfast(self):
        directory = self.tempdir()
        bad = self.write(directory, "bad.rb", "a 1,2,,\n")
        good = self.write(directory, "good.rb", "a(1, 2)\n")
        plain = self.run_binary([str(bad), str(good)], cwd=directory)
        if plain.code != 0:
            raise ProbeFailure("plain named syntax error should warn and exit 0, got %d" % plain.code)
        if plain.stdout != "a(1, 2)\n":
            raise ProbeFailure("plain mode did not format the good file after a warning: %r" % plain.stdout)
        fail_fast = self.run_binary(["--fail-fast", str(bad)], cwd=directory)
        if fail_fast.code != 1:
            raise ProbeFailure("--fail-fast syntax error exited %d, want 1" % fail_fast.code)
        missing = self.run_binary(["--fail-fast", "missing.rb"], cwd=directory)
        if missing.code != 3:
            raise ProbeFailure("--fail-fast missing input exited %d, want 3" % missing.code)
        return "warnings continue processing and --fail-fast maps syntax/IO errors to 1/3"

    def check_stdin_devnull_and_terminal(self):
        devnull = subprocess.DEVNULL
        empty = self.run_binary([], cwd=self.tempdir(), stdin=devnull)
        if empty.code != 0:
            raise ProbeFailure("empty stdin exited %d, want 0" % empty.code)
        if "Usage: rubyfmt" in empty.stdout:
            raise ProbeFailure("empty stdin printed help instead of reading input")

        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                [str(self.binary)],
                cwd=str(self.tempdir()),
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            os.close(slave)
            slave = -1
            stdout, stderr = process.communicate(timeout=30)
            try:
                while select.select([master], [], [], 0)[0]:
                    os.read(master, 4096)
            except OSError:
                pass
        finally:
            if slave >= 0:
                os.close(slave)
            os.close(master)
        text = stdout.decode("utf-8", "replace")
        if process.returncode != 0 or "Usage: rubyfmt" not in text:
            raise ProbeFailure("terminal stdin did not print help: code=%d stdout=%r stderr=%r" % (
                process.returncode, text, stderr.decode("utf-8", "replace")))
        return "empty piped stdin is read and terminal stdin prints help"

    def check_check_diff_exact(self):
        directory = self.tempdir()
        self.write(directory, "test.rb", "a 1,2,3\n")
        result = self.run_binary(["--check", "test.rb"], cwd=directory)
        if result.code != 5 or not diff_matches(result.stdout, [("test.rb", "a 1,2,3\n", "a(1, 2, 3)\n")]):
            raise ProbeFailure("unexpected check diff: code=%d stdout=%r" % (result.code, result.stdout))
        go = shutil.which("go")
        deps = self.run([go, "list", "-deps", "./cmd/rubyfmt"])
        if deps.code != 0 or "github.com/pmezard/go-difflib/difflib" not in deps.stdout.splitlines():
            raise ProbeFailure("check diff is not produced through the Go difflib dependency")
        return "check mode emits a valid unified diff through the Go difflib path"

    def check_library_contract(self):
        go = shutil.which("go")
        stage = self.tempdir()
        probe_path = stage / "zz_f2p_library_probe_test.go"
        # The instruction only documents that the library entry point returns
        # formatted bytes or a syntax/IO error that maps to the documented exit
        # codes. Assert that documented behavior without pinning the concrete
        # error type or package-level helper names, so any reasonable Go API
        # shape (e.g. ErrSyntax + errors.Is, or a Kind field) is accepted as
        # long as the returned error exposes its exit code.
        content = '''package rubyfmt_test

import (
\t"bytes"
\t"testing"

\trubyfmt "rubyfmt"
)

// The public function type is part of the documented contract. In particular,
// returning a concrete pointer error is not assignable to this function type.
var _ func([]byte) ([]byte, error) = rubyfmt.FormatBuffer

func TestF2PLibraryContract(t *testing.T) {
\tout, err := rubyfmt.FormatBuffer([]byte("a 1,2,3"))
\tif err != nil {
\t\tt.Fatalf("FormatBuffer returned an error: %v", err)
\t}
\tif !bytes.Equal(out, []byte("a(1, 2, 3)\\n")) {
\t\tt.Fatalf("unexpected formatted bytes: %q", out)
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
\t\tt.Fatalf("syntax error exit code = %d, want 1", coded.ExitCode())
\t}
}
'''
        probe_path.write_text(content, encoding="utf-8")
        overlay_path = stage / "overlay.json"
        overlay_path.write_text(
            json.dumps({"Replace": {str(self.repo / probe_path.name): str(probe_path)}}),
            encoding="utf-8",
        )
        result = self.run(
            [go, "test", "-mod=vendor", "-buildvcs=false", "-overlay", str(overlay_path),
             "-run", "TestF2PLibraryContract", "-count=1", "."],
            timeout=300,
        )
        if result.code != 0:
            raise ProbeFailure("Go library contract test failed: " + (result.stdout + result.stderr).strip())
        return "Go FormatBuffer/ExitCode library contract is satisfied"

    def check_ignored_stdin_filepath(self):
        directory = self.tempdir()
        self.write(directory, ".rubyfmtignore", "ignored.rb\n")
        data = b"a 1,2,3"
        normal = self.run_binary(["--stdin-filepath", "ignored.rb"], cwd=directory, data=data)
        if normal.code != 0 or normal.stdout != "a 1,2,3":
            raise ProbeFailure("ignored stdin filepath was not passed through: code=%d stdout=%r" % (
                normal.code, normal.stdout))
        checked = self.run_binary(["--check", "--stdin-filepath", "ignored.rb"], cwd=directory, data=data)
        if checked.code != 0 or checked.stdout != "":
            raise ProbeFailure("ignored stdin filepath under --check produced output or failed: code=%d stdout=%r" % (
                checked.code, checked.stdout))
        return "ignored stdin filepath is passthrough in normal mode and silent in check mode"

    def check_version_help_identity(self):
        version = self.run_binary(["-V"])
        if version.code != 0 or not version.stdout.startswith("rubyfmt ") or "rubyfmt-main" in version.stdout:
            raise ProbeFailure("unexpected -V result: code=%d stdout=%r" % (version.code, version.stdout))
        help_result = self.run_binary(["-h"])
        if help_result.code != 0 or "Usage: rubyfmt" not in help_result.stdout or "rubyfmt-main" in help_result.stdout:
            raise ProbeFailure("unexpected -h result: code=%d stdout=%r" % (help_result.code, help_result.stdout))
        unknown = self.run_binary(["--not-a-real-flag"])
        if unknown.code == 0 or "rubyfmt" not in unknown.stderr or "rubyfmt-main" in unknown.stderr:
            raise ProbeFailure("unexpected unknown-flag result: code=%d stderr=%r" % (unknown.code, unknown.stderr))
        return "version, help, and unknown-flag diagnostics identify the Go rubyfmt executable"

    def check_inplace_mtime(self):
        directory = self.tempdir()
        unchanged = self.write(directory, "unchanged.rb", "a(1, 2, 3)\n")
        changed = self.write(directory, "changed.rb", "a 1,2,3\n")
        old = time.time() - 30
        os.utime(unchanged, (old, old))
        os.utime(changed, (old, old))
        before_unchanged = unchanged.stat().st_mtime_ns
        before_changed = changed.stat().st_mtime_ns
        result = self.run_binary(["-i", str(unchanged), str(changed)], cwd=directory)
        if result.code != 0 or result.stdout != "":
            raise ProbeFailure("in-place run failed: code=%d stdout=%r" % (result.code, result.stdout))
        if unchanged.read_text(encoding="utf-8") != "a(1, 2, 3)\n":
            raise ProbeFailure("unchanged file content was modified")
        if changed.read_text(encoding="utf-8") != "a(1, 2, 3)\n":
            raise ProbeFailure("changed file was not formatted")
        if unchanged.stat().st_mtime_ns != before_unchanged:
            raise ProbeFailure("unchanged file mtime changed")
        if changed.stat().st_mtime_ns <= before_changed:
            raise ProbeFailure("changed file mtime did not advance")
        return "in-place mode preserves unchanged mtimes and advances rewritten mtimes"

    def check_go_build_and_dependency_list(self):
        go = self.require_go_module()
        output = self.tempdir() / "rubyfmt"
        build = self.run(
            [go, "build", "-mod=vendor", "-buildvcs=false", "-o", str(output), "./cmd/rubyfmt"],
            timeout=300,
        )
        if build.code != 0:
            raise ProbeFailure("documented Go build failed: " + (build.stderr or build.stdout).strip())
        formatted = self.run([str(output)], data=b"a 1,2,3")
        if formatted.code != 0 or formatted.stdout != "a(1, 2, 3)\n":
            raise ProbeFailure("newly built binary failed to format: code=%d stdout=%r" % (
                formatted.code, formatted.stdout))
        deps = self.run([go, "list", "-deps", "./cmd/rubyfmt"])
        if deps.code != 0:
            raise ProbeFailure("go list -deps failed: " + deps.stderr.strip())
        packages = deps.stdout.splitlines()
        if "rubyfmt/internal/prism" not in packages:
            raise ProbeFailure("go dependency graph does not contain rubyfmt/internal/prism")
        forbidden = {"rubyfmt-main", "librubyfmt", "ruby-prism"}
        found = sorted(package for package in packages if package in forbidden)
        if found:
            raise ProbeFailure("Go dependency graph still contains Rust packages: %r" % found)
        return "Go 1.24 build succeeds and dependency graph uses vendored Prism without Rust packages"

    def check_cgo_prism_without_ruby_or_rust(self):
        go = self.require_go_module()
        listing = self.run([go, "list", "-json", "./internal/prism"])
        if listing.code != 0:
            raise ProbeFailure("cannot inspect internal/prism parser build: " + listing.stderr.strip())
        try:
            package = json.loads(listing.stdout)
        except (ValueError, TypeError) as exc:
            raise ProbeFailure("invalid internal/prism package metadata: " + str(exc)) from exc
        if not isinstance(package, dict) or package.get("ImportPath") != "rubyfmt/internal/prism":
            raise ProbeFailure("unexpected internal/prism package: " + listing.stdout.strip())
        # The contract requires prism.go to be a cgo bridge; generated AST
        # conversion files may legitimately use cgo in the same package too.
        if "prism.go" not in package.get("CgoFiles", []):
            raise ProbeFailure("internal/prism is not built through cgo: " + listing.stdout.strip())
        if "prism.c" not in package.get("CFiles", []):
            raise ProbeFailure("vendored Prism C parser is not compiled: " + listing.stdout.strip())
        for manifest in ("go.mod", "go.sum"):
            path = self.repo / manifest
            if path.is_file() and "ruby-prism" in path.read_text(encoding="utf-8", errors="replace"):
                raise ProbeFailure("%s still declares the Rust ruby-prism dependency" % manifest)
        env = dict(os.environ)
        env["PATH"] = "/nonexistent"
        result = self.run_binary([], cwd=self.tempdir(), data=b"a 1,2,3", env=env)
        if result.code != 0 or result.stdout != "a(1, 2, 3)\n":
            raise ProbeFailure("binary depends on ruby or Rust on PATH: code=%d stdout=%r stderr=%r" % (
                result.code, result.stdout, result.stderr))
        return "binary parses through vendored Prism via cgo without ruby or Rust on PATH"

    def check_header_opt_in_window(self):
        opted_in = b"# rubyfmt: true\na 1,2,3\n"
        formatted = self.run_binary(["--header-opt-in"], data=opted_in)
        if formatted.code != 0 or formatted.stdout != "# rubyfmt: true\na(1, 2, 3)\n":
            raise ProbeFailure("header opt-in did not format a leading marker: code=%d stdout=%r" % (
                formatted.code, formatted.stdout))
        late = b"#" + (b"x" * 500) + b"\n# rubyfmt: true\na 1,2,3\n"
        passthrough = self.run_binary(["--header-opt-in"], data=late)
        if passthrough.code != 0 or passthrough.stdout.encode("utf-8") != late:
            raise ProbeFailure("marker after the first 500 bytes incorrectly opted in")
        return "header opt-in honors the leading 500-byte window"

    def check_ignore_negation_traversal(self):
        directory = self.tempdir()
        self.write(directory, ".rubyfmtignore", "/a/b/\n!nested.rb\n")
        outside = self.write(directory, "a/test.rb", "a 1,2,3\n")
        ignored = self.write(directory, "a/b/test.rb", "a 1,2,3\n")
        nested = self.write(directory, "a/b/nested.rb", "a 1,2,3\n")
        result = self.run_binary(["-i", "a"], cwd=directory)
        if result.code != 0:
            raise ProbeFailure("ignore traversal run exited %d" % result.code)
        if outside.read_text(encoding="utf-8") != "a(1, 2, 3)\n":
            raise ProbeFailure("non-ignored nested file was not formatted")
        if ignored.read_text(encoding="utf-8") != "a 1,2,3\n":
            raise ProbeFailure("ignored /a/b/ file was formatted")
        if nested.read_text(encoding="utf-8") != "a 1,2,3\n":
            raise ProbeFailure("negation caused traversal into the ignored directory")
        return "ignored directory pruning wins over nested negation patterns"


def run_probe(repo, binary=None):
    probe = TargetProbe(repo, binary=binary)
    results = {}
    try:
        try:
            probe.build()
            capability_error = None
        except ProbeFailure as exc:
            capability_error = str(exc)

        cache = {}
        for observation in OBSERVATIONS:
            check_name = CHECK_FOR_OBSERVATION[observation]
            if capability_error is not None:
                results[observation] = {"status": "FAIL", "reason": capability_error, "check": check_name}
                continue
            if check_name not in cache:
                try:
                    detail = getattr(probe, "check_" + check_name)()
                    cache[check_name] = {"status": "PASS", "reason": detail}
                except ProbeFailure as exc:
                    cache[check_name] = {"status": "FAIL", "reason": str(exc)}
                except Exception as exc:
                    cache[check_name] = {"status": "FAIL", "reason": "%s: %s" % (type(exc).__name__, exc)}
            cached = cache[check_name]
            results[observation] = {
                "status": cached["status"],
                "reason": cached["reason"],
                "check": check_name,
            }
    finally:
        probe.cleanup()
    return results


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("RUBYFMT_REPO", "/workspace/repo"))
    parser.add_argument("--binary", default=None)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)
    results = run_probe(args.repo, binary=args.binary)
    payload = {
        "batch_id": BATCH_ID,
        "observations": OBSERVATIONS,
        "results": results,
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.json:
        pathlib.Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if all(result["status"] == "PASS" for result in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
