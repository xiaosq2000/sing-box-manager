//go:build e2e

package e2e

import (
	"strconv"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

// These helpers run only on the disposable Windows runner. Restore its settings
// even when an assertion or a later uninstall fails.
func prepareWindowsSettings(t *testing.T) {
	t.Helper()
	registry := winsettings.PowerShell{}
	for key, names := range map[string][]string{
		winsettings.EnvironmentKey:   {"HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "SOCKS_PROXY", "ALL_PROXY", "NO_PROXY", "Path"},
		winsettings.InternetKey:      {"ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"},
		winsettings.ChromePolicyKey:  {winsettings.WebRtcPolicyName},
		winsettings.EdgePolicyKey:    {winsettings.WebRtcPolicyName},
		winsettings.BravePolicyKey:   {winsettings.WebRtcPolicyName},
		winsettings.FirefoxPolicyKey: {winsettings.FirefoxProxyOnlyName},
	} {
		saved, err := registry.Read(key, names)
		if err != nil {
			t.Fatal(err)
		}
		restore := map[string]winsettings.Value{}
		clear := map[string]winsettings.Value{}
		for _, name := range names {
			restore[name] = saved[name]
			if name != "Path" {
				clear[name] = winsettings.Value{}
			}
		}
		notification := ""
		if key == winsettings.EnvironmentKey {
			notification = "environment"
		} else if key == winsettings.InternetKey {
			notification = "desktop"
		}
		t.Cleanup(func() {
			if err := registry.Apply(key, restore, notification); err != nil {
				t.Errorf("restore Windows settings: %v", err)
			}
		})
		if err := registry.Apply(key, clear, notification); err != nil {
			t.Fatal(err)
		}
	}
}

func checkWindowsSwitches(t *testing.T, layout paths.Layout) {
	t.Helper()
	sbc := layout.SBC()
	registry := winsettings.PowerShell{}
	values, err := registry.Read(winsettings.EnvironmentKey, []string{"HTTP_PROXY", "Path"})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(values["Path"].Text, layout.CLIDir()) {
		t.Fatal("install did not add sbc to user PATH")
	}
	proxy := proxyURL(t, run(t, nil, sbc, "env"))
	if values["HTTP_PROXY"].Text != proxy.String() {
		t.Fatal("install did not set user proxy")
	}
	// Run the generated PowerShell to check that it changes this process's env.
	expect(t, run(t, nil, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
		"& '"+strings.ReplaceAll(sbc, "'", "''")+"' env | Invoke-Expression; [Console]::Write($env:HTTP_PROXY)"), proxy.String())
	run(t, nil, sbc, "desktop", "on")
	expect(t, run(t, nil, sbc, "desktop"), "on (Windows)")
	run(t, nil, sbc, "off")
	values, err = registry.Read(winsettings.EnvironmentKey, []string{"HTTP_PROXY"})
	if err != nil || len(values) != 0 {
		t.Fatalf("off left user environment: %v", err)
	}
	expect(t, run(t, nil, sbc, "desktop"), "off (Windows)")
	expect(t, run(t, nil, sbc, "status"), "service:  running")
	run(t, nil, sbc, "on")
	expect(t, run(t, nil, sbc, "desktop"), "on (Windows)")
	// Port changes must move both desktop and environment with the running proxy.
	oldPort := proxy.Port()
	port := freePort(t)
	run(t, nil, sbc, "port", strconv.Itoa(port))
	values, err = registry.Read(winsettings.EnvironmentKey, []string{"HTTP_PROXY"})
	if err != nil || !strings.HasSuffix(values["HTTP_PROXY"].Text, ":"+strconv.Itoa(port)) {
		t.Fatalf("environment did not follow port: %v", err)
	}
	expect(t, run(t, nil, sbc, "desktop"), "on (Windows)")
	run(t, nil, sbc, "port", oldPort)
}

func checkWindowsSettingsRemoved(t *testing.T, layout paths.Layout) {
	t.Helper()
	registry := winsettings.PowerShell{}
	values, err := registry.Read(winsettings.EnvironmentKey, []string{"HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "SOCKS_PROXY", "NO_PROXY", "Path"})
	if err != nil {
		t.Fatal(err)
	}
	for name, value := range values {
		if name != "Path" {
			t.Errorf("uninstall left %s", name)
		} else if strings.Contains(value.Text, layout.CLIDir()) {
			t.Error("uninstall left sbc on PATH")
		}
	}
	values, err = registry.Read(winsettings.InternetKey, []string{"ProxyEnable"})
	if err != nil || values["ProxyEnable"].Text != "0" {
		t.Fatalf("uninstall left desktop proxy enabled: %v", err)
	}
}

// installWindows runs the portal's one-liner unchanged. Only the interactive
// Read-Host prompt is supplied by the harness; downloads and child processes are
// real, and the installer's platform/elevation checks remain in force.
func installWindows(t *testing.T, engine string, portal *portal) {
	t.Helper()
	const script = `
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Security
$before = Get-ExecutionPolicy -List | Out-String
$script:link = [Console]::In.ReadLine()
$script:prompts = 0
function Read-Host {
    param([string] $Prompt, [switch] $AsSecureString)
    if (-not $AsSecureString -or $script:prompts -ne 0) { throw 'Unexpected installer prompt' }
    $script:prompts++
    return (ConvertTo-SecureString -String $script:link -AsPlainText -Force)
}
& ([scriptblock]::Create((irm $env:SBC_E2E_INSTALLER)))
if ($script:prompts -ne 1) { throw 'The installer did not prompt for its link' }
if ((Get-ExecutionPolicy -List | Out-String) -ne $before) { throw 'The installer changed execution policy' }
`
	output, code := runInput(t, portal.link()+"\n", []string{
		"SBC_E2E_INSTALLER=" + portal.server.URL + "/install.ps1",
		"SBC_ALLOW_ADMIN=1",
	},
		engine, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Restricted", "-Command", script)
	if code != 0 {
		t.Fatalf("%s installer exited %d:\n%s", engine, code, output)
	}
	expect(t, output, "a page loads through the proxy", "sbc is installed. Open a new terminal")
	if strings.Contains(output, token) {
		t.Error("the installer printed the subscription token")
	}
	for _, path := range []string{"/install.ps1", "/sub/" + token + "/files/sbc/windows-amd64/sbc"} {
		if portal.requestCount(path) == 0 {
			t.Errorf("the installer did not download %s", path)
		}
	}
}
