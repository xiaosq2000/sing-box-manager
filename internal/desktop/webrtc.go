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
		return &winsettings.WebRTC{StateFile: layout.WebRTCFile(),
			ApplyProfiles:  func() error { return applyFirefoxWebRTC(goos) },
			RevertProfiles: func() error { return revertFirefoxWebRTC(goos) }}, nil
	}
	if goos != "linux" && goos != "darwin" {
		return nil, i18n.New("WebRTC settings are unavailable on this platform")
	}
	p := &unixPrivacy{goos: goos, run: run, uid: strconv.Itoa(os.Getuid()), policies: unixBrowserPolicies(goos)}
	p.Manager = browserprivacy.Manager{StateFile: layout.WebRTCFile(),
		Lock:   func() (func(), error) { return lockUnixPrivacy(layout.WebRTCFile() + ".lock") },
		Enable: p.enable, Remove: p.remove}
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

func remainingFirefox(goos string, remaining []string) ([]string, error) {
	for _, dir := range firefoxDataDirs(goos) {
		profiles, err := findFirefoxProfiles(dir)
		if err != nil {
			return nil, err
		}
		if len(profiles) > 0 {
			remaining = append(remaining, i18n.T("Firefox profiles: check saved WebRTC preferences in about:config; see the WebRTC guide"))
			break
		}
	}
	return remaining, nil
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
	var result error
	for _, policy := range p.policies {
		var err error
		if p.goos == "darwin" {
			err = p.setMacPolicy(policy, true)
		} else {
			err = p.setLinuxPolicy(policy, true)
		}
		result = errors.Join(result, err)
	}
	return errors.Join(result, p.reloadMacPreferences(), applyFirefoxWebRTC(p.goos))
}
func (p *unixPrivacy) remove() error {
	var result error
	for _, policy := range p.policies {
		var err error
		if p.goos == "darwin" {
			err = p.setMacPolicy(policy, false)
		} else {
			err = p.setLinuxPolicy(policy, false)
		}
		result = errors.Join(result, err)
	}
	return errors.Join(result, p.reloadMacPreferences(), revertFirefoxWebRTC(p.goos))
}
func (p *unixPrivacy) Remaining() ([]string, error) {
	var locations []string
	for _, policy := range p.policies {
		if p.goos == "darwin" {
			names := []string{policy.name}
			if policy.name == winsettings.EdgeWebRtcPolicyName {
				names = append(names, winsettings.WebRtcPolicyName)
			}
			for _, location := range []string{policy.location, filepath.Base(policy.location)} {
				for _, name := range names {
					_, present, err := readMacDefault(p.run, location, name)
					if err != nil {
						return nil, err
					}
					if present {
						locations = append(locations, location+" / "+name)
					}
				}
			}
		} else {
			files, err := linuxPolicyFiles(policy.location)
			if err != nil {
				return nil, err
			}
			for _, file := range files {
				values, err := readLinuxPolicy(file)
				if err != nil {
					return nil, err
				}
				if _, ok := values[policy.name]; ok {
					locations = append(locations, file)
				} else if _, ok := values[winsettings.WebRtcPolicyName]; ok {
					locations = append(locations, file)
				}
			}
		}
	}
	return locations, nil
}

func privateFile(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, i18n.Errorf("WebRTC settings path is not a regular file: %s", path)
	}
	return os.ReadFile(filepath.Clean(path))
}
