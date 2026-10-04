package desktop

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

const (
	macSafariPFAnchor = "com.xiaosq2000.sbc.webrtc"
	macSafariPFRule   = "block drop out proto udp to any port {3478, 19302, 5349}\n"
)

var windowsBrowserKeys = []string{
	winsettings.ChromePolicyKey,
	winsettings.EdgePolicyKey,
	winsettings.BravePolicyKey,
}

var macBrowserDomains = []string{
	"com.google.Chrome",
	"com.microsoft.Edge",
	"com.brave.Browser",
}

// applyWindowsBrowserPolicies configures Chrome, Edge, Brave and Firefox policies under HKCU
// and updates Firefox user.js profiles.
func applyWindowsBrowserPolicies(reg winsettings.Registry) {
	for _, key := range windowsBrowserKeys {
		current, err := reg.Read(key, []string{winsettings.WebRtcPolicyName})
		if err == nil {
			val := current[winsettings.WebRtcPolicyName].Text
			if val == "" || val == winsettings.WebRtcDisableNonProxiedUDP {
				_ = reg.Apply(key, map[string]winsettings.Value{
					winsettings.WebRtcPolicyName: {Text: winsettings.WebRtcDisableNonProxiedUDP, Kind: "String"},
				}, "")
			}
		}
	}
	current, err := reg.Read(winsettings.FirefoxPolicyKey, winsettings.FirefoxWebRtcPrefs)
	if err == nil {
		toApply := make(map[string]winsettings.Value)
		for _, pref := range winsettings.FirefoxWebRtcPrefs {
			val := current[pref].Text
			if val == "" || val == "true" {
				toApply[pref] = winsettings.Value{Text: "true", Kind: "String"}
			}
		}
		if len(toApply) > 0 {
			_ = reg.Apply(winsettings.FirefoxPolicyKey, toApply, "")
		}
	}
	_ = applyFirefoxWebRTC("windows")
}

// revertWindowsBrowserPolicies removes WebRTC leak prevention policies set by sbc under HKCU
// and restores Firefox user.js profiles.
func revertWindowsBrowserPolicies(reg winsettings.Registry) {
	for _, key := range windowsBrowserKeys {
		current, err := reg.Read(key, []string{winsettings.WebRtcPolicyName})
		if err == nil && current[winsettings.WebRtcPolicyName].Text == winsettings.WebRtcDisableNonProxiedUDP {
			_ = reg.Apply(key, map[string]winsettings.Value{
				winsettings.WebRtcPolicyName: {},
			}, "")
		}
	}
	current, err := reg.Read(winsettings.FirefoxPolicyKey, winsettings.FirefoxWebRtcPrefs)
	if err == nil {
		toDelete := make(map[string]winsettings.Value)
		for _, pref := range winsettings.FirefoxWebRtcPrefs {
			if current[pref].Text == "true" {
				toDelete[pref] = winsettings.Value{}
			}
		}
		if len(toDelete) > 0 {
			_ = reg.Apply(winsettings.FirefoxPolicyKey, toDelete, "")
		}
	}
	_ = revertFirefoxWebRTC("windows")
}

// setMacBrowserPolicies configures WebRTC policy for Chrome, Edge and Brave on macOS.
func setMacBrowserPolicies(run Runner) {
	for _, domain := range macBrowserDomains {
		_, _ = run("defaults", "write", domain, "WebRtcIPHandling", "-string", "disable_non_proxied_udp")
	}
}

// revertMacBrowserPolicies removes WebRTC policy for Chrome, Edge and Brave on macOS if set to disable_non_proxied_udp.
func revertMacBrowserPolicies(run Runner) {
	for _, domain := range macBrowserDomains {
		if val, err := run("defaults", "read", domain, "WebRtcIPHandling"); err == nil && strings.TrimSpace(val) == "disable_non_proxied_udp" {
			_, _ = run("defaults", "delete", domain, "WebRtcIPHandling")
		}
	}
}

// setMacSafariSTUNFilter loads a pfctl anchor blocking outbound UDP on common STUN ports.
func setMacSafariSTUNFilter(run Runner) {
	ruleFile := filepath.Join(os.TempDir(), "sbc-webrtc.pf")
	if err := os.WriteFile(ruleFile, []byte(macSafariPFRule), 0644); err != nil {
		return
	}
	defer os.Remove(ruleFile)
	_, _ = run("sudo", "pfctl", "-a", macSafariPFAnchor, "-f", ruleFile)
	_, _ = run("sudo", "pfctl", "-e")
}

// revertMacSafariSTUNFilter flushes the pfctl anchor blocking outbound UDP on STUN ports.
func revertMacSafariSTUNFilter(run Runner) {
	_, _ = run("sudo", "pfctl", "-a", macSafariPFAnchor, "-F", "all")
}

const linuxWebRtcJSON = "{\"WebRtcIPHandling\": \"disable_non_proxied_udp\"}\n"

var linuxChromiumDirs = []string{
	"/etc/opt/chrome/policies/managed",
	"/etc/chromium/policies/managed",
	"/etc/brave/policies/managed",
	"/etc/opt/edge/policies/managed",
}

var isLinuxBrowserInstalled = func(dir string) bool {
	switch dir {
	case "/etc/opt/chrome/policies/managed":
		if _, err := exec.LookPath("google-chrome"); err == nil {
			return true
		}
		if _, err := exec.LookPath("google-chrome-stable"); err == nil {
			return true
		}
		if _, err := os.Stat("/opt/google/chrome"); err == nil {
			return true
		}
		if _, err := os.Stat("/etc/opt/chrome"); err == nil {
			return true
		}
	case "/etc/chromium/policies/managed":
		if _, err := exec.LookPath("chromium"); err == nil {
			return true
		}
		if _, err := exec.LookPath("chromium-browser"); err == nil {
			return true
		}
		if _, err := os.Stat("/etc/chromium"); err == nil {
			return true
		}
	case "/etc/brave/policies/managed":
		if _, err := exec.LookPath("brave-browser"); err == nil {
			return true
		}
		if _, err := os.Stat("/etc/brave"); err == nil {
			return true
		}
	case "/etc/opt/edge/policies/managed":
		if _, err := exec.LookPath("microsoft-edge"); err == nil {
			return true
		}
		if _, err := os.Stat("/opt/microsoft/msedge"); err == nil {
			return true
		}
	}
	return false
}

// setLinuxChromiumPolicies writes the WebRtcIPHandling policy for installed Chromium browsers.
func setLinuxChromiumPolicies(run Runner) {
	for _, dir := range linuxChromiumDirs {
		filePath := filepath.Join(dir, "webrtc.json")
		if data, err := os.ReadFile(filePath); err == nil && strings.Contains(string(data), winsettings.WebRtcPolicyName) {
			continue
		}
		if !isLinuxBrowserInstalled(dir) {
			continue
		}
		tmpFile := filepath.Join(os.TempDir(), "sbc-webrtc.json")
		if err := os.WriteFile(tmpFile, []byte(linuxWebRtcJSON), 0644); err != nil {
			continue
		}
		_, _ = run("sudo", "mkdir", "-p", dir)
		_, _ = run("sudo", "cp", tmpFile, filePath)
		_ = os.Remove(tmpFile)
	}
}

// revertLinuxChromiumPolicies removes sbc-managed WebRTC policy files for Chromium browsers.
func revertLinuxChromiumPolicies(run Runner) {
	for _, dir := range linuxChromiumDirs {
		filePath := filepath.Join(dir, "webrtc.json")
		if data, err := os.ReadFile(filePath); err == nil && strings.Contains(string(data), winsettings.WebRtcPolicyName) {
			_, _ = run("sudo", "rm", "-f", filePath)
		}
	}
}
