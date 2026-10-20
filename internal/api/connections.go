package api

import "net/http"

// Connection is one live connection in sing-box's connection list.
type Connection struct {
	ID       string             `json:"id"`
	Metadata ConnectionMetadata `json:"metadata"`
}

// ConnectionMetadata is the part of a connection that names its inbound and
// destination. Type is "<inbound_type>/<inbound_tag>", such as "tun/tun-in".
type ConnectionMetadata struct {
	Type            string `json:"type"`
	Network         string `json:"network"`
	DestinationIP   string `json:"destinationIP"`
	DestinationPort string `json:"destinationPort"`
}

// Connections returns the live connections the Clash API tracks.
func (c *Client) Connections() ([]Connection, error) {
	var payload struct {
		Connections []Connection `json:"connections"`
	}
	if err := c.call(http.MethodGet, "/connections", nil, &payload); err != nil {
		return nil, err
	}
	return payload.Connections, nil
}
