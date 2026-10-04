// Package winsettings manages sbc's per-user Windows settings. Registry data
// travels as JSON on standard input, never in process arguments or error text.
package winsettings

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/powershell"
)

const (
	InternetKey      = `Software\Microsoft\Windows\CurrentVersion\Internet Settings`
	EnvironmentKey   = `Environment`
	ChromePolicyKey  = `Software\Policies\Google\Chrome`
	EdgePolicyKey    = `Software\Policies\Microsoft\Edge`
	BravePolicyKey   = `Software\Policies\BraveSoftware\Brave`
	FirefoxPolicyKey = `Software\Policies\Mozilla\Firefox\Preferences`
)

const (
	WebRtcPolicyName           = "WebRtcIPHandling"
	WebRtcDisableNonProxiedUDP = "disable_non_proxied_udp"
	FirefoxProxyOnlyName       = "media.peerconnection.ice.proxy_only"
)

var FirefoxWebRtcPrefs = []string{
	"media.peerconnection.ice.no_host",
	"media.peerconnection.ice.default_address_only",
	"media.peerconnection.ice.proxy_only",
	"media.peerconnection.ice.proxy_only_if_behind_proxy",
}

// Value preserves the registry type and unexpanded text. An empty Kind deletes
// a value; it is distinct from a present, empty string.
type Value struct {
	Text string
	Kind string
}

type Registry interface {
	Read(key string, names []string) (map[string]Value, error)
	Apply(key string, changes map[string]Value, notification string) error
}

// PowerShell uses Windows PowerShell 5.1 and the .NET registry API. Run is
// replaceable in tests; nil runs the embedded script against HKCU.
type PowerShell struct {
	Run func(input []byte) ([]byte, error)
}

//go:embed registry.ps1
var registryScript string

func (p PowerShell) call(request any) ([]byte, error) {
	input, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	if p.Run != nil {
		return p.Run(input)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	command := powershell.Command(ctx, "-NoProfile", "-NonInteractive", "-Command", registryScript)
	command.Stdin = bytes.NewReader(input)
	output, err := command.Output()
	if err != nil {
		// PowerShell errors can quote values. Neither stderr nor input may reach
		// logs: proxy URLs can contain a password.
		return nil, i18n.New("Windows could not read or change the user settings; check registry permissions and PowerShell access")
	}
	return output, nil
}

func (p PowerShell) Read(key string, names []string) (map[string]Value, error) {
	output, err := p.call(struct {
		Key   string
		Names []string
	}{key, names})
	if err != nil {
		return nil, err
	}
	values := map[string]Value{}
	if err := json.Unmarshal(bytes.TrimPrefix(output, []byte{0xef, 0xbb, 0xbf}), &values); err != nil {
		return nil, i18n.New("Windows returned unreadable user settings")
	}
	return values, nil
}

func (p PowerShell) Apply(key string, changes map[string]Value, notification string) error {
	if len(changes) == 0 {
		return nil
	}
	_, err := p.call(struct {
		Key          string
		Changes      map[string]Value
		Notification string
	}{key, changes, notification})
	return err
}
