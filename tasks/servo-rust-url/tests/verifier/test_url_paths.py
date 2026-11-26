"""Exercise the actual Go path checks against compliant doubles and mutants.

The Dockerfile runs this entire module with its Go toolchain. Host-side Python
checks can run ProbeWiringChecks separately when Go is unavailable; the Go test
is never silently skipped or reported as passed without execution.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "assets/shared/integration/probe/probe.go"
FUNCTIONS = (
    "checkMinimalPathContract", "checkPathRecordContract", "pathRecordState",
    "checkClearedPathContract", "checkURLConformance",
)


def contract_functions(source):
    functions = []
    for name in FUNCTIONS:
        match = re.search(r"^func " + name + r"\([^\n]*\) [^\n]*\{\n.*?^\}", source, re.M | re.S)
        if match is None:
            raise ValueError("missing complete Go contract function: " + name)
        functions.append(match.group())
    return "\n\n".join(functions)


FAKE_URL = r'''package url

import (
    "errors"
    neturl "net/url"
    "strings"
)

var ErrRelativeURLWithCannotBeABaseBase = errors.New("cannot be a base")
var Mode = "preserve"

type Host struct{}
type URL struct {
    scheme, host, path, query, fragment string
    authority, opaque, qok, fok, cleared, mutated bool
    cachedSegments []string
}

func mode(name string) bool { return Mode == name || Mode == "root_" + name }
func rootVariant() bool { return Mode == "root" || strings.HasPrefix(Mode, "root_") }

func Parse(input string) (*URL, error) {
    parsed, err := neturl.Parse(input)
    if err != nil { return nil, err }
    rest := strings.TrimPrefix(input, parsed.Scheme + ":")
    beforeFragment, _, fok := strings.Cut(rest, "#")
    _, _, qok := strings.Cut(beforeFragment, "?")
    u := &URL{scheme: parsed.Scheme, host: parsed.Host, path: parsed.EscapedPath(),
        query: parsed.RawQuery, fragment: parsed.Fragment,
        authority: strings.HasPrefix(rest, "//"), qok: qok, fok: fok}
    u.opaque = !u.authority && !strings.HasPrefix(rest, "/")
    if u.opaque { u.path = parsed.Opaque }
    if u.IsSpecial() && u.path == "" { u.path = "/" }
    return u, nil
}

func (u *URL) IsSpecial() bool { return u.scheme == "https" || u.scheme == "file" }
func (u *URL) Scheme() string { return u.scheme }
func (u *URL) Username() string { return "" }
func (u *URL) Password() (string, bool) { return "", false }
func (u *URL) AsStr() string {
    s := u.scheme + ":"
    if u.authority { s += "//" + u.host }
    s += u.path
    if u.qok { s += "?" + u.query }
    if u.fok { s += "#" + u.fragment }
    if Mode == "bad_serialization" && u.mutated { s += "extra" }
    return s
}
func (u *URL) Path() string { return u.path }
func (u *URL) CannotBeABase() bool {
    if Mode == "wrong_opaque_flag" { return !u.opaque }
    return u.opaque
}
func (u *URL) HasAuthority() bool {
    if Mode == "bad_authority_flag" && u.mutated { return false }
    return u.authority
}
func (u *URL) HasHost() bool { return u.host != "" }
func (u *URL) HostStr() (string, bool) {
    if Mode == "bad_host_accessor" && u.mutated { return "wrong.example", true }
    return u.host, u.host != ""
}
func (u *URL) Host() (Host, bool) { return Host{}, u.host != "" }
func (u *URL) Domain() (string, bool) { return u.host, u.host != "" }
func (u *URL) Port() (uint16, bool) {
    return 0, Mode == "opaque_components" && u.cleared && u.opaque
}
func (u *URL) Query() (string, bool) { return u.query, u.qok }
func (u *URL) Fragment() (string, bool) { return u.fragment, u.fok }
func (u *URL) PathSegments() ([]string, bool) {
    if u.opaque {
        if Mode == "opaque_segments" { return []string{"opaque"}, true }
        return nil, false
    }
    if u.path == "" {
        if !u.mutated {
            if Mode == "phantom_parsed_segment" { return []string{""}, true }
            if mode("empty_slice") { return []string{}, true }
            if mode("nil_slice") { return nil, true }
            return nil, false
        }
        switch Mode {
        case "lost_segment": return nil, false
        case "nil_segment": return nil, true
        case "zero_segments": return []string{}, true
        case "extra_segment": return []string{"", ""}, true
        }
        return []string{""}, true
    }
    if Mode == "missing_root_segment" && u.path == "/" { return nil, false }
    if mode("bad_root_segments") && u.cleared && u.path == "/" { return []string{"wrong"}, true }
    if mode("join_mutates_segments") && u.cleared && u.path == "/" {
        if u.cachedSegments == nil { u.cachedSegments = []string{""} }
        return u.cachedSegments, true
    }
    path := strings.TrimPrefix(u.path, "/")
    if Mode == "decoded_segments" { path, _ = neturl.PathUnescape(path) }
    parts := strings.Split(path, "/")
    if Mode == "collapsed_empty_segments" {
        filtered := []string{}
        for _, part := range parts { if part != "" { filtered = append(filtered, part) } }
        return filtered, true
    }
    return parts, true
}
func (u *URL) SetPath(path string) {
    u.cleared = true
    if u.IsSpecial() { u.path = "/" } else { u.path = path }
    if !u.authority && !u.IsSpecial() {
        if rootVariant() { u.path, u.opaque = "/", mode("stale_opaque") } else { u.opaque = !mode("stale_opaque") }
    }
    if mode("all_opaque") { u.opaque = true }
    if mode("clear_noop") { u.path = "/old" }
    if mode("drop_query") { u.query, u.qok = "", false }
    if mode("drop_fragment") { u.fragment, u.fok = "", false }
    if mode("drop_empty_query") && u.query == "" { u.qok = false }
    if mode("drop_empty_fragment") && u.fragment == "" { u.fok = false }
}
func (u *URL) Join(reference string) (*URL, error) {
    if mode("all_joins_fail") && u.cleared { return nil, errors.New("join failed") }
    if u.opaque && !strings.HasPrefix(reference, "#") {
        if Mode == "wrong_join_error" { return nil, errors.New("wrong error") }
        if Mode != "join_accepts_opaque" { return nil, ErrRelativeURLWithCannotBeABaseBase }
    }
    result := *u
    result.fok, result.fragment = false, ""
    if strings.HasPrefix(reference, "#") {
        result.fok, result.fragment = true, reference[1:]
        if mode("fragment_drops_query") && u.cleared { result.qok, result.query = false, "" }
    } else if strings.HasPrefix(reference, "?") {
        result.qok, result.query = true, reference[1:]
    } else {
        result.path = "/" + reference
        result.qok, result.query = false, ""
        if mode("wrong_join_path") && u.cleared { result.path = "/wrong" }
    }
    if mode("join_mutates_base") && u.cleared { u.fragment, u.fok = "mutated", true }
    if mode("join_mutates_segments") && u.cleared && len(u.cachedSegments) != 0 { u.cachedSegments[0] = "mutated" }
    if mode("join_mutates_flag") && u.cleared { u.opaque = !u.opaque }
    return &result, nil
}

type PathSegmentsMut struct { u *URL }
func (u *URL) PathSegmentsMut() (*PathSegmentsMut, error) {
    if u.opaque {
        if Mode == "mutating_rejection" { u.path = "/" }
        if Mode != "opaque_mutator" { return nil, errors.New("opaque path") }
    }
    if mode("nil_mutator") { return nil, nil }
    if mode("reject_hierarchical_mutator") && u.cleared { return nil, errors.New("wrong rejection") }
    if strings.Contains(Mode, "normalize") && u.path == "" { u.path = "/" }
    return &PathSegmentsMut{u}, nil
}
func (m *PathSegmentsMut) corrupt() {
    m.u.mutated = true
    switch Mode {
    case "mutator_changes_query": m.u.query = "wrong"
    case "mutator_changes_fragment": m.u.fragment = "wrong"
    case "mutator_loses_query": m.u.qok = false
    case "mutator_loses_fragment": m.u.fok = false
    case "bad_minimal_path": m.u.path = "/extra"
    }
}
func (m *PathSegmentsMut) Clear() *PathSegmentsMut {
    if Mode != "clear_pop_preserves_empty" { m.u.path = "/" }
    m.corrupt(); return m
}
func (m *PathSegmentsMut) Pop() *PathSegmentsMut { return m.Clear() }
func (m *PathSegmentsMut) PopIfEmpty() *PathSegmentsMut { m.corrupt(); return m }
func (m *PathSegmentsMut) Push(segment string) *PathSegmentsMut {
    if Mode == "literal_dot" { m.u.path += "/" + segment }
    m.corrupt(); return m
}
func (m *PathSegmentsMut) Extend(segments []string) *PathSegmentsMut { m.corrupt(); return m }
'''


GO_TEST = r'''package main

import (
    "encoding/json"
    "os"
    "path/filepath"
    "strings"
    "testing"
    "github.com/servo/rust-url/url"
)

var compliantModes = []string{"preserve", "normalize", "empty_slice", "nil_slice", "root", "root_normalize", "root_empty_slice", "root_nil_slice"}

var parsedCases = []struct{ input, href, path string }{
    {"custom://example.net?q#frag", "custom://example.net?q#frag", ""},
    {"custom://example.net/", "custom://example.net/", "/"},
    {"custom://", "custom://", ""},
    {"custom://?", "custom://?", ""},
    {"custom://#", "custom://#", ""},
    {"custom:/a%2Fb//c/", "custom:/a%2Fb//c/", "/a%2Fb//c/"},
    {"https://example.net", "https://example.net/", "/"},
    {"file:///a/", "file:///a/", "/a/"},
    {"data:text/plain,hi", "data:text/plain,hi", "text/plain,hi"},
    {"custom:", "custom:", ""},
    {"blob:", "blob:", ""},
}

func TestCompliantVariants(t *testing.T) {
    for _, mode := range compliantModes {
        t.Run(mode, func(t *testing.T) {
            url.Mode = mode
            for _, check := range []func() []string{checkMinimalPathContract, checkClearedPathContract} {
                if failures := check(); len(failures) != 0 { t.Fatal(failures) }
            }
            for _, tc := range parsedCases {
                u, err := url.Parse(tc.input)
                if err != nil { t.Fatal(err) }
                if failures := checkPathRecordContract(u, tc.href, tc.path); len(failures) != 0 { t.Fatal(tc.input, failures) }
            }
        })
    }
}

func TestClearedPathMutants(t *testing.T) {
    for _, mode := range []string{
        "stale_opaque", "join_accepts_opaque", "wrong_join_error", "opaque_components",
        "opaque_mutator", "mutating_rejection", "all_opaque", "clear_noop",
        "drop_query", "drop_fragment", "drop_empty_query", "drop_empty_fragment",
        "all_joins_fail", "fragment_drops_query", "wrong_join_path", "join_mutates_base",
        "nil_mutator", "reject_hierarchical_mutator", "bad_root_segments",
        "join_mutates_flag", "join_mutates_segments",
    } {
        t.Run(mode, func(t *testing.T) {
            url.Mode = mode
            if failures := checkClearedPathContract(); len(failures) == 0 { t.Fatal("accepted mutant", mode) }
        })
    }
}

func TestGPT05Regression(t *testing.T) {
    url.Mode = "stale_opaque"
    failures := strings.Join(checkClearedPathContract(), "\n")
    if !strings.Contains(failures, "SetPath(empty) left an inconsistent URL record") {
        t.Fatalf("accepted stale opaque state: %s", failures)
    }
    if strings.Contains(failures, "changed its cleared base URL") {
        t.Fatalf("invented a Join mutation after an invalid setter: %s", failures)
    }
}

func TestRootPathMutants(t *testing.T) {
    for _, mode := range []string{
        "stale_opaque", "all_opaque", "clear_noop", "drop_query", "drop_fragment",
        "drop_empty_query", "drop_empty_fragment", "all_joins_fail", "fragment_drops_query",
        "wrong_join_path", "join_mutates_base", "join_mutates_flag", "join_mutates_segments",
        "nil_mutator", "reject_hierarchical_mutator", "bad_root_segments",
    } {
        t.Run(mode, func(t *testing.T) {
            url.Mode = "root_" + mode
            if failures := checkClearedPathContract(); len(failures) == 0 { t.Fatal("accepted root mutant", mode) }
        })
    }
}

func TestJoinMutationDiagnostics(t *testing.T) {
    for _, mode := range []string{"join_mutates_base", "root_join_mutates_base", "root_join_mutates_flag", "root_join_mutates_segments"} {
        t.Run(mode, func(t *testing.T) {
            url.Mode = mode
            failures := strings.Join(checkClearedPathContract(), "\n")
            if !strings.Contains(failures, "changed its cleared base URL") { t.Fatal("missed real Join mutation", failures) }
        })
    }
    for _, mode := range []string{"clear_noop", "root_clear_noop", "stale_opaque", "root_stale_opaque"} {
        t.Run(mode, func(t *testing.T) {
            url.Mode = mode
            failures := strings.Join(checkClearedPathContract(), "\n")
            if failures == "" || strings.Contains(failures, "changed its cleared base URL") { t.Fatal("wrong diagnosis", failures) }
        })
    }
}

func TestParsedPathMutants(t *testing.T) {
    for _, mode := range []string{"phantom_parsed_segment", "missing_root_segment", "decoded_segments", "collapsed_empty_segments", "opaque_segments", "wrong_opaque_flag"} {
        t.Run(mode, func(t *testing.T) {
            url.Mode = mode
            failures := []string{}
            for _, tc := range parsedCases {
                u, err := url.Parse(tc.input)
                if err != nil { t.Fatal(err) }
                failures = append(failures, checkPathRecordContract(u, tc.href, tc.path)...)
            }
            if len(failures) == 0 { t.Fatal("accepted parsed-path mutant", mode) }
        })
    }
}

func TestCorpusUsesPathAccessors(t *testing.T) {
    repoRoot = t.TempDir()
    directory := filepath.Join(repoRoot, "url", "tests")
    if err := os.MkdirAll(directory, 0755); err != nil { t.Fatal(err) }
    entries := []map[string]string{}
    for _, tc := range parsedCases {
        entries = append(entries, map[string]string{"input": tc.input, "href": tc.href, "pathname": tc.path})
    }
    data, err := json.Marshal(entries)
    if err != nil { t.Fatal(err) }
    if err := os.WriteFile(filepath.Join(directory, "urltestdata.json"), data, 0644); err != nil { t.Fatal(err) }
    for _, mode := range compliantModes {
        url.Mode = mode
        if failures := checkURLConformance(); len(failures) != 0 { t.Fatal(mode, failures) }
    }
    url.Mode = "phantom_parsed_segment"
    failures := strings.Join(checkURLConformance(), "\n")
    if !strings.Contains(failures, "phantom segments") { t.Fatal("corpus missed GPT01 regression", failures) }
}

func TestMinimalPathMutants(t *testing.T) {
    for _, mode := range []string{
        "lost_segment", "nil_segment", "zero_segments", "extra_segment", "clear_pop_preserves_empty",
        "bad_minimal_path", "literal_dot", "bad_serialization", "bad_authority_flag", "bad_host_accessor",
        "mutator_changes_query", "mutator_changes_fragment", "mutator_loses_query", "mutator_loses_fragment",
        "nil_mutator",
    } {
        t.Run(mode, func(t *testing.T) {
            url.Mode = mode
            if failures := checkMinimalPathContract(); len(failures) == 0 { t.Fatal("accepted mutant", mode) }
        })
    }
}
'''


class ProbeWiringChecks(unittest.TestCase):
    def test_fixture_implements_extracted_method_calls(self):
        # Catch a stale test double before the Docker-only Go compilation step.
        # This checks fixture completeness; the Go suite still checks types and
        # behavior and remains mandatory in the evaluator image build.
        methods = set(re.findall(
            r"func\s+\([^\n]*\*(?:URL|PathSegmentsMut)\)\s+([A-Z]\w*)\(", FAKE_URL
        ))
        packages_and_testing = {"errors", "fmt", "json", "os", "reflect", "strings", "url", "filepath", "t"}
        source = contract_functions(PROBE.read_text()) + GO_TEST
        calls = {(receiver, method) for receiver, method in re.findall(
            r"\b([a-zA-Z_]\w*)\.([A-Z]\w*)\(", source
        ) if receiver not in packages_and_testing}
        missing = sorted(f"{receiver}.{method}" for receiver, method in calls if method not in methods)
        self.assertEqual(missing, [], "Go regression fixture is missing methods used by extracted checks")

    def test_new_checks_execute_under_f035(self):
        source = PROBE.read_text()
        contract_functions(source)
        main_check = re.search(r"^func checkURLMutationContract\(\).*?^\}", source, re.M | re.S).group()
        for name in ("checkMinimalPathContract", "checkClearedPathContract"):
            self.assertIn(name + "()...", main_check)
        self.assertIn('enforceContract([]string{"F2P-035"}, checkURLMutationContract)', source)

    def test_corpus_checks_path_accessors(self):
        source = PROBE.read_text()
        check = re.search(r"^func checkURLConformance\(\).*?^\}", source, re.M | re.S).group()
        self.assertIn("checkPathRecordContract(parsed, expectedHref, expectedPath)", check)
        self.assertIn('enforceContract([]string{"F2P-003"}, checkURLConformance)', source)

    def test_embedded_assets_match_runtime_assets(self):
        source = (ROOT / "verifier/test.patch").read_text()
        projected = {}
        for block in re.split(r"(?=^diff --git )", source, flags=re.M):
            if not block.startswith("diff --git "):
                continue
            header = re.fullmatch(r"diff --git a/tests/((?:shared|target|fixtures)/.+) b/tests/\1", block.splitlines()[0])
            if header is None:
                continue
            self.assertIn("--- /dev/null\n", block, header[1])
            content = ""
            for line in block.splitlines(keepends=True):
                if line.startswith("+") and not line.startswith("+++ "):
                    content += line[1:]
                elif line.startswith("\\ No newline at end of file"):
                    content = content.removesuffix("\n")
            self.assertNotIn(header[1], projected)
            projected[header[1]] = content.encode()
        assets = {path.relative_to(ROOT / "assets").as_posix(): path.read_bytes()
                  for directory in ("shared", "target", "fixtures")
                  for path in (ROOT / "assets" / directory).rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts}
        self.assertEqual(set(projected), set(assets))
        for name, content in assets.items():
            self.assertEqual(projected[name], content, name)


class GoBehaviorChecks(unittest.TestCase):
    def test_compliant_variants_and_mutants(self):
        go = shutil.which("go")
        self.assertIsNotNone(go, "Go is required to execute the verifier's Go behavior regression suite")
        with tempfile.TemporaryDirectory(prefix="url-path-contract-") as directory:
            work = Path(directory)
            (work / "url").mkdir()
            (work / "go.mod").write_text("module github.com/servo/rust-url\n\ngo 1.23\n")
            (work / "url/url.go").write_text(FAKE_URL)
            (work / "contract.go").write_text(
                'package main\nimport ("encoding/json"; "errors"; "fmt"; "os"; "reflect"; "strings"; "github.com/servo/rust-url/url")\n'
                + 'var repoRoot string\n'
                + contract_functions(PROBE.read_text()) + "\n"
            )
            (work / "contract_test.go").write_text(GO_TEST)
            env = dict(os.environ, CGO_ENABLED="0", GOTOOLCHAIN="local", GOPROXY="off",
                       GOWORK="off", GOFLAGS="", GOMAXPROCS="2")
            result = subprocess.run([go, "test", "-p=1", "-count=1", "-json", "."],
                                    cwd=work, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            passed = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            cases = [event["Test"] for event in passed if event.get("Action") == "pass" and "Test" in event]
            self.assertTrue({
                "TestCompliantVariants", "TestClearedPathMutants", "TestGPT05Regression",
                "TestRootPathMutants", "TestJoinMutationDiagnostics", "TestParsedPathMutants",
                "TestCorpusUsesPathAccessors", "TestMinimalPathMutants",
            }.issubset(cases), cases)
            self.assertGreaterEqual(len(cases), 82, cases)


if __name__ == "__main__":
    unittest.main()
