package oauth2

import "context"

// StandardTokenIntrospectionResponse is the standard OAuth2 token introspection
// response (RFC 7662 s2.2), extended by EF.
type StandardTokenIntrospectionResponse[EF ExtraTokenFields, TT TokenType] struct{}

// NewStandardTokenIntrospectionResponse instantiates a new introspection response.
func NewStandardTokenIntrospectionResponse[EF ExtraTokenFields, TT TokenType](active bool, extraFields EF) *StandardTokenIntrospectionResponse[EF, TT] {
	panic("TODO")
}

// Active reports whether the presented token is currently active.
func (r StandardTokenIntrospectionResponse[EF, TT]) Active() bool { panic("TODO") }

// Scopes returns the scopes associated with the token.
func (r StandardTokenIntrospectionResponse[EF, TT]) Scopes() []Scope { panic("TODO") }

// ClientId returns the client identifier for the token.
func (r StandardTokenIntrospectionResponse[EF, TT]) ClientId() *ClientId { panic("TODO") }

// Username returns the human-readable resource owner identifier.
func (r StandardTokenIntrospectionResponse[EF, TT]) Username() *string { panic("TODO") }

// TokenType returns the token type, if present.
func (r StandardTokenIntrospectionResponse[EF, TT]) TokenType() (TokenType, bool) {
	panic("TODO")
}

// Exp returns the expiration timestamp in seconds, if present.
func (r StandardTokenIntrospectionResponse[EF, TT]) Exp() *int64 { panic("TODO") }

// Iat returns the issuance timestamp in seconds, if present.
func (r StandardTokenIntrospectionResponse[EF, TT]) Iat() *int64 { panic("TODO") }

// Nbf returns the not-before timestamp in seconds, if present.
func (r StandardTokenIntrospectionResponse[EF, TT]) Nbf() *int64 { panic("TODO") }

// Sub returns the subject of the token, if present.
func (r StandardTokenIntrospectionResponse[EF, TT]) Sub() *string { panic("TODO") }

// Aud returns the intended audience(s) of the token, if present.
func (r StandardTokenIntrospectionResponse[EF, TT]) Aud() []string { panic("TODO") }

// Iss returns the issuer of the token, if present.
func (r StandardTokenIntrospectionResponse[EF, TT]) Iss() *string { panic("TODO") }

// Jti returns the token identifier, if present.
func (r StandardTokenIntrospectionResponse[EF, TT]) Jti() *string { panic("TODO") }

// ExtraFields returns the user-defined extra fields.
func (r StandardTokenIntrospectionResponse[EF, TT]) ExtraFields() EF { panic("TODO") }

// SetActive sets the active field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetActive(active bool) { panic("TODO") }

// SetScopes sets the scope field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetScopes(scopes []Scope) { panic("TODO") }

// SetClientId sets the client_id field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetClientId(clientID *ClientId) {
	panic("TODO")
}

// SetUsername sets the username field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetUsername(username *string) {
	panic("TODO")
}

// SetTokenType sets the token_type field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetTokenType(tokenType *TT) {
	panic("TODO")
}

// SetExp sets the exp field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetExp(exp *int64) { panic("TODO") }

// SetIat sets the iat field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetIat(iat *int64) { panic("TODO") }

// SetNbf sets the nbf field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetNbf(nbf *int64) { panic("TODO") }

// SetSub sets the sub field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetSub(sub *string) { panic("TODO") }

// SetAud sets the aud field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetAud(aud []string) { panic("TODO") }

// SetIss sets the iss field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetIss(iss *string) { panic("TODO") }

// SetJti sets the jti field.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetJti(jti *string) { panic("TODO") }

// SetExtraFields sets the user-defined extra fields.
func (r *StandardTokenIntrospectionResponse[EF, TT]) SetExtraFields(extra EF) {
	panic("TODO")
}

// UnmarshalJSON implements space-delimited scopes, case-insensitive token types
// and string-or-list audiences.
func (r *StandardTokenIntrospectionResponse[EF, TT]) UnmarshalJSON(data []byte) error {
	panic("TODO")
}

// MarshalJSON emits the canonical field order and flattens extra fields.
func (r StandardTokenIntrospectionResponse[EF, TT]) MarshalJSON() ([]byte, error) {
	panic("TODO")
}

// IntrospectionRequest is a request to introspect an access token.
type IntrospectionRequest[TE ErrorResponse, TIR TokenIntrospectionResponse] struct{}

// SetTokenTypeHint sets the optional token_type_hint parameter.
func (r *IntrospectionRequest[TE, TIR]) SetTokenTypeHint(value string) *IntrospectionRequest[TE, TIR] {
	panic("TODO")
}

// AddExtraParam appends an extra param to the introspection request.
func (r *IntrospectionRequest[TE, TIR]) AddExtraParam(name, value string) *IntrospectionRequest[TE, TIR] {
	panic("TODO")
}

func (r *IntrospectionRequest[TE, TIR]) prepareRequest() HTTPRequest { panic("TODO") }

// Request synchronously sends the request and awaits a response.
func (r *IntrospectionRequest[TE, TIR]) Request(client SyncHTTPClient) (TIR, *RequestTokenError) {
	panic("TODO")
}

// RequestAsync asynchronously sends the request and awaits a response.
func (r *IntrospectionRequest[TE, TIR]) RequestAsync(ctx context.Context, client AsyncHTTPClient) (TIR, *RequestTokenError) {
	panic("TODO")
}
