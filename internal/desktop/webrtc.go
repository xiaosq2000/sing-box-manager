package desktop

import (
	"errors"
	"os"
	"strconv"

	"github.com/xiaosq2000/sing-box-manager/internal/browserprivacy"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

// BrowserPrivacy manages Chrome WebRTC leak protection independently of proxy
// toggles. CleanupWebRTC must finish before uninstall removes the client.
type BrowserPrivacy interface {
	SetWebRTC(enabled bool) error
	CleanupWebRTC() error
	RemainingWebRTC() ([]string, error)
	WebRTCOptedOut() (bool, error)
}

// WebRTCSettings is implemented by the Chrome policy manager and fixtures.
type WebRTCSettings interface {
	Ensure() error
	Set(enabled bool) error
	Cleanup() error
	Remaining() ([]string, error)
	OptedOut() (bool, error)
}

// NewBrowserPrivacy returns the Chrome policy manager on Linux.
func NewBrowserPrivacy(goos string, run Runner) (BrowserPrivacy, error) {
	settings, err := newChromePrivacy(goos, run)
	if err != nil {
		return nil, err
	}
	return &browserPrivacy{settings}, nil
}

func newChromePrivacy(goos string, run Runner) (*chromePrivacy, error) {
	if goos != "linux" {
		return nil, i18n.New("Chrome WebRTC protection is available on Linux only")
	}
	layout, err := paths.Default()
	if err != nil {
		return nil, err
	}
	p := &chromePrivacy{run: run, uid: strconv.Itoa(os.Getuid()), policy: googleChromePolicy}
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
}

func (p *browserPrivacy) SetWebRTC(enabled bool) error { return p.Set(enabled) }
func (p *browserPrivacy) CleanupWebRTC() error         { return p.Cleanup() }
func (p *browserPrivacy) RemainingWebRTC() ([]string, error) {
	return p.Remaining()
}
func (p *browserPrivacy) WebRTCOptedOut() (bool, error) { return p.OptedOut() }

type chromePrivacy struct {
	browserprivacy.Manager
	uid    string
	run    Runner
	policy unixPolicy
}

func (p *chromePrivacy) enable() error {
	return p.setLinuxPolicy(p.policy, true)
}

func (p *chromePrivacy) remove() error {
	return errors.Join(p.removeLegacyChromePolicy(), p.setLinuxPolicy(p.policy, false))
}

func (p *chromePrivacy) Remaining() ([]string, error) {
	return p.remainingChromePolicies()
}

type unixPolicy struct {
	location string
	name     string
	programs []string
}

// googleChromePolicy is the only browser policy sbc manages.
var googleChromePolicy = unixPolicy{
	location: "/etc/opt/chrome/policies/managed",
	name:     "WebRtcIPHandling",
	programs: []string{
		"google-chrome",
		"google-chrome-stable",
		"google-chrome-beta",
		"google-chrome-unstable",
	},
}
