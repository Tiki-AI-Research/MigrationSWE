package oauth2

import (
	"fmt"
	"net/url"
)

// secret is the shared storage for secret-bearing newtypes. The concrete
// implementation redacts its content in every formatted representation and
// exposes the value only through Secret().
type secret struct{}

// Secret returns the underlying secret value.
func (s secret) Secret() string { panic("TODO") }

func (s secret) String() string { panic("TODO") }

// Format implements fmt.Formatter so %v/%s/%#v redact the secret.
func (s secret) Format(f fmt.State, verb rune) { panic("TODO") }

func (s secret) GoString() string { panic("TODO") }

func (s secret) MarshalJSON() ([]byte, error) { panic("TODO") }

func (s *secret) UnmarshalJSON(b []byte) error { panic("TODO") }

// ClientSecret is the client password issued to the client during registration.
type ClientSecret struct{ secret }

func NewClientSecret(v string) ClientSecret { panic("TODO") }

// AccessToken is a token returned by the token endpoint.
type AccessToken struct{ secret }

func NewAccessToken(v string) AccessToken { panic("TODO") }

// RefreshToken is used to obtain a new access token.
type RefreshToken struct{ secret }

func NewRefreshToken(v string) RefreshToken { panic("TODO") }

// AuthorizationCode is returned from the authorization endpoint.
type AuthorizationCode struct{ secret }

func NewAuthorizationCode(v string) AuthorizationCode { panic("TODO") }

// PkceCodeVerifier is the PKCE code verifier.
type PkceCodeVerifier struct{ secret }

func NewPkceCodeVerifier(v string) PkceCodeVerifier { panic("TODO") }

// CsrfToken is the value used for CSRF protection via the state parameter.
type CsrfToken struct{ secret }

func NewCsrfToken(v string) CsrfToken { panic("TODO") }

// NewCsrfTokenRandom generates a new random, base64-encoded 128-bit CSRF token.
func NewCsrfTokenRandom() CsrfToken { panic("TODO") }

// NewCsrfTokenRandomLen generates a random CSRF token of the specified byte length.
func NewCsrfTokenRandomLen(numBytes int) CsrfToken { panic("TODO") }

// ResourceOwnerPassword is the resource owner's password.
type ResourceOwnerPassword struct{ secret }

func NewResourceOwnerPassword(v string) ResourceOwnerPassword { panic("TODO") }

// DeviceCode is returned by the device authorization endpoint.
type DeviceCode struct{ secret }

func NewDeviceCode(v string) DeviceCode { panic("TODO") }

// UserCode is returned by the device authorization endpoint for the user.
type UserCode struct{ secret }

func NewUserCode(v string) UserCode { panic("TODO") }

// VerificationUriComplete is the verification URI including the user code.
type VerificationUriComplete struct{ secret }

func NewVerificationUriComplete(v string) VerificationUriComplete { panic("TODO") }

// SecretsEqual compares two secret strings in constant time.
func SecretsEqual(a, b string) bool { panic("TODO") }

// stringer is the shared storage for non-secret string newtypes.
type stringer struct{}

func (s stringer) String() string { panic("TODO") }

func (s stringer) MarshalJSON() ([]byte, error) { panic("TODO") }

func (s *stringer) UnmarshalJSON(b []byte) error { panic("TODO") }

// ClientId is the client identifier issued during registration.
type ClientId struct{ stringer }

func NewClientId(v string) ClientId { panic("TODO") }

// ResponseType is the authorization endpoint response (grant) type.
type ResponseType struct{ stringer }

func NewResponseType(v string) ResponseType { panic("TODO") }

// ResourceOwnerUsername is the resource owner's username.
type ResourceOwnerUsername struct{ stringer }

func NewResourceOwnerUsername(v string) ResourceOwnerUsername { panic("TODO") }

// Scope is an access token scope.
type Scope struct{ stringer }

func NewScope(v string) Scope { panic("TODO") }

// AsStr returns the scope as a string.
func (s Scope) AsStr() string { panic("TODO") }

// PkceCodeChallengeMethod is the method used for PKCE protection.
type PkceCodeChallengeMethod struct{ stringer }

func NewPkceCodeChallengeMethod(v string) PkceCodeChallengeMethod { panic("TODO") }

// PkceCodeChallenge is the code challenge used for PKCE protection.
type PkceCodeChallenge struct{}

// AsStr returns the PKCE code challenge as a string.
func (c PkceCodeChallenge) AsStr() string { panic("TODO") }

// ChallengeMethod returns the PKCE code challenge method.
func (c PkceCodeChallenge) ChallengeMethod() PkceCodeChallengeMethod { panic("TODO") }

// NewPkceCodeChallengeRandomSha256 generates a random SHA-256 PKCE challenge/verifier.
func NewPkceCodeChallengeRandomSha256() (PkceCodeChallenge, PkceCodeVerifier) { panic("TODO") }

// NewPkceCodeChallengeRandomSha256Len generates a random SHA-256 PKCE
// challenge/verifier using numBytes of random data.
func NewPkceCodeChallengeRandomSha256Len(numBytes int) (PkceCodeChallenge, PkceCodeVerifier) {
	panic("TODO")
}

// NewPkceCodeChallengeFromVerifierSha256 derives a SHA-256 PKCE code challenge.
func NewPkceCodeChallengeFromVerifierSha256(verifier PkceCodeVerifier) PkceCodeChallenge {
	panic("TODO")
}

// NewPkceCodeChallengeRandomPlain generates a random plain PKCE challenge/verifier.
func NewPkceCodeChallengeRandomPlain() (PkceCodeChallenge, PkceCodeVerifier) { panic("TODO") }

// NewPkceCodeChallengeFromVerifierPlain derives a plain PKCE code challenge.
func NewPkceCodeChallengeFromVerifierPlain(verifier PkceCodeVerifier) PkceCodeChallenge {
	panic("TODO")
}

// urlType is the shared storage for URL-backed newtypes.
type urlType struct{}

// URL returns the type as a parsed *url.URL.
func (u urlType) URL() *url.URL { panic("TODO") }

func (u urlType) String() string { panic("TODO") }

// AuthUrl is the authorization endpoint URL.
type AuthUrl struct{ urlType }

func NewAuthUrl(raw string) (*AuthUrl, error) { panic("TODO") }

// TokenUrl is the token endpoint URL.
type TokenUrl struct{ urlType }

func NewTokenUrl(raw string) (*TokenUrl, error) { panic("TODO") }

// RedirectUrl is the client's redirection endpoint URL.
type RedirectUrl struct{ urlType }

func NewRedirectUrl(raw string) (*RedirectUrl, error) { panic("TODO") }

// IntrospectionUrl is the RFC 7662 token introspection endpoint URL.
type IntrospectionUrl struct{ urlType }

func NewIntrospectionUrl(raw string) (*IntrospectionUrl, error) { panic("TODO") }

// RevocationUrl is the RFC 7009 token revocation endpoint URL.
type RevocationUrl struct{ urlType }

func NewRevocationUrl(raw string) (*RevocationUrl, error) { panic("TODO") }

// DeviceAuthorizationUrl is the device authorization endpoint URL.
type DeviceAuthorizationUrl struct{ urlType }

func NewDeviceAuthorizationUrl(raw string) (*DeviceAuthorizationUrl, error) { panic("TODO") }

// EndUserVerificationUrl is the end-user verification URI.
type EndUserVerificationUrl struct{ urlType }

func NewEndUserVerificationUrl(raw string) (EndUserVerificationUrl, error) { panic("TODO") }
