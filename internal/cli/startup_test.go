package cli

import (
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/service"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

type callbackService struct {
	service.Manager
	start func() error
}

func (s callbackService) Start() error { return s.start() }

func TestPermissionRemainsOpenThroughAsynchronousReadiness(t *testing.T) {
	h := newHarness(t)
	h.svc.active = false
	var ready atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !ready.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	}))
	defer server.Close()
	h.writeLocal(t, func(local *singbox.Local) { local.APIPort = server.Listener.Addr().(*net.TCPAddr).Port })
	completed := make(chan error, 1)
	manager := callbackService{Manager: h.svc, start: func() error {
		h.svc.active = true
		go func() {
			time.Sleep(30 * time.Millisecond)
			engine, err := prepareStartup(h.env, h.layout)
			if err == nil {
				h.svc.engine = engine
				ready.Store(true)
			}
			completed <- err
		}()
		return nil
	}}
	code := withOperation(h.env, func(env Env) int {
		if err := env.startup.start(manager, false); err != nil {
			return fail(env, err)
		}
		if granted, err := maintenance.StartGranted(h.layout); err != nil || granted {
			t.Errorf("grant survived readiness: %t %v", granted, err)
		}
		if err := <-completed; err != nil {
			t.Error(err)
		}
		return 0
	})
	if code != 0 {
		select {
		case err := <-completed:
			t.Logf("wrapper: %v", err)
		default:
		}
		t.Fatalf("asynchronous start: %d %s", code, h.errOut)
	}
}

func TestStartupRetriesATransientEngineProbe(t *testing.T) {
	h := newHarness(t)
	unlock, err := maintenance.Lock(h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	grant, err := maintenance.Permission(h.layout, true)
	if err != nil {
		t.Fatal(err)
	}
	defer grant.Close()
	probe, err := maintenance.LockEngine(h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer probe.Close()
	result := make(chan error, 1)
	go func() {
		engine, err := prepareStartup(h.env, h.layout)
		if engine != nil {
			engine.Close()
		}
		result <- err
	}()
	select {
	case err := <-result:
		t.Fatalf("probe caused premature refusal: %v", err)
	case <-time.After(10 * time.Millisecond):
	}
	probe.Close()
	if err := <-result; err != nil {
		t.Fatal(err)
	}
}

func TestAllStartsRejectAnUnrelatedOperation(t *testing.T) {
	for _, args := range [][]string{{"on"}, {"desktop", "on"}, {"docker", "on", "--yes"}, {"start"}, {"restart"}, {"run"}} {
		t.Run(args[0], func(t *testing.T) {
			h := newHarness(t)
			h.svc.active = false
			paths.WriteFile(install.RecoveryFile(h.layout), []byte("pending fixture"), 0o600)
			h.env.Exec = func(string, []string, io.Writer, string) error { t.Error("executed during replacement"); return nil }
			unlock, err := maintenance.Lock(h.layout)
			if err != nil {
				t.Fatal(err)
			}
			defer unlock()
			if code := Run(h.env, args); code != 1 || h.svc.active || len(h.svc.calls) != 0 {
				t.Fatalf("bypass: %d %v %s", code, h.svc.calls, h.errOut)
			}
			if _, err := os.Stat(install.RecoveryFile(h.layout)); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestControlledStartupRequiresExclusivePermissionAndKeepsItsEngineLease(t *testing.T) {
	h := newHarness(t)
	unlock, err := maintenance.Lock(h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	shared, err := maintenance.Permission(h.layout, false)
	if err != nil {
		t.Fatal(err)
	}
	if engine, err := prepareStartup(h.env, h.layout); !errors.Is(err, maintenance.ErrBusy) {
		engine.Close()
		t.Fatalf("shared permission granted startup: %v", err)
	}
	shared.Close()
	grant, err := maintenance.Permission(h.layout, true)
	if err != nil {
		t.Fatal(err)
	}
	defer grant.Close()
	paths.WriteFile(install.RecoveryFile(h.layout), []byte("pending fixture"), 0o600)
	engine, err := prepareStartup(h.env, h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer engine.Close()
	if len(h.svc.calls) != 0 {
		t.Fatal("controlled startup performed migration")
	}
	if other, err := prepareStartup(h.env, h.layout); !errors.Is(err, maintenance.ErrBusy) {
		other.Close()
		t.Fatalf("duplicate startup: %v", err)
	}
	grant.Close()
	guard, err := newStartupGuard(h.env, h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer guard.close()
	guard.env.startupTimeout = 20 * time.Millisecond
	guard.attempted = h.svc
	// The stalled wrapper owns engine, not fakeService. Even if its old API
	// answers, rollback must refuse until that wrapper has actually exited.
	if err = guard.beforeRestore(); !errors.Is(err, maintenance.ErrBusy) {
		t.Fatalf("restored under stalled wrapper: %v", err)
	}
	engine.Close()
	if err = guard.beforeRestore(); err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeMutationWaitsForStartupButPolicyChangesDoNot(t *testing.T) {
	h := newHarness(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	listener.Close()
	h.writeLocal(t, func(local *singbox.Local) { local.APIPort = port })
	h.env.startupTimeout = 20 * time.Millisecond
	engine, err := maintenance.LockEngine(h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer engine.Close()
	if code := Run(h.env, []string{"port", "3100"}); code != 1 {
		t.Fatal("modified config during startup")
	}
	if code := Run(h.env, []string{"upgrade", "auto", "off"}); code != 0 {
		t.Fatal(h.errOut)
	}
	guard, err := newStartupGuard(h.env, h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer guard.close()
	if err = guard.settle(); !errors.Is(err, maintenance.ErrBusy) {
		t.Fatalf("unready startup accepted: %v", err)
	}
	engine.Close()
	if err = guard.settle(); err != nil {
		t.Fatal(err)
	}
}

func TestStartupRejectsAnOccupiedControllerBeforeExec(t *testing.T) {
	h := newHarness(t)
	h.env.CheckVacant = controllerVacant
	h.env.Exec = func(string, []string, io.Writer, string) error {
		t.Error("executed with an occupied controller")
		return nil
	}
	if code := Run(h.env, []string{"run"}); code != 1 {
		t.Fatal("accepted old listener")
	}
	if busy, err := maintenance.EngineBusy(h.layout); err != nil || busy {
		t.Fatalf("failed startup leaked lease: %t %v", busy, err)
	}
}
