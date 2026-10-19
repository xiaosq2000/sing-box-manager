package maintenance

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestDefaultsAndSavedOptOut(t *testing.T) {
	l := paths.Layout{Config: filepath.Join(t.TempDir(), "config")}
	s, err := Load(l)
	if err != nil || !s.Refresh.Enabled || !s.Upgrade.Enabled || s.Refresh.Seconds != 21600 || s.Upgrade.Seconds != 21600 {
		t.Fatalf("defaults: %+v %v", s, err)
	}
	if _, err := os.Stat(File(l)); !os.IsNotExist(err) {
		t.Fatal("query wrote defaults")
	}
	s.Refresh.Enabled = false
	s.Upgrade.Enabled = false
	s.Upgrade.Seconds = 86400
	s.Upgrade.LastAttempt = time.Now().UTC()
	s.Upgrade.Result = "failed"
	if err = s.Save(l); err != nil {
		t.Fatal(err)
	}
	loaded, err := Load(l)
	if err != nil || loaded != s {
		t.Fatalf("saved policy: %+v %v", loaded, err)
	}
	for _, text := range []string{"", `{}`, `{"version":2}`, `{"version":1,"refresh":{"interval_seconds":0}}`, `{} {}`} {
		os.WriteFile(File(l), []byte(text), 0o600)
		if _, err := Load(l); err == nil {
			t.Fatalf("accepted invalid settings %q", text)
		}
	}
}
func TestIntervals(t *testing.T) {
	for _, value := range []string{"30m", "6h", "24h", "1m", "720h", "61s"} {
		if _, err := Interval(value); err != nil {
			t.Errorf("%s: %v", value, err)
		}
	}
	for _, value := range []string{"", "30", "59s", "1.001m", "721h", "-1h", "0s", "99999999999h"} {
		if _, err := Interval(value); err == nil {
			t.Errorf("accepted %s", value)
		}
	}
}
func TestOperationLockAcrossProcesses(t *testing.T) {
	if dir := os.Getenv("SBC_LOCK_TEST_DIR"); dir != "" {
		_, err := Lock(paths.Layout{Config: dir})
		if !errors.Is(err, ErrBusy) {
			t.Fatalf("child acquired the parent's lock: %v", err)
		}
		return
	}
	l := paths.Layout{Config: filepath.Join(t.TempDir(), "config")}
	unlock, err := Lock(l)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if unlock != nil {
			unlock()
		}
	}()
	if other, err := Lock(l); !errors.Is(err, ErrBusy) {
		if other != nil {
			other()
		}
		t.Fatalf("second lock: %v", err)
	}
	child := exec.Command(os.Args[0], "-test.run=^TestOperationLockAcrossProcesses$")
	child.Env = append(os.Environ(), "SBC_LOCK_TEST_DIR="+l.Config)
	if out, err := child.CombinedOutput(); err != nil {
		t.Fatalf("child: %v %s", err, out)
	}
	// Uninstallation can remove the config tree without unlinking the guard.
	os.RemoveAll(l.Config)
	if other, err := Lock(l); !errors.Is(err, ErrBusy) {
		if other != nil {
			other()
		}
		t.Fatalf("lock lost after config removal: %v", err)
	}
	unlock()
	unlock, err = Lock(l)
	if err != nil {
		t.Fatal(err)
	}
}
