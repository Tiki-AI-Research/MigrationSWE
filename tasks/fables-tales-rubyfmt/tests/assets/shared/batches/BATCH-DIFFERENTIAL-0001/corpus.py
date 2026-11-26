#!/usr/bin/env python3
"""Shared differential corpus for BATCH-DIFFERENTIAL-0001.

The same corpus is executed against the Rust reference implementation
(``/input/source``) and the Go migration (``/input/target``).  Every case
projects the observable result into a JSON value that is independent of the
implementation language, absolute temporary paths, and debug logging, so two
implementations that preserve the documented contract produce byte-identical
normalized results.

Usage:
    python3 corpus.py --binary /path/to/rubyfmt --repo /input/target \
        --output /tmp/target-results.json

Compare two result files produced this way with --compare.
"""

import argparse
import hashlib
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))
from diff_contract import canonical_diff

TOKEN_WORK = "<WORK>"
TOKEN_REPO = "<REPO>"

CASES = {}


def case(observation_id):
    def register(fn):
        CASES[observation_id] = fn
        return fn

    return register


class Context:
    def __init__(self, binary, repo):
        self.binary = str(binary)
        self.repo = Path(repo)
        self.work = Path(tempfile.mkdtemp(prefix="rubyfmt-diff-corpus-"))

    def cleanup(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def make_dir(self, name):
        path = self.work / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, path, content):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as handle:
            handle.write(content)
        return path

    def read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def run(self, args, stdin=None, cwd=None):
        data = None
        if stdin is not None:
            data = stdin.encode() if isinstance(stdin, str) else stdin
        completed = subprocess.run(
            [self.binary, *args],
            input=data,
            cwd=str(cwd or self.work),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        return {
            "code": completed.returncode,
            "stdout": completed.stdout.decode("utf-8", "replace"),
            "stderr": completed.stderr.decode("utf-8", "replace"),
        }

    def run_bytes(self, args, stdin=None, cwd=None):
        data = None
        if stdin is not None:
            data = stdin.encode() if isinstance(stdin, str) else stdin
        completed = subprocess.run(
            [self.binary, *args],
            input=data,
            cwd=str(cwd or self.work),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        return {
            "code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def run_tty(self, args):
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [self.binary, *args],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.work),
        )
        os.close(slave)
        try:
            stdout, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, _ = process.communicate()
        finally:
            os.close(master)
        return {
            "code": process.returncode,
            "stdout": stdout.decode("utf-8", "replace"),
        }

    def norm(self, value):
        if isinstance(value, str):
            value = value.replace(str(self.work), TOKEN_WORK)
            value = value.replace(str(self.repo), TOKEN_REPO)
            return value
        if isinstance(value, list):
            return [self.norm(item) for item in value]
        if isinstance(value, dict):
            return {key: self.norm(item) for key, item in value.items()}
        return value


def tree_digest(root):
    root = Path(root)
    digest = hashlib.sha256()
    if not root.exists():
        return "missing"
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            digest.update(rel.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def impl_surface(repo):
    manifests = [name for name in ("go.mod", "Cargo.toml") if (repo / name).is_file()]
    entries = [
        name
        for name in ("cmd/rubyfmt/main.go", "src/main.rs")
        if (repo / name).is_file()
    ]
    test_roots = [name for name in ("tests", "internal") if (repo / name).is_dir()]
    return {
        "has_manifest": bool(manifests),
        "has_entrypoint": bool(entries),
        "has_tests": bool(test_roots),
        "has_readme": (repo / "README.md").is_file(),
    }


def format_once(ctx, text, args=()):
    result = ctx.run([*args], stdin=text)
    return {
        "code": result["code"],
        "stdout": ctx.norm(result["stdout"]),
    }


@case("DIFF-001")
def diff_001(ctx):
    work = ctx.make_dir("cli-shapes")
    one = ctx.write(work / "one.rb", "a 1,2,3\n")
    two_dir = ctx.make_dir("cli-shapes/dir")
    ctx.write(two_dir / "two.rb", "a 4,5,6\n")
    path_list = ctx.write(work / "paths.txt", str(one) + "\n")
    return ctx.norm(
        {
            "file": ctx.run([str(one)])["stdout"],
            "directory": ctx.run([str(two_dir)])["stdout"],
            "at_file": ctx.run(["@" + str(path_list)])["stdout"],
            "stdin": ctx.run([], stdin="a 7,8,9")["stdout"],
        }
    )


@case("DIFF-002")
def diff_002(ctx):
    return {"editor_plugins_sha256": tree_digest(ctx.repo / "editor_plugins")}


@case("DIFF-003")
def diff_003(ctx):
    work = ctx.make_dir("include-paths")
    target = ctx.write(work / "test.rb", "a 1,2,3\n")
    directory = ctx.make_dir("include-paths/nested")
    ctx.write(directory / "nested.rb", "a 4,5,6\n")
    path_list = ctx.write(work / "list.txt", str(target) + "\n")
    return {
        "file": ctx.norm(ctx.run([str(target)])["stdout"]),
        "directory": ctx.norm(ctx.run([str(directory)])["stdout"]),
        "at_file": ctx.norm(ctx.run(["@" + str(path_list)])["stdout"]),
    }


@case("DIFF-004")
def diff_004(ctx):
    work = ctx.make_dir("default-sink")
    target = ctx.write(work / "test.rb", "a 1,2,3\n")
    result = ctx.run([str(target)])
    after = ctx.read(target).decode()
    directory = ctx.make_dir("default-sink/dir")
    nested = ctx.write(directory / "nested.rb", "a 4,5,6\n")
    ctx.run([str(directory)])
    return {
        "stdout": ctx.norm(result["stdout"]),
        "file_after": after,
        "nested_after": ctx.read(nested).decode(),
    }


@case("DIFF-005")
def diff_005(ctx):
    work = ctx.make_dir("check-diff")
    target = ctx.write(work / "test.rb", "a 1,2,3\n")
    long_result = ctx.run(["--check", str(target)])
    short_result = ctx.run(["-c", str(target)])
    return {
        "long": {
            "code": long_result["code"],
            "stdout": ctx.norm(canonical_diff(long_result["stdout"], [(str(target), "a 1,2,3\n", "a(1, 2, 3)\n")])),
        },
        "short": {
            "code": short_result["code"],
            "stdout": ctx.norm(canonical_diff(short_result["stdout"], [(str(target), "a 1,2,3\n", "a(1, 2, 3)\n")])),
        },
    }


@case("DIFF-006")
def diff_006(ctx):
    work = ctx.make_dir("check-status")
    bad = ctx.write(work / "bad.rb", "a 1,2,,\n")
    clean = ctx.write(work / "clean.rb", "a(1, 2, 3)\n")
    bad_result = ctx.run(["--check", str(bad)])
    clean_result = ctx.run(["--check", str(clean)])
    return {
        "syntax_code": bad_result["code"],
        "syntax_stdout": ctx.norm(bad_result["stdout"]),
        "clean_code": clean_result["code"],
        "clean_stdout": ctx.norm(clean_result["stdout"]),
    }


@case("DIFF-007")
def diff_007(ctx):
    work = ctx.make_dir("in-place")
    changed = ctx.write(work / "changed.rb", "a 1,2,3\n")
    unchanged = ctx.write(work / "unchanged.rb", "a(1, 2, 3)\n")
    result = ctx.run(["-i", str(changed), str(unchanged)])
    return {
        "code": result["code"],
        "stdout": ctx.norm(result["stdout"]),
        "changed_after": ctx.read(changed).decode(),
        "unchanged_after": ctx.read(unchanged).decode(),
    }


@case("DIFF-008")
def diff_008(ctx):
    work = ctx.make_dir("mtime")
    unchanged = ctx.write(work / "unchanged.rb", "a(1, 2, 3)\n")
    changed = ctx.write(work / "changed.rb", "a 1,2,3\n")
    stamp = 1_600_000_000
    os.utime(unchanged, (stamp, stamp))
    os.utime(changed, (stamp, stamp))
    time.sleep(0.05)
    result = ctx.run(["-i", str(unchanged), str(changed)])
    unchanged_mtime = int(os.stat(unchanged).st_mtime)
    changed_mtime = int(os.stat(changed).st_mtime)
    return {
        "code": result["code"],
        "unchanged_preserved": unchanged_mtime == stamp,
        "changed_advanced": changed_mtime > stamp,
        "changed_after": ctx.read(changed).decode(),
    }


@case("DIFF-009")
def diff_009(ctx):
    piped = ctx.run([], stdin="a 1,2,3")
    terminal = ctx.run_tty([])
    return {
        "piped_code": piped["code"],
        "piped_stdout": ctx.norm(piped["stdout"]),
        "terminal_code": terminal["code"],
        "terminal_has_usage": "Usage: rubyfmt" in terminal["stdout"],
        "terminal_format_output": "a(1, 2, 3)\n" in terminal["stdout"],
    }


@case("DIFF-010")
def diff_010(ctx):
    work = ctx.make_dir("stdin-filepath")
    ctx.write(work / ".rubyfmtignore", "ignored.rb\n")
    target = ctx.write(work / "test.rb", "a 1,2,3\n")
    ignored = ctx.run(
        ["--stdin-filepath", "ignored.rb"], stdin="a 1,2,3", cwd=work
    )
    positional = ctx.run(
        ["--stdin-filepath", "lib/foo.rb", str(target)], stdin="", cwd=work
    )
    in_place = ctx.run(
        ["--stdin-filepath", "lib/foo.rb", "-i"], stdin="a 1,2,3", cwd=work
    )
    return {
        "ignored_code": ignored["code"],
        "ignored_stdout": ignored["stdout"],
        "positional_code_nonzero": positional["code"] != 0,
        "positional_conflict": "--stdin-filepath" in positional["stderr"]
        and "cannot be used with" in positional["stderr"],
        "in_place_code_nonzero": in_place["code"] != 0,
        "in_place_conflict": "--stdin-filepath" in in_place["stderr"]
        and "cannot be used with" in in_place["stderr"],
    }


@case("DIFF-011")
def diff_011(ctx):
    opted_in = format_once(ctx, "# rubyfmt: true\na 1,2,3\n", ["--header-opt-in"])
    without_marker = format_once(ctx, "a 1,2,3", ["--header-opt-in"])
    return {"opted_in": opted_in, "without_marker": without_marker}


@case("DIFF-012")
def diff_012(ctx):
    opted_out = format_once(ctx, "# rubyfmt: false\na 1,2,3", ["--header-opt-out"])
    return opted_out


@case("DIFF-013")
def diff_013(ctx):
    results = {}
    for ignore_name in (".rubyfmtignore", ".gitignore"):
        work = ctx.make_dir("ignore-" + ignore_name.lstrip("."))
        (work / ".git").mkdir()
        ctx.write(work / ignore_name, "/a/b/\n")
        ctx.write(work / "a" / "test.rb", "a 1,2,3\n")
        ctx.write(work / "a" / "b" / "test.rb", "a 4,5,6\n")
        result = ctx.run(["-i", "a"], cwd=work)
        results[ignore_name] = {
            "code": result["code"],
            "formatted": ctx.read(work / "a" / "test.rb").decode(),
            "ignored_after": ctx.read(work / "a" / "b" / "test.rb").decode(),
        }
    return results


@case("DIFF-014")
def diff_014(ctx):
    work = ctx.make_dir("include-gitignored")
    (work / ".git").mkdir()
    ctx.write(work / ".gitignore", "ignored.rb\n")
    ignored = ctx.write(work / "ignored.rb", "a 1,2,3\n")
    first = ctx.run(["-i", "."], cwd=work)
    after_default = ctx.read(ignored).decode()
    second = ctx.run(["-i", "--include-gitignored", "."], cwd=work)
    after_include = ctx.read(ignored).decode()

    explicit = ctx.make_dir("explicit-ignore")
    explicit_file = ctx.write(explicit / "test.rb", "a 1,2,3\n")
    ctx.write(explicit / ".rubyfmtignore", "test.rb\n")
    third = ctx.run(["-i", str(explicit_file)], cwd=explicit)
    return {
        "default_code": first["code"],
        "default_after": after_default,
        "include_code": second["code"],
        "include_after": after_include,
        "explicit_code": third["code"],
        "explicit_after": ctx.read(explicit_file).decode(),
    }


@case("DIFF-015")
def diff_015(ctx):
    work = ctx.make_dir("library-proxy")
    good = ctx.write(work / "good.rb", "a 1,2,3\n")
    bad = ctx.write(work / "bad.rb", "a 1,2,,\n")
    success = ctx.run([str(good)])
    syntax = ctx.run([str(bad)])
    missing = ctx.run(["--fail-fast", str(work / "missing.rb")])
    return {
        "format_code": success["code"],
        "format_stdout": success["stdout"],
        "syntax_code": syntax["code"],
        "io_code": missing["code"],
    }


@case("DIFF-016")
def diff_016(ctx):
    outputs = {}
    for flag in ("-V", "--version", "-h", "--help"):
        result = ctx.run([flag])
        outputs[flag] = {
            "code": result["code"],
            "starts_rubyfmt": result["stdout"].startswith("rubyfmt ")
            if flag in ("-V", "--version")
            else None,
            "contains_usage": "Usage: rubyfmt" in result["stdout"]
            if flag in ("-h", "--help")
            else None,
            "contains_old_name": "rubyfmt-main" in result["stdout"]
            or "rubyfmt-main" in result["stderr"],
        }
    return outputs


@case("DIFF-017")
def diff_017(ctx):
    work = ctx.make_dir("stdin-file")
    target = ctx.write(work / "test.rb", "a 1,2,3\n")
    stdin_result = ctx.run([], stdin="a 1,2,3")
    file_result = ctx.run([str(target)])
    return {
        "stdin_code": stdin_result["code"],
        "stdin_stdout": stdin_result["stdout"],
        "file_code": file_result["code"],
        "file_stdout": file_result["stdout"],
    }


def _fixture_pairs(repo):
    fixtures = repo / "fixtures"
    for expected in sorted(fixtures.rglob("*_expected.rb")):
        actual = expected.with_name(expected.name[: -len("_expected.rb")] + "_actual.rb")
        if actual.is_file():
            yield actual, expected


@case("DIFF-018")
def diff_018(ctx):
    digest = hashlib.sha256()
    pairs = 0
    mismatches = []
    for actual, expected in _fixture_pairs(ctx.repo):
        pairs += 1
        expected_bytes = ctx.read(expected)
        actual_result = ctx.run_bytes([str(actual)])
        idempotent_result = ctx.run_bytes([str(expected)])
        actual_ok = actual_result["stdout"] == expected_bytes
        idempotent_ok = idempotent_result["stdout"] == expected_bytes
        rel = actual.relative_to(ctx.repo).as_posix()
        digest.update(rel.encode())
        digest.update(actual_result["stdout"])
        digest.update(idempotent_result["stdout"])
        if not (actual_ok and idempotent_ok):
            mismatches.append(rel)
    return {
        "pairs": pairs,
        "mismatches": mismatches[:20],
        "mismatch_count": len(mismatches),
        "digest": digest.hexdigest(),
    }


def _stress(ctx, name):
    script = ctx.repo / "ci" / name
    original = subprocess.run(
        ["ruby", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    formatted = ctx.run([str(script)])
    temp = ctx.write(ctx.work / ("stress-" + name), formatted["stdout"].encode())
    after = subprocess.run(
        ["ruby", str(temp)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    digest = hashlib.sha256()
    digest.update(original.stdout)
    digest.update(after.stdout)
    return {
        "original_code": original.returncode,
        "formatted_code": formatted["code"],
        "after_code": after.returncode,
        "same_stdout": original.stdout == after.stdout,
        "digest": digest.hexdigest(),
    }


@case("DIFF-019")
def diff_019(ctx):
    names = (
        "methods_stress_test.rb",
        "string_literals_stress_test.rb",
        "array_literals_stress_test.rb",
    )
    return {name: _stress(ctx, name) for name in names}


@case("DIFF-020")
def diff_020(ctx):
    result = ctx.run([], stdin="a 1,2,,")
    return {
        "code": result["code"],
        "stderr_has_syntax_error": "syntax error" in result["stderr"].lower(),
    }


@case("DIFF-021")
def diff_021(ctx):
    work = ctx.make_dir("warning-continuation")
    bad = ctx.write(work / "bad.rb", "a 1,2,,\n")
    good = ctx.write(work / "good.rb", "a(1, 2)\n")
    missing = work / "missing.rb"
    result = ctx.run([str(bad), str(good), str(missing)])
    return {
        "code": result["code"],
        "stdout": result["stdout"],
        "stderr_has_syntax_error": "syntax error" in result["stderr"].lower(),
        "stderr_has_io_error": "io error" in result["stderr"].lower(),
    }


@case("DIFF-022")
def diff_022(ctx):
    work = ctx.make_dir("fail-fast")
    bad = ctx.write(work / "bad.rb", "a 1,2,,\n")
    missing = work / "missing.rb"
    return {
        "syntax_code": ctx.run(["--fail-fast", str(bad)])["code"],
        "missing_file_code": ctx.run(["--fail-fast", str(missing)])["code"],
        "missing_at_file_code": ctx.run(["--fail-fast", "@missing-list.txt"])["code"],
        "missing_at_file_plain_code": ctx.run(["@missing-list.txt"])["code"],
    }


@case("DIFF-023")
def diff_023(ctx):
    work = ctx.make_dir("check-headers")
    target = ctx.write(work / "lib" / "foo.rb", "a 1,2,3\n")
    file_result = ctx.run(["--check", str(target)])
    stdin_result = ctx.run(
        ["--check", "--stdin-filepath", "lib/foo.rb"], stdin="a 1,2,3\n"
    )
    return {
        "file": {
            "code": file_result["code"],
            "stdout": ctx.norm(canonical_diff(file_result["stdout"], [(str(target), "a 1,2,3\n", "a(1, 2, 3)\n")])),
        },
        "stdin_filepath": {
            "code": stdin_result["code"],
            "stdout": canonical_diff(stdin_result["stdout"], [("lib/foo.rb", "a 1,2,3\n", "a(1, 2, 3)\n")]),
        },
    }


@case("DIFF-024")
def diff_024(ctx):
    work = ctx.make_dir("ignored-stdin")
    ctx.write(work / ".rubyfmtignore", "ignored.rb\n")
    normal = ctx.run(
        ["--stdin-filepath", "ignored.rb"], stdin="a 1,2,3", cwd=work
    )
    check = ctx.run(
        ["--check", "--stdin-filepath", "ignored.rb"], stdin="a 1,2,3", cwd=work
    )
    return {
        "normal_code": normal["code"],
        "normal_stdout": normal["stdout"],
        "check_code": check["code"],
        "check_stdout": check["stdout"],
    }


@case("DIFF-025")
def diff_025(ctx):
    work = ctx.make_dir("extensions")
    rake = ctx.write(work / "test.rake", "a 1,2,3\n")
    directory = ctx.make_dir("extensions/dir")
    ruby_file = ctx.write(directory / "test.rb", "a 1,2,3\n")
    text_file = ctx.write(directory / "test.txt", "a 1,2,3\n")
    explicit = ctx.run(["-i", str(rake)])
    traversal = ctx.run(["-i", str(directory)])
    return {
        "explicit_code": explicit["code"],
        "rake_after": ctx.read(rake).decode(),
        "traversal_code": traversal["code"],
        "rb_after": ctx.read(ruby_file).decode(),
        "txt_after": ctx.read(text_file).decode(),
    }


@case("DIFF-026")
def diff_026(ctx):
    work = ctx.make_dir("capabilities")
    target = ctx.write(work / "test.rb", "a 1,2,3\n")
    check = ctx.run(["--check", str(target)])
    in_place_file = ctx.write(work / "in_place.rb", "a 4,5,6\n")
    ctx.run(["-i", str(in_place_file)])
    version = ctx.run(["-V"])
    return {
        "file": ctx.norm(ctx.run([str(target)])["stdout"]),
        "stdin": ctx.run([], stdin="a 1,2,3")["stdout"],
        "check_code": check["code"],
        "check_has_diff": "--- " in check["stdout"] and "+++ " in check["stdout"],
        "in_place_after": ctx.read(in_place_file).decode(),
        "version_ok": version["stdout"].startswith("rubyfmt "),
        "help_ok": "Usage: rubyfmt" in ctx.run(["--help"])["stdout"],
    }


@case("DIFF-027")
def diff_027(ctx):
    surface = impl_surface(ctx.repo)
    return {
        "implementation_surface": surface,
        "fixtures_sha256": tree_digest(ctx.repo / "fixtures"),
        "editor_plugins_sha256": tree_digest(ctx.repo / "editor_plugins"),
        "ci_sha256": tree_digest(ctx.repo / "ci"),
    }


@case("DIFF-028")
def diff_028(ctx):
    work = ctx.make_dir("flag-contract")
    check_file = ctx.write(work / "check.rb", "a 1,2,3\n")
    in_place_file = ctx.write(work / "in_place.rb", "a 1,2,3\n")
    ctx.run(["-i", str(in_place_file)])
    ctx.write(work / ".rubyfmtignore", "ignored.rb\n")
    ignored = ctx.run(
        ["--stdin-filepath", "ignored.rb"], stdin="a 1,2,3", cwd=work
    )
    return {
        "check_short_code": ctx.run(["-c", str(check_file)])["code"],
        "check_long_code": ctx.run(["--check", str(check_file)])["code"],
        "in_place_after": ctx.read(in_place_file).decode(),
        "stdin_filepath_passthrough": ignored["stdout"],
        "header_opt_in": format_once(
            ctx, "# rubyfmt: true\na 1,2,3\n", ["--header-opt-in"]
        ),
        "header_opt_out": format_once(
            ctx, "# rubyfmt: false\na 1,2,3", ["--header-opt-out"]
        ),
        "fail_fast_syntax_code": ctx.run(
            ["--fail-fast"], stdin="a 1,2,,"
        )["code"],
        "version_prefix": ctx.run(["--version"])["stdout"].split(" ")[0],
    }


@case("DIFF-029")
def diff_029(ctx):
    return {
        "fixtures_sha256": tree_digest(ctx.repo / "fixtures"),
        "editor_plugins_sha256": tree_digest(ctx.repo / "editor_plugins"),
    }


@case("DIFF-030")
def diff_030(ctx):
    digest = hashlib.sha256()
    sample = list(_fixture_pairs(ctx.repo))[:10]
    for actual, expected in sample:
        expected_bytes = ctx.read(expected)
        result = ctx.run_bytes([str(actual)])
        digest.update(actual.relative_to(ctx.repo).as_posix().encode())
        digest.update(expected_bytes)
        digest.update(result["stdout"])
    in_place = ctx.write(ctx.work / "harness-in-place.rb", "a 1,2,3\n")
    ctx.run(["-i", str(in_place)])
    digest.update(ctx.read(in_place))
    check = ctx.run(["--check"], stdin="a 1,2,3\n")
    digest.update(str(check["code"]).encode())
    digest.update(canonical_diff(check["stdout"], [("stdin", "a 1,2,3\n", "a(1, 2, 3)\n")]).encode())
    return {
        "sample_pairs": len(sample),
        "in_place_after": ctx.read(in_place).decode(),
        "check_code": check["code"],
        "digest": digest.hexdigest(),
    }


@case("DIFF-031")
def diff_031(ctx):
    syntax = ctx.run([], stdin="a 1,2,,")
    check = ctx.run(["--check"], stdin="a 1,2,3\n")
    arbitrary = ctx.run([], stdin="b 4,5,6")
    return {
        "invalid_fails": syntax["code"] != 0,
        "invalid_has_syntax_error": "syntax error" in syntax["stderr"].lower(),
        "check_diff_code": check["code"],
        "check_has_diff": "--- stdin" in check["stdout"]
        and "+++ stdin" in check["stdout"],
        "arbitrary_input": arbitrary["stdout"],
    }


@case("DIFF-032")
def diff_032(ctx):
    return impl_surface(ctx.repo)


def run_all(binary, repo):
    ctx = Context(binary, repo)
    try:
        return {name: fn(ctx) for name, fn in sorted(CASES.items())}
    finally:
        ctx.cleanup()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
    parser.add_argument("--repo")
    parser.add_argument("--output")
    parser.add_argument("--compare", nargs=2)
    parser.add_argument("--expected")
    args = parser.parse_args(argv)

    if args.compare:
        first = json.loads(Path(args.compare[0]).read_text())
        second = json.loads(Path(args.compare[1]).read_text())
        differing = sorted(
            key
            for key in set(first) | set(second)
            if first.get(key) != second.get(key)
        )
        print(json.dumps({"differing": differing, "equal": not differing}, indent=2))
        return 0 if not differing else 1

    results = run_all(args.binary, args.repo)
    payload = json.dumps(results, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(payload + "\n")
    else:
        print(payload)

    if args.expected:
        expected = json.loads(Path(args.expected).read_text())
        differing = sorted(
            key for key in set(expected) | set(results) if expected.get(key) != results.get(key)
        )
        if differing:
            print("differing observations: " + ", ".join(differing), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
