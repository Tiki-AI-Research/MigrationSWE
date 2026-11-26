package f2pprobe

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/servo/rust-url/formurlencoded"
	"github.com/servo/rust-url/idna"
	"github.com/servo/rust-url/idna/punycode"
	"github.com/servo/rust-url/percentencoding"
	url "github.com/servo/rust-url/url"
)

func req(cond bool, format string, a ...any) error {
	if cond {
		return nil
	}
	return fmt.Errorf(format, a...)
}

func targetRoot() string {
	if r := os.Getenv("F2P_TARGET_ROOT"); r != "" {
		return r
	}
	return "."
}

func obs025() error {
	u, err := url.Parse("https://example.com/")
	if err != nil {
		return err
	}
	return req(u.AsStr() == "https://example.com/", "AsStr=%q", u.AsStr())
}

func obs026() error {
	o, _ := url.Parse("https://example.com:443/")
	a := o.Origin()
	if !a.IsTuple() {
		return errors.New("https://example.com:443 origin not a tuple")
	}
	if got := a.ASCIISerialization(); got != "https://example.com" {
		return fmt.Errorf("tuple serialization=%q", got)
	}
	f, _ := url.Parse("file:///etc")
	fo := f.Origin()
	if fo.IsTuple() {
		return errors.New("file origin reported as tuple")
	}
	if got := fo.ASCIISerialization(); got != "null" {
		return fmt.Errorf("file serialization=%q", got)
	}
	b, _ := url.Parse("blob:https://example.com/uuid")
	bo := b.Origin()
	if !bo.IsTuple() {
		return errors.New("blob origin did not derive inner tuple origin")
	}
	if got := bo.ASCIISerialization(); got != "https://example.com" {
		return fmt.Errorf("blob serialization=%q", got)
	}
	return nil
}

func obs032() error {
	cases := []struct {
		in       string
		wantPort uint16
	}{
		{"http://example.com/", 80},
		{"https://x/", 443},
		{"ftp://x/", 21},
	}
	for _, c := range cases {
		u, err := url.Parse(c.in)
		if err != nil {
			return err
		}
		if p, ok := u.PortOrKnownDefault(); p != c.wantPort || !ok {
			return fmt.Errorf("%s PortOrKnownDefault=(%d,%v)", c.in, p, ok)
		}
		if _, ok := u.Port(); ok {
			return fmt.Errorf("%s Port() should be absent", c.in)
		}
	}
	u, _ := url.Parse("https://x:8443/")
	if p, ok := u.Port(); p != 8443 || !ok {
		return fmt.Errorf("explicit port=(%d,%v)", p, ok)
	}
	d, _ := url.Parse("http://example.com/")
	p := uint16(80)
	if err := d.SetPort(&p); err != nil {
		return err
	}
	if _, ok := d.Port(); ok {
		return errors.New("scheme-default port was not elided from Port()")
	}
	return req(d.AsStr() == "http://example.com/", "AsStr=%q", d.AsStr())
}

func obs033() error {
	p := formurlencoded.ParseString("foo=bar&baz")
	if n, v, ok := p.Next(); n != "foo" || v != "bar" || !ok {
		return fmt.Errorf("first=(%q,%q,%v)", n, v, ok)
	}
	if n, v, ok := p.Next(); n != "baz" || v != "" || !ok {
		return fmt.Errorf("second=(%q,%q,%v)", n, v, ok)
	}
	if _, _, ok := p.Next(); ok {
		return errors.New("parser not exhausted")
	}
	q := formurlencoded.ParseString("%23first=%25try%25")
	if n, v, ok := q.Next(); n != "#first" || v != "%try%" || !ok {
		return fmt.Errorf("percent=(%q,%q,%v)", n, v, ok)
	}
	r := formurlencoded.ParseString("a=1+2")
	if n, v, ok := r.Next(); n != "a" || v != "1 2" || !ok {
		return fmt.Errorf("plus=(%q,%q,%v)", n, v, ok)
	}
	return nil
}

func obs034() error {
	p := formurlencoded.ParseString("a=%00%9F%92%96")
	n, v, ok := p.Next()
	want := "\x00\uFFFD\uFFFD\uFFFD"
	if n != "a" || v != want || !ok {
		return fmt.Errorf("lossy=(%q,%q,%v) want %q", n, v, ok, want)
	}
	return nil
}

func obs035() error {
	u, err := url.Parse("http://[:::1]")
	if u != nil {
		return errors.New("expected nil *URL")
	}
	if !errors.Is(err, url.ErrInvalidIPv6Address) {
		return fmt.Errorf("err=%v", err)
	}
	d, err := idna.DomainToASCII("example.com")
	if err != nil || d != "example.com" {
		return fmt.Errorf("DomainToASCII=(%q,%v)", d, err)
	}
	return nil
}

func obs036() error {
	if d, err := idna.DomainToASCII("EXAMPLE.COM"); err != nil || d != "example.com" {
		return fmt.Errorf("EXAMPLE.COM=(%q,%v)", d, err)
	}
	if d, err := idna.DomainToASCII("faß.de"); err != nil || d != "xn--fa-hia.de" {
		return fmt.Errorf("faß.de=(%q,%v)", d, err)
	}
	if _, err := idna.DomainToASCIIStrict("foo_bar.example"); !errors.Is(err, idna.ErrInvalidDomain) {
		return fmt.Errorf("strict error=%v", err)
	}
	if d, err := idna.DomainToASCII("foo_bar.example"); err != nil || d != "foo_bar.example" {
		return fmt.Errorf("non-strict=(%q,%v)", d, err)
	}
	if d, err := idna.DomainToUnicode("xn--fa-hia.de"); err != nil || d != "faß.de" {
		return fmt.Errorf("DomainToUnicode=(%q,%v)", d, err)
	}
	return nil
}

func obs038() error { return obs035() }

func obs040() error {
	u, err := url.Parse("data:text/plain,Hello?World#")
	if err != nil {
		return err
	}
	if !u.CannotBeABase() {
		return errors.New("CannotBeABase=false")
	}
	if u.Scheme() != "data" {
		return fmt.Errorf("scheme=%q", u.Scheme())
	}
	if u.Path() != "text/plain,Hello" {
		return fmt.Errorf("path=%q", u.Path())
	}
	if _, ok := u.PathSegments(); ok {
		return errors.New("PathSegments ok for cannot-be-a-base")
	}
	if u.HasHost() {
		return errors.New("HasHost true for cannot-be-a-base")
	}
	if q, ok := u.Query(); q != "World" || !ok {
		return fmt.Errorf("query=(%q,%v)", q, ok)
	}
	if f, ok := u.Fragment(); f != "" || !ok {
		return fmt.Errorf("fragment=(%q,%v)", f, ok)
	}
	return req(u.AsStr() == "data:text/plain,Hello?World#", "AsStr=%q", u.AsStr())
}

func obs042() error {
	if s, ok := punycode.EncodeString("bücher"); s != "bcher-kva" || !ok {
		return fmt.Errorf("encode=(%q,%v)", s, ok)
	}
	if s, ok := punycode.DecodeToString("bcher-kva"); s != "bücher" || !ok {
		return fmt.Errorf("decode=(%q,%v)", s, ok)
	}
	if r, ok := punycode.Decode("<<>>"); r != nil || ok {
		return fmt.Errorf("invalid input=(%v,%v)", r, ok)
	}
	return nil
}

func obs045() error {
	u, err := url.Parse("http://[:::1]")
	if u != nil {
		return errors.New("expected nil *URL")
	}
	if !errors.Is(err, url.ErrInvalidIPv6Address) {
		return fmt.Errorf("err=%v does not match ErrInvalidIPv6Address", err)
	}
	if errors.Is(err, url.ErrInvalidPort) {
		return errors.New("err incorrectly matched ErrInvalidPort")
	}
	return nil
}

func obs030() error {
	u, err := url.Parse("data:text/plain,Hello?World#")
	if err != nil {
		return err
	}
	if f, ok := u.Fragment(); f != "" || !ok {
		return fmt.Errorf("empty-but-present fragment=(%q,%v)", f, ok)
	}
	v, err := url.Parse("data:text/plain,Hello")
	if err != nil {
		return err
	}
	if f, ok := v.Fragment(); f != "" || ok {
		return fmt.Errorf("absent fragment=(%q,%v)", f, ok)
	}
	return nil
}

func obs027() error {
	h, err := url.ParseHost("[2001:0db8:0000:0000:0000:ff00:0042:8329]")
	if err != nil {
		return err
	}
	if h.Kind() != url.HostIPv6 {
		return fmt.Errorf("kind=%v", h.Kind())
	}
	if got := h.String(); got != "[2001:db8::ff00:42:8329]" {
		return fmt.Errorf("String=%q", got)
	}
	if _, err := url.ParseHost("[:::1]"); !errors.Is(err, url.ErrInvalidIPv6Address) {
		return fmt.Errorf("err=%v", err)
	}
	return nil
}

func obs031() error {
	if got := percentencoding.PercentEncodeByte(0x3F); got != "%3F" {
		return fmt.Errorf("PercentEncodeByte=%q", got)
	}
	s := percentencoding.NonAlphanumeric
	tt := s.Add('a')
	if got := percentencoding.PercentEncode([]byte("a"), &s); got != "a" {
		return fmt.Errorf("receiver mutated: %q", got)
	}
	if got := percentencoding.PercentEncode([]byte("a"), &tt); got != "%61" {
		return fmt.Errorf("added byte not encoded: %q", got)
	}
	return nil
}

func obs046() error {
	if got := percentencoding.PercentEncode([]byte("foo bar?"), &percentencoding.NonAlphanumeric); got != "foo%20bar%3F" {
		return fmt.Errorf("encode=%q", got)
	}
	return obs031()
}

func obs039() error {
	d := percentencoding.DecodeString("%00%9F%92%96")
	v, err := d.DecodeUTF8()
	if v != "" {
		return fmt.Errorf("value=%q", v)
	}
	var ue *percentencoding.UTF8Error
	if !errors.As(err, &ue) {
		return fmt.Errorf("not *UTF8Error: %v", err)
	}
	if ue.ValidUpTo() != 1 {
		return fmt.Errorf("ValidUpTo=%d", ue.ValidUpTo())
	}
	n, ok := ue.ErrorLen()
	if n != 1 || !ok {
		return fmt.Errorf("ErrorLen=(%d,%v)", n, ok)
	}
	if got := d.DecodeUTF8Lossy(); got != "\x00\uFFFD\uFFFD\uFFFD" {
		return fmt.Errorf("DecodeUTF8Lossy=%q", got)
	}
	return nil
}

func obs048() error { return obs032() }
func obs044() error { return obs040() }

func obs101registry() error {
	const in = "https://github.com/rust-lang/rust/issues?labels=E-easy&state=open"
	u, err := url.Parse(in)
	if err != nil {
		return err
	}
	if u.AsStr() != in {
		return fmt.Errorf("AsStr=%q", u.AsStr())
	}
	if u.Scheme() != "https" {
		return fmt.Errorf("scheme=%q", u.Scheme())
	}
	if u.Username() != "" {
		return fmt.Errorf("username=%q", u.Username())
	}
	if _, ok := u.Password(); ok {
		return errors.New("password present")
	}
	if h, ok := u.HostStr(); !ok || h != "github.com" {
		return fmt.Errorf("HostStr=(%q,%v)", h, ok)
	}
	if host, ok := u.Host(); !ok || host.Kind() != url.HostDomain {
		return fmt.Errorf("Host=(%v,%v)", host, ok)
	}
	if p, ok := u.Port(); p != 0 || ok {
		return fmt.Errorf("Port=(%d,%v)", p, ok)
	}
	if u.Path() != "/rust-lang/rust/issues" {
		return fmt.Errorf("Path=%q", u.Path())
	}
	segs, ok := u.PathSegments()
	if !ok || len(segs) != 3 || segs[0] != "rust-lang" || segs[1] != "rust" || segs[2] != "issues" {
		return fmt.Errorf("PathSegments=%v", segs)
	}
	if q, ok := u.Query(); !ok || q != "labels=E-easy&state=open" {
		return fmt.Errorf("Query=(%q,%v)", q, ok)
	}
	if f, ok := u.Fragment(); ok || f != "" {
		return fmt.Errorf("Fragment=(%q,%v)", f, ok)
	}
	if u.CannotBeABase() {
		return errors.New("CannotBeABase=true")
	}
	return nil
}

func TestF2PObservations(t *testing.T) {
	checks := []struct {
		obs string
		fn  func() error
	}{
		{"F2P-025", obs025},
		{"F2P-026", obs026},
		{"F2P-027", obs027},
		{"F2P-028", obs026},
		{"F2P-029", obs025},
		{"F2P-030", obs030},
		{"F2P-031", obs031},
		{"F2P-032", obs032},
		{"F2P-033", obs033},
		{"F2P-034", obs034},
		{"F2P-035", obs035},
		{"F2P-036", obs036},
		{"F2P-038", obs038},
		{"F2P-039", obs039},
		{"F2P-040", obs040},
		{"F2P-042", obs042},
		{"F2P-044", obs044},
		{"F2P-045", obs045},
		{"F2P-046", obs046},
		{"F2P-048", obs048},
	}
	for _, c := range checks {
		t.Run(c.obs, func(t *testing.T) {
			if err := c.fn(); err != nil {
				t.Fatalf("%s: %v", c.obs, err)
			}
			fmt.Printf("OBS %s PASS\n", c.obs)
		})
	}
}

func gitOut(root string, args ...string) (string, error) {
	c := exec.Command("git", args...)
	c.Dir = root
	b, err := c.CombinedOutput()
	return string(b), err
}

func obs037() error {
	root := targetRoot()
	for _, p := range []string{
		"url/tests/urltestdata.json",
		"url/tests/setters_tests.json",
		"idna/tests/IdnaTestV2.txt",
		"idna/tests/punycode_tests.json",
		"idna/tests/bad_punycode_tests.json",
	} {
		if _, err := os.Stat(filepath.Join(root, p)); err != nil {
			return err
		}
	}
	out, err := gitOut(root, "diff", "--name-only")
	if err != nil {
		return fmt.Errorf("git diff: %v: %s", err, out)
	}
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, "url/tests/") || strings.HasPrefix(line, "idna/tests/") {
			return fmt.Errorf("protected conformance file modified: %s", line)
		}
	}
	return nil
}

func obs041() error {
	root := targetRoot()
	out, err := gitOut(root, "diff", "--name-only")
	if err != nil {
		return fmt.Errorf("git diff: %v: %s", err, out)
	}
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		for _, p := range []string{"url/src/", "idna/src/", "percent_encoding/src/", "form_urlencoded/src/", "Cargo.toml"} {
			if strings.HasPrefix(line, p) {
				return fmt.Errorf("Rust crate source modified: %s", line)
			}
		}
	}
	u, err := url.Parse("https://github.com/rust-lang/rust/issues?labels=E-easy&state=open")
	if err != nil {
		return err
	}
	return req(u.Path() == "/rust-lang/rust/issues", "Path=%q", u.Path())
}

func obs043() error {
	root := targetRoot()
	out, err := gitOut(root, "diff", "--name-only")
	if err != nil {
		return err
	}
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasSuffix(line, "fuzz_targets/parse.rs") || strings.HasPrefix(line, "url/benches/") {
			return fmt.Errorf("benchmark/fuzz target modified: %s", line)
		}
	}
	return nil
}

var (
	f2pSkipCall      = regexp.MustCompile(`\.\s*Skip(?:f|Now)?\s*\(`)
	f2pSkipMethod    = regexp.MustCompile(`\)\s*\.\s*Skip(?:f|Now)?\b`)
	f2pSkipReflect   = regexp.MustCompile(`\.\s*MethodByName\s*\(\s*['"]Skip(?:f|Now)?['"]\s*\)`)
	f2pSkipValue     = regexp.MustCompile(`(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)+Skip(?:f|Now)?\b`)
	f2pSkipValueBind = regexp.MustCompile(`(?:^|[;{}\n])[ \t]*(?:var[ \t]+)?\b([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|=)\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)+Skip(?:f|Now)?\b`)
	f2pSkipBoolBef   = regexp.MustCompile(`(?:^|[^\w])(?:if|for|switch|case|!|&&|\|\||==|!=)\s*$`)
	f2pSkipBoolAft   = regexp.MustCompile(`\A\s*(?:\{|&&|\|\||==|!=)`)
)

// f2pHasGoSkip recognises a testing.T skip across all equivalent spellings
// (selector call, method expression, a method value stored anywhere and invoked
// directly or indirectly, reflection) instead of the literal "t.Skip"
// substring.  RE2 lacks lookahead, so a matched method value is checked for a
// trailing call byte; a value used only as a boolean condition, and a direct
// binding whose target is never invoked, are treated as ordinary field reads.
func f2pHasGoSkip(source string) bool {
	if f2pSkipCall.MatchString(source) ||
		f2pSkipMethod.MatchString(source) ||
		f2pSkipReflect.MatchString(source) {
		return true
	}
	var bound [][2]int
	for _, match := range f2pSkipValueBind.FindAllStringSubmatchIndex(source, -1) {
		if match[2] < 0 {
			continue
		}
		if f2pSkipTrailingCall(source, match[1]) {
			continue
		}
		bound = append(bound, [2]int{match[0], match[1]})
		name := regexp.QuoteMeta(source[match[2]:match[3]])
		if regexp.MustCompile(`\b` + name + `\s*\(`).MatchString(source) {
			return true
		}
	}
	for _, match := range f2pSkipValue.FindAllStringIndex(source, -1) {
		if f2pSkipTrailingCall(source, match[1]) {
			continue
		}
		if f2pSkipCovered(match, bound) {
			continue
		}
		if f2pSkipBoolBef.MatchString(source[:match[0]]) {
			continue
		}
		if f2pSkipBoolAft.MatchString(source[match[1]:]) {
			continue
		}
		return true
	}
	return false
}

func f2pSkipTrailingCall(source string, end int) bool {
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

func f2pSkipCovered(match []int, bound [][2]int) bool {
	for _, span := range bound {
		if span[0] <= match[0] && match[1] <= span[1] {
			return true
		}
	}
	return false
}

func obs047() error {
	root := targetRoot()
	var bad []string
	filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() || !strings.HasSuffix(p, "_test.go") {
			return nil
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return nil
		}
		if f2pHasGoSkip(string(b)) {
			bad = append(bad, p)
		}
		return nil
	})
	if len(bad) > 0 {
		return fmt.Errorf("go test skip found in %v", bad)
	}
	return nil
}

func TestF2PRepoObservations(t *testing.T) {
	checks := []struct {
		obs string
		fn  func() error
	}{
		{"F2P-037", obs037},
		{"F2P-041", obs041},
		{"F2P-043", obs043},
		{"F2P-047", obs047},
	}
	for _, c := range checks {
		t.Run(c.obs, func(t *testing.T) {
			if err := c.fn(); err != nil {
				t.Fatalf("%s: %v", c.obs, err)
			}
			fmt.Printf("OBS %s PASS\n", c.obs)
		})
	}
}
