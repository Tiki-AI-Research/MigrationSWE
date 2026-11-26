#!/usr/bin/env python3
"""Deterministic selfcheck for the mechanism-level verifier fixes.

Not executed during grading.  Run from the task ``tests/`` directory:

    python3 verifier/selfcheck.py

It exercises

* the process-fallback detector against the equivalent-writing family
  (escapes, concatenation, byte arrays, renaming, env indirection, aliases,
  context cancellation, ``os.StartProcess``/``syscall.StartProcess`` selectors
  and ``exec.Cmd`` struct literals) plus valid negative controls; the r6
  hardening additionally resolves the launch capability from Go imports
  (``syscall``/``unix`` aliases, dot imports, raw ``SYS_EXECVE``) and from
  ``exec.Cmd`` *values* (``var``/field/parameter + ``.Path``/``.Args`` +
  ``.Run``/``.Start``) and function values (``var run = exec.Command``); and
* the ``tests_not_tampered`` assertion-fidelity mechanism against an equivalent
  restructure, weakened/deadened assertions, deleted assertions, dropped test
  cases, whole-file rewrites that delete the protected assertions (with and
  without padding) and an independent candidate-authored file at the same path.

The whole-rewrite and dropped-case families are the r4 hardening: derivation is
now detected with symmetric (recall *and* precision) line containment, so a file
that keeps the reference scaffolding but deletes its assertions can no longer
pass as the candidate's own test layout.

Exits non-zero when any expectation is violated.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve()
_TESTS = HERE.parent.parent  # ``.../tests``
# Drop the script directory (``tests/verifier``) so the sibling module
# ``verifier.py`` does not shadow the ``verifier`` package, and import from the
# bundle root instead.  Prefer ``cd tests && python3 -m verifier.selfcheck``.
_SCRIPT_DIR = HERE.parent.resolve()
sys.path[:] = [
    entry
    for entry in sys.path
    if not entry or Path(entry).resolve() != _SCRIPT_DIR
]
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from verifier.detectors.dynamic import detect_source_runtime_dynamic_load  # noqa: E402
from verifier.detectors.ffi import detect_source_runtime_ffi  # noqa: E402
from verifier.detectors.subprocess import detect_source_runtime_subprocess  # noqa: E402
from verifier.evidence import build_hardgate_evidence, build_source_test_baseline  # noqa: E402


def _config() -> dict:
    return json.loads((_TESTS / "config.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1. Process-fallback detector (family coverage)
# --------------------------------------------------------------------------

_PROD = "package core\n\nimport (\n\t\"os\"\n\t\"os/exec\"\n)\n\n"
_CTX = "package core\n\nimport (\n\t\"context\"\n\t\"os/exec\"\n)\n\n"
_TEST = "package core\n\nimport (\n\t\"os/exec\"\n\t\"testing\"\n)\n\n"

_FALLBACK_CASES: tuple[tuple[str, dict[str, str], bool], ...] = (
    ("escape-hex command", {"core/a.go": _PROD + "func f() { exec.Command(\"\\x63\\x61\\x72\\x67\\x6f\") }\n"}, True),
    ("escape-octal command", {"core/a.go": _PROD + "func f() { exec.Command(\"\\143\\141\\162\\147\\157\") }\n"}, True),
    ("escape-unicode command", {"core/a.go": _PROD + "func f() { exec.Command(\"\\u0063\\u0061\\u0072\\u0067\\u006f\") }\n"}, True),
    ("renamed engine", {"core/b.go": _PROD + "func f(p string) { exec.Command(\"seriecore\", \"graph\", p) }\n"}, True),
    ("renamed engine const", {"core/c.go": _PROD + "const engine = \"serie-core-rs\"\n\nfunc f(p string) { exec.Command(engine, \"graph\", p) }\n"}, True),
    ("literal concatenation", {"core/d.go": _PROD + "func f(p string) { const e = \"serie-core-\" + \"rs\"\n\texec.Command(e, \"graph\", p) }\n"}, True),
    ("escape concatenation", {"core/e.go": _PROD + "func f() { exec.Command(\"\\x63\\x61\\x72\" + \"\\x67\\x6f\") }\n"}, True),
    ("byte array command", {"core/f.go": _PROD + "func f() { c := string([]byte{0x63, 0x61, 0x72, 0x67, 0x6f})\n\texec.Command(c) }\n"}, True),
    ("env indirection", {"core/g.go": _PROD + "func f() { c := os.Getenv(\"SERIE_RS_BIN\")\n\texec.Command(c) }\n"}, True),
    ("unresolved sprintf result", {"core/h.go": _PROD + "func f() { exec.Command(fmt.Sprintf(\"%s\", \"cargo\")) }\n"}, True),
    ("struct-field command", {"core/i.go": _PROD + "type r struct{ bin string }\n\nfunc f(x r) { exec.Command(x.bin) }\n"}, True),
    ("import-alias sink", {"core/j.go": "package core\n\nimport ex \"os/exec\"\n\nfunc f() { ex.Command(\"seriecore\") }\n"}, True),
    ("CommandContext second arg", {"core/k.go": _CTX + "func f(ctx context.Context) { exec.CommandContext(ctx, \"seriecore\") }\n"}, True),
    ("slice-index command", {"core/l.go": _PROD + "func f(a []string) { exec.Command(a[0]) }\n"}, True),
    ("token inside test file", {"core/m_test.go": _TEST + "func TestX(t *testing.T) { exec.Command(\"cargo\") }\n"}, True),
    ("direct go plugin loader", {"core/vd.go": "package core\n\nimport \"plugin\"\n\nfunc f() { p, _ := plugin.Open(\"x.so\"); _ = p }\n"}, True),
    ("aliased go plugin loader", {"core/ve.go": "package core\n\nimport pl \"plugin\"\n\nfunc f() { p, _ := pl.Open(\"seriecore.so\"); _ = p }\n"}, True),
    # valid negative controls
    ("sanctioned git literal", {"core/n.go": _PROD + "func f() { exec.Command(\"git\", \"log\") }\n"}, False),
    ("sanctioned git const", {"core/o.go": _PROD + "const gitBinary = \"git\"\n\nfunc f() { exec.Command(gitBinary, \"log\") }\n"}, False),
    ("sanctioned git LookPath", {"core/p.go": _PROD + "func f() { bin, _ := exec.LookPath(\"git\")\n\texec.Command(bin, \"log\") }\n"}, False),
    ("sanctioned git in test", {"core/q_test.go": _TEST + "func TestY(t *testing.T) { exec.Command(\"git\", \"init\") }\n"}, False),
    ("protected rust user command", {"src/external.rs": "use std::process::Command;\nfn run(c: &[String]) { let mut child = Command::new(&c[0]).spawn().unwrap(); }\n"}, False),
    # ---------------------------------------------------------------
    # r5 sink-family hardening: equivalent Go process-launch writers that the
    # old fixed ``exec.Command(`` spelling list never reached, so a
    # run-time-built engine name could be launched without any L2 evidence.
    # ---------------------------------------------------------------
    ("os.StartProcess literal", {"core/oa.go": _PROD + "func f() { os.StartProcess(\"cargo\", []string{\"cargo\"}, &os.ProcAttr{}) }\n"}, True),
    ("os.StartProcess byte array", {"core/ob.go": _PROD + "func eng() string { return string([]byte{0x63, 0x61, 0x72, 0x67, 0x6f}) }\n\nfunc f() { os.StartProcess(eng(), nil, &os.ProcAttr{}) }\n"}, True),
    ("os.StartProcess octal escape", {"core/oc.go": _PROD + "func f() { os.StartProcess(\"\\143\\141\\162\\147\\157\", nil, &os.ProcAttr{}) }\n"}, True),
    ("os.StartProcess concatenated", {"core/od.go": _PROD + "func f() { os.StartProcess(\"car\"+\"go\", nil, &os.ProcAttr{}) }\n"}, True),
    ("os.StartProcess unresolved var", {"core/oe.go": _PROD + "func f() { name := engineName()\n\tos.StartProcess(name, nil, &os.ProcAttr{}) }\n"}, True),
    ("os import-alias StartProcess", {"core/of.go": "package core\n\nimport o \"os\"\n\nfunc f() { o.StartProcess(\"seriecore\", nil, &o.ProcAttr{}) }\n"}, True),
    ("syscall.StartProcess", {"core/og.go": "package core\n\nimport \"syscall\"\n\nfunc f() { syscall.StartProcess(\"cargo\", nil, &syscall.ProcAttr{}) }\n"}, True),
    ("exec.Cmd struct Path", {"core/oh.go": _PROD + "func f() { cmd := &exec.Cmd{Path: \"cargo\", Args: []string{\"cargo\", \"build\"}}\n\t_ = cmd }\n"}, True),
    ("exec.Cmd struct Path bytes", {"core/oi.go": _PROD + "func nm() string { return string([]byte{0x63, 0x61, 0x72, 0x67, 0x6f}) }\n\nfunc f() { cmd := &exec.Cmd{Path: nm(), Args: []string{nm()}}\n\t_ = cmd }\n"}, True),
    ("exec.Cmd struct Args only", {"core/oj.go": _PROD + "func f() { cmd := &exec.Cmd{Args: []string{\"seriecore\", \"graph\"}}\n\t_ = cmd }\n"}, True),
    ("exec.Cmd struct Args bytes", {"core/ok.go": _PROD + "func f() { cmd := &exec.Cmd{Args: []string{string([]byte{0x63, 0x61, 0x72, 0x67, 0x6f}), \"graph\"}}\n\t_ = cmd }\n"}, True),
    ("exec import-alias Cmd struct", {"core/ol.go": "package core\n\nimport ex \"os/exec\"\n\nfunc f() { cmd := &ex.Cmd{Path: \"cargo\"}\n\t_ = cmd }\n"}, True),
    # r5 negative controls: the sanctioned ``git`` command stays allowed through
    # every launch writer.
    ("sanctioned os.StartProcess git", {"core/om.go": _PROD + "func f() { os.StartProcess(\"git\", []string{\"git\", \"log\"}, &os.ProcAttr{}) }\n"}, False),
    ("sanctioned exec.Cmd struct git path", {"core/on.go": _PROD + "func f() { cmd := &exec.Cmd{Path: \"git\", Args: []string{\"git\", \"log\"}}\n\t_ = cmd }\n"}, False),
    ("sanctioned exec.Cmd struct git args", {"core/oo.go": _PROD + "func f() { cmd := &exec.Cmd{Args: []string{\"git\", \"log\"}}\n\t_ = cmd }\n"}, False),
    # ---------------------------------------------------------------
    # r6 semantic sink-family hardening: the launch capability is resolved
    # from the file's imports (alias -> path) and from ``exec.Cmd`` *values*,
    # so the ``syscall`` alias method family, field-populated ``exec.Cmd``
    # values, raw execve syscalls, dot imports and function values are all
    # sinks -- not only their canonical spellings.
    # ---------------------------------------------------------------
    ("syscall import-alias Exec", {"core/ra.go": "package core\n\nimport sc \"syscall\"\n\nfunc f() { sc.Exec(\"cargo\", nil, nil) }\n"}, True),
    ("syscall import-alias ForkExec bytes", {"core/rb.go": "package core\n\nimport sc \"syscall\"\n\nfunc f() { sc.ForkExec(string([]byte{0x63, 0x61, 0x72, 0x67, 0x6f}), nil, nil) }\n"}, True),
    ("syscall import-alias StartProcess", {"core/rc.go": "package core\n\nimport sc \"syscall\"\n\nfunc f() { sc.StartProcess(\"cargo\", nil, &sc.ProcAttr{}) }\n"}, True),
    ("unix import-alias Exec", {"core/rd.go": "package core\n\nimport ux \"golang.org/x/sys/unix\"\n\nfunc f() { ux.Exec(\"seriecore\", nil, nil) }\n"}, True),
    ("raw SYS_EXECVE syscall", {"core/re.go": "package core\n\nimport \"syscall\"\n\nfunc f() { syscall.RawSyscall(syscall.SYS_EXECVE, 0, 0, 0) }\n"}, True),
    ("syscall dot import Exec", {"core/rf.go": "package core\n\nimport . \"syscall\"\n\nfunc f() { Exec(\"cargo\", nil, nil) }\n"}, True),
    ("exec.Cmd value field Path then Run", {"core/rg.go": "package core\n\nimport \"os/exec\"\n\nfunc f() error {\n\tvar c exec.Cmd\n\tc.Path = string([]byte{0x63, 0x61, 0x72, 0x67, 0x6f})\n\tc.Args = []string{c.Path, \"graph\"}\n\treturn c.Run()\n}\n"}, True),
    ("exec.Cmd value Args only then Start", {"core/rh.go": "package core\n\nimport \"os/exec\"\n\nfunc f() error {\n\tvar c exec.Cmd\n\tc.Args = []string{\"seriecore\"}\n\treturn c.Start()\n}\n"}, True),
    ("exec.Cmd struct field receiver", {"core/ri.go": "package core\n\nimport \"os/exec\"\n\ntype runner struct{ c exec.Cmd }\n\nfunc (r *runner) launch() error {\n\tr.c.Path = \"cargo\"\n\treturn r.c.Run()\n}\n"}, True),
    ("exec.Cmd new() field then Run", {"core/rj.go": "package core\n\nimport \"os/exec\"\n\nfunc f() error {\n\tc := new(exec.Cmd)\n\tc.Path = \"serie-core-rs\"\n\treturn c.Run()\n}\n"}, True),
    ("exec.Command function value", {"core/rk.go": "package core\n\nimport \"os/exec\"\n\nvar run = exec.Command\n\nfunc f() { _ = run(\"cargo\", \"graph\") }\n"}, True),
    ("exec.Command bare function value", {"core/rr.go": "package core\n\nimport \"os/exec\"\n\nvar run = exec.Command\n"}, True),
    # r6 negative controls: every writer still resolves the sanctioned ``git``.
    ("sanctioned syscall alias Exec git", {"core/rl.go": "package core\n\nimport sc \"syscall\"\n\nfunc f() { sc.Exec(\"/usr/bin/git\", []string{\"git\"}, nil) }\n"}, False),
    ("sanctioned unix alias Exec git", {"core/rm.go": "package core\n\nimport ux \"golang.org/x/sys/unix\"\n\nfunc f() { ux.Exec(\"/usr/bin/git\", nil, nil) }\n"}, False),
    ("sanctioned exec.Cmd value field git", {"core/rn.go": "package core\n\nimport \"os/exec\"\n\nfunc f() error {\n\tvar c exec.Cmd\n\tc.Path = \"/usr/bin/git\"\n\tc.Args = []string{\"git\", \"log\"}\n\treturn c.Run()\n}\n"}, False),
    ("sanctioned exec.Cmd value assigned git", {"core/ro.go": "package core\n\nimport \"os/exec\"\n\nfunc f() error {\n\tvar c *exec.Cmd\n\tc = exec.Command(\"git\", \"log\")\n\treturn c.Run()\n}\n"}, False),
    ("unpopulated exec.Cmd parameter", {"core/rp.go": "package core\n\nimport \"os/exec\"\n\nfunc kick(c *exec.Cmd) error { return c.Run() }\n\nfunc f() error { return kick(exec.Command(\"git\", \"log\")) }\n"}, False),
    ("sanctioned exec.Command function value git", {"core/rq.go": "package core\n\nimport \"os/exec\"\n\nvar runCtl = exec.Command\n\nfunc f() error { return runCtl(\"git\", \"log\").Run() }\n"}, False),
    ("sanctioned helper returning exec.Cmd", {"core/rs.go": "package core\n\nimport \"os/exec\"\n\nfunc gitCommand(args ...string) *exec.Cmd {\n\treturn exec.Command(\"git\", args...)\n}\n"}, False),
)


def check_fallback_detector() -> list[str]:
    failures: list[str] = []
    config = _config()
    for description, sources, expect in _FALLBACK_CASES:
        with tempfile.TemporaryDirectory(prefix="serie-selfcheck-") as directory:
            root = Path(directory)
            for name, text in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            hits = detect_source_runtime_subprocess(root, config)
            hits = [
                *hits,
                *detect_source_runtime_dynamic_load(root, config),
                *detect_source_runtime_ffi(root, config),
            ]
        if bool(hits) != expect:
            failures.append(
                f"detector[{description}]: expected {'hit' if expect else 'no-hit'}, got {hits}"
            )
    return failures


# --------------------------------------------------------------------------
# 2. tests_not_tampered assertion fidelity
# --------------------------------------------------------------------------

_REFERENCE = """package core

import (
\t"reflect"
\t"testing"
)

func TestSortEdges(t *testing.T) {
\tgot := SortEdges([]int{2, 1})
\twant := []int{1, 2}
\tif !reflect.DeepEqual(got, want) {
\t\tt.Fatalf("unexpected: %v", got)
\t}
}

func TestMaxLane(t *testing.T) {
\tgot := MaxLane([]int{0, 1, 1})
\tif got != 1 {
\t\tt.Fatalf("unexpected max: %d", got)
\t}
}
"""

_RESTRUCTURED = """package core

import (
\t"reflect"
\t"testing"
)

func TestSortEdges(t *testing.T) {
\tcases := []struct {
\t\tname string
\t\tin   []int
\t}{
\t\t{name: "mixed", in: []int{2, 1}},
\t}
\tfor _, tc := range cases {
\t\tt.Run(tc.name, func(t *testing.T) {
\t\t\tgot := SortEdges(tc.in)
\t\t\twant := []int{1, 2}
\t\t\tif !reflect.DeepEqual(got, want) {
\t\t\t\tt.Fatalf("unexpected: %v", got)
\t\t\t}
\t\t})
\t}
}

func TestMaxLane(t *testing.T) {
\tgot := MaxLane([]int{0, 1, 1})
\tif got != 1 {
\t\tt.Fatalf("unexpected max: %d", got)
\t}
}
"""

_WEAKENED_GUARD = _REFERENCE.replace(
    "if !reflect.DeepEqual(got, want) {", "if false && !reflect.DeepEqual(got, want) {"
)

_DEAD_NESTED = _REFERENCE.replace(
    "\tif !reflect.DeepEqual(got, want) {\n\t\tt.Fatalf(\"unexpected: %v\", got)\n\t}",
    "\tif false {\n\t\tif !reflect.DeepEqual(got, want) {\n\t\t\tt.Fatalf(\"unexpected: %v\", got)\n\t\t}\n\t}",
)

_DELETED_ASSERTION = _REFERENCE.replace(
    "\tif got != 1 {\n\t\tt.Fatalf(\"unexpected max: %d\", got)\n\t}\n", ""
)

# ``downgraded assertion``: ``t.Fatalf`` -> ``t.Logf`` keeps the structural token
# stream (every call identifier collapses to ``ID``) but turns a failing
# assertion into a passing log; the structural shortcut must not hide it.
_DOWNGRADED_ASSERTION = _REFERENCE.replace("t.Fatalf(", "t.Logf(")

_INDEPENDENT = """package core

import "testing"

func TestOwnGraphLayout(t *testing.T) {
\tfor i := 0; i < 3; i++ {
\t\tif i < 0 {
\t\t\tt.Errorf("impossible %d", i)
\t\t}
\t}
}
"""

# ``whole rewrite``: keep the reference's imports and call scaffolding, delete
# the protected assertion blocks and replace them with the reference's own
# non-assertion lines.  Deleting the assertions also deletes most reference
# lines, so recall collapses; the surviving lines are nearly the whole (tiny)
# file, so precision stays high.  This is the ``weaken-baseline-assert`` family
# that the recall-only derivative gate used to wave through.
_WHOLE_REWRITE = """package core

import (
\t"reflect"
\t"testing"
)

// TestSortEdgesRewritten removes the protected assertions.
func TestSortEdgesRewritten(t *testing.T) {
\tgot := SortEdges([]int{2, 1})
\twant := []int{1, 2}
\t_ = got
\t_ = want
\t_ = reflect.DeepEqual
}
"""

# ``whole rewrite with padding``: same deletion, plus filler distractors.  The
# derivation must still be detected while the reference lines remain a majority
# of the rewritten file.
_WHOLE_REWRITE_PADDED = """package core

import (
\t"reflect"
\t"testing"
)

var rewrittenPadding = []string{"a", "b", "c"}

func rewrittenHelper(value int) int {
\treturn value
}

// TestSortEdgesRewritten removes the protected assertions.
func TestSortEdgesRewritten(t *testing.T) {
\tgot := SortEdges([]int{2, 1})
\twant := []int{1, 2}
\t_ = got
\t_ = want
\t_ = reflect.DeepEqual
\t_ = rewrittenHelper
\t_ = rewrittenPadding
}
"""

# ``dropped test case``: keep most of the reference body but delete one whole
# test function (and its assertion) -- a high-recall derivative.
_DROPPED_CASE = _REFERENCE.replace(
    """func TestMaxLane(t *testing.T) {
\tgot := MaxLane([]int{0, 1, 1})
\tif got != 1 {
\t\tt.Fatalf("unexpected max: %d", got)
\t}
}
""",
    "",
)

_FIDELITY_CASES: tuple[tuple[str, str, bool], ...] = (
    ("identical", _REFERENCE, False),
    ("equivalent restructure", _RESTRUCTURED, False),
    ("weakened guard", _WEAKENED_GUARD, True),
    ("dead nested guard", _DEAD_NESTED, True),
    ("deleted assertion", _DELETED_ASSERTION, True),
    ("independent candidate file", _INDEPENDENT, False),
    ("whole rewrite deleting assertions", _WHOLE_REWRITE, True),
    ("whole rewrite with padding", _WHOLE_REWRITE_PADDED, True),
    ("dropped whole test case", _DROPPED_CASE, True),
    ("downgraded assertion to log", _DOWNGRADED_ASSERTION, True),
)


def _tamper_status(files: dict[str, str], baseline: dict) -> str:
    config = _config()
    with tempfile.TemporaryDirectory(prefix="serie-fidelity-") as directory:
        case = Path(directory)
        for name, text in files.items():
            path = case / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        evidence = build_hardgate_evidence(
            case,
            config=config,
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline=baseline,
            test_baseline_source="target-test-baseline",
        )
    return str(evidence["tests_not_tampered"]["status"])


def check_test_fidelity() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="serie-reference-") as directory:
        reference_root = Path(directory)
        (reference_root / "core").mkdir()
        (reference_root / "core" / "graph_test.go").write_text(_REFERENCE, encoding="utf-8")
        baseline = build_source_test_baseline(reference_root)
    for description, text, expect_tamper in _FIDELITY_CASES:
        status = _tamper_status({"core/graph_test.go": text}, baseline)
        tampered = status == "FAIL"
        if tampered != expect_tamper:
            failures.append(
                f"fidelity[{description}]: expected "
                f"{'FAIL' if expect_tamper else 'PASS'}, got {status}"
            )
    # A candidate file under a different path is never tampering.
    status = _tamper_status({"core/other_test.go": _INDEPENDENT}, baseline)
    if status not in {"PASS", "NA"}:
        failures.append(f"fidelity[new-path]: expected PASS/NA, got {status}")
    return failures


def main() -> int:
    failures = [*check_fallback_detector(), *check_test_fidelity()]
    for line in failures:
        print("FAIL:", line, file=sys.stderr)
    if failures:
        print(f"selfcheck: {len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("selfcheck: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
