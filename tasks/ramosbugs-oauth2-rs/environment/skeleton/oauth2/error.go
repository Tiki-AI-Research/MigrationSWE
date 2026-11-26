package oauth2

// ConfigurationErrorKind discriminates configuration errors.
type ConfigurationErrorKind int

const (
	// ConfigurationMissingURL indicates that a required endpoint URL is not set.
	ConfigurationMissingURL ConfigurationErrorKind = iota
	// ConfigurationInsecureURL indicates that an endpoint URL must be HTTPS.
	ConfigurationInsecureURL
)

// ConfigurationError is returned when the client is incorrectly configured.
type ConfigurationError struct {
	Kind     ConfigurationErrorKind
	Endpoint string
}

func (e *ConfigurationError) Error() string { panic("TODO") }

// AuthType indicates whether requests use basic authentication or the body.
type AuthType int

const (
	// BasicAuth includes the client_id and client_secret using HTTP basic auth.
	BasicAuth AuthType = iota
	// RequestBody includes the client_id and client_secret in the request body.
	RequestBody
)

// String returns a readable name for the authentication type.
func (a AuthType) String() string { panic("TODO") }

// StandardErrorResponse is the standard OAuth2 server error response (RFC 6749 s5.2).
type StandardErrorResponse[T ErrorResponseType] struct {
	Error            T       `json:"error"`
	ErrorDescription *string `json:"error_description,omitempty"`
	ErrorURI         *string `json:"error_uri,omitempty"`
}

// NewStandardErrorResponse instantiates a new standard error response.
func NewStandardErrorResponse[T ErrorResponseType](err T, description, uri *string) StandardErrorResponse[T] {
	panic("TODO")
}

// ErrorType returns the required error code.
func (r StandardErrorResponse[T]) ErrorType() T { panic("TODO") }

// ErrorString implements ErrorResponse.
func (r StandardErrorResponse[T]) ErrorString() string { panic("TODO") }

// MarshalJSON emits the canonical field order.
func (r StandardErrorResponse[T]) MarshalJSON() ([]byte, error) { panic("TODO") }

// UnmarshalJSON keeps the generic type a valid json target.
func (r *StandardErrorResponse[T]) UnmarshalJSON(data []byte) error { panic("TODO") }

// RequestTokenErrorKind discriminates the failure modes of a token request.
type RequestTokenErrorKind int

const (
	// RequestTokenErrorServerResponse indicates a parsed server error response.
	RequestTokenErrorServerResponse RequestTokenErrorKind = iota
	// RequestTokenErrorRequest indicates a transport or request failure.
	RequestTokenErrorRequest
	// RequestTokenErrorParse indicates a response parse failure.
	RequestTokenErrorParse
	// RequestTokenErrorOther indicates some other error.
	RequestTokenErrorOther
)

// RequestTokenError is returned when a token request fails.
type RequestTokenError struct {
	Kind RequestTokenErrorKind

	// Response holds the parsed server error response.
	Response any

	// RequestErr holds the underlying transport error.
	RequestErr error

	// ParseErr holds the underlying parse error.
	ParseErr error

	// RawBody holds the raw response body for a parse failure.
	RawBody []byte

	// OtherMsg holds the message for RequestTokenErrorOther.
	OtherMsg string
}

func (e *RequestTokenError) Error() string { panic("TODO") }

// Unwrap allows errors.Is/errors.As to inspect the underlying cause.
func (e *RequestTokenError) Unwrap() error { panic("TODO") }

// ParseResponseError builds a parse error carrying the raw body.
func ParseResponseError(err error, raw []byte) *RequestTokenError { panic("TODO") }

// OtherResponseError builds a generic other error.
func OtherResponseError(msg string) *RequestTokenError { panic("TODO") }

// ServerResponseError builds a server-response error.
func ServerResponseError[T ErrorResponse](response T) *RequestTokenError { panic("TODO") }

// HTTPClientErrorKind discriminates built-in HTTP client failures.
type HTTPClientErrorKind int

const (
	// HTTPClientErrorBackend indicates an error returned by the backend client.
	HTTPClientErrorBackend HTTPClientErrorKind = iota
	// HTTPClientErrorHTTP indicates a non-backend HTTP error.
	HTTPClientErrorHTTP
	// HTTPClientErrorIO indicates an I/O error.
	HTTPClientErrorIO
	// HTTPClientErrorOther indicates any other error.
	HTTPClientErrorOther
)

// HTTPClientError is returned by built-in HTTP clients when requests fail.
type HTTPClientError struct {
	Kind HTTPClientErrorKind
	Err  error
	Msg  string
}

func (e *HTTPClientError) Error() string { panic("TODO") }

func (e *HTTPClientError) Unwrap() error { panic("TODO") }
