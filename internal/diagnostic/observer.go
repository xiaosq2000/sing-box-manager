// Package diagnostic measures requests without changing the engine's routing.
package diagnostic

import (
	"context"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
)

// Evidence describes only a socket created by this command.
type Evidence struct {
	Verified bool   `json:"verified"`
	Path     string `json:"path"`
}

type socket struct {
	source, port, destination, destinationPort, host string
	began, ended                                     time.Time
	id                                               string
	evidence                                         Evidence
	ambiguous                                        bool
}

// Observer keeps bounded connection evidence in memory. Snapshots never run on
// the HTTP trace or download reader goroutine.
type Observer struct {
	mu        sync.Mutex
	sockets   []*socket
	baseline  map[string]bool
	owners    map[string]int
	inbound   string
	protocols []string
	list      func(context.Context) ([]api.Connection, error)
	wake      chan struct{}
	cancel    context.CancelFunc
	done      chan struct{}
}

func NewObserver(ctx context.Context, inbound string, protocols []string, list func(context.Context) ([]api.Connection, error)) (*Observer, error) {
	ctx, cancel := context.WithCancel(ctx)
	o := &Observer{baseline: make(map[string]bool), owners: make(map[string]int), inbound: inbound, protocols: protocols, list: list, wake: make(chan struct{}, 1), cancel: cancel, done: make(chan struct{})}
	initial, stop := context.WithTimeout(ctx, 250*time.Millisecond)
	connections, err := list(initial)
	stop()
	if err != nil {
		cancel()
		close(o.done)
		return nil, err
	}
	for _, connection := range connections {
		o.baseline[connection.ID] = true
	}
	go o.run(ctx)
	return o, nil
}

func (o *Observer) run(ctx context.Context) {
	defer close(o.done)
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	var last time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case <-o.wake:
		case <-ticker.C:
		}
		if wait := time.Until(last.Add(5 * time.Millisecond)); wait > 0 {
			timer := time.NewTimer(wait)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
		}
		last = time.Now()
		probe, stop := context.WithTimeout(ctx, 250*time.Millisecond)
		began := time.Now()
		connections, err := o.list(probe)
		ended := time.Now()
		stop()
		if err == nil {
			o.observe(connections, began, ended)
		}
	}
}

func (o *Observer) invalidate(index int) {
	o.sockets[index].ambiguous = true
	o.sockets[index].evidence = Evidence{Path: "unknown"}
}

func (o *Observer) observe(connections []api.Connection, began, ended time.Time) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.owners == nil {
		o.owners = make(map[string]int)
	}
	for _, connection := range connections {
		if connection.ID == "" || o.baseline[connection.ID] {
			continue
		}
		m := connection.Metadata
		if m.Type != o.inbound || m.Network != "tcp" {
			continue
		}
		for index, s := range o.sockets {
			// The complete snapshot interval must fit inside this socket's life.
			// A delayed response cannot prove a socket that closed or started later.
			if began.Before(s.began) || (!s.ended.IsZero() && !ended.Before(s.ended)) {
				continue
			}
			if m.SourceIP != s.source || m.SourcePort != s.port || m.DestinationPort != s.destinationPort {
				continue
			}
			if m.Host != "" && !strings.EqualFold(m.Host, s.host) {
				continue
			}
			if strings.HasPrefix(o.inbound, "tun/") && m.DestinationIP != s.destination {
				continue
			}
			if owner, ok := o.owners[connection.ID]; ok && owner != index {
				o.invalidate(owner)
				o.invalidate(index)
				continue
			}
			if len(o.owners) >= 4096 {
				continue
			}
			o.owners[connection.ID] = index
			if s.id != "" && s.id != connection.ID {
				o.invalidate(index)
			}
			s.id = connection.ID
			if !s.ambiguous {
				s.evidence = Evidence{Verified: true, Path: o.path(connection.Chains)}
			}
		}
	}
}

func (o *Observer) path(chains []string) string {
	path := "unknown"
	for _, chain := range chains {
		candidate := ""
		if chain == "direct" {
			candidate = "direct"
		}
		for _, protocol := range o.protocols {
			if chain == protocol {
				candidate = "proxy/" + protocol
			}
		}
		if candidate != "" {
			if path != "unknown" && path != candidate {
				return "unknown"
			}
			path = candidate
		}
	}
	return path
}

// Track never waits for the API. Wrap should be used for real connections so
// closure and read errors end the evidence interval.
func (o *Observer) Track(conn net.Conn, host, port string) int {
	source, sourcePort, err := net.SplitHostPort(conn.LocalAddr().String())
	if err != nil {
		return -1
	}
	destination, _, err := net.SplitHostPort(conn.RemoteAddr().String())
	if err != nil {
		return -1
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if len(o.sockets) >= 256 {
		return -1
	}
	o.sockets = append(o.sockets, &socket{source: source, port: sourcePort, destination: destination, destinationPort: port, host: host, began: time.Now(), evidence: Evidence{Path: "unknown"}})
	select {
	case o.wake <- struct{}{}:
	default:
	}
	return len(o.sockets) - 1
}

func (o *Observer) finish(index int) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if index >= 0 && index < len(o.sockets) && o.sockets[index].ended.IsZero() {
		o.sockets[index].ended = time.Now()
	}
}

type observedConn struct {
	net.Conn
	observer *Observer
	index    int
}

func (c *observedConn) Read(data []byte) (int, error) {
	n, err := c.Conn.Read(data)
	if err != nil {
		c.observer.finish(c.index)
	}
	return n, err
}
func (c *observedConn) Write(data []byte) (int, error) {
	n, err := c.Conn.Write(data)
	if err != nil {
		c.observer.finish(c.index)
	}
	return n, err
}
func (c *observedConn) Close() error { c.observer.finish(c.index); return c.Conn.Close() }

func (o *Observer) Wrap(conn net.Conn, host, port string) (net.Conn, int) {
	index := o.Track(conn, host, port)
	if index < 0 {
		return conn, index
	}
	return &observedConn{Conn: conn, observer: o, index: index}, index
}

func (o *Observer) Evidence(index int) Evidence {
	o.mu.Lock()
	defer o.mu.Unlock()
	if index < 0 || index >= len(o.sockets) {
		return Evidence{Path: "unknown"}
	}
	return o.sockets[index].evidence
}

func (o *Observer) Stop() { o.cancel(); <-o.done }
