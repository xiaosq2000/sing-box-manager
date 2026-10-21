//go:build !windows

package tun

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
)

// Umask is process-wide. Change it only in disposable test subprocesses.
func TestTUNDirectoryModesIgnoreCallerUmask(t *testing.T) {
	if scenario := os.Getenv("SBC_TUN_PERMISSION_SCENARIO"); scenario != "" {
		parts := strings.Split(scenario, "/")
		if len(parts) != 2 {
			t.Fatal("invalid permission fixture scenario")
		}
		mask, err := strconv.ParseInt(parts[0], 8, 32)
		if err != nil {
			t.Fatal(err)
		}
		previous := syscall.Umask(int(mask))
		defer syscall.Umask(previous)
		checkTUNDirectoryModes(t, parts[1] == "existing-private")
		return
	}
	for _, scenario := range []string{"022/fresh", "077/fresh", "022/existing-private", "077/existing-private"} {
		t.Run(scenario, func(t *testing.T) {
			command := exec.Command(os.Args[0], "-test.run=^TestTUNDirectoryModesIgnoreCallerUmask$", "-test.v")
			command.Env = append(os.Environ(), "SBC_TUN_PERMISSION_SCENARIO="+scenario)
			if output, err := command.CombinedOutput(); err != nil {
				t.Fatalf("permission subprocess failed: %v\n%s", err, output)
			}
		})
	}
}

func checkTUNDirectoryModes(t *testing.T, existingPrivate bool) {
	t.Helper()
	opts, _, stop := lifecycleOptions(t)
	if existingPrivate {
		for _, dir := range []string{filepath.Dir(opts.Paths.ConfigDir), opts.Paths.ControlDir()} {
			if err := os.MkdirAll(dir, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(dir, 0o700); err != nil {
				t.Fatal(err)
			}
		}
	}
	unlock, err := controlLock(opts.Paths)
	if err != nil {
		t.Fatal(err)
	}
	unlock()
	assertPermissionMode(t, filepath.Dir(opts.Paths.ConfigDir), 0o755)
	assertPermissionMode(t, opts.Paths.ControlDir(), 0o755)
	if err := os.WriteFile(filepath.Join(opts.BinDir, "libcronet.so"), []byte("fixture library"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(opts.RulesDir, "fixture.srs"), []byte("fixture rules"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Apply(opts); err != nil {
		t.Fatal(err)
	}
	for path, want := range map[string]os.FileMode{
		filepath.Dir(opts.Paths.ConfigDir): 0o755,
		opts.Paths.ControlDir():            0o755,
		opts.Paths.ConfigDir:               0o750,
		opts.Paths.RulesDir:                0o750,
		opts.Paths.BinaryDir:               0o755,
		opts.Paths.StateDir:                0o700,
		opts.Paths.Config:                  0o640,
		opts.Paths.Owner:                   0o640,
		opts.Paths.Binary:                  0o755,
		filepath.Join(opts.Paths.BinaryDir, "libcronet.so"):              0o644,
		filepath.Join(opts.Paths.RulesDir, "fixture.srs"):                0o640,
		opts.Paths.journal():                                             0o644,
		opts.Paths.blocked():                                             0o644,
		filepath.Join(opts.Paths.ControlDir(), "recover-sbc"):            0o755,
		filepath.Join(opts.Paths.ControlDir(), "control.operation.lock"): 0o600,
		opts.BinDir:   0o700,
		opts.RulesDir: 0o700,
	} {
		assertPermissionMode(t, path, want)
	}
	if err := Commit(stop, opts.Transaction); err != nil {
		t.Fatal(err)
	}
	if err := Stop(stop); err != nil {
		t.Fatal(err)
	}
	assertPhase(t, opts.Paths, "off")
	assertPermissionMode(t, opts.Paths.ControlDir(), 0o755)
	assertPermissionMode(t, opts.Paths.journal(), 0o644)
	assertPermissionMode(t, opts.Paths.StateDir, 0o700)
}

func assertPermissionMode(t *testing.T, path string, want os.FileMode) {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Error(err)
		return
	}
	if got := info.Mode().Perm(); got != want {
		t.Errorf("%s mode = %04o, want %04o", path, got, want)
	}
}
