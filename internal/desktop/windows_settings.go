package desktop

import (
	"fmt"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

// Windows manages the per-user WinINet proxy. A PAC belongs to another tool,
// even when the manual proxy switch is off.
type Windows struct{ Registry winsettings.Registry }

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
	applyWindowsBrowserPolicies(w.Registry)
	return w.Registry.Apply(winsettings.InternetKey, map[string]winsettings.Value{
		"ProxyServer":   {Text: fmt.Sprintf("%s:%d", Host, port), Kind: "String"},
		"ProxyOverride": {Text: "localhost;127.*;::1;host.docker.internal;<local>", Kind: "String"},
		"ProxyEnable":   {Text: "1", Kind: "DWord"},
	}, "desktop")
}

func (w *Windows) Off() error {
	revertWindowsBrowserPolicies(w.Registry)
	return w.Registry.Apply(winsettings.InternetKey, map[string]winsettings.Value{
		"ProxyEnable": {Text: "0", Kind: "DWord"},
	}, "desktop")
}
