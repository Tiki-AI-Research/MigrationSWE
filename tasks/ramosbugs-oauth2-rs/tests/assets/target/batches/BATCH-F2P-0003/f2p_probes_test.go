// Package oauth2 target-side F2P probes for BATCH-F2P-0003.
//
// Every probe executes a concrete declared behavior against the migrated Go
// package and asserts the frozen target postcondition. These probes are the
// target half of the F2P observations; the source half (Rust) is expected to
// FAIL the same predicate because the required target technology (Go 1.22) is
// not in effect there.
//
// The external test package observes only the exported API and injected HTTP
// calls, independent of private implementation names or storage layouts.
package oauth2_test

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	. "oauth2/oauth2"
)

// probeCaptureClient records the request it received and returns a scripted
// response. It is the injected client the F2P probes route their calls through.
type probeCaptureClient struct {
	t        *testing.T
	response HTTPResponse
	err      error
	calls    int
	request  HTTPRequest
}

func (c *probeCaptureClient) Call(request HTTPRequest) (HTTPResponse, error) {
	c.calls++
	c.request = request
	if c.err != nil {
		return HTTPResponse{}, c.err
	}
	return c.response, nil
}

func probeJSONHeader() map[string]string {
	return map[string]string{"Content-Type": "application/json"}
}

func probeStatusBody(status int, body string) HTTPResponse {
	return HTTPResponse{StatusCode: status, Header: probeJSONHeader(), Body: []byte(body)}
}

// F2P-049 / F2P-058 / F2P-064 (R030): Basic auth must separately url-encode the
// client id and secret before base64. Go http.Request.SetBasicAuth (raw
// concatenation) is the failing migration.
func TestProbeR030BasicAuthCredentialEncoding(t *testing.T) {
	client := NewBasicClient(NewClientId("a b")).SetClientSecret(NewClientSecret("c:d"))
	client.SetTokenURL(migMustTokenURL(t, "https://example.com/token"))

	request, reqErr := client.ExchangeClientCredentials()
	if reqErr != nil {
		t.Fatalf("ExchangeClientCredentials: %v", reqErr)
	}
	capture := &probeCaptureClient{t: t, response: probeStatusBody(200, `{"access_token":"t","token_type":"bearer"}`)}
	if _, err := request.Request(capture); err != nil {
		t.Fatalf("Request: %v", err)
	}

	want := "Basic " + base64.StdEncoding.EncodeToString([]byte("a+b:c%3Ad"))
	if got := capture.request.Header["Authorization"]; got != want {
		t.Fatalf("Authorization: got %q, want %q", got, want)
	}
	raw := "Basic " + base64.StdEncoding.EncodeToString([]byte("a b:c:d"))
	if capture.request.Header["Authorization"] == raw {
		t.Fatal("Authorization used raw SetBasicAuth concatenation")
	}
	client = NewBasicClient(NewClientId("a~*")).SetClientSecret(NewClientSecret("b~*")).SetTokenURL(migMustTokenURL(t, "https://example.com/token"))
	request, reqErr = client.ExchangeClientCredentials()
	if reqErr != nil { t.Fatal(reqErr) }
	if _, err := request.Request(capture); err != nil { t.Fatal(err) }
	want = "Basic " + base64.StdEncoding.EncodeToString([]byte("a%7E*:b%7E*"))
	if got := capture.request.Header["Authorization"]; got != want {
		t.Fatalf("form encoding must preserve * and escape ~: got %q, want %q", got, want)
	}
}

// F2P-055 / F2P-061 / F2P-062 / F2P-063 / F2P-065 (R032): token types parse
// case-insensitively and re-serialize to their canonical string.
func TestProbeR032TokenTypeCaseInsensitive(t *testing.T) {
	var response BasicTokenResponse
	if err := json.Unmarshal([]byte(`{"access_token":"t","token_type":"BEARER"}`), &response); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := response.TokenTypeValue(); got != TokenType(BasicTokenTypeBearer) {
		t.Fatalf("TokenTypeValue: got %#v, want %#v", got, BasicTokenTypeBearer)
	}
	out, err := json.Marshal(response)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(out), `"token_type":"bearer"`) {
		t.Fatalf("re-marshalled token type not canonical: %s", out)
	}
	if strings.Contains(string(out), "BEARER") {
		t.Fatalf("re-marshalled token type preserved raw casing: %s", out)
	}
}

// F2P-069 / F2P-070 (R036): StandardErrorResponse marshals keys in the order
// error, error_description, error_uri.
func TestProbeR036StandardErrorResponseOrdering(t *testing.T) {
	description := "bad"
	uri := "u"
	response := NewStandardErrorResponse[BasicErrorResponseType](
		BasicErrorResponseTypeInvalidGrant, &description, &uri,
	)
	out, err := json.Marshal(response)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	want := `{"error":"invalid_grant","error_description":"bad","error_uri":"u"}`
	if string(out) != want {
		t.Fatalf("serialized order: got %s, want %s", out, want)
	}
}

// F2P-050 (R040): a revocation response with status 200 and a non-empty body
// returns a nil error; the body is ignored.
func TestProbeR040RevocationBodyIgnored(t *testing.T) {
	client := NewBasicClient(NewClientId("id"))
	client.SetRevocationURL(migMustRevocationURL(t, "https://example.com/revoke"))
	client.SetClientSecret(NewClientSecret("secret"))

	request, reqErr := client.RevokeToken(NewStandardRevocableTokenFromAccessToken(NewAccessToken("token-value")))
	if reqErr != nil {
		t.Fatalf("RevokeToken: %v", reqErr)
	}
	capture := &probeCaptureClient{t: t, response: probeStatusBody(200, `{"not":"a token response"}`)}
	if err := request.Request(capture); err != nil {
		t.Fatalf("revocation with non-empty body returned error: %v", err)
	}
}

// F2P-051 / F2P-052 / F2P-054 / F2P-059 (R035): a non-HTTPS revocation URL is
// rejected with ConfigurationInsecureURL before any request is sent.
func TestProbeR035RevocationInsecureURL(t *testing.T) {
	client := NewBasicClient(NewClientId("id"))
	client.SetRevocationURL(migMustRevocationURL(t, "http://example.com/revoke"))

	if _, err := client.RevokeToken(NewStandardRevocableTokenFromAccessToken(NewAccessToken("token-value"))); err == nil {
		t.Fatal("expected ConfigurationError for non-HTTPS revocation URL")
	} else {
		cfg, ok := err.(*ConfigurationError)
		if !ok {
			t.Fatalf("error type: got %T, want *ConfigurationError", err)
		}
		if cfg.Kind != ConfigurationInsecureURL {
			t.Fatalf("ConfigurationError.Kind: got %v, want ConfigurationInsecureURL", cfg.Kind)
		}
	}

	sent := &probeCaptureClient{t: t, response: probeStatusBody(200, "")}
	https := NewBasicClient(NewClientId("id"))
	https.SetRevocationURL(migMustRevocationURL(t, "https://example.com/revoke"))
	if _, err := https.RevokeToken(NewStandardRevocableTokenFromAccessToken(NewAccessToken("t"))); err != nil {
		t.Fatalf("https RevokeToken: %v", err)
	}
	if sent.calls != 0 {
		t.Fatalf("injected client called %d times, want 0", sent.calls)
	}
}

// F2P-051 / F2P-054 / F2P-056 (R035/R014): a flow accessor whose required
// endpoint is unset returns ConfigurationMissingURL naming the endpoint.
func TestProbeR014MissingEndpoint(t *testing.T) {
	client := NewBasicClient(NewClientId("id"))
	if _, err := client.ExchangeCode(NewAuthorizationCode("code")); err == nil {
		t.Fatal("expected ConfigurationError for unset token URL")
	} else {
		cfg, ok := err.(*ConfigurationError)
		if !ok {
			t.Fatalf("error type: got %T, want *ConfigurationError", err)
		}
		if cfg.Kind != ConfigurationMissingURL || cfg.Endpoint != "token" {
			t.Fatalf("got %+v, want missing token URL", cfg)
		}
	}
}

// F2P-061 / F2P-057 (R005): every flow request is a POST with form content type
// and JSON Accept header, and a form-encoded body.
func TestProbeR005FlowRequestShape(t *testing.T) {
	client := newProbeTokenClient(t)
	request, reqErr := client.ExchangeClientCredentials()
	if reqErr != nil {
		t.Fatalf("ExchangeClientCredentials: %v", reqErr)
	}
	capture := &probeCaptureClient{t: t, response: probeStatusBody(200, `{"access_token":"t","token_type":"bearer"}`)}
	if _, err := request.Request(capture); err != nil {
		t.Fatalf("Request: %v", err)
	}
	if capture.request.Method != "POST" {
		t.Fatalf("method: got %q, want POST", capture.request.Method)
	}
	if got := capture.request.Header["Content-Type"]; got != "application/x-www-form-urlencoded" {
		t.Fatalf("Content-Type: got %q", got)
	}
	if got := capture.request.Header["Accept"]; got != "application/json" {
		t.Fatalf("Accept: got %q", got)
	}
	if !strings.Contains(string(capture.request.Body), "grant_type=client_credentials") {
		t.Fatalf("body not form-encoded: %q", capture.request.Body)
	}
}

// F2P-062 / F2P-053 / F2P-058 (R006): with BasicAuth the credentials live in the
// Authorization header; with RequestBody they live in the form body.
func TestProbeR006ClientCredentialPlacement(t *testing.T) {
	client := newProbeTokenClient(t)
	request, reqErr := client.ExchangeClientCredentials()
	if reqErr != nil {
		t.Fatalf("ExchangeClientCredentials: %v", reqErr)
	}
	capture := &probeCaptureClient{t: t, response: probeStatusBody(200, `{"access_token":"t","token_type":"bearer"}`)}
	if _, err := request.Request(capture); err != nil {
		t.Fatalf("Request: %v", err)
	}
	if capture.request.Header["Authorization"] == "" {
		t.Fatal("BasicAuth request missing Authorization header")
	}
	body := string(capture.request.Body)
	if strings.Contains(body, "client_id=") || strings.Contains(body, "client_secret=") {
		t.Fatalf("BasicAuth body must omit client credentials: %q", body)
	}

	bodyClient := newProbeTokenClient(t).SetAuthType(RequestBody)
	bodyRequest, reqErr := bodyClient.ExchangeClientCredentials()
	if reqErr != nil {
		t.Fatalf("ExchangeClientCredentials: %v", reqErr)
	}
	bodyCapture := &probeCaptureClient{t: t, response: probeStatusBody(200, `{"access_token":"t","token_type":"bearer"}`)}
	if _, err := bodyRequest.Request(bodyCapture); err != nil {
		t.Fatalf("Request: %v", err)
	}
	if bodyCapture.request.Header["Authorization"] != "" {
		t.Fatal("RequestBody auth must not set Authorization")
	}
	if !strings.Contains(string(bodyCapture.request.Body), "client_id=probe-client") {
		t.Fatalf("RequestBody body missing client_id: %q", bodyCapture.request.Body)
	}
}

// F2P-060 / F2P-066 (R033): Content-Type comparison is case-insensitive and
// parameter-insensitive; a wrong type is named in an other error.
func TestProbeR033ContentTypeValidation(t *testing.T) {
	request, reqErr := newProbeTokenClient(t).ExchangeClientCredentials()
	if reqErr != nil {
		t.Fatalf("ExchangeClientCredentials: %v", reqErr)
	}

	ok := &probeCaptureClient{t: t, response: HTTPResponse{
		StatusCode: 200,
		Header:     map[string]string{"Content-Type": "Application/JSON; charset=utf-8"},
		Body:       []byte(`{"access_token":"t","token_type":"bearer"}`),
	}}
	if _, err := request.Request(ok); err != nil {
		t.Fatalf("parameterized/case-variant JSON content type rejected: %v", err)
	}

	bad := &probeCaptureClient{t: t, response: HTTPResponse{
		StatusCode: 200,
		Header:     map[string]string{"Content-Type": "text/plain"},
		Body:       []byte(`{"access_token":"t","token_type":"bearer"}`),
	}}
	_, err := request.Request(bad)
	if err == nil {
		t.Fatal("text/plain content type accepted")
	}
	if err.Kind != RequestTokenErrorOther || !strings.Contains(err.Error(), "text/plain") {
		t.Fatalf("got kind %v error %v, want other error naming text/plain", err.Kind, err)
	}
}

// F2P-064 / F2P-066 / F2P-071 (R015): a 200 JSON token response deserializes
// every documented field.
func TestProbeR015SuccessfulTokenDeserialization(t *testing.T) {
	request, reqErr := newProbeTokenClient(t).ExchangeClientCredentials()
	if reqErr != nil {
		t.Fatalf("ExchangeClientCredentials: %v", reqErr)
	}
	body := `{"access_token":"at","token_type":"bearer","expires_in":3600,"refresh_token":"rt","scope":"a b","extra":"x"}`
	capture := &probeCaptureClient{t: t, response: probeStatusBody(200, body)}
	response, err := request.Request(capture)
	if err != nil {
		t.Fatalf("Request: %v", err)
	}
	if response.AccessToken().Secret() != "at" {
		t.Fatalf("access token: %q", response.AccessToken().Secret())
	}
	if expires, ok := response.ExpiresIn(); !ok || expires != 3600 {
		t.Fatalf("expires_in: %v %v", expires, ok)
	}
	if response.RefreshToken() == nil || response.RefreshToken().Secret() != "rt" {
		t.Fatal("refresh token not parsed")
	}
	if len(response.Scopes()) != 2 || response.Scopes()[0].AsStr() != "a" || response.Scopes()[1].AsStr() != "b" {
		t.Fatalf("scopes: %#v", response.Scopes())
	}
}

// F2P-068 / F2P-070 (R016): non-200 responses map to server-response, parse and
// other errors based on body parseability and emptiness.
func TestProbeR016Non200ErrorMapping(t *testing.T) {
	request, reqErr := newProbeTokenClient(t).ExchangeClientCredentials()
	if reqErr != nil {
		t.Fatalf("ExchangeClientCredentials: %v", reqErr)
	}

	parsed := &probeCaptureClient{t: t, response: probeStatusBody(400, `{"error":"invalid_grant"}`)}
	if _, err := request.Request(parsed); err == nil || err.Kind != RequestTokenErrorServerResponse {
		t.Fatalf("parseable body: got %v", err)
	}

	unparseable := &probeCaptureClient{t: t, response: probeStatusBody(400, `not-json`)}
	if _, err := request.Request(unparseable); err == nil || err.Kind != RequestTokenErrorParse || string(err.RawBody) != "not-json" {
		t.Fatalf("unparseable body: got %v", err)
	}

	empty := &probeCaptureClient{t: t, response: HTTPResponse{StatusCode: 400, Header: map[string]string{}}}
	if _, err := request.Request(empty); err == nil || err.Kind != RequestTokenErrorOther {
		t.Fatalf("empty body: got %v", err)
	}
}

// F2P-067 / F2P-068 / F2P-071 (R043): the caller-supplied HTTP client is the one
// invoked; a fixed internal net/http client would ignore injection.
func TestProbeR043HTTPClientInjection(t *testing.T) {
	request, reqErr := newProbeTokenClient(t).ExchangeClientCredentials()
	if reqErr != nil {
		t.Fatalf("ExchangeClientCredentials: %v", reqErr)
	}
	calls := 0
	injected := SyncHTTPClientFunc(func(HTTPRequest) (HTTPResponse, error) {
		calls++
		return probeStatusBody(200, `{"access_token":"t","token_type":"bearer"}`), nil
	})
	if _, err := request.Request(injected); err != nil {
		t.Fatalf("Request: %v", err)
	}
	if calls != 1 {
		t.Fatalf("injected client calls: got %d, want 1", calls)
	}
}

// F2P-072 (R009): AddExtraParam appends the supplied name/value to every request
// type's outgoing request.
func TestProbeR009ExtraParams(t *testing.T) {
	client := newProbeTokenClient(t)

	codeRequest, err := client.ExchangeCode(NewAuthorizationCode("code"))
	if err != nil {
		t.Fatalf("ExchangeCode: %v", err)
	}
	assertProbeBodyHas(t, migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := codeRequest.AddExtraParam("extra", "1").Request(httpClient); return failure }), "extra=1")

	credentialsRequest, err := client.ExchangeClientCredentials()
	if err != nil {
		t.Fatalf("ExchangeClientCredentials: %v", err)
	}
	assertProbeBodyHas(t, migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := credentialsRequest.AddExtraParam("extra", "1").Request(httpClient); return failure }), "extra=1")

	passwordRequest, err := client.ExchangePassword(ptrProbeString(NewResourceOwnerUsername("user")), ptrProbeString(NewResourceOwnerPassword("pass")))
	if err != nil {
		t.Fatalf("ExchangePassword: %v", err)
	}
	assertProbeBodyHas(t, migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := passwordRequest.AddExtraParam("extra", "1").Request(httpClient); return failure }), "extra=1")

	refreshRequest, err := client.ExchangeRefreshToken(ptrProbeString(NewRefreshToken("rt")))
	if err != nil {
		t.Fatalf("ExchangeRefreshToken: %v", err)
	}
	assertProbeBodyHas(t, migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := refreshRequest.AddExtraParam("extra", "1").Request(httpClient); return failure }), "extra=1")

	deviceClient := newProbeTokenClient(t)
	deviceClient.SetDeviceAuthorizationURL(migMustDeviceAuthURL(t, "https://example.com/device"))
	deviceRequest, err := deviceClient.ExchangeDeviceCode()
	if err != nil {
		t.Fatalf("ExchangeDeviceCode: %v", err)
	}
	assertProbeBodyHas(t, migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := deviceRequest.AddExtraParam("extra", "1").Request(httpClient); return failure }), "extra=1")

	introspectionClient := newProbeTokenClient(t)
	introspectionClient.SetIntrospectionURL(migMustIntrospectionURL(t, "https://example.com/introspect"))
	introspectionRequest, err := introspectionClient.Introspect(ptrProbeString(NewAccessToken("at")))
	if err != nil {
		t.Fatalf("Introspect: %v", err)
	}
	assertProbeBodyHas(t, migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { _, failure := introspectionRequest.AddExtraParam("extra", "1").Request(httpClient); return failure }), "extra=1")

	revocationClient := newProbeTokenClient(t)
	revocationClient.SetRevocationURL(migMustRevocationURL(t, "https://example.com/revoke"))
	revocationRequest, err := revocationClient.RevokeToken(NewStandardRevocableTokenFromAccessToken(NewAccessToken("at")))
	if err != nil {
		t.Fatalf("RevokeToken: %v", err)
	}
	assertProbeBodyHas(t, migCaptureRequest(t, func(httpClient SyncHTTPClient) *RequestTokenError { return revocationRequest.AddExtraParam("extra", "1").Request(httpClient) }), "extra=1")
}

// F2P-056 (R004): NewBasicClient defaults to BasicAuth and every endpoint setter
// round-trips through its accessor.
func TestProbeR004ClientConstructionAndSetters(t *testing.T) {
	client := NewBasicClient(NewClientId("id"))
	if client.AuthType() != BasicAuth {
		t.Fatalf("default AuthType: got %v, want BasicAuth", client.AuthType())
	}
	client.SetClientSecret(NewClientSecret("secret"))
	client.SetAuthURL(migMustAuthURL(t, "https://example.com/auth"))
	client.SetTokenURL(migMustTokenURL(t, "https://example.com/token"))
	client.SetRedirectURL(migMustRedirectURL(t, "https://example.com/cb"))
	client.SetDeviceAuthorizationURL(migMustDeviceAuthURL(t, "https://example.com/device"))
	client.SetIntrospectionURL(migMustIntrospectionURL(t, "https://example.com/introspect"))
	client.SetRevocationURL(migMustRevocationURL(t, "https://example.com/revoke"))

	if client.AuthURI() == nil || client.AuthURI().String() != "https://example.com/auth" {
		t.Fatalf("AuthURI: %v", client.AuthURI())
	}
	if client.TokenURI() == nil || client.TokenURI().String() != "https://example.com/token" {
		t.Fatalf("TokenURI: %v", client.TokenURI())
	}
	if client.RedirectURI() == nil || client.RedirectURI().String() != "https://example.com/cb" {
		t.Fatalf("RedirectURI: %v", client.RedirectURI())
	}
	if client.DeviceAuthorizationURL() == nil || client.DeviceAuthorizationURL().String() != "https://example.com/device" {
		t.Fatalf("DeviceAuthorizationURL: %v", client.DeviceAuthorizationURL())
	}
	if client.IntrospectionURL() == nil || client.IntrospectionURL().String() != "https://example.com/introspect" {
		t.Fatalf("IntrospectionURL: %v", client.IntrospectionURL())
	}
	if client.RevocationURL() == nil || client.RevocationURL().String() != "https://example.com/revoke" {
		t.Fatalf("RevocationURL: %v", client.RevocationURL())
	}
}

// F2P-053 / F2P-057 (R027): the out-of-scope Rust components have no Go
// migration counterpart; the migrated Go package must not reference them.
func TestProbeR027OutOfScopeComponentsUnmigrated(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		// Probe may run from the module root; walk down to the package.
		entries, err = os.ReadDir("oauth2")
		if err != nil {
			t.Fatalf("read package dir: %v", err)
		}
	}
	forbidden := []string{"reqwest", "curl_client", "ureq", "wasm32"}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") {
			continue
		}
		data, err := os.ReadFile(filepath.Clean(name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		lower := strings.ToLower(string(data))
		for _, needle := range forbidden {
			if strings.Contains(lower, needle) {
				// The migration must not port the out-of-scope transports.
				if name == "f2p_probes_test.go" {
					continue
				}
				t.Fatalf("out-of-scope component %q referenced in %s", needle, name)
			}
		}
	}
}

// F2P-049 (R031/R030): the authorization URL preserves insertion ordering; the
// default code flow emits response_type, client_id, state in that order.
func TestProbeR031AuthorizationURLOrdering(t *testing.T) {
	client := NewBasicClient(NewClientId("client")).SetAuthURL(migMustAuthURL(t, "https://auth.example/authorize"))
	request, err := client.AuthorizeURL(func() CsrfToken { return NewCsrfToken("xyz") })
	if err != nil {
		t.Fatalf("AuthorizeURL: %v", err)
	}
	url, _ := request.URL()
	if url.RawQuery != "response_type=code&client_id=client&state=xyz" {
		t.Fatalf("RawQuery: got %q", url.RawQuery)
	}
	url2, _ := request.AddScope(NewScope("a b")).URL()
	if url2.RawQuery != "response_type=code&client_id=client&state=xyz&scope=a+b" {
		t.Fatalf("RawQuery with scope: got %q", url2.RawQuery)
	}
}

// F2P-041 (R041): an absent device interval defaults to 5s; a present interval is
// honoured.
func TestProbeR041DeviceIntervalDefault(t *testing.T) {
	var absent StandardDeviceAuthorizationResponse
	if err := json.Unmarshal([]byte(`{"device_code":"dc","user_code":"uc","verification_uri":"https://example.com/verify","expires_in":1800}`), &absent); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if absent.Interval() != 5*time.Second {
		t.Fatalf("default interval: got %v, want 5s", absent.Interval())
	}
	var present StandardDeviceAuthorizationResponse
	if err := json.Unmarshal([]byte(`{"device_code":"dc","user_code":"uc","verification_uri":"https://example.com/verify","expires_in":1800,"interval":10}`), &present); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if present.Interval() != 10*time.Second {
		t.Fatalf("present interval: got %v, want 10s", present.Interval())
	}
}

func newProbeTokenClient(t *testing.T) *BasicClient {
	t.Helper()
	return NewBasicClient(NewClientId("probe-client")).
		SetClientSecret(NewClientSecret("probe-secret")).
		SetTokenURL(migMustTokenURL(t, "https://example.com/token"))
}

func ptrProbeString[T any](v T) *T { return &v }

func assertProbeBodyHas(t *testing.T, request HTTPRequest, needle string) {
	t.Helper()
	if !strings.Contains(string(request.Body), needle) {
		t.Fatalf("request body %q does not contain %q", request.Body, needle)
	}
}

var _ = fmt.Sprintf
var _ = reflect.DeepEqual
