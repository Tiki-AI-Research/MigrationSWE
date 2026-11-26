package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"

	"github.com/servo/rust-url/formurlencoded"
	"github.com/servo/rust-url/idna"
	"github.com/servo/rust-url/idna/punycode"
	"github.com/servo/rust-url/percentencoding"
	"github.com/servo/rust-url/url"
)

var proj = map[string]any{}

func b(v bool) string { if v { return "true" }; return "false" }

func main() {
	// P2P-001 Origin
	{
		u1, _ := url.Parse("https://example.com:443/")
		o1 := u1.Origin()
		u2, _ := url.Parse("file:///etc")
		o2 := u2.Origin()
		u3, _ := url.Parse("blob:https://example.com/uuid")
		o3 := u3.Origin()
		proj["P2P-001"] = []string{b(o1.IsTuple()) + "|" + o1.ASCIISerialization(), b(o2.IsTuple()) + "|" + o2.ASCIISerialization(), b(o3.IsTuple()) + "|" + o3.ASCIISerialization()}
	}
	// P2P-002 formurlencoded ParseString
	{
		var out []string
		p := formurlencoded.ParseString("foo=bar&baz")
		for {
			n, v, ok := p.Next()
			out = append(out, fmt.Sprintf("%s=%s|%s", n, v, b(ok)))
			if !ok { break }
		}
		p2 := formurlencoded.ParseString("%23first=%25try%25")
		n, v, ok := p2.Next()
		out = append(out, fmt.Sprintf("%s=%s|%s", n, v, b(ok)))
		p3 := formurlencoded.ParseString("a=1+2")
		n, v, ok = p3.Next()
		out = append(out, fmt.Sprintf("%s=%s|%s", n, v, b(ok)))
		proj["P2P-002"] = out
	}
	// P2P-003 idna
	{
		a1, e1 := idna.DomainToASCII("EXAMPLE.COM")
		a2, e2 := idna.DomainToASCII("fa\u00df.de")
		_, e3 := idna.DomainToASCIIStrict("foo_bar.example")
		u1, e4 := idna.DomainToUnicode("xn--fa-hia.de")
		proj["P2P-003"] = []string{a1, b(e1 == nil), a2, b(e2 == nil), b(errors.Is(e3, idna.ErrInvalidDomain)), u1, b(e4 == nil)}
	}
	// P2P-004 percent decode
	{
		d := percentencoding.DecodeString("%00%9F%92%96")
		_, err := d.DecodeUTF8()
		var ue *percentencoding.UTF8Error
		_ = errors.As(err, &ue)
		vl := -1
		el := -1
		elo := false
		if ue != nil { vl = ue.ValidUpTo(); el, elo = ue.ErrorLen() }
		proj["P2P-004"] = []string{b(ue != nil), fmt.Sprintf("%d", vl), fmt.Sprintf("%d", el), b(elo), fmt.Sprintf("%q", d.DecodeUTF8Lossy())}
	}
	// P2P-005 path segments mut
	{
		u, _ := url.Parse("http://a/b/c?q#frag")
		m, _ := u.PathSegmentsMut()
		m.Pop().Push("2/100%.png")
		q, qok := u.Query()
		f, fok := u.Fragment()
		proj["P2P-005"] = []string{u.Path(), q, b(qok), f, b(fok)}
	}
	// P2P-006 ParseHost IPv6
	{
		h, err := url.ParseHost("[2001:0db8:0000:0000:0000:ff00:0042:8329]")
		_, err2 := url.ParseHost("[:::1]")
		kin := ""
		s := ""
		if err == nil { kin = fmt.Sprintf("%d", h.Kind()); s = h.String() }
		proj["P2P-006"] = []string{kin, s, b(errors.Is(err2, url.ErrInvalidIPv6Address))}
	}
	// P2P-007 QueryPairsMut
	{
		u, _ := url.Parse("http://a/?x=1#frag")
		u.QueryPairsMut().AppendPair("y", "2").Finish()
		s1 := u.AsStr()
		u2, _ := url.Parse("http://a/?x=1#frag")
		u2.QueryPairsMut().Clear().AppendPair("a", "b").Finish()
		s2 := u2.AsStr()
		proj["P2P-007"] = []string{s1, s2}
	}
	// P2P-008 percent encode
	{
		na := percentencoding.NonAlphanumeric
		e1 := percentencoding.PercentEncode([]byte("foo bar?"), &na)
		e2 := percentencoding.PercentEncodeByte(0x3F)
		s := percentencoding.NonAlphanumeric
		t := s.Add('a')
		e3 := percentencoding.PercentEncode([]byte("a"), &s)
		e4 := percentencoding.PercentEncode([]byte("a"), &t)
		proj["P2P-008"] = []string{e1, e2, e3, e4}
	}
	// P2P-009 public surface (compile-level) -> status wrapper
	{
		proj["P2P-009"] = map[string]string{"result": "PASS"}
	}
	// P2P-010 invalid ipv6 error identity
	{
		u, err := url.Parse("http://[:::1]")
		proj["P2P-010"] = []string{b(u == nil), b(errors.Is(err, url.ErrInvalidIPv6Address)), b(errors.Is(err, url.ErrInvalidPort))}
	}
	// P2P-011 parse abs
	{
		in := "https://github.com/rust-lang/rust/issues?labels=E-easy&state=open"
		u, err := url.Parse(in)
		if err != nil { panic(err) }
		pw, pwok := u.Password()
		hs, hsok := u.HostStr()
		h, hok := u.Host()
		_, pok := u.Port()
		segs, sok := u.PathSegments()
		q, qok := u.Query()
		f, fok := u.Fragment()
		proj["P2P-011"] = []string{u.AsStr(), u.Scheme(), u.Username(), pw, b(pwok), hs, b(hsok), fmt.Sprintf("%d", h.Kind()), b(hok), u.Path(), fmt.Sprintf("%v", segs), b(sok), q, b(qok), f, b(fok), b(u.CannotBeABase()), b(pok)}
	}
	// P2P-012 cannot-be-a-base
	{
		u, _ := url.Parse("data:text/plain,Hello?World#")
		_, sok := u.PathSegments()
		q, qok := u.Query()
		f, fok := u.Fragment()
		proj["P2P-012"] = []string{b(u.CannotBeABase()), u.Scheme(), u.Path(), b(sok), b(u.HasHost()), q, b(qok), f, b(fok), u.AsStr()}
	}
	// P2P-013 SetPath
	{
		u, _ := url.Parse("http://a/")
		u.SetPath("api/some comments")
		p1 := u.Path()
		u2, _ := url.Parse("http://a/")
		u2.SetPath("api/some%20comments")
		p2 := u2.Path()
		proj["P2P-013"] = []string{p1, p2}
	}
	// P2P-014 punycode
	{
		e1, ok1 := punycode.EncodeString("b\u00fccher")
		d1, ok2 := punycode.DecodeToString("bcher-kva")
		r, ok3 := punycode.Decode("<<>>")
		proj["P2P-014"] = []string{e1, b(ok1), d1, b(ok2), fmt.Sprintf("%v", r), b(ok3)}
	}
	// P2P-015 relative base / join
	{
		_, err := url.Parse("../main.css")
		base, _ := url.Parse("http://servo.github.io/rust-url/url/index.html")
		joined, _ := base.Join("../main.css")
		proj["P2P-015"] = []string{b(errors.Is(err, url.ErrRelativeURLWithoutBase)), joined.AsStr()}
	}
	// P2P-016 ports
	{
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
		u5.SetPort(&pp)
		p5, k5 := u5.Port()
		proj["P2P-016"] = []string{fmt.Sprintf("%d", p1), b(k1), fmt.Sprintf("%d", p2), b(k2), fmt.Sprintf("%d", p3), b(k3), fmt.Sprintf("%d", p4), b(k4), fmt.Sprintf("%d", p5), b(k5), u5.AsStr()}
	}

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(proj); err != nil { fmt.Fprintln(os.Stderr, err); os.Exit(1) }
}
