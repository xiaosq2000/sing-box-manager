package diagnostic

import (
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
)

func lifecycleRecord(id string) api.Connection {
	return api.Connection{ID: id, Metadata: api.ConnectionMetadata{Type: "tun/fixture", Network: "tcp", Host: "fixture.test", SourceIP: "192.0.2.10", SourcePort: "45000", DestinationIP: "203.0.113.10", DestinationPort: "443"}, Chains: []string{"direct"}}
}

func TestClosedSocketCannotBorrowProofAfterPortReuse(t *testing.T) {
	o := &Observer{inbound: "tun/fixture", baseline: map[string]bool{}}
	a := o.Track(fixtureConn{}, "fixture.test", "443")
	o.finish(a)
	b := o.Track(fixtureConn{}, "fixture.test", "443")
	o.observe([]api.Connection{lifecycleRecord("new-b")}, time.Now(), time.Now())
	if evidence := o.Evidence(a); evidence.Verified || evidence.Path != "unknown" {
		t.Fatalf("closed A borrowed B: %+v", evidence)
	}
	if !o.Evidence(b).Verified {
		t.Fatal("live B lost its own proof")
	}
}

func TestDelayedSnapshotCannotCrossSocketLifetimes(t *testing.T) {
	o := &Observer{inbound: "tun/fixture", baseline: map[string]bool{}}
	a := o.Track(fixtureConn{}, "fixture.test", "443")
	began := time.Now()
	o.finish(a)
	b := o.Track(fixtureConn{}, "fixture.test", "443")
	o.observe([]api.Connection{lifecycleRecord("late-a")}, began, time.Now())
	if o.Evidence(a).Verified || o.Evidence(b).Verified {
		t.Fatal("delayed snapshot granted expired evidence")
	}
}

func TestConnectionIDHasOneOwnerAndAmbiguityHasNoPath(t *testing.T) {
	o := &Observer{inbound: "tun/fixture", baseline: map[string]bool{}}
	a := o.Track(fixtureConn{}, "fixture.test", "443")
	o.observe([]api.Connection{lifecycleRecord("same-id")}, time.Now(), time.Now())
	o.finish(a)
	b := o.Track(fixtureConn{}, "fixture.test", "443")
	o.observe([]api.Connection{lifecycleRecord("same-id")}, time.Now(), time.Now())
	for _, index := range []int{a, b} {
		if e := o.Evidence(index); e.Verified || e.Path != "unknown" {
			t.Fatalf("ambiguous ownership: %+v", e)
		}
	}
	c := &Observer{inbound: "tun/fixture", protocols: []string{"snell"}, baseline: map[string]bool{}}
	index := c.Track(fixtureConn{}, "fixture.test", "443")
	record := lifecycleRecord("contradictory")
	record.Chains = []string{"direct", "snell"}
	c.observe([]api.Connection{record}, time.Now(), time.Now())
	if e := c.Evidence(index); !e.Verified || e.Path != "unknown" {
		t.Fatalf("contradictory chain produced a route claim: %+v", e)
	}
}
