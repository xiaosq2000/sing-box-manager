package desktop

import (
	"errors"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
	"testing"
)

type windowsRegistry struct {
	values       map[string]winsettings.Value
	err          error
	notification string
}

func (r *windowsRegistry) Read(_ string, _ []string) (map[string]winsettings.Value, error) {
	return r.values, r.err
}
func (r *windowsRegistry) Apply(_ string, changes map[string]winsettings.Value, notification string) error {
	if r.err != nil {
		return r.err
	}
	r.notification = notification
	for name, value := range changes {
		r.values[name] = value
	}
	return nil
}
func TestWindowsDesktopStateAndSwitches(t *testing.T) {
	registry := &windowsRegistry{values: map[string]winsettings.Value{}}
	desk := &Windows{Registry: registry}
	if state, err := desk.State(1080); err != nil || state != Off {
		t.Fatalf("empty: %v %v", state, err)
	}
	if err := desk.On(1080); err != nil {
		t.Fatal(err)
	}
	if state, err := desk.State(1080); err != nil || state != On {
		t.Fatalf("on: %v %v", state, err)
	}
	if registry.notification != "desktop" || registry.values["ProxyOverride"].Text != "localhost;127.*;::1;host.docker.internal;<local>" {
		t.Fatal("missing refresh or wrong bypass list")
	}
	if registry.values[winsettings.WebRtcPolicyName].Text != winsettings.WebRtcDisableNonProxiedUDP {
		t.Fatal("missing WebRTC policy for Chromium")
	}
	if registry.values[winsettings.FirefoxProxyOnlyName].Text != "true" {
		t.Fatal("missing WebRTC policy for Firefox")
	}
	if state, _ := desk.State(2080); state != Other {
		t.Fatal("foreign port counted as ours")
	}
	if err := desk.Off(); err != nil {
		t.Fatal(err)
	}
	if state, _ := desk.State(1080); state != Off {
		t.Fatal("off failed")
	}
	if registry.values["ProxyServer"].Text != "127.0.0.1:1080" {
		t.Fatal("off erased the address")
	}
	if registry.values[winsettings.WebRtcPolicyName].Kind != "" {
		t.Fatal("off did not remove Chromium WebRTC policy")
	}
	if registry.values[winsettings.FirefoxProxyOnlyName].Kind != "" {
		t.Fatal("off did not remove Firefox WebRTC policy")
	}
	registry.values["AutoConfigURL"] = winsettings.Value{Text: "https://example.com/pac", Kind: "String"}
	if state, _ := desk.State(1080); state != Other {
		t.Fatal("PAC can be overwritten")
	}
	registry.err = errors.New("denied")
	if _, err := desk.State(1080); !errors.Is(err, registry.err) {
		t.Fatal("read error suppressed")
	}
	if err := desk.On(1080); !errors.Is(err, registry.err) {
		t.Fatal("write error suppressed")
	}
}

func TestWindowsDesktopPreservesForeignBrowserPolicies(t *testing.T) {
	registry := &windowsRegistry{values: map[string]winsettings.Value{
		winsettings.WebRtcPolicyName:     {Text: "default", Kind: "String"},
		winsettings.FirefoxProxyOnlyName: {Text: "false", Kind: "String"},
	}}
	desk := &Windows{Registry: registry}
	if err := desk.On(1080); err != nil {
		t.Fatal(err)
	}
	if registry.values[winsettings.WebRtcPolicyName].Text != "default" {
		t.Fatal("overwrote foreign Chromium WebRTC policy")
	}
	if registry.values[winsettings.FirefoxProxyOnlyName].Text != "false" {
		t.Fatal("overwrote foreign Firefox WebRTC policy")
	}
	if err := desk.Off(); err != nil {
		t.Fatal(err)
	}
	if registry.values[winsettings.WebRtcPolicyName].Text != "default" {
		t.Fatal("erased foreign Chromium WebRTC policy")
	}
	if registry.values[winsettings.FirefoxProxyOnlyName].Text != "false" {
		t.Fatal("erased foreign Firefox WebRTC policy")
	}
}
