package install

import (
	"bytes"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestInterruptedUpgradeRestoresFilesBeforeTheNextAction(t *testing.T) {
	if config := os.Getenv("SBC_RECOVERY_TEST_CONFIG"); config != "" {
		l := paths.Layout{Config: config, Data: os.Getenv("SBC_RECOVERY_TEST_DATA")}
		i := &Installer{Layout: l, OS: "linux", Arch: "amd64", Keys: []string{os.Getenv("SBC_RECOVERY_TEST_KEY")}, Client: http.DefaultClient, Log: os.Stdout}
		link, err := ReadLink(l)
		if err != nil {
			t.Fatal(err)
		}
		_, err = i.Upgrade(link, "previous", func() error { os.Exit(77); return nil }, nil)
		t.Fatalf("upgrade did not reach the interruption: %v", err)
	}
	p, i, l := installed(t)
	paths.WriteFile(l.SBC(), []byte("previous sbc"), 0o755)
	paths.WriteFile(l.SingBoxVersion(), []byte("1.14.1\n"), 0o600)
	paths.WriteFile(l.Link(), []byte(link(t, p).String()+"\n"), 0o600)
	before, _ := os.ReadFile(l.ConfigFile())
	child := exec.Command(os.Args[0], "-test.run=^TestInterruptedUpgradeRestoresFilesBeforeTheNextAction$")
	child.Env = append(os.Environ(), "SBC_RECOVERY_TEST_CONFIG="+l.Config, "SBC_RECOVERY_TEST_DATA="+l.Data, "SBC_RECOVERY_TEST_KEY="+p.publicKey())
	output, err := child.CombinedOutput()
	if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 77 {
		t.Fatalf("child: %v %s", err, output)
	}
	if data, _ := os.ReadFile(l.SBC()); string(data) != fakeSBC {
		t.Fatalf("candidate was not committed before interruption: %q", data)
	}
	if _, err := i.Upgrade(link(t, p), "previous", nil, nil); err == nil {
		t.Fatal("overwrote pending recovery")
	}
	restarts := 0
	recovered, err := i.Recover(func() error { restarts++; return nil }, func() error { return nil })
	if err != nil || !recovered || restarts != 1 {
		t.Fatalf("recovery: %t %d %v", recovered, restarts, err)
	}
	if data, _ := os.ReadFile(l.SBC()); string(data) != "previous sbc" {
		t.Fatalf("sbc not restored: %q", data)
	}
	if data, _ := os.ReadFile(l.SingBoxVersion()); string(data) != "1.14.1\n" {
		t.Fatalf("engine version not restored: %q", data)
	}
	if data, _ := os.ReadFile(l.ConfigFile()); !bytes.Equal(data, before) {
		t.Fatal("config not restored")
	}
	if data, err := os.ReadFile(l.SBCVersion()); err != nil || string(data) != "previous\n" {
		t.Fatalf("restored build receipt: %q %v", data, err)
	}
	if _, err := os.Stat(RecoveryFile(l)); !os.IsNotExist(err) {
		t.Fatal("recovery record remains")
	}
	if recovered, err := i.Recover(nil, nil); err != nil || recovered {
		t.Fatalf("replayed a completed recovery: %t %v", recovered, err)
	}
}

func TestRecoveryRejectsPathsOutsideItsInstallation(t *testing.T) {
	_, i, l := installed(t)
	r := upgradeRecovery{Stage: l.Data + "/.upgrade-fixture", Files: map[string]string{l.ConfigFile(): l.Data + "/.upgrade-fixture/previous/0"}}
	if err := r.validate(l); err != nil {
		t.Fatal(err)
	}
	foreign := filepath.Join(t.TempDir(), "foreign-config")
	if err := os.WriteFile(foreign, []byte("keep fixture"), 0o600); err != nil {
		t.Fatal(err)
	}
	r.Files[foreign] = ""
	if err := r.save(l); err != nil {
		t.Fatal(err)
	}
	if _, err := i.Recover(nil, nil); err == nil || !strings.Contains(err.Error(), "invalid") {
		t.Fatalf("unsafe recovery: %v", err)
	}
	if data, err := os.ReadFile(foreign); err != nil || string(data) != "keep fixture" {
		t.Fatalf("foreign fixture changed: %q %v", data, err)
	}
}
