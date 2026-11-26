package oauth2_test

// Evaluator-owned helpers use only the exported contract. Keeping the probes
// in an external package allows candidates to choose their own private layout.

import (
	"encoding/json"
	"fmt"
	"reflect"
	"testing"

	. "oauth2/oauth2"
)

type migMockHTTPClient struct {
	t           *testing.T
	wantMethod  string
	wantURL     string
	wantHeaders map[string]string
	wantBody    string
	response    HTTPResponse
	responseErr error
}

func (m *migMockHTTPClient) Call(request HTTPRequest) (HTTPResponse, error) {
	if request.Method != m.wantMethod {
		m.t.Errorf("method: got %q, want %q", request.Method, m.wantMethod)
	}
	if request.URL != m.wantURL {
		m.t.Errorf("url: got %q, want %q", request.URL, m.wantURL)
	}
	if !reflect.DeepEqual(request.Header, m.wantHeaders) {
		m.t.Errorf("headers: got %#v, want %#v", request.Header, m.wantHeaders)
	}
	if string(request.Body) != m.wantBody {
		m.t.Errorf("body: got %q, want %q", string(request.Body), m.wantBody)
	}
	if m.responseErr != nil {
		return HTTPResponse{}, m.responseErr
	}
	return m.response, nil
}

func migNewHTTPResponse(status int, header map[string]string, body string) HTTPResponse {
	if header == nil {
		header = map[string]string{}
	}
	return HTTPResponse{StatusCode: status, Header: header, Body: []byte(body)}
}

func migJsonHeader() map[string]string {
	return map[string]string{"Content-Type": "application/json"}
}

func migBasicAuthHeaders() map[string]string {
	return map[string]string{
		"Accept":        "application/json",
		"Content-Type":  "application/x-www-form-urlencoded",
		"Authorization": "Basic YWFhOmJiYg==",
	}
}

func migNewTestClient(t *testing.T) *BasicClient {
	t.Helper()
	authURL, err := NewAuthUrl("https://example.com/auth")
	if err != nil {
		t.Fatal(err)
	}
	tokenURL, err := NewTokenUrl("https://example.com/token")
	if err != nil {
		t.Fatal(err)
	}
	return NewBasicClient(NewClientId("aaa")).
		SetAuthURL(authURL).
		SetTokenURL(tokenURL).
		SetClientSecret(NewClientSecret("bbb"))
}

func migMustTokenURL(t *testing.T, raw string) *TokenUrl {
	t.Helper()
	u, err := NewTokenUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func migMustAuthURL(t *testing.T, raw string) *AuthUrl {
	t.Helper()
	u, err := NewAuthUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func migMustRedirectURL(t *testing.T, raw string) *RedirectUrl {
	t.Helper()
	u, err := NewRedirectUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func migMustIntrospectionURL(t *testing.T, raw string) *IntrospectionUrl {
	t.Helper()
	u, err := NewIntrospectionUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func migMustRevocationURL(t *testing.T, raw string) *RevocationUrl {
	t.Helper()
	u, err := NewRevocationUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func migMustDeviceAuthURL(t *testing.T, raw string) *DeviceAuthorizationUrl {
	t.Helper()
	u, err := NewDeviceAuthorizationUrl(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

// migCaptureRequest observes bytes sent through the public HTTP abstraction.
// The response includes valid fields for every flow so no private request
// preparation method or response representation is required by a probe.
func migCaptureRequest(t *testing.T, send func(SyncHTTPClient) *RequestTokenError) HTTPRequest {
	t.Helper()
	var request HTTPRequest
	calls := 0
	failure := send(SyncHTTPClientFunc(func(got HTTPRequest) (HTTPResponse, error) {
		calls++
		request = got
		return migNewHTTPResponse(200, migJsonHeader(), `{"access_token":"at","token_type":"bearer","active":true,"device_code":"dc","user_code":"uc","verification_uri":"https://example.com/verify","expires_in":1000,"interval":5}`), nil
	}))
	if failure != nil {
		t.Fatalf("request failed while capturing outgoing bytes: %v", failure)
	}
	if calls != 1 {
		t.Fatalf("injected HTTP client calls: got %d, want 1", calls)
	}
	return request
}

func migDeviceResponse(t *testing.T, expiresIn int) *StandardDeviceAuthorizationResponse {
	t.Helper()
	var response StandardDeviceAuthorizationResponse
	body := fmt.Sprintf(`{"device_code":"dc","user_code":"uc","verification_uri":"https://example.com/verify","expires_in":%d,"interval":5}`, expiresIn)
	if err := json.Unmarshal([]byte(body), &response); err != nil {
		t.Fatal(err)
	}
	return &response
}
