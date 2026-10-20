package cli

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

func TestTunOnRefusesARemoteShellBeforeSudo(t *testing.T) {
	host := tun.SystemHost()
	host.GOOS = "linux"
	host.Euid = 1000
	host.Getenv = func(name string) string {
		if name == "SSH_CONNECTION" {
			return "198.51.100.7 12345 203.0.113.9 22"
		}
		return ""
	}
	var sudoCalled bool
	var stdout, stderr bytes.Buffer
	env := Env{
		OS:      "linux",
		Stdout:  &stdout,
		Stderr:  &stderr,
		TunHost: func() tun.Host { return host },
		Sudo: func(string, []string) error {
			sudoCalled = true
			return nil
		},
	}
	if code := runTunOn(env); code != 1 {
		t.Fatalf("runTunOn code = %d", code)
	}
	if sudoCalled {
		t.Fatal("sudo ran before the environment check refused")
	}
	if !strings.Contains(stderr.String(), "SSH") {
		t.Fatalf("stderr = %q", stderr.String())
	}
}

func TestTunOffStopsRootAndClearsLocalState(t *testing.T) {
	layout := paths.Layout{Config: filepath.Join(t.TempDir(), "config"), Data: filepath.Join(t.TempDir(), "data")}
	if err := tun.WriteState(layout, tun.State{APIPort: 63001, APISecret: "secret", Interface: tun.InterfaceName}); err != nil {
		t.Fatal(err)
	}
	var self string
	var args []string
	var stdout, stderr bytes.Buffer
	env := Env{
		OS:     "linux",
		Stdout: &stdout,
		Stderr: &stderr,
		Layout: func() (paths.Layout, error) { return layout, nil },
		Self:   func() (string, error) { return "/tmp/sbc", nil },
		Sudo: func(path string, arguments []string) error {
			self, args = path, arguments
			return nil
		},
	}
	if code := runTunOff(env); code != 0 {
		t.Fatalf("runTunOff code = %d: %s", code, stderr.String())
	}
	if self != "/tmp/sbc" || len(args) != 1 || args[0] != "_tun-off" {
		t.Fatalf("sudo = %q %v", self, args)
	}
	if _, err := os.Stat(tun.StatePath(layout)); !os.IsNotExist(err) {
		t.Fatalf("state survived tun off: %v", err)
	}
}
