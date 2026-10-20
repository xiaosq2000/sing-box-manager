package desktop

import (
	"errors"
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

func TestWindowsDesktopStateAndSwitches(t *testing.T) {
	t.Setenv("APPDATA", t.TempDir())
	registry := &windowsRegistry{values: map[string]map[string]winsettings.Value{}}
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
	if state, _ := desk.State(1080); state != Off {
		t.Fatal("off must disable the proxy")
	}
	if internet["ProxyServer"].Text != "127.0.0.1:1080" {
		t.Fatal("off erased the address")
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
