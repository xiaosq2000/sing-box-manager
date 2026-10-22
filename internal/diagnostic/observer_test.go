package diagnostic

import (
	"context"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
)

type fixtureConn struct{ net.Conn }

func (fixtureConn) LocalAddr() net.Addr {
	return &net.TCPAddr{IP: net.ParseIP("192.0.2.10"), Port: 45000}
}
func (fixtureConn) RemoteAddr() net.Addr {
	return &net.TCPAddr{IP: net.ParseIP("203.0.113.10"), Port: 443}
}

func TestObserverRequiresSocketIdentityAndNeverGuessesDirect(t *testing.T) {
	base := api.Connection{ID: "new", Metadata: api.ConnectionMetadata{Type: "tun/fixture", Network: "tcp", Host: "fixture.test", SourceIP: "192.0.2.10", SourcePort: "45000", DestinationIP: "203.0.113.10", DestinationPort: "443"}, Chains: []string{"proxy", "snell"}}
	for _, item := range []struct {
		name   string
		mutate func(*api.Connection)
		valid  bool
	}{
		{"matching", func(*api.Connection) {}, true},
		{"wrong-inbound", func(c *api.Connection) { c.Metadata.Type = "mixed/fixture" }, false},
		{"wrong-source", func(c *api.Connection) { c.Metadata.SourceIP = "192.0.2.11" }, false},
		{"wrong-port", func(c *api.Connection) { c.Metadata.SourcePort = "45001" }, false},
		{"wrong-target", func(c *api.Connection) { c.Metadata.DestinationIP = "203.0.113.11" }, false},
		{"wrong-host", func(c *api.Connection) { c.Metadata.Host = "unrelated.test" }, false},
		{"wrong-network", func(c *api.Connection) { c.Metadata.Network = "udp" }, false},
		{"old-id", func(c *api.Connection) { c.ID = "old" }, false},
	} {
		t.Run(item.name, func(t *testing.T) {
			o := &Observer{inbound: "tun/fixture", protocols: []string{"snell"}, baseline: map[string]bool{"old": true}}
			index := o.Track(fixtureConn{}, "fixture.test", "443")
			connection := base
			item.mutate(&connection)
			o.observe([]api.Connection{connection}, time.Now(), time.Now())
			evidence := o.Evidence(index)
			if evidence.Verified != item.valid {
				t.Fatalf("%+v", evidence)
			}
			if !item.valid && evidence.Path != "unknown" {
				t.Fatal("absence became a route claim")
			}
			if item.valid && evidence.Path != "proxy/snell" {
				t.Fatal(evidence)
			}
		})
	}
	o := &Observer{inbound: "tun/fixture", baseline: map[string]bool{}}
	index := o.Track(fixtureConn{}, "fixture.test", "443")
	other := base
	other.ID = "second"
	o.observe([]api.Connection{base, other}, time.Now(), time.Now())
	if o.Evidence(index).Verified {
		t.Fatal("ambiguous record granted proof")
	}
}

func TestObserverCancellationJoinsBoundedWorker(t *testing.T) {
	var calls, inFlight, maximum atomic.Int32
	ctx := context.Background()
	o, err := NewObserver(ctx, "tun/fixture", nil, func(ctx context.Context) ([]api.Connection, error) {
		n := inFlight.Add(1)
		defer inFlight.Add(-1)
		if n > maximum.Load() {
			maximum.Store(n)
		}
		if calls.Add(1) == 1 {
			return nil, nil
		}
		<-ctx.Done()
		return nil, ctx.Err()
	})
	if err != nil {
		t.Fatal(err)
	}
	for range 300 {
		o.Track(fixtureConn{}, "fixture.test", "443")
	}
	if len(o.sockets) != 256 {
		t.Fatal("unbounded tracking")
	}
	time.Sleep(25 * time.Millisecond)
	done := make(chan struct{})
	go func() { o.Stop(); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("worker did not stop")
	}
	if maximum.Load() != 1 || inFlight.Load() != 0 {
		t.Fatal("snapshot worker leaked or multiplied")
	}
}
