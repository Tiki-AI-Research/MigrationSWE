#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import pty
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))
from diff_contract import diff_matches

HERE = pathlib.Path(__file__).resolve()
REPO_ROOT = HERE.parents[3]
BATCH_ID = "BATCH-P2P-0001"
PROBE_REL = "shared/batches/BATCH-P2P-0001/p2p_probe.py"

RESULTS = {}
NATIVE = {}
BINARY = None
SUITE_META = {}
FIXTURE_CASE_SPECS = {
    "P2P-001": ("fixtures/small/rspec_its_expected.rb", "foo"),
    "P2P-008": ("fixtures/small/rspec_its_actual.rb", "foo"),
    "P2P-018": ("fixtures/small/rspec_its_actual.rb", "bees"),
    "P2P-030": ("fixtures/small/rspec_its_expected.rb", "a"),
}

ALL_OBSERVATION_IDS = [
    "P2P-030", "P2P-002", "P2P-003", "P2P-004", "P2P-005", "P2P-006",
    "P2P-009", "P2P-010", "P2P-012", "P2P-013", "P2P-014", "P2P-015",
    "P2P-016", "P2P-017", "P2P-019", "P2P-020", "P2P-021", "P2P-022",
    "P2P-023", "P2P-024", "P2P-026", "P2P-027", "P2P-031", "P2P-032",
    "P2P-033", "P2P-034", "P2P-035", "P2P-036", "P2P-007", "P2P-011",
    "P2P-025", "P2P-028", "P2P-029", "P2P-001", "P2P-008", "P2P-018",
]


def _status(ok):
    return "PASS" if ok else "FAIL"


def record(oid, ok, detail=None, value=None):
    status = _status(ok)
    result = {
        "result": status,
        "status": status,
        "value": value if value is not None else {"result": status},
    }
    if not ok and detail is not None:
        result["detail"] = detail
    RESULTS[oid] = result


def run(binary, args, cwd, stdin_data=None, tty_stdin=False, env=None):
    if not binary:
        return 127, "", "rubyfmt executable unavailable"
    try:
        if tty_stdin:
            master, slave = pty.openpty()
            try:
                p = subprocess.run([binary, *args], cwd=cwd, stdin=slave,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, timeout=30)
            finally:
                os.close(slave)
                os.close(master)
            return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        kwargs = {}
        if stdin_data is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = stdin_data.encode() if isinstance(stdin_data, str) else stdin_data
        p = subprocess.run([binary, *args], cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=60, **kwargs)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)


def run_bytes(binary, args, cwd, stdin_data=None, env=None):
    if not binary:
        return 127, b"", b"rubyfmt executable unavailable"
    try:
        kwargs = {}
        if stdin_data is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = stdin_data.encode() if isinstance(stdin_data, str) else stdin_data
        p = subprocess.run([binary, *args], cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=60, **kwargs)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, b"", str(exc).encode()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


def fixture_pair(repo, rel_actual):
    p = pathlib.Path(repo) / rel_actual
    return p, pathlib.Path(str(p)[:-len("_actual.rb")] + "_expected.rb")


def check_fixture(binary, repo, rel_actual):
    actual, expected = fixture_pair(repo, rel_actual)
    exp = expected.read_bytes()
    code1, out1, err1 = run_bytes(binary, [str(actual)], repo)
    code2, out2, err2 = run_bytes(binary, [str(expected)], repo)
    ok = code1 == 0 and out1 == exp and code2 == 0 and out2 == exp
    projection = {
        "formatted_sha256": hashlib.sha256(out1).hexdigest(),
        "formatted_length": len(out1),
    }
    detail = None if ok else {
        "actual": [code1, out1[:200].decode("utf-8", "replace"), err1[:200].decode("utf-8", "replace")],
        "idempotent": [code2, out2[:200].decode("utf-8", "replace"), err2[:200].decode("utf-8", "replace")],
    }
    return ok, projection, detail


def case_chunks(text, case):
    chunks = text.rstrip("\n").split("\n\n")
    markers = {
        "a": ('it "a"', 'it("a"', 'it(\n'),
        "foo": ('describe "foo"', 'describe("foo"'),
        "bees": ('RSpec.describe "bees"', 'RSpec.describe("bees"'),
    }[case]
    return [c for c in chunks if any(m in c for m in markers)]


def check_fixture_case(binary, repo, rel_file, case):
    root = pathlib.Path(repo)
    input_path = root / rel_file
    expected_path = pathlib.Path(str(input_path)[:-len("_expected.rb")] + "_expected.rb") \
        if rel_file.endswith("_expected.rb") else pathlib.Path(str(input_path)[:-len("_actual.rb")] + "_expected.rb")
    if not input_path.is_file() or not expected_path.is_file():
        return False, {"case": case}, {"missing": [str(input_path), str(expected_path)]}

    actual_chunks = case_chunks(input_path.read_text(), case)
    expected_chunks = case_chunks(expected_path.read_text(), case)
    if not actual_chunks or not expected_chunks or len(actual_chunks) != len(expected_chunks):
        return False, {"case": case, "chunks": [len(actual_chunks), len(expected_chunks)]}, {
            "case": case,
            "reason": "could not align named fixture case chunks",
            "input_chunks": len(actual_chunks),
            "expected_chunks": len(expected_chunks),
        }

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="p2p-case-"))
    outputs = []
    expected_outputs = []
    detail_chunks = []
    try:
        for index, (chunk, expected_chunk) in enumerate(zip(actual_chunks, expected_chunks), 1):
            source_file = tmp / ("%s-%d.rb" % (case, index))
            write(source_file, chunk + "\n")
            code, out, err = run_bytes(binary, [str(source_file)], repo)
            outputs.append(out)

            expected_file = tmp / ("%s-expected-%d.rb" % (case, index))
            write(expected_file, expected_chunk + "\n")
            ecode, eout, eerr = run_bytes(binary, [str(expected_file)], repo)
            expected_outputs.append(eout)
            if code != 0 or out != eout or ecode != 0:
                detail_chunks.append({
                    "index": index,
                    "input": code,
                    "expected": ecode,
                    "input_output": out[:160].decode("utf-8", "replace"),
                    "expected_output": eout[:160].decode("utf-8", "replace"),
                    "input_error": err[:160].decode("utf-8", "replace"),
                    "expected_error": eerr[:160].decode("utf-8", "replace"),
                })
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    formatted = b"\n".join(outputs)
    expected_formatted = b"\n".join(expected_outputs)
    value = {
        "case": case,
        "file": rel_file,
        "chunk_count": len(outputs),
        "formatted_length": len(formatted),
        "formatted_sha256": hashlib.sha256(formatted).hexdigest(),
    }
    ok = not detail_chunks and formatted == expected_formatted
    return ok, value, {"case": case, "chunks": detail_chunks} if not ok else None


def cargo_binary():
    candidate = shutil.which("cargo")
    if candidate:
        return candidate
    fallback = "/tmp/cargo/bin/cargo"
    return fallback if os.path.isfile(fallback) else None


def native_env(side):
    env = os.environ.copy()
    if side == "source":
        cargo = cargo_binary()
        env.setdefault("CARGO_HOME", "/tmp/cargo")
        env.setdefault("RUSTUP_HOME", "/tmp/rustup")
        env.setdefault("CARGO_TARGET_DIR", "/tmp/source-target")
        return cargo, env
    return None, env


def run_native(cmd, cwd, env=None, timeout=1800):
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=timeout)
        return {
            "command": cmd,
            "cwd": str(cwd),
            "returncode": p.returncode,
            "result": _status(p.returncode == 0),
            "stdout_tail": p.stdout.decode("utf-8", "replace")[-4000:],
            "stderr_tail": p.stderr.decode("utf-8", "replace")[-2000:],
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "command": cmd,
            "cwd": str(cwd),
            "returncode": 127,
            "result": "FAIL",
            "stdout_tail": "",
            "stderr_tail": str(exc),
        }


def detect_side():
    text = str(HERE)
    if "/target/" in text:
        return "target"
    if "/source/" in text:
        return "source"
    cwd = pathlib.Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "go.mod").is_file():
            return "target"
        if (candidate / "Cargo.toml").is_file():
            return "source"
    return "target" if pathlib.Path("/input/target").is_dir() else "source"


def detect_repo(side=None):
    for value in (os.environ.get("P2P_REPO"), os.environ.get("REPO_ROOT"), os.environ.get("REPO")):
        if value and pathlib.Path(value).is_dir():
            return value
    cwd = pathlib.Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "go.mod").is_file() or (candidate / "Cargo.toml").is_file():
            return str(candidate)
    side = side or detect_side()
    for candidate in ("/input/%s" % side, "/workspace/repo"):
        if pathlib.Path(candidate).is_dir():
            return candidate
    return str(cwd)


def resolve_binary(side, repo, provided):
    if provided and os.path.isfile(provided) and os.access(provided, os.X_OK):
        return provided
    root = pathlib.Path(repo)
    for name in ("rubyfmt", "rubyfmt-main"):
        candidate = root / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    build_dir = tempfile.mkdtemp(prefix="p2p-binary-")
    if side == "target":
        out = os.path.join(build_dir, "rubyfmt")
        built = run_native(["go", "build", "-mod=vendor", "-buildvcs=false", "-o", out, "./cmd/rubyfmt"], repo)
        if built["returncode"] == 0 and os.path.isfile(out):
            return out
    else:
        cargo = cargo_binary()
        if cargo:
            env = os.environ.copy()
            env.setdefault("CARGO_HOME", "/tmp/cargo")
            env.setdefault("RUSTUP_HOME", "/tmp/rustup")
            env.setdefault("CARGO_TARGET_DIR", os.path.join(build_dir, "target"))
            built = run_native([cargo, "build", "--bin", "rubyfmt-main"], repo, env=env)
            candidate = pathlib.Path(env["CARGO_TARGET_DIR"]) / "debug" / "rubyfmt-main"
            if built["returncode"] == 0 and candidate.is_file():
                return str(candidate)
    return ""


def native_suite_projection(side, repo):
    if side == "target":
        return run_native(["go", "test", "-count=1", "./..."], repo)
    cargo, env = native_env(side)
    if not cargo:
        return {"command": [], "cwd": str(repo), "returncode": 127,
                "result": "FAIL", "stdout_tail": "", "stderr_tail": "cargo unavailable"}
    return run_native([cargo, "test", "--no-fail-fast"], repo, env=env)


def fixture_native_projection(side, repo):
    if side == "target":
        return run_native(
            ["go", "test", "-count=1", "-v", "./internal/formatter",
             "-run", "^TestFixtures$/^small_rspec_its$"],
            repo,
        )
    cargo, env = native_env(side)
    if not cargo:
        return {"command": [], "cwd": str(repo), "returncode": 127,
                "result": "FAIL", "stdout_tail": "", "stderr_tail": "cargo unavailable"}
    return run_native(
        [cargo, "test", "--test", "fixtures_test", "--",
         "test_small_rspec_its", "--exact"],
        repo, env=env,
    )


def all_observation_ids():
    return list(ALL_OBSERVATION_IDS)


def main():
    global BINARY
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--side", default="")
    ap.add_argument("--out", "--report", "--output", dest="out", default="")
    args = ap.parse_args()
    side = args.side or detect_side()
    repo = args.repo or detect_repo(side)
    root = pathlib.Path(repo)
    BINARY = resolve_binary(side, repo, args.binary)
    b = BINARY

    all_ids = all_observation_ids()

    NATIVE["all"] = native_suite_projection(side, repo)
    fixture_native = fixture_native_projection(side, repo)
    fixture_test = "TestFixtures/small_rspec_its" if side == "target" else "test_small_rspec_its"
    SUITE_META.setdefault("P2P-015", {
        "result": NATIVE["all"]["result"],
        "tests": " ".join(NATIVE["all"]["command"]),
    })

    markers = (["Cargo.toml", "src/main.rs", "librubyfmt/Cargo.toml"] if side == "source"
               else ["go.mod", "cmd/rubyfmt/main.go", "rubyfmt.go", "internal/formatter/formatter.go"])
    record("P2P-002", all((root / m).is_file() for m in markers), markers)

    okv = True
    details = {}
    for flag in ["-V", "--version"]:
        c, o, e = run(b, [flag], repo)
        okv = okv and c == 0 and o.startswith("rubyfmt ") and "rubyfmt-main" not in (o + e)
        details[flag] = [c, o[:80], e[:80]]
    for flag in ["-h", "--help"]:
        c, o, e = run(b, [flag], repo)
        okv = okv and c == 0 and "Usage: rubyfmt" in o and "rubyfmt-main" not in (o + e)
        details[flag] = [c, o[:120], e[:80]]
    record("P2P-004", okv, details)

    c, helpout, e = run(b, ["--help"], repo)
    flags = ["--check", "--in-place", "--stdin-filepath", "--header-opt-in",
             "--header-opt-out", "--include-gitignored", "--fail-fast", "--version"]
    okflags = (c == 0 and all(f in helpout for f in flags)
               and "-c" in helpout and "-i" in helpout and "-V" in helpout and "-h" in helpout)
    record("P2P-009", okflags, {"help_has_flags": okflags})
    record("P2P-010", okflags, {"help_has_flags": okflags})

    with tempfile.TemporaryDirectory(prefix="p2p-") as td:
        td = pathlib.Path(td)
        f = td / "test.rb"
        write(f, "a 1,2,3\n")
        before = f.read_bytes()
        c, o, e = run(b, [str(f)], td)
        ok = c == 0 and o == "a(1, 2, 3)\n" and f.read_bytes() == before
        c2, o2, e2 = run(b, [], td, stdin_data="a 1,2,3")
        record("P2P-006", ok and c2 == 0 and o2 == "a(1, 2, 3)\n",
               {"file": [c, o, e], "stdin": [c2, o2, e2], "unchanged": f.read_bytes() == before})
        record("P2P-022", ok and c2 == 0 and o2 == "a(1, 2, 3)\n",
               {"file": [c, o, e], "stdin": [c2, o2, e2]})

        c, o, e = run(b, ["--check", str(f)], td)
        changes = [(str(f), "a 1,2,3\n", "a(1, 2, 3)\n")]
        record("P2P-007", c == 5 and diff_matches(o, changes), {"code": c, "out": o, "err": e})
        c1, o1, e1 = run(b, ["-c", str(f)], td)
        record("P2P-003", c == 5 and diff_matches(o, changes) and c1 == 5 and diff_matches(o1, changes), {"long": c, "short": c1})

        c, o, e = run(b, ["--check", "--stdin-filepath", "lib/foo.rb"], td, stdin_data="a 1,2,3\n")
        record("P2P-017", c == 5 and diff_matches(o, [("lib/foo.rb", "a 1,2,3\n", "a(1, 2, 3)\n")]), {"code": c, "out": o, "err": e})

        unchanged = td / "unchanged.rb"
        changed = td / "changed.rb"
        write(unchanged, "a(1, 2, 3)\n")
        write(changed, "a 1,2,3\n")
        time.sleep(0.02)
        m0u = unchanged.stat().st_mtime_ns
        m0c = changed.stat().st_mtime_ns
        c, o, e = run(b, ["-i", str(unchanged), str(changed)], td)
        ok = (c == 0 and o == "" and unchanged.read_text() == "a(1, 2, 3)\n"
              and changed.read_text() == "a(1, 2, 3)\n")
        record("P2P-036", ok, {"code": c, "out": o, "err": e})
        record("P2P-034", ok and unchanged.stat().st_mtime_ns == m0u and changed.stat().st_mtime_ns != m0c,
               {"mtime_unchanged_same": unchanged.stat().st_mtime_ns == m0u,
                "mtime_changed_diff": changed.stat().st_mtime_ns != m0c})

        clean = td / "clean.rb"
        write(clean, "a(1, 2, 3)\n")
        c, o, e = run(b, ["--check", str(clean)], td)
        bad = td / "bad.rb"
        write(bad, "a 1,2,,\n")
        cb, ob, eb = run(b, ["--check", str(bad)], td)
        record("P2P-035", c == 0 and o == "" and cb == 1,
               {"clean": [c, o], "bad": [cb, ob[:100], eb[:100]]})

        c, o, e = run(b, [], td, stdin_data="a 1,2,,")
        record("P2P-027", c == 1 and "syntax error" in e.lower(), {"code": c, "out": o[:100], "err": e[:300]})

        good = td / "good.rb"
        write(good, "a(1, 2)\n")
        c, o, e = run(b, [str(bad), str(td / "missing.rb"), str(good)], td)
        record("P2P-031", c == 0 and o == "a(1, 2)\n" and e.strip() != "",
               {"code": c, "out": o, "err": e[:300]})

        c1, o1, e1 = run(b, ["--fail-fast", str(bad)], td)
        c2, o2, e2 = run(b, ["--fail-fast", str(td / "missing.rb")], td)
        c3, o3, e3 = run(b, ["--fail-fast", "@missing.txt"], td)
        record("P2P-005", c1 == 1 and c2 == 3 and c3 == 3,
               {"syntax": [c1, o1, e1[:100]], "missing": [c2, o2, e2[:100]], "atfile": [c3, o3, e3[:100]]})

        c, o, e = run(b, [], td, stdin_data="a 1,2,3")
        cs, os_, es = run(b, [], td, stdin_data="a 1,2,,")
        cm, om, em = run(b, ["--fail-fast", str(td / "missing.rb")], td)
        record("P2P-012", c == 0 and o == "a(1, 2, 3)\n" and cs == 1 and cm == 3,
               {"ok": [c, o], "syntax": [cs, os_[:80], es[:80]], "io": [cm, om[:80], em[:80]]})

        c, o, e = run(b, ["--header-opt-in"], td, stdin_data="# rubyfmt: true\na 1,2,3\n")
        c2, o2, e2 = run(b, ["--header-opt-in"], td, stdin_data="a 1,2,3")
        record("P2P-013", c == 0 and o == "# rubyfmt: true\na(1, 2, 3)\n" and c2 == 0 and o2 == "a 1,2,3",
               {"opted": [c, o, e], "plain": [c2, o2, e2]})
        c3, o3, e3 = run(b, ["--header-opt-out"], td, stdin_data="# rubyfmt: false\na 1,2,3")
        record("P2P-024", c3 == 0 and o3 == "# rubyfmt: false\na 1,2,3", {"code": c3, "out": o3, "err": e3})

        write(td / ".rubyfmtignore", "ignored.rb\n")
        c, o, e = run(b, ["--stdin-filepath", "ignored.rb"], td, stdin_data="a 1,2,3")
        c2, o2, e2 = run(b, ["--stdin-filepath", "ignored.rb", str(good)], td, stdin_data="a 1,2,3")
        c3, o3, e3 = run(b, ["--stdin-filepath", "ignored.rb", "-i"], td, stdin_data="a 1,2,3")
        record("P2P-023", c == 0 and o == "a 1,2,3" and c2 != 0 and c3 != 0
               and e2.strip() != "" and e3.strip() != "",
               {"ignored": [c, o, e], "path_conflict": [c2, o2, e2[:120]],
                "inplace_conflict": [c3, o3, e3[:120]]})
        c4, o4, e4 = run(b, ["--check", "--stdin-filepath", "ignored.rb"], td, stdin_data="a 1,2,3")
        record("P2P-032", c4 == 0 and o4 == "", {"normal": [c, o], "check": [c4, o4, e4]})

        cp, op, ep = run(b, [], td, stdin_data="a 1,2,3")
        ct, ot, et = run(b, [], td, tty_stdin=True)
        record("P2P-021", cp == 0 and op == "a(1, 2, 3)\n" and ct == 0 and "Usage: rubyfmt" in ot,
               {"pipe": [cp, op, ep], "tty": [ct, ot[:100], et[:100]]})

        d = td / "tree"
        (d / "sub").mkdir(parents=True)
        f1 = d / "one.rb"
        f2 = d / "sub" / "two.rb"
        write(f1, "a 1,2,3\n")
        write(f2, "a 4,5,6\n")
        c, o, e = run(b, [str(f1)], td)
        write(d / "list.txt", str(f1) + "\n")
        write(d / "@list.txt", str(f1) + "\n")
        cl, ol, el = run(b, ["@list.txt"], d)
        cd, od, ed = run(b, [str(d)], td)
        record("P2P-011", c == 0 and o == "a(1, 2, 3)\n" and cl == 0 and ol == "a(1, 2, 3)\n"
               and cd == 0 and "a(1, 2, 3)\n" in od and "a(4, 5, 6)\n" in od,
               {"file": [c, o, e], "at": [cl, ol, el], "dir": [cd, od, ed]})

        lib_ok = True
        if side == "target":
            r = subprocess.run(["go", "test", "./..."], cwd=repo, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=600)
            lib_ok = r.returncode == 0
        p26f = run(b, [str(f1)], td)
        p26l = run(b, ["@list.txt"], d)
        p26d = run(b, [str(d)], td)
        p26s = run(b, [], td, stdin_data="a 1,2,3")
        record("P2P-026", p26f[0] == 0 and p26f[1] == "a(1, 2, 3)\n"
               and p26l[0] == 0 and p26l[1] == "a(1, 2, 3)\n"
               and p26d[0] == 0 and p26s[0] == 0 and p26s[1] == "a(1, 2, 3)\n" and lib_ok,
               {"file": p26f[:2], "at": p26l[:2], "dir": p26d[0], "stdin": p26s[:2],
                "library_suite": lib_ok})

        ig = td / "ign"
        (ig / "a" / "b").mkdir(parents=True)
        write(ig / ".rubyfmtignore", "/a/b/\n")
        write(ig / "a" / "test.rb", "a 1,2,3\n")
        write(ig / "a" / "b" / "test.rb", "a 4,5,6\n")
        c, o, e = run(b, ["-i", "a"], ig)
        record("P2P-029", c == 0 and (ig / "a" / "test.rb").read_text() == "a(1, 2, 3)\n"
               and (ig / "a" / "b" / "test.rb").read_text() == "a 4,5,6\n", {"code": c, "err": e})

        gitd = td / "gitd"
        (gitd / ".git").mkdir(parents=True)
        write(gitd / ".gitignore", "ignored.rb\n")
        write(gitd / "ignored.rb", "a 1,2,3\n")
        c, o, e = run(b, ["--include-gitignored", "-i", str(gitd / "ignored.rb")], gitd)
        write(gitd / "ignored.rb", "a 1,2,3\n")
        c2, o2, e2 = run(b, ["-i", str(gitd / "ignored.rb")], gitd)
        record("P2P-020", c == 0 and c2 == 0 and (gitd / "ignored.rb").read_text() == "a(1, 2, 3)\n",
               {"include": [c, o, e], "explicit": [c2, o2, e2]})

        ex = td / "ext"
        ex.mkdir()
        write(ex / "test.rake", "a 1,2,3\n")
        write(ex / "test.rb", "a 1,2,3\n")
        write(ex / "test.txt", "a 1,2,3\n")
        c, o, e = run(b, ["-i", str(ex / "test.rake")], ex)
        c2, o2, e2 = run(b, ["-i", str(ex)], td)
        record("P2P-028", c == 0 and (ex / "test.rake").read_text() == "a(1, 2, 3)\n"
               and c2 == 0 and (ex / "test.rb").read_text() == "a(1, 2, 3)\n"
               and (ex / "test.txt").read_text() == "a 1,2,3\n",
               {"explicit": [c, o, e], "dir": [c2, o2, e2]})

        stress_ok = True
        stress_detail = {}
        for s in ["ci/methods_stress_test.rb", "ci/string_literals_stress_test.rb",
                  "ci/array_literals_stress_test.rb"]:
            p = root / s
            before = subprocess.run(["ruby", str(p)], cwd=repo, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            c, o, e = run(b, [str(p)], repo)
            tmp = td / ("stress-" + pathlib.Path(s).name)
            write(tmp, o)
            after = subprocess.run(["ruby", str(tmp)], cwd=td, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
            ok = (before.returncode == 0 and c == 0 and after.returncode == 0
                  and before.stdout == after.stdout)
            stress_ok = stress_ok and ok
            stress_detail[s] = ok
        record("P2P-016", stress_ok, stress_detail)

        fixtures = root / "fixtures"
        pairs = []
        for p in sorted(fixtures.rglob("*_actual.rb")):
            e = pathlib.Path(str(p)[:-len("_actual.rb")] + "_expected.rb")
            if e.is_file():
                pairs.append((p, e))
        all_ok = True
        for a, e in pairs:
            exp = e.read_bytes()
            c1, o1, er1 = run_bytes(b, [str(a)], repo)
            c2, o2, er2 = run_bytes(b, [str(e)], repo)
            if not (c1 == 0 and o1 == exp and c2 == 0 and o2 == exp):
                all_ok = False
                break
        record("P2P-025", all_ok, {"pairs": len(pairs)})

        fixture_native_ok = fixture_native["returncode"] == 0
        for oid in ("P2P-001", "P2P-008", "P2P-018", "P2P-030"):
            rel_file, case = FIXTURE_CASE_SPECS[oid]
            ok_case, value, detail = check_fixture_case(b, repo, rel_file, case)
            ok = fixture_native_ok and ok_case
            record(oid, ok, None if ok else (detail or {"fixture_native": fixture_native}), value=value)
            RESULTS[oid].update({
                "fixture_native_result": fixture_native["result"],
                "fixture_case": case,
                "fixture_input": rel_file,
            })
            NATIVE[oid] = {
                "native_test": fixture_test,
                "projection": fixture_native,
                "probe": PROBE_REL,
                "probe_result": _status(ok),
                "value": value,
                "fixture_case": case,
                "fixture_input": rel_file,
            }

    def tree_hash(path):
        h = hashlib.sha256()
        for p in sorted(pathlib.Path(path).rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(path)).encode() + b"\0")
                h.update(p.read_bytes() + b"\0")
        return h.hexdigest()

    expected_editor = "7d60c31360b194977744dafb8e6dbfa863346f30b199a74a8c46926da7984711"
    expected_fixtures = "dd1e01b3cf9f3275ee5af71e7fab90020caf9c309e206ff40ac4509172484c1b"
    got_editor = tree_hash(root / "editor_plugins")
    got_fixtures = tree_hash(root / "fixtures")
    record("P2P-014", got_editor == expected_editor, {"editor_plugins_sha256": got_editor})
    record("P2P-019", got_editor == expected_editor and got_fixtures == expected_fixtures,
           {"editor_plugins_sha256": got_editor, "fixtures_sha256": got_fixtures})

    suite15 = SUITE_META.get("P2P-015", {"result": "FAIL", "detail": "suite result unavailable"})
    record("P2P-015", suite15.get("result") == "PASS", suite15)

    if side == "target":
        scope_ok = (all((root / m).exists() for m in
                        ["go.mod", "cmd/rubyfmt/main.go", "rubyfmt.go", "internal/cli", "internal/formatter"])
                    and not (root / "browser").exists() and not (root / "client").exists())
    else:
        scope_ok = (all((root / m).exists() for m in
                        ["Cargo.toml", "src/main.rs", "librubyfmt", "tests"])
                    and not (root / "browser").exists() and not (root / "client").exists())
    record("P2P-033", scope_ok, {"scope_ok": scope_ok})

    record("P2P-003", RESULTS.get("P2P-003", {}).get("result") == "PASS"
           and RESULTS.get("P2P-025", {}).get("result") == "PASS",
           {"fixture_exact": RESULTS.get("P2P-025")})

    for oid in all_ids:
        RESULTS.setdefault(oid, {"result": "FAIL", "status": "FAIL",
                                 "value": {"result": "FAIL"},
                                 "detail": "observation not executed"})

    values = {oid: RESULTS[oid]["value"] for oid in all_ids}
    observations = [
        {"id": oid, "status": RESULTS[oid]["status"], "result": RESULTS[oid]["result"],
         "value": RESULTS[oid]["value"]}
        for oid in all_ids
    ]
    failed = [oid for oid in all_ids if RESULTS[oid].get("result") != "PASS"]
    out = {
        "side": side,
        "binary": os.path.basename(b),
        "repo": str(root),
        "suite": SUITE_META,
        "native_suite": NATIVE["all"],
        "native_tests": {oid: NATIVE[oid] for oid in all_ids if oid in NATIVE},
        "required_runtime_observations": all_ids,
        "values": values,
        "observations": observations,
        "results": {oid: RESULTS[oid] for oid in all_ids},
        "failed": failed,
        "total": len(all_ids),
    }
    if args.out:
        pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.out).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps({"side": side, "failed": failed, "total": len(all_ids)}), file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    SUITE_META = json.loads(os.environ.get("SUITE_META", "{}"))
    sys.exit(main())
