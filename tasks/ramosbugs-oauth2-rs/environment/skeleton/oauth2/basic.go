package oauth2

// BasicTokenType is the basic OAuth2 authorization token type.
type BasicTokenType string

const (
	// BasicTokenTypeBearer is the bearer token type (RFC 6750).
	BasicTokenTypeBearer BasicTokenType = "bearer"
	// BasicTokenTypeMac is the MAC token type.
	BasicTokenTypeMac BasicTokenType = "mac"
)

// TokenTypeString returns the canonical string for this token type.
func (t BasicTokenType) TokenTypeString() string { panic("TODO") }

// BasicErrorResponseType is the basic OAuth2 error response type (RFC 6749 s5.2).
type BasicErrorResponseType string

const (
	BasicErrorResponseTypeInvalidClient        BasicErrorResponseType = "invalid_client"
	BasicErrorResponseTypeInvalidGrant         BasicErrorResponseType = "invalid_grant"
	BasicErrorResponseTypeInvalidRequest       BasicErrorResponseType = "invalid_request"
	BasicErrorResponseTypeInvalidScope         BasicErrorResponseType = "invalid_scope"
	BasicErrorResponseTypeUnauthorizedClient   BasicErrorResponseType = "unauthorized_client"
	BasicErrorResponseTypeUnsupportedGrantType BasicErrorResponseType = "unsupported_grant_type"
)

// ErrorTypeString returns the canonical string for this error type.
func (t BasicErrorResponseType) ErrorTypeString() string { panic("TODO") }

// BasicErrorResponse is the error response specialization for the basic client.
type BasicErrorResponse = StandardErrorResponse[BasicErrorResponseType]

// BasicTokenResponse is the basic OAuth2 token response.
type BasicTokenResponse = StandardTokenResponse[EmptyExtraTokenFields, BasicTokenType]

// BasicTokenIntrospectionResponse is the basic OAuth2 token introspection response.
type BasicTokenIntrospectionResponse = StandardTokenIntrospectionResponse[EmptyExtraTokenFields, BasicTokenType]

// BasicRevocationErrorResponse is the revocation error response specialization.
type BasicRevocationErrorResponse = StandardErrorResponse[RevocationErrorResponseType]

// BasicClient is the basic OAuth2 client specialization.
type BasicClient = Client[
	BasicErrorResponse,
	BasicTokenResponse,
	BasicTokenIntrospectionResponse,
	StandardRevocableToken,
	BasicRevocationErrorResponse,
]

// BasicRequestTokenError is the token error specialization for the basic client.
type BasicRequestTokenError = RequestTokenError
