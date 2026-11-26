package oauth2_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	. "oauth2/oauth2"
)

// This probe is the target-side half of the BATCH-DIFFERENTIAL-0001 shared case
// corpus. Each TestDIFFnnn corresponds to observation DIFF-nnn and asserts the
// normalized observable result that the Rust reference and the migrated Go
// package must agree on. It uses only the exported contract of the migrated
// package, through an injected HTTP client.

// DIFF-001: a consumer can assemble a client and execute a flow request through
// an injected fake HTTP client, receiving the documented response type.
func TestDIFF001(t *testing.T) {
	client := migNewTestClient(t)
	request, err := client.ExchangeClientCredentials()
	if err != nil {
		t.Fatal(err)
	}
	caller := &migMockHTTPClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: migBasicAuthHeaders(),
		wantBody:    "grant_type=client_credentials",
		response:    migNewHTTPResponse(200, migJsonHeader(), `{"access_token":"at","token_type":"bearer"}`),
	}
	response, tokenErr := request.Request(caller)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if response.AccessToken().Secret() != "at" {
		t.Fatalf("access token: got %q", response.AccessToken().Secret())
	}
	if tt, ok := response.TokenTypeValue().(BasicTokenType); !ok || tt != BasicTokenTypeBearer {
		t.Fatalf("token type: got %#v", response.TokenTypeValue())
	}
}

// DIFF-002: construction defaults to BasicAuth and every setter stores its value
// for the matching accessor.
func TestDIFF002(t *testing.T) {
	client := NewBasicClient(NewClientId("id"))
	if client.AuthType() != BasicAuth {
		t.Fatalf("default auth type: got %v", client.AuthType())
	}
	client = client.
		SetAuthURL(migMustAuthURL(t, "https://example.com/auth")).
		SetTokenURL(migMustTokenURL(t, "https://example.com/token")).
		SetRedirectURL(migMustRedirectURL(t, "https://example.com/redirect")).
		SetDeviceAuthorizationURL(migMustDeviceAuthURL(t, "https://example.com/device")).
		SetIntrospectionURL(migMustIntrospectionURL(t, "https://example.com/introspect")).
		SetRevocationURL(migMustRevocationURL(t, "https://example.com/revoke")).
		SetClientSecret(NewClientSecret("secret"))
	if client.AuthURI().String() != "https://example.com/auth" ||
		client.TokenURI().String() != "https://example.com/token" ||
		client.RedirectURI().String() != "https://example.com/redirect" ||
		client.DeviceAuthorizationURL().String() != "https://example.com/device" ||
		client.IntrospectionURL().String() != "https://example.com/introspect" ||
		client.RevocationURL().String() != "https://example.com/revoke" {
		t.Fatal("one or more endpoint accessors did not round-trip the set value")
	}
	if _, err := client.ExchangeCode(NewAuthorizationCode("c")); err != nil {
		t.Fatalf("flow accessor should be fallible and succeed when configured: %v", err)
	}
}

// DIFF-003: every flow request is a form-encoded POST that accepts JSON.
func TestDIFF003(t *testing.T) {
	client := migNewTestClient(t)
	request, err := client.ExchangeClientCredentials()
	if err != nil {
		t.Fatal(err)
	}
	req := migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := request.Request(httpClient); return failure })
	if req.Method != "POST" {
		t.Fatalf("method: got %q", req.Method)
	}
	if req.Header["Content-Type"] != "application/x-www-form-urlencoded" {
		t.Fatalf("content-type: got %q", req.Header["Content-Type"])
	}
	if req.Header["Accept"] != "application/json" {
		t.Fatalf("accept: got %q", req.Header["Accept"])
	}
}

// DIFF-004: BasicAuth url-encodes the id and secret separately before base64.
func TestDIFF004(t *testing.T) {
	authURL := migMustAuthURL(t, "https://example.com/auth")
	tokenURL := migMustTokenURL(t, "https://example.com/token")
	basic := NewBasicClient(NewClientId("a b")).SetClientSecret(NewClientSecret("c:d")).
		SetAuthURL(authURL).SetTokenURL(tokenURL)
	request, err := basic.ExchangeClientCredentials()
	if err != nil {
		t.Fatal(err)
	}
	req := migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := request.Request(httpClient); return failure })
	if got, want := req.Header["Authorization"], "Basic "+base64.StdEncoding.EncodeToString([]byte("a+b:c%3Ad")); got != want {
		t.Fatalf("authorization: got %q, want %q", got, want)
	}
	for _, kv := range []string{"client_id=", "client_secret="} {
		if strings.Contains(string(req.Body), kv) {
			t.Fatalf("body must not contain %q when BasicAuth with secret is used: %q", kv, string(req.Body))
		}
	}

	body := NewBasicClient(NewClientId("a b")).SetClientSecret(NewClientSecret("c:d")).
		SetAuthType(RequestBody).SetAuthURL(authURL).SetTokenURL(tokenURL)
	breq, err := body.ExchangeClientCredentials()
	if err != nil {
		t.Fatal(err)
	}
	got := string(migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := breq.Request(httpClient); return failure }).Body)
	if !strings.Contains(got, "client_id=a+b") || !strings.Contains(got, "client_secret=c%3Ad") {
		t.Fatalf("RequestBody auth body: got %q", got)
	}
}

// DIFF-005: the authorization URL parameter set.
func TestDIFF005(t *testing.T) {
	client := migNewTestClient(t)
	request, err := client.AuthorizeURL(func() CsrfToken { return NewCsrfToken("csrf_token") })
	if err != nil {
		t.Fatal(err)
	}
	got, _ := request.AddScope(NewScope("a b")).AddExtraParam("extra", "x").URL()
	want := "https://example.com/auth?response_type=code&client_id=aaa&state=csrf_token&scope=a+b&extra=x"
	if got.String() != want {
		t.Fatalf("auth url: got %q, want %q", got.String(), want)
	}
	implicit, err := client.AuthorizeURL(func() CsrfToken { return NewCsrfToken("s") })
	if err != nil {
		t.Fatal(err)
	}
	iu, _ := implicit.UseImplicitFlow().URL()
	if iu.String() != "https://example.com/auth?response_type=token&client_id=aaa&state=s" {
		t.Fatalf("implicit url: got %q", iu.String())
	}
}

// DIFF-006: revocation request parameters and non-HTTPS rejection.
func TestDIFF006(t *testing.T) {
	client := migNewTestClient(t).SetRevocationURL(migMustRevocationURL(t, "https://revocation/url"))
	request, err := client.RevokeToken(NewStandardRevocableTokenFromAccessToken(NewAccessToken("access_token_123")))
	if err != nil {
		t.Fatal(err)
	}
	req := migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { return request.Request(httpClient) })
	if string(req.Body) != "token=access_token_123&token_type_hint=access_token" {
		t.Fatalf("revocation body: got %q", string(req.Body))
	}
	insecure := migNewTestClient(t).SetRevocationURL(migMustRevocationURL(t, "http://revocation/url"))
	if _, err := insecure.RevokeToken(NewStandardRevocableTokenFromAccessToken(NewAccessToken("t"))); err == nil {
		t.Fatal("expected insecure-URL configuration error")
	}
}

// DIFF-007: AddExtraParam appends to every request type.
func TestDIFF007(t *testing.T) {
	client := migNewTestClient(t).
		SetRedirectURL(migMustRedirectURL(t, "https://example.com/redirect")).
		SetDeviceAuthorizationURL(migMustDeviceAuthURL(t, "https://example.com/device")).
		SetIntrospectionURL(migMustIntrospectionURL(t, "https://example.com/introspect")).
		SetRevocationURL(migMustRevocationURL(t, "https://example.com/revoke"))

	authReq, _ := client.AuthorizeURL(func() CsrfToken { return NewCsrfToken("s") })
	if got, _ := authReq.AddExtraParam("x", "1").URL(); !strings.Contains(got.RawQuery, "x=1") {
		t.Fatalf("authorization extra param missing: %q", got.RawQuery)
	}
	code, _ := client.ExchangeCode(NewAuthorizationCode("c"))
	if !strings.Contains(string(migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := code.AddExtraParam("x", "1").Request(httpClient); return failure }).Body), "x=1") {
		t.Fatal("code extra param missing")
	}
	cc, _ := client.ExchangeClientCredentials()
	if !strings.Contains(string(migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := cc.AddExtraParam("x", "1").Request(httpClient); return failure }).Body), "x=1") {
		t.Fatal("client-credentials extra param missing")
	}
	user := NewResourceOwnerUsername("u")
	pass := NewResourceOwnerPassword("p")
	pw, _ := client.ExchangePassword(&user, &pass)
	if !strings.Contains(string(migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := pw.AddExtraParam("x", "1").Request(httpClient); return failure }).Body), "x=1") {
		t.Fatal("password extra param missing")
	}
	rt := NewRefreshToken("r")
	refresh, _ := client.ExchangeRefreshToken(&rt)
	if !strings.Contains(string(migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := refresh.AddExtraParam("x", "1").Request(httpClient); return failure }).Body), "x=1") {
		t.Fatal("refresh extra param missing")
	}
	device, _ := client.ExchangeDeviceCode()
	if !strings.Contains(string(migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := device.AddExtraParam("x", "1").Request(httpClient); return failure }).Body), "x=1") {
		t.Fatal("device authorization extra param missing")
	}
	deviceResp := migDeviceResponse(t, 1000)
	deviceAccess, _ := client.ExchangeDeviceAccessToken(deviceResp)
	if !strings.Contains(string(migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := deviceAccess.AddExtraParam("x", "1").Request(httpClient, func(time.Duration) {}, nil); return failure }).Body), "x=1") {
		t.Fatal("device access token extra param missing")
	}
	introspectToken := NewAccessToken("t")
	introspect, _ := client.Introspect(&introspectToken)
	if !strings.Contains(string(migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := introspect.AddExtraParam("x", "1").Request(httpClient); return failure }).Body), "x=1") {
		t.Fatal("introspection extra param missing")
	}
	revoke, _ := client.RevokeToken(NewStandardRevocableTokenFromAccessToken(NewAccessToken("t")))
	if !strings.Contains(string(migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { return revoke.AddExtraParam("x", "1").Request(httpClient) }).Body), "x=1") {
		t.Fatal("revocation extra param missing")
	}
}

// DIFF-008: synchronous and asynchronous clients are injectable and invoked.
func TestDIFF008(t *testing.T) {
	client := migNewTestClient(t)
	request, err := client.ExchangeClientCredentials()
	if err != nil {
		t.Fatal(err)
	}
	syncCalls := 0
	syncClient := SyncHTTPClientFunc(func(HTTPRequest) (HTTPResponse, error) {
		syncCalls++
		return migNewHTTPResponse(200, migJsonHeader(), `{"access_token":"at","token_type":"bearer"}`), nil
	})
	if _, tokenErr := request.Request(syncClient); tokenErr != nil {
		t.Fatal(tokenErr)
	}
	if syncCalls != 1 {
		t.Fatalf("sync injected client calls: got %d", syncCalls)
	}

	asyncCalls := 0
	asyncClient := AsyncHTTPClientFunc(func(context.Context, HTTPRequest) (HTTPResponse, error) {
		asyncCalls++
		return migNewHTTPResponse(200, migJsonHeader(), `{"access_token":"at","token_type":"bearer"}`), nil
	})
	if _, tokenErr := request.RequestAsync(context.Background(), asyncClient); tokenErr != nil {
		t.Fatal(tokenErr)
	}
	if asyncCalls != 1 {
		t.Fatalf("async injected client calls: got %d", asyncCalls)
	}
}

// DIFF-009: secret-bearing newtypes redact their content in formatted output.
func TestDIFF009(t *testing.T) {
	// The contract requires formatted redaction, which fmt.Formatter can
	// provide without implementing fmt.Stringer.
	secrets := []any{
		NewClientSecret("s3cr3t"),
		NewAccessToken("s3cr3t"),
		NewRefreshToken("s3cr3t"),
		NewAuthorizationCode("s3cr3t"),
		NewPkceCodeVerifier("s3cr3t"),
		NewCsrfToken("s3cr3t"),
		NewDeviceCode("s3cr3t"),
		NewUserCode("s3cr3t"),
		NewVerificationUriComplete("s3cr3t"),
	}
	for _, s := range secrets {
		for _, format := range []string{"%s", "%v", "%#v"} {
			got := fmt.Sprintf(format, s)
			if strings.Contains(got, "s3cr3t") {
				t.Fatalf("format %q leaked secret: %q", format, got)
			}
		}
	}
	if NewClientSecret("s3cr3t").Secret() != "s3cr3t" {
		t.Fatal("Secret() must expose the underlying value")
	}
}

// DIFF-010: standard error responses and enum canonical serialization.
func TestDIFF010(t *testing.T) {
	desc := "bad"
	uri := "u"
	resp := NewStandardErrorResponse(BasicErrorResponseTypeInvalidGrant, &desc, &uri)
	raw, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != `{"error":"invalid_grant","error_description":"bad","error_uri":"u"}` {
		t.Fatalf("standard error response: got %s", raw)
	}
	raw, _ = json.Marshal(NewStandardErrorResponse(BasicErrorResponseTypeInvalidClient, nil, nil))
	if string(raw) != `{"error":"invalid_client"}` {
		t.Fatalf("minimal error response: got %s", raw)
	}
	if BasicTokenTypeBearer.TokenTypeString() != "bearer" || BasicErrorResponseTypeInvalidGrant.ErrorTypeString() != "invalid_grant" {
		t.Fatal("canonical enum serialization mismatch")
	}
}

// DIFF-011: behavioral parity with the reference fake-client scenarios for a
// representative request/response exchange.
func TestDIFF011(t *testing.T) {
	client := migNewTestClient(t)
	request, err := client.ExchangeCode(NewAuthorizationCode("code_123"))
	if err != nil {
		t.Fatal(err)
	}
	caller := &migMockHTTPClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: migBasicAuthHeaders(),
		wantBody:    "grant_type=authorization_code&code=code_123",
		response:    migNewHTTPResponse(200, migJsonHeader(), `{"access_token":"at","token_type":"bearer"}`),
	}
	if _, tokenErr := request.Request(caller); tokenErr != nil {
		t.Fatal(tokenErr)
	}
}

// DIFF-012: every flow accessor fails with the missing-URL configuration error.
func TestDIFF012(t *testing.T) {
	client := NewBasicClient(NewClientId("id"))
	user := NewResourceOwnerUsername("u")
	pass := NewResourceOwnerPassword("p")
	rt := NewRefreshToken("r")
	at := NewAccessToken("t")
	_, authErr := client.AuthorizeURL(func() CsrfToken { return NewCsrfToken("s") })
	_, codeErr := client.ExchangeCode(NewAuthorizationCode("c"))
	_, credErr := client.ExchangeClientCredentials()
	_, pwErr := client.ExchangePassword(&user, &pass)
	_, refreshErr := client.ExchangeRefreshToken(&rt)
	_, deviceErr := client.ExchangeDeviceCode()
	_, introspectErr := client.Introspect(&at)
	_, revokeErr := client.RevokeToken(NewStandardRevocableTokenFromAccessToken(at))
	checks := []struct {
		name string
		err  error
	}{
		{"authorization", authErr},
		{"token", codeErr},
		{"token", credErr},
		{"token", pwErr},
		{"token", refreshErr},
		{"device authorization", deviceErr},
		{"introspection", introspectErr},
		{"revocation", revokeErr},
	}
	for _, c := range checks {
		configErr, ok := c.err.(*ConfigurationError)
		if !ok {
			t.Fatalf("%s: expected *ConfigurationError, got %T", c.name, c.err)
		}
		if configErr.Kind != ConfigurationMissingURL || configErr.Endpoint != c.name {
			t.Fatalf("%s: got kind=%v endpoint=%q", c.name, configErr.Kind, configErr.Endpoint)
		}
	}
}

// DIFF-013: successful token and introspection response deserialization.
func TestDIFF013(t *testing.T) {
	var token BasicTokenResponse
	if err := json.Unmarshal([]byte(`{"access_token":"at","token_type":"bearer","expires_in":3600,"refresh_token":"rt","scope":"a b c"}`), &token); err != nil {
		t.Fatal(err)
	}
	if sec, ok := token.ExpiresIn(); !ok || sec != 3600 {
		t.Fatalf("expires_in: got %d, %v", sec, ok)
	}
	if token.RefreshToken() == nil || token.RefreshToken().Secret() != "rt" {
		t.Fatal("refresh token not populated")
	}
	if got := len(token.Scopes()); got != 3 {
		t.Fatalf("scopes: got %d", got)
	}
	var introspection BasicTokenIntrospectionResponse
	if err := json.Unmarshal([]byte(`{"active":true,"client_id":"cid","scope":"a b"}`), &introspection); err != nil {
		t.Fatal(err)
	}
	if !introspection.Active() || introspection.ClientId() == nil || introspection.ClientId().String() != "cid" {
		t.Fatal("introspection not populated")
	}
}

// DIFF-014: token request error mapping for non-200 responses.
func TestDIFF014(t *testing.T) {
	client := migNewTestClient(t)
	request, _ := client.ExchangeClientCredentials()

	server := &migMockHTTPClient{t: t, wantMethod: "POST", wantURL: "https://example.com/token", wantHeaders: migBasicAuthHeaders(), wantBody: "grant_type=client_credentials",
		response: migNewHTTPResponse(400, migJsonHeader(), `{"error":"invalid_grant"}`)}
	if _, tokenErr := request.Request(server); tokenErr == nil || tokenErr.Kind != RequestTokenErrorServerResponse {
		t.Fatalf("parseable: got %#v", tokenErr)
	}

	parse := &migMockHTTPClient{t: t, wantMethod: "POST", wantURL: "https://example.com/token", wantHeaders: migBasicAuthHeaders(), wantBody: "grant_type=client_credentials",
		response: migNewHTTPResponse(400, migJsonHeader(), `not json`)}
	if _, tokenErr := request.Request(parse); tokenErr == nil || tokenErr.Kind != RequestTokenErrorParse || string(tokenErr.RawBody) != "not json" {
		t.Fatalf("unparseable: got %#v", tokenErr)
	}

	empty := &migMockHTTPClient{t: t, wantMethod: "POST", wantURL: "https://example.com/token", wantHeaders: migBasicAuthHeaders(), wantBody: "grant_type=client_credentials",
		response: migNewHTTPResponse(500, nil, "")}
	if _, tokenErr := request.Request(empty); tokenErr == nil || tokenErr.Kind != RequestTokenErrorOther {
		t.Fatalf("empty: got %#v", tokenErr)
	}
}

// DIFF-015: Content-Type and empty-body validation.
func TestDIFF015(t *testing.T) {
	client := migNewTestClient(t)
	request, _ := client.ExchangeClientCredentials()
	wrongType := &migMockHTTPClient{t: t, wantMethod: "POST", wantURL: "https://example.com/token", wantHeaders: migBasicAuthHeaders(), wantBody: "grant_type=client_credentials",
		response: migNewHTTPResponse(200, map[string]string{"Content-Type": "text/plain"}, `x`)}
	if _, tokenErr := request.Request(wrongType); tokenErr == nil || tokenErr.Kind != RequestTokenErrorOther || !strings.Contains(tokenErr.Error(), "text/plain") {
		t.Fatalf("content-type: got %#v", tokenErr)
	}
	caseless := &migMockHTTPClient{t: t, wantMethod: "POST", wantURL: "https://example.com/token", wantHeaders: migBasicAuthHeaders(), wantBody: "grant_type=client_credentials",
		response: migNewHTTPResponse(200, map[string]string{"Content-Type": "Application/JSON; charset=utf-8"}, `{"access_token":"at","token_type":"bearer"}`)}
	if _, tokenErr := request.Request(caseless); tokenErr != nil {
		t.Fatalf("case-insensitive content-type must be accepted: %v", tokenErr)
	}
	emptyBody := &migMockHTTPClient{t: t, wantMethod: "POST", wantURL: "https://example.com/token", wantHeaders: migBasicAuthHeaders(), wantBody: "grant_type=client_credentials",
		response: migNewHTTPResponse(200, migJsonHeader(), "")}
	if _, tokenErr := request.Request(emptyBody); tokenErr == nil || tokenErr.Kind != RequestTokenErrorOther {
		t.Fatalf("empty body: got %#v", tokenErr)
	}
}

// DIFF-016: revocation inspects only the status code.
func TestDIFF016(t *testing.T) {
	client := migNewTestClient(t).SetRevocationURL(migMustRevocationURL(t, "https://revocation/url"))
	request, _ := client.RevokeToken(NewStandardRevocableTokenFromAccessToken(NewAccessToken("at")))
	ok := &migMockHTTPClient{t: t, wantMethod: "POST", wantURL: "https://revocation/url", wantHeaders: migBasicAuthHeaders(), wantBody: "token=at&token_type_hint=access_token",
		response: migNewHTTPResponse(200, map[string]string{"Content-Type": "application/octet-stream"}, string([]byte{1, 2, 3}))}
	if tokenErr := request.Request(ok); tokenErr != nil {
		t.Fatalf("200 with unparseable body must be ignored: %v", tokenErr)
	}
	bad := &migMockHTTPClient{t: t, wantMethod: "POST", wantURL: "https://revocation/url", wantHeaders: migBasicAuthHeaders(), wantBody: "token=at&token_type_hint=access_token",
		response: migNewHTTPResponse(400, migJsonHeader(), `{"error":"unsupported_token_type"}`)}
	if tokenErr := request.Request(bad); tokenErr == nil || tokenErr.Kind != RequestTokenErrorServerResponse {
		t.Fatalf("non-200: got %#v", tokenErr)
	}
}

// DIFF-017: insecure revocation URL is rejected before any request is sent.
func TestDIFF017(t *testing.T) {
	client := migNewTestClient(t).SetRevocationURL(migMustRevocationURL(t, "http://revocation/url"))
	if _, err := client.RevokeToken(NewStandardRevocableTokenFromAccessToken(NewAccessToken("at"))); err == nil {
		t.Fatal("expected insecure-URL error")
	}
}

// DIFF-018: device access token polling intervals.
func TestDIFF018(t *testing.T) {
	migCheckDeadlineBoundary(t)
	client := migNewTestClient(t)
	deviceResponse := migDeviceResponse(t, 1000)
	request, err := client.ExchangeDeviceAccessToken(deviceResponse)
	if err != nil {
		t.Fatal(err)
	}
	var sleeps []time.Duration
	now := time.Now()
	request.SetTimeFunc(func() time.Time { return now })
	polls := 0
	caller := SyncHTTPClientFunc(func(HTTPRequest) (HTTPResponse, error) {
		polls++
		switch polls {
		case 1:
			return migNewHTTPResponse(400, migJsonHeader(), `{"error":"authorization_pending"}`), nil
		case 2:
			return migNewHTTPResponse(400, migJsonHeader(), `{"error":"slow_down"}`), nil
		case 3:
			return HTTPResponse{}, errors.New("transport")
		default:
			return migNewHTTPResponse(200, migJsonHeader(), `{"access_token":"at","token_type":"bearer"}`), nil
		}
	})
	token, tokenErr := request.Request(caller, func(d time.Duration) { sleeps = append(sleeps, d) }, nil)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if token.AccessToken().Secret() != "at" {
		t.Fatal("access token not returned after polling")
	}
	want := []time.Duration{5 * time.Second, 10 * time.Second, 10 * time.Second}
	if !reflect.DeepEqual(sleeps, want) {
		t.Fatalf("sleep intervals: got %v, want %v", sleeps, want)
	}

	// Expiry returns an expired_token server response error.
	expiring := migDeviceResponse(t, 0)
	expReq, _ := client.ExchangeDeviceAccessToken(expiring)
	expBase := time.Now()
	expElapsed := time.Duration(0)
	expReq.SetTimeFunc(func() time.Time { return expBase.Add(expElapsed) })
	_, errResp := expReq.Request(
		SyncHTTPClientFunc(func(HTTPRequest) (HTTPResponse, error) {
			return migNewHTTPResponse(400, migJsonHeader(), `{"error":"authorization_pending"}`), nil
		}),
		func(d time.Duration) { expElapsed += d }, nil)
	if errResp == nil || errResp.Kind != RequestTokenErrorServerResponse {
		t.Fatalf("expiry: got %#v", errResp)
	}
	if resp, ok := errResp.Response.(DeviceCodeErrorResponse); !ok || resp.Error != DeviceCodeErrorResponseTypeExpiredToken {
		t.Fatalf("expiry response: got %#v", errResp.Response)
	}
}

// DIFF-019: token types parse case-insensitively and serialize canonically.
func TestDIFF019(t *testing.T) {
	for _, raw := range []string{`{"access_token":"a","token_type":"BEARER"}`, `{"access_token":"a","token_type":"bearer"}`} {
		var resp BasicTokenResponse
		if err := json.Unmarshal([]byte(raw), &resp); err != nil {
			t.Fatal(err)
		}
		tt, ok := resp.TokenTypeValue().(BasicTokenType)
		if !ok || tt != BasicTokenTypeBearer {
			t.Fatalf("token type for %s: got %#v", raw, resp.TokenTypeValue())
		}
	}
	raw, _ := json.Marshal(NewStandardTokenResponse(NewAccessToken("a"), BasicTokenTypeBearer, EmptyExtraTokenFields{}))
	if !strings.Contains(string(raw), `"token_type":"bearer"`) {
		t.Fatalf("canonical token type: got %s", raw)
	}
}

// DIFF-020: authorization URL parameter set, encoding and ordering is exact.
func TestDIFF020(t *testing.T) {
	authURL := migMustAuthURL(t, "https://auth.example/authorize")
	client := NewBasicClient(NewClientId("client")).SetAuthURL(authURL).
		SetClientSecret(NewClientSecret("s"))
	request, err := client.AuthorizeURL(func() CsrfToken { return NewCsrfToken("xyz") })
	if err != nil {
		t.Fatal(err)
	}
	got, _ := request.URL()
	if got.RawQuery != "response_type=code&client_id=client&state=xyz" {
		t.Fatalf("raw query: got %q", got.RawQuery)
	}
	withScope, _ := request.AddScope(NewScope("a b")).URL()
	if withScope.RawQuery != "response_type=code&client_id=client&state=xyz&scope=a+b" {
		t.Fatalf("raw query with scope: got %q", withScope.RawQuery)
	}
}

// DIFF-021: the migrated package retains the documented public capabilities.
func TestDIFF021(t *testing.T) {
	client := migNewTestClient(t).
		SetDeviceAuthorizationURL(migMustDeviceAuthURL(t, "https://example.com/device")).
		SetIntrospectionURL(migMustIntrospectionURL(t, "https://example.com/introspect")).
		SetRevocationURL(migMustRevocationURL(t, "https://example.com/revoke"))
	if _, err := client.ExchangeClientCredentials(); err != nil {
		t.Fatal(err)
	}
	if _, err := client.ExchangeCode(NewAuthorizationCode("c")); err != nil {
		t.Fatal(err)
	}
	rt := NewRefreshToken("r")
	if _, err := client.ExchangeRefreshToken(&rt); err != nil {
		t.Fatal(err)
	}
	u, p := NewResourceOwnerUsername("u"), NewResourceOwnerPassword("p")
	if _, err := client.ExchangePassword(&u, &p); err != nil {
		t.Fatal(err)
	}
	if _, err := client.ExchangeDeviceCode(); err != nil {
		t.Fatal(err)
	}
	at := NewAccessToken("t")
	if _, err := client.Introspect(&at); err != nil {
		t.Fatal(err)
	}
	if _, err := client.RevokeToken(NewStandardRevocableTokenFromAccessToken(at)); err != nil {
		t.Fatal(err)
	}
}

// DIFF-022: public configuration and error contract identity.
func TestDIFF022(t *testing.T) {
	if got := (&ConfigurationError{Kind: ConfigurationMissingURL, Endpoint: "token"}).Error(); got != "No token endpoint URL specified" {
		t.Fatalf("missing url message: got %q", got)
	}
	if got := (&ConfigurationError{Kind: ConfigurationInsecureURL, Endpoint: "revocation"}).Error(); got != "Scheme for revocation endpoint URL must be HTTPS" {
		t.Fatalf("insecure url message: got %q", got)
	}
	if BasicAuth != AuthType(0) || RequestBody != AuthType(1) {
		t.Fatal("auth type values changed")
	}
	var _ ErrorResponse = BasicErrorResponse{}
	var _ RevocableToken = StandardRevocableToken{}
	var _ TokenResponse = BasicTokenResponse{}
	var _ TokenIntrospectionResponse = BasicTokenIntrospectionResponse{}
}


// A poll must not be sent once the timeout has elapsed, including equality.
func migCheckDeadlineBoundary(t *testing.T) {
	t.Helper()
	for _, async := range []bool{false, true} {
		request, err := migNewTestClient(t).ExchangeDeviceAccessToken(migDeviceResponse(t, 1000))
		if err != nil { t.Fatal(err) }
		now := time.Unix(0, 0)
		request.SetTimeFunc(func() time.Time { return now })
		timeout := 5 * time.Second
		calls := 0
		var sleeps []time.Duration
		call := func(HTTPRequest) (HTTPResponse, error) {
			calls++
			if calls == 1 { return migNewHTTPResponse(400, migJsonHeader(), `{"error":"authorization_pending"}`), nil }
			return migNewHTTPResponse(200, migJsonHeader(), `{"access_token":"too-late","token_type":"bearer"}`), nil
		}
		sleep := func(d time.Duration) { sleeps = append(sleeps, d); now = now.Add(d) }
		var failure *RequestTokenError
		if async {
			asyncClient := AsyncHTTPClientFunc(func(_ context.Context, r HTTPRequest) (HTTPResponse, error) { return call(r) })
			// RequestAsync(...) does not prescribe the sleep callback's
			// signature. Exercise both equivalent caller-supplied forms.
			switch asyncRequest := any(request).(type) {
			case interface {
				RequestAsync(context.Context, AsyncHTTPClient, func(context.Context, time.Duration) error, *time.Duration) (BasicTokenResponse, *RequestTokenError)
			}:
				_, failure = asyncRequest.RequestAsync(context.Background(), asyncClient, func(_ context.Context, d time.Duration) error { sleep(d); return nil }, &timeout)
			case interface {
				RequestAsync(context.Context, AsyncHTTPClient, func(time.Duration), *time.Duration) (BasicTokenResponse, *RequestTokenError)
			}:
				_, failure = asyncRequest.RequestAsync(context.Background(), asyncClient, sleep, &timeout)
			default:
				t.Fatal("device polling must expose RequestAsync with a caller-supplied sleep function")
			}
		} else {
			_, failure = request.Request(SyncHTTPClientFunc(call), sleep, &timeout)
		}
		if failure == nil || failure.Kind != RequestTokenErrorServerResponse { t.Fatalf("elapsed timeout must return expired_token: %#v", failure) }
		var code DeviceCodeErrorResponseType
		switch response := failure.Response.(type) {
		case DeviceCodeErrorResponse: code = response.Error
		case *DeviceCodeErrorResponse: code = response.Error
		default: t.Fatalf("unexpected expiry response type: %T", failure.Response)
		}
		if code != DeviceCodeErrorResponseTypeExpiredToken { t.Fatalf("expiry error: %q", code) }
		if calls != 1 || !reflect.DeepEqual(sleeps, []time.Duration{5 * time.Second}) { t.Fatalf("poll after timeout: calls=%d sleeps=%v", calls, sleeps) }
	}
}
