package oauth2

import "context"

// EmptyExtraTokenFields is the default (empty) extra token fields.
type EmptyExtraTokenFields struct{}

func (EmptyExtraTokenFields) IsExtraTokenFields() { panic("TODO") }

// StandardTokenResponse is the standard OAuth2 token response (RFC 6749 s5.1),
// extended by EF.
type StandardTokenResponse[EF ExtraTokenFields, TT TokenType] struct{}

// NewStandardTokenResponse instantiates a new OAuth2 token response.
func NewStandardTokenResponse[EF ExtraTokenFields, TT TokenType](accessToken AccessToken, tokenType TT, extraFields EF) *StandardTokenResponse[EF, TT] {
	panic("TODO")
}

// AccessToken returns the access token issued by the authorization server.
func (r StandardTokenResponse[EF, TT]) AccessToken() AccessToken { panic("TODO") }

// TokenTypeValue returns the type of the token issued.
func (r StandardTokenResponse[EF, TT]) TokenTypeValue() TokenType { panic("TODO") }

// ExpiresIn returns the lifetime in seconds of the access token, if provided.
func (r StandardTokenResponse[EF, TT]) ExpiresIn() (uint64, bool) { panic("TODO") }

// RefreshToken returns the refresh token, if any.
func (r StandardTokenResponse[EF, TT]) RefreshToken() *RefreshToken { panic("TODO") }

// Scopes returns the scope of the access token, if included.
func (r StandardTokenResponse[EF, TT]) Scopes() []Scope { panic("TODO") }

// ExtraFields returns the user-defined extra fields.
func (r StandardTokenResponse[EF, TT]) ExtraFields() EF { panic("TODO") }

// SetAccessToken sets the access_token field.
func (r *StandardTokenResponse[EF, TT]) SetAccessToken(t AccessToken) { panic("TODO") }

// SetTokenType sets the token_type field.
func (r *StandardTokenResponse[EF, TT]) SetTokenType(t TT) { panic("TODO") }

// SetExpiresIn sets the expires_in field.
func (r *StandardTokenResponse[EF, TT]) SetExpiresIn(seconds uint64) { panic("TODO") }

// SetRefreshToken sets the refresh_token field.
func (r *StandardTokenResponse[EF, TT]) SetRefreshToken(t *RefreshToken) { panic("TODO") }

// SetScopes sets the scope field.
func (r *StandardTokenResponse[EF, TT]) SetScopes(scopes []Scope) { panic("TODO") }

// SetExtraFields sets the user-defined extra fields.
func (r *StandardTokenResponse[EF, TT]) SetExtraFields(extra EF) { panic("TODO") }

// UnmarshalJSON implements case-insensitive token types and space-delimited scopes.
func (r *StandardTokenResponse[EF, TT]) UnmarshalJSON(data []byte) error { panic("TODO") }

// MarshalJSON emits the canonical field order and flattens extra fields.
func (r StandardTokenResponse[EF, TT]) MarshalJSON() ([]byte, error) { panic("TODO") }

// CodeTokenRequest is a request to exchange an authorization code for an access token.
type CodeTokenRequest[TE ErrorResponse, TR TokenResponse] struct{}

// AddExtraParam appends an extra param to the token request.
func (r *CodeTokenRequest[TE, TR]) AddExtraParam(name, value string) *CodeTokenRequest[TE, TR] {
	panic("TODO")
}

// AddScope appends a scope to the token request.
func (r *CodeTokenRequest[TE, TR]) AddScope(scope Scope) *CodeTokenRequest[TE, TR] {
	panic("TODO")
}

// AddScopes appends scopes to the token request.
func (r *CodeTokenRequest[TE, TR]) AddScopes(scopes []Scope) *CodeTokenRequest[TE, TR] {
	panic("TODO")
}

// SetPkceVerifier completes the PKCE protocol flow.
func (r *CodeTokenRequest[TE, TR]) SetPkceVerifier(verifier PkceCodeVerifier) *CodeTokenRequest[TE, TR] {
	panic("TODO")
}

// SetRedirectURI overrides the redirect_url.
func (r *CodeTokenRequest[TE, TR]) SetRedirectURI(redirectURL RedirectUrl) *CodeTokenRequest[TE, TR] {
	panic("TODO")
}

func (r *CodeTokenRequest[TE, TR]) prepareRequest() HTTPRequest { panic("TODO") }

// Request synchronously sends the request and awaits a response.
func (r *CodeTokenRequest[TE, TR]) Request(client SyncHTTPClient) (TR, *RequestTokenError) {
	panic("TODO")
}

// RequestAsync asynchronously sends the request and awaits a response.
func (r *CodeTokenRequest[TE, TR]) RequestAsync(ctx context.Context, client AsyncHTTPClient) (TR, *RequestTokenError) {
	panic("TODO")
}

// RefreshTokenRequest is a request to exchange a refresh token for an access token.
type RefreshTokenRequest[TE ErrorResponse, TR TokenResponse] struct{}

// AddExtraParam appends an extra param to the token request.
func (r *RefreshTokenRequest[TE, TR]) AddExtraParam(name, value string) *RefreshTokenRequest[TE, TR] {
	panic("TODO")
}

// AddScope appends a new scope to the token request.
func (r *RefreshTokenRequest[TE, TR]) AddScope(scope Scope) *RefreshTokenRequest[TE, TR] {
	panic("TODO")
}

// AddScopes appends a collection of scopes to the token request.
func (r *RefreshTokenRequest[TE, TR]) AddScopes(scopes []Scope) *RefreshTokenRequest[TE, TR] {
	panic("TODO")
}

func (r *RefreshTokenRequest[TE, TR]) prepareRequest() HTTPRequest { panic("TODO") }

// Request synchronously sends the request and awaits a response.
func (r *RefreshTokenRequest[TE, TR]) Request(client SyncHTTPClient) (TR, *RequestTokenError) {
	panic("TODO")
}

// RequestAsync asynchronously sends the request and awaits a response.
func (r *RefreshTokenRequest[TE, TR]) RequestAsync(ctx context.Context, client AsyncHTTPClient) (TR, *RequestTokenError) {
	panic("TODO")
}

// PasswordTokenRequest is a request to exchange resource owner credentials for
// an access token.
type PasswordTokenRequest[TE ErrorResponse, TR TokenResponse] struct{}

// AddExtraParam appends an extra param to the token request.
func (r *PasswordTokenRequest[TE, TR]) AddExtraParam(name, value string) *PasswordTokenRequest[TE, TR] {
	panic("TODO")
}

// AddScope appends a new scope to the token request.
func (r *PasswordTokenRequest[TE, TR]) AddScope(scope Scope) *PasswordTokenRequest[TE, TR] {
	panic("TODO")
}

// AddScopes appends a collection of scopes to the token request.
func (r *PasswordTokenRequest[TE, TR]) AddScopes(scopes []Scope) *PasswordTokenRequest[TE, TR] {
	panic("TODO")
}

func (r *PasswordTokenRequest[TE, TR]) prepareRequest() HTTPRequest { panic("TODO") }

// Request synchronously sends the request and awaits a response.
func (r *PasswordTokenRequest[TE, TR]) Request(client SyncHTTPClient) (TR, *RequestTokenError) {
	panic("TODO")
}

// RequestAsync asynchronously sends the request and awaits a response.
func (r *PasswordTokenRequest[TE, TR]) RequestAsync(ctx context.Context, client AsyncHTTPClient) (TR, *RequestTokenError) {
	panic("TODO")
}

// ClientCredentialsTokenRequest is a request to exchange client credentials for
// an access token.
type ClientCredentialsTokenRequest[TE ErrorResponse, TR TokenResponse] struct{}

// AddExtraParam appends an extra param to the token request.
func (r *ClientCredentialsTokenRequest[TE, TR]) AddExtraParam(name, value string) *ClientCredentialsTokenRequest[TE, TR] {
	panic("TODO")
}

// AddScope appends a new scope to the token request.
func (r *ClientCredentialsTokenRequest[TE, TR]) AddScope(scope Scope) *ClientCredentialsTokenRequest[TE, TR] {
	panic("TODO")
}

// AddScopes appends a collection of scopes to the token request.
func (r *ClientCredentialsTokenRequest[TE, TR]) AddScopes(scopes []Scope) *ClientCredentialsTokenRequest[TE, TR] {
	panic("TODO")
}

func (r *ClientCredentialsTokenRequest[TE, TR]) prepareRequest() HTTPRequest { panic("TODO") }

// Request synchronously sends the request and awaits a response.
func (r *ClientCredentialsTokenRequest[TE, TR]) Request(client SyncHTTPClient) (TR, *RequestTokenError) {
	panic("TODO")
}

// RequestAsync asynchronously sends the request and awaits a response.
func (r *ClientCredentialsTokenRequest[TE, TR]) RequestAsync(ctx context.Context, client AsyncHTTPClient) (TR, *RequestTokenError) {
	panic("TODO")
}
