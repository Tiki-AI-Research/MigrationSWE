package oauth2

import "context"

// RevocationErrorResponseType is the RFC 7009 token revocation error type.
type RevocationErrorResponseType string

const (
	// RevocationErrorResponseTypeUnsupportedTokenType indicates the server does
	// not support revocation of the presented token type.
	RevocationErrorResponseTypeUnsupportedTokenType RevocationErrorResponseType = "unsupported_token_type"
)

// ErrorTypeString returns the canonical string for this error type.
func (t RevocationErrorResponseType) ErrorTypeString() string { panic("TODO") }

// StandardRevocableToken is a token representation usable with authorization
// servers that support RFC 7009 token revocation.
type StandardRevocableToken struct{}

// NewStandardRevocableTokenFromAccessToken builds a revocable token from an access token.
func NewStandardRevocableTokenFromAccessToken(token AccessToken) StandardRevocableToken {
	panic("TODO")
}

// NewStandardRevocableTokenFromRefreshToken builds a revocable token from a refresh token.
func NewStandardRevocableTokenFromRefreshToken(token RefreshToken) StandardRevocableToken {
	panic("TODO")
}

// Secret returns the token value to be revoked.
func (t StandardRevocableToken) Secret() string { panic("TODO") }

// TypeHint returns the RFC 7009 token type hint for the token.
func (t StandardRevocableToken) TypeHint() string { panic("TODO") }

// RevocationRequest is a request to revoke a token via an RFC 7009 endpoint.
type RevocationRequest[RT RevocableToken, TRE ErrorResponse] struct{}

// AddExtraParam appends an extra param to the token revocation request.
func (r *RevocationRequest[RT, TRE]) AddExtraParam(name, value string) *RevocationRequest[RT, TRE] {
	panic("TODO")
}

func (r *RevocationRequest[RT, TRE]) prepareRequest() HTTPRequest { panic("TODO") }

// Request synchronously sends the request and awaits a response. A nil return
// value indicates success.
func (r *RevocationRequest[RT, TRE]) Request(client SyncHTTPClient) *RequestTokenError {
	panic("TODO")
}

// RequestAsync asynchronously sends the request and awaits a response. A nil
// return value indicates success.
func (r *RevocationRequest[RT, TRE]) RequestAsync(ctx context.Context, client AsyncHTTPClient) *RequestTokenError {
	panic("TODO")
}
