package install

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestUpgradeFailureKeepsTheWorkingInstallation(t *testing.T) {
	for _, phase := range []string{"download", "validation", "restart", "readiness", "recovery"} {
		t.Run(phase, func(t *testing.T) {
			p, i, l := installed(t)
			if err := paths.WriteFile(l.SBC(), []byte("previous sbc"), 0o755); err != nil {
				t.Fatal(err)
			}
			os.WriteFile(l.SBCVersion(), []byte("previous\n"), 0o600)
			os.WriteFile(l.SingBoxVersion(), []byte("1.14.1\n"), 0o600)
			// Make the candidate engine and library differ from their backups.
			os.WriteFile(l.SingBox(), []byte("previous engine"), 0o755)
			os.WriteFile(filepath.Join(l.Bin(), "libcronet.so"), []byte("previous library"), 0o755)
			before := map[string][]byte{}
			for _, path := range []string{l.SBC(), l.SBCVersion(), l.SingBox(), l.SingBoxVersion(), l.ConfigFile(), filepath.Join(l.Bin(), "libcronet.so")} {
				data, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				before[path] = data
			}
			if phase == "download" {
				p.tamper = "sbc/linux-amd64/sbc"
			}
			if phase == "validation" {
				p.config = strings.ReplaceAll(p.config, "china", "reject")
			}
			restarts, checks := 0, 0
			restart := func() error {
				restarts++
				if phase == "restart" && restarts == 1 {
					return errors.New("cannot restart candidate")
				}
				return nil
			}
			ready := func() error {
				checks++
				if phase == "recovery" || (phase == "readiness" && checks == 1) {
					return errors.New("not ready")
				}
				return nil
			}
			if _, err := i.Upgrade(link(t, p), "previous", restart, ready); err == nil {
				t.Fatal("accepted a failed upgrade")
			}
			for path, want := range before {
				got, _ := os.ReadFile(path)
				if !bytes.Equal(got, want) {
					t.Errorf("changed %s: %q", filepath.Base(path), got)
				}
			}
			if phase == "download" || phase == "validation" {
				if restarts != 0 {
					t.Fatalf("restarted before validation: %d", restarts)
				}
			} else if restarts != 2 {
				t.Fatalf("restart and rollback count: %d", restarts)
			}
			stages, _ := filepath.Glob(filepath.Join(l.Data, ".upgrade-*"))
			if phase == "recovery" {
				if len(stages) != 1 {
					t.Fatalf("missing recovery files: %v", stages)
				}
			} else if len(stages) != 0 {
				t.Fatalf("temporary stages remain: %v", stages)
			}
		})
	}
}

func TestUpgradeRejectsAnUnusableSignedClient(t *testing.T) {
	for _, binary := range []string{"not an executable", "#!/bin/sh\nprintf 'sbc wrong-build\\n'\n"} {
		t.Run(binary[:3], func(t *testing.T) {
			p, i, l := installed(t)
			if err := paths.WriteFile(l.SBC(), []byte("previous client"), 0o755); err != nil {
				t.Fatal(err)
			}
			p.files["sbc/linux-amd64/sbc"] = []byte(binary)
			if _, err := i.Upgrade(link(t, p), "previous", nil, nil); err == nil || !strings.Contains(err.Error(), "version check") {
				t.Fatalf("unusable candidate: %v", err)
			}
			if data, _ := os.ReadFile(l.SBC()); string(data) != "previous client" {
				t.Fatal("unusable candidate replaced the client")
			}
		})
	}
}

func TestFailedFileRecoveryDoesNotRestartWithMixedFiles(t *testing.T) {
	p, i, l := installed(t)
	if err := paths.WriteFile(l.SBC(), []byte("previous client"), 0o755); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(l.SingBoxVersion(), []byte("1.14.1\n"), 0o600)
	restarts := 0
	restart := func() error {
		restarts++
		data, err := os.ReadFile(RecoveryFile(l))
		if err != nil {
			t.Fatal(err)
		}
		var record upgradeRecovery
		if err = json.Unmarshal(data, &record); err != nil {
			t.Fatal(err)
		}
		if err = os.Remove(record.Files[l.SBC()]); err != nil {
			t.Fatal(err)
		}
		return errors.New("fixture restart failure")
	}
	if _, err := i.Upgrade(link(t, p), "previous", restart, nil); err == nil || !strings.Contains(err.Error(), "recovery failed") {
		t.Fatalf("recovery failure: %v", err)
	}
	if restarts != 1 {
		t.Fatalf("restarted after incomplete file recovery: %d", restarts)
	}
	if _, err := os.Stat(RecoveryFile(l)); err != nil {
		t.Fatal("lost recovery record")
	}
}

func TestUpgradeRecordsInstalledBuildAndDoesNotRestartForSbcOnly(t *testing.T) {
	p, i, l := installed(t)
	os.WriteFile(l.SBCVersion(), []byte("previous\n"), 0o600)
	p.usage = "upload=11; download=22"
	upgraded, err := i.Upgrade(link(t, p), "previous", func() error { t.Error("unnecessary restart"); return nil }, func() error { t.Error("unnecessary readiness check"); return nil })
	if err != nil || upgraded.SBC != "abc123" {
		t.Fatalf("upgrade: %+v %v", upgraded, err)
	}
	data, _ := os.ReadFile(l.SBCVersion())
	if string(data) != "abc123\n" {
		t.Fatalf("installed build %q", data)
	}
	if available, _, err := i.CheckUpgrade(link(t, p), strings.TrimSpace(string(data))); err != nil || available.Any() {
		t.Fatalf("still reports an upgrade: %+v %v", available, err)
	}
	if usage, _, ok := ReadUsage(l); !ok || usage.Upload != 11 || usage.Download != 22 {
		t.Fatalf("traffic: %+v %v", usage, ok)
	}
}
