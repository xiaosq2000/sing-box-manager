package cli

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

type runtimeFixture struct {
	h            *harness
	api          *fakeAPI
	server       *httptest.Server
	capture      tun.CaptureState
	state        tun.State
	mixed        atomic.Int32
	lock         sync.Mutex
	calls        []string
	rejectWrites bool
}

func newRuntimeFixture(t *testing.T) *runtimeFixture {
	t.Helper()
	f := &runtimeFixture{
		h: newHarness(t), api: &fakeAPI{mode: "gfw", protocol: "naive"},
		capture: tun.CaptureState{Status: "active", Phase: "active", Transaction: strings.Repeat("a", 32)},
	}
	mixed := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mixed.Add(1)
		f.h.api.ServeHTTP(w, r)
	}))
	t.Cleanup(mixed.Close)
	address, _ := url.Parse(mixed.URL)
	port, _ := strconv.Atoi(address.Port())
	f.h.writeLocal(t, func(local *singbox.Local) { local.APIPort = port })
	f.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.lock.Lock()
		defer f.lock.Unlock()
		f.calls = append(f.calls, r.Method+" "+r.URL.Path)
		if r.Header.Get("Authorization") != "Bearer tun-runtime-fixture-secret" {
			http.Error(w, "fixture unauthorized", http.StatusUnauthorized)
			return
		}
		if f.rejectWrites && r.Method != http.MethodGet {
			http.Error(w, "fixture mutation refused", http.StatusServiceUnavailable)
			return
		}
		r.Header.Set("Authorization", "Bearer s3cret")
		f.api.ServeHTTP(w, r)
	}))
	t.Cleanup(f.server.Close)
	address, _ = url.Parse(f.server.URL)
	port, _ = strconv.Atoi(address.Port())
	f.state = tun.State{APIPort: port, APISecret: "tun-runtime-fixture-secret", Interface: tun.InterfaceName}
	if err := tun.WriteState(f.h.layout, f.state); err != nil {
		t.Fatal(err)
	}
	f.h.env.TunCapture = func() tun.CaptureState { return f.capture }
	return f
}

func (f *runtimeFixture) requests() []string {
	f.lock.Lock()
	defer f.lock.Unlock()
	return append([]string(nil), f.calls...)
}

func (f *runtimeFixture) selected(mode, protocol string) bool {
	f.lock.Lock()
	defer f.lock.Unlock()
	return f.api.mode == mode && f.api.protocol == protocol
}

func (f *runtimeFixture) untouched(t *testing.T) {
	t.Helper()
	if f.mixed.Load() != 0 || len(f.h.svc.calls) != 0 {
		t.Fatalf("TUN command touched mixed runtime: %d API calls, services %v", f.mixed.Load(), f.h.svc.calls)
	}
	if strings.Contains(f.h.out.String()+f.h.errOut.String(), f.state.APISecret) {
		t.Fatal("TUN command printed the API secret")
	}
}

func TestTUNQueriesAndSwitchesUseOnlyTheOwnerAPI(t *testing.T) {
	f := newRuntimeFixture(t)
	config, _ := os.ReadFile(f.h.layout.ConfigFile())
	state, _ := os.ReadFile(f.h.layout.TunState())
	for _, command := range [][]string{{"route"}, {"protocol"}, {"status"}} {
		f.h.out.Reset()
		if code := Run(f.h.env, command); code != 0 {
			t.Fatalf("%v: %s", command, f.h.errOut)
		}
		if command[0] == "route" && !strings.Contains(f.h.out.String(), "gfw-proxy (current)") {
			t.Fatal(f.h.out)
		}
		if command[0] == "protocol" && !strings.Contains(f.h.out.String(), "naive (current)") {
			t.Fatal(f.h.out)
		}
		if command[0] == "status" && !strings.Contains(f.h.out.String(), "route:    gfw-proxy\nprotocol: naive\n") {
			t.Fatal(f.h.out)
		}
	}
	for _, call := range f.requests() {
		if !strings.HasPrefix(call, "GET ") {
			t.Fatal("query changed the active selection")
		}
	}
	for _, command := range [][]string{{"route", "ai-only"}, {"route", "gfw"}, {"protocol", "trojan"}} {
		if code := Run(f.h.env, command); code != 0 {
			t.Fatalf("%v: %s", command, f.h.errOut)
		}
	}
	if !f.selected("gfw", "trojan") {
		t.Fatal("TUN API did not receive the selection")
	}
	if f.h.api.mode != "china" || f.h.api.protocol != "trojan" {
		t.Fatal("mixed selection changed")
	}
	for _, command := range [][]string{{"route", "not-a-route"}, {"protocol", "not-a-protocol"}} {
		if code := Run(f.h.env, command); code != 1 {
			t.Fatalf("invalid selection accepted: %v", command)
		}
	}
	if !f.selected("gfw", "trojan") {
		t.Fatal("invalid choice changed the selection")
	}
	for path, want := range map[string][]byte{f.h.layout.ConfigFile(): config, f.h.layout.TunState(): state} {
		got, err := os.ReadFile(path)
		if err != nil || !bytes.Equal(got, want) {
			t.Fatal("runtime switch changed installed files")
		}
	}
	f.untouched(t)
}

func TestTUNSelectionNeedsNoMixedConfiguration(t *testing.T) {
	for _, missing := range []bool{true, false} {
		t.Run(strconv.FormatBool(missing), func(t *testing.T) {
			f := newRuntimeFixture(t)
			if missing {
				os.Remove(f.h.layout.ConfigFile())
			} else {
				os.WriteFile(f.h.layout.ConfigFile(), []byte("not JSON"), 0o600)
			}
			for _, command := range [][]string{{"route"}, {"protocol", "trojan"}, {"status"}} {
				if code := Run(f.h.env, command); code != 0 {
					t.Fatalf("%v: %s", command, f.h.errOut)
				}
			}
			f.untouched(t)
		})
	}
}

func TestTUNSelectionRefusesUncommittedAndUnknownCapture(t *testing.T) {
	for _, capture := range []tun.CaptureState{
		{Status: "dirty", Phase: "pending"}, {Status: "dirty", Phase: "dirty"},
		{Status: "active", Phase: "pending", Transaction: strings.Repeat("a", 32)},
		{Status: "active", Phase: "active"}, {Status: "unknown"},
		{Status: "off", Phase: "pending"}, {Status: "off", Phase: "active"}, {Status: "off", Phase: "dirty"},
	} {
		t.Run(capture.Status+"-"+capture.Phase, func(t *testing.T) {
			f := newRuntimeFixture(t)
			f.capture = capture
			for _, command := range [][]string{{"route"}, {"route", "global"}, {"protocol"}, {"protocol", "trojan"}, {"status"}} {
				if code := Run(f.h.env, command); code != 1 {
					t.Fatalf("unsafe state accepted: %v", command)
				}
			}
			if strings.Contains(f.h.out.String(), "tun:      active") {
				t.Fatal("status reported an unsafe generation as active")
			}
			if len(f.requests()) != 0 {
				t.Fatal("unsafe capture called the API")
			}
			f.untouched(t)
		})
	}
}

func TestTUNSelectionNeverFallsBackForBadOwnerStateOrAPI(t *testing.T) {
	for _, broken := range []string{"missing", "malformed", "zero-port", "negative-port", "large-port", "empty-secret", "wrong-interface", "wrong-secret", "stopped-api"} {
		t.Run(broken, func(t *testing.T) {
			f := newRuntimeFixture(t)
			switch broken {
			case "zero-port":
				f.state.APIPort = 0
			case "negative-port":
				f.state.APIPort = -1
			case "large-port":
				f.state.APIPort = 65536
			case "empty-secret":
				f.state.APISecret = ""
			case "wrong-interface":
				f.state.Interface = "another-tun"
			case "wrong-secret":
				f.state.APISecret = "wrong-fixture-secret"
			case "stopped-api":
				f.server.Close()
			}
			if err := tun.WriteState(f.h.layout, f.state); err != nil {
				t.Fatal(err)
			}
			if broken == "missing" {
				os.Remove(f.h.layout.TunState())
			}
			if broken == "malformed" {
				os.WriteFile(f.h.layout.TunState(), []byte("not JSON"), 0o600)
			}
			for _, command := range [][]string{{"route"}, {"route", "global"}, {"protocol"}, {"protocol", "trojan"}, {"status"}} {
				if code := Run(f.h.env, command); code != 1 {
					t.Fatalf("bad TUN state accepted: %v", command)
				}
			}
			if !f.selected("gfw", "naive") {
				t.Fatal("failed command changed selection")
			}
			// The empty credential is invalid, not a printable secret.
			if f.state.APISecret == "" {
				f.state.APISecret = "tun-runtime-fixture-secret"
			}
			f.untouched(t)
		})
	}
}

func TestTUNMutationFailureDoesNotFallBackOrRestart(t *testing.T) {
	f := newRuntimeFixture(t)
	f.lock.Lock()
	f.rejectWrites = true
	f.lock.Unlock()
	for _, command := range [][]string{{"route", "global"}, {"protocol", "trojan"}} {
		if code := Run(f.h.env, command); code != 1 || !strings.Contains(f.h.errOut.String(), "fixture mutation refused") {
			t.Fatalf("failed mutation was not reported: %v: %s", command, f.h.errOut)
		}
	}
	if !f.selected("gfw", "naive") {
		t.Fatal("rejected mutation changed selection")
	}
	f.untouched(t)
}

func TestOffCaptureIgnoresStaleTUNState(t *testing.T) {
	f := newRuntimeFixture(t)
	f.capture = tun.CaptureState{Status: "off", Phase: "off"}
	if code := Run(f.h.env, []string{"route"}); code != 0 || !strings.Contains(f.h.out.String(), "china-direct (current)") {
		t.Fatalf("off did not preserve the mixed path: %s%s", f.h.out, f.h.errOut)
	}
	if f.mixed.Load() != 1 || len(f.requests()) != 0 {
		t.Fatal("off capture used a stale TUN API")
	}
}

func TestOfflineRouteListDoesNotQueryEitherRuntime(t *testing.T) {
	f := newRuntimeFixture(t)
	f.capture = tun.CaptureState{Status: "dirty", Phase: "pending"}
	config := []byte(`{"experimental":{"clash_api":{"default_mode":"china-direct"}}}`)
	if err := os.WriteFile(f.h.layout.ConfigFile(), config, 0o600); err != nil {
		t.Fatal(err)
	}
	if code := Run(f.h.env, []string{"route", "--list"}); code != 0 || !strings.Contains(f.h.out.String(), "china-direct") {
		t.Fatal("installed route list needed an active runtime")
	}
	if len(f.requests()) != 0 {
		t.Fatal("offline list called the TUN API")
	}
	f.untouched(t)
}

func TestTUNMutationsKeepTheOperationLock(t *testing.T) {
	f := newRuntimeFixture(t)
	unlock, err := maintenance.Lock(f.h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	for _, command := range [][]string{{"route", "global"}, {"protocol", "trojan"}} {
		if code := Run(f.h.env, command); code != 1 || !strings.Contains(f.h.errOut.String(), maintenance.ErrBusy.Error()) {
			t.Fatalf("mutation bypassed operation lock: %v", command)
		}
	}
	if len(f.requests()) != 0 {
		t.Fatal("locked mutation reached the API")
	}
	if code := Run(f.h.env, []string{"route"}); code != 0 {
		t.Fatal("operation lock blocked a query")
	}
	f.untouched(t)
}

func TestTUNUnsafeDiagnosticsRefuseBeforeAnyNetworkOrConfigAccess(t *testing.T) {
	for _, capture := range []tun.CaptureState{
		{Status: "dirty", Phase: "pending"}, {Status: "dirty", Phase: "dirty"},
		{Status: "off", Phase: "pending"}, {Status: "off", Phase: "active"}, {Status: "unknown"},
	} {
		for _, command := range [][]string{{"ip"}, {"ip", "--dual-stack"}, {"speed", "download"}, {"check", "https://fixture.test/"}} {
			t.Run(capture.Status+"-"+capture.Phase+"-"+strings.Join(command, "-"), func(t *testing.T) {
				f := newRuntimeFixture(t)
				f.capture = capture
				f.h.env.Layout = func() (paths.Layout, error) {
					t.Fatal("unsafe diagnostic inspected config")
					return paths.Layout{}, nil
				}
				if code := Run(f.h.env, command); code != 1 || !strings.Contains(f.h.errOut.String(), "TUN state is unsafe") {
					t.Fatalf("code %d: %s", code, f.h.errOut)
				}
				if len(f.requests()) != 0 {
					t.Fatal("refused diagnostic called the TUN API")
				}
				f.untouched(t)
			})
		}
	}
}

func TestTUNDiagnosticsRequireGenerationBoundOwnerState(t *testing.T) {
	f := newRuntimeFixture(t)
	if code := Run(f.h.env, []string{"ip"}); code != 1 || !strings.Contains(f.h.errOut.String(), "generation-bound") {
		t.Fatalf("code %d: %s", code, f.h.errOut)
	}
	if len(f.requests()) != 0 {
		t.Fatal("invalid generation called the API")
	}
	f.untouched(t)
}
