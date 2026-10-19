package cli

import (
	"os"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestPolicyChangesDoNotRecoverOrRestartAnInterruptedUpgrade(t *testing.T) {
	h := newHarness(t)
	paths.WriteFile(install.RecoveryFile(h.layout), []byte("invalid fixture recovery record"), 0o600)
	if code := Run(h.env, []string{"upgrade", "auto", "off"}); code != 0 {
		t.Fatalf("policy change: %d %s", code, h.errOut)
	}
	if code := Run(h.env, []string{"_maintenance", "upgrade"}); code != 0 {
		t.Fatalf("disabled worker: %d %s", code, h.errOut)
	}
	for _, call := range h.svc.calls {
		if call != "schedule" && call != "retire-refresh" {
			t.Fatalf("policy performed %s", call)
		}
	}
	if _, err := os.Stat(install.RecoveryFile(h.layout)); err != nil {
		t.Fatal("policy removed recovery evidence")
	}
}

func TestHintCleanupWaitsForACommittedUpgrade(t *testing.T) {
	h := newHarness(t)
	paths.WriteFile(h.layout.UpgradeHint(), []byte(h.env.Version+" \n"), 0o600)
	unlock, err := maintenance.Lock(h.layout)
	if err != nil {
		t.Fatal(err)
	}
	discardStaleUpgradeHint(h.layout, h.env.Version)
	unlock()
	if _, err := os.Stat(h.layout.UpgradeHint()); err != nil {
		t.Fatal("removed hint during an active transaction")
	}
	paths.WriteFile(install.RecoveryFile(h.layout), []byte("pending fixture"), 0o600)
	discardStaleUpgradeHint(h.layout, h.env.Version)
	if _, err := os.Stat(h.layout.UpgradeHint()); err != nil {
		t.Fatal("removed hint before interrupted recovery")
	}
}

func TestStatusAndShellInitDiscardHintsWrittenByAnOlderUpdater(t *testing.T) {
	for _, command := range [][]string{{"status"}, {"init", "bash"}} {
		t.Run(command[0], func(t *testing.T) {
			h := newHarness(t)
			paths.WriteFile(h.layout.SBCVersion(), []byte("installed-build\n"), 0o600)
			paths.WriteFile(h.layout.SingBoxVersion(), []byte("1.14.2\n"), 0o600)
			for _, hint := range []string{"installed-build \n", " 1.14.2\n", "installed-build 1.14.2\n"} {
				paths.WriteFile(h.layout.UpgradeHint(), []byte(hint), 0o600)
				if code := Run(h.env, command); code != 0 {
					t.Fatalf("%v: %d %s", command, code, h.errOut)
				}
				if _, err := os.Stat(h.layout.UpgradeHint()); !os.IsNotExist(err) {
					t.Fatalf("stale hint remains: %q", hint)
				}
			}
			paths.WriteFile(h.layout.UpgradeHint(), []byte("next-build \n"), 0o600)
			Run(h.env, command)
			if _, err := os.Stat(h.layout.UpgradeHint()); err != nil {
				t.Fatal("a genuine update notification was removed")
			}
		})
	}
}
