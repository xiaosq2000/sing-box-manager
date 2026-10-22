package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"

	"github.com/xiaosq2000/sing-box-manager/internal/routes"
)

// RuntimeSnapshot is a read-only view of the engine's current selections.
type RuntimeSnapshot struct {
	Route     string
	Protocol  string
	Protocols []string
}

func (c *Client) diagnosticBody(ctx context.Context, path string, limit int64) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+path, nil)
	if err != nil {
		return nil, ErrNotRunning
	}
	request.Header.Set("Authorization", "Bearer "+c.secret)
	response, err := c.http.Do(request)
	if err != nil {
		return nil, ErrNotRunning
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, ErrNotRunning
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, limit+1))
	if err != nil || int64(len(body)) > limit {
		return nil, ErrNotRunning
	}
	return body, nil
}

func (c *Client) diagnosticRead(ctx context.Context, path string, target any) error {
	body, err := c.diagnosticBody(ctx, path, 64<<10)
	if err != nil || json.Unmarshal(body, target) != nil {
		return ErrNotRunning
	}
	return nil
}

func (c *Client) RuntimeContext(ctx context.Context) (RuntimeSnapshot, error) {
	var mode struct {
		Mode string `json:"mode"`
	}
	var proxy struct {
		Now string   `json:"now"`
		All []string `json:"all"`
	}
	if err := c.diagnosticRead(ctx, "/configs", &mode); err != nil {
		return RuntimeSnapshot{}, err
	}
	if err := c.diagnosticRead(ctx, "/proxies/proxy", &proxy); err != nil {
		return RuntimeSnapshot{}, err
	}
	if mode.Mode == "" || proxy.Now == "" || len(proxy.All) > 64 {
		return RuntimeSnapshot{}, ErrNotRunning
	}
	return RuntimeSnapshot{Route: routes.Canonical(mode.Mode), Protocol: proxy.Now, Protocols: proxy.All}, nil
}
