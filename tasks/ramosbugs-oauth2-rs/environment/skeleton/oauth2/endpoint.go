package oauth2

import "context"

// HTTPRequest is an HTTP request: a method, URL, headers and body bytes.
type HTTPRequest struct {
	Method string
	URL    string
	Header map[string]string
	Body   []byte
}

// HTTPResponse is an HTTP response: a status code, headers and body bytes.
type HTTPResponse struct {
	StatusCode int
	Header     map[string]string
	Body       []byte
}

// SyncHTTPClient is a synchronous (blocking) HTTP client.
type SyncHTTPClient interface {
	Call(request HTTPRequest) (HTTPResponse, error)
}

// AsyncHTTPClient is an asynchronous HTTP client.
type AsyncHTTPClient interface {
	Call(ctx context.Context, request HTTPRequest) (HTTPResponse, error)
}

// SyncHTTPClientFunc adapts a function to the SyncHTTPClient interface.
type SyncHTTPClientFunc func(HTTPRequest) (HTTPResponse, error)

// Call implements SyncHTTPClient.
func (f SyncHTTPClientFunc) Call(request HTTPRequest) (HTTPResponse, error) { panic("TODO") }

// AsyncHTTPClientFunc adapts a function to the AsyncHTTPClient interface.
type AsyncHTTPClientFunc func(context.Context, HTTPRequest) (HTTPResponse, error)

// Call implements AsyncHTTPClient.
func (f AsyncHTTPClientFunc) Call(ctx context.Context, request HTTPRequest) (HTTPResponse, error) {
	panic("TODO")
}
