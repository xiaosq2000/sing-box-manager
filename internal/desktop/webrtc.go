package desktop

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"

	"github.com/xiaosq2000/sing-box-manager/internal/browserprivacy"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

// NewBrowserPrivacy does not require a desktop session or a default route.
func NewBrowserPrivacy(goos string, run Runner) (BrowserPrivacy, error) {
	settings, err := newPrivacy(goos, run)
	if err != nil {
		return nil, err
	}
	return &browserPrivacy{settings, goos}, nil
}

func newPrivacy(goos string, run Runner) (WebRTCSettings, error) {
	layout, err := paths.Default()
	if err != nil {
		return nil, err
	}
	if goos == "windows" {
		return &winsettings.WebRTC{
			Registry:       winsettings.PowerShell{},
			StateFile:      layout.WebRTCFile(),
			ApplyProfiles:  func() error { return applyFirefoxWebRTC(goos) },
			RevertProfiles: func() error { return revertFirefoxWebRTC(goos) },
		}, nil
	}
	if goos != "linux" && goos != "darwin" {
		return nil, i18n.New("WebRTC settings are unavailable on this platform")
	}
	p := &unixPrivacy{goos: goos, run: run, uid: strconv.Itoa(os.Getuid()), policies: unixBrowserPolicies(goos)}
	p.Manager = browserprivacy.Manager{
		StateFile: layout.WebRTCFile(),
		Lock:      func() (func(), error) { return lockUnixPrivacy(layout.WebRTCFile() + ".lock") },
		Enable:    p.enable,
		Remove:    p.remove,
	}
	return p, nil
}

type browserPrivacy struct {
	WebRTCSettings
	goos string
}

func (p *browserPrivacy) SetWebRTC(enabled bool) error { return p.Set(enabled) }
func (p *browserPrivacy) CleanupWebRTC() error         { return p.Cleanup() }
func (p *browserPrivacy) RemainingWebRTC() ([]string, error) {
	remaining, err := p.Remaining()
	if err != nil {
		return nil, err
	}
	return remainingFirefox(p.goos, remaining)
}

type unixPolicy struct {
	location, name string
	programs       []string
}

func unixBrowserPolicies(goos string) []unixPolicy {
	if goos == "darwin" {
		return []unixPolicy{
			{location: "/Library/Managed Preferences/com.google.Chrome", name: winsettings.WebRtcPolicyName},
			{location: "/Library/Managed Preferences/org.chromium.Chromium", name: winsettings.WebRtcPolicyName},
			{location: "/Library/Managed Preferences/com.microsoft.Edge", name: winsettings.EdgeWebRtcPolicyName},
			{location: "/Library/Managed Preferences/com.brave.Browser", name: winsettings.WebRtcPolicyName},
		}
	}
	return []unixPolicy{
		{"/etc/opt/chrome/policies/managed", winsettings.WebRtcPolicyName, []string{"google-chrome", "google-chrome-stable"}},
		{"/etc/chromium/policies/managed", winsettings.WebRtcPolicyName, []string{"chromium", "chromium-browser"}},
		{"/etc/chromium-browser/policies/managed", winsettings.WebRtcPolicyName, []string{"chromium-browser"}},
		{"/etc/opt/edge/policies/managed", winsettings.EdgeWebRtcPolicyName, []string{"microsoft-edge", "microsoft-edge-stable"}},
		{"/etc/brave/policies/managed", winsettings.WebRtcPolicyName, []string{"brave-browser", "brave-browser-stable"}},
	}
}

type unixPrivacy struct {
	browserprivacy.Manager
	goos, uid string
	run       Runner
	policies  []unixPolicy
}

func (p *unixPrivacy) enable() error {
	return errors.Join(p.setPolicies(true), p.reloadMacPreferences(), applyFirefoxWebRTC(p.goos))
}

func (p *unixPrivacy) remove() error {
	var legacyErr error
	if p.goos == "linux" {
		legacyErr = p.removeLegacyLinuxPolicies()
	}
	return errors.Join(legacyErr, p.setPolicies(false), p.reloadMacPreferences(), revertFirefoxWebRTC(p.goos))
}

func (p *unixPrivacy) setPolicies(enabled bool) error {
	var result error
	for _, policy := range p.policies {
		var err error
		if p.goos == "darwin" {
			err = p.setMacPolicy(policy, enabled)
		} else {
			err = p.setLinuxPolicy(policy, enabled)
		}
		result = errors.Join(result, err)
	}
	return result
}

func (p *unixPrivacy) Remaining() ([]string, error) {
	if p.goos == "darwin" {
		return p.remainingMacPolicies()
	}
	return p.remainingLinuxPolicies()
}

// regularFileInfo checks the path itself, without following symbolic links.
func regularFileInfo(path string) (os.FileInfo, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, i18n.Errorf("WebRTC settings path is not a regular file: %s", path)
	}
	return info, nil
}

func privateFile(path string) ([]byte, error) {
	if _, err := regularFileInfo(path); err != nil {
		return nil, err
	}
	return os.ReadFile(filepath.Clean(path))
}
