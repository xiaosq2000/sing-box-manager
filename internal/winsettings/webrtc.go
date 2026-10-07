package winsettings

import (
	"sync"

	"github.com/xiaosq2000/sing-box-manager/internal/browserprivacy"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Ownership is kept beside the policies, not in the removable sbc directory.
// Each DWord=1 is an intent to create the corresponding absent policy. The
// restricted helper alone writes this key, with an administrator-owned ACL.
const (
	WebRTCMetadataKey         = `Software\Policies\sbc\WebRTC`
	WebRTCChromeOwnerName     = "Chrome"
	WebRTCEdgeOwnerName       = "EdgeLocalhostIP"
	WebRTCEdgeLegacyOwnerName = "Edge"
	WebRTCBraveOwnerName      = "Brave"
	WebRTCFirefoxNoHostName   = "FirefoxNoHost"
	WebRTCFirefoxAddressName  = "FirefoxDefaultAddress"
	WebRTCFirefoxProxyName    = "FirefoxProxyOnly"
	WebRTCFirefoxBehindName   = "FirefoxBehindProxy"
	WebRTCMetadataIntent      = "1"
	WebRTCMetadataKind        = "DWord"
)

type webRTCPolicy struct {
	key, name, owner string
	want             Value
	chromium         bool
	obsolete         bool
}

// Keep the elevated allowlist fixed. Firefox's existing values are preserved,
// even when they differ from these defaults. Chromium conflicts are errors.
var webRTCPolicies = []webRTCPolicy{
	{
		key: ChromePolicyKey, name: WebRtcPolicyName, owner: WebRTCChromeOwnerName,
		want: Value{WebRtcDisableNonProxiedUDP, "String"}, chromium: true,
	},
	{
		key: EdgePolicyKey, name: EdgeWebRtcPolicyName, owner: WebRTCEdgeOwnerName,
		want: Value{WebRtcDisableNonProxiedUDP, "String"}, chromium: true,
	},
	// Edge ignores the Chrome policy name. Retain its old ownership identity
	// only for cleanup, so migration never claims an existing supported policy.
	{
		key: EdgePolicyKey, name: WebRtcPolicyName, owner: WebRTCEdgeLegacyOwnerName,
		want: Value{WebRtcDisableNonProxiedUDP, "String"}, obsolete: true,
	},
	{
		key: BravePolicyKey, name: WebRtcPolicyName, owner: WebRTCBraveOwnerName,
		want: Value{WebRtcDisableNonProxiedUDP, "String"}, chromium: true,
	},
	{
		key: FirefoxPolicyKey, name: "media.peerconnection.ice.no_host", owner: WebRTCFirefoxNoHostName,
		want: Value{"true", "String"},
	},
	{
		key: FirefoxPolicyKey, name: "media.peerconnection.ice.default_address_only", owner: WebRTCFirefoxAddressName,
		want: Value{"true", "String"},
	},
	{
		key: FirefoxPolicyKey, name: FirefoxProxyOnlyName, owner: WebRTCFirefoxProxyName,
		want: Value{"true", "String"},
	},
	{
		key: FirefoxPolicyKey, name: "media.peerconnection.ice.proxy_only_if_behind_proxy", owner: WebRTCFirefoxBehindName,
		want: Value{"true", "String"},
	},
}

// WebRTC manages persistent browser protection independently of proxy toggles.
// Change accepts only "on" or "off"; nil uses the restricted native UAC helper.
// Profile callbacks run in the original, unelevated process. StateFile stores
// only "off\n"; an absent file means enabled. An empty path supports Ensure and
// Cleanup, but Set(false) requires a path to persist the explicit opt-out.
// Only Set(true) removes this file; uninstall owns final directory removal.
type WebRTC struct {
	Registry       Registry
	StateFile      string
	Change         func(action string) error
	ApplyProfiles  func() error
	RevertProfiles func() error
}

// All managers in this process share the lock. Windows also locks across
// processes for the original user, independently of the elevated helper lock.
var webRTCOperationMu sync.Mutex

func lockWebRTC() (func(), error) {
	webRTCOperationMu.Lock()
	unlock, err := lockWebRTCProcess()
	if err != nil {
		webRTCOperationMu.Unlock()
		return nil, i18n.Errorf("could not lock WebRTC settings: %w", err)
	}
	return func() {
		unlock()
		webRTCOperationMu.Unlock()
	}, nil
}

func (w *WebRTC) registry() Registry {
	if w.Registry != nil {
		return w.Registry
	}
	return PowerShell{}
}

func (w *WebRTC) change(action string) error {
	if w.Change != nil {
		return w.Change(action)
	}
	return changeWebRTCPolicies(action)
}

func (w *WebRTC) policyValues() (map[string]Value, error) {
	result := map[string]Value{}
	for _, policy := range webRTCPolicies {
		if _, read := result[policy.owner]; read {
			continue
		}
		var names []string
		for _, entry := range webRTCPolicies {
			if entry.key == policy.key {
				names = append(names, entry.name)
			}
		}
		values, err := w.registry().Read(policy.key, names)
		if err != nil {
			return nil, i18n.Errorf("could not read WebRTC policy at HKCU\\%s: %w", policy.key, err)
		}
		for _, entry := range webRTCPolicies {
			if entry.key == policy.key {
				result[entry.owner] = values[entry.name]
			}
		}
	}
	return result, nil
}

func (w *WebRTC) needsSetup() (bool, error) {
	values, err := w.policyValues()
	if err != nil {
		return false, err
	}
	missing := false
	for _, policy := range webRTCPolicies {
		if policy.obsolete {
			continue
		}
		value := values[policy.owner]
		if policy.chromium && value.Kind != "" && value != policy.want {
			return false, i18n.Errorf("an existing WebRTC policy at HKCU\\%s conflicts with proxy-only UDP; review your browser policies", policy.key)
		}
		missing = missing || value.Kind == ""
	}
	owned, err := w.ownership()
	if err != nil {
		return false, err
	}
	return missing || owned[WebRTCEdgeLegacyOwnerName].Kind != "", nil
}

func (w *WebRTC) enable() error {
	needsSetup, err := w.needsSetup()
	if err != nil {
		return err
	}
	if needsSetup {
		if err := w.change("on"); err != nil {
			return err
		}
		needsSetup, err = w.needsSetup()
		if err != nil {
			return err
		}
		if needsSetup {
			return i18n.New("Windows did not retain all WebRTC policies; retry 'sbc webrtc on'")
		}
	}
	if w.ApplyProfiles != nil {
		return w.ApplyProfiles()
	}
	return nil
}

func (w *WebRTC) ownership() (map[string]Value, error) {
	names := make([]string, 0, len(webRTCPolicies))
	for _, policy := range webRTCPolicies {
		names = append(names, policy.owner)
	}
	values, err := w.registry().Read(WebRTCMetadataKey, names)
	if err != nil {
		return nil, i18n.Errorf("could not read WebRTC policy ownership: %w", err)
	}
	for _, name := range names {
		value := values[name]
		if value.Kind != "" && value != (Value{WebRTCMetadataIntent, WebRTCMetadataKind}) {
			return nil, i18n.New("the WebRTC policy ownership record is invalid; cleanup cannot safely continue")
		}
	}
	return values, nil
}

func (w *WebRTC) cleanupSettings() error {
	owned, err := w.ownership()
	if err != nil {
		return err
	}
	needsChange := false
	for _, policy := range webRTCPolicies {
		if owned[policy.owner].Kind != "" {
			needsChange = true
			break
		}
	}
	if needsChange {
		if err := w.change("off"); err != nil {
			return err
		}
		remaining, err := w.ownership()
		if err != nil {
			return err
		}
		values, err := w.policyValues()
		if err != nil {
			return err
		}
		for _, policy := range webRTCPolicies {
			if remaining[policy.owner].Kind != "" || (owned[policy.owner].Kind != "" && values[policy.owner] == policy.want) {
				return i18n.New("Windows did not remove all owned WebRTC policies; retry cleanup")
			}
		}
	}
	if w.RevertProfiles != nil {
		return w.RevertProfiles()
	}
	return nil
}

func (w *WebRTC) Remaining() ([]string, error) {
	values, err := w.policyValues()
	if err != nil {
		return nil, err
	}
	var locations []string
	for _, policy := range webRTCPolicies {
		if values[policy.owner].Kind != "" {
			locations = append(locations, `HKCU\`+policy.key+`\`+policy.name)
		}
	}
	return locations, nil
}

func (w *WebRTC) manager() *browserprivacy.Manager {
	return &browserprivacy.Manager{StateFile: w.StateFile, Lock: lockWebRTC, Enable: w.enable, Remove: w.cleanupSettings}
}
func (w *WebRTC) Ensure() error          { return w.manager().Ensure() }
func (w *WebRTC) Set(enabled bool) error { return w.manager().Set(enabled) }
func (w *WebRTC) Cleanup() error         { return w.manager().Cleanup() }
