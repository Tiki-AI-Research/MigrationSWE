// Command migration-probe is the consolidated target-side projection program
// used by the INTEGRATION harness (source/runner, target/runner).
//
// It evaluates every P2P, F2P and DIFFERENTIAL observation required by the
// migration contract against a Go module that satisfies
// module github.com/servo/rust-url, and prints a stable JSON projection:
//
//	{"<observation-id>": {"ok": <bool>, "value": <any>}}
//
// The program is merged by the INTEGRATION batch from the per-batch target
// probes. It never reads the reference target from the asset tree; the caller
// points it at a candidate module via a Go `replace` directive.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/servo/rust-url/formurlencoded"
	"github.com/servo/rust-url/idna"
	"github.com/servo/rust-url/idna/punycode"
	"github.com/servo/rust-url/percentencoding"
	"github.com/servo/rust-url/url"
)

type proj struct {
	OK    bool `json:"ok"`
	Value any  `json:"value"`
}

var out = map[string]proj{}

func set(id string, ok bool, value any) { out[id] = proj{OK: ok, Value: value} }

func setAll(ids []string, ok bool, value any) {
	for _, id := range ids {
		set(id, ok, value)
	}
}

func yes(v bool) string {
	if v {
		return "true"
	}
	return "false"
}

// HostKind constants are named discriminants; instruction.md does not fix
// their numeric values. Keep the projection stable across representations.
func projectedHostKind(h url.Host) int {
	kind := h.Kind()
	switch kind {
	case url.HostDomain:
		return 0
	case url.HostIPv4:
		return 1
	case url.HostIPv6:
		return 2
	default:
		return -1
	}
}

// Compile calls on a Host value without requiring Host to be a concrete
// struct. Interface representations preserve the same public capabilities.
func compileHostSurface(h url.Host) {
	var _ url.HostKind = h.Kind()
	var _ string = h.Domain()
	var _ net.IP = h.IP()
	var _ string = h.String()
}

var repoRoot = "."

// ---- shared surfaces (P2P and their DIFFERENTIAL twins) -------------------

func surfaceParseIssueList() (any, bool) {
	const in = "https://github.com/rust-lang/rust/issues?labels=E-easy&state=open"
	u, err := url.Parse(in)
	if err != nil {
		return fmt.Sprintf("parse-error: %v", err), false
	}
	pw, pwok := u.Password()
	hs, hsok := u.HostStr()
	h, hok := u.Host()
	_, pok := u.Port()
	segs, sok := u.PathSegments()
	q, qok := u.Query()
	f, fok := u.Fragment()
	v := map[string]any{
		"AsStr":         u.AsStr(),
		"Scheme":        u.Scheme(),
		"Username":      u.Username(),
		"Password":      map[string]any{"value": pw, "present": pwok},
		"HostStr":       map[string]any{"value": hs, "present": hsok},
		"HostKind":      projectedHostKind(h),
		"HostPresent":   hok,
		"Port":          map[string]any{"value": 0, "present": pok},
		"Path":          u.Path(),
		"PathSegments":  map[string]any{"value": segs, "ok": sok},
		"Query":         map[string]any{"value": q, "present": qok},
		"Fragment":      map[string]any{"value": f, "present": fok},
		"CannotBeABase": u.CannotBeABase(),
	}
	ok := u.AsStr() == in && u.Scheme() == "https" && u.Username() == "" &&
		!pwok && hs == "github.com" && hsok && h.Kind() == url.HostDomain && hok &&
		!pok && u.Path() == "/rust-lang/rust/issues" &&
		reflect.DeepEqual(segs, []string{"rust-lang", "rust", "issues"}) && sok &&
		q == "labels=E-easy&state=open" && qok && !fok && !u.CannotBeABase()
	return v, ok
}

func surfaceCannotBeABase() (any, bool) {
	u, err := url.Parse("data:text/plain,Hello?World#")
	if err != nil {
		return fmt.Sprintf("parse-error: %v", err), false
	}
	_, sok := u.PathSegments()
	q, qok := u.Query()
	f, fok := u.Fragment()
	v := map[string]any{
		"CannotBeABase": u.CannotBeABase(),
		"Scheme":        u.Scheme(),
		"Path":          u.Path(),
		"PathSegments":  map[string]any{"ok": sok},
		"HasHost":       u.HasHost(),
		"Query":         map[string]any{"value": q, "present": qok},
		"Fragment":      map[string]any{"value": f, "present": fok},
		"AsStr":         u.AsStr(),
	}
	ok := u.CannotBeABase() && u.Scheme() == "data" && u.Path() == "text/plain,Hello" &&
		!sok && !u.HasHost() && q == "World" && qok && f == "" && fok &&
		u.AsStr() == "data:text/plain,Hello?World#"
	return v, ok
}

func surfaceInvalidIPv6() (any, bool) {
	u, err := url.Parse("http://[:::1]")
	ok := u == nil && err != nil && errors.Is(err, url.ErrInvalidIPv6Address) &&
		!errors.Is(err, url.ErrInvalidPort)
	return map[string]any{
		"ParseError":                    err != nil,
		"NilURL":                        u == nil,
		"ErrorsIsErrInvalidIPv6Address": errors.Is(err, url.ErrInvalidIPv6Address),
		"ErrorsIsErrInvalidPort":        errors.Is(err, url.ErrInvalidPort),
	}, ok
}

func surfaceRelativeJoin() (any, bool) {
	_, err := url.Parse("../a")
	base, berr := url.Parse("http://servo.github.io/rust-url/url/index.html")
	joined, jerr := base.Join("../main.css")
	v := map[string]any{
		"ParseError":                        err != nil,
		"ErrorsIsErrRelativeURLWithoutBase": errors.Is(err, url.ErrRelativeURLWithoutBase),
		"BaseError":                         fmt.Sprint(berr),
		"JoinError":                         fmt.Sprint(jerr),
		"Join":                              map[string]any{"input": "../main.css", "AsStr": joined.AsStr()},
	}
	ok := errors.Is(err, url.ErrRelativeURLWithoutBase) && berr == nil && jerr == nil &&
		joined.AsStr() == "http://servo.github.io/rust-url/main.css"
	return v, ok
}

func surfaceParseHostIPv6() (any, bool) {
	h, err := url.ParseHost("[2001:0db8:0000:0000:0000:ff00:0042:8329]")
	_, err2 := url.ParseHost("[:::1]")
	v := map[string]any{
		"HostKind": projectedHostKind(h),
		"String":   h.String(),
		"Invalid": map[string]any{
			"input":                         "[:::1]",
			"ErrorsIsErrInvalidIPv6Address": errors.Is(err2, url.ErrInvalidIPv6Address),
		},
	}
	ok := err == nil && h.Kind() == url.HostIPv6 && h.String() == "[2001:db8::ff00:42:8329]" &&
		err2 != nil && errors.Is(err2, url.ErrInvalidIPv6Address)
	return v, ok
}

func surfacePorts() (any, bool) {
	u1, _ := url.Parse("http://example.com/")
	p1, k1 := u1.PortOrKnownDefault()
	u2, _ := url.Parse("https://x/")
	p2, k2 := u2.PortOrKnownDefault()
	u3, _ := url.Parse("ftp://x/")
	p3, k3 := u3.PortOrKnownDefault()
	u4, _ := url.Parse("https://x:8443/")
	p4, k4 := u4.Port()
	u5, _ := url.Parse("http://example.com/")
	pp := uint16(80)
	_ = u5.SetPort(&pp)
	p5, k5 := u5.Port()
	// The value has no meaning when the presence flag is false.
	if !k5 {
		p5 = 0
	}
	v := map[string]any{
		"http://example.com/": map[string]any{
			"PortOrKnownDefault": map[string]any{"value": p1, "present": k1},
			"Port":               map[string]any{"value": 0, "present": false},
		},
		"https://x/":      map[string]any{"PortOrKnownDefault": map[string]any{"value": p2, "present": k2}},
		"ftp://x/":        map[string]any{"PortOrKnownDefault": map[string]any{"value": p3, "present": k3}},
		"https://x:8443/": map[string]any{"Port": map[string]any{"value": p4, "present": k4}},
		"SetPortDefaultElision": map[string]any{
			"input": "http://example.com/", "setPort": 80,
			"Port": map[string]any{"value": p5, "present": k5}, "AsStr": u5.AsStr(),
		},
	}
	ok := p1 == 80 && k1 && p2 == 443 && k2 && p3 == 21 && k3 &&
		p4 == 8443 && k4 && !k5 && u5.AsStr() == "http://example.com/"
	return v, ok
}

func surfaceSetPath() (any, bool) {
	u, _ := url.Parse("http://a/")
	u.SetPath("api/some comments")
	p1 := u.Path()
	u2, _ := url.Parse("http://a/")
	u2.SetPath("api/some%20comments")
	p2 := u2.Path()
	v := map[string]any{
		"SetPath(\"api/some comments\")":   p1,
		"SetPath(\"api/some%20comments\")": p2,
	}
	return v, p1 == "/api/some%20comments" && p2 == "/api/some%20comments"
}

func surfacePathSegmentsMut() (any, bool) {
	u, _ := url.Parse("http://a/b/c?q#frag")
	m, err := u.PathSegmentsMut()
	if err != nil {
		return fmt.Sprintf("mut-error: %v", err), false
	}
	m.Pop().Push("2/100%.png")
	q, qok := u.Query()
	f, fok := u.Fragment()
	v := map[string]any{
		"Path":     u.Path(),
		"Query":    map[string]any{"value": q, "present": qok},
		"Fragment": map[string]any{"value": f, "present": fok},
	}
	ok := u.Path() == "/b/2%2F100%25.png" && q == "q" && qok && f == "frag" && fok
	return v, ok
}

func surfaceQueryPairsMut() (any, bool) {
	u, _ := url.Parse("http://a/?x=1#frag")
	u.QueryPairsMut().AppendPair("y", "2").Finish()
	s1 := u.AsStr()
	u2, _ := url.Parse("http://a/?x=1#frag")
	u2.QueryPairsMut().Clear().AppendPair("a", "b").Finish()
	s2 := u2.AsStr()
	v := map[string]any{
		"AppendPair(y,2)":         s1,
		"Clear().AppendPair(a,b)": s2,
	}
	return v, s1 == "http://a/?x=1&y=2#frag" && s2 == "http://a/?a=b#frag"
}

func surfaceFormParse() (any, bool) {
	var rows [][]any
	p := formurlencoded.ParseString("foo=bar&baz")
	for {
		n, val, ok := p.Next()
		rows = append(rows, []any{n, val, ok})
		if !ok {
			break
		}
	}
	var pct [][]any
	q := formurlencoded.ParseString("%23first=%25try%25")
	n, val, ok := q.Next()
	pct = append(pct, []any{n, val, ok})
	var plus [][]any
	r := formurlencoded.ParseString("a=1+2")
	n, val, ok = r.Next()
	plus = append(plus, []any{n, val, ok})
	v := map[string]any{
		"foo=bar&baz":        rows,
		"%23first=%25try%25": pct,
		"a=1+2":              plus,
	}
	okAll := len(rows) == 3 && rows[0][0] == "foo" && rows[0][1] == "bar" && rows[0][2] == true &&
		rows[1][0] == "baz" && rows[1][1] == "" && rows[1][2] == true && rows[2][2] == false &&
		pct[0][0] == "#first" && pct[0][1] == "%try%" && pct[0][2] == true &&
		plus[0][0] == "a" && plus[0][1] == "1 2" && plus[0][2] == true
	return v, okAll
}

func surfacePercentEncode() (any, bool) {
	na := percentencoding.NonAlphanumeric
	e1 := percentencoding.PercentEncode([]byte("foo bar?"), &na)
	e2 := percentencoding.PercentEncodeByte(0x3F)
	s := percentencoding.NonAlphanumeric
	t := s.Add('a')
	e3 := percentencoding.PercentEncode([]byte("a"), &s)
	e4 := percentencoding.PercentEncode([]byte("a"), &t)
	v := map[string]any{
		"PercentEncode(foo bar?, NonAlphanumeric)": e1,
		"PercentEncodeByte(0x3F)":                  e2,
		"AddValueSemantics":                        map[string]any{"base": e3, "withAdd": e4},
	}
	ok := e1 == "foo%20bar%3F" && e2 == "%3F" && e3 == "a" && e4 == "%61"
	return v, ok
}

func surfacePercentDecode() (any, bool) {
	d := percentencoding.DecodeString("%00%9F%92%96")
	_, err := d.DecodeUTF8()
	var ue *percentencoding.UTF8Error
	okErr := errors.As(err, &ue)
	vl := -1
	el := -1
	elo := false
	if ue != nil {
		vl = ue.ValidUpTo()
		el, elo = ue.ErrorLen()
	}
	lossy := percentencoding.DecodeString("%00%9F%92%96").DecodeUTF8Lossy()
	v := map[string]any{
		"DecodeUTF8IsUTF8Error": okErr,
		"ValidUpTo":             vl,
		"ErrorLen":              map[string]any{"value": el, "present": elo},
		"DecodeUTF8Lossy":       lossy,
	}
	ok := okErr && vl == 1 && el == 1 && elo && lossy == "\x00\uFFFD\uFFFD\uFFFD"
	return v, ok
}

func surfaceIDNA() (any, bool) {
	a1, e1 := idna.DomainToASCII("EXAMPLE.COM")
	a2, e2 := idna.DomainToASCII("faß.de")
	_, e3 := idna.DomainToASCIIStrict("foo_bar.example")
	loose, e4 := idna.DomainToASCII("foo_bar.example")
	u1, e5 := idna.DomainToUnicode("xn--fa-hia.de")
	v := map[string]any{
		"DomainToASCII(EXAMPLE.COM)":           map[string]any{"value": a1, "ok": e1 == nil},
		"DomainToASCII(faß.de)":                map[string]any{"value": a2, "ok": e2 == nil},
		"DomainToASCIIStrict(foo_bar.example)": map[string]any{"ErrorsIsErrInvalidDomain": errors.Is(e3, idna.ErrInvalidDomain)},
		"DomainToASCII(foo_bar.example)":       map[string]any{"value": loose, "ok": e4 == nil},
		"DomainToUnicode(xn--fa-hia.de)":       map[string]any{"value": u1, "ok": e5 == nil},
	}
	ok := a1 == "example.com" && e1 == nil && a2 == "xn--fa-hia.de" && e2 == nil &&
		errors.Is(e3, idna.ErrInvalidDomain) && loose == "foo_bar.example" && e4 == nil &&
		u1 == "faß.de" && e5 == nil
	return v, ok
}

func surfacePunycode() (any, bool) {
	e1, ok1 := punycode.EncodeString("bücher")
	d1, ok2 := punycode.DecodeToString("bcher-kva")
	r, ok3 := punycode.Decode("<<>>")
	v := map[string]any{
		"EncodeString(bücher)":      map[string]any{"value": e1, "ok": ok1},
		"DecodeToString(bcher-kva)": map[string]any{"value": d1, "ok": ok2},
		"Decode(<<>>)":              map[string]any{"value": r, "ok": ok3},
	}
	okp := e1 == "bcher-kva" && ok1 && d1 == "bücher" && ok2 && r == nil && !ok3
	return v, okp
}

func surfaceOrigin() (any, bool) {
	u1, _ := url.Parse("https://example.com:443/")
	o1 := u1.Origin()
	u2, _ := url.Parse("file:///etc")
	o2 := u2.Origin()
	u3, _ := url.Parse("blob:https://example.com/uuid")
	o3 := u3.Origin()
	v := map[string]any{
		"https://example.com:443/":      map[string]any{"IsTuple": o1.IsTuple(), "ASCIISerialization": o1.ASCIISerialization()},
		"file:///etc":                   map[string]any{"IsTuple": o2.IsTuple(), "ASCIISerialization": o2.ASCIISerialization()},
		"blob:https://example.com/uuid": map[string]any{"IsTuple": o3.IsTuple(), "ASCIISerialization": o3.ASCIISerialization()},
	}
	ok := o1.IsTuple() && o1.ASCIISerialization() == "https://example.com" &&
		!o2.IsTuple() && o2.ASCIISerialization() == "null" &&
		o3.IsTuple() && o3.ASCIISerialization() == "https://example.com"
	return v, ok
}

var publicPackages = []string{
	"github.com/servo/rust-url/url",
	"github.com/servo/rust-url/idna",
	"github.com/servo/rust-url/idna/punycode",
	"github.com/servo/rust-url/percentencoding",
	"github.com/servo/rust-url/formurlencoded",
}

func surfacePublicSurface() (any, bool) {
	// Reaching this point proves every required package compiled and every
	// referenced symbol resolved, which is the compile-level public surface.
	v := map[string]any{"compile_unchanged": true, "packages": publicPackages}
	return v, true
}

func assignP2P() {
	type surface struct {
		ids []string
		fn  func() (any, bool)
	}
	groups := []surface{
		{[]string{"P2P-001", "DIFF-015"}, surfaceOrigin},
		{[]string{"P2P-002", "DIFF-010"}, surfaceFormParse},
		{[]string{"P2P-003", "DIFF-013"}, surfaceIDNA},
		{[]string{"P2P-004", "DIFF-012"}, surfacePercentDecode},
		{[]string{"P2P-005", "DIFF-008"}, surfacePathSegmentsMut},
		{[]string{"P2P-006", "DIFF-005"}, surfaceParseHostIPv6},
		{[]string{"P2P-007", "DIFF-009"}, surfaceQueryPairsMut},
		{[]string{"P2P-008", "DIFF-011"}, surfacePercentEncode},
		{[]string{"P2P-009", "DIFF-016"}, surfacePublicSurface},
		{[]string{"P2P-010", "DIFF-003"}, surfaceInvalidIPv6},
		{[]string{"P2P-011", "DIFF-001"}, surfaceParseIssueList},
		{[]string{"P2P-012", "DIFF-002"}, surfaceCannotBeABase},
		{[]string{"P2P-013", "DIFF-007"}, surfaceSetPath},
		{[]string{"P2P-014", "DIFF-014"}, surfacePunycode},
		{[]string{"P2P-015", "DIFF-004"}, surfaceRelativeJoin},
		{[]string{"P2P-016", "DIFF-006"}, surfacePorts},
	}
	for _, g := range groups {
		val, ok := g.fn()
		setAll(g.ids, ok, val)
	}
}

// ---- repository-condition checks -----------------------------------------

func gitOut(args ...string) (string, error) {
	c := exec.Command("git", args...)
	c.Dir = repoRoot
	b, err := c.CombinedOutput()
	return string(b), err
}

var rustSrcPrefixes = []string{"url/src/", "idna/src/", "percent_encoding/src/", "form_urlencoded/src/", "Cargo.toml"}

var protectedInputs = []string{
	"url/tests/urltestdata.json",
	"url/tests/setters_tests.json",
	"idna/tests/IdnaTestV2.txt",
	"idna/tests/punycode_tests.json",
	"idna/tests/bad_punycode_tests.json",
}

func changedFiles() ([]string, error) {
	out, err := gitOut("status", "--porcelain", "--untracked-files=all")
	if err != nil {
		return nil, err
	}
	var files []string
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimRight(line, "\r")
		if len(line) < 4 {
			continue
		}
		files = append(files, strings.TrimSpace(line[3:]))
	}
	// Tracked modifications relative to HEAD.
	out2, err := gitOut("diff", "--name-only", "HEAD")
	if err == nil {
		for _, line := range strings.Split(out2, "\n") {
			line = strings.TrimSpace(line)
			if line != "" {
				files = append(files, line)
			}
		}
	}
	return files, nil
}

func repoChangeSet() (any, bool) {
	files, err := changedFiles()
	if err != nil {
		return fmt.Sprintf("git-error: %v", err), false
	}
	bad := []string{}
	for _, f := range files {
		// Test fixtures may accompany new Go tests. Production changes are
		// restricted to Go source and the module manifest by instruction.md.
		fixture := strings.Contains(f, "/testdata/") &&
			(strings.HasPrefix(f, "url/") || strings.HasPrefix(f, "idna/") ||
				strings.HasPrefix(f, "percentencoding/") || strings.HasPrefix(f, "formurlencoded/"))
		if f != "go.mod" && !strings.HasSuffix(f, ".go") && !fixture &&
			f != ".gocache" && f != ".gopath" &&
			!strings.HasPrefix(f, ".gocache/") && !strings.HasPrefix(f, ".gopath/") {
			bad = append(bad, f)
		}
		for _, p := range rustSrcPrefixes {
			if strings.HasPrefix(f, p) {
				bad = append(bad, f)
			}
		}
	}
	// These source assets are explicitly out of scope even if their names
	// happen to end in .go or resemble a new fixture directory.
	for _, f := range files {
		for _, prefix := range []string{"data-url/", "url_debug_tests/", ".github/", "fuzz/", "fuzz_targets/", "url/benches/", "url/benchmarks/"} {
			if strings.HasPrefix(f, prefix) { bad = append(bad, f) }
		}
		if strings.HasSuffix(f, ".natvis") { bad = append(bad, f) }
	}
	ok := len(bad) == 0
	return map[string]any{"changed": files, "rustSrcModified": bad}, ok
}

func repoProtected() (any, bool) {
	for _, p := range protectedInputs {
		if _, err := os.Stat(p); err != nil {
			// Paths are relative to repoRoot.
			if _, err2 := os.Stat(repoRoot + "/" + p); err2 != nil {
				return fmt.Sprintf("missing protected input: %s", p), false
			}
		}
	}
	files, err := changedFiles()
	if err != nil {
		return fmt.Sprintf("git-error: %v", err), false
	}
	bad := []string{}
	for _, f := range files {
		for _, p := range protectedInputs {
			if f == p {
				bad = append(bad, f)
			}
		}
	}
	return map[string]any{"modified": bad}, len(bad) == 0
}

var (
	goSkipCall      = regexp.MustCompile(`\.\s*Skip(?:f|Now)?\s*\(`)
	goSkipMethod    = regexp.MustCompile(`\)\s*\.\s*Skip(?:f|Now)?\b`)
	goSkipReflect   = regexp.MustCompile(`\.\s*MethodByName\s*\(\s*['"]Skip(?:f|Now)?['"]\s*\)`)
	goSkipValue     = regexp.MustCompile(`(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)+Skip(?:f|Now)?\b`)
	goSkipValueBind = regexp.MustCompile(`(?:^|[;{}\n])[ \t]*(?:var[ \t]+)?\b([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|=)\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)+Skip(?:f|Now)?\b`)
	goSkipBoolBef   = regexp.MustCompile(`(?:^|[^\w])(?:if|for|switch|case|!|&&|\|\||==|!=)\s*$`)
	goSkipBoolAft   = regexp.MustCompile(`\A\s*(?:\{|&&|\|\||==|!=)`)
)

// hasGoSkip reports a testing.T skip across every equivalent spelling: a
// selector call (t.Skip(...)), a method expression ((*testing.T).Skip), a
// method *value* stored anywhere (variable, struct field, map/slice element,
// channel, argument, return, interface) and invoked directly or indirectly, or
// reflection (MethodByName("Skip")).  RE2 lacks lookahead, so a matched method
// value is checked for a trailing call byte.  A value used only as a boolean
// condition, and a direct binding whose target is never invoked, are treated as
// ordinary field reads.
func hasGoSkip(source string) bool {
	if goSkipCall.MatchString(source) ||
		goSkipMethod.MatchString(source) ||
		goSkipReflect.MatchString(source) {
		return true
	}
	var bound [][2]int
	for _, match := range goSkipValueBind.FindAllStringSubmatchIndex(source, -1) {
		if match[2] < 0 {
			continue
		}
		if skipTrailingCall(source, match[1]) {
			continue
		}
		bound = append(bound, [2]int{match[0], match[1]})
		name := regexp.QuoteMeta(source[match[2]:match[3]])
		if regexp.MustCompile(`\b` + name + `\s*\(`).MatchString(source) {
			return true
		}
	}
	for _, match := range goSkipValue.FindAllStringIndex(source, -1) {
		if skipTrailingCall(source, match[1]) {
			continue
		}
		if skipCovered(match, bound) {
			continue
		}
		if goSkipBoolBef.MatchString(source[:match[0]]) {
			continue
		}
		if goSkipBoolAft.MatchString(source[match[1]:]) {
			continue
		}
		return true
	}
	return false
}

func skipTrailingCall(source string, end int) bool {
	for end < len(source) {
		switch source[end] {
		case ' ', '\t', '\n', '\r':
			end++
			continue
		}
		break
	}
	return end < len(source) && source[end] == '('
}

func skipCovered(match []int, bound [][2]int) bool {
	for _, span := range bound {
		if span[0] <= match[0] && match[1] <= span[1] {
			return true
		}
	}
	return false
}

func repoNoSkips() (any, bool) {
	scanner := os.Getenv("MIGRATION_GO_SKIP_SCANNER")
	if scanner == "" {
		return "Go skip scanner is missing", false
	}
	data, err := exec.Command("python3", scanner, repoRoot).Output()
	if err != nil {
		return fmt.Sprintf("Go skip scan failed: %v", err), false
	}
	var sources map[string]string
	if err := json.Unmarshal(data, &sources); err != nil {
		return fmt.Sprintf("invalid Go skip scan: %v", err), false
	}
	var bad []string
	for path, source := range sources {
		if hasGoSkip(source) {
			bad = append(bad, path)
		}
	}
	sort.Strings(bad)
	return map[string]any{"tSkipFiles": bad}, len(bad) == 0
}

func repoBenchFuzz() (any, bool) {
	files, err := changedFiles()
	if err != nil {
		return fmt.Sprintf("git-error: %v", err), false
	}
	var bad []string
	for _, f := range files {
		if strings.Contains(f, "fuzz_targets/") || strings.HasPrefix(f, "url/benches/") {
			bad = append(bad, f)
		}
	}
	return map[string]any{"modified": bad}, len(bad) == 0
}

func stdlibOnly() (any, bool) {
	cmd := exec.Command("go", "list", "-deps", "./url")
	cmd.Dir = repoRoot
	cmd.Env = append(os.Environ(), "GOPROXY=off", "GOTOOLCHAIN=local")
	b, err := cmd.CombinedOutput()
	if err != nil {
		return string(b), false
	}
	var thirdParty []string
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, "github.com/servo/rust-url") {
			continue
		}
		if strings.Contains(line, ".") {
			first := strings.SplitN(line, "/", 2)[0]
			if strings.Contains(first, ".") {
				thirdParty = append(thirdParty, line)
			}
		}
	}
	return map[string]any{"packages": strings.Split(strings.TrimSpace(string(b)), "\n"), "thirdParty": thirdParty}, len(thirdParty) == 0
}

func filepathWalk(root string, fn func(string)) error {
	return walk(root, fn)
}

func walk(root string, fn func(string)) error {
	entries, err := os.ReadDir(root)
	if err != nil {
		return err
	}
	for _, e := range entries {
		p := root + "/" + e.Name()
		if e.IsDir() {
			if e.Name() == ".git" || e.Name() == "vendor" {
				continue
			}
			if err := walk(p, fn); err != nil {
				return err
			}
			continue
		}
		fn(p)
	}
	return nil
}

var _ = net.IP{}

// ---- main ----------------------------------------------------------------

func main() {
	if r := os.Getenv("MIGRATION_REPO_ROOT"); r != "" {
		repoRoot = r
	} else if r := os.Getenv("MIGRATION_REPO"); r != "" {
		repoRoot = r
	} else if r := os.Getenv("REPO_ROOT"); r != "" {
		repoRoot = r
	} else if r := os.Getenv("REPO"); r != "" {
		repoRoot = r
	} else if r := os.Getenv("REPO_ROOT"); r != "" {
		repoRoot = r
	}

	assignP2P()

	// --- F2P-0001 group ---
	// QueryPairsMut fragment preservation / Clear semantics.
	func() {
		u, err := url.Parse("http://a/?x=1#frag")
		if err != nil {
			setAll([]string{"F2P-001", "F2P-005"}, false, err.Error())
			return
		}
		got := u.QueryPairsMut().Clear().AppendPair("a", "b").Finish().AsStr()
		setAll([]string{"F2P-001", "F2P-005"}, got == "http://a/?a=b#frag", got)
	}()
	// invalid IPv6 + idna, no Rust delegation.
	func() {
		_, err := url.Parse("http://[:::1]")
		ok := err != nil && errors.Is(err, url.ErrInvalidIPv6Address)
		d, derr := idna.DomainToASCII("example.com")
		ok = ok && d == "example.com" && derr == nil
		setAll([]string{"F2P-002", "F2P-008", "F2P-009"}, ok, fmt.Sprintf("parse=%v idna=%q/%v", err, d, derr))
	}()
	// canonical parse under pure-Go build.
	func() {
		const in = "https://github.com/rust-lang/rust/issues?labels=E-easy&state=open"
		u, err := url.Parse(in)
		ok := err == nil && u.AsStr() == in && u.Scheme() == "https"
		set("F2P-003", ok, fmt.Sprintf("%v", err))
	}()
	// pure Go dependencies (dependency-declaration).
	func() {
		u, err := url.Parse("https://example.com/")
		set("F2P-004", err == nil && u.AsStr() == "https://example.com/", fmt.Sprintf("%v", err))
	}()
	// punycode RFC 3492.
	func() {
		e, eok := punycode.EncodeString("bücher")
		d, dok := punycode.DecodeToString("bcher-kva")
		_, badok := punycode.Decode("<<>>")
		set("F2P-006", e == "bcher-kva" && eok && d == "bücher" && dok && !badok, e+"/"+d)
	}()
	// strict vs non-strict idna.
	func() {
		a, aerr := idna.DomainToASCII("foo_bar.example")
		_, serr := idna.DomainToASCIIStrict("foo_bar.example")
		ok := a == "foo_bar.example" && aerr == nil && serr != nil && errors.Is(serr, idna.ErrInvalidDomain)
		setAll([]string{"F2P-007", "F2P-018"}, ok, fmt.Sprintf("loose=%q strict=%v", a, serr))
	}()
	// PathSegmentsMut.
	func() {
		u, err := url.Parse("http://a/b/c?q#frag")
		if err != nil {
			setAll([]string{"F2P-010", "F2P-016"}, false, err.Error())
			return
		}
		m, e := u.PathSegmentsMut()
		if e != nil {
			setAll([]string{"F2P-010", "F2P-016"}, false, e.Error())
			return
		}
		m.Pop().Push("2/100%.png")
		q, qok := u.Query()
		f, fok := u.Fragment()
		ok := u.Path() == "/b/2%2F100%25.png" && q == "q" && qok && f == "frag" && fok
		setAll([]string{"F2P-010", "F2P-016"}, ok, u.Path())
	}()
	// IPv6 RFC 5952.
	func() {
		h, err := url.ParseHost("[2001:0db8:0000:0000:0000:ff00:0042:8329]")
		s := ""
		if err == nil {
			s = h.String()
		}
		_, err2 := url.ParseHost("[:::1]")
		ok := err == nil && h.Kind() == url.HostIPv6 && s == "[2001:db8::ff00:42:8329]" &&
			err2 != nil && errors.Is(err2, url.ErrInvalidIPv6Address)
		set("F2P-011", ok, s)
	}()
	// repository change set.
	func() {
		v, ok := repoChangeSet()
		setAll([]string{"F2P-012", "F2P-055"}, ok, v)
	}()
	// wpt.rs parse accessors.
	func() {
		v, ok := surfaceParseIssueList()
		set("F2P-013", ok, v)
	}()
	// fragment comma-ok presence.
	func() {
		u1, _ := url.Parse("data:text/plain,Hello?World#")
		u2, _ := url.Parse("data:text/plain,Hello")
		f1, ok1 := u1.Fragment()
		f2, ok2 := u2.Fragment()
		ok := f1 == "" && ok1 && f2 == "" && !ok2
		set("F2P-014", ok, fmt.Sprintf("present=%q/%v absent=%q/%v", f1, ok1, f2, ok2))
	}()
	// PercentEncode uppercase + value semantics Add.
	func() {
		v, ok := surfacePercentEncode()
		set("F2P-015", ok, v)
	}()
	// full accessor contract.
	func() {
		v, ok := surfaceParseIssueList()
		setAll([]string{"F2P-017", "F2P-020"}, ok, v)
	}()
	// UTF8 error reporting / lossy.
	func() {
		v, ok := surfacePercentDecode()
		setAll([]string{"F2P-019", "F2P-023", "F2P-024"}, ok, v)
	}()
	// sentinel error identity.
	func() {
		_, err := url.Parse("http://[:::1]")
		ok := err != nil && errors.Is(err, url.ErrInvalidIPv6Address) && !errors.Is(err, url.ErrInvalidPort)
		setAll([]string{"F2P-021", "F2P-022"}, ok, fmt.Sprint(err))
	}()

	// --- F2P-0002 group ---
	func() {
		u, err := url.Parse("https://example.com/")
		ok := err == nil && u.AsStr() == "https://example.com/"
		setAll([]string{"F2P-025", "F2P-029"}, ok, fmt.Sprintf("%v", err))
	}()
	func() {
		v, ok := surfaceOrigin()
		setAll([]string{"F2P-026", "F2P-028"}, ok, v)
	}()
	func() {
		v, ok := surfaceParseHostIPv6()
		set("F2P-027", ok, v)
	}()
	func() {
		u1, _ := url.Parse("data:text/plain,Hello?World#")
		u2, _ := url.Parse("data:text/plain,Hello")
		f1, ok1 := u1.Fragment()
		f2, ok2 := u2.Fragment()
		ok := f1 == "" && ok1 && f2 == "" && !ok2
		set("F2P-030", ok, fmt.Sprintf("%q/%v %q/%v", f1, ok1, f2, ok2))
	}()
	func() {
		if got := percentencoding.PercentEncodeByte(0x3F); got != "%3F" {
			setAll([]string{"F2P-031", "F2P-046"}, false, got)
			return
		}
		s := percentencoding.NonAlphanumeric
		tt := s.Add('a')
		sa := percentencoding.PercentEncode([]byte("a"), &s)
		ta := percentencoding.PercentEncode([]byte("a"), &tt)
		pe := percentencoding.PercentEncode([]byte("foo bar?"), &percentencoding.NonAlphanumeric)
		ok := sa == "a" && ta == "%61" && pe == "foo%20bar%3F"
		setAll([]string{"F2P-031", "F2P-046"}, ok, fmt.Sprintf("%s %s %s", sa, ta, pe))
	}()
	func() {
		v, ok := surfacePorts()
		setAll([]string{"F2P-032", "F2P-048"}, ok, v)
	}()
	func() {
		v, ok := surfaceFormParse()
		set("F2P-033", ok, v)
	}()
	func() {
		p := formurlencoded.ParseString("a=%00%9F%92%96")
		n, val, ok := p.Next()
		want := "\x00\uFFFD\uFFFD\uFFFD"
		set("F2P-034", n == "a" && val == want && ok, fmt.Sprintf("%q", val))
	}()
	func() {
		v, ok := surfaceInvalidIPv6()
		setAll([]string{"F2P-035", "F2P-038"}, ok, v)
	}()
	func() {
		v, ok := surfaceIDNA()
		set("F2P-036", ok, v)
	}()
	func() {
		v, ok := repoProtected()
		set("F2P-037", ok, v)
	}()
	func() {
		v, ok := surfacePercentDecode()
		set("F2P-039", ok, v)
	}()
	func() {
		v, ok := surfaceCannotBeABase()
		setAll([]string{"F2P-040", "F2P-044"}, ok, v)
	}()
	func() {
		v, ok := repoChangeSet()
		set("F2P-041", ok, v)
	}()
	func() {
		v, ok := surfacePunycode()
		set("F2P-042", ok, v)
	}()
	func() {
		v, ok := repoBenchFuzz()
		set("F2P-043", ok, v)
	}()
	func() {
		_, err := url.Parse("http://[:::1]")
		ok := err != nil && errors.Is(err, url.ErrInvalidIPv6Address) && !errors.Is(err, url.ErrInvalidPort)
		set("F2P-045", ok, fmt.Sprint(err))
	}()
	func() {
		v, ok := repoNoSkips()
		set("F2P-047", ok, v)
	}()

	// --- F2P-0003 group ---
	func() {
		v, ok := stdlibOnly()
		set("F2P-049", ok, v)
	}()
	func() {
		v, ok := surfacePathSegmentsMut()
		set("F2P-050", ok, v)
	}()
	func() {
		a, aerr := idna.DomainToASCII("foo_bar.example")
		_, serr := idna.DomainToASCIIStrict("foo_bar.example")
		ok := a == "foo_bar.example" && aerr == nil && errors.Is(serr, idna.ErrInvalidDomain)
		setAll([]string{"F2P-051", "F2P-065"}, ok, fmt.Sprintf("%q/%v", a, serr))
	}()
	func() {
		v, ok := surfacePercentDecode()
		setAll([]string{"F2P-052", "F2P-057"}, ok, v)
	}()
	func() {
		v, ok := repoChangeSet()
		u, err := url.Parse("https://github.com/rust-lang/rust/issues?labels=E-easy&state=open")
		ok = ok && err == nil && u.Path() == "/rust-lang/rust/issues"
		set("F2P-053", ok, v)
	}()
	func() {
		d, err := idna.DomainToASCII("faß.de")
		set("F2P-054", err == nil && d == "xn--fa-hia.de", fmt.Sprintf("%q/%v", d, err))
	}()
	func() {
		v, ok := repoProtected()
		set("F2P-056", ok, v)
	}()
	func() {
		v, ok := surfacePunycode()
		set("F2P-058", ok, v)
	}()
	func() {
		v, ok := surfacePercentEncode()
		setAll([]string{"F2P-059", "F2P-061", "F2P-062"}, ok, v)
	}()
	func() {
		u, err := url.Parse("http://[:::1]")
		ok := u == nil && err != nil && errors.Is(err, url.ErrInvalidIPv6Address) && !errors.Is(err, url.ErrInvalidPort)
		setAll([]string{"F2P-060", "F2P-063", "F2P-070"}, ok, fmt.Sprint(err))
	}()
	func() {
		u, err := url.Parse("http://a/?x=1#frag")
		if err != nil {
			setAll([]string{"F2P-064", "F2P-071"}, false, err.Error())
			return
		}
		got := u.QueryPairsMut().Clear().AppendPair("a", "b").Finish().AsStr()
		p := formurlencoded.ParseString("foo=bar&baz")
		n1, v1, o1 := p.Next()
		n2, v2, o2 := p.Next()
		_, _, o3 := p.Next()
		ok := got == "http://a/?a=b#frag" && n1 == "foo" && v1 == "bar" && o1 &&
			n2 == "baz" && v2 == "" && o2 && !o3
		setAll([]string{"F2P-064", "F2P-071"}, ok, got)
	}()
	func() {
		v, ok := surfacePorts()
		set("F2P-066", ok, v)
	}()
	func() {
		_, err := url.Parse("http://[:::1]")
		ok := err != nil && errors.Is(err, url.ErrInvalidIPv6Address)
		d, derr := idna.DomainToASCII("example.com")
		ok = ok && d == "example.com" && derr == nil
		setAll([]string{"F2P-067", "F2P-068"}, ok, fmt.Sprintf("parse=%v idna=%q", err, d))
	}()
	func() {
		a, _ := url.Parse("data:text/plain,Hello?World#")
		b, _ := url.Parse("data:text/plain,Hello")
		f1, ok1 := a.Fragment()
		f2, ok2 := b.Fragment()
		set("F2P-069", f1 == "" && ok1 && f2 == "" && !ok2, fmt.Sprintf("%q/%v %q/%v", f1, ok1, f2, ok2))
	}()
	func() {
		v, ok := surfaceParseHostIPv6()
		set("F2P-072", ok, v)
	}()

	// These postconditions supplement the example projections. Keep their
	// diagnostics in the scored observations so a passing example cannot hide
	// an invalid public signature or an untested error path.
	enforceContract([]string{"F2P-006", "F2P-042", "F2P-058"}, checkPunycodeContract)
	enforceContract([]string{"F2P-007", "F2P-018", "F2P-051", "F2P-065"}, checkIDNAContract)
	enforceContract([]string{"F2P-035"}, checkURLMutationContract)
	enforceContract([]string{"F2P-003"}, checkURLConformance)
	enforceContract([]string{"F2P-019", "F2P-031"}, checkEncodingContract)

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// Compile the complete surface from instruction.md. In particular, ordinary
// call expressions discard return values and cannot detect a Serializer method
// that incorrectly returns *Serializer instead of having no result.
var (
	_ interface {
		Join(string) (*url.URL, error)
		AsStr() string
		Scheme() string
		Username() string
		Password() (string, bool)
		HasHost() bool
		HostStr() (string, bool)
		Host() (url.Host, bool)
		Domain() (string, bool)
		Port() (uint16, bool)
		PortOrKnownDefault() (uint16, bool)
		Path() string
		PathSegments() ([]string, bool)
		Query() (string, bool)
		QueryPairs() *formurlencoded.Parser
		Fragment() (string, bool)
		CannotBeABase() bool
		IsSpecial() bool
		HasAuthority() bool
		Authority() string
		Origin() url.Origin
		SetFragment(*string)
		SetQuery(*string)
		SetPath(string)
		SetPort(*uint16) error
		SetHost(*string) error
		SetUsername(string) error
		SetPassword(*string) error
		SetScheme(string) error
		QueryPairsMut() *url.QueryMutator
		PathSegmentsMut() (*url.PathSegmentsMut, error)
	} = (*url.URL)(nil)
	_ interface {
		AppendPair(string, string) *url.QueryMutator
		AppendKeyOnly(string) *url.QueryMutator
		ExtendPairs([][2]string) *url.QueryMutator
		Clear() *url.QueryMutator
		Finish() *url.URL
	} = (*url.QueryMutator)(nil)
	_ interface {
		Clear() *url.PathSegmentsMut
		PopIfEmpty() *url.PathSegmentsMut
		Pop() *url.PathSegmentsMut
		Push(string) *url.PathSegmentsMut
		Extend([]string) *url.PathSegmentsMut
	} = (*url.PathSegmentsMut)(nil)
	_ interface {
		IsTuple() bool
		ASCIISerialization() string
		UnicodeSerialization() string
	} = (*url.Origin)(nil)
	_ interface {
		Next() (string, string, bool)
	} = (*formurlencoded.Parser)(nil)
	_ interface {
		AppendPair(string, string)
		AppendKeyOnly(string)
		ExtendPairs([][2]string)
		ExtendKeysOnly([]string)
		Clear()
		Finish() string
	} = (*formurlencoded.Serializer)(nil)
	_ interface {
		Add(byte) percentencoding.ASCIIStringSet
		Remove(byte) percentencoding.ASCIIStringSet
		Union(percentencoding.ASCIIStringSet) percentencoding.ASCIIStringSet
		Complement() percentencoding.ASCIIStringSet
	} = (*percentencoding.ASCIIStringSet)(nil)
	_ interface {
		Bytes() []byte
		DecodeUTF8() (string, error)
		DecodeUTF8Lossy() string
	} = (*percentencoding.PercentDecode)(nil)
	_ interface {
		Error() string
		ValidUpTo() int
		ErrorLen() (int, bool)
	} = (*percentencoding.UTF8Error)(nil)
	_ func(string) (*url.URL, error) = url.Parse
	_ func(string) (url.Host, error) = url.ParseHost
	_ func(string) (url.Host, error) = url.ParseOpaqueHost
	_ func() *url.Origin = url.NewOpaqueOrigin
	_ func(string) (string, error) = idna.DomainToASCII
	_ func(string) (string, error) = idna.DomainToASCIIStrict
	_ func(string) (string, error) = idna.DomainToUnicode
	_ func([]rune) (string, bool) = punycode.Encode
	_ func(string) (string, bool) = punycode.EncodeString
	_ func(string) ([]rune, bool) = punycode.Decode
	_ func(string) (string, bool) = punycode.DecodeToString
	_ func(byte) string = percentencoding.PercentEncodeByte
	_ func([]byte, *percentencoding.ASCIIStringSet) string = percentencoding.PercentEncode
	_ func(string, *percentencoding.ASCIIStringSet) string = percentencoding.UTF8PercentEncode
	_ func([]byte) *percentencoding.PercentDecode = percentencoding.Decode
	_ func(string) *percentencoding.PercentDecode = percentencoding.DecodeString
	_ func([]byte) *formurlencoded.Parser = formurlencoded.Parse
	_ func(string) *formurlencoded.Parser = formurlencoded.ParseString
	_ func([]byte) string = formurlencoded.ByteSerialize
	_ func(*string) *formurlencoded.Serializer = formurlencoded.NewSerializer
	_ error = url.ErrEmptyHost
	_ error = url.ErrIdna
	_ error = url.ErrInvalidPort
	_ error = url.ErrInvalidIPv4Address
	_ error = url.ErrInvalidIPv6Address
	_ error = url.ErrInvalidDomainCharacter
	_ error = url.ErrRelativeURLWithoutBase
	_ error = url.ErrRelativeURLWithCannotBeABaseBase
	_ error = url.ErrSetHostOnCannotBeABaseURL
	_ error = url.ErrOverflow
	_ error = idna.ErrInvalidDomain
)

func enforceContract(ids []string, check func() []string) {
	failures := func() (failures []string) {
		defer func() {
			if value := recover(); value != nil {
				failures = append(failures, fmt.Sprintf("contract check panicked: %v", value))
			}
		}()
		return check()
	}()
	if len(failures) == 0 {
		return
	}
	for _, id := range ids {
		previous := out[id]
		set(id, false, map[string]any{"example": previous.Value, "contract_errors": failures})
	}
}

func checkPunycodeContract() []string {
	var failures []string
	// A delimiter at byte zero does not introduce a basic-code-point prefix.
	// It remains part of the encoded integer stream and is invalid there.
	// Exercise both decoding APIs: the public valid-vector corpus alone does
	// not detect implementations that silently discard this delimiter.
	for _, input := range []string{"-", "-a", "-abc", "<<>>", "abc!", "\xff", "\u00e9-"} {
		if decoded, ok := punycode.Decode(input); ok {
			failures = append(failures, fmt.Sprintf("Decode accepted malformed Punycode %q as %q", input, string(decoded)))
		}
		if decoded, ok := punycode.DecodeToString(input); ok {
			failures = append(failures, fmt.Sprintf("DecodeToString accepted malformed Punycode %q as %q", input, decoded))
		}
	}
	if encoded, ok := punycode.EncodeString(string([]byte{0xff})); ok {
		failures = append(failures, fmt.Sprintf("EncodeString accepted invalid UTF-8 as %q", encoded))
	}
	// Preserve valid empty/basic-only inputs and scalar-range boundaries.
	for _, input := range []string{"", "ASCII-._", "b\u00fccher", "\U0010ffff"} {
		encoded, ok := punycode.EncodeString(input)
		decoded, decodeOK := punycode.DecodeToString(encoded)
		runes, runesOK := punycode.Decode(encoded)
		if !ok || !decodeOK || !runesOK || decoded != input || string(runes) != input {
			failures = append(failures, fmt.Sprintf("Punycode boundary round trip failed for %q", input))
		}
	}
	for _, value := range []rune{-1, 0xd800, 0xdfff, utf8.MaxRune + 1} {
		if encoded, ok := punycode.Encode([]rune{'a', value}); ok {
			failures = append(failures, fmt.Sprintf("Encode accepted invalid scalar %U as %q", value, encoded))
		}
	}
	// Read the protected public corpus, rather than duplicating its answers.
	data, err := os.ReadFile(repoRoot + "/idna/tests/punycode_tests.json")
	var cases []struct { Decoded, Encoded string }
	if err != nil || json.Unmarshal(data, &cases) != nil || len(cases) == 0 {
		return append(failures, "cannot read nonempty public Punycode corpus")
	}
	for i, tc := range cases {
		encoded, ok := punycode.EncodeString(tc.Decoded)
		encodedRunes, okRunes := punycode.Encode([]rune(tc.Decoded))
		decoded, okDecode := punycode.DecodeToString(tc.Encoded)
		decodedRunes, okDecodeRunes := punycode.Decode(tc.Encoded)
		if !ok || !okRunes || encoded != tc.Encoded || encodedRunes != tc.Encoded ||
			!okDecode || !okDecodeRunes || decoded != tc.Decoded || string(decodedRunes) != tc.Decoded {
			failures = append(failures, fmt.Sprintf("public Punycode vector %d failed", i))
		}
	}
	data, err = os.ReadFile(repoRoot + "/idna/tests/bad_punycode_tests.json")
	if err != nil || json.Unmarshal(data, &cases) != nil || len(cases) == 0 {
		return append(failures, "cannot read nonempty Punycode overflow corpus")
	}
	for i, tc := range cases {
		if _, ok := punycode.EncodeString(tc.Decoded); ok {
			failures = append(failures, fmt.Sprintf("overflow vector %d was accepted", i))
		}
	}
	return failures
}

func checkIDNAContract() []string {
	var failures []string
	// The Bidi rule applies to ASCII labels too when another label is RTL.
	// Returning an error without marking the offending ASCII label loses the
	// required error rendering, even if an ASCII fast path kept that label.
	for _, input := range []string{
		"a\u0080b.example", "\u0308abc.example", "ab\u200dcd.example", "xn--", "xn--0.pt",
		"0.\u05d0", "\u05d0.0", "a-.\u05d0", "0.xn--4db",
	} {
		output, err := idna.DomainToUnicode(input)
		if !errors.Is(err, idna.ErrInvalidDomain) || !strings.ContainsRune(output, utf8.RuneError) {
			failures = append(failures, fmt.Sprintf("DomainToUnicode(%q) lacks error rendering: %q, %v", input, output, err))
		}
		if _, err := idna.DomainToASCII(input); !errors.Is(err, idna.ErrInvalidDomain) {
			failures = append(failures, fmt.Sprintf("DomainToASCII accepted invalid domain %q", input))
		}
	}
	for _, tc := range []struct { input, ascii, unicode string }{
		{"ＦＯＯ。BÜCHER", "foo.xn--bcher-kva", "foo.bücher"},
		{"u\u0308.example", "xn--tda.example", "ü.example"},
		{"A0.\u05d0", "a0.xn--4db", "a0.\u05d0"},
		{"A0.xn--4db", "a0.xn--4db", "a0.\u05d0"},
	} {
		ascii, errASCII := idna.DomainToASCII(tc.input)
		unicode, errUnicode := idna.DomainToUnicode(tc.input)
		if errASCII != nil || ascii != tc.ascii || errUnicode != nil || unicode != tc.unicode {
			failures = append(failures, fmt.Sprintf("IDNA normalization failed for %q", tc.input))
		}
	}
	for _, input := range []string{"-label.example", "label-.example", "ab--c.example", strings.Repeat("a", 64) + ".example"} {
		if _, err := idna.DomainToASCIIStrict(input); !errors.Is(err, idna.ErrInvalidDomain) {
			failures = append(failures, fmt.Sprintf("strict IDNA accepted %q", input))
		}
	}
	// UTS #46 counts code points, not UTF-8 bytes, when checking the third
	// and fourth positions. Check both Unicode input and its A-label form;
	// permissive processing must still allow these hyphens.
	for _, tc := range []struct{ unicode, ascii string }{
		{"éa--b.example", "xn--a--b-9oa.example"},
		{"aé--b.example", "xn--a--b-bpa.example"},
	} {
		for _, input := range []string{tc.unicode, tc.ascii} {
			if _, err := idna.DomainToASCIIStrict(input); !errors.Is(err, idna.ErrInvalidDomain) {
				failures = append(failures, fmt.Sprintf("strict IDNA accepted third/fourth-position hyphens in %q", input))
			}
			if got, err := idna.DomainToASCII(input); err != nil || got != tc.ascii {
				failures = append(failures, fmt.Sprintf("permissive IDNA(%q) = %q, %v; want %q", input, got, err, tc.ascii))
			}
		}
	}
	// Hyphens in the second and third positions are valid even when their
	// UTF-8 byte offsets happen to be two and three.
	for _, input := range []string{"é--a.example", "xn----a-9la.example"} {
		if got, err := idna.DomainToASCIIStrict(input); err != nil || got != "xn----a-9la.example" {
			failures = append(failures, fmt.Sprintf("strict IDNA rejected valid hyphen positions in %q: %q, %v", input, got, err))
		}
	}
	if _, err := idna.DomainToASCIIStrict(strings.Repeat("a", 63) + ".example"); err != nil {
		failures = append(failures, "strict IDNA rejected a valid 63-byte label")
	}
	return failures
}

// Exercise the public setter and every host accessor together. A serialization
// check alone cannot detect a removed host left behind in the parsed record.
func checkHostRemovalContract() []string {
	var failures []string
	for _, tc := range []struct{ input, want string }{
		{"custom://user:pass@old.example:8042/a?q#frag", "custom:/a?q#frag"},
		{"custom://[2001:db8::1]:8042/a?q#frag", "custom:/a?q#frag"},
		{"file://old.example/a?q#frag", "file:///a?q#frag"},
		{"file://192.0.2.1/a?q#frag", "file:///a?q#frag"},
		{"file://[2001:db8::1]/a?q#frag", "file:///a?q#frag"},
		{"custom://old.example?q#frag", "custom:/?q#frag"},
	} {
		u, err := url.Parse(tc.input)
		if err != nil || u == nil {
			failures = append(failures, fmt.Sprintf("host removal setup %q: %v", tc.input, err))
			continue
		}
		if err := u.SetHost(nil); err != nil {
			failures = append(failures, fmt.Sprintf("SetHost(nil) on %q: %v", tc.input, err))
			continue
		}
		_, hostStrPresent := u.HostStr()
		_, hostPresent := u.Host()
		_, domainPresent := u.Domain()
		_, portPresent := u.Port()
		_, passwordPresent := u.Password()
		q, queryPresent := u.Query()
		f, fragmentPresent := u.Fragment()
		if u.AsStr() != tc.want || u.HasHost() || hostStrPresent || hostPresent || domainPresent || portPresent || passwordPresent || u.Username() != "" || u.Authority() != "" || u.CannotBeABase() || u.HasAuthority() != (u.Scheme() == "file") || q != "q" || !queryPresent || f != "frag" || !fragmentPresent {
			failures = append(failures, fmt.Sprintf("SetHost(nil) left an inconsistent URL record for %q: %q, HasHost=%v, HostStr=%v, Host=%v, Domain=%v", tc.input, u.AsStr(), u.HasHost(), hostStrPresent, hostPresent, domainPresent))
		}
		// Removing an already absent host is harmless. A new host can then be
		// installed without reviving credentials or the old explicit port.
		if err := u.SetHost(nil); err != nil || u.AsStr() != tc.want {
			failures = append(failures, fmt.Sprintf("repeated SetHost(nil) changed %q", tc.input))
		}
		replacement := "new.example"
		if err := u.SetHost(&replacement); err != nil {
			failures = append(failures, fmt.Sprintf("SetHost after removal on %q: %v", tc.input, err))
		} else if host, present := u.HostStr(); !present || host != replacement || !u.HasHost() || !u.HasAuthority() || u.Authority() != replacement {
			failures = append(failures, fmt.Sprintf("SetHost after removal did not restore a consistent host for %q", tc.input))
		}
	}
	for _, input := range []string{"http://old.example/a?q#frag", "https://[2001:db8::1]/a?q#frag"} {
		u, err := url.Parse(input)
		if err != nil || u == nil {
			failures = append(failures, "special host removal setup failed")
			continue
		}
		if err := u.SetHost(nil); !errors.Is(err, url.ErrEmptyHost) || u.AsStr() != input || !u.HasHost() {
			failures = append(failures, fmt.Sprintf("SetHost(nil) must reject removal of a required special host: %q", input))
		}
	}
	return failures
}

// Empty non-special authority paths need not gain a slash for a no-op.
// The public mutator contract still requires an observable empty segment.
func checkMinimalPathContract() []string {
	var failures []string
	for _, operation := range []struct {
		name string
		apply func(*url.PathSegmentsMut)
		allowEmptyPath bool
	}{
		{"Clear", func(m *url.PathSegmentsMut) { m.Clear() }, false},
		{"Pop", func(m *url.PathSegmentsMut) { m.Pop() }, false},
		{"PopIfEmpty", func(m *url.PathSegmentsMut) { m.PopIfEmpty() }, true},
		{"Extend(empty)", func(m *url.PathSegmentsMut) { m.Extend(nil) }, true},
		{"Push(dot)", func(m *url.PathSegmentsMut) { m.Push(".") }, true},
		{"Push(dot-dot)", func(m *url.PathSegmentsMut) { m.Push("..") }, true},
	} {
		empty, parseErr := url.Parse("custom://example.net?q#frag")
		if parseErr != nil || empty == nil {
			failures = append(failures, fmt.Sprintf("empty path setup failed: %v", parseErr))
			continue
		}
		segments, mutationErr := empty.PathSegmentsMut()
		if mutationErr != nil || segments == nil {
			failures = append(failures, "PathSegmentsMut rejected an empty hierarchical path")
			continue
		}
		operation.apply(segments)
		parts, present := empty.PathSegments()
		path := empty.Path()
		validPath := path == "/" || operation.allowEmptyPath && path == ""
		q, qPresent := empty.Query()
		f, fPresent := empty.Fragment()
		host, hostPresent := empty.HostStr()
		if !validPath || !present || !reflect.DeepEqual(parts, []string{""}) || empty.AsStr() != "custom://example.net"+path+"?q#frag" || empty.CannotBeABase() || !empty.HasAuthority() || !empty.HasHost() || !hostPresent || host != "example.net" || !qPresent || q != "q" || !fPresent || f != "frag" {
			failures = append(failures, fmt.Sprintf("%s lost the minimal path segment: %q", operation.name, empty.AsStr()))
		}
	}
	return failures
}

// Compare public path accessors with an independently expected serialization.
// An empty hierarchical path has zero segments; it differs from the root path,
// whose one empty segment serializes as "/". The public Go contract leaves the
// presence flag for the zero-segment case unspecified, so accept either flag.
func checkPathRecordContract(u *url.URL, serialized, path string) []string {
	var failures []string
	_, rest, hasScheme := strings.Cut(serialized, ":")
	if !hasScheme {
		return []string{"path contract requires an absolute expected URL"}
	}
	opaque := !strings.HasPrefix(rest, "/")
	if u.CannotBeABase() != opaque {
		failures = append(failures, fmt.Sprintf("CannotBeABase = %v, want %v for %q", u.CannotBeABase(), opaque, serialized))
	}
	parts, present := u.PathSegments()
	if opaque {
		_, hostStrPresent := u.HostStr()
		_, hostPresent := u.Host()
		_, domainPresent := u.Domain()
		_, portPresent := u.Port()
		if present || u.HasHost() || hostStrPresent || hostPresent || domainPresent || portPresent || u.HasAuthority() {
			failures = append(failures, "opaque URL exposed host, port, authority, or path segments")
		}
	} else if path == "" {
		if present && len(parts) != 0 {
			failures = append(failures, fmt.Sprintf("empty hierarchical path exposed phantom segments: %q", parts))
		}
	} else if !strings.HasPrefix(path, "/") {
		failures = append(failures, "expected hierarchical path has no leading slash")
	} else if want := strings.Split(path[1:], "/"); !present || !reflect.DeepEqual(parts, want) {
		failures = append(failures, fmt.Sprintf("PathSegments = %q (present=%v), want %q", parts, present, want))
	}
	return failures
}

// Snapshot observed state, rather than a desired result, when checking that a
// read-only operation leaves its receiver unchanged. Copy the segment slice so
// an aliased slice cannot hide a later mutation; ignore absent accessor values.
func pathRecordState(u *url.URL) []any {
	parts, present := u.PathSegments()
	if present { parts = append([]string(nil), parts...) } else { parts = nil }
	host, hostPresent := u.HostStr()
	if !hostPresent { host = "" }
	q, qPresent := u.Query()
	if !qPresent { q = "" }
	f, fPresent := u.Fragment()
	if !fPresent { f = "" }
	return []any{u.AsStr(), u.Path(), u.CannotBeABase(), u.HasAuthority(), u.HasHost(),
		host, hostPresent, parts, present, q, qPresent, f, fPresent}
}

// The written contract permits both Rust's empty opaque result and a standard
// root-path result when clearing a hostless non-special path. Select a complete
// expected state, then exercise the original record's operations in that state.
func checkClearedPathContract() []string {
	var failures []string
	for _, tc := range []struct {
		input string
		prefix string
		opaque bool
		path string
		allowRoot bool
	}{
		{"custom:/old", "custom:", true, "", true},
		{"custom:/old?q#frag", "custom:", true, "", true},
		{"alt+scheme:/deep/path?#", "alt+scheme:", true, "", true},
		{"custom://example.net/old?q#frag", "custom://example.net", false, "", false},
		{"custom:///old?#", "custom://", false, "", false},
		{"https://example.net/old?q#frag", "https://example.net/", false, "/", false},
		{"file:///old?q#frag", "file:///", false, "/", false},
	} {
		u, err := url.Parse(tc.input)
		if err != nil || u == nil {
			failures = append(failures, fmt.Sprintf("clear path setup %q: %v", tc.input, err))
			continue
		}
		q, qPresent := u.Query()
		f, fPresent := u.Fragment()
		host, hostPresent := u.HostStr()
		hadHost, hadAuthority := u.HasHost(), u.HasAuthority()
		suffix := ""
		if qPresent { suffix += "?" + q }
		if fPresent { suffix += "#" + f }
		u.SetPath("")
		prefix, path, opaque := tc.prefix, tc.path, tc.opaque
		if tc.allowRoot && u.Path() == "/" {
			prefix, path, opaque = tc.prefix+"/", "/", false
		}
		want := prefix + suffix
		newQ, newQPresent := u.Query()
		newF, newFPresent := u.Fragment()
		newHost, newHostPresent := u.HostStr()
		if u.AsStr() != want || u.Path() != path || u.CannotBeABase() != opaque || newQ != q || newQPresent != qPresent || newF != f || newFPresent != fPresent || newHost != host || newHostPresent != hostPresent || u.HasHost() != hadHost || u.HasAuthority() != hadAuthority {
			failures = append(failures, fmt.Sprintf("SetPath(empty) left an inconsistent URL record for %q: %q, CannotBeABase=%v", tc.input, u.AsStr(), u.CannotBeABase()))
			// Dependent operations have no accepted starting state here. Report
			// the setter defect without inventing downstream side effects.
			continue
		}
		for _, message := range checkPathRecordContract(u, want, path) {
			failures = append(failures, fmt.Sprintf("SetPath(empty) on %q: %s", tc.input, message))
		}
		// Check the original record, not just a newly parsed serialization:
		// stale internal flags can otherwise be hidden by reparsing.
		for _, reference := range []string{"next", "?replacement", "#replacement"} {
			before := pathRecordState(u)
			joined, joinErr := u.Join(reference)
			if !reflect.DeepEqual(pathRecordState(u), before) {
				failures = append(failures, fmt.Sprintf("Join(%q) changed its cleared base URL from %q", reference, tc.input))
			}
			if opaque && !strings.HasPrefix(reference, "#") {
				if joined != nil || !errors.Is(joinErr, url.ErrRelativeURLWithCannotBeABaseBase) {
					failures = append(failures, fmt.Sprintf("Join(%q) accepted a cleared cannot-be-a-base URL from %q", reference, tc.input))
				}
			} else {
				joinPath, joinWant := path, prefix
				if reference == "next" {
					joinPath = "/next"
					if path == "" { joinWant += "/" }
				} else if reference == "#replacement" && qPresent {
					joinWant += "?" + q
				}
				joinWant += reference
				if joinErr != nil || joined == nil || joined.AsStr() != joinWant || joined.Path() != joinPath || joined.CannotBeABase() != opaque {
					failures = append(failures, fmt.Sprintf("Join(%q) failed after clearing path for %q: %v", reference, tc.input, joinErr))
				} else {
					for _, message := range checkPathRecordContract(joined, joinWant, joinPath) {
						failures = append(failures, fmt.Sprintf("Join(%q) after clearing %q: %s", reference, tc.input, message))
					}
				}
			}
		}
		before := pathRecordState(u)
		m, mutationErr := u.PathSegmentsMut()
		if opaque {
			if mutationErr == nil || !reflect.DeepEqual(pathRecordState(u), before) {
				failures = append(failures, fmt.Sprintf("PathSegmentsMut accepted or changed a cleared cannot-be-a-base URL from %q", tc.input))
			}
		} else if mutationErr != nil || m == nil {
			failures = append(failures, fmt.Sprintf("PathSegmentsMut rejected a cleared hierarchical URL from %q", tc.input))
		}
	}
	return failures
}

func checkURLMutationContract() []string {
	failures := checkHostRemovalContract()
	failures = append(failures, checkMinimalPathContract()...)
	failures = append(failures, checkClearedPathContract()...)
	// Setter arguments are values to serialize, not shared mutable URL state.
	owned, err := url.Parse("https://example.net/a?q#frag")
	if err != nil || owned == nil {
		return append(failures, fmt.Sprintf("port ownership setup failed: %v", err))
	}
	port := uint16(8443)
	if err := owned.SetPort(&port); err != nil {
		failures = append(failures, fmt.Sprintf("SetPort failed: %v", err))
	} else {
		port = 9443
		got, present := owned.Port()
		known, knownPresent := owned.PortOrKnownDefault()
		if !present || got != 8443 || !knownPresent || known != 8443 || owned.AsStr() != "https://example.net:8443/a?q#frag" {
			failures = append(failures, "SetPort retained its caller's mutable pointer")
		}
	}
	// A path setter uses a state override, so leading delimiters belong to
	// the new path, while the existing query and fragment remain untouched.
	for _, tc := range []struct{ input, want string }{
		{"?query", "/%3Fquery"},
		{"#fragment", "/%23fragment"},
		{"?a#b", "/%3Fa%23b"},
	} {
		changed, err := url.Parse("custom://example.net/old?q#frag")
		if err != nil { failures = append(failures, "path setter setup failed"); continue }
		changed.SetPath(tc.input)
		if changed.Path() != tc.want || changed.AsStr() != "custom://example.net"+tc.want+"?q#frag" {
			failures = append(failures, fmt.Sprintf("SetPath(%q) dropped or reinterpreted path data: %q", tc.input, changed.AsStr()))
		}
	}
	u, err := url.Parse("http://example.com:443/a/b?q#frag")
	if err != nil {
		return []string{fmt.Sprintf("mutation setup failed: %v", err)}
	}
	if err := u.SetScheme("https:"); err != nil || u.AsStr() != "https://example.com/a/b?q#frag" {
		failures = append(failures, fmt.Sprintf("SetScheme with colon/default port: %q, %v", u.AsStr(), err))
	}
	m, err := u.PathSegmentsMut()
	if err != nil {
		return append(failures, "PathSegmentsMut rejected a hierarchical URL")
	}
	m.Clear().Extend([]string{".", "..", "x/y%", ""}).PopIfEmpty().Push("end")
	if u.Path() != "/x%2Fy%25/end" {
		failures = append(failures, fmt.Sprintf("path chain = %q", u.Path()))
	}
	q, qok := u.Query()
	f, fok := u.Fragment()
	if q != "q" || !qok || f != "frag" || !fok {
		failures = append(failures, "path mutation changed query or fragment")
	}
	u.QueryPairsMut().Clear().AppendKeyOnly("a b").ExtendPairs([][2]string{{"c", "+"}}).Finish()
	if q, ok := u.Query(); q != "a+b&c=%2B" || !ok {
		failures = append(failures, "query mutator key/extend chain failed")
	}
	u.QueryPairsMut().Clear().Finish()
	q, qok = u.Query()
	f, fok = u.Fragment()
	if q != "" || !qok || f != "frag" || !fok {
		failures = append(failures, "query Clear removed query presence or changed fragment")
	}
	host, err := url.ParseHost("[::ffff:192.0.2.128]")
	if err != nil || host.String() != "[::ffff:c000:280]" || host.Kind() != url.HostIPv6 {
		failures = append(failures, "IPv4-mapped IPv6 serialization failed")
	}
	return failures
}

func checkEncodingContract() []string {
	var failures []string
	controls := percentencoding.Controls
	complement := controls.Complement()
	combined := controls.Union(complement).Remove('a')
	for b := 0; b < 256; b++ {
		want := string([]byte{byte(b)})
		if b != 'a' {
			want = fmt.Sprintf("%%%02X", b)
		}
		if got := percentencoding.PercentEncode([]byte{byte(b)}, &combined); got != want {
			failures = append(failures, fmt.Sprintf("ASCII set operations failed at byte %d", b))
		}
	}
	d := percentencoding.DecodeString("ok%E2%82")
	_, err := d.DecodeUTF8()
	var utf8Err *percentencoding.UTF8Error
	if !errors.As(err, &utf8Err) {
		failures = append(failures, "truncated UTF-8 lacks UTF8Error")
	} else if _, hasLength := utf8Err.ErrorLen(); hasLength || utf8Err.ValidUpTo() != 2 {
		failures = append(failures, "truncated UTF-8 error metadata is wrong")
	}
	if d.DecodeUTF8Lossy() != "ok\ufffd" {
		failures = append(failures, "truncated UTF-8 must produce one replacement")
	}
	target := "existing=1"
	s := formurlencoded.NewSerializer(&target)
	s.AppendPair("a b", "+&")
	s.AppendKeyOnly("key")
	if s.Finish() != "existing=1&a+b=%2B%26&key" {
		failures = append(failures, "form serialization with existing target failed")
	}
	target = "existing=1"
	s = formurlencoded.NewSerializer(&target)
	s.Clear()
	s.ExtendPairs([][2]string{{"x", "y"}})
	s.ExtendKeysOnly([]string{"z"})
	if s.Finish() != "x=y&z" {
		failures = append(failures, "form Clear/Extend contract failed")
	}
	return failures
}

// Read every protected parsing vector independently of the candidate's own
// test suite. Rust's expected_failures.txt is not a contract exemption: the
// requested Go parser must produce the URL Standard's serialization.
func checkURLConformance() []string {
	data, err := os.ReadFile(repoRoot + "/url/tests/urltestdata.json")
	var cases []json.RawMessage
	if err != nil || json.Unmarshal(data, &cases) != nil || len(cases) == 0 {
		return []string{"cannot read nonempty public URL corpus"}
	}
	var failures []string
	total := 0
	for index, item := range cases {
		var tc struct {
			Input *string `json:"input"`
			Base *string `json:"base"`
			Failure bool `json:"failure"`
		}
		if json.Unmarshal(item, &tc) != nil || tc.Input == nil { continue } // corpus comments
		total++
		var expected map[string]json.RawMessage
		if err := json.Unmarshal(item, &expected); err != nil {
			failures = append(failures, fmt.Sprintf("URL corpus entry %d is invalid", index))
			continue
		}
		var parsed *url.URL
		var parseErr error
		if tc.Base == nil {
			parsed, parseErr = url.Parse(*tc.Input)
		} else {
			base, err := url.Parse(*tc.Base)
			if err != nil || base == nil {
				parseErr = fmt.Errorf("invalid base: %v", err)
			} else {
				parsed, parseErr = base.Join(*tc.Input)
			}
		}
		if tc.Failure {
			if parsed != nil || parseErr == nil {
				failures = append(failures, fmt.Sprintf("URL vector %d (%q): expected a nil URL and parse error", index, *tc.Input))
			}
			continue
		}
		if parseErr != nil || parsed == nil {
			failures = append(failures, fmt.Sprintf("URL vector %d (%q): unexpected error %v", index, *tc.Input, parseErr))
			continue
		}
		var expectedHref, expectedPath string
		if json.Unmarshal(expected["href"], &expectedHref) != nil || json.Unmarshal(expected["pathname"], &expectedPath) != nil {
			failures = append(failures, fmt.Sprintf("URL corpus entry %d lacks href/pathname", index))
		} else {
			for _, message := range checkPathRecordContract(parsed, expectedHref, expectedPath) {
				failures = append(failures, fmt.Sprintf("URL vector %d (%q): %s", index, *tc.Input, message))
			}
		}
		hostname, _ := parsed.HostStr()
		host, port := hostname, ""
		if value, present := parsed.Port(); present {
			port = fmt.Sprintf("%d", value)
			host += ":" + port
		}
		password, _ := parsed.Password()
		search, _ := parsed.Query()
		if search != "" { search = "?" + search }
		hash, _ := parsed.Fragment()
		if hash != "" { hash = "#" + hash }
		actual := map[string]string{
			"href": parsed.AsStr(), "protocol": parsed.Scheme()+":",
			"username": parsed.Username(), "password": password,
			"host": host, "hostname": hostname, "port": port,
			"pathname": parsed.Path(), "search": search, "hash": hash,
		}
		// The browser corpus's origin column is a different API (notably
		// blob origins); the migrated Origin contract has separate checks.
		for _, key := range []string{"href", "protocol", "username", "password", "host", "hostname", "port", "pathname", "search", "hash"} {
			if raw, present := expected[key]; present {
				var want string
				if json.Unmarshal(raw, &want) != nil || actual[key] != want {
					failures = append(failures, fmt.Sprintf("URL vector %d (%q): %s = %q, want %q", index, *tc.Input, key, actual[key], want))
					break
				}
			}
		}
	}
	if total == 0 { failures = append(failures, "public URL corpus has no parsing cases") }
	return failures
}
