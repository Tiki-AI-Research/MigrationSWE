package probe

import (
	"errors"
	"testing"

	"github.com/servo/rust-url/formurlencoded"
	"github.com/servo/rust-url/idna"
	"github.com/servo/rust-url/idna/punycode"
	"github.com/servo/rust-url/percentencoding"
	"github.com/servo/rust-url/url"
)

func TestF2P_050_PathSegmentsMut(t *testing.T) {
	u, err := url.Parse("http://a/b/c?q#frag")
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	m, err := u.PathSegmentsMut()
	if err != nil {
		t.Fatalf("mut: %v", err)
	}
	m.Pop().Push("2/100%.png")
	if got := u.Path(); got != "/b/2%2F100%25.png" {
		t.Fatalf("Path=%q", got)
	}
	if q, ok := u.Query(); q != "q" || !ok {
		t.Fatalf("Query=%q,%v", q, ok)
	}
	if f, ok := u.Fragment(); f != "frag" || !ok {
		t.Fatalf("Fragment=%q,%v", f, ok)
	}
}

func TestF2P_051_065_111_IDNAStrict(t *testing.T) {
	if got, err := idna.DomainToASCII("foo_bar.example"); got != "foo_bar.example" || err != nil {
		t.Fatalf("DomainToASCII=%q,%v", got, err)
	}
	if _, err := idna.DomainToASCIIStrict("foo_bar.example"); !errors.Is(err, idna.ErrInvalidDomain) {
		t.Fatalf("strict err=%v", err)
	}
}

func TestF2P_052_057_110_UTF8Error(t *testing.T) {
	d := percentencoding.DecodeString("%00%9F%92%96")
	_, err := d.DecodeUTF8()
	ue, ok := err.(*percentencoding.UTF8Error)
	if !ok {
		t.Fatalf("err type %T", err)
	}
	if ue.ValidUpTo() != 1 {
		t.Fatalf("ValidUpTo=%d", ue.ValidUpTo())
	}
	if l, has := ue.ErrorLen(); l != 1 || !has {
		t.Fatalf("ErrorLen=%d,%v", l, has)
	}
	if got := d.DecodeUTF8Lossy(); got != "\x00\uFFFD\uFFFD\uFFFD" {
		t.Fatalf("lossy=%q", got)
	}
}

func TestF2P_054_021_StdlibIDNA(t *testing.T) {
	if got, err := idna.DomainToASCII("faß.de"); got != "xn--fa-hia.de" || err != nil {
		t.Fatalf("DomainToASCII=%q,%v", got, err)
	}
}

func TestF2P_055_003_ParseAccessors(t *testing.T) {
	const in = "https://github.com/rust-lang/rust/issues?labels=E-easy&state=open"
	u, err := url.Parse(in)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if u.AsStr() != in {
		t.Fatalf("AsStr=%q", u.AsStr())
	}
	if u.Scheme() != "https" || u.Username() != "" {
		t.Fatalf("scheme/user=%q/%q", u.Scheme(), u.Username())
	}
	if _, ok := u.Password(); ok {
		t.Fatalf("password present")
	}
	if h, ok := u.HostStr(); h != "github.com" || !ok {
		t.Fatalf("HostStr=%q,%v", h, ok)
	}
	if h, ok := u.Host(); !ok || h.Kind() != url.HostDomain {
		t.Fatalf("Host=%v,%v", h, ok)
	}
	if p, ok := u.Port(); p != 0 || ok {
		t.Fatalf("Port=%d,%v", p, ok)
	}
	if u.Path() != "/rust-lang/rust/issues" {
		t.Fatalf("Path=%q", u.Path())
	}
	segs, ok := u.PathSegments()
	if !ok || len(segs) != 3 || segs[0] != "rust-lang" || segs[1] != "rust" || segs[2] != "issues" {
		t.Fatalf("PathSegments=%v,%v", segs, ok)
	}
	if q, ok := u.Query(); q != "labels=E-easy&state=open" || !ok {
		t.Fatalf("Query=%q,%v", q, ok)
	}
	if f, ok := u.Fragment(); f != "" || ok {
		t.Fatalf("Fragment=%q,%v", f, ok)
	}
	if u.CannotBeABase() {
		t.Fatalf("CannotBeABase true")
	}
}

func TestF2P_058_112_Punycode(t *testing.T) {
	if s, ok := punycode.EncodeString("bücher"); s != "bcher-kva" || !ok {
		t.Fatalf("EncodeString=%q,%v", s, ok)
	}
	if s, ok := punycode.DecodeToString("bcher-kva"); s != "bücher" || !ok {
		t.Fatalf("DecodeToString=%q,%v", s, ok)
	}
	if r, ok := punycode.Decode("<<>>"); r != nil || ok {
		t.Fatalf("Decode=%v,%v", r, ok)
	}
}

func TestF2P_059_061_062_109_EncodeByteAdd(t *testing.T) {
	if got := percentencoding.PercentEncodeByte(0x3F); got != "%3F" {
		t.Fatalf("PercentEncodeByte=%q", got)
	}
	s := percentencoding.NonAlphanumeric
	tt := s.Add('a')
	if got := percentencoding.PercentEncode([]byte("a"), &s); got != "a" {
		t.Fatalf("s encode=%q", got)
	}
	if got := percentencoding.PercentEncode([]byte("a"), &tt); got != "%61" {
		t.Fatalf("t encode=%q", got)
	}
}

func TestF2P_060_063_070_102_SentinelIPv6(t *testing.T) {
	u, err := url.Parse("http://[:::1]")
	if u != nil {
		t.Fatalf("u=%v", u)
	}
	if !errors.Is(err, url.ErrInvalidIPv6Address) {
		t.Fatalf("err=%v", err)
	}
	if errors.Is(err, url.ErrInvalidPort) {
		t.Fatalf("err is ErrInvalidPort")
	}
}

func TestF2P_064_071_105_QueryPairsMut(t *testing.T) {
	u, err := url.Parse("http://a/?x=1#frag")
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	got := u.QueryPairsMut().Clear().AppendPair("a", "b").Finish().AsStr()
	if got != "http://a/?a=b#frag" {
		t.Fatalf("AsStr=%q", got)
	}
	p := formurlencoded.ParseString("foo=bar&baz")
	if n, v, ok := p.Next(); n != "foo" || v != "bar" || !ok {
		t.Fatalf("next1=%q,%q,%v", n, v, ok)
	}
	if n, v, ok := p.Next(); n != "baz" || v != "" || !ok {
		t.Fatalf("next2=%q,%q,%v", n, v, ok)
	}
	if _, _, ok := p.Next(); ok {
		t.Fatalf("next3 ok")
	}
}

func TestF2P_066_103_Ports(t *testing.T) {
	u, _ := url.Parse("http://example.com/")
	if p, ok := u.PortOrKnownDefault(); p != 80 || !ok {
		t.Fatalf("default=%d,%v", p, ok)
	}
	if p, ok := u.Port(); p != 0 || ok {
		t.Fatalf("port=%d,%v", p, ok)
	}
	v, _ := url.Parse("https://x:8443/")
	if p, ok := v.Port(); p != 8443 || !ok {
		t.Fatalf("8443=%d,%v", p, ok)
	}
	p := uint16(80)
	if err := u.SetPort(&p); err != nil {
		t.Fatalf("SetPort: %v", err)
	}
	if pp, ok := u.Port(); pp != 0 || ok {
		t.Fatalf("after set port=%d,%v", pp, ok)
	}
	if u.AsStr() != "http://example.com/" {
		t.Fatalf("AsStr=%q", u.AsStr())
	}
}

func TestF2P_067_068_002_NoDelegation(t *testing.T) {
	u, err := url.Parse("http://[:::1]")
	if u != nil || !errors.Is(err, url.ErrInvalidIPv6Address) {
		t.Fatalf("u=%v err=%v", u, err)
	}
	if got, err := idna.DomainToASCII("example.com"); got != "example.com" || err != nil {
		t.Fatalf("idna=%q,%v", got, err)
	}
}

func TestF2P_069_101_FragmentPresence(t *testing.T) {
	a, err := url.Parse("data:text/plain,Hello?World#")
	if err != nil {
		t.Fatalf("parse a: %v", err)
	}
	if f, ok := a.Fragment(); f != "" || !ok {
		t.Fatalf("a fragment=%q,%v", f, ok)
	}
	b, err := url.Parse("data:text/plain,Hello")
	if err != nil {
		t.Fatalf("parse b: %v", err)
	}
	if f, ok := b.Fragment(); f != "" || ok {
		t.Fatalf("b fragment=%q,%v", f, ok)
	}
}

func TestF2P_072_106_IPv6Host(t *testing.T) {
	h, err := url.ParseHost("[2001:0db8:0000:0000:0000:ff00:0042:8329]")
	if err != nil {
		t.Fatalf("ParseHost: %v", err)
	}
	if h.String() != "[2001:db8::ff00:42:8329]" {
		t.Fatalf("String=%q", h.String())
	}
	if _, err := url.ParseHost("[:::1]"); !errors.Is(err, url.ErrInvalidIPv6Address) {
		t.Fatalf("err=%v", err)
	}
}

func TestF2P_053_024_PathOutcome(t *testing.T) {
	u, err := url.Parse("https://github.com/rust-lang/rust/issues?labels=E-easy&state=open")
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if u.Path() != "/rust-lang/rust/issues" {
		t.Fatalf("Path=%q", u.Path())
	}
}
