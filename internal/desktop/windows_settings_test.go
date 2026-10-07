package desktop

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

type windowsRegistry struct {
	values       map[string]map[string]winsettings.Value
	err          error
	notification string
}

func (r *windowsRegistry) Read(key string, names []string) (map[string]winsettings.Value, error) {
	if r.err != nil {
		return nil, r.err
	}
	values := map[string]winsettings.Value{}
	for _, name := range names {
		if value, ok := r.values[key][name]; ok {
			values[name] = value
		}
	}
	return values, nil
}

func (r *windowsRegistry) Apply(key string, changes map[string]winsettings.Value, notification string) error {
	if r.err != nil {
		return r.err
	}
	if r.values[key] == nil {
		r.values[key] = map[string]winsettings.Value{}
	}
	for name, value := range changes {
		if value.Kind == "" {
			delete(r.values[key], name)
		} else {
			r.values[key][name] = value
		}
	}
	r.notification = notification
	return nil
}

type fakeWebRTC struct {
	enabled, optedOut bool
	ensures, cleanups int
	err               error
}

func (p *fakeWebRTC) Ensure() error {
	p.ensures++
	if p.err != nil {
		return p.err
	}
	if !p.optedOut {
		p.enabled = true
	}
	return nil
}
func (p *fakeWebRTC) Set(enabled bool) error {
	if p.err != nil {
		return p.err
	}
	p.enabled, p.optedOut = enabled, !enabled
	return nil
}
func (p *fakeWebRTC) Cleanup() error {
	p.cleanups++
	if p.err != nil {
		return p.err
	}
	p.enabled = false
	return nil
}
func (p *fakeWebRTC) Remaining() ([]string, error) { return nil, p.err }

func TestWindowsDesktopStateAndSwitches(t *testing.T) {
	t.Setenv("APPDATA", t.TempDir())
	registry := &windowsRegistry{values: map[string]map[string]winsettings.Value{}}
	privacy := &fakeWebRTC{}
	desk := &Windows{Registry: registry, Privacy: privacy}
	if state, err := desk.State(1080); err != nil || state != Off {
		t.Fatalf("empty: %v %v", state, err)
	}
	if err := desk.On(1080); err != nil {
		t.Fatal(err)
	}
	if state, err := desk.State(1080); err != nil || state != On || !privacy.enabled {
		t.Fatalf("on: %v %v, privacy=%v", state, err, privacy.enabled)
	}
	internet := registry.values[winsettings.InternetKey]
	if registry.notification != "desktop" || internet["ProxyOverride"].Text != "localhost;127.*;::1;host.docker.internal;<local>" {
		t.Fatal("missing refresh or wrong bypass list")
	}
	if state, _ := desk.State(2080); state != Other {
		t.Fatal("foreign port counted as ours")
	}
	if err := desk.Off(); err != nil {
		t.Fatal(err)
	}
	if state, _ := desk.State(1080); state != Off || !privacy.enabled || privacy.cleanups != 0 {
		t.Fatal("off must disable only the proxy and retain browser protection")
	}
	if internet["ProxyServer"].Text != "127.0.0.1:1080" {
		t.Fatal("off erased the address")
	}
	if err := desk.SetWebRTC(false); err != nil {
		t.Fatal(err)
	}
	if err := desk.On(1080); err != nil || privacy.enabled {
		t.Fatalf("on must honor the explicit privacy opt-out: %v", err)
	}
	if err := desk.SetWebRTC(true); err != nil || !privacy.enabled {
		t.Fatalf("explicit enable: %v", err)
	}
	if err := desk.CleanupWebRTC(); err != nil || privacy.enabled || privacy.cleanups != 1 {
		t.Fatalf("cleanup: %v", err)
	}
	internet["AutoConfigURL"] = winsettings.Value{Text: "https://example.com/pac", Kind: "String"}
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

func TestWindowsDesktopDoesNotEnableProxyAfterPolicyFailure(t *testing.T) {
	t.Setenv("APPDATA", t.TempDir())
	registry := &windowsRegistry{values: map[string]map[string]winsettings.Value{}}
	privacy := &fakeWebRTC{err: errors.New("UAC canceled")}
	desk := &Windows{Registry: registry, Privacy: privacy}
	if err := desk.On(1080); !errors.Is(err, privacy.err) {
		t.Fatalf("policy failure reported success: %v", err)
	}
	if len(registry.values) != 0 {
		t.Fatal("policy failure enabled WinINet")
	}
	if err := desk.CleanupWebRTC(); !errors.Is(err, privacy.err) {
		t.Fatalf("cleanup failure reported success: %v", err)
	}
	if _, err := desk.RemainingWebRTC(); !errors.Is(err, privacy.err) {
		t.Fatalf("policy read failure reported success: %v", err)
	}
}

func TestWindowsRemainingWebRTCReportsOnlyActualFirefoxPreferences(t *testing.T) {
	appData := t.TempDir()
	t.Setenv("APPDATA", appData)
	profile := filepath.Join(appData, "Mozilla", "Firefox", "Profiles", "fixture")
	if err := os.MkdirAll(profile, 0700); err != nil {
		t.Fatal(err)
	}
	desk := &Windows{Privacy: &fakeWebRTC{}}
	remaining, err := desk.RemainingWebRTC()
	if err != nil || len(remaining) != 0 {
		t.Fatalf("clean profile needs no manual work: %v, %v", remaining, err)
	}
	path := filepath.Join(profile, "prefs.js")
	if err := os.WriteFile(path, []byte(firefoxPrefs[0]+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	remaining, err = desk.RemainingWebRTC()
	if err != nil || len(remaining) != 1 || remaining[0] != path {
		t.Fatalf("unowned saved preference was hidden: %v, %v", remaining, err)
	}
}

func TestWindowsDesktopRequiresPolicyManager(t *testing.T) {
	desk := &Windows{Registry: &windowsRegistry{values: map[string]map[string]winsettings.Value{}}}
	if err := desk.On(1080); err == nil {
		t.Fatal("unconfigured policy manager reported success")
	}
}
