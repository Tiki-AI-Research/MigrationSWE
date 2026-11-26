package oauth2_test

import (
	"context"
	"encoding/json"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"

	o "oauth2/oauth2"
)

// f2pClient is an injected fake HTTP client that asserts the exact request the
// implementation produces. It mirrors the reference fake client.
type f2pClient struct {
	t       *testing.T
	method  string
	url     string
	header  map[string]string
	body    string
	resp    o.HTTPResponse
	respErr error
	calls   int
}

func (c *f2pClient) Call(req o.HTTPRequest) (o.HTTPResponse, error) {
	c.calls++
	if c.method != "" && req.Method != c.method {
		c.t.Errorf("method: got %q, want %q", req.Method, c.method)
	}
	if c.url != "" && req.URL != c.url {
		c.t.Errorf("url: got %q, want %q", req.URL, c.url)
	}
	if c.header != nil && !reflect.DeepEqual(req.Header, c.header) {
		c.t.Errorf("header: got %#v, want %#v", req.Header, c.header)
	}
	if c.body != "" && string(req.Body) != c.body {
		c.t.Errorf("body: got %q, want %q", string(req.Body), c.body)
	}
	if c.respErr != nil {
		return o.HTTPResponse{}, c.respErr
	}
	return c.resp, nil
}

func f2pJSONResp(status int, body string) o.HTTPResponse {
	return o.HTTPResponse{StatusCode: status, Header: map[string]string{"Content-Type": "application/json"}, Body: []byte(body)}
}

func f2pResp(status int, header map[string]string, body string) o.HTTPResponse {
	return o.HTTPResponse{StatusCode: status, Header: header, Body: []byte(body)}
}

func mustAuthURL(t *testing.T, raw string) *o.AuthUrl {
	t.Helper()
	u, err := o.NewAuthUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func mustTokenURL(t *testing.T, raw string) *o.TokenUrl {
	t.Helper()
	u, err := o.NewTokenUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func mustRedirectURL(t *testing.T, raw string) *o.RedirectUrl {
	t.Helper()
	u, err := o.NewRedirectUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func mustRevocationURL(t *testing.T, raw string) *o.RevocationUrl {
	t.Helper()
	u, err := o.NewRevocationUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func f2pBasicHeaders() map[string]string {
	return map[string]string{
		"Accept":        "application/json",
		"Content-Type":  "application/x-www-form-urlencoded",
		"Authorization": "Basic YWFhOmJiYg==",
	}
}

func f2pNewClient(t *testing.T) *o.BasicClient {
	t.Helper()
	return o.NewBasicClient(o.NewClientId("aaa")).
		SetAuthURL(mustAuthURL(t, "https://example.com/auth")).
		SetTokenURL(mustTokenURL(t, "https://example.com/token")).
		SetClientSecret(o.NewClientSecret("bbb"))
}

// F2P-001: behavioral parity with the Rust fake-client scenarios.
func TestF2P0001_FakeClientParity(t *testing.T) {
	client := f2pNewClient(t)
	rc := &f2pClient{
		t:      t,
		method: "POST",
		url:    "https://example.com/token",
		header: f2pBasicHeaders(),
		body:   "grant_type=client_credentials",
		resp:   f2pJSONResp(200, `{"access_token":"12/34","token_type":"bearer","scope":"read write"}`),
	}
	req, err := client.ExchangeClientCredentials()
	if err != nil {
		t.Fatal(err)
	}
	token, tokenErr := req.Request(rc)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if token.AccessToken().Secret() != "12/34" {
		t.Fatalf("access token: got %q", token.AccessToken().Secret())
	}
	if token.TokenTypeValue() != o.BasicTokenTypeBearer {
		t.Fatalf("token type: got %v", token.TokenTypeValue())
	}
	if len(token.Scopes()) != 2 || token.Scopes()[0].AsStr() != "read" || token.Scopes()[1].AsStr() != "write" {
		t.Fatalf("scopes: got %#v", token.Scopes())
	}
}

// F2P-002 and F2P-009: Content-Type and empty-body validation.
func TestF2P0002_ContentTypeAndEmptyBody(t *testing.T) {
	f2pCheckMediaTypes(t)
	client := f2pNewClient(t)

	req, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	_, tokenErr := req.Request(&f2pClient{
		t:    t,
		resp: f2pResp(200, map[string]string{"Content-Type": "text/plain"}, "broken json"),
	})
	if tokenErr == nil {
		t.Fatal("expected content-type error")
	}
	if tokenErr.Kind != o.RequestTokenErrorOther {
		t.Fatalf("kind: got %v", tokenErr.Kind)
	}
	if !strings.Contains(tokenErr.Error(), "text/plain") {
		t.Fatalf("error should name the content type: %q", tokenErr.Error())
	}

	req2, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	_, tokenErr2 := req2.Request(&f2pClient{
		t:    t,
		resp: f2pResp(200, map[string]string{"Content-Type": "application/json"}, ""),
	})
	if tokenErr2 == nil {
		t.Fatal("expected empty-body error")
	}
	if tokenErr2.Kind != o.RequestTokenErrorOther {
		t.Fatalf("kind: got %v", tokenErr2.Kind)
	}
	if !strings.Contains(tokenErr2.Error(), "empty response body") {
		t.Fatalf("error should report empty body: %q", tokenErr2.Error())
	}
}

// F2P-003: scope (de)serialization as a space-joined string.
func TestF2P0003_ScopeRoundTrip(t *testing.T) {
	var resp o.BasicTokenResponse
	if err := json.Unmarshal([]byte(`{"access_token":"t","token_type":"bearer","scope":"a b c"}`), &resp); err != nil {
		t.Fatal(err)
	}
	scopes := resp.Scopes()
	if len(scopes) != 3 || scopes[0].AsStr() != "a" || scopes[1].AsStr() != "b" || scopes[2].AsStr() != "c" {
		t.Fatalf("scopes: got %#v", scopes)
	}
	serialized, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(serialized), `"scope":"a b c"`) {
		t.Fatalf("scope must serialize as a single space-joined string: %s", serialized)
	}
}

// F2P-004: go.mod declares module oauth2 / go 1.22 with no third-party deps.
func TestF2P0004_DependencyManifest(t *testing.T) {
	gomod := f2pFindUp(t, "go.mod")
	data, err := os.ReadFile(gomod)
	if err != nil {
		t.Fatal(err)
	}
	src := string(data)
	if !regexp.MustCompile(`(?m)^module\s+oauth2\s*$`).MatchString(src) {
		t.Fatalf("go.mod must declare module oauth2:\n%s", src)
	}
	if !regexp.MustCompile(`(?m)^go\s+1\.22\s*$`).MatchString(src) {
		t.Fatalf("go.mod must declare go 1.22:\n%s", src)
	}
	if regexp.MustCompile(`(?m)^\s*require\b`).MatchString(src) {
		t.Fatalf("go.mod must not contain require directives:\n%s", src)
	}
	for _, imp := range f2pImports(t, false) {
		if strings.Contains(imp, "golang.org/x/oauth2") || strings.Contains(imp, "oauth2") && strings.Contains(imp, ".") {
			t.Fatalf("third-party OAuth2 import found: %q", imp)
		}
	}
}

// F2P-005 and F2P-013: Sync/Async call operation with case-insensitive content type.
func TestF2P0005_CallOperation(t *testing.T) {
	client := f2pNewClient(t)

	async := o.AsyncHTTPClientFunc(func(ctx context.Context, req o.HTTPRequest) (o.HTTPResponse, error) {
		return f2pResp(200, map[string]string{"Content-Type": "Application/JSON; charset=utf-8"},
			`{"access_token":"12/34","token_type":"bearer"}`), nil
	})
	req, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	token, tokenErr := req.RequestAsync(context.Background(), async)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if token.AccessToken().Secret() != "12/34" {
		t.Fatalf("access token: got %q", token.AccessToken().Secret())
	}

	sync := o.SyncHTTPClientFunc(func(req o.HTTPRequest) (o.HTTPResponse, error) {
		return f2pResp(200, map[string]string{"Content-Type": "application/json"},
			`{"access_token":"56/78","token_type":"bearer"}`), nil
	})
	req2, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	token2, tokenErr2 := req2.Request(sync)
	if tokenErr2 != nil {
		t.Fatalf("unexpected error: %v", tokenErr2)
	}
	if token2.AccessToken().Secret() != "56/78" {
		t.Fatalf("access token: got %q", token2.AccessToken().Secret())
	}
}

// F2P-006: non-200 token error mapping.
func TestF2P0006_Non200ErrorMapping(t *testing.T) {
	client := f2pNewClient(t)
	for _, body := range []string{`{}`, `null`, `{"error":null}`, `{"error":17}`} {
		request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
		if err != nil {
			t.Fatal(err)
		}
		_, failure := request.Request(&f2pClient{t: t, resp: f2pJSONResp(400, body)})
		if failure == nil || failure.Kind != o.RequestTokenErrorParse || string(failure.RawBody) != body {
			t.Fatalf("invalid required error field in %s must produce a parse error carrying the body: %#v", body, failure)
		}
	}

	req, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	_, serverErr := req.Request(&f2pClient{t: t, resp: f2pJSONResp(400, `{"error":"invalid_request","error_description":"x"}`)})
	if serverErr == nil || serverErr.Kind != o.RequestTokenErrorServerResponse {
		t.Fatalf("server response kind: %#v", serverErr)
	}

	req2, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	_, parseErr := req2.Request(&f2pClient{t: t, resp: f2pResp(400, nil, "not json")})
	if parseErr == nil || parseErr.Kind != o.RequestTokenErrorParse {
		t.Fatalf("parse kind: %#v", parseErr)
	}
	if string(parseErr.RawBody) != "not json" {
		t.Fatalf("raw body: got %q", parseErr.RawBody)
	}

	req3, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	_, otherErr := req3.Request(&f2pClient{t: t, resp: f2pResp(400, nil, "")})
	if otherErr == nil || otherErr.Kind != o.RequestTokenErrorOther {
		t.Fatalf("other kind: %#v", otherErr)
	}
}

// F2P-008 and F2P-010: runtime endpoint completeness validation.
func TestF2P0008_MissingTokenEndpoint(t *testing.T) {
	client := o.NewBasicClient(o.NewClientId("aaa"))
	req, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if req != nil {
		t.Fatal("expected nil request when token URL is unset")
	}
	if err == nil {
		t.Fatal("expected missing URL error")
	}
	ce, ok := err.(*o.ConfigurationError)
	if !ok {
		t.Fatalf("error type: %T", err)
	}
	if ce.Kind != o.ConfigurationMissingURL || ce.Endpoint != "token" {
		t.Fatalf("error: %#v", ce)
	}
	if ce.Error() != "No token endpoint URL specified" {
		t.Fatalf("message: got %q", ce.Error())
	}
}

// F2P-011 and F2P-012: authorization URL parameter set, encoding and ordering.
func TestF2P0011_AuthorizationURLOrdering(t *testing.T) {
	auth := mustAuthURL(t, "https://auth.example/authorize")
	client := o.NewBasicClient(o.NewClientId("client")).SetAuthURL(auth)

	req, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("xyz") })
	if err != nil {
		t.Fatal(err)
	}
	u, _ := req.URL()
	if u.RawQuery != "response_type=code&client_id=client&state=xyz" {
		t.Fatalf("raw query: got %q", u.RawQuery)
	}

	req2, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("xyz") })
	if err != nil {
		t.Fatal(err)
	}
	u2, _ := req2.AddScope(o.NewScope("a b")).URL()
	if u2.RawQuery != "response_type=code&client_id=client&state=xyz&scope=a+b" {
		t.Fatalf("raw query with scope: got %q", u2.RawQuery)
	}
}

func TestF2P0012_AuthorizationURLParamSet(t *testing.T) {
	auth := mustAuthURL(t, "https://auth.example/authorize")
	redirect := mustRedirectURL(t, "https://localhost/redirect")
	client := o.NewBasicClient(o.NewClientId("client")).
		SetAuthURL(auth).
		SetRedirectURL(redirect)

	req, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("xyz") })
	if err != nil {
		t.Fatal(err)
	}
	verifier := o.NewPkceCodeVerifier("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
	u, _ := req.SetPkceChallenge(o.NewPkceCodeChallengeFromVerifierSha256(verifier)).
		AddExtraParam("foo", "bar").
		URL()
	want := "response_type=code&client_id=client&state=xyz" +
		"&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM" +
		"&code_challenge_method=S256" +
		"&redirect_uri=https%3A%2F%2Flocalhost%2Fredirect" +
		"&foo=bar"
	if u.RawQuery != want {
		t.Fatalf("raw query:\n got %q\nwant %q", u.RawQuery, want)
	}
}

// F2P-014, F2P-019 and F2P-023: insecure revocation URL rejection before any request.
func TestF2P0014_InsecureRevocationURL(t *testing.T) {
	client := f2pNewClient(t).SetRevocationURL(mustRevocationURL(t, "http://revocation/url"))
	rc := &f2pClient{t: t}
	req, err := client.RevokeToken(o.NewStandardRevocableTokenFromAccessToken(o.NewAccessToken("access_token_123")))
	if req != nil {
		t.Fatal("expected nil request for insecure URL")
	}
	if err == nil {
		t.Fatal("expected insecure URL error")
	}
	ce, ok := err.(*o.ConfigurationError)
	if !ok {
		t.Fatalf("error type: %T", err)
	}
	if ce.Kind != o.ConfigurationInsecureURL || ce.Endpoint != "revocation" {
		t.Fatalf("error: %#v", ce)
	}
	if rc.calls != 0 {
		t.Fatalf("HTTP client must not be called, got %d calls", rc.calls)
	}
}

// F2P-015: introspection audience accepts a string or an array.
func TestF2P0015_AudienceDecoding(t *testing.T) {
	f2pCheckOptionalTokenType(t)
	var single o.BasicTokenIntrospectionResponse
	if err := json.Unmarshal([]byte(`{"active":true,"aud":"one"}`), &single); err != nil {
		t.Fatal(err)
	}
	if len(single.Aud()) != 1 || single.Aud()[0] != "one" {
		t.Fatalf("single audience: %#v", single.Aud())
	}

	var list o.BasicTokenIntrospectionResponse
	if err := json.Unmarshal([]byte(`{"active":true,"aud":["one","two"]}`), &list); err != nil {
		t.Fatal(err)
	}
	if len(list.Aud()) != 2 || list.Aud()[0] != "one" || list.Aud()[1] != "two" {
		t.Fatalf("list audience: %#v", list.Aud())
	}
}

// F2P-016: a revocation 200 response body is ignored.
func TestF2P0016_RevocationIgnoresBody(t *testing.T) {
	f2pCheckRevocationResponses(t)
	client := f2pNewClient(t).SetRevocationURL(mustRevocationURL(t, "https://revocation/url"))
	rc := &f2pClient{t: t, resp: f2pResp(200, nil, `{"access_token":"must-be-ignored"}`)}
	req, err := client.RevokeToken(o.NewStandardRevocableTokenFromAccessToken(o.NewAccessToken("access_token_123")))
	if err != nil {
		t.Fatal(err)
	}
	if tokenErr := req.Request(rc); tokenErr != nil {
		t.Fatalf("expected nil error for 200 response, got %v", tokenErr)
	}
}

// F2P-017: StandardErrorResponse JSON preserves the canonical key order.
func TestF2P0017_OrderedErrorSerialization(t *testing.T) {
	f2pCheckCanonicalEnums(t)
	description := "bad"
	uri := "u"
	resp := o.NewStandardErrorResponse(o.BasicErrorResponseTypeInvalidGrant, &description, &uri)
	serialized, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"error":"invalid_grant","error_description":"bad","error_uri":"u"}`
	if string(serialized) != want {
		t.Fatalf("serialized: got %s, want %s", serialized, want)
	}
}

// F2P-018 and F2P-032: token type parsing is case-insensitive and canonical.
func TestF2P0018_TokenTypeCaseInsensitive(t *testing.T) {
	f2pCheckDirectTokenTypes(t)
	var resp o.BasicTokenResponse
	if err := json.Unmarshal([]byte(`{"access_token":"t","token_type":"BEARER"}`), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.TokenTypeValue() != o.BasicTokenTypeBearer {
		t.Fatalf("token type: got %v", resp.TokenTypeValue())
	}
	serialized, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(serialized), `"token_type":"bearer"`) {
		t.Fatalf("token type must serialize canonically: %s", serialized)
	}
}

// F2P-021: production code does not delegate to the Rust runtime.
func TestF2P0021_NoRuntimeDelegation(t *testing.T) {
	// Import-level: every delegation mechanism needs one of these packages, so a
	// migrated package that declares the mechanism as an import is rejected here.
	for _, imp := range f2pImports(t, false) {
		switch {
		case imp == "C":
			t.Fatalf("cgo import found: %q", imp)
		case imp == "os/exec":
			t.Fatalf("os/exec found: %q", imp)
		case imp == "net/rpc" || strings.HasPrefix(imp, "net/rpc/"):
			t.Fatalf("RPC import found: %q", imp)
		case imp == "plugin":
			t.Fatalf("dynamic loading import found: %q", imp)
		case imp == "runtime/cgo" || strings.HasPrefix(imp, "runtime/cgo"):
			t.Fatalf("cgo runtime import found: %q", imp)
		}
	}

	// Symbol/alias level: resolve the package path behind every import alias so
	// a renamed import (o "os", ex "os/exec", u "golang.org/x/sys/unix") cannot
	// hide a delegation sink, and match the sink against the resolved *package
	// path* rather than a fixed spelling.  Documentation that merely names a
	// sink must not count, so comments and string literals are blanked first.
	// The raw-syscall entry points are only a mechanism alongside an exec-family
	// syscall constant, while the exec-family constant is itself fail-closed
	// even with no visible import (a dot import or helper alias cannot hide it).
	sinkSelectors := map[string][]string{
		"os/exec":               {"Command", "CommandContext", "Cmd"},
		"os":                    {"StartProcess"},
		"syscall":               {"Exec", "ForkExec", "StartProcess"},
		"plugin":                {"Open"},
		"golang.org/x/sys/unix": {"Exec", "Execve", "ForkExec", "PosixSpawn"},
	}
	rawCall := regexp.MustCompile(`\b(?:Raw)?Syscall(?:6)?\s*\(`)
	execConstant := regexp.MustCompile(`\bSYS_(?:EXECVE|EXECVEAT|FORK|VFORK|POSIX_SPAWN)\b`)
	bareExecConstant := regexp.MustCompile(`\bSYS_(?:EXECVE|EXECVEAT|POSIX_SPAWN)\b`)
	processStart := regexp.MustCompile(`\bProcess\b[^;\n]*?\.\s*Start\s*\(`)
	for name, source := range f2pNonTestSources(t) {
		code := f2pStripComments(source)
		for alias, path := range f2pImportAliases(t, name, source) {
			if alias == "." || alias == "_" {
				continue
			}
			for _, selector := range sinkSelectors[path] {
				sink := regexp.MustCompile(`\b` + regexp.QuoteMeta(alias) + `\s*\.\s*` + selector + `\b`)
				if sink.MatchString(code) {
					t.Fatalf("runtime delegation sink %s.%s found in %s", alias, selector, name)
				}
			}
			if path == "syscall" && execConstant.MatchString(code) && rawCall.MatchString(code) {
				t.Fatalf("raw exec syscall found in %s", name)
			}
			if path == "os" && processStart.MatchString(code) {
				t.Fatalf("os.Process.Start found in %s", name)
			}
		}
		if bareExecConstant.MatchString(code) {
			t.Fatalf("exec-family syscall constant found in %s", name)
		}
	}
	// The linkname compiler directive is a comment, so it is matched on the raw
	// source before comments are blanked.
	linkname := regexp.MustCompile(`(?m)^\s*//go:linkname\b`)
	for name, source := range f2pNonTestSources(t) {
		if linkname.MatchString(source) {
			t.Fatalf("//go:linkname directive found in %s", name)
		}
	}
}

// f2pNonTestSources returns the raw text of every non-test .go file in the
// package directory, keyed by file name.
func f2pNonTestSources(t *testing.T) map[string]string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	sources := map[string]string{}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		sources[name] = string(data)
	}
	return sources
}

// f2pImportAliases resolves the package path behind every import alias in one
// production source file, keyed by the name the file uses to reference it.
// An unaliased import binds the last path segment (os/exec -> exec).
func f2pImportAliases(t *testing.T, name string, source string) map[string]string {
	t.Helper()
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, name, source, parser.ImportsOnly)
	if err != nil {
		t.Fatalf("parse %s: %v", name, err)
	}
	aliases := map[string]string{}
	for _, spec := range file.Imports {
		path := strings.Trim(spec.Path.Value, `"`)
		alias := path
		if index := strings.LastIndex(path, "/"); index >= 0 {
			alias = path[index+1:]
		}
		if spec.Name != nil && spec.Name.Name != "" {
			alias = spec.Name.Name
		}
		aliases[alias] = path
	}
	return aliases
}

// f2pStripComments blanks // and /* */ comments while preserving string and
// rune literals and the line structure, so a sink that only appears in prose
// is not mistaken for a call.
func f2pStripComments(text string) string {
	out := make([]byte, 0, len(text))
	n := len(text)
	for i := 0; i < n; {
		c := text[i]
		if c == '"' || c == '\'' || c == '`' {
			quote := c
			out = append(out, c)
			i++
			for i < n {
				ch := text[i]
				if ch == '\\' && quote != '`' && i+1 < n {
					out = append(out, ch, text[i+1])
					i += 2
					continue
				}
				out = append(out, ch)
				i++
				if ch == quote {
					break
				}
			}
			continue
		}
		if c == '/' && i+1 < n && text[i+1] == '/' {
			for i < n && text[i] != '\n' {
				out = append(out, ' ')
				i++
			}
			continue
		}
		if c == '/' && i+1 < n && text[i+1] == '*' {
			out = append(out, ' ', ' ')
			i += 2
			for i < n && !(text[i] == '*' && i+1 < n && text[i+1] == '/') {
				if text[i] == '\n' {
					out = append(out, '\n')
				} else {
					out = append(out, ' ')
				}
				i++
			}
			if i < n {
				out = append(out, ' ', ' ')
				i += 2
			}
			continue
		}
		out = append(out, c)
		i++
	}
	return string(out)
}

// F2P-020: client construction, endpoint setters and fallible accessors.
func TestF2P0020_ClientConstructionAndSetters(t *testing.T) {
	auth := mustAuthURL(t, "https://auth")
	token := mustTokenURL(t, "https://token")
	redirect := mustRedirectURL(t, "https://redir")
	device, err := o.NewDeviceAuthorizationUrl("https://device")
	if err != nil {
		t.Fatal(err)
	}
	introspection, err := o.NewIntrospectionUrl("https://introspect")
	if err != nil {
		t.Fatal(err)
	}
	revocation := mustRevocationURL(t, "https://revoke")

	client := o.NewBasicClient(o.NewClientId("id")).SetClientSecret(o.NewClientSecret("s"))
	if client.AuthType() != o.BasicAuth {
		t.Fatalf("default auth type: got %v", client.AuthType())
	}
	client.SetAuthURL(auth).SetTokenURL(token).SetRedirectURL(redirect).
		SetDeviceAuthorizationURL(device).SetIntrospectionURL(introspection).SetRevocationURL(revocation)
	// The contract preserves endpoint values; setters and accessors may copy
	// their URLs rather than retain the caller's pointer.
	if client.AuthURI() == nil || client.AuthURI().String() != auth.String() ||
		client.TokenURI() == nil || client.TokenURI().String() != token.String() ||
		client.RedirectURI() == nil || client.RedirectURI().String() != redirect.String() ||
		client.DeviceAuthorizationURL() == nil || client.DeviceAuthorizationURL().String() != device.String() ||
		client.IntrospectionURL() == nil || client.IntrospectionURL().String() != introspection.String() ||
		client.RevocationURL() == nil || client.RevocationURL().String() != revocation.String() {
		t.Fatal("setters did not store their endpoints")
	}
	if client.ClientId().String() != "id" {
		t.Fatalf("client id: got %q", client.ClientId().String())
	}

	// Conditional setters accept an optional endpoint and produce fallible accessors.
	client.SetAuthURLOption(nil).SetTokenURLOption(nil)
	if client.AuthURI() != nil || client.TokenURI() != nil {
		t.Fatal("conditional setters did not clear the endpoints")
	}
	if _, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("x") }); err == nil {
		t.Fatal("expected fallible accessor error when auth URL unset")
	}
}

// F2P-022: public configuration and error contract identity.
func TestF2P0022_ContractIdentity(t *testing.T) {
	f2pCheckCodeScopes(t)
	var _ o.AuthType = o.BasicAuth
	var _ o.AuthType = o.RequestBody
	var _ *o.AuthUrl
	var _ *o.TokenUrl
	var _ *o.RedirectUrl
	var _ *o.IntrospectionUrl
	var _ *o.RevocationUrl
	var _ *o.DeviceAuthorizationUrl

	missing := &o.ConfigurationError{Kind: o.ConfigurationMissingURL, Endpoint: "revocation"}
	if missing.Error() != "No revocation endpoint URL specified" {
		t.Fatalf("missing message: got %q", missing.Error())
	}
	insecure := &o.ConfigurationError{Kind: o.ConfigurationInsecureURL, Endpoint: "revocation"}
	if insecure.Error() != "Scheme for revocation endpoint URL must be HTTPS" {
		t.Fatalf("insecure message: got %q", insecure.Error())
	}
	_ = &o.RequestTokenError{Kind: o.RequestTokenErrorServerResponse}
	_ = &o.HTTPClientError{Kind: o.HTTPClientErrorBackend}
	_ = o.NewStandardErrorResponse(o.BasicErrorResponseTypeInvalidRequest, nil, nil)
}

// F2P-007: caller-supplied SyncHTTPClientFunc is the one invoked.
func TestF2P0007_HTTPClientInjection(t *testing.T) {
	client := f2pNewClient(t)
	called := false
	fn := o.SyncHTTPClientFunc(func(req o.HTTPRequest) (o.HTTPResponse, error) {
		called = true
		return f2pJSONResp(200, `{"access_token":"injected","token_type":"bearer"}`), nil
	})
	req, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	token, tokenErr := req.Request(fn)
	if !called {
		t.Fatal("injected client was not invoked")
	}
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if token.AccessToken().Secret() != "injected" {
		t.Fatalf("access token: got %q", token.AccessToken().Secret())
	}
}

// F2P-024: full token response deserialization.
func TestF2P0024_FullTokenDeserialization(t *testing.T) {
	f2pCheckRequiredResponseFields(t)
	f2pCheckFlattenedExtraFields(t)
	client := f2pNewClient(t)
	body := `{"access_token":"12/34","token_type":"bearer","scope":"read write","expires_in":3600,"refresh_token":"foobar"}`
	rc := &f2pClient{t: t, resp: f2pJSONResp(200, body)}
	req, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	token, tokenErr := req.Request(rc)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if token.AccessToken().Secret() != "12/34" {
		t.Fatalf("access token: got %q", token.AccessToken().Secret())
	}
	if expires, ok := token.ExpiresIn(); !ok || expires != 3600 {
		t.Fatalf("expires_in: got %v %v", expires, ok)
	}
	if token.RefreshToken() == nil || token.RefreshToken().Secret() != "foobar" {
		t.Fatalf("refresh token: %#v", token.RefreshToken())
	}
	if len(token.Scopes()) != 2 || token.Scopes()[0].AsStr() != "read" || token.Scopes()[1].AsStr() != "write" {
		t.Fatalf("scopes: got %#v", token.Scopes())
	}
}

type f2pCollectedExtraFields map[string]json.RawMessage

func (f2pCollectedExtraFields) IsExtraTokenFields()               {}
func (f2pCollectedExtraFields) IsExtraDeviceAuthorizationFields() {}

// The Rust responses flatten EF: known protocol members are consumed by the
// standard response, and only the remaining members belong to ExtraFields.
// A map observes this boundary without imposing any private representation.
func f2pCheckFlattenedExtraFields(t *testing.T) {
	t.Helper()
	want := f2pCollectedExtraFields{"tenant": json.RawMessage(`"space"`), "quota": json.RawMessage(`7`)}
	t.Run("token_extra_fields", func(t *testing.T) {
		var response o.StandardTokenResponse[f2pCollectedExtraFields, o.BasicTokenType]
		body := `{"access_token":"at","token_type":"bearer","expires_in":60,"refresh_token":"rt","scope":"read write","tenant":"space","quota":7}`
		if err := json.Unmarshal([]byte(body), &response); err != nil {
			t.Fatal(err)
		}
		if got := response.ExtraFields(); !reflect.DeepEqual(got, want) {
			t.Fatalf("token extra fields: got %s, want only tenant and quota", f2pExtraFieldsJSON(got))
		}
	})
	t.Run("introspection_extra_fields", func(t *testing.T) {
		var response o.StandardTokenIntrospectionResponse[f2pCollectedExtraFields, o.BasicTokenType]
		body := `{"active":true,"scope":"read write","client_id":"id","username":"user","token_type":"bearer","exp":60,"iat":1,"nbf":2,"sub":"subject","aud":["api"],"iss":"issuer","jti":"jwt-id","tenant":"space","quota":7}`
		if err := json.Unmarshal([]byte(body), &response); err != nil {
			t.Fatal(err)
		}
		if got := response.ExtraFields(); !reflect.DeepEqual(got, want) {
			t.Fatalf("introspection extra fields: got %s, want only tenant and quota", f2pExtraFieldsJSON(got))
		}
	})
	t.Run("device_extra_fields", func(t *testing.T) {
		var response o.DeviceAuthorizationResponse[f2pCollectedExtraFields]
		body := `{"device_code":"dc","user_code":"uc","verification_uri":"https://example.com/verify","verification_uri_complete":"https://example.com/verify?code=uc","expires_in":60,"interval":5,"tenant":"space","quota":7}`
		if err := json.Unmarshal([]byte(body), &response); err != nil {
			t.Fatal(err)
		}
		if got := response.ExtraFields(); !reflect.DeepEqual(got, want) {
			t.Fatalf("device extra fields: got %s, want only tenant and quota", f2pExtraFieldsJSON(got))
		}
	})
}

func f2pExtraFieldsJSON(fields f2pCollectedExtraFields) []byte {
	data, _ := json.Marshal(fields)
	return data
}

// The contract exposes scope builders on every token request, including code
// exchange. A runtime interface assertion keeps a missing method from turning
// the entire evaluator suite into a compilation failure.
func f2pCheckCodeScopes(t *testing.T) {
	t.Helper()
	request, err := f2pNewClient(t).ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	type scopeBuilder interface {
		AddScope(o.Scope) *o.CodeTokenRequest[o.BasicErrorResponse, o.BasicTokenResponse]
		AddScopes([]o.Scope) *o.CodeTokenRequest[o.BasicErrorResponse, o.BasicTokenResponse]
	}
	builder, ok := any(request).(scopeBuilder)
	if !ok {
		t.Fatal("CodeTokenRequest must expose AddScope and AddScopes")
	}
	builder.AddScope(o.NewScope("first"))
	builder.AddScopes([]o.Scope{o.NewScope("second"), o.NewScope("third")})
	caller := &f2pClient{t: t, method: "POST", header: f2pBasicHeaders(),
		body: "grant_type=authorization_code&code=ccc&scope=first+second+third",
		resp: f2pJSONResp(200, `{"access_token":"t","token_type":"bearer"}`)}
	if _, failure := request.Request(caller); failure != nil {
		t.Fatal(failure)
	}
	if _, failure := request.RequestAsync(context.Background(), o.AsyncHTTPClientFunc(func(_ context.Context, req o.HTTPRequest) (o.HTTPResponse, error) {
		return caller.Call(req)
	})); failure != nil {
		t.Fatal(failure)
	}
	if caller.calls != 2 {
		t.Fatalf("sync and async code requests must call the injected client: %d", caller.calls)
	}
}

func f2pCheckMediaTypes(t *testing.T) {
	t.Helper()
	client := f2pNewClient(t)
	intro, err := o.NewIntrospectionUrl("https://example.com/introspect")
	if err != nil { t.Fatal(err) }
	client.SetIntrospectionURL(intro)
	for _, tc := range []struct {
		key, value string
		valid bool
	}{
		{"Content-Type", "application/jsonp", false},
		{"content-type", "application/json+xml", false},
		{"CONTENT-TYPE", "text/plain", false},
		{"Content-Type", "application/json garbage", false},
		{"content-type", "Application/JSON ; charset=UTF-8", true},
		{"Content-Type", "application/json; charset=utf-8", true},
	} {
		for _, status := range []int{200, 400, 500} {
			for _, introspection := range []bool{false, true} {
				for _, async := range []bool{false, true} {
					body := `{"access_token":"t","token_type":"bearer"}`
					if introspection { body = `{"active":true}` }
					if status != 200 { body = `{"error":"invalid_request"}` }
					response := f2pResp(status, map[string]string{tc.key: tc.value}, body)
					syncClient := o.SyncHTTPClientFunc(func(o.HTTPRequest) (o.HTTPResponse, error) { return response, nil })
					asyncClient := o.AsyncHTTPClientFunc(func(context.Context, o.HTTPRequest) (o.HTTPResponse, error) { return response, nil })
					var failure *o.RequestTokenError
					if introspection {
						token := o.NewAccessToken("t")
						request, configErr := client.Introspect(&token)
						if configErr != nil { t.Fatal(configErr) }
						if async { _, failure = request.RequestAsync(context.Background(), asyncClient) } else { _, failure = request.Request(syncClient) }
					} else {
						request, configErr := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
						if configErr != nil { t.Fatal(configErr) }
						if async { _, failure = request.RequestAsync(context.Background(), asyncClient) } else { _, failure = request.Request(syncClient) }
					}
					if !tc.valid {
						if failure == nil || failure.Kind != o.RequestTokenErrorOther || !strings.Contains(failure.Error(), tc.value) {
							t.Fatalf("status %d: invalid media type %q must produce an other error naming it: %#v", status, tc.value, failure)
						}
					} else if status == 200 {
						if failure != nil { t.Fatalf("valid media type %q rejected: %v", tc.value, failure) }
					} else if failure == nil || failure.Kind != o.RequestTokenErrorServerResponse {
						t.Fatalf("status %d: JSON error response must be parsed: %#v", status, failure)
					}
				}
			}
		}
	}
}

// Successful revocation ignores all body bytes and Content-Type. Failures use
// the same error path as the other endpoints, for sync and async clients.
func f2pCheckRevocationResponses(t *testing.T) {
	t.Helper()
	client := f2pNewClient(t).SetRevocationURL(mustRevocationURL(t, "https://example.com/revoke"))
	for _, status := range []int{200, 400} {
		for _, contentType := range []string{"application/json", "application/octet-stream"} {
			for _, body := range []string{"", string([]byte{1, 2, 3}), `{"error":"unsupported_token_type"}`} {
				for _, async := range []bool{false, true} {
					response := f2pResp(status, map[string]string{"content-type": contentType}, body)
					request, configErr := client.RevokeToken(o.NewStandardRevocableTokenFromAccessToken(o.NewAccessToken("token")))
					if configErr != nil { t.Fatal(configErr) }
					var failure *o.RequestTokenError
					if async {
						failure = request.RequestAsync(context.Background(), o.AsyncHTTPClientFunc(func(context.Context, o.HTTPRequest) (o.HTTPResponse, error) { return response, nil }))
					} else {
						failure = request.Request(o.SyncHTTPClientFunc(func(o.HTTPRequest) (o.HTTPResponse, error) { return response, nil }))
					}
					if status == 200 {
						if failure != nil { t.Fatalf("revocation 200 must ignore body and media type: %#v", failure) }
						continue
					}
					if failure == nil { t.Fatal("revocation non-200 must return an error") }
					if contentType != "application/json" {
						if failure.Kind != o.RequestTokenErrorOther || !strings.Contains(failure.Error(), contentType) { t.Fatalf("unexpected media type must be reported: %#v", failure) }
					} else if body == "" {
						if failure.Kind != o.RequestTokenErrorOther { t.Fatalf("empty error body: %#v", failure) }
					} else if !json.Valid([]byte(body)) {
						if failure.Kind != o.RequestTokenErrorParse || string(failure.RawBody) != body { t.Fatalf("parse error must carry raw body: %#v", failure) }
					} else if failure.Kind != o.RequestTokenErrorServerResponse { t.Fatalf("JSON server error: %#v", failure) }
				}
			}
		}
	}
}

func f2pCheckDirectTokenTypes(t *testing.T) {
	t.Helper()
	for _, tc := range []struct { raw string; want o.BasicTokenType }{
		{`"BEARER"`, o.BasicTokenTypeBearer}, {`"Bearer"`, o.BasicTokenTypeBearer},
		{`"MAC"`, o.BasicTokenTypeMac}, {`"mAc"`, o.BasicTokenTypeMac},
	} {
		var value o.BasicTokenType
		if err := json.Unmarshal([]byte(tc.raw), &value); err != nil { t.Fatal(err) }
		if value != tc.want { t.Fatalf("direct token type decode %s: got %q, want %q", tc.raw, value, tc.want) }
		body, err := json.Marshal(value)
		if err != nil { t.Fatal(err) }
		if string(body) != `"`+tc.want.TokenTypeString()+`"` { t.Fatalf("canonical token type: %s", body) }
	}
}

type f2pCanonicalToken struct{ Value string }
func (v f2pCanonicalToken) TokenTypeString() string { return strings.ToLower(v.Value) }
func (v *f2pCanonicalToken) UnmarshalJSON(data []byte) error { return json.Unmarshal(data, &v.Value) }
type f2pCanonicalError struct{ Value string }
func (v f2pCanonicalError) ErrorTypeString() string { return v.Value }

// Extension interfaces specify canonical strings, independent of the custom
// value's default JSON encoding (which, for these structs, is an object).
func f2pCheckCanonicalEnums(t *testing.T) {
	t.Helper()
	token := o.NewStandardTokenResponse(o.NewAccessToken("at"), f2pCanonicalToken{"BEARER"}, o.EmptyExtraTokenFields{})
	body, err := json.Marshal(token)
	if err != nil { t.Fatal(err) }
	if !strings.Contains(string(body), `"token_type":"bearer"`) { t.Fatalf("custom token type must serialize its canonical string: %s", body) }
	var introspection o.StandardTokenIntrospectionResponse[o.EmptyExtraTokenFields, f2pCanonicalToken]
	if err := json.Unmarshal([]byte(`{"active":true,"token_type":"BEARER"}`), &introspection); err != nil { t.Fatal(err) }
	body, err = json.Marshal(introspection)
	if err != nil { t.Fatal(err) }
	if !strings.Contains(string(body), `"token_type":"bearer"`) { t.Fatalf("custom introspection token type must serialize its canonical string: %s", body) }
	body, err = json.Marshal(o.NewStandardErrorResponse(f2pCanonicalError{"invalid_grant"}, nil, nil))
	if err != nil { t.Fatal(err) }
	if string(body) != `{"error":"invalid_grant"}` { t.Fatalf("custom error type must serialize its canonical string: %s", body) }
}

func f2pCheckRequiredResponseFields(t *testing.T) {
	t.Helper()
	for _, tc := range []struct {
		kind, body string
	}{
		{"token", `{}`},
		{"token", `{"token_type":"bearer"}`},
		{"token", `{"access_token":"t"}`},
		{"token", `{"access_token":null,"token_type":"bearer"}`},
		{"token", `{"access_token":"t","token_type":null}`},
		{"introspection", `{}`},
		{"introspection", `{"active":null}`},
		{"device", `{"user_code":"u","verification_uri":"https://example.com/verify","expires_in":60}`},
		{"device", `{"device_code":"d","verification_uri":"https://example.com/verify","expires_in":60}`},
		{"device", `{"device_code":"d","user_code":"u","verification_uri":"https://example.com/verify"}`},
	} {
		caller := &f2pClient{t: t, resp: f2pJSONResp(200, tc.body)}
		client := f2pNewClient(t)
		var failure *o.RequestTokenError
		switch tc.kind {
		case "token":
			request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
			if err != nil { t.Fatal(err) }
			_, failure = request.Request(caller)
		case "introspection":
			endpoint, err := o.NewIntrospectionUrl("https://example.com/introspect")
			if err != nil { t.Fatal(err) }
			token := o.NewAccessToken("t")
			request, err := client.SetIntrospectionURL(endpoint).Introspect(&token)
			if err != nil { t.Fatal(err) }
			_, failure = request.Request(caller)
		case "device":
			endpoint, err := o.NewDeviceAuthorizationUrl("https://example.com/device")
			if err != nil { t.Fatal(err) }
			request, err := client.SetDeviceAuthorizationURL(endpoint).ExchangeDeviceCode()
			if err != nil { t.Fatal(err) }
			_, failure = request.Request(caller)
		}
		if failure == nil || failure.Kind != o.RequestTokenErrorParse || string(failure.RawBody) != tc.body {
			t.Fatalf("invalid %s response must produce a parse error carrying the body %s: %#v", tc.kind, tc.body, failure)
		}
	}
}

func f2pFindUp(t *testing.T, name string) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		candidate := filepath.Join(dir, name)
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	t.Fatalf("could not locate %s above the working directory", name)
	return ""
}

func f2pImports(t *testing.T, includeTests bool) []string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	var imports []string
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") {
			continue
		}
		if !includeTests && strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, filepath.Join(dir, name), nil, parser.ImportsOnly)
		if err != nil {
			t.Fatalf("parse %s: %v", name, err)
		}
		for _, spec := range file.Imports {
			imports = append(imports, strings.Trim(spec.Path.Value, `"`))
		}
	}
	return imports
}

// Optional introspection fields preserve the difference between absence/null
// and a present token type.
func f2pCheckOptionalTokenType(t *testing.T) {
	t.Helper()
	for _, body := range []string{`{"active":true}`, `{"active":true,"token_type":null}`} {
		var response o.BasicTokenIntrospectionResponse
		if err := json.Unmarshal([]byte(body), &response); err != nil { t.Fatalf("optional token_type: %v", err) }
		if tokenType, present := response.TokenType(); present || tokenType != nil { t.Fatalf("absent/null token_type must be absent: %v %v", tokenType, present) }
	}
}
