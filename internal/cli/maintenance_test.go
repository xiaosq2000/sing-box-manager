package cli

import (
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
	"strings"
	"sync/atomic"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestMaintenancePolicyQueriesAndIndependentChanges(t *testing.T) {
	h := newHarness(t)
	for _, action := range []string{"refresh", "upgrade"} {
		h.out.Reset()
		if code := Run(h.env, []string{action, "auto"}); code != 0 || h.out.String() != "on\n" {
			t.Fatalf("default %s: %d %s", action, code, h.out)
		}
	}
	if _, err := os.Stat(maintenance.File(h.layout)); !os.IsNotExist(err) {
		t.Fatal("query wrote settings")
	}
	if len(h.svc.calls) != 0 {
		t.Fatalf("query touched service: %v", h.svc.calls)
	}
	for _, args := range [][]string{{"refresh", "auto", "off"}, {"upgrade", "interval", "24h"}, {"upgrade", "auto", "off"}} {
		if code := Run(h.env, args); code != 0 {
			t.Fatalf("%v: %d %s", args, code, h.errOut)
		}
	}
	s, err := maintenance.Load(h.layout)
	if err != nil || s.Refresh.Enabled || s.Upgrade.Enabled || s.Refresh.Seconds != 21600 || s.Upgrade.Seconds != 86400 || !s.Scheduled {
		t.Fatalf("saved settings: %+v %v", s, err)
	}
	for _, call := range h.svc.calls {
		if call != "schedule" && call != "retire-refresh" {
			t.Fatalf("policy performed %s", call)
		}
	}
	if code := Run(h.env, []string{"refresh", "interval", "1h"}); code != 0 {
		t.Fatal(h.errOut)
	}
	if h.svc.plan.Refresh.Enabled || h.svc.plan.Upgrade.Enabled || h.svc.plan.Upgrade.Seconds != 86400 {
		t.Fatalf("changed other policy: %+v", h.svc.plan)
	}
}

func TestMaintenanceScheduleFailurePreservesExplicitOptOut(t *testing.T) {
	h := newHarness(t)
	h.svc.scheduleErr = errors.New("scheduler unavailable")
	if code := Run(h.env, []string{"upgrade", "auto", "off"}); code != 1 {
		t.Fatalf("code %d", code)
	}
	s, err := maintenance.Load(h.layout)
	if err != nil || s.Upgrade.Enabled || s.Scheduled {
		t.Fatalf("unsafe settings: %+v %v", s, err)
	}
	h.svc.scheduleErr = nil
	if err := ensureMaintenance(h.env, h.layout, false); err != nil {
		t.Fatal(err)
	}
	if h.svc.plan.Upgrade.Enabled {
		t.Fatal("retry lost opt-out")
	}
}

func TestMaintenanceMigratesOnceAndReinstallPreservesPolicies(t *testing.T) {
	h := newHarness(t)
	if code := Run(h.env, []string{"update"}); code != 0 {
		t.Fatalf("migration: %d %s", code, h.errOut)
	}
	if !h.svc.plan.Refresh.Enabled || !h.svc.plan.Upgrade.Enabled {
		t.Fatal("old install did not receive both defaults")
	}
	if code := Run(h.env, []string{"update"}); code != 1 {
		t.Fatal("legacy command remains an action alias")
	}
	if code := Run(h.env, []string{"upgrade", "auto", "off"}); code != 0 {
		t.Fatal(h.errOut)
	}
	self := h.layout.SBC()
	if err := paths.WriteFile(self, []byte("fixture sbc"), 0o755); err != nil {
		t.Fatal(err)
	}
	h.env.Self = func() (string, error) { return self, nil }
	if err := installService(h.env, h.layout); err != nil {
		t.Fatal(err)
	}
	if h.svc.plan.Upgrade.Enabled || !h.svc.plan.Refresh.Enabled {
		t.Fatal("reinstall reset policies")
	}
}

func TestMaintenanceStartupDoesNotBlockAnUpgradeOrForgetSavedSettings(t *testing.T) {
	h := newHarness(t)
	unlock, err := maintenance.Lock(h.layout)
	if err != nil {
		t.Fatal(err)
	}
	initializeMaintenance(h.env, h.layout)
	if len(h.svc.calls) != 0 {
		t.Fatal("proxy startup changed schedules during an upgrade")
	}
	unlock()
	initializeMaintenance(h.env, h.layout)
	if !h.svc.plan.Refresh.Enabled || !h.svc.plan.Upgrade.Enabled {
		t.Fatal("startup did not migrate")
	}
	calls := len(h.svc.calls)
	initializeMaintenance(h.env, h.layout)
	if len(h.svc.calls) != calls {
		t.Fatal("startup reinstalled schedules")
	}
}

func TestFirstUpgradeClearsTheHintAndRefreshUsesTheInstalledBuild(t *testing.T) {
	h := newHarness(t)
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	binary := []byte("#!/bin/sh\nprintf 'sbc new-build\\n'\n")
	sum := sha256.Sum256(binary)
	manifest, _ := json.Marshal(map[string]any{"version": 1, "sbc": "new-build", "sing_box": "1.14.2", "files": []map[string]any{{"path": "sbc/linux-amd64/sbc", "size": len(binary), "sha256": hex.EncodeToString(sum[:])}}})
	config, err := os.ReadFile(h.layout.ConfigFile())
	if err != nil {
		t.Fatal(err)
	}
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		switch {
		case strings.HasSuffix(r.URL.Path, "/manifest.json.sig"):
			io.WriteString(w, base64.StdEncoding.EncodeToString(ed25519.Sign(private, manifest)))
		case strings.HasSuffix(r.URL.Path, "/manifest.json"):
			w.Write(manifest)
		case strings.HasSuffix(r.URL.Path, "/sbc"):
			w.Write(binary)
		default:
			json.NewEncoder(w).Encode(map[string]any{"version": 1, "config": json.RawMessage(config)})
		}
	}))
	defer server.Close()
	paths.WriteFile(h.layout.Link(), []byte(server.URL+"/sub/abcdefghijklmnopqrstuv\n"), 0o600)
	paths.WriteFile(h.layout.SingBoxVersion(), []byte("1.14.2\n"), 0o600)
	paths.WriteFile(h.layout.SingBox(), []byte("#!/bin/sh\nexit 0\n"), 0o755)
	paths.WriteFile(h.layout.SBC(), []byte("fixture old sbc"), 0o755)
	paths.WriteFile(h.layout.UpgradeHint(), []byte("new-build \n"), 0o600)
	h.env.Installer = func(l paths.Layout, log io.Writer) *install.Installer {
		return &install.Installer{Layout: l, Client: server.Client(), Keys: []string{base64.StdEncoding.EncodeToString(public)}, OS: "linux", Arch: "amd64", Log: log}
	}
	settings := maintenance.Defaults()
	settings.Scheduled = true
	settings.Refresh.Enabled = false
	settings.Upgrade.Enabled = false
	if err = settings.Save(h.layout); err != nil {
		t.Fatal(err)
	}
	// A disabled automatic worker must not access the subscription.
	if code := Run(h.env, []string{"_maintenance", "upgrade"}); code != 0 || requests.Load() != 0 {
		t.Fatalf("disabled worker: %d, %d requests", code, requests.Load())
	}
	oldVersion := h.env.Version
	if code := Run(h.env, []string{"upgrade"}); code != 0 {
		t.Fatalf("first upgrade: %d %s", code, h.errOut)
	}
	if _, err := os.Stat(h.layout.UpgradeHint()); !os.IsNotExist(err) {
		t.Fatal("first upgrade left its notification")
	}
	if h.env.Version != oldVersion {
		t.Fatal("test did not retain the executing process's old build")
	}
	if code := Run(h.env, []string{"refresh"}); code != 0 {
		t.Fatalf("refresh: %d %s", code, h.errOut)
	}
	if _, err := os.Stat(h.layout.UpgradeHint()); !os.IsNotExist(err) {
		t.Fatal("refresh recreated a hint using the old process version")
	}
	settings, err = maintenance.Load(h.layout)
	if err != nil || settings.Upgrade.Result != "ok" || settings.Upgrade.LastSuccess.IsZero() || settings.Refresh.Result != "ok" || settings.Refresh.Enabled || settings.Upgrade.Enabled {
		t.Fatalf("execution state: %+v %v", settings, err)
	}
}

func TestManualMaintenanceRefusesConcurrentMutation(t *testing.T) {
	h := newHarness(t)
	unlock, err := maintenance.Lock(h.layout)
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	for _, args := range [][]string{{"refresh"}, {"upgrade"}, {"upgrade", "auto", "off"}, {"port", "3100"}, {"link", "set"}} {
		h.errOut.Reset()
		if code := Run(h.env, args); code != 1 || !strings.Contains(h.errOut.String(), "another sbc") {
			t.Fatalf("%v: %d %s", args, code, h.errOut)
		}
	}
}
