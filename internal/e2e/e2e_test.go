//go:build e2e

// Package e2e installs sbc on the machine that runs it, through the hosted
// installer and a stand-in portal, and drives it through its life. It changes
// the current user's services, so it builds only with the e2e tag and runs
// only where SBC_E2E=1 says the machine is disposable, such as a CI runner.
package e2e

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestInstallSwitchRollBackAndUninstall(t *testing.T) {
	if os.Getenv("SBC_E2E") != "1" {
		t.Skip("this installs sbc for the current user; set SBC_E2E=1 on a disposable machine")
	}
	if runtime.GOOS == "windows" {
		for _, engine := range []string{"powershell.exe", "pwsh.exe"} {
			if _, err := exec.LookPath(engine); err != nil {
				continue
			}
			t.Run(engine, func(t *testing.T) { testLifecycle(t, engine) })
		}
		return
	}
	testLifecycle(t, "")
}

func testLifecycle(t *testing.T, engine string) {
	t.Helper()
	layout, err := paths.Default()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(layout.ConfigFile()); err == nil {
		t.Fatal("sbc is installed here, and this test would replace it; run it on a disposable machine")
	}
	if runtime.GOOS == "windows" {
		prepareWindowsSettings(t)
	}
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	version := pinnedSingBox(t, root)
	platform := runtime.GOOS + "-" + runtime.GOARCH
	archive := fetchSingBox(t, version, platform)
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	built := buildSBC(t, root, base64.StdEncoding.EncodeToString(public))
	sbcBinary, err := os.ReadFile(built)
	if err != nil {
		t.Fatal(err)
	}
	upstream := startUpstream(t, extractSingBox(t, archive))
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "reached")
	}))
	t.Cleanup(site.Close)
	installer, err := os.ReadFile(filepath.Join(root, "sing_box_manager", "web", "client", "install.ps1"))
	if err != nil {
		t.Fatal(err)
	}
	portal := startPortal(t, private, version, map[string][]byte{
		"sing-box/" + archiveName(version, platform): archive,
		"sbc/" + platform + "/sbc":                   sbcBinary,
	}, clientConfig(upstream, 0), installer)

	sbc := layout.SBC()
	t.Cleanup(func() {
		// Leaves the machine as it was when a step failed partway.
		if _, err := os.Stat(sbc); err == nil {
			exec.Command(sbc, "uninstall", "--yes").Run()
		}
	})

	if runtime.GOOS == "windows" {
		installWindows(t, engine, portal)
	} else {
		// The hosted installer, as a person runs it, reading the link from a
		// file in place of the terminal.
		answer := filepath.Join(t.TempDir(), "link")
		if err := os.WriteFile(answer, []byte(portal.link()+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		installer := filepath.Join(root, "sing_box_manager", "web", "client", "install.sh")
		expect(t, run(t, []string{"SBC_INSTALL_TTY=" + answer}, "sh", installer, "--no-rc", "--shell", "bash"),
			"a page loads through the proxy")
	}
	expect(t, run(t, nil, sbc, "version"), "sbc e2e")
	expect(t, run(t, nil, sbc, "status"), "service:  running", "route:    china", "protocol: socks", "traffic:")
	proxy := proxyURL(t, run(t, nil, sbc, "env"))
	expectPage(t, proxy, site.URL)
	if runtime.GOOS == "windows" {
		checkWindowsSwitches(t, layout)
	} else {
		checkUnixWebRTC(t, layout)
	}
	pid := singBoxPID(t, layout.SingBox())

	// Route and protocol switch through sing-box's API, without a restart.
	run(t, nil, sbc, "route", "gfw")
	run(t, nil, sbc, "protocol", "http")
	expect(t, run(t, nil, sbc, "status"), "route:    gfw", "protocol: http")
	if now := singBoxPID(t, layout.SingBox()); now != pid {
		t.Errorf("switching restarted sing-box: pid %s became %s", pid, now)
	}
	expectPage(t, proxy, site.URL)
	run(t, nil, sbc, "speed", "--all")

	// A refresh whose config cannot start rolls back to the last good one,
	// and the cache file keeps the route and protocol across the restarts.
	blocked := holdPort(t)
	portal.setConfig(clientConfig(upstream, blocked))
	output, code := runCode(t, nil, sbc, "update")
	if code == 0 {
		t.Fatalf("a config that cannot start was applied:\n%s", output)
	}
	expect(t, output, "the previous one is back")
	config, err := os.ReadFile(layout.ConfigFile())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(config), blockedTag) {
		t.Error("the config that cannot start stayed in place")
	}
	expectPage(t, proxy, site.URL)
	expect(t, run(t, nil, sbc, "status"), "service:  running", "route:    gfw", "protocol: http")
	portal.setConfig(clientConfig(upstream, 0))

	// Uninstall removes the service, the process and every file. On Windows
	// the directory sbc runs from goes a moment after sbc has exited.
	expect(t, run(t, nil, sbc, "uninstall", "--yes"), "sbc is removed.")
	expectServiceGone(t)
	if runtime.GOOS == "windows" {
		checkWindowsSettingsRemoved(t, layout)
	} else {
		checkUnixWebRTCRemoved(t)
	}
	for _, path := range []string{layout.Config, layout.Data} {
		for try := 0; ; try++ {
			if _, err := os.Stat(path); os.IsNotExist(err) {
				break
			}
			if try == 20 {
				t.Errorf("uninstall left %s", path)
				break
			}
			time.Sleep(500 * time.Millisecond)
		}
	}
	if pids := pgrep(layout.SingBox()); pids != "" {
		t.Errorf("sing-box still runs after uninstall: %s", pids)
	}
}
