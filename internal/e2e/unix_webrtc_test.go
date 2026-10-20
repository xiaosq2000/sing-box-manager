//go:build e2e

package e2e

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func unixChrome(t *testing.T) string {
	t.Helper()
	if runtime.GOOS != "linux" {
		t.Skip("Chrome WebRTC checks are Linux-only")
	}
	for _, name := range []string{"google-chrome", "google-chrome-stable", "google-chrome-beta", "google-chrome-unstable"} {
		if path, err := exec.LookPath(name); err == nil {
			return path
		}
	}
	t.Fatal("the disposable Linux runner needs Google Chrome for the WebRTC regression check")
	return ""
}

// A native Chrome, a fresh profile and loopback STUN prove policy enforcement.
// No command-line flag sets WebRTC policy.
func checkUnixWebRTC(t *testing.T, layout paths.Layout) {
	t.Helper()
	if runtime.GOOS != "linux" || os.Getenv("SBC_E2E") != "1" {
		t.Fatal("Chrome WebRTC checks require a disposable Linux runner")
	}
	chrome := unixChrome(t)
	checkBrowserSTUN(t, chrome, false)
	run(t, nil, layout.SBC(), "privacy", "browser", "on")
	run(t, nil, layout.SBC(), "off")
	run(t, nil, layout.SBC(), "on")
	checkBrowserSTUN(t, chrome, true)
	run(t, nil, layout.SBC(), "privacy", "browser", "off")
	run(t, nil, layout.SBC(), "on")
	checkBrowserSTUN(t, chrome, false)
	run(t, nil, layout.SBC(), "privacy", "browser", "on")
	// Simulate manual deletion while the mode stays enabled, then repair.
	run(t, nil, "sudo", "rm", filepath.Join("/etc/opt/chrome/policies/managed", "sbc-webrtc-"+strconv.Itoa(os.Getuid())+".json"))
	run(t, nil, layout.SBC(), "privacy", "browser", "on")
	checkBrowserSTUN(t, chrome, true)
}

func checkUnixWebRTCRemoved(t *testing.T) {
	t.Helper()
	chrome := unixChrome(t)
	checkBrowserSTUN(t, chrome, false)
}
