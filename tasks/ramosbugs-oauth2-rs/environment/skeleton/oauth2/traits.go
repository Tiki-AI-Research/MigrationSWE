package oauth2

// Skeleton: exported API surface only. Every function and method body is a
// TODO stub; implement them to satisfy the documented behavior.

// ErrorResponseType is implemented by values that can be used as the `error`
// field of a standard OAuth2 error response.
type ErrorResponseType interface {
	ErrorTypeString() string
}

// ErrorResponse is a server error response.
type ErrorResponse interface {
	ErrorString() string
}

// TokenType is the type of an OAuth2 access token.
type TokenType interface {
	TokenTypeString() string
}

// ExtraTokenFields is implemented by user-defined types that add extra fields
// to a token response.
type ExtraTokenFields interface {
	IsExtraTokenFields()
}

// TokenResponse is the common interface shared by all token responses.
type TokenResponse interface {
	AccessToken() AccessToken
	TokenTypeValue() TokenType
	ExpiresIn() (uint64, bool)
	RefreshToken() *RefreshToken
	Scopes() []Scope
}

// TokenIntrospectionResponse is the common interface shared by all token
// introspection responses.
type TokenIntrospectionResponse interface {
	Active() bool
	Scopes() []Scope
	ClientId() *ClientId
	Username() *string
	TokenType() (TokenType, bool)
	Exp() *int64
	Iat() *int64
	Nbf() *int64
	Sub() *string
	Aud() []string
	Iss() *string
	Jti() *string
}

// RevocableToken is a token that can be revoked per RFC 7009.
type RevocableToken interface {
	Secret() string
	TypeHint() string
}

// ExtraDeviceAuthorizationFields is implemented by user-defined types that add
// extra fields to a device authorization response.
type ExtraDeviceAuthorizationFields interface {
	IsExtraDeviceAuthorizationFields()
}
