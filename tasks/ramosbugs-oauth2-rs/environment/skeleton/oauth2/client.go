package oauth2

// Client stores the configuration for an OAuth2 client.
//
// Because Go has no compile-time typestate, endpoint completeness is validated
// at runtime: every flow accessor returns a ConfigurationError when its
// required endpoint has not been configured.
type Client[
	TE ErrorResponse,
	TR TokenResponse,
	TIR TokenIntrospectionResponse,
	RT RevocableToken,
	TRE ErrorResponse,
] struct{}

// NewClient initializes an OAuth2 client with the specified client ID and the
// given extension type parameters.
func NewClient[TE ErrorResponse, TR TokenResponse, TIR TokenIntrospectionResponse, RT RevocableToken, TRE ErrorResponse](clientID ClientId) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// NewBasicClient initializes a basic OAuth2 client.
func NewBasicClient(clientID ClientId) *BasicClient { panic("TODO") }

// SetAuthType sets the client authentication type. The default is BasicAuth.
func (c *Client[TE, TR, TIR, RT, TRE]) SetAuthType(authType AuthType) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetClientSecret sets the client secret.
func (c *Client[TE, TR, TIR, RT, TRE]) SetClientSecret(secret ClientSecret) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetAuthURL sets the authorization endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetAuthURL(url *AuthUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetAuthURLOption conditionally sets the authorization endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetAuthURLOption(url *AuthUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetTokenURL sets the token endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetTokenURL(url *TokenUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetTokenURLOption conditionally sets the token endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetTokenURLOption(url *TokenUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetRedirectURL sets the redirect URL used by the authorization endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetRedirectURL(url *RedirectUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetDeviceAuthorizationURL sets the device authorization endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetDeviceAuthorizationURL(url *DeviceAuthorizationUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetDeviceAuthorizationURLOption conditionally sets the device authorization endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetDeviceAuthorizationURLOption(url *DeviceAuthorizationUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetIntrospectionURL sets the RFC 7662 introspection endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetIntrospectionURL(url *IntrospectionUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetIntrospectionURLOption conditionally sets the RFC 7662 introspection endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetIntrospectionURLOption(url *IntrospectionUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetRevocationURL sets the RFC 7009 revocation endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetRevocationURL(url *RevocationUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// SetRevocationURLOption conditionally sets the RFC 7009 revocation endpoint.
func (c *Client[TE, TR, TIR, RT, TRE]) SetRevocationURLOption(url *RevocationUrl) *Client[TE, TR, TIR, RT, TRE] {
	panic("TODO")
}

// ClientId returns the client ID.
func (c *Client[TE, TR, TIR, RT, TRE]) ClientId() ClientId { panic("TODO") }

// AuthType returns the type of client authentication in use.
func (c *Client[TE, TR, TIR, RT, TRE]) AuthType() AuthType { panic("TODO") }

// RedirectURI returns the redirect URL, if set.
func (c *Client[TE, TR, TIR, RT, TRE]) RedirectURI() *RedirectUrl { panic("TODO") }

// AuthURI returns the authorization endpoint, if set.
func (c *Client[TE, TR, TIR, RT, TRE]) AuthURI() *AuthUrl { panic("TODO") }

// TokenURI returns the token endpoint, if set.
func (c *Client[TE, TR, TIR, RT, TRE]) TokenURI() *TokenUrl { panic("TODO") }

// DeviceAuthorizationURL returns the device authorization endpoint, if set.
func (c *Client[TE, TR, TIR, RT, TRE]) DeviceAuthorizationURL() *DeviceAuthorizationUrl {
	panic("TODO")
}

// IntrospectionURL returns the introspection endpoint, if set.
func (c *Client[TE, TR, TIR, RT, TRE]) IntrospectionURL() *IntrospectionUrl { panic("TODO") }

// RevocationURL returns the revocation endpoint, if set.
func (c *Client[TE, TR, TIR, RT, TRE]) RevocationURL() *RevocationUrl { panic("TODO") }

// AuthorizeURL generates an authorization URL for a new authorization request.
func (c *Client[TE, TR, TIR, RT, TRE]) AuthorizeURL(stateFn func() CsrfToken) (*AuthorizationRequest, error) {
	panic("TODO")
}

// ExchangeCode exchanges an authorization code for an access token.
func (c *Client[TE, TR, TIR, RT, TRE]) ExchangeCode(code AuthorizationCode) (*CodeTokenRequest[TE, TR], error) {
	panic("TODO")
}

// ExchangeClientCredentials requests an access token using the client credentials flow.
func (c *Client[TE, TR, TIR, RT, TRE]) ExchangeClientCredentials() (*ClientCredentialsTokenRequest[TE, TR], error) {
	panic("TODO")
}

// ExchangePassword requests an access token using the password flow.
func (c *Client[TE, TR, TIR, RT, TRE]) ExchangePassword(username *ResourceOwnerUsername, password *ResourceOwnerPassword) (*PasswordTokenRequest[TE, TR], error) {
	panic("TODO")
}

// ExchangeRefreshToken exchanges a refresh token for an access token.
func (c *Client[TE, TR, TIR, RT, TRE]) ExchangeRefreshToken(refreshToken *RefreshToken) (*RefreshTokenRequest[TE, TR], error) {
	panic("TODO")
}

// ExchangeDeviceCode begins the device authorization flow.
func (c *Client[TE, TR, TIR, RT, TRE]) ExchangeDeviceCode() (*DeviceAuthorizationRequest[TE], error) {
	panic("TODO")
}

// ExchangeDeviceAccessToken polls the token endpoint for an access token.
func (c *Client[TE, TR, TIR, RT, TRE]) ExchangeDeviceAccessToken(response DeviceAuthorizationResponseLike) (*DeviceAccessTokenRequest[TR], error) {
	panic("TODO")
}

// Introspect retrieves metadata for an access token using RFC 7662.
func (c *Client[TE, TR, TIR, RT, TRE]) Introspect(token *AccessToken) (*IntrospectionRequest[TE, TIR], error) {
	panic("TODO")
}

// RevokeToken revokes an access or refresh token using RFC 7009.
func (c *Client[TE, TR, TIR, RT, TRE]) RevokeToken(token RT) (*RevocationRequest[RT, TRE], error) {
	panic("TODO")
}
