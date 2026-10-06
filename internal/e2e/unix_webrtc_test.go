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

type unixBrowser struct{ name, executable, domain, policy, directory string }

func unixBrowsers(t *testing.T) []unixBrowser {
	t.Helper()
	browsers := []unixBrowser{
		{name: "Chrome", executable: "google-chrome", domain: "com.google.Chrome", policy: "WebRtcIPHandling", directory: "/etc/opt/chrome/policies/managed"},
		{name: "Edge", executable: "microsoft-edge", domain: "com.microsoft.Edge", policy: "WebRtcLocalhostIpHandling", directory: "/etc/opt/edge/policies/managed"},
		{name: "Brave", executable: "brave-browser", domain: "com.brave.Browser", policy: "WebRtcIPHandling", directory: "/etc/brave/policies/managed"},
	}
	var installed []unixBrowser
	for _, browser := range browsers {
		if runtime.GOOS == "darwin" {
			applications := map[string]string{"Chrome": "Google Chrome", "Edge": "Microsoft Edge", "Brave": "Brave Browser"}
			app := applications[browser.name]
			browser.executable = "/Applications/" + app + ".app/Contents/MacOS/" + app
		} else if path, err := exec.LookPath(browser.executable); err == nil {
			browser.executable = path
		}
		if info, err := os.Stat(browser.executable); err == nil && !info.IsDir() {
			installed = append(installed, browser)
		} else if browser.name == "Chrome" {
			t.Fatal("the disposable Unix runner needs Chrome for the WebRTC regression check")
		}
	}
	return installed
}

// A native browser, a fresh profile and loopback STUN prove policy enforcement.
// No command-line flag sets WebRTC policy. Chrome is required. Edge and Brave
// also run when installed on the disposable runner.
func checkUnixWebRTC(t *testing.T, layout paths.Layout) {
	t.Helper()
	if runtime.GOOS == "windows" || os.Getenv("SBC_E2E") != "1" {
		t.Fatal("Unix WebRTC checks require a disposable runner")
	}
	browsers := unixBrowsers(t)
	for _, browser := range browsers {
		t.Run(browser.name+"-unprotected", func(t *testing.T) { checkBrowserSTUN(t, browser.executable, false) })
	}
	run(t, nil, layout.SBC(), "webrtc", "on")
	run(t, nil, layout.SBC(), "off")
	run(t, nil, layout.SBC(), "on")
	for _, browser := range browsers {
		t.Run(browser.name+"-protected-after-toggles", func(t *testing.T) { checkBrowserSTUN(t, browser.executable, true) })
	}
	run(t, nil, layout.SBC(), "webrtc", "off")
	run(t, nil, layout.SBC(), "on")
	for _, browser := range browsers {
		t.Run(browser.name+"-opted-out", func(t *testing.T) { checkBrowserSTUN(t, browser.executable, false) })
	}
	run(t, nil, layout.SBC(), "webrtc", "on")
	// Simulate manual deletion while mode stays enabled, then repair explicitly.
	for _, browser := range browsers {
		if runtime.GOOS == "darwin" {
			run(t, nil, "sudo", "/usr/bin/defaults", "delete", "/Library/Managed Preferences/"+browser.domain, browser.policy)
		} else {
			run(t, nil, "sudo", "rm", filepath.Join(browser.directory, "sbc-webrtc-"+strconv.Itoa(os.Getuid())+".json"))
		}
	}
	run(t, nil, layout.SBC(), "webrtc", "on")
	for _, browser := range browsers {
		t.Run(browser.name+"-repaired", func(t *testing.T) { checkBrowserSTUN(t, browser.executable, true) })
	}
}
func checkUnixWebRTCRemoved(t *testing.T) {
	for _, browser := range unixBrowsers(t) {
		t.Run(browser.name+"-after-uninstall", func(t *testing.T) { checkBrowserSTUN(t, browser.executable, false) })
	}
}
