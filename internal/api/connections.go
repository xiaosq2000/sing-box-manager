package api

import (
	"context"
	"encoding/json"
	"net/http"
)

// Connection is one live connection in sing-box's connection list.
type Connection struct {
	ID       string             `json:"id"`
	Metadata ConnectionMetadata `json:"metadata"`
	Chains   []string           `json:"chains"`
}

// Type is the inbound type and tag, such as tun/tun-in.
type ConnectionMetadata struct {
	Type            string `json:"type"`
	Host            string `json:"host"`
	Network         string `json:"network"`
	SourceIP        string `json:"sourceIP"`
	SourcePort      string `json:"sourcePort"`
	DestinationIP   string `json:"destinationIP"`
	DestinationPort string `json:"destinationPort"`
}

func (c *Client) Connections() ([]Connection, error) {
	var payload struct {
		Connections []Connection `json:"connections"`
	}
	if err := c.call(http.MethodGet, "/connections", nil, &payload); err != nil {
		return nil, err
	}
	return payload.Connections, nil
}

// ConnectionsContext bounds snapshots and never exposes engine response bodies.
func (c *Client) ConnectionsContext(ctx context.Context) ([]Connection, error) {
	data, err := c.diagnosticBody(ctx, "/connections", 2<<20)
	if err != nil {
		return nil, err
	}
	var payload struct {
		Connections []Connection `json:"connections"`
	}
	if json.Unmarshal(data, &payload) != nil || len(payload.Connections) > 4096 {
		return nil, ErrNotRunning
	}
	return payload.Connections, nil
}
