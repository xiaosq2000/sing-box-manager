package install

import (
	"bytes"
	"errors"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"os"
	"strings"
	"testing"
)

func TestUpgradeDoesNotRestoreUntilFailedStartupStops(t *testing.T) {
	p, i, l := installed(t)
	if err := paths.WriteFile(l.SBC(), []byte("previous client"), 0o755); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(l.SingBoxVersion(), []byte("1.14.1\n"), 0o600)
	var candidate []byte
	starts := 0
	i.BeforeRestore = func() error { return errors.New("fixture candidate still holds engine lease") }
	_, err := i.Upgrade(link(t, p), "previous", func() error {
		starts++
		candidate, _ = os.ReadFile(l.SBC())
		return errors.New("fixture readiness failed")
	}, nil)
	if err == nil || !strings.Contains(err.Error(), "recovery failed") || starts != 1 {
		t.Fatalf("unsafe recovery: %d %v", starts, err)
	}
	if data, _ := os.ReadFile(l.SBC()); !bytes.Equal(data, candidate) {
		t.Fatal("restored beneath a live candidate")
	}
	if _, err := os.Stat(RecoveryFile(l)); err != nil {
		t.Fatal("lost recovery journal")
	}
	if _, err := i.Recover(nil, nil); err == nil {
		t.Fatal("recovery bypassed the stop guard")
	}
	if data, _ := os.ReadFile(l.SBC()); !bytes.Equal(data, candidate) {
		t.Fatal("recovery changed live candidate files")
	}
	i.BeforeRestore = func() error { return nil }
	if recovered, err := i.Recover(nil, nil); err != nil || !recovered {
		t.Fatalf("repaired recovery: %t %v", recovered, err)
	}
	if data, _ := os.ReadFile(l.SBC()); string(data) != "previous client" {
		t.Fatal("did not restore previous client")
	}
}

func TestRefreshKeepsBackupWhenCandidateCannotStop(t *testing.T) {
	p, i, l := installed(t)
	previous, _ := os.ReadFile(l.ConfigFile())
	p.config = strings.Replace(p.config, `"china"`, `"gfw"`, 1)
	i.BeforeRestore = func() error { return errors.New("fixture candidate still starting") }
	_, err := i.Refresh(link(t, p), func() error { return errors.New("fixture failed startup") }, func() error { return nil })
	if err == nil || !strings.Contains(err.Error(), "still starting") {
		t.Fatal(err)
	}
	if backup, _ := os.ReadFile(l.ConfigFile() + ".prev"); !bytes.Equal(backup, previous) {
		t.Fatal("lost previous config")
	}
	if live, _ := os.ReadFile(l.ConfigFile()); bytes.Equal(live, previous) {
		t.Fatal("restored before candidate stopped")
	}
}
