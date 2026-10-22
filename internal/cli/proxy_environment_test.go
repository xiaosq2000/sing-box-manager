package cli

import (
	"bytes"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/service"
	"github.com/xiaosq2000/sing-box-manager/internal/shell"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

func TestEnvRefusesUncertainCaptureBeforeConfigAccess(t *testing.T) {
	for _, state := range []tun.CaptureState{
		{Status: "active", Phase: "active"}, {Status: "dirty", Phase: "pending"},
		{Status: "dirty", Phase: "dirty"}, {Status: "off", Phase: "active"}, {},
	} {
		for _, args := range [][]string{nil, {"on"}} {
			var out, errOut bytes.Buffer
			env := Env{OS: "linux", Stdout: &out, Stderr: &errOut,
				TunCapture: func() tun.CaptureState { return state },
				Layout: func() (paths.Layout, error) {
					t.Error("refused exports accessed configuration")
					return paths.Layout{}, errors.New("not allowed")
				},
			}
			if runEnv(env, args) != 1 || out.Len() != 0 || !strings.Contains(errOut.String(), "unavailable") {
				t.Fatalf("unsafe export guard: stdout=%q stderr=%q", out.String(), errOut.String())
			}
			out.Reset()
			if runEnv(env, []string{"off"}) != 0 || !strings.HasPrefix(out.String(), "unset ") {
				t.Fatal("explicit cleanup became unavailable")
			}
		}
	}
}

type migrationService struct {
	service.Manager
	beforeStop func()
}

func (s migrationService) Stop() error {
	s.beforeStop()
	return s.Manager.Stop()
}

func TestMigrationWarnsBeforeStoppingMixedAndNeverPrintsValues(t *testing.T) {
	h := newHarness(t)
	h.writeLocal(t, func(local *singbox.Local) {
		local.Username, local.Password = "fixture-user", "fixture-private-password"
	})
	values := shell.Values(2080, "fixture-user", "fixture-private-password")
	values["ALL_PROXY"] = "socks5://foreign:foreign-private-password@other:9999"
	h.env.Getenv = func(name string) string { return values[name] }
	h.env.Service = func(string) (service.Manager, error) {
		return migrationService{h.svc, func() {
			if !strings.Contains(h.errOut.String(), "Running programs can retain") {
				t.Error("Mixed stopped before the migration warning")
			}
		}}, nil
	}
	if err := clearMixedProxy(h.env, h.layout); err != nil {
		t.Fatal(err)
	}
	text := h.errOut.String()
	for _, forbidden := range []string{"fixture-private-password", "foreign-private-password", "http://", "socks5://"} {
		if strings.Contains(text, forbidden) {
			t.Fatalf("diagnostic exposed a value: forbidden token %q", forbidden)
		}
	}
	for _, expected := range []string{"http_proxy", "ALL_PROXY", "Other processes were not checked", "Automatic cleanup preserves"} {
		if !strings.Contains(text, expected) {
			t.Errorf("diagnostic lacks %q", expected)
		}
	}
	if h.svc.active || h.out.Len() != 0 {
		t.Fatal("migration did not stop Mixed or printed shell code")
	}
}

func TestStatusDiagnosesOnlyItsInheritedEnvironment(t *testing.T) {
	h := newHarness(t)
	h.env.TunCapture = func() tun.CaptureState { return tun.CaptureState{Status: "dirty", Phase: "pending"} }
	h.env.Getenv = func(name string) string {
		if name == "http_proxy" {
			return "http://127.0.0.1:2080"
		}
		if name == "HTTPS_PROXY" {
			return "http://private:synthetic-secret@external:3128"
		}
		return ""
	}
	if runStatus(h.env) != 1 || !strings.Contains(h.out.String(), "tun:      dirty") {
		t.Fatal("environment warning changed dirty capture status")
	}
	if !strings.Contains(h.errOut.String(), "http_proxy") || !strings.Contains(h.errOut.String(), "HTTPS_PROXY") || strings.Contains(h.errOut.String(), "synthetic-secret") {
		t.Fatal("incorrect or unredacted environment warning")
	}
}

func TestCleanTunActivationNeedsNoMigrationWarning(t *testing.T) {
	h := newHarness(t)
	h.svc.active = false
	warnMixedMigration(h.env, h.layout)
	if h.errOut.Len() != 0 {
		t.Fatal("clean TUN operation should not prescribe application restarts")
	}
	if err := os.WriteFile(h.layout.EnvFile(), []byte("stale"), 0o600); err != nil {
		t.Fatal(err)
	}
	warnMixedMigration(h.env, h.layout)
	if !strings.Contains(h.errOut.String(), "Running programs can retain") {
		t.Fatal("stale settings file needs a migration warning")
	}
}

func TestMigrationWarningUsesChinese(t *testing.T) {
	h := newHarness(t)
	i18n.SetChinese(true)
	defer i18n.SetChinese(false)
	warnMixedMigration(h.env, h.layout)
	if !strings.Contains(h.errOut.String(), "启动环境") || strings.Contains(h.errOut.String(), "Pi") {
		t.Fatal("missing generic Chinese migration guidance")
	}
}
