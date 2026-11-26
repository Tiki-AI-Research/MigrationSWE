package oauth2

import (
	"context"
	"time"
)

// DeviceCodeErrorResponseType is the device authorization error type.
type DeviceCodeErrorResponseType string

const (
	DeviceCodeErrorResponseTypeAuthorizationPending DeviceCodeErrorResponseType = "authorization_pending"
	DeviceCodeErrorResponseTypeSlowDown             DeviceCodeErrorResponseType = "slow_down"
	DeviceCodeErrorResponseTypeAccessDenied         DeviceCodeErrorResponseType = "access_denied"
	DeviceCodeErrorResponseTypeExpiredToken         DeviceCodeErrorResponseType = "expired_token"
)

// ErrorTypeString returns the canonical string for this error type.
func (t DeviceCodeErrorResponseType) ErrorTypeString() string { panic("TODO") }

// DeviceCodeErrorResponse is the error response specialization for the device
// code flow.
type DeviceCodeErrorResponse = StandardErrorResponse[DeviceCodeErrorResponseType]

// EmptyExtraDeviceAuthorizationFields is the default (empty) extra device
// authorization fields.
type EmptyExtraDeviceAuthorizationFields struct{}

func (EmptyExtraDeviceAuthorizationFields) IsExtraDeviceAuthorizationFields() { panic("TODO") }

// DeviceAuthorizationResponse is the standard OAuth2 device authorization
// response, extended by EF.
type DeviceAuthorizationResponse[EF ExtraDeviceAuthorizationFields] struct {
	deviceCode DeviceCode
	interval   uint64
	expiresIn  uint64
}

// StandardDeviceAuthorizationResponse throws away extra received response fields.
type StandardDeviceAuthorizationResponse = DeviceAuthorizationResponse[EmptyExtraDeviceAuthorizationFields]

// DeviceAuthorizationResponseLike is the subset of a device authorization
// response needed to poll for an access token.
type DeviceAuthorizationResponseLike interface {
	DeviceCode() DeviceCode
	Interval() time.Duration
	ExpiresIn() time.Duration
}

// DeviceCode returns the device verification code.
func (r *DeviceAuthorizationResponse[EF]) DeviceCode() DeviceCode { panic("TODO") }

// UserCode returns the end-user verification code.
func (r *DeviceAuthorizationResponse[EF]) UserCode() UserCode { panic("TODO") }

// VerificationURI returns the end-user verification URI.
func (r *DeviceAuthorizationResponse[EF]) VerificationURI() EndUserVerificationUrl { panic("TODO") }

// VerificationURIComplete returns the verification URI including the user code, if any.
func (r *DeviceAuthorizationResponse[EF]) VerificationURIComplete() *VerificationUriComplete {
	panic("TODO")
}

// ExpiresIn returns the lifetime of the device_code and user_code.
func (r *DeviceAuthorizationResponse[EF]) ExpiresIn() time.Duration { panic("TODO") }

// Interval returns the minimum polling interval.
func (r *DeviceAuthorizationResponse[EF]) Interval() time.Duration { panic("TODO") }

// ExtraFields returns the user-defined extra fields.
func (r *DeviceAuthorizationResponse[EF]) ExtraFields() EF { panic("TODO") }

// UnmarshalJSON implements the interval default and the legacy verification_url alias.
func (r *DeviceAuthorizationResponse[EF]) UnmarshalJSON(data []byte) error { panic("TODO") }

// DeviceAuthorizationRequest is a request for a set of verification codes.
type DeviceAuthorizationRequest[TE ErrorResponse] struct{}

// AddExtraParam appends an extra param to the device authorization request.
func (r *DeviceAuthorizationRequest[TE]) AddExtraParam(name, value string) *DeviceAuthorizationRequest[TE] {
	panic("TODO")
}

// AddScope appends a new scope to the device authorization request.
func (r *DeviceAuthorizationRequest[TE]) AddScope(scope Scope) *DeviceAuthorizationRequest[TE] {
	panic("TODO")
}

// AddScopes appends a collection of scopes to the device authorization request.
func (r *DeviceAuthorizationRequest[TE]) AddScopes(scopes []Scope) *DeviceAuthorizationRequest[TE] {
	panic("TODO")
}

func (r *DeviceAuthorizationRequest[TE]) prepareRequest() HTTPRequest { panic("TODO") }

// Request synchronously sends the request and awaits a response.
func (r *DeviceAuthorizationRequest[TE]) Request(client SyncHTTPClient) (*StandardDeviceAuthorizationResponse, *RequestTokenError) {
	panic("TODO")
}

// RequestAsync asynchronously sends the request and awaits a response.
func (r *DeviceAuthorizationRequest[TE]) RequestAsync(ctx context.Context, client AsyncHTTPClient) (*StandardDeviceAuthorizationResponse, *RequestTokenError) {
	panic("TODO")
}

// DeviceAccessTokenRequest is a request for a device access token.
type DeviceAccessTokenRequest[TR TokenResponse] struct{}

// AddExtraParam appends an extra param to the token request.
func (r *DeviceAccessTokenRequest[TR]) AddExtraParam(name, value string) *DeviceAccessTokenRequest[TR] {
	panic("TODO")
}

// SetTimeFunc specifies a function for returning the current time.
func (r *DeviceAccessTokenRequest[TR]) SetTimeFunc(timeFn func() time.Time) *DeviceAccessTokenRequest[TR] {
	panic("TODO")
}

// SetMaxBackoffInterval sets the upper limit of the sleep interval used when
// the HTTP client returns an error.
func (r *DeviceAccessTokenRequest[TR]) SetMaxBackoffInterval(interval time.Duration) *DeviceAccessTokenRequest[TR] {
	panic("TODO")
}

func (r *DeviceAccessTokenRequest[TR]) prepareRequest() HTTPRequest { panic("TODO") }

// Request synchronously polls the authorization server, waiting via sleepFn.
func (r *DeviceAccessTokenRequest[TR]) Request(client SyncHTTPClient, sleepFn func(time.Duration), timeout *time.Duration) (TR, *RequestTokenError) {
	panic("TODO")
}

// RequestAsync asynchronously polls the authorization server, waiting via sleepFn.
func (r *DeviceAccessTokenRequest[TR]) RequestAsync(ctx context.Context, client AsyncHTTPClient, sleepFn func(context.Context, time.Duration) error, timeout *time.Duration) (TR, *RequestTokenError) {
	panic("TODO")
}
