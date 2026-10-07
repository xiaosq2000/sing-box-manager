package desktop

import (
	"fmt"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

// BrowserPrivacy manages browser settings independently of proxy toggles.
// CleanupWebRTC must finish before uninstall removes the client and its state.
type BrowserPrivacy interface {
	SetWebRTC(enabled bool) error
	CleanupWebRTC() error
	RemainingWebRTC() ([]string, error)
}

// WebRTCSettings is implemented by the policy manager and fixture substitutes.
type WebRTCSettings interface {
	Ensure() error
	Set(enabled bool) error
	Cleanup() error
	Remaining() ([]string, error)
}

// Windows manages the per-user WinINet proxy. A PAC belongs to another tool,
// even when the manual proxy switch is off.
type Windows struct {
	Registry winsettings.Registry
	Privacy  WebRTCSettings
}

func (w *Windows) Name() string { return "Windows" }

func (w *Windows) State(port int) (State, error) {
	values, err := w.Registry.Read(winsettings.InternetKey, []string{"ProxyEnable", "ProxyServer", "AutoConfigURL"})
	if err != nil {
		return Off, err
	}
	if strings.TrimSpace(values["AutoConfigURL"].Text) != "" {
		return Other, nil
	}
	if values["ProxyEnable"].Text != "1" {
		return Off, nil
	}
	if values["ProxyServer"].Text == fmt.Sprintf("%s:%d", Host, port) {
		return On, nil
	}
	return Other, nil
}

func (w *Windows) On(port int) error {
	if w.Privacy == nil {
		return i18n.New("Windows WebRTC settings are unavailable")
	}
	if err := w.Privacy.Ensure(); err != nil {
		return err
	}
	return w.Registry.Apply(winsettings.InternetKey, map[string]winsettings.Value{
		"ProxyServer":   {Text: fmt.Sprintf("%s:%d", Host, port), Kind: "String"},
		"ProxyOverride": {Text: "localhost;127.*;::1;host.docker.internal;<local>", Kind: "String"},
		"ProxyEnable":   {Text: "1", Kind: "DWord"},
	}, "desktop")
}

func (w *Windows) Off() error {
	// Browser protection persists. Only an explicit reset or uninstall removes it.
	return w.Registry.Apply(winsettings.InternetKey, map[string]winsettings.Value{
		"ProxyEnable": {Text: "0", Kind: "DWord"},
	}, "desktop")
}

func (w *Windows) SetWebRTC(enabled bool) error {
	if w.Privacy == nil {
		return i18n.New("Windows WebRTC settings are unavailable")
	}
	return w.Privacy.Set(enabled)
}

func (w *Windows) CleanupWebRTC() error {
	if w.Privacy == nil {
		return i18n.New("Windows WebRTC settings are unavailable")
	}
	return w.Privacy.Cleanup()
}

func (w *Windows) RemainingWebRTC() ([]string, error) {
	if w.Privacy == nil {
		return nil, i18n.New("Windows WebRTC settings are unavailable")
	}
	remaining, err := w.Privacy.Remaining()
	if err != nil {
		return nil, err
	}
	return remainingFirefox("windows", remaining)
}
