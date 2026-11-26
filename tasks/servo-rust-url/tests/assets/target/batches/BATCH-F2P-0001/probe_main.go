package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"

	"github.com/servo/rust-url/idna"
	"github.com/servo/rust-url/idna/punycode"
	"github.com/servo/rust-url/percentencoding"
	"github.com/servo/rust-url/url"
)

type result struct {
	ObservationID string `json:"observation_id"`
	Status        string `json:"status"`
	Detail        string `json:"detail"`
}

type report struct {
	Side    string   `json:"side"`
	Results []result `json:"results"`
}

var results []result

func rec(ids []string, ok bool, detail string) {
	status := "FAIL"
	if ok {
		status = "PASS"
	}
	for _, id := range ids {
		results = append(results, result{ObservationID: id, Status: status, Detail: detail})
	}
}

func main() {
	side := os.Getenv("PROBE_SIDE")
	if side == "" {
		side = "target"
	}

	// F2P-001 / F2P-005 : QueryPairsMut keeps fragment; Clear == empty query
	func() {
		u, err := url.Parse("http://a/?x=1#frag")
		if err != nil {
			rec([]string{"F2P-001", "F2P-005"}, false, err.Error())
			return
		}
		got := u.QueryPairsMut().Clear().AppendPair("a", "b").Finish().AsStr()
		rec([]string{"F2P-001", "F2P-005"}, got == "http://a/?a=b#frag", got)
	}()

	// F2P-002 / F2P-008 / F2P-009 : no rust delegation; invalid IPv6 + idna
	func() {
		_, err := url.Parse("http://[:::1]")
		ok := err != nil && errors.Is(err, url.ErrInvalidIPv6Address)
		d, derr := idna.DomainToASCII("example.com")
		ok = ok && d == "example.com" && derr == nil
		rec([]string{"F2P-002", "F2P-008", "F2P-009"}, ok, fmt.Sprintf("parse=%v idna=%q/%v", err, d, derr))
	}()

	// F2P-003 : parse canonical URL under pure-Go build
	func() {
		in := "https://github.com/rust-lang/rust/issues?labels=E-easy&state=open"
		u, err := url.Parse(in)
		ok := err == nil && u.AsStr() == in && u.Scheme() == "https"
		rec([]string{"F2P-003"}, ok, fmt.Sprintf("%v %q", err, u))
	}()

	// F2P-004 : pure Go dependencies
	func() {
		u, err := url.Parse("https://example.com/")
		rec([]string{"F2P-004"}, err == nil && u.AsStr() == "https://example.com/", fmt.Sprintf("%v", err))
	}()

	// F2P-006 : punycode RFC3492
	func() {
		e, eok := punycode.EncodeString("bücher")
		d, dok := punycode.DecodeToString("bcher-kva")
		_, badok := punycode.Decode("<<>>")
		ok := e == "bcher-kva" && eok && d == "bücher" && dok && !badok
		rec([]string{"F2P-006"}, ok, fmt.Sprintf("enc=%q/%v dec=%q/%v bad=%v", e, eok, d, dok, badok))
	}()

	// F2P-007 / F2P-018 : strict vs non-strict idna
	func() {
		a, aerr := idna.DomainToASCII("foo_bar.example")
		_, serr := idna.DomainToASCIIStrict("foo_bar.example")
		ok := a == "foo_bar.example" && aerr == nil && serr != nil && errors.Is(serr, idna.ErrInvalidDomain)
		rec([]string{"F2P-007", "F2P-018"}, ok, fmt.Sprintf("loose=%q/%v strict=%v", a, aerr, serr))
	}()

	// F2P-010 / F2P-016 : PathSegmentsMut percent-encoding
	func() {
		u, err := url.Parse("http://a/b/c?q#frag")
		if err != nil {
			rec([]string{"F2P-010", "F2P-016"}, false, err.Error())
			return
		}
		m, e := u.PathSegmentsMut()
		if e != nil {
			rec([]string{"F2P-010", "F2P-016"}, false, "mut err "+e.Error())
			return
		}
		m.Pop().Push("2/100%.png")
		q, qok := u.Query()
		f, fok := u.Fragment()
		ok := u.Path() == "/b/2%2F100%25.png" && q == "q" && qok && f == "frag" && fok
		rec([]string{"F2P-010", "F2P-016"}, ok, fmt.Sprintf("path=%q q=%q/%v f=%q/%v", u.Path(), q, qok, f, fok))
	}()

	// F2P-011 : IPv6 RFC5952
	func() {
		h, err := url.ParseHost("[2001:0db8:0000:0000:0000:ff00:0042:8329]")
		s := ""
		if err == nil {
			s = h.String()
		}
		_, err2 := url.ParseHost("[:::1]")
		ok := err == nil && h.Kind() == url.HostIPv6 && s == "[2001:db8::ff00:42:8329]" &&
			err2 != nil && errors.Is(err2, url.ErrInvalidIPv6Address)
		rec([]string{"F2P-011"}, ok, fmt.Sprintf("host=%q err2=%v", s, err2))
	}()

	// F2P-014 : fragment comma-ok semantics
	func() {
		u1, _ := url.Parse("data:text/plain,Hello?World#")
		u2, _ := url.Parse("data:text/plain,Hello")
		f1, ok1 := u1.Fragment()
		f2, ok2 := u2.Fragment()
		ok := f1 == "" && ok1 && f2 == "" && !ok2
		rec([]string{"F2P-014"}, ok, fmt.Sprintf("present=%q/%v absent=%q/%v", f1, ok1, f2, ok2))
	}()

	// F2P-015 : PercentEncode uppercase + value semantics Add
	func() {
		pe := percentencoding.PercentEncode([]byte("foo bar?"), &percentencoding.NonAlphanumeric)
		pb := percentencoding.PercentEncodeByte(0x3F)
		s := percentencoding.NonAlphanumeric
		t := s.Add('a')
		sa := percentencoding.PercentEncode([]byte("a"), &s)
		ta := percentencoding.PercentEncode([]byte("a"), &t)
		ok := pe == "foo%20bar%3F" && pb == "%3F" && sa == "a" && ta == "%61"
		rec([]string{"F2P-015"}, ok, fmt.Sprintf("pe=%q pb=%q s=%q t=%q", pe, pb, sa, ta))
	}()

	// F2P-019 / F2P-023 / F2P-024 : UTF8 error reporting / lossy
	func() {
		d := percentencoding.DecodeString("%00%9F%92%96")
		out, err := d.DecodeUTF8()
		var ue *percentencoding.UTF8Error
		okErr := errors.As(err, &ue)
		validUpTo, errLen, errLenOK := -1, -1, false
		if okErr {
			validUpTo = ue.ValidUpTo()
			errLen, errLenOK = ue.ErrorLen()
		}
		lossy := percentencoding.DecodeString("%00%9F%92%96").DecodeUTF8Lossy()
		ok := okErr && validUpTo == 1 && errLen == 1 && errLenOK && lossy == "\x00\uFFFD\uFFFD\uFFFD"
		rec([]string{"F2P-019", "F2P-023", "F2P-024"}, ok,
			fmt.Sprintf("out=%q err=%v vup=%d elen=%d/%v lossy=%q", out, err, validUpTo, errLen, errLenOK, lossy))
	}()

	// F2P-021 / F2P-022 : sentinel error identity
	func() {
		_, err := url.Parse("http://[:::1]")
		ok := err != nil && errors.Is(err, url.ErrInvalidIPv6Address) && !errors.Is(err, url.ErrInvalidPort)
		rec([]string{"F2P-021", "F2P-022"}, ok, fmt.Sprint(err))
	}()

	// F2P-017 / F2P-020 : full accessor contract on canonical URL
	func() {
		in := "https://github.com/rust-lang/rust/issues?labels=E-easy&state=open"
		u, err := url.Parse(in)
		if err != nil {
			rec([]string{"F2P-017", "F2P-020"}, false, err.Error())
			return
		}
		pw, pwOK := u.Password()
		host, hostOK := u.Host()
		port, portOK := u.Port()
		segs, segOK := u.PathSegments()
		q, qOK := u.Query()
		f, fOK := u.Fragment()
		hs, hsOK := u.HostStr()
		ok := u.AsStr() == in && u.Scheme() == "https" && u.Username() == "" &&
			pw == "" && !pwOK && hs == "github.com" && hsOK &&
			host.Kind() == url.HostDomain && port == 0 && !portOK && hostOK &&
			u.Path() == "/rust-lang/rust/issues" && reflect.DeepEqual(segs, []string{"rust-lang", "rust", "issues"}) && segOK &&
			q == "labels=E-easy&state=open" && qOK && f == "" && !fOK && !u.CannotBeABase()
		rec([]string{"F2P-017", "F2P-020"}, ok, fmt.Sprintf("%v", u))
	}()

	rep := report{Side: side, Results: results}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(rep)
}
