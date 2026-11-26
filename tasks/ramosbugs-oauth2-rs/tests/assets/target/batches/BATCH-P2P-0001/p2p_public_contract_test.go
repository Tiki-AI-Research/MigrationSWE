// Package oauth2_test target-side P2P payload for BATCH-P2P-0001.
//
// This file is the evaluator-owned P2P payload: it implements every
// ``target.selectors`` entry declared for the P2P observations in
// ``shared/integration/observation_map.json`` (for example ``TestAuthorizeURL``
// and ``TestExchangeCodeSuccessfulWithBasicAuth``).  It is written as an
// external test package and depends only on the exported public API of the
// migrated library, so it observes the documented request/response behavior
// without relying on any unexported reference implementation detail.  The
// INTEGRATION runner copies it next to the migrated package and rewrites the
// import path below to the candidate's real import path.
package oauth2_test

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	o "oauth2/oauth2"
)

// --------------------------------------------------------------------------- //
// self-contained helpers (external package: public API only)
// --------------------------------------------------------------------------- //

// p2pMockClient asserts the exact request the implementation produces and
// returns a scripted response.  It mirrors the reference injected fake client.
type p2pMockClient struct {
	t           *testing.T
	wantMethod  string
	wantURL     string
	wantHeaders map[string]string
	wantBody    string
	response    o.HTTPResponse
	responseErr error
	calls       int
}

func (c *p2pMockClient) Call(request o.HTTPRequest) (o.HTTPResponse, error) {
	c.calls++
	if c.wantMethod != "" && request.Method != c.wantMethod {
		c.t.Errorf("method: got %q, want %q", request.Method, c.wantMethod)
	}
	if c.wantURL != "" && request.URL != c.wantURL {
		c.t.Errorf("url: got %q, want %q", request.URL, c.wantURL)
	}
	if c.wantHeaders != nil && !reflect.DeepEqual(request.Header, c.wantHeaders) {
		c.t.Errorf("headers: got %#v, want %#v", request.Header, c.wantHeaders)
	}
	if c.wantBody != "" && string(request.Body) != c.wantBody {
		c.t.Errorf("body: got %q, want %q", string(request.Body), c.wantBody)
	}
	if c.responseErr != nil {
		return o.HTTPResponse{}, c.responseErr
	}
	return c.response, nil
}

func p2pNewHTTPResponse(status int, header map[string]string, body string) o.HTTPResponse {
	if header == nil {
		header = map[string]string{}
	}
	return o.HTTPResponse{StatusCode: status, Header: header, Body: []byte(body)}
}

func p2pJSONHeader() map[string]string {
	return map[string]string{"Content-Type": "application/json"}
}

func p2pBasicAuthHeaders() map[string]string {
	return map[string]string{
		"Accept":        "application/json",
		"Content-Type":  "application/x-www-form-urlencoded",
		"Authorization": "Basic YWFhOmJiYg==",
	}
}

func p2pBodyAuthHeaders() map[string]string {
	return map[string]string{
		"Accept":       "application/json",
		"Content-Type": "application/x-www-form-urlencoded",
	}
}

func p2pStrPtr(s string) *string { return &s }

func p2pNewTestClient(t *testing.T) *o.BasicClient {
	t.Helper()
	authURL, err := o.NewAuthUrl("https://example.com/auth")
	if err != nil {
		t.Fatal(err)
	}
	tokenURL, err := o.NewTokenUrl("https://example.com/token")
	if err != nil {
		t.Fatal(err)
	}
	return o.NewBasicClient(o.NewClientId("aaa")).
		SetAuthURL(authURL).
		SetTokenURL(tokenURL).
		SetClientSecret(o.NewClientSecret("bbb"))
}

func p2pMustTokenURL(t *testing.T, raw string) *o.TokenUrl {
	t.Helper()
	u, err := o.NewTokenUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func p2pMustAuthURL(t *testing.T, raw string) *o.AuthUrl {
	t.Helper()
	u, err := o.NewAuthUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func p2pMustRedirectURL(t *testing.T, raw string) *o.RedirectUrl {
	t.Helper()
	u, err := o.NewRedirectUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func p2pMustIntrospectionURL(t *testing.T, raw string) *o.IntrospectionUrl {
	t.Helper()
	u, err := o.NewIntrospectionUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func p2pMustRevocationURL(t *testing.T, raw string) *o.RevocationUrl {
	t.Helper()
	u, err := o.NewRevocationUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func p2pMustDeviceAuthURL(t *testing.T, raw string) *o.DeviceAuthorizationUrl {
	t.Helper()
	u, err := o.NewDeviceAuthorizationUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

// p2pMockSuccessFailClient returns a scripted sequence of responses:
// numFailure copies of failure followed by one success.
type p2pMockSuccessFailClient struct {
	t          *testing.T
	wantURL    string
	wantHeader map[string]string
	wantBody   string
	failure    o.HTTPResponse
	success    o.HTTPResponse
	calls      int
	numFailure int
}

func (m *p2pMockSuccessFailClient) Call(request o.HTTPRequest) (o.HTTPResponse, error) {
	if request.Method != "POST" {
		m.t.Errorf("method: got %q, want POST", request.Method)
	}
	if request.URL != m.wantURL {
		m.t.Errorf("url: got %q, want %q", request.URL, m.wantURL)
	}
	if !reflect.DeepEqual(request.Header, m.wantHeader) {
		m.t.Errorf("headers: got %#v, want %#v", request.Header, m.wantHeader)
	}
	if string(request.Body) != m.wantBody {
		m.t.Errorf("body: got %q, want %q", string(request.Body), m.wantBody)
	}
	m.calls++
	if m.calls <= m.numFailure {
		return m.failure, nil
	}
	return m.success, nil
}

type p2pIncreasingTime struct{ n int64 }

func (t *p2pIncreasingTime) next() time.Time {
	v := t.n
	t.n++
	return time.Unix(v, 0).UTC()
}

func p2pMockTimeFunc() func() time.Time {
	timer := &p2pIncreasingTime{}
	return timer.next
}

func p2pMockSleepFunc(time.Duration) {}

func p2pDeviceTokenSuccessBody() string {
	return `{"access_token": "12/34", "token_type": "bearer", "scope": "openid"}`
}

func p2pNewDeviceAuthDetails(t *testing.T, expiresIn int) *o.StandardDeviceAuthorizationResponse {
	t.Helper()
	body := fmt.Sprintf(`{"device_code": "12345", "verification_uri": "https://verify/here", `+
		`"user_code": "abcde", "verification_uri_complete": "https://verify/here?abcde", `+
		`"expires_in": %d, "interval": 1}`, expiresIn)

	deviceAuthURL := p2pMustDeviceAuthURL(t, "https://deviceauth/here")
	client := p2pNewTestClient(t).SetDeviceAuthorizationURL(deviceAuthURL)
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://deviceauth/here",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "scope=openid&foo=bar",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), body),
	}
	request, err := client.ExchangeDeviceCode()
	if err != nil {
		t.Fatal(err)
	}
	details, tokenErr := request.AddExtraParam("foo", "bar").AddScope(o.NewScope("openid")).Request(caller)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	return details
}

func p2pAssertDeviceAuthDetails(t *testing.T, details *o.StandardDeviceAuthorizationResponse, expiresIn int) {
	t.Helper()
	if details.DeviceCode().Secret() != "12345" {
		t.Fatalf("device code: got %q", details.DeviceCode().Secret())
	}
	if details.VerificationURI().String() != "https://verify/here" {
		t.Fatalf("verification uri: got %q", details.VerificationURI().String())
	}
	if details.UserCode().Secret() != "abcde" {
		t.Fatalf("user code: got %q", details.UserCode().Secret())
	}
	if details.VerificationURIComplete() == nil || details.VerificationURIComplete().Secret() != "https://verify/here?abcde" {
		t.Fatalf("verification uri complete: %#v", details.VerificationURIComplete())
	}
	if details.ExpiresIn() != time.Duration(expiresIn)*time.Second {
		t.Fatalf("expires_in: got %v", details.ExpiresIn())
	}
	if details.Interval() != 1*time.Second {
		t.Fatalf("interval: got %v", details.Interval())
	}
}

// --------------------------------------------------------------------------- //
// P2P selectors
// --------------------------------------------------------------------------- //

func TestAuthorizeURL(t *testing.T) {
	client := p2pNewTestClient(t)
	request, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("csrf_token") })
	if err != nil {
		t.Fatal(err)
	}
	url, state := request.URL()
	if url.String() != "https://example.com/auth?response_type=code&client_id=aaa&state=csrf_token" {
		t.Fatalf("got %q", url.String())
	}
	if state.Secret() != "csrf_token" {
		t.Fatalf("state: got %q", state.Secret())
	}
}

func TestAuthorizeURLPKCE(t *testing.T) {
	client := p2pNewTestClient(t)
	verifier := o.NewPkceCodeVerifier("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
	request, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("csrf_token") })
	if err != nil {
		t.Fatal(err)
	}
	url, _ := request.SetPkceChallenge(o.NewPkceCodeChallengeFromVerifierSha256(verifier)).URL()
	want := "https://example.com/auth" +
		"?response_type=code&client_id=aaa" +
		"&state=csrf_token" +
		"&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM" +
		"&code_challenge_method=S256"
	if url.String() != want {
		t.Fatalf("got %q, want %q", url.String(), want)
	}
}

func TestAuthorizeURLImplicit(t *testing.T) {
	client := p2pNewTestClient(t)
	request, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("csrf_token") })
	if err != nil {
		t.Fatal(err)
	}
	url, _ := request.UseImplicitFlow().URL()
	if url.String() != "https://example.com/auth?response_type=token&client_id=aaa&state=csrf_token" {
		t.Fatalf("got %q", url.String())
	}
}

func TestAuthorizeURLWithParam(t *testing.T) {
	client := o.NewBasicClient(o.NewClientId("aaa")).
		SetClientSecret(o.NewClientSecret("bbb")).
		SetAuthURL(p2pMustAuthURL(t, "https://example.com/auth?foo=bar")).
		SetTokenURL(p2pMustTokenURL(t, "https://example.com/token"))

	request, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("csrf_token") })
	if err != nil {
		t.Fatal(err)
	}
	url, _ := request.URL()
	if url.String() != "https://example.com/auth?foo=bar&response_type=code&client_id=aaa&state=csrf_token" {
		t.Fatalf("got %q", url.String())
	}
}

func TestAuthorizeURLWithScopes(t *testing.T) {
	client := p2pNewTestClient(t)
	request, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("csrf_token") })
	if err != nil {
		t.Fatal(err)
	}
	url, _ := request.AddScopes([]o.Scope{o.NewScope("read"), o.NewScope("write")}).URL()
	want := "https://example.com/auth?response_type=code&client_id=aaa&state=csrf_token&scope=read+write"
	if url.String() != want {
		t.Fatalf("got %q, want %q", url.String(), want)
	}
}

func TestAuthorizeURLWithOneScope(t *testing.T) {
	client := p2pNewTestClient(t)
	request, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("csrf_token") })
	if err != nil {
		t.Fatal(err)
	}
	url, _ := request.AddScope(o.NewScope("read")).URL()
	want := "https://example.com/auth?response_type=code&client_id=aaa&state=csrf_token&scope=read"
	if url.String() != want {
		t.Fatalf("got %q, want %q", url.String(), want)
	}
}

func TestAuthorizeURLWithRedirectURL(t *testing.T) {
	client := p2pNewTestClient(t).
		SetRedirectURL(p2pMustRedirectURL(t, "https://localhost/redirect"))
	request, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("csrf_token") })
	if err != nil {
		t.Fatal(err)
	}
	url, _ := request.URL()
	want := "https://example.com/auth?response_type=code&client_id=aaa&state=csrf_token" +
		"&redirect_uri=https%3A%2F%2Flocalhost%2Fredirect"
	if url.String() != want {
		t.Fatalf("got %q, want %q", url.String(), want)
	}
}

func TestAuthorizeURLMissingEndpoint(t *testing.T) {
	client := o.NewBasicClient(o.NewClientId("aaa"))
	if _, err := client.AuthorizeURL(func() o.CsrfToken { return o.NewCsrfToken("x") }); err == nil {
		t.Fatal("expected missing-URL error")
	} else if err.Error() != "No authorization endpoint URL specified" {
		t.Fatalf("got %q", err.Error())
	}
}

func TestAsyncClientClosure(t *testing.T) {
	client := p2pNewTestClient(t)
	response := p2pNewHTTPResponse(200, nil, `{"access_token": "12/34", "token_type": "BEARER"}`)

	asyncClient := o.AsyncHTTPClientFunc(func(ctx context.Context, request o.HTTPRequest) (o.HTTPResponse, error) {
		return response, nil
	})

	request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	token, tokenErr := request.RequestAsync(context.Background(), asyncClient)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if token.AccessToken().Secret() != "12/34" {
		t.Fatalf("access token: got %q", token.AccessToken().Secret())
	}
	if token.TokenTypeValue() != o.BasicTokenTypeBearer {
		t.Fatalf("token type: got %v", token.TokenTypeValue())
	}
}

func TestExchangeCodeSuccessfulWithMinimalJSONResponse(t *testing.T) {
	client := o.NewBasicClient(o.NewClientId("aaa")).
		SetClientSecret(o.NewClientSecret("bbb")).
		SetAuthURL(p2pMustAuthURL(t, "https://example.com/auth")).
		SetTokenURL(p2pMustTokenURL(t, "https://example.com/token"))

	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "grant_type=authorization_code&code=ccc",
		response:    p2pNewHTTPResponse(200, nil, `{"access_token": "12/34", "token_type": "BEARER"}`),
	}

	request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	token, tokenErr := request.Request(caller)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}

	if token.AccessToken().Secret() != "12/34" {
		t.Fatalf("access token: got %q", token.AccessToken().Secret())
	}
	if token.TokenTypeValue() != o.BasicTokenTypeBearer {
		t.Fatalf("token type: got %v", token.TokenTypeValue())
	}
	if _, ok := token.ExpiresIn(); ok {
		t.Fatal("expected no expires_in")
	}
	if token.RefreshToken() != nil {
		t.Fatal("expected no refresh token")
	}

	serialized, err := json.Marshal(token)
	if err != nil {
		t.Fatal(err)
	}
	if string(serialized) != `{"access_token":"12/34","token_type":"bearer"}` {
		t.Fatalf("serialized: got %s", serialized)
	}

	var roundTripped o.BasicTokenResponse
	if err := json.Unmarshal([]byte(serialized), &roundTripped); err != nil {
		t.Fatal(err)
	}
	if roundTripped.AccessToken().Secret() != token.AccessToken().Secret() {
		t.Fatal("round trip access token mismatch")
	}
}

func TestExchangeCodeSuccessfulWithCompleteJSONResponse(t *testing.T) {
	client := p2pNewTestClient(t).SetAuthType(o.RequestBody)
	body := `{"access_token": "12/34", "token_type": "bearer", "scope": "read write", "expires_in": 3600, "refresh_token": "foobar"}`
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBodyAuthHeaders(),
		wantBody:    "grant_type=authorization_code&code=ccc&client_id=aaa&client_secret=bbb",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), body),
	}

	request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	token, tokenErr := request.Request(caller)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}

	if len(token.Scopes()) != 2 {
		t.Fatalf("scopes: got %#v", token.Scopes())
	}
	if expires, ok := token.ExpiresIn(); !ok || expires != 3600 {
		t.Fatalf("expires_in: got %v %v", expires, ok)
	}
	if token.RefreshToken() == nil || token.RefreshToken().Secret() != "foobar" {
		t.Fatal("refresh token mismatch")
	}

	serialized, _ := json.Marshal(token)
	want := `{"access_token":"12/34","token_type":"bearer","expires_in":3600,"refresh_token":"foobar","scope":"read write"}`
	if string(serialized) != want {
		t.Fatalf("serialized: got %s, want %s", serialized, want)
	}
}

func TestExchangeCodeSuccessfulWithPKCEAndExtension(t *testing.T) {
	client := p2pNewTestClient(t).
		SetAuthType(o.BasicAuth).
		SetRedirectURL(p2pMustRedirectURL(t, "https://redirect/here"))
	verifier := o.NewPkceCodeVerifier("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody: "grant_type=authorization_code&code=ccc" +
			"&code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk" +
			"&redirect_uri=https%3A%2F%2Fredirect%2Fhere&foo=bar",
		response: p2pNewHTTPResponse(200, p2pJSONHeader(), `{"access_token": "12/34", "token_type": "bearer", "scope": "read write"}`),
	}
	request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	if _, tokenErr := request.SetPkceVerifier(verifier).AddExtraParam("foo", "bar").Request(caller); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestExchangeCodeSuccessfulWithBasicAuth(t *testing.T) {
	client := p2pNewTestClient(t).
		SetAuthType(o.BasicAuth).
		SetRedirectURL(p2pMustRedirectURL(t, "https://redirect/here"))
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "grant_type=authorization_code&code=ccc&redirect_uri=https%3A%2F%2Fredirect%2Fhere",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), `{"access_token": "12/34", "token_type": "bearer", "scope": "read write"}`),
	}
	request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	if _, tokenErr := request.Request(caller); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestExchangeCodeWithSimpleJSONError(t *testing.T) {
	client := p2pNewTestClient(t)
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "grant_type=authorization_code&code=ccc",
		response: p2pNewHTTPResponse(400, p2pJSONHeader(),
			`{"error": "invalid_request", "error_description": "stuff happened"}`),
	}
	request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	_, tokenErr := request.Request(caller)
	if tokenErr == nil {
		t.Fatal("expected error")
	}
	if tokenErr.Kind != o.RequestTokenErrorServerResponse {
		t.Fatalf("kind: got %v", tokenErr.Kind)
	}
	errorResponse, ok := tokenErr.Response.(o.BasicErrorResponse)
	if !ok {
		t.Fatalf("unexpected response type %T", tokenErr.Response)
	}
	if errorResponse.Error != o.BasicErrorResponseTypeInvalidRequest {
		t.Fatalf("error: got %v", errorResponse.Error)
	}
	if errorResponse.ErrorDescription == nil || *errorResponse.ErrorDescription != "stuff happened" {
		t.Fatalf("description: %#v", errorResponse.ErrorDescription)
	}
	if errorResponse.ErrorURI != nil {
		t.Fatal("expected no error_uri")
	}
	if got := tokenErr.Error(); got != "Server returned error response: invalid_request: stuff happened" {
		t.Fatalf("error string: got %q", got)
	}
	serialized, _ := json.Marshal(errorResponse)
	if string(serialized) != `{"error":"invalid_request","error_description":"stuff happened"}` {
		t.Fatalf("serialized: got %s", serialized)
	}
}

func TestExchangeCodeWithJSONParseError(t *testing.T) {
	client := p2pNewTestClient(t)
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "grant_type=authorization_code&code=ccc",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), "broken json"),
	}
	request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	_, tokenErr := request.Request(caller)
	if tokenErr == nil {
		t.Fatal("expected error")
	}
	if tokenErr.Kind != o.RequestTokenErrorParse {
		t.Fatalf("kind: got %v", tokenErr.Kind)
	}
	if string(tokenErr.RawBody) != "broken json" {
		t.Fatalf("raw body: got %q", tokenErr.RawBody)
	}
}

func TestExchangeCodeWithUnexpectedContentType(t *testing.T) {
	client := p2pNewTestClient(t)
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "grant_type=authorization_code&code=ccc",
		response:    p2pNewHTTPResponse(200, map[string]string{"Content-Type": "text/plain"}, "broken json"),
	}
	request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	_, tokenErr := request.Request(caller)
	if tokenErr == nil {
		t.Fatal("expected error")
	}
	if tokenErr.Kind != o.RequestTokenErrorOther {
		t.Fatalf("kind: got %v", tokenErr.Kind)
	}
	if !strings.Contains(tokenErr.Error(), "text/plain") {
		t.Fatalf("error must name the unexpected content type: %q", tokenErr.Error())
	}
}

func TestExchangeCodeWith400StatusCode(t *testing.T) {
	client := p2pNewTestClient(t)
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "grant_type=authorization_code&code=ccc",
		response: p2pNewHTTPResponse(400, p2pJSONHeader(),
			`{"error":"invalid_request","error_description":"Expired code."}`),
	}
	request, err := client.ExchangeCode(o.NewAuthorizationCode("ccc"))
	if err != nil {
		t.Fatal(err)
	}
	_, tokenErr := request.Request(caller)
	if tokenErr == nil {
		t.Fatal("expected error")
	}
	if tokenErr.Error() != "Server returned error response: invalid_request: Expired code." {
		t.Fatalf("got %q", tokenErr.Error())
	}
}

func TestExchangeCodeMissingTokenEndpoint(t *testing.T) {
	client := o.NewBasicClient(o.NewClientId("aaa"))
	if _, err := client.ExchangeCode(o.NewAuthorizationCode("ccc")); err == nil {
		t.Fatal("expected missing-URL error")
	} else if err.Error() != "No token endpoint URL specified" {
		t.Fatalf("got %q", err.Error())
	}
}

func TestExchangeClientCredentialsWithBasicAuth(t *testing.T) {
	client := o.NewBasicClient(o.NewClientId("aaa/;&")).
		SetClientSecret(o.NewClientSecret("bbb/;&")).
		SetAuthURL(p2pMustAuthURL(t, "https://example.com/auth")).
		SetTokenURL(p2pMustTokenURL(t, "https://example.com/token")).
		SetAuthType(o.BasicAuth)

	headers := map[string]string{
		"Accept":        "application/json",
		"Content-Type":  "application/x-www-form-urlencoded",
		"Authorization": "Basic YWFhJTJGJTNCJTI2OmJiYiUyRiUzQiUyNg==",
	}
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: headers,
		wantBody:    "grant_type=client_credentials",
		response:    p2pNewHTTPResponse(200, nil, `{"access_token": "12/34", "token_type": "bearer", "scope": "read write"}`),
	}

	request, err := client.ExchangeClientCredentials()
	if err != nil {
		t.Fatal(err)
	}
	token, tokenErr := request.Request(caller)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if len(token.Scopes()) != 2 {
		t.Fatalf("scopes: got %#v", token.Scopes())
	}
}

func TestExchangeClientCredentialsWithBasicAuthButNoClientSecret(t *testing.T) {
	client := o.NewBasicClient(o.NewClientId("aaa/;&")).
		SetAuthURL(p2pMustAuthURL(t, "https://example.com/auth")).
		SetTokenURL(p2pMustTokenURL(t, "https://example.com/token")).
		SetAuthType(o.BasicAuth)

	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBodyAuthHeaders(),
		wantBody:    "grant_type=client_credentials&client_id=aaa%2F%3B%26",
		response:    p2pNewHTTPResponse(200, nil, `{"access_token": "12/34", "token_type": "bearer", "scope": "read write"}`),
	}
	request, err := client.ExchangeClientCredentials()
	if err != nil {
		t.Fatal(err)
	}
	if _, tokenErr := request.Request(caller); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestExchangeClientCredentialsWithBodyAuthAndScope(t *testing.T) {
	client := p2pNewTestClient(t).SetAuthType(o.RequestBody)
	caller := &p2pMockClient{
		t:          t,
		wantMethod: "POST",
		wantURL:    "https://example.com/token",
		wantHeaders: map[string]string{
			"Accept":       "application/json",
			"Content-Type": "application/x-www-form-urlencoded",
		},
		wantBody: "grant_type=client_credentials&scope=read+write&client_id=aaa&client_secret=bbb",
		response: p2pNewHTTPResponse(200, map[string]string{"Content-Type": "APPLICATION/jSoN"},
			`{"access_token": "12/34", "token_type": "bearer", "scope": "read write"}`),
	}
	request, err := client.ExchangeClientCredentials()
	if err != nil {
		t.Fatal(err)
	}
	if _, tokenErr := request.AddScope(o.NewScope("read")).AddScope(o.NewScope("write")).Request(caller); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestTokenRevocationWithMissingURL(t *testing.T) {
	client := p2pNewTestClient(t).SetRevocationURLOption(nil)
	_, err := client.RevokeToken(o.NewStandardRevocableTokenFromAccessToken(o.NewAccessToken("access_token_123")))
	if err == nil {
		t.Fatal("expected missing-URL error")
	}
	if err.Error() != "No revocation endpoint URL specified" {
		t.Fatalf("got %q", err.Error())
	}
}

func TestTokenRevocationWithNonHTTPSURL(t *testing.T) {
	client := p2pNewTestClient(t).SetRevocationURL(p2pMustRevocationURL(t, "http://revocation/url"))
	_, err := client.RevokeToken(o.NewStandardRevocableTokenFromAccessToken(o.NewAccessToken("access_token_123")))
	if err == nil {
		t.Fatal("expected insecure-URL error")
	}
	if err.Error() != "Scheme for revocation endpoint URL must be HTTPS" {
		t.Fatalf("got %q", err.Error())
	}
}

func TestTokenRevocationWithAccessTokenAndEmptyJSONResponse(t *testing.T) {
	client := p2pNewTestClient(t).SetRevocationURL(p2pMustRevocationURL(t, "https://revocation/url"))
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://revocation/url",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "token=access_token_123&token_type_hint=access_token",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), `{}`),
	}
	request, err := client.RevokeToken(o.NewStandardRevocableTokenFromAccessToken(o.NewAccessToken("access_token_123")))
	if err != nil {
		t.Fatal(err)
	}
	if tokenErr := request.Request(caller); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestTokenRevocationWithAccessTokenAndEmptyResponse(t *testing.T) {
	client := p2pNewTestClient(t).SetRevocationURL(p2pMustRevocationURL(t, "https://revocation/url"))
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://revocation/url",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "token=access_token_123&token_type_hint=access_token",
		response:    p2pNewHTTPResponse(200, nil, ""),
	}
	request, err := client.RevokeToken(o.NewStandardRevocableTokenFromAccessToken(o.NewAccessToken("access_token_123")))
	if err != nil {
		t.Fatal(err)
	}
	if tokenErr := request.Request(caller); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestTokenRevocationWithAccessTokenAndNonJSONResponse(t *testing.T) {
	client := p2pNewTestClient(t).SetRevocationURL(p2pMustRevocationURL(t, "https://revocation/url"))
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://revocation/url",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "token=access_token_123&token_type_hint=access_token",
		response:    p2pNewHTTPResponse(200, map[string]string{"Content-Type": "application/octet-stream"}, string([]byte{1, 2, 3})),
	}
	request, err := client.RevokeToken(o.NewStandardRevocableTokenFromAccessToken(o.NewAccessToken("access_token_123")))
	if err != nil {
		t.Fatal(err)
	}
	if tokenErr := request.Request(caller); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestTokenRevocationWithRefreshToken(t *testing.T) {
	client := p2pNewTestClient(t).SetRevocationURL(p2pMustRevocationURL(t, "https://revocation/url"))
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://revocation/url",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "token=refresh_token_123&token_type_hint=refresh_token",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), `{}`),
	}
	request, err := client.RevokeToken(o.NewStandardRevocableTokenFromRefreshToken(o.NewRefreshToken("refresh_token_123")))
	if err != nil {
		t.Fatal(err)
	}
	if tokenErr := request.Request(caller); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestTokenIntrospectionSuccessfulWithBasicAuthMinimalResponse(t *testing.T) {
	client := p2pNewTestClient(t).
		SetAuthType(o.BasicAuth).
		SetRedirectURL(p2pMustRedirectURL(t, "https://redirect/here")).
		SetIntrospectionURL(p2pMustIntrospectionURL(t, "https://introspection/url"))

	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://introspection/url",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "token=access_token_123",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), `{"active": true}`),
	}
	token := o.NewAccessToken("access_token_123")
	request, err := client.Introspect(&token)
	if err != nil {
		t.Fatal(err)
	}
	response, tokenErr := request.Request(caller)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if !response.Active() {
		t.Fatal("expected active")
	}
	if response.Scopes() != nil || response.ClientId() != nil || response.Username() != nil {
		t.Fatal("expected empty optional fields")
	}
	if _, ok := response.TokenType(); ok {
		t.Fatal("expected no token type")
	}
	if response.Exp() != nil || response.Iat() != nil || response.Nbf() != nil {
		t.Fatal("expected no timestamps")
	}
	if response.Sub() != nil || response.Aud() != nil || response.Iss() != nil || response.Jti() != nil {
		t.Fatal("expected no jwt claims")
	}
}

func TestTokenIntrospectionSuccessfulWithBasicAuthFullResponse(t *testing.T) {
	client := p2pNewTestClient(t).
		SetAuthType(o.BasicAuth).
		SetRedirectURL(p2pMustRedirectURL(t, "https://redirect/here")).
		SetIntrospectionURL(p2pMustIntrospectionURL(t, "https://introspection/url"))

	body := `{
		"active": true,
		"scope": "email profile",
		"client_id": "aaa",
		"username": "demo",
		"token_type": "bearer",
		"exp": 1604073517,
		"iat": 1604073217,
		"nbf": 1604073317,
		"sub": "demo",
		"aud": "demo",
		"iss": "http://127.0.0.1:8080/auth/realms/test-realm",
		"jti": "be1b7da2-fc18-47b3-bdf1-7a4f50bcf53f"
	}`
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://introspection/url",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "token=access_token_123&token_type_hint=access_token",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), body),
	}
	token := o.NewAccessToken("access_token_123")
	request, err := client.Introspect(&token)
	if err != nil {
		t.Fatal(err)
	}
	response, tokenErr := request.SetTokenTypeHint("access_token").Request(caller)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if !response.Active() {
		t.Fatal("expected active")
	}
	if len(response.Scopes()) != 2 || response.Scopes()[0].AsStr() != "email" || response.Scopes()[1].AsStr() != "profile" {
		t.Fatalf("scopes: got %#v", response.Scopes())
	}
	if response.ClientId() == nil || response.ClientId().String() != "aaa" {
		t.Fatalf("client_id: %#v", response.ClientId())
	}
	if response.Username() == nil || *response.Username() != "demo" {
		t.Fatalf("username: %#v", response.Username())
	}
	tokenType, ok := response.TokenType()
	if !ok || tokenType != o.BasicTokenTypeBearer {
		t.Fatalf("token_type: %#v %v", tokenType, ok)
	}
	if response.Exp() == nil || *response.Exp() != 1604073517 {
		t.Fatalf("exp: %#v", response.Exp())
	}
	if response.Iat() == nil || *response.Iat() != 1604073217 {
		t.Fatalf("iat: %#v", response.Iat())
	}
	if response.Nbf() == nil || *response.Nbf() != 1604073317 {
		t.Fatalf("nbf: %#v", response.Nbf())
	}
	if response.Sub() == nil || *response.Sub() != "demo" {
		t.Fatalf("sub: %#v", response.Sub())
	}
	if len(response.Aud()) != 1 || response.Aud()[0] != "demo" {
		t.Fatalf("aud: %#v", response.Aud())
	}
	if response.Iss() == nil || *response.Iss() != "http://127.0.0.1:8080/auth/realms/test-realm" {
		t.Fatalf("iss: %#v", response.Iss())
	}
	if response.Jti() == nil || *response.Jti() != "be1b7da2-fc18-47b3-bdf1-7a4f50bcf53f" {
		t.Fatalf("jti: %#v", response.Jti())
	}
}

func TestTokenIntrospectionMissingEndpoint(t *testing.T) {
	client := p2pNewTestClient(t)
	token := o.NewAccessToken("access_token_123")
	if _, err := client.Introspect(&token); err == nil {
		t.Fatal("expected missing-URL error")
	} else if err.Error() != "No introspection endpoint URL specified" {
		t.Fatalf("got %q", err.Error())
	}
}

func TestErrorResponseSerializer(t *testing.T) {
	unauthorized := o.NewStandardErrorResponse(o.BasicErrorResponseTypeUnauthorizedClient, nil, nil)
	serialized, err := json.Marshal(unauthorized)
	if err != nil {
		t.Fatal(err)
	}
	if string(serialized) != `{"error":"unauthorized_client"}` {
		t.Fatalf("got %s", serialized)
	}

	invalid := o.NewStandardErrorResponse(
		o.BasicErrorResponseTypeInvalidClient,
		p2pStrPtr("Invalid client_id"),
		p2pStrPtr("https://example.com/errors/invalid_client"),
	)
	serialized, err = json.Marshal(invalid)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"error":"invalid_client","error_description":"Invalid client_id","error_uri":"https://example.com/errors/invalid_client"}`
	if string(serialized) != want {
		t.Fatalf("got %s, want %s", serialized, want)
	}

	var decoded o.StandardErrorResponse[o.BasicErrorResponseType]
	if err := json.Unmarshal(serialized, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Error != invalid.Error ||
		decoded.ErrorDescription == nil || *decoded.ErrorDescription != *invalid.ErrorDescription ||
		decoded.ErrorURI == nil || *decoded.ErrorURI != *invalid.ErrorURI {
		t.Fatalf("round trip mismatch: %#v != %#v", decoded, invalid)
	}
}

func TestConfigurationErrorMessages(t *testing.T) {
	missing := &o.ConfigurationError{Kind: o.ConfigurationMissingURL, Endpoint: "revocation"}
	if got := missing.Error(); got != "No revocation endpoint URL specified" {
		t.Fatalf("got %q", got)
	}
	insecure := &o.ConfigurationError{Kind: o.ConfigurationInsecureURL, Endpoint: "revocation"}
	if got := insecure.Error(); got != "Scheme for revocation endpoint URL must be HTTPS" {
		t.Fatalf("got %q", got)
	}
}

func TestSecretConversion(t *testing.T) {
	secret := o.NewCsrfToken("top_secret")
	if secret.Secret() != "top_secret" {
		t.Fatalf("Secret(): got %q", secret.Secret())
	}
}

func TestSecretRedaction(t *testing.T) {
	secret := o.NewClientSecret("top_secret")
	if got := p2pSprintf("%v", secret); got != "ClientSecret([redacted])" {
		t.Fatalf("got %q", got)
	}
	if got := p2pSprintf("%s", secret); got != "ClientSecret([redacted])" {
		t.Fatalf("got %q", got)
	}
	if got := p2pSprintf("%#v", secret); got != "ClientSecret([redacted])" {
		t.Fatalf("got %q", got)
	}
	if p2pSprintf("%v", secret) == "top_secret" {
		t.Fatal("secret value leaked in formatted output")
	}
	values := []struct {
		name string
		value any
	}{
		{"ClientSecret", new(o.ClientSecret)},
		{"AccessToken", new(o.AccessToken)},
		{"RefreshToken", new(o.RefreshToken)},
		{"AuthorizationCode", new(o.AuthorizationCode)},
		{"PkceCodeVerifier", new(o.PkceCodeVerifier)},
		{"CsrfToken", new(o.CsrfToken)},
		{"ResourceOwnerPassword", new(o.ResourceOwnerPassword)},
		{"DeviceCode", new(o.DeviceCode)},
		{"UserCode", new(o.UserCode)},
		{"VerificationUriComplete", new(o.VerificationUriComplete)},
	}
	for _, value := range values {
		for _, encoded := range []string{`""`, `"top_secret"`} {
			if err := json.Unmarshal([]byte(encoded), value.value); err != nil {
				t.Fatal(err)
			}
			wantSecret := ""
			if encoded == `"top_secret"` {
				wantSecret = "top_secret"
			}
			if got := value.value.(interface{ Secret() string }).Secret(); got != wantSecret {
				t.Fatalf("%s accessor changed the secret", value.name)
			}
			for _, verb := range []string{"%v", "%s", "%#v"} {
				for _, printable := range []any{value.value, reflect.ValueOf(value.value).Elem().Interface()} {
					if got := p2pSprintf(verb, printable); got != value.name+"([redacted])" {
						t.Fatalf("%s %s: got %q", value.name, verb, got)
					}
				}
			}
		}
	}
}

func TestDeviceTokenPendingThenSuccess(t *testing.T) {
	details := p2pNewDeviceAuthDetails(t, 20)
	p2pAssertDeviceAuthDetails(t, details, 20)

	caller := &p2pMockSuccessFailClient{
		t:          t,
		wantURL:    "https://example.com/token",
		wantHeader: p2pBasicAuthHeaders(),
		wantBody:   "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code&device_code=12345",
		failure: p2pNewHTTPResponse(400, p2pJSONHeader(),
			`{"error": "authorization_pending", "error_description": "Still waiting for user"}`),
		numFailure: 5,
		success:    p2pNewHTTPResponse(200, p2pJSONHeader(), p2pDeviceTokenSuccessBody()),
	}

	client := p2pNewTestClient(t)
	request, err := client.ExchangeDeviceAccessToken(details)
	if err != nil {
		t.Fatal(err)
	}
	token, tokenErr := request.SetTimeFunc(p2pMockTimeFunc()).Request(caller, p2pMockSleepFunc, nil)
	if tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
	if token.AccessToken().Secret() != "12/34" {
		t.Fatalf("access token: got %q", token.AccessToken().Secret())
	}
	if token.TokenTypeValue() != o.BasicTokenTypeBearer {
		t.Fatalf("token type: got %v", token.TokenTypeValue())
	}
	if len(token.Scopes()) != 1 || token.Scopes()[0].AsStr() != "openid" {
		t.Fatalf("scopes: got %#v", token.Scopes())
	}
	if _, ok := token.ExpiresIn(); ok {
		t.Fatal("expected no expires_in")
	}
	if token.RefreshToken() != nil {
		t.Fatal("expected no refresh token")
	}
}

func TestDeviceTokenSlowdownThenSuccess(t *testing.T) {
	details := p2pNewDeviceAuthDetails(t, 3600)
	p2pAssertDeviceAuthDetails(t, details, 3600)

	caller := &p2pMockSuccessFailClient{
		t:          t,
		wantURL:    "https://example.com/token",
		wantHeader: p2pBasicAuthHeaders(),
		wantBody:   "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code&device_code=12345",
		failure: p2pNewHTTPResponse(400, p2pJSONHeader(),
			`{"error": "slow_down", "error_description": "Woah there partner"}`),
		numFailure: 5,
		success:    p2pNewHTTPResponse(200, p2pJSONHeader(), p2pDeviceTokenSuccessBody()),
	}

	client := p2pNewTestClient(t)
	request, err := client.ExchangeDeviceAccessToken(details)
	if err != nil {
		t.Fatal(err)
	}
	if _, tokenErr := request.SetTimeFunc(p2pMockTimeFunc()).Request(caller, p2pMockSleepFunc, nil); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestExchangeDeviceCodeAndToken(t *testing.T) {
	details := p2pNewDeviceAuthDetails(t, 3600)
	p2pAssertDeviceAuthDetails(t, details, 3600)

	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code&device_code=12345",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), p2pDeviceTokenSuccessBody()),
	}

	client := p2pNewTestClient(t)
	request, err := client.ExchangeDeviceAccessToken(details)
	if err != nil {
		t.Fatal(err)
	}
	if _, tokenErr := request.SetTimeFunc(p2pMockTimeFunc()).Request(caller, p2pMockSleepFunc, nil); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

func TestDeviceTokenAuthorizationTimeout(t *testing.T) {
	details := p2pNewDeviceAuthDetails(t, 2)
	p2pAssertDeviceAuthDetails(t, details, 2)

	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code&device_code=12345",
		response: p2pNewHTTPResponse(400, p2pJSONHeader(),
			`{"error": "authorization_pending", "error_description": "Still waiting for user"}`),
	}

	client := p2pNewTestClient(t)
	request, err := client.ExchangeDeviceAccessToken(details)
	if err != nil {
		t.Fatal(err)
	}
	_, tokenErr := request.SetTimeFunc(p2pMockTimeFunc()).Request(caller, p2pMockSleepFunc, nil)
	if tokenErr == nil {
		t.Fatal("expected expiry error")
	}
	errorResponse, ok := tokenErr.Response.(o.DeviceCodeErrorResponse)
	if !ok {
		t.Fatalf("unexpected response type %T", tokenErr.Response)
	}
	if errorResponse.Error != o.DeviceCodeErrorResponseTypeExpiredToken {
		t.Fatalf("error: got %v", errorResponse.Error)
	}
	if errorResponse.ErrorDescription == nil || *errorResponse.ErrorDescription != "This device code has expired." {
		t.Fatalf("description: %#v", errorResponse.ErrorDescription)
	}
}

func TestDeviceTokenExpired(t *testing.T) {
	details := p2pNewDeviceAuthDetails(t, 2)
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://example.com/token",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code&device_code=12345",
		response: p2pNewHTTPResponse(400, p2pJSONHeader(),
			`{"error": "expired_token", "error_description": "Token has expired"}`),
	}
	client := p2pNewTestClient(t)
	request, err := client.ExchangeDeviceAccessToken(details)
	if err != nil {
		t.Fatal(err)
	}
	_, tokenErr := request.SetTimeFunc(p2pMockTimeFunc()).Request(caller, p2pMockSleepFunc, nil)
	if tokenErr == nil {
		t.Fatal("expected error")
	}
	errorResponse := tokenErr.Response.(o.DeviceCodeErrorResponse)
	if errorResponse.Error != o.DeviceCodeErrorResponseTypeExpiredToken {
		t.Fatalf("error: got %v", errorResponse.Error)
	}
}

// --------------------------------------------------------------------------- //
// TestExtensionRevocationSuccessful needs the documented extension points.
// --------------------------------------------------------------------------- //

type p2pColorfulTokenType string

const (
	p2pColorfulTokenTypeGreen p2pColorfulTokenType = "green"
	p2pColorfulTokenTypeRed   p2pColorfulTokenType = "red"
)

func (t p2pColorfulTokenType) TokenTypeString() string { return string(t) }

type p2pColorfulFields struct {
	Shape  *string `json:"shape,omitempty"`
	Height uint32  `json:"height"`
}

func (p2pColorfulFields) IsExtraTokenFields() {}

type p2pColorfulErrorResponseType string

const (
	p2pColorfulErrorResponseTypeTooDark  p2pColorfulErrorResponseType = "too_dark"
	p2pColorfulErrorResponseTypeTooLight p2pColorfulErrorResponseType = "too_light"
)

func (t p2pColorfulErrorResponseType) ErrorTypeString() string { return string(t) }

type p2pColorfulRevocableToken struct {
	SecretValue string
	Hint        string
}

func (t p2pColorfulRevocableToken) Secret() string   { return t.SecretValue }
func (t p2pColorfulRevocableToken) TypeHint() string { return t.Hint }

type p2pColorfulClient = o.Client[
	o.StandardErrorResponse[p2pColorfulErrorResponseType],
	o.StandardTokenResponse[p2pColorfulFields, p2pColorfulTokenType],
	o.StandardTokenIntrospectionResponse[p2pColorfulFields, p2pColorfulTokenType],
	p2pColorfulRevocableToken,
	o.StandardErrorResponse[p2pColorfulErrorResponseType],
]

func p2pNewColorfulClient(t *testing.T) *p2pColorfulClient {
	t.Helper()
	return o.NewClient[
		o.StandardErrorResponse[p2pColorfulErrorResponseType],
		o.StandardTokenResponse[p2pColorfulFields, p2pColorfulTokenType],
		o.StandardTokenIntrospectionResponse[p2pColorfulFields, p2pColorfulTokenType],
		p2pColorfulRevocableToken,
		o.StandardErrorResponse[p2pColorfulErrorResponseType],
	](o.NewClientId("aaa")).
		SetClientSecret(o.NewClientSecret("bbb")).
		SetAuthURL(p2pMustAuthURL(t, "https://example.com/auth")).
		SetTokenURL(p2pMustTokenURL(t, "https://example.com/token"))
}

func TestExtensionRevocationSuccessful(t *testing.T) {
	client := p2pNewColorfulClient(t).
		SetRevocationURL(p2pMustRevocationURL(t, "https://revocation/url"))
	caller := &p2pMockClient{
		t:           t,
		wantMethod:  "POST",
		wantURL:     "https://revocation/url",
		wantHeaders: p2pBasicAuthHeaders(),
		wantBody:    "token=colorful_token_123&token_type_hint=red_token",
		response:    p2pNewHTTPResponse(200, p2pJSONHeader(), `{}`),
	}

	request, err := client.RevokeToken(p2pColorfulRevocableToken{SecretValue: "colorful_token_123", Hint: "red_token"})
	if err != nil {
		t.Fatal(err)
	}
	if tokenErr := request.Request(caller); tokenErr != nil {
		t.Fatalf("unexpected error: %v", tokenErr)
	}
}

// p2pSprintf keeps the formatting assertions self-contained: the external probe
// package is compiled together with the F2P probe, so every helper name here is
// namespaced with the p2p prefix.
func p2pSprintf(format string, args ...any) string {
	return fmt.Sprintf(format, args...)
}
