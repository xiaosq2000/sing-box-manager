package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLocalRecoveryPrecedesControlPlaneAccessAndKeepsPolicy(t *testing.T) {
	f := newLifecycleFixture(t)
	inactive(t, f)
	store := fixtureStore(t, f)
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		requests++
		if request.Method != "GET" {
			t.Fatal("failed recovery uploaded status")
		}
		if _, err := os.Stat(f.properties + ".started"); err != nil {
			t.Fatal("network request preceded local recovery")
		}
		return &http.Response{StatusCode: http.StatusServiceUnavailable, Body: io.NopCloser(strings.NewReader(""))}, nil
	})}
	current := &f.bundle
	for i := 0; i < 2; i++ {
		err := cycle(context.Background(), client, f.identity, f.state, f.engine, "sbm-g0-proxy.service", store, &current)
		if err == nil || err.Error() != "published engine startup failed" {
			t.Fatalf("failed local start: %v", err)
		}
	}
	if requests != 2 || current.Generation != 1 {
		t.Fatal("local recovery changed policy")
	}
	gaps, err := store.GapCount(context.Background())
	if err != nil || gaps != 1 {
		t.Fatal("recovery duplicated or omitted coverage uncertainty")
	}
}

func TestUnconfirmedIdentityCannotBeReplacedByNewRemotePolicy(t *testing.T) {
	f := newLifecycleFixture(t)
	f.receipt.Invocation = strings.Repeat("b", 32)
	putJSON(t, filepath.Join(f.state, "running.json"), f.receipt)
	candidate := f.bundle
	candidate.Generation++
	encoded, err := json.Marshal(candidate)
	if err != nil {
		t.Fatal(err)
	}
	data := string(encoded)
	client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(data))}, nil
	})}
	current := &f.bundle
	if err := cycle(context.Background(), client, f.identity, f.state, f.engine, "sbm-g0-proxy.service", fixtureStore(t, f), &current); err == nil || err.Error() != "engine startup identity unconfirmed" {
		t.Fatalf("unconfirmed identity accepted: %v", err)
	}
	if _, err := os.Stat(f.properties + ".stopped"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("unconfirmed engine stopped")
	}
	if _, err := os.Stat(filepath.Join(f.state, "generations/2")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("unconfirmed identity allowed staging")
	}
	selected, _, err := currentBundle(f.state, f.identity.NodeID)
	if err != nil || selected.Generation != 1 {
		t.Fatal("unconfirmed process allowed publication")
	}
}

func TestStoppedServiceStatesAndCgroupGuards(t *testing.T) {
	for _, status := range []serviceState{{active: "inactive"}, {active: "failed"}, {active: "activating", sub: "auto-restart"}} {
		if !status.stopped() {
			t.Fatal("empty stopped state rejected")
		}
		status.pid = 1
		if status.stopped() {
			t.Fatal("live PID accepted as stopped")
		}
	}
	if (serviceState{active: "activating", sub: "start-pre"}).stopped() {
		t.Fatal("live pre-start accepted as stopped")
	}
	for _, group := range []string{"/", "relative", "/system.slice/../escape"} {
		if emptyGroup(group) == nil {
			t.Fatal("unsafe cgroup accepted")
		}
	}
}
