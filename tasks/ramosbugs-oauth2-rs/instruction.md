# Migrate the oauth2 client library from Rust to Go

## Background

The repository contains the `oauth2` library (version 5.1.0, Rust edition 2021), an extensible, strongly-typed OAuth2 client. It implements OAuth2 (RFC 6749), token introspection (RFC 7662), token revocation (RFC 7009), and the device authorization flow (RFC 8628).

The library separates protocol logic from HTTP transport behind synchronous and asynchronous client abstractions. Callers may inject their own HTTP client.

This task re-implements the library's protocol logic in Go while preserving its public behavior.

## Goal

- Migrate the `oauth2` library's public protocol behavior from Rust (edition 2021) to Go 1.22.
- The Go implementation MUST provide client assembly, the OAuth2 flow requests, token introspection, token revocation, request and response handling, the HTTP client abstraction, and the error contract described below.
- The Go implementation MUST implement the protocol logic itself using the Go standard library.
- Because Go has no compile-time typestate, endpoint completeness MUST be validated at runtime and MUST surface as the missing-URL configuration error.
- Out of scope: the `oauth2-reqwest` crate, the built-in Rust HTTP transports, wasm32/browser targets, and all client UI.

## Public contract to preserve

- **Client assembly.** Construct a client from a client ID; the default client authentication is BasicAuth. Setters MUST configure the authorization, token, redirect, device authorization, introspection, and revocation endpoints, and the client secret. Conditional setters MUST accept an optional endpoint and produce fallible accessors.
- **Requests.** Flow requests MUST be HTTP POST requests whose bodies are form-encoded (`application/x-www-form-urlencoded`) and MUST send `Accept: application/json`.
- **Client authentication.** When authentication is BasicAuth and a client secret is set, the request MUST carry `Authorization: Basic base64(urlencode(client_id):urlencode(client_secret))`. Otherwise `client_id` MUST be placed in the form body, together with `client_secret` when one is set.
- **Flows.** Support client credentials, authorization code (with an optional PKCE verifier and redirect URL override), refresh token (with optional scopes), resource owner password, device authorization, and device access token requests.
- **Authorization URL.** MUST include `response_type`, `client_id`, and `state`; MUST include `code_challenge` and `code_challenge_method` only with a PKCE challenge; `redirect_uri` only when a redirect URL is set; `scope` (space-joined) only when scopes are present; and appended extra parameters. The code flow is the default, the implicit flow uses `response_type=token`, and a custom response type MUST be supported.
- **Introspection.** Send `token`, an optional `token_type_hint`, and extra parameters, and parse the introspection response.
- **Revocation.** Revoke a token identified by its secret and optional type hint. The revocation URL MUST be HTTPS; otherwise a configuration error MUST be returned.
- **Extra parameters.** Every request type MUST allow appending arbitrary extra parameters.
- **HTTP client abstraction.** Synchronous and asynchronous clients MUST expose one call operation taking a request (method, URL, headers, body bytes) and returning a response (status, headers, body bytes) or an error. Callers MUST be able to inject a custom client.
- **Error contract.** Distinguish a parsed server error response, a transport/request failure, a response parse failure carrying the raw body, and other errors. Provide a configuration error for a missing endpoint URL and for a non-HTTPS revocation URL. Provide an HTTP client error for backend, HTTP, I/O, and other failures.
- **Secret handling.** Secret-bearing values (client secret, tokens, authorization code, PKCE verifier, CSRF state, device and user codes) MUST redact their content in formatted output and MUST expose a secret accessor. Concretely, every secret-bearing type — `ClientSecret`, `AccessToken`, `RefreshToken`, `AuthorizationCode`, `PkceCodeVerifier`, `CsrfToken`, `ResourceOwnerPassword`, `DeviceCode`, `UserCode`, `VerificationUriComplete` — MUST render as `<TypeName>([redacted])` for the `%v`, `%s` and `%#v` verbs (`fmt.Sprintf("%v", NewClientSecret("x"))` MUST equal `"ClientSecret([redacted])"`, `fmt.Sprintf("%v", NewAccessToken("x"))` MUST equal `"AccessToken([redacted])"`, and so on), and its secret accessor MUST return the underlying value unchanged.
- **Standard error responses.** MUST carry `error` (required), `error_description` (optional), and `error_uri` (optional). Token-type values MUST parse case-insensitively and serialize to their canonical strings. Error-type values MUST serialize to their canonical strings.

The library's existing Rust unit tests and doctests under `oauth2/src` exercise these behaviors with an injected fake HTTP client and define the expected requests, responses, and errors. The Go implementation MUST reproduce the same observable scenarios.

## Go public API contract

The Go module is named `oauth2` and the migrated package is `oauth2`, located in the `oauth2/` directory (import path `oauth2/oauth2`, `go 1.22`). Go has no compile-time typestate, so a single generic `Client` plus runtime endpoint validation replaces the Rust builder types. Preserve the following exported names: a caller written against this contract must keep compiling without editing call sites.

```go
// --- client configuration -------------------------------------------------
type AuthType int
const ( BasicAuth AuthType = iota; RequestBody ) // BasicAuth is the default

type ClientId struct{ /* ... */ };     func NewClientId(v string) ClientId
type ClientSecret struct{ /* ... */ }; func NewClientSecret(v string) ClientSecret

type AuthUrl struct{ /* ... */ };                func NewAuthUrl(raw string) (*AuthUrl, error)
type TokenUrl struct{ /* ... */ };               func NewTokenUrl(raw string) (*TokenUrl, error)
type RedirectUrl struct{ /* ... */ };            func NewRedirectUrl(raw string) (*RedirectUrl, error)
type DeviceAuthorizationUrl struct{ /* ... */ }; func NewDeviceAuthorizationUrl(raw string) (*DeviceAuthorizationUrl, error)
type IntrospectionUrl struct{ /* ... */ };       func NewIntrospectionUrl(raw string) (*IntrospectionUrl, error)
type RevocationUrl struct{ /* ... */ };          func NewRevocationUrl(raw string) (*RevocationUrl, error)
type EndUserVerificationUrl struct{ /* ... */ }; func NewEndUserVerificationUrl(raw string) (EndUserVerificationUrl, error)
// Every *Url above exposes URL() *url.URL and String() string.

type Client[TE ErrorResponse, TR TokenResponse, TIR TokenIntrospectionResponse, RT RevocableToken, TRE ErrorResponse] struct{ /* ... */ }
type BasicClient = Client[BasicErrorResponse, BasicTokenResponse, BasicTokenIntrospectionResponse, StandardRevocableToken, BasicRevocationErrorResponse]

func NewClient[TE ErrorResponse, TR TokenResponse, TIR TokenIntrospectionResponse, RT RevocableToken, TRE ErrorResponse](clientID ClientId) *Client[TE, TR, TIR, RT, TRE]
func NewBasicClient(clientID ClientId) *BasicClient

// Setters (chainable; the *Option variants accept an optional endpoint):
//   SetAuthType(AuthType)                       SetClientSecret(ClientSecret)
//   SetAuthURL(*AuthUrl)         SetAuthURLOption(*AuthUrl)
//   SetTokenURL(*TokenUrl)       SetTokenURLOption(*TokenUrl)
//   SetRedirectURL(*RedirectUrl)
//   SetDeviceAuthorizationURL(*DeviceAuthorizationUrl)  SetDeviceAuthorizationURLOption(*DeviceAuthorizationUrl)
//   SetIntrospectionURL(*IntrospectionUrl)      SetIntrospectionURLOption(*IntrospectionUrl)
//   SetRevocationURL(*RevocationUrl)            SetRevocationURLOption(*RevocationUrl)
// Accessors:
//   ClientId() ClientId   AuthType() AuthType
//   AuthURI() *AuthUrl    TokenURI() *TokenUrl    RedirectURI() *RedirectUrl
//   DeviceAuthorizationURL() *DeviceAuthorizationUrl
//   IntrospectionURL() *IntrospectionUrl          RevocationURL() *RevocationUrl

// Flow accessors (all fallible with *ConfigurationError when an endpoint is unset):
//   AuthorizeURL(stateFn func() CsrfToken) (*AuthorizationRequest, error)
//   ExchangeCode(AuthorizationCode) (*CodeTokenRequest[TE, TR], error)
//   ExchangeClientCredentials() (*ClientCredentialsTokenRequest[TE, TR], error)
//   ExchangePassword(*ResourceOwnerUsername, *ResourceOwnerPassword) (*PasswordTokenRequest[TE, TR], error)
//   ExchangeRefreshToken(*RefreshToken) (*RefreshTokenRequest[TE, TR], error)
//   ExchangeDeviceCode() (*DeviceAuthorizationRequest[TE], error)
//   ExchangeDeviceAccessToken(DeviceAuthorizationResponseLike) (*DeviceAccessTokenRequest[TR], error)
//   Introspect(*AccessToken) (*IntrospectionRequest[TE, TIR], error)
//   RevokeToken(RT) (*RevocationRequest[RT, TRE], error)

// --- requests -------------------------------------------------------------
// AuthorizationRequest: AddScope(Scope) / AddScopes([]Scope) / AddExtraParam(name, value string)
//   / UseImplicitFlow() / SetResponseType(ResponseType) / SetPkceChallenge(PkceCodeChallenge)
//   / SetRedirectURI(RedirectUrl) ; URL() (*url.URL, CsrfToken)
// Token requests (CodeTokenRequest, ClientCredentialsTokenRequest, PasswordTokenRequest,
// RefreshTokenRequest): AddExtraParam(name, value string); token requests also AddScope/
// AddScopes; CodeTokenRequest also SetPkceVerifier(PkceCodeVerifier) and SetRedirectURI(RedirectUrl);
//   Request(SyncHTTPClient) (TR, *RequestTokenError)
//   RequestAsync(context.Context, AsyncHTTPClient) (TR, *RequestTokenError)
// DeviceAuthorizationRequest: AddExtraParam / AddScope / AddScopes /
//   Request(SyncHTTPClient) (*StandardDeviceAuthorizationResponse, *RequestTokenError) / RequestAsync(...)
// DeviceAccessTokenRequest: AddExtraParam / SetTimeFunc(func() time.Time) / SetMaxBackoffInterval(time.Duration) /
//   Request(SyncHTTPClient, sleepFn func(time.Duration), timeout *time.Duration) (TR, *RequestTokenError) / RequestAsync(...)
// IntrospectionRequest: AddExtraParam / SetTokenTypeHint(string) /
//   Request(SyncHTTPClient) (TIR, *RequestTokenError) / RequestAsync(...)
// RevocationRequest: AddExtraParam / Request(SyncHTTPClient) *RequestTokenError / RequestAsync(...)

// --- responses ------------------------------------------------------------
type StandardTokenResponse[EF ExtraTokenFields, TT TokenType] struct{ /* ... */ }
type BasicTokenResponse = StandardTokenResponse[EmptyExtraTokenFields, BasicTokenType]
func NewStandardTokenResponse[EF ExtraTokenFields, TT TokenType](accessToken AccessToken, tokenType TT, extraFields EF) *StandardTokenResponse[EF, TT]
//   AccessToken() AccessToken   TokenTypeValue() TokenType   ExpiresIn() (uint64, bool)
//   RefreshToken() *RefreshToken   Scopes() []Scope   ExtraFields() EF
//   SetAccessToken / SetTokenType / SetExpiresIn(uint64) / SetRefreshToken(*RefreshToken) / SetScopes([]Scope) / SetExtraFields

type StandardTokenIntrospectionResponse[EF ExtraTokenFields, TT TokenType] struct{ /* ... */ }
type BasicTokenIntrospectionResponse = StandardTokenIntrospectionResponse[EmptyExtraTokenFields, BasicTokenType]
//   Active() bool   Scopes() []Scope   ClientId() *ClientId   Username() *string
//   TokenType() (TokenType, bool)   Exp()/Iat()/Nbf() *int64
//   Sub()/Iss()/Jti() *string   Aud() []string   ExtraFields() EF

type DeviceAuthorizationResponse[EF ExtraDeviceAuthorizationFields] struct{ /* ... */ }
type StandardDeviceAuthorizationResponse = DeviceAuthorizationResponse[EmptyExtraDeviceAuthorizationFields]
type DeviceAuthorizationResponseLike interface {
	DeviceCode() DeviceCode
	Interval() time.Duration
	ExpiresIn() time.Duration
}
//   DeviceCode() DeviceCode   UserCode() UserCode   VerificationURI() EndUserVerificationUrl
//   VerificationURIComplete() *VerificationUriComplete   ExpiresIn()/Interval() time.Duration   ExtraFields() EF

type StandardErrorResponse[T ErrorResponseType] struct {
	Error            T       `json:"error"`
	ErrorDescription *string `json:"error_description,omitempty"`
	ErrorURI         *string `json:"error_uri,omitempty"`
}
func NewStandardErrorResponse[T ErrorResponseType](err T, description, uri *string) StandardErrorResponse[T]
//   ErrorType() T   ErrorString() string
type BasicErrorResponse = StandardErrorResponse[BasicErrorResponseType]
type BasicRevocationErrorResponse = StandardErrorResponse[RevocationErrorResponseType]
type DeviceCodeErrorResponse = StandardErrorResponse[DeviceCodeErrorResponseType]

type StandardRevocableToken struct{ /* ... */ }
func NewStandardRevocableTokenFromAccessToken(AccessToken) StandardRevocableToken
func NewStandardRevocableTokenFromRefreshToken(RefreshToken) StandardRevocableToken
//   Secret() string   TypeHint() string

// --- error contract -------------------------------------------------------
type ConfigurationErrorKind int
const ( ConfigurationMissingURL ConfigurationErrorKind = iota; ConfigurationInsecureURL )
type ConfigurationError struct { Kind ConfigurationErrorKind; Endpoint string }
//   Error(): "No <endpoint> endpoint URL specified" or "Scheme for <endpoint> endpoint URL must be HTTPS"

type RequestTokenErrorKind int
const ( RequestTokenErrorServerResponse RequestTokenErrorKind = iota; RequestTokenErrorRequest; RequestTokenErrorParse; RequestTokenErrorOther )
type RequestTokenError struct {
	Kind       RequestTokenErrorKind
	Response   any    // parsed server error response (RequestTokenErrorServerResponse)
	RequestErr error  // underlying transport error (RequestTokenErrorRequest)
	ParseErr   error  // underlying parse error (RequestTokenErrorParse)
	RawBody    []byte // raw response body on a parse failure
	OtherMsg   string
}
type BasicRequestTokenError = RequestTokenError

type HTTPClientErrorKind int
const ( HTTPClientErrorBackend HTTPClientErrorKind = iota; HTTPClientErrorHTTP; HTTPClientErrorIO; HTTPClientErrorOther )
type HTTPClientError struct { Kind HTTPClientErrorKind; Err error; Msg string }

// --- HTTP client abstraction ---------------------------------------------
type HTTPRequest  struct { Method, URL string; Header map[string]string; Body []byte }
type HTTPResponse struct { StatusCode int; Header map[string]string; Body []byte }
type SyncHTTPClient interface { Call(HTTPRequest) (HTTPResponse, error) }
type AsyncHTTPClient interface { Call(context.Context, HTTPRequest) (HTTPResponse, error) }
type SyncHTTPClientFunc func(HTTPRequest) (HTTPResponse, error)
type AsyncHTTPClientFunc func(context.Context, HTTPRequest) (HTTPResponse, error)

// --- secret-bearing values, scopes, PKCE ----------------------------------
// Each secret newtype exposes Secret() string and redacts its content in
// formatted output (%v, %s, %#v):
// ClientSecret, AccessToken, RefreshToken, AuthorizationCode, PkceCodeVerifier,
// CsrfToken, DeviceCode, UserCode, VerificationUriComplete, ResourceOwnerPassword.
// Scalar newtypes: ClientId, ResourceOwnerUsername, Scope (AsStr() string), ResponseType (String() string).
func SecretsEqual(a, b string) bool

type PkceCodeChallengeMethod struct{ /* ... */ } // String() string ("S256" or "plain")
type PkceCodeChallenge struct{ /* ... */ }       // AsStr() string; ChallengeMethod() PkceCodeChallengeMethod
func NewPkceCodeChallengeFromVerifierSha256(PkceCodeVerifier) PkceCodeChallenge
func NewPkceCodeChallengeFromVerifierPlain(PkceCodeVerifier) PkceCodeChallenge
func NewPkceCodeChallengeRandomSha256() (PkceCodeChallenge, PkceCodeVerifier)
func NewPkceCodeChallengeRandomPlain() (PkceCodeChallenge, PkceCodeVerifier)

// --- extension points (user-implementable) --------------------------------
type ErrorResponseType interface { ErrorTypeString() string }
type ErrorResponse     interface { ErrorString() string }
type TokenType         interface { TokenTypeString() string }
type ExtraTokenFields  interface { IsExtraTokenFields() }
type TokenResponse     interface { AccessToken() AccessToken; TokenTypeValue() TokenType; ExpiresIn() (uint64, bool); RefreshToken() *RefreshToken; Scopes() []Scope }
type TokenIntrospectionResponse interface {
	Active() bool; Scopes() []Scope; ClientId() *ClientId; Username() *string
	TokenType() (TokenType, bool); Exp() *int64; Iat() *int64; Nbf() *int64
	Sub() *string; Aud() []string; Iss() *string; Jti() *string
}
type RevocableToken    interface { Secret() string; TypeHint() string }
type ExtraDeviceAuthorizationFields interface { IsExtraDeviceAuthorizationFields() }

// --- canonical enum values ------------------------------------------------
type BasicTokenType string
const ( BasicTokenTypeBearer BasicTokenType = "bearer"; BasicTokenTypeMac BasicTokenType = "mac" )
//   (BasicTokenType).TokenTypeString() string
type BasicErrorResponseType string
const (
	BasicErrorResponseTypeInvalidClient        BasicErrorResponseType = "invalid_client"
	BasicErrorResponseTypeInvalidGrant         BasicErrorResponseType = "invalid_grant"
	BasicErrorResponseTypeInvalidRequest       BasicErrorResponseType = "invalid_request"
	BasicErrorResponseTypeInvalidScope         BasicErrorResponseType = "invalid_scope"
	BasicErrorResponseTypeUnauthorizedClient   BasicErrorResponseType = "unauthorized_client"
	BasicErrorResponseTypeUnsupportedGrantType BasicErrorResponseType = "unsupported_grant_type"
)
//   (BasicErrorResponseType).ErrorTypeString() string
type RevocationErrorResponseType string
const RevocationErrorResponseTypeUnsupportedTokenType RevocationErrorResponseType = "unsupported_token_type"
type DeviceCodeErrorResponseType string
const (
	DeviceCodeErrorResponseTypeAuthorizationPending DeviceCodeErrorResponseType = "authorization_pending"
	DeviceCodeErrorResponseTypeSlowDown             DeviceCodeErrorResponseType = "slow_down"
	DeviceCodeErrorResponseTypeAccessDenied         DeviceCodeErrorResponseType = "access_denied"
	DeviceCodeErrorResponseTypeExpiredToken         DeviceCodeErrorResponseType = "expired_token"
)
```

Keep the generic type parameters in the `Client` alias in the order shown (`ErrorResponse`, `TokenResponse`, `TokenIntrospectionResponse`, `RevocableToken`, `ErrorResponse`) so the `BasicClient` alias and caller-specialized clients keep compiling. The Go package MUST compile with only the standard library and MUST declare no third-party `require` directives.

## Behavior requirements

- If an endpoint required for a request is not set, the client MUST return the missing-URL configuration error identifying the endpoint.
- If a token or introspection endpoint returns status 200 with a non-empty JSON body, the client MUST deserialize it into the corresponding response type, including the access token, token type, optional `expires_in` seconds, optional refresh token, optional space-delimited `scope`, and extra fields.
- If the status is not 200 and the body is non-empty, the client MUST parse the body into the error response and return a server-response error. If parsing fails, the client MUST return a parse error carrying the raw body. If the body is empty, the client MUST return a generic other error.
- If the response `Content-Type` is present and is not `application/json` (compared case-insensitively and ignoring parameters), the client MUST return an other error naming the unexpected content type. If the body is empty, the client MUST return an other error reporting an empty body.
- For a revocation response with status `200`, the client MUST ignore the response body and `Content-Type` entirely: it MUST NOT parse, deserialize or validate the body. `RevokeToken` MUST return a nil error even when the body is empty, non-JSON, or otherwise unparseable (for example a `200` response with `Content-Type: application/octet-stream` and body `\x01\x02\x03` MUST still succeed). A non-`200` revocation response MUST follow the standard error path described above: an unexpected `Content-Type` produces an other error naming that content type; otherwise an empty body produces an other error, a non-empty valid JSON error response produces a server-response error, and a body that cannot be parsed into the error response produces a parse error carrying the raw body. The body and `Content-Type` exception applies only to status `200`.
- If a revocation endpoint URL is not HTTPS, the client MUST return the insecure-URL configuration error before sending any request.
- When polling the device access token endpoint, the client MUST wait by calling the caller-supplied sleep function between polls. The initial interval MUST be the device authorization response `interval`, defaulting to 5 seconds. On `authorization_pending` the client MUST retry with the same interval; on `slow_down` it MUST add 5 seconds; on a transport error it MUST back off exponentially up to a cap (default 10 seconds, overridable by the caller). When the timeout elapses it MUST return an `expired_token` server-response error.
- Token-type values MUST deserialize case-insensitively (for example `BEARER` and `bearer` both yield the bearer token type) and MUST serialize to their canonical strings.
- The authorization URL MUST match the parameter set, encoding, and ordering asserted by the baseline tests for the same input.

## Constraints

1. The migration MUST NOT remove public capabilities or change the OAuth2 protocol semantics of RFC 6749, RFC 7662, RFC 7009, or RFC 8628.
2. Production Go code MUST NOT delegate the migrated responsibilities to the Rust runtime through subprocesses, cgo/FFI, RPC, or dynamic loading.
3. The implementation MUST NOT introduce a third-party OAuth2 library; the protocol logic MUST be written in Go.
4. Changes MUST be limited to the migrated `oauth2` library sources and their supporting files.
5. The `oauth2-reqwest` crate, the built-in Rust HTTP transports, wasm32/browser targets, and client UI are out of scope and MUST NOT be migrated.
6. Existing public endpoint configuration and error contract names MUST NOT be changed; rebuild equivalent behavior with Go idioms.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
8. Work inside the repository at `/workspace/repo`, and save all implementation as files in that repository so the evaluator can collect the changes.

## Validation

From `/workspace/repo`:

- The existing Rust suite documents and exercises the reference behavior: `cargo test --tests --examples`, `cargo test --doc`, and `cargo test --all-features`.
- Once the Go implementation is in place, run `go build ./...` and `go test ./...` to confirm the Go package compiles and its tests pass.

These checks cover compilation and the exercised scenarios only; they do not by themselves satisfy every requirement.
