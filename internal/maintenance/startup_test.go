package maintenance

import (
	"errors"
	"path/filepath"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestStartupPermissionModesAndOldGrantBarrier(t *testing.T) {
	layout := paths.Layout{Config: filepath.Join(t.TempDir(), "config")}
	op, err := Lock(layout)
	if err != nil {
		t.Fatal(err)
	}
	defer op()
	shared, err := Permission(layout, false)
	if err != nil {
		t.Fatal(err)
	}
	for range 20 {
		probe, err := acquire(layout, ".startup.lock", true)
		if err != nil {
			t.Fatal(err)
		}
		granted, err := StartGranted(layout)
		probe.Close()
		if err != nil || granted {
			t.Fatalf("shared probe granted startup: %t %v", granted, err)
		}
	}
	shared.Close()
	grant, err := Permission(layout, true)
	if err != nil {
		t.Fatal(err)
	}
	defer grant.Close()
	engine, err := LockEngine(layout)
	if err != nil {
		t.Fatal(err)
	}
	defer engine.Close()
	if allowed, err := StartGranted(layout); err != nil || !allowed {
		t.Fatalf("grant: %t %v", allowed, err)
	}
	// Model unordered descriptor closure: the old operation lock disappears
	// before its grant. The next writer cannot enter its write phase yet.
	op()
	op2, err := Lock(layout)
	if err != nil {
		t.Fatal(err)
	}
	defer op2()
	if guard, err := acquire(layout, ".startup.lock", true); !errors.Is(err, ErrBusy) {
		guard.Close()
		t.Fatalf("writer bypassed old grant: %v", err)
	}
	grant.Close()
	shared, err = Permission(layout, false)
	if err != nil {
		t.Fatal(err)
	}
	defer shared.Close()
	if busy, err := EngineBusy(layout); err != nil || !busy {
		t.Fatalf("lost admitted startup: %t %v", busy, err)
	}
	if allowed, err := StartGranted(layout); err != nil || allowed {
		t.Fatalf("revoked grant: %t %v", allowed, err)
	}
}

func TestOnlyAnEngineLeaseCanCrossExec(t *testing.T) {
	layout := paths.Layout{Config: filepath.Join(t.TempDir(), "config")}
	lease, err := Permission(layout, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()
	if err := lease.KeepAcrossExec(); err == nil {
		t.Fatal("permission lease can leak into the engine")
	}
	lease.Close()
	if err := lease.KeepAcrossExec(); err == nil {
		t.Fatal("closed lease accepted")
	}
}
