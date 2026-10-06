package desktop

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

// fakeGSettings keeps keys the way gsettings prints them.
type fakeGSettings struct {
	values map[string]string
	writes []string
}

func newGSettings(values map[string]string) *fakeGSettings {
	return &fakeGSettings{values: values}
}

func (f *fakeGSettings) run(name string, args ...string) (string, error) {
	if name == "sudo" {
		f.writes = append(f.writes, name+" "+strings.Join(args, " "))
		return "", nil
	}
	if name != "gsettings" || len(args) < 3 {
		return "", errors.New("unexpected command " + name)
	}
	key := args[1] + " " + args[2]
	switch args[0] {
	case "get":
		value, ok := f.values[key]
		if !ok {
			value = "''"
		}
		return value + "\n", nil
	case "set":
		f.values[key] = args[3]
		f.writes = append(f.writes, key)
		return "", nil
	}
	return "", errors.New("unexpected gsettings " + args[0])
}

// The settings the bash client's `proxy desktop on` left on a machine.
func bashClientSettings(port string) map[string]string {
	values := map[string]string{"org.gnome.system.proxy mode": "'manual'"}
	for _, scheme := range []string{"http", "https", "ftp", "socks"} {
		values["org.gnome.system.proxy."+scheme+" host"] = "'127.0.0.1'"
		values["org.gnome.system.proxy."+scheme+" port"] = port
	}
	return values
}

func TestNewPicksSupportedDesktops(t *testing.T) {
	env := func(values map[string]string) func(string) string {
		return func(name string) string { return values[name] }
	}
	if desktop, err := New("linux", env(map[string]string{"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}), nil); err != nil || desktop.Name() != "GNOME" {
		t.Errorf("GNOME: %v, %v", desktop, err)
	}
	for _, values := range []map[string]string{{"XDG_CURRENT_DESKTOP": "KDE"}, {}} {
		if _, err := New("linux", env(values), nil); !errors.Is(err, ErrUnsupported) {
			t.Errorf("%v: got %v", values, err)
		}
	}
	if desk, err := New("windows", env(nil), nil); err != nil || desk.Name() != "Windows" {
		t.Errorf("windows: got %v", err)
	}
}

func TestGNOMEStateTellsOursFromSomeoneElses(t *testing.T) {
	cases := []struct {
		values map[string]string
		want   State
	}{
		{map[string]string{}, Off},
		{map[string]string{"org.gnome.system.proxy mode": "'none'"}, Off},
		{bashClientSettings("1080"), On},
		{bashClientSettings("3128"), Other},
	}
	for _, c := range cases {
		gnome := &GNOME{Run: newGSettings(c.values).run}
		if got, err := gnome.State(1080); err != nil || got != c.want {
			t.Errorf("%v: got %v, %v; want %v", c.values, got, err, c.want)
		}
	}
}

func TestGNOMEOnSwitchesTheModeLastAndOffKeepsTheHosts(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	settings := newGSettings(map[string]string{})
	gnome := &GNOME{Run: settings.run}

	if err := gnome.On(2080); err != nil {
		t.Fatal(err)
	}
	if state, _ := gnome.State(2080); state != On {
		t.Errorf("after on: %v", state)
	}
	if last := settings.writes[len(settings.writes)-1]; last != "org.gnome.system.proxy mode" {
		t.Errorf("the last write was %q", last)
	}
	if settings.values["org.gnome.system.proxy.ftp port"] != "2080" ||
		settings.values["org.gnome.system.proxy ignore-hosts"] != "['localhost', '127.0.0.0/8', '::1', 'host.docker.internal']" {
		t.Errorf("values %v", settings.values)
	}

	if err := gnome.Off(); err != nil {
		t.Fatal(err)
	}
	if state, _ := gnome.State(2080); state != Off || settings.values["org.gnome.system.proxy.http host"] != "'127.0.0.1'" {
		t.Errorf("after off: %v, %v", state, settings.values)
	}
}

// fakeMac answers route, networksetup and defaults the way macOS prints them.
type fakeMac struct {
	proxies  map[string]string // getter -> "server port", empty when disabled
	defaults map[string]string
	calls    []string
}

func (f *fakeMac) run(name string, args ...string) (string, error) {
	f.calls = append(f.calls, name+" "+strings.Join(args, " "))
	switch {
	case name == "route":
		return "   route to: default\n  gateway: 192.168.1.1\n  interface: en0\n", nil
	case name == "networksetup" && args[0] == "-listnetworkserviceorder":
		return "An asterisk (*) denotes that a network service is disabled.\n" +
			"(1) USB Ethernet\n(Hardware Port: USB 10/100/1000 LAN, Device: en7)\n\n" +
			"(2) Wi-Fi\n(Hardware Port: Wi-Fi, Device: en0)\n", nil
	case name == "networksetup":
		server, port, _ := strings.Cut(f.proxies[args[0]], " ")
		enabled := "No"
		if server != "" {
			enabled = "Yes"
		}
		return "Enabled: " + enabled + "\nServer: " + server + "\nPort: " + port + "\nAuthenticated Proxy Enabled: 0\n", nil
	case name == "defaults":
		if f.defaults == nil {
			f.defaults = map[string]string{}
		}
		if args[0] == "write" && len(args) >= 5 {
			f.defaults[args[1]+" "+args[2]] = args[4]
			return "", nil
		}
		if args[0] == "read" && len(args) >= 3 {
			val, ok := f.defaults[args[1]+" "+args[2]]
			if !ok {
				return "", errors.New("domain/key not found")
			}
			return val, nil
		}
		if args[0] == "delete" && len(args) >= 3 {
			delete(f.defaults, args[1]+" "+args[2])
			return "", nil
		}
	}
	return "", nil
}

func TestMacOSFindsTheServiceOfTheDefaultRoute(t *testing.T) {
	mac := &fakeMac{}
	desktop, err := New("darwin", nil, mac.run)
	if err != nil || desktop.Name() != "Wi-Fi" {
		t.Fatalf("got %v, %v", desktop, err)
	}
}

func TestMacOSStateAndChanges(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	mac := &fakeMac{proxies: map[string]string{}}
	desktop := &MacOS{Service: "Wi-Fi", Run: mac.run}
	if state, _ := desktop.State(1080); state != Off {
		t.Errorf("no proxies: %v", state)
	}
	mac.proxies["-getwebproxy"] = "127.0.0.1 1080"
	mac.proxies["-getsocksfirewallproxy"] = "127.0.0.1 1080"
	if state, _ := desktop.State(1080); state != On {
		t.Errorf("ours: %v", state)
	}
	mac.proxies["-getsecurewebproxy"] = "proxy.corp 8080"
	if state, _ := desktop.State(1080); state != Other {
		t.Errorf("a corporate proxy: %v", state)
	}

	mac.calls = nil
	if err := desktop.On(2080); err != nil {
		t.Fatal(err)
	}
	if err := desktop.Off(); err != nil {
		t.Fatal(err)
	}
	ruleFile := filepath.Join(os.TempDir(), "sbc-webrtc.pf")
	want := []string{
		"sudo networksetup -setwebproxy Wi-Fi 127.0.0.1 2080",
		"sudo networksetup -setsecurewebproxy Wi-Fi 127.0.0.1 2080",
		"sudo networksetup -setsocksfirewallproxy Wi-Fi 127.0.0.1 2080",
		"sudo networksetup -setproxybypassdomains Wi-Fi localhost 127.0.0.0/8 ::1 host.docker.internal",
		"defaults write com.google.Chrome WebRtcIPHandling -string disable_non_proxied_udp",
		"defaults write com.microsoft.Edge WebRtcIPHandling -string disable_non_proxied_udp",
		"defaults write com.brave.Browser WebRtcIPHandling -string disable_non_proxied_udp",
		"sudo pfctl -a com.xiaosq2000.sbc.webrtc -f " + ruleFile,
		"sudo pfctl -e",
		"sudo networksetup -setwebproxystate Wi-Fi off",
		"sudo networksetup -setsecurewebproxystate Wi-Fi off",
		"sudo networksetup -setsocksfirewallproxystate Wi-Fi off",
		"defaults read com.google.Chrome WebRtcIPHandling",
		"defaults delete com.google.Chrome WebRtcIPHandling",
		"defaults read com.microsoft.Edge WebRtcIPHandling",
		"defaults delete com.microsoft.Edge WebRtcIPHandling",
		"defaults read com.brave.Browser WebRtcIPHandling",
		"defaults delete com.brave.Browser WebRtcIPHandling",
		"sudo pfctl -a com.xiaosq2000.sbc.webrtc -F all",
	}
	if strings.Join(mac.calls, "\n") != strings.Join(want, "\n") {
		t.Errorf("calls:\n%s\nwant:\n%s", strings.Join(mac.calls, "\n"), strings.Join(want, "\n"))
	}
}

func TestLinuxChromiumPoliciesLifecycle(t *testing.T) {
	tempDir := t.TempDir()
	policyDir := filepath.Join(tempDir, "policies", "managed")
	origDirs := linuxChromiumDirs
	origInstalled := isLinuxBrowserInstalled
	defer func() {
		linuxChromiumDirs = origDirs
		isLinuxBrowserInstalled = origInstalled
	}()
	linuxChromiumDirs = []string{policyDir}
	isLinuxBrowserInstalled = func(dir string) bool { return true }

	var calls []string
	run := func(name string, args ...string) (string, error) {
		calls = append(calls, name+" "+strings.Join(args, " "))
		if name == "sudo" && len(args) >= 3 && args[0] == "cp" {
			src := args[1]
			dst := args[2]
			data, err := os.ReadFile(src)
			if err != nil {
				return "", err
			}
			return "", os.WriteFile(dst, data, 0644)
		}
		if name == "sudo" && len(args) >= 3 && args[0] == "mkdir" {
			return "", os.MkdirAll(args[2], 0755)
		}
		if name == "sudo" && len(args) >= 3 && args[0] == "rm" {
			return "", os.Remove(args[2])
		}
		return "", nil
	}

	// 1. Initial apply writes policy file
	setLinuxChromiumPolicies(run)
	targetFile := filepath.Join(policyDir, "webrtc.json")
	data, err := os.ReadFile(targetFile)
	if err != nil {
		t.Fatalf("target policy file not created: %v", err)
	}
	if !strings.Contains(string(data), winsettings.WebRtcPolicyName) {
		t.Fatalf("unexpected content: %s", string(data))
	}
	callCount := len(calls)
	if callCount != 2 {
		t.Fatalf("expected 2 calls, got %d: %v", callCount, calls)
	}

	// 2. Second apply is idempotent and does not run sudo
	setLinuxChromiumPolicies(run)
	if len(calls) != callCount {
		t.Fatalf("expected idempotent no-op, got additional calls: %v", calls[callCount:])
	}

	// 3. Revert removes the policy file
	revertLinuxChromiumPolicies(run)
	if _, err := os.Stat(targetFile); !os.IsNotExist(err) {
		t.Fatalf("policy file should be removed, got err: %v", err)
	}
}
