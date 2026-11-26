package oauth2

import "net/url"

// AuthorizationRequest is a request to the authorization endpoint.
type AuthorizationRequest struct{}

// AddScope appends a new scope to the authorization URL.
func (r *AuthorizationRequest) AddScope(scope Scope) *AuthorizationRequest { panic("TODO") }

// AddScopes appends a collection of scopes to the authorization URL.
func (r *AuthorizationRequest) AddScopes(scopes []Scope) *AuthorizationRequest { panic("TODO") }

// AddExtraParam appends an extra param to the authorization URL.
func (r *AuthorizationRequest) AddExtraParam(name, value string) *AuthorizationRequest { panic("TODO") }

// UseImplicitFlow enables the implicit grant flow.
func (r *AuthorizationRequest) UseImplicitFlow() *AuthorizationRequest { panic("TODO") }

// SetResponseType enables custom flows other than the code and token grants.
func (r *AuthorizationRequest) SetResponseType(responseType ResponseType) *AuthorizationRequest {
	panic("TODO")
}

// SetPkceChallenge enables PKCE.
func (r *AuthorizationRequest) SetPkceChallenge(challenge PkceCodeChallenge) *AuthorizationRequest {
	panic("TODO")
}

// SetRedirectURI overrides the redirect_url.
func (r *AuthorizationRequest) SetRedirectURI(redirectURL RedirectUrl) *AuthorizationRequest {
	panic("TODO")
}

// URL returns the full authorization URL and the CSRF state for this request.
func (r *AuthorizationRequest) URL() (*url.URL, CsrfToken) { panic("TODO") }
