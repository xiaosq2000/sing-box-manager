package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/contract"
)

func TestApplyPreservesExactConfigBytes(t *testing.T) {
	for name, config := range map[string]json.RawMessage{
		"html":       json.RawMessage(`{"password":"fixture&<>secret"}`),
		"whitespace": json.RawMessage("{\n  \"log\": { \"level\": \"error\" }\n}\n"),
	} {
		t.Run(name, func(t *testing.T) {
			f := newLifecycleFixture(t)
			candidate := f.bundle
			candidate.Generation++
			candidate.Config = config
			candidate.ConfigSHA256 = contract.Hash(config)
			engine := filepath.Join(f.state, "engine")
			put(t, engine, []byte("#!/bin/sh\nexit 0\n"))
			if err := os.Chmod(engine, 0700); err != nil {
				t.Fatal(err)
			}
			// The fake service cannot start. Publication must still select exact bytes.
			if err := apply(context.Background(), f.state, engine, "sbm-g0-proxy.service", candidate); err == nil || err.Error() != "published engine startup failed" {
				t.Fatalf("apply failed before startup: %v", err)
			}
			loaded, _, err := currentBundle(f.state, f.identity.NodeID)
			if err != nil || loaded.ConfigSHA256 != candidate.ConfigSHA256 || !bytes.Equal(loaded.Config, config) {
				t.Fatalf("persisted config changed: %v", err)
			}
		})
	}
}

func TestInvalidStagedConfigDoesNotStopOrPublish(t *testing.T) {
	f := newLifecycleFixture(t)
	candidate := f.bundle
	candidate.Generation++
	candidate.ConfigSHA256 = "invalid-digest"
	engine := filepath.Join(f.state, "engine")
	put(t, engine, []byte("#!/bin/sh\nexit 0\n"))
	if err := os.Chmod(engine, 0700); err != nil {
		t.Fatal(err)
	}
	if err := apply(context.Background(), f.state, engine, "sbm-g0-proxy.service", candidate); err == nil || err.Error() != "staged generation invalid" {
		t.Fatalf("invalid staging accepted: %v", err)
	}
	if _, err := os.Stat(f.properties + ".stopped"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("invalid staging stopped engine")
	}
	current, _, err := currentBundle(f.state, f.identity.NodeID)
	if err != nil || current.Generation != 1 {
		t.Fatal("invalid staging changed policy")
	}
}
