package main

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/counter"
	"google.golang.org/grpc"
)

func sampleServer(t *testing.T, f *lifecycleFixture) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	server.RegisterService(&grpc.ServiceDesc{
		ServiceName: "v2ray.core.app.stats.command.StatsService", HandlerType: (*interface{})(nil),
		Methods: []grpc.MethodDesc{{MethodName: "QueryStats", Handler: func(_ any, _ context.Context, decode func(any) error, _ grpc.UnaryServerInterceptor) (any, error) {
			if err := decode(&counter.QueryStatsRequest{}); err != nil {
				return nil, err
			}
			return &counter.QueryStatsResponse{Stat: []*counter.Stat{{Name: "user>>>trojan:alice>>>traffic>>>downlink", Value: 71}}}, nil
		}}},
	}, &struct{}{})
	go server.Serve(listener)
	t.Cleanup(server.Stop)
	f.bundle.StatsAddress = listener.Addr().String()
	putJSON(t, filepath.Join(f.state, "generations/1/bundle.json"), f.bundle)
}

func TestReplacementCommitsFinalCountersBeforeStopAndPublication(t *testing.T) {
	f := newLifecycleFixture(t)
	sampleServer(t, &f)
	store := fixtureStore(t, f)
	candidate := f.bundle
	candidate.Generation++
	if err := apply(context.Background(), f.state, f.engine, "sbm-g0-proxy.service", candidate, store); err == nil || err.Error() != "published engine startup failed" {
		t.Fatalf("apply: %v", err)
	}
	rows, err := store.Snapshots(context.Background())
	if err != nil || len(rows) != 1 || rows[0].Download != 71 {
		t.Fatalf("missing final counters: %v %v", rows, err)
	}
	// A confirmed ordinary stop must not create an unexpected-termination gap.
	if err := store.Record(context.Background(), strings.Repeat("b", 32), f.bundle.Roster[0].CredentialID, 0, 4, time.Now()); err != nil {
		t.Fatal(err)
	}
	gaps, err := store.GapCount(context.Background())
	if err != nil || gaps != 0 {
		t.Fatalf("ordinary replacement marked uncertain: %d %v", gaps, err)
	}
}

func TestFailedFinalSampleOrCommitLeavesOldEngineAndPointer(t *testing.T) {
	for _, fault := range []string{"counter", "commit"} {
		t.Run(fault, func(t *testing.T) {
			f := newLifecycleFixture(t)
			if fault == "commit" {
				sampleServer(t, &f)
			} else {
				listener, err := net.Listen("tcp", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { listener.Close() }) // Bound but never serves gRPC.
				f.bundle.StatsAddress = listener.Addr().String()
				putJSON(t, filepath.Join(f.state, "generations/1/bundle.json"), f.bundle)
			}
			store := fixtureStore(t, f)
			if fault == "commit" {
				store.Close()
			}
			ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
			defer cancel()
			candidate := f.bundle
			candidate.Generation++
			err := apply(ctx, f.state, f.engine, "sbm-g0-proxy.service", candidate, store)
			if err == nil || !strings.HasPrefix(err.Error(), "final sample failed:") {
				t.Fatalf("failure accepted: %v", err)
			}
			if _, err := os.Stat(f.properties + ".stopped"); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("failed final sample stopped engine")
			}
			selected, _, err := currentBundle(f.state, f.identity.NodeID)
			if err != nil || selected.Generation != 1 {
				t.Fatal("failed final sample changed policy")
			}
		})
	}
}

func TestFailedStopDoesNotPublishOrConfirmFinality(t *testing.T) {
	f := newLifecycleFixture(t)
	sampleServer(t, &f)
	store := fixtureStore(t, f)
	t.Setenv("SBM_TEST_STOP_FAIL", "1")
	candidate := f.bundle
	candidate.Generation++
	if err := apply(context.Background(), f.state, f.engine, "sbm-g0-proxy.service", candidate, store); err == nil || err.Error() != "engine stop failed" {
		t.Fatalf("stop failure: %v", err)
	}
	selected, _, err := currentBundle(f.state, f.identity.NodeID)
	if err != nil || selected.Generation != 1 {
		t.Fatal("failed stop published")
	}
	if err := store.Record(context.Background(), strings.Repeat("b", 32), f.bundle.Roster[0].CredentialID, 0, 4, time.Now()); err != nil {
		t.Fatal(err)
	}
	gaps, err := store.GapCount(context.Background())
	if err != nil || gaps != 1 {
		t.Fatal("failed stop fabricated a finality receipt")
	}
}

func TestRecoveryNeverStartsUnconfirmedLiveIdentity(t *testing.T) {
	f := newLifecycleFixture(t)
	f.receipt.Digest = "wrong"
	putJSON(t, filepath.Join(f.state, "running.json"), f.receipt)
	err := recoverCurrent(context.Background(), f.state, f.engine, "sbm-g0-proxy.service", f.bundle, fixtureStore(t, f))
	if err == nil {
		t.Fatal("unconfirmed process accepted")
	}
	if _, err := os.Stat(f.properties + ".started"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("unconfirmed live process replaced")
	}
}
