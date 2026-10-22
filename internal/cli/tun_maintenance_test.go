package cli

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

func TestTunActiveUsesTheInjectedCaptureState(t *testing.T) {
	for _, status := range []string{"off", "active", "dirty", "unknown"} {
		t.Run(status, func(t *testing.T) {
			calls := 0
			env := Env{TunCapture: func() tun.CaptureState {
				calls++
				return tun.CaptureState{Status: status}
			}}
			if got := tunActive(env); got != (status != "off") || calls != 1 {
				t.Fatalf("capture guard=%v, calls=%d", got, calls)
			}
		})
	}
}

type tunMaintenanceFixture struct {
	h         *harness
	capture   tun.CaptureState
	config    []byte
	mixed     []byte
	key       ed25519.PrivateKey
	build     string
	failure   string
	sudoCalls []string
	requests  int
	before    map[string][]byte
}

func newTunMaintenanceFixture(t *testing.T) *tunMaintenanceFixture {
	t.Helper()
	h := newHarness(t)
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	f := &tunMaintenanceFixture{h: h, key: key, build: "new-build",
		capture: tun.CaptureState{Status: "active", Phase: "active", Transaction: strings.Repeat("1", 32)},
		config:  []byte(`{"inbounds":[{"type":"tun","tag":"tun"}],"outbounds":[{"type":"selector","tag":"proxy","outbounds":["trojan","naive"],"default":"naive"}],"route":{"rules":[{"clash_mode":"china-direct"},{"clash_mode":"direct"},{"clash_mode":"global"}]},"experimental":{"clash_api":{"default_mode":"global"},"cache_file":{"enabled":true,"cache_id":"stale-namespace"}}}`)}
	f.mixed, err = os.ReadFile(h.layout.ConfigFile())
	if err != nil {
		t.Fatal(err)
	}
	local, err := singbox.ReadLocal(f.mixed)
	if err != nil {
		t.Fatal(err)
	}
	fingerprint, err := tun.SourceFingerprint(f.config, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = tun.WriteState(h.layout, tun.State{APIPort: local.APIPort, APISecret: local.APISecret, Interface: tun.InterfaceName, SourceHash: fingerprint}); err != nil {
		t.Fatal(err)
	}
	for path, value := range map[string]string{h.layout.SBC(): "#!/bin/sh\nprintf 'sbc old-build\\n'\n", h.layout.SBCVersion(): "old-build\n", h.layout.SingBoxVersion(): "1.14.2\n"} {
		if err = paths.WriteFile(path, []byte(value), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	settings := maintenance.Defaults()
	settings.Scheduled = true
	if err = settings.Save(h.layout); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(server.Close)
	if err = paths.WriteFile(h.layout.Link(), []byte(server.URL+"/sub/abcdefghijklmnopqrstuv\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	h.env.Installer = func(l paths.Layout, log io.Writer) *install.Installer {
		return &install.Installer{Layout: l, Client: server.Client(), Keys: []string{base64.StdEncoding.EncodeToString(key.Public().(ed25519.PublicKey))}, OS: "linux", Arch: "amd64", Log: log}
	}
	h.env.OS = "linux"
	h.env.Self = func() (string, error) { return h.layout.SBC(), nil }
	info, err := os.Stat(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	h.env.TunHost = func() tun.Host {
		return tun.Host{GOOS: "linux", Euid: 1000, Getenv: func(string) string { return "" },
			ReadFile: func(string) ([]byte, error) { return nil, os.ErrNotExist },
			LookPath: func(name string) (string, error) { return "/fixture/bin/" + name, nil },
			Stat: func(path string) (os.FileInfo, error) {
				if path == "/.dockerenv" {
					return nil, os.ErrNotExist
				}
				return info, nil
			}, Shared: func() bool { return false }}
	}
	h.env.TunCapture = func() tun.CaptureState { return f.capture }
	h.env.Sudo = func(_ string, args []string) error { return f.sudo(t, args) }
	h.svc.active = false
	f.before = f.snapshot(t)
	return f
}

func (f *tunMaintenanceFixture) serve(w http.ResponseWriter, r *http.Request) {
	f.requests++
	client := []byte("#!/bin/sh\nprintf 'sbc " + f.build + "\\n'\n")
	sum := sha256.Sum256(client)
	manifest, _ := json.Marshal(map[string]any{"version": 1, "sbc": f.build, "sing_box": "1.14.2", "files": []map[string]any{{"path": "sbc/linux-amd64/sbc", "size": len(client), "sha256": hex.EncodeToString(sum[:])}}})
	switch {
	case strings.HasSuffix(r.URL.Path, "/manifest.json.sig"):
		io.WriteString(w, base64.StdEncoding.EncodeToString(ed25519.Sign(f.key, manifest)))
	case strings.HasSuffix(r.URL.Path, "/manifest.json"):
		w.Write(manifest)
	case strings.HasSuffix(r.URL.Path, "/sbc"):
		w.Write(client)
	default:
		json.NewEncoder(w).Encode(map[string]any{"version": 1, "config": json.RawMessage(f.mixed), "config_tun": json.RawMessage(f.config)})
	}
}

func (f *tunMaintenanceFixture) snapshot(t *testing.T) map[string][]byte {
	t.Helper()
	result := map[string][]byte{}
	for _, path := range []string{f.h.layout.ConfigFile(), f.h.layout.SBC(), f.h.layout.SBCVersion(), f.h.layout.SingBoxVersion(), f.h.layout.TunState()} {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		result[path] = data
	}
	return result
}

func (f *tunMaintenanceFixture) assertSnapshot(t *testing.T, want map[string][]byte, stateRemoved bool) {
	t.Helper()
	for path, data := range want {
		got, err := os.ReadFile(path)
		if path == f.h.layout.TunState() && stateRemoved {
			if !os.IsNotExist(err) {
				t.Fatal("stopped TUN retained API state")
			}
			continue
		}
		if err != nil || !bytes.Equal(data, got) {
			t.Fatalf("unexpected live write: %s: %v", filepath.Base(path), err)
		}
	}
}

func (f *tunMaintenanceFixture) sudo(t *testing.T, args []string) error {
	t.Helper()
	f.sudoCalls = append(f.sudoCalls, args[0])
	argument := func(name string) string {
		for index, value := range args {
			if value == name && index+1 < len(args) {
				return args[index+1]
			}
		}
		return ""
	}
	switch args[0] {
	case "_tun-apply":
		f.assertSnapshot(t, f.before, false)
		data, err := os.ReadFile(argument("--config"))
		if err != nil {
			t.Fatal(err)
		}
		var config struct {
			Experimental struct {
				Clash struct {
					Mode string `json:"default_mode"`
				} `json:"clash_api"`
				Cache struct {
					ID string `json:"cache_id"`
				} `json:"cache_file"`
			} `json:"experimental"`
			Outbounds []struct{ Tag, Default string } `json:"outbounds"`
		}
		if json.Unmarshal(data, &config) != nil || config.Experimental.Clash.Mode != "china-direct" || config.Experimental.Cache.ID != "sbc-tun-"+argument("--transaction") || config.Outbounds[0].Default != "trojan" {
			t.Fatal("candidate did not preserve the current policy before startup")
		}
		if argument("--route") != "china-direct" || argument("--protocol") != "trojan" {
			t.Fatal("root did not receive the current selection")
		}
		if f.failure == "candidate" {
			return errors.New("fixture root candidate rejected")
		}
		f.capture = tun.CaptureState{Status: "dirty", Phase: "pending", Transaction: argument("--transaction")}
		if f.failure == "apply" {
			return errors.New("fixture activation failed")
		}
		if f.failure == "journal" {
			if err := os.Remove(install.RecoveryFile(f.h.layout)); err != nil {
				t.Fatal(err)
			}
			return errors.New("fixture user journal disappeared")
		}
		if f.failure == "publish" {
			if err := os.Mkdir(f.h.layout.SBC()+".tmp", 0o700); err != nil {
				t.Fatal(err)
			}
		}
		if f.failure == "state" {
			if err := os.Rename(f.h.layout.TunState(), f.h.layout.TunState()+".held"); err != nil {
				t.Fatal(err)
			}
			if err := os.Mkdir(f.h.layout.TunState(), 0o700); err != nil {
				t.Fatal(err)
			}
		}
	case "_tun-commit":
		state, err := tun.ReadState(f.h.layout)
		if err != nil || state.SourceHash == "" || state.Transaction != f.capture.Transaction || state.InboundTag != "tun" {
			t.Fatal("root commit preceded generation-bound user API-state publication")
		}
		if _, err = os.Stat(install.RecoveryFile(f.h.layout)); err != nil {
			t.Fatal("root commit had no user recovery record")
		}
		if f.failure == "commit" || f.failure == "stop" {
			return errors.New("fixture commit failed")
		}
		f.capture.Status, f.capture.Phase, f.capture.Committed = "active", "active", true
		if f.failure == "committed-reply" {
			return errors.New("fixture lost the commit reply")
		}
	case "_tun-recover":
		if argument("--transaction") != f.capture.Transaction {
			t.Fatal("recovery targeted another generation")
		}
		if f.failure == "stop" {
			return errors.New("fixture stop failed")
		}
		f.capture.Status, f.capture.Phase = "off", "off"
		if f.failure == "publish" {
			if err := os.Remove(f.h.layout.SBC() + ".tmp"); err != nil {
				t.Fatal(err)
			}
		}
		if f.failure == "state" {
			if err := os.Remove(f.h.layout.TunState()); err != nil {
				t.Fatal(err)
			}
		}
	default:
		t.Fatalf("unexpected root action: %s", args[0])
	}
	return nil
}

func TestTunMaintenanceCommitsWithoutMixedRestart(t *testing.T) {
	for _, action := range []string{"refresh", "upgrade"} {
		t.Run(action, func(t *testing.T) {
			f := newTunMaintenanceFixture(t)
			if code := Run(f.h.env, []string{action}); code != 0 {
				t.Fatalf("%d: %s", code, f.h.errOut)
			}
			if strings.Join(f.sudoCalls, ",") != "_tun-apply,_tun-commit" || f.capture.Phase != "active" {
				t.Fatalf("unexpected lifecycle: %v %+v", f.sudoCalls, f.capture)
			}
			if len(f.h.svc.calls) != 0 {
				t.Fatalf("maintenance used mixed hooks: %v", f.h.svc.calls)
			}
			if _, err := os.Stat(install.RecoveryFile(f.h.layout)); !os.IsNotExist(err) {
				t.Fatal("committed update retained a recovery record")
			}
			data, _ := os.ReadFile(f.h.layout.SBCVersion())
			want := "old-build\n"
			if action == "upgrade" {
				want = "new-build\n"
			}
			if string(data) != want {
				t.Fatal("wrong installed build receipt")
			}
		})
	}
}

func TestTunMaintenanceFailureNeverRestartsAnOldGeneration(t *testing.T) {
	for _, failure := range []string{"candidate", "apply", "journal", "publish", "state", "commit", "stop"} {
		t.Run(failure, func(t *testing.T) {
			f := newTunMaintenanceFixture(t)
			f.failure = failure
			if code := Run(f.h.env, []string{"upgrade"}); code != 1 {
				t.Fatalf("failure returned %d", code)
			}
			if len(f.h.svc.calls) != 0 {
				t.Fatal("failure touched mixed mode")
			}
			if failure == "stop" {
				if f.capture.Status != "dirty" {
					t.Fatal("failed stop lost its block")
				}
				if _, err := os.Stat(install.RecoveryFile(f.h.layout)); err != nil {
					t.Fatal("failed stop lost recovery")
				}
				data, _ := os.ReadFile(f.h.layout.SBCVersion())
				if string(data) != "new-build\n" {
					t.Fatal("rollback overwrote files before shutdown")
				}
				return
			}
			if failure == "candidate" {
				if f.capture.Status != "active" || len(f.sudoCalls) != 1 {
					t.Fatal("candidate rejection stopped the existing TUN")
				}
				f.assertSnapshot(t, f.before, false)
			} else {
				if f.capture.Status != "off" {
					t.Fatal("failed activation retained capture")
				}
				f.assertSnapshot(t, f.before, true)
			}
		})
	}
}

func TestLostTunCommitReplyKeepsTheCommittedGeneration(t *testing.T) {
	f := newTunMaintenanceFixture(t)
	f.failure = "committed-reply"
	if Run(f.h.env, []string{"upgrade"}) == 0 {
		t.Fatal("lost reply was not reported")
	}
	if f.capture.Phase != "active" || f.capture.Transaction == strings.Repeat("1", 32) {
		t.Fatal("committed generation was not preserved")
	}
	if strings.Join(f.sudoCalls, ",") != "_tun-apply,_tun-commit" || len(f.h.svc.calls) != 0 {
		t.Fatal("commit acknowledgement failure restarted a service")
	}
	if _, err := os.Stat(install.RecoveryFile(f.h.layout)); !os.IsNotExist(err) {
		t.Fatal("committed journal was not retired")
	}
	state, err := tun.ReadState(f.h.layout)
	if err != nil || state.Transaction != f.capture.Transaction || state.InboundTag != "tun" {
		t.Fatal("lost acknowledgement discarded diagnostic ownership", state, err)
	}
	if !strings.Contains(f.h.errOut.String(), "new TUN generation is committed") {
		t.Fatalf("incorrect commit boundary: %s", f.h.errOut)
	}
}

func TestInterruptedTunMaintenanceUsesTheRootCommitBoundary(t *testing.T) {
	for _, boundary := range []string{"previous-active", "pending", "off", "committed", "committed-off", "committed-dirty", "other-active", "other-off", "identity-changed"} {
		t.Run(boundary, func(t *testing.T) {
			f := newTunMaintenanceFixture(t)
			i := f.h.env.Installer(f.h.layout, f.h.out)
			link, err := install.ReadLink(f.h.layout)
			if err != nil {
				t.Fatal(err)
			}
			plan, err := i.PrepareTunUpdate(link, "old-build", f.h.layout.SBC(), true)
			if err != nil {
				t.Fatal(err)
			}
			defer plan.Close()
			id := strings.Repeat("2", 32)
			if err = plan.Begin(id, f.capture.Transaction, "china-direct", "trojan"); err != nil {
				t.Fatal(err)
			}
			if boundary != "previous-active" {
				if err = plan.Publish(); err != nil {
					t.Fatal(err)
				}
			}
			want := f.snapshot(t)
			switch boundary {
			case "pending":
				f.capture = tun.CaptureState{Status: "dirty", Phase: "pending", Transaction: id}
			case "off":
				f.capture = tun.CaptureState{Status: "off", Phase: "off", Transaction: id}
			case "committed":
				f.capture = tun.CaptureState{Status: "active", Phase: "active", Transaction: id}
			case "committed-off":
				f.capture = tun.CaptureState{Status: "off", Phase: "off", Transaction: id, Committed: true}
			case "committed-dirty":
				f.capture = tun.CaptureState{Status: "dirty", Phase: "dirty", Transaction: id, Committed: true}
			case "other-active", "other-off":
				status := "active"
				if boundary == "other-off" {
					status = "off"
				}
				f.capture = tun.CaptureState{Status: status, Phase: status, Transaction: strings.Repeat("3", 32)}
			case "identity-changed":
				f.capture = tun.CaptureState{Status: "dirty", Phase: "pending", Transaction: id}
				sudo := f.h.env.Sudo
				f.h.env.Sudo = func(self string, args []string) error {
					err := sudo(self, args)
					f.capture.Transaction = strings.Repeat("3", 32)
					return err
				}
			}
			r, err := i.PendingTunUpdate()
			if err != nil || r == nil {
				t.Fatal("missing typed recovery")
			}
			_, err = recoverTunMaintenance(f.h.env, f.h.layout, r)
			if strings.HasPrefix(boundary, "other-") || boundary == "committed-dirty" || boundary == "identity-changed" {
				wantCalls := 0
				if boundary == "identity-changed" {
					wantCalls = 1
				}
				if err == nil || len(f.sudoCalls) != wantCalls {
					t.Fatal("uncertain recovery changed a generation")
				}
				f.assertSnapshot(t, want, false)
				if _, err := os.Stat(install.RecoveryFile(f.h.layout)); err != nil {
					t.Fatal("uncertain recovery lost its journal")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if boundary == "committed" || boundary == "committed-off" {
				f.assertSnapshot(t, want, boundary == "committed-off")
			} else {
				f.assertSnapshot(t, f.before, boundary != "previous-active")
			}
			if len(f.h.svc.calls) != 0 {
				t.Fatal("recovery started mixed mode")
			}
			if boundary == "pending" && strings.Join(f.sudoCalls, ",") != "_tun-recover" {
				t.Fatal("pending generation was not stopped first")
			}
			if _, err = os.Stat(install.RecoveryFile(f.h.layout)); !os.IsNotExist(err) {
				t.Fatal("finished recovery retained its journal")
			}
		})
	}
}

func TestProxyStartupCannotImplicitlyRecoverATunUpdate(t *testing.T) {
	for _, args := range [][]string{{"on"}, {"start"}, {"restart"}, {"tun", "on"}, {"desktop", "on"}, {"docker", "on", "--yes"}} {
		t.Run(strings.Join(args, "-"), func(t *testing.T) {
			f := newTunMaintenanceFixture(t)
			i := f.h.env.Installer(f.h.layout, f.h.out)
			link, err := install.ReadLink(f.h.layout)
			if err != nil {
				t.Fatal(err)
			}
			plan, err := i.PrepareTunUpdate(link, "old-build", f.h.layout.SBC(), false)
			if err != nil {
				t.Fatal(err)
			}
			defer plan.Close()
			if err = plan.Begin(strings.Repeat("2", 32), f.capture.Transaction, "china-direct", "trojan"); err != nil {
				t.Fatal(err)
			}
			if Run(f.h.env, args) == 0 {
				t.Fatal("startup bypassed explicit TUN recovery")
			}
			if len(f.sudoCalls) != 0 || len(f.h.svc.calls) != 0 {
				t.Fatal("startup changed a service while recovery was pending")
			}
			f.assertSnapshot(t, f.before, false)
			if _, err = os.Stat(install.RecoveryFile(f.h.layout)); err != nil {
				t.Fatal("startup discarded TUN recovery")
			}
		})
	}
}

func TestTunAutomaticMaintenanceOnlyChecksAndHints(t *testing.T) {
	for _, action := range []string{"refresh", "upgrade"} {
		t.Run(action, func(t *testing.T) {
			f := newTunMaintenanceFixture(t)
			f.build = "old-build"
			for _, changed := range []bool{false, true, false} {
				if changed {
					f.config = bytes.Replace(f.config, []byte("stale-namespace"), []byte("changed-namespace"), 1)
				} else {
					f.config = bytes.Replace(f.config, []byte("changed-namespace"), []byte("stale-namespace"), 1)
				}
				if code := Run(f.h.env, []string{"_maintenance", action}); code != 0 {
					t.Fatal(f.h.errOut)
				}
				_, err := os.Stat(f.h.layout.TunUpdateHint())
				if changed != (err == nil) {
					t.Fatal("update hint does not match the checked source")
				}
				f.assertSnapshot(t, f.before, false)
			}
			if len(f.sudoCalls) != 0 || len(f.h.svc.calls) != 0 {
				t.Fatal("automatic check changed a service")
			}
			f.build = "new-build"
			if code := Run(f.h.env, []string{"_maintenance", action}); code != 0 {
				t.Fatal(f.h.errOut)
			}
			if code := Run(f.h.env, []string{"status"}); code != 0 || !strings.Contains(f.h.out.String(), "TUN updates are available") {
				t.Fatal("status lost the manual update instruction")
			}
		})
	}
}

func TestTunMaintenanceRefusesABusyMixedEngine(t *testing.T) {
	f := newTunMaintenanceFixture(t)
	engine, err := maintenance.LockEngine(f.h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer engine.Close()
	if code := Run(f.h.env, []string{"upgrade"}); code != 1 || f.requests != 0 || len(f.sudoCalls) != 0 {
		t.Fatal("maintenance bypassed the mixed engine lease")
	}
}

func TestPolicyChangesWaitForMaintenanceLock(t *testing.T) {
	h := newHarness(t)
	unlock, err := maintenance.Lock(h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	for _, args := range [][]string{{"route", "global"}, {"protocol", "naive"}} {
		if code := Run(h.env, args); code != 1 {
			t.Fatal("policy changed during maintenance")
		}
	}
}
