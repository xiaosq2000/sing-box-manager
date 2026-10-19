package tun

import (
	"encoding/json"
	"os"
	"os/user"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestCheckRefusesUnsafeHosts(t *testing.T) {
	base := func() Host {
		return Host{
			GOOS:   "linux",
			Euid:   1000,
			Getenv: func(string) string { return "" },
			Stat: func(name string) (os.FileInfo, error) {
				switch name {
				case "/run/systemd/system", "/dev/net/tun":
					return nil, nil
				}
				return nil, os.ErrNotExist
			},
			ReadFile: func(name string) ([]byte, error) {
				switch name {
				case "/proc/sys/kernel/osrelease":
					return []byte("6.8.0-generic"), nil
				case "/proc/version":
					return []byte("Linux version 6.8.0"), nil
				case "/proc/1/cgroup":
					return []byte("0::/init.scope"), nil
				}
				return nil, os.ErrNotExist
			},
			LookPath: func(name string) (string, error) { return "/usr/bin/" + name, nil },
			Shared:   func() bool { return false },
		}
	}
	if err := Check(base()); err != nil {
		t.Fatalf("base host was refused: %v", err)
	}

	cases := []struct {
		name   string
		mutate func(*Host)
		match  string
	}{
		{"not linux", func(h *Host) { h.GOOS = "darwin" }, "native Linux"},
		{"root user", func(h *Host) { h.Euid = 0 }, "as your user"},
		{"ssh", func(h *Host) { h.Getenv = func(string) string { return "1.2.3.4" } }, "SSH"},
		{"wsl", func(h *Host) {
			h.ReadFile = func(name string) ([]byte, error) {
				if name == "/proc/sys/kernel/osrelease" {
					return []byte("5.15.153.1-microsoft-standard-WSL2"), nil
				}
				return base().ReadFile(name)
			}
		}, "WSL2"},
		{"container", func(h *Host) {
			h.Stat = func(name string) (os.FileInfo, error) {
				if name == "/run/systemd/container" {
					return nil, nil
				}
				return base().Stat(name)
			}
			h.ReadFile = func(name string) ([]byte, error) {
				if name == "/run/systemd/container" {
					return []byte("docker\n"), nil
				}
				return base().ReadFile(name)
			}
		}, "container"},
		{"no systemd", func(h *Host) {
			h.Stat = func(name string) (os.FileInfo, error) {
				if name == "/run/systemd/system" {
					return nil, os.ErrNotExist
				}
				return base().Stat(name)
			}
		}, "systemd"},
		{"no tun", func(h *Host) {
			h.Stat = func(name string) (os.FileInfo, error) {
				if name == "/dev/net/tun" {
					return nil, os.ErrNotExist
				}
				return base().Stat(name)
			}
		}, "/dev/net/tun"},
		{"missing tool", func(h *Host) {
			h.LookPath = func(name string) (string, error) {
				if name == "nft" {
					return "", os.ErrNotExist
				}
				return "/usr/bin/" + name, nil
			}
		}, "nft"},
		{"shared host", func(h *Host) { h.Shared = func() bool { return true } }, "single-user"},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			host := base()
			test.mutate(&host)
			err := Check(host)
			if err == nil || !strings.Contains(err.Error(), test.match) {
				t.Fatalf("Check error = %v, want substring %q", err, test.match)
			}
		})
	}
}

func TestPatchAddsLocalAPIAndRulePaths(t *testing.T) {
	config := []byte(`{
		"experimental": {"clash_api": {"default_mode": "china-direct"}, "cache_file": {"enabled": true}},
		"route": {"rule_set": [
			{"tag": "a", "type": "remote", "initial_path": "rules/a.srs"},
			{"tag": "b", "type": "remote", "initial_path": "rules/b.srs"},
			{"tag": "c", "type": "remote", "initial_path": "rules/a.srs"}
		]}
	}`)
	patched, files, err := Patch(config, Local{
		APIPort:   63001,
		APISecret: "fixture-secret",
		CachePath: "/var/lib/sbc-tun/cache.db",
		RulesDir:  "/etc/sbc/tun/rules",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 2 || files[0] != "a.srs" || files[1] != "b.srs" {
		t.Fatalf("files = %v", files)
	}
	var root map[string]any
	if err := json.Unmarshal(patched, &root); err != nil {
		t.Fatal(err)
	}
	experimental := root["experimental"].(map[string]any)
	api := experimental["clash_api"].(map[string]any)
	if api["external_controller"] != "127.0.0.1:63001" || api["secret"] != "fixture-secret" {
		t.Fatalf("API = %#v", api)
	}
	if experimental["cache_file"].(map[string]any)["path"] != "/var/lib/sbc-tun/cache.db" {
		t.Fatalf("cache = %#v", experimental["cache_file"])
	}
	sets := root["route"].(map[string]any)["rule_set"].([]any)
	first := sets[0].(map[string]any)["initial_path"]
	if first != "/etc/sbc/tun/rules/a.srs" {
		t.Fatalf("initial_path = %v", first)
	}
	if sets[0].(map[string]any)["update_interval"] != "0" {
		t.Fatalf("update_interval = %v", sets[0].(map[string]any)["update_interval"])
	}

	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, patched, 0o600); err != nil {
		t.Fatal(err)
	}
	port, secret, err := ReadAPI(path)
	if err != nil || port != 63001 || secret != "fixture-secret" {
		t.Fatalf("ReadAPI = %d %q %v", port, secret, err)
	}
}

func TestPatchRefusesAConfigWithoutExperimental(t *testing.T) {
	if _, _, err := Patch([]byte(`{"inbounds": []}`), Local{}); err == nil {
		t.Fatal("expected an error")
	}
}

func TestUnitCarriesTheHardeningAndTheMarker(t *testing.T) {
	paths := DefaultPaths()
	unit := Unit(paths)
	for _, want := range []string{
		"User=sbc-tun",
		"AmbientCapabilities=CAP_NET_ADMIN",
		"CapabilityBoundingSet=CAP_NET_ADMIN",
		"DeviceAllow=/dev/net/tun rw",
		"ConditionPathExists=/dev/net/tun",
		"ExecStartPre=+/bin/touch \"" + paths.Active + "\"",
		"ExecStopPost=+/bin/rm -f \"" + paths.Active + "\"",
		"RuntimeDirectory=sbc-tun",
		"StateDirectory=sbc-tun",
		"LD_LIBRARY_PATH=" + paths.BinaryDir,
	} {
		if !strings.Contains(unit, want) {
			t.Errorf("unit lacks %q:\n%s", want, unit)
		}
	}
}

func TestStateAndOwnerRoundTrip(t *testing.T) {
	layout := paths.Layout{Config: t.TempDir(), Data: t.TempDir()}
	state := State{APIPort: 63001, APISecret: "fixture-secret", Interface: InterfaceName}
	if err := WriteState(layout, state); err != nil {
		t.Fatal(err)
	}
	got, err := ReadState(layout)
	if err != nil || got != state {
		t.Fatalf("ReadState = %#v %v", got, err)
	}
	info, err := os.Stat(StatePath(layout))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("state mode = %v %v", info, err)
	}
	if err := RemoveState(layout); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadState(layout); !os.IsNotExist(err) {
		t.Fatalf("state still exists: %v", err)
	}

	ownerPath := filepath.Join(t.TempDir(), "owner")
	if err := WriteOwner(ownerPath, Owner{UID: 1000, User: "alice"}); err != nil {
		t.Fatal(err)
	}
	owner, err := ReadOwner(ownerPath)
	if err != nil || owner.UID != 1000 || owner.User != "alice" {
		t.Fatalf("ReadOwner = %#v %v", owner, err)
	}
}

func TestProbeUsesTheServiceIdentityAndCleansUp(t *testing.T) {
	runner := &fakeRunner{}
	tools := Tools{IP: "/usr/sbin/ip", Nft: "/usr/sbin/nft", SystemdRun: "/usr/bin/systemd-run"}
	if err := Probe(runner, tools, DefaultPaths()); err != nil {
		t.Fatal(err)
	}
	joined := runner.joined()
	for _, want := range []string{
		"User=sbc-tun",
		"DeviceAllow=/dev/net/tun rw",
		"ip tuntap add dev sbc-tun-probe0 mode tun",
		"nft add table inet sbc_tun_probe_",
		"nft delete table inet sbc_tun_probe_",
	} {
		if !strings.Contains(joined, want) {
			t.Errorf("probe lacks %q:\n%s", want, joined)
		}
	}
}

type fakeRunner struct {
	runCalls  []string
	startErr  bool
	outputs   map[string]string
	failStart bool
}

func (f *fakeRunner) Run(name string, args ...string) error {
	f.runCalls = append(f.runCalls, strings.Join(append([]string{name}, args...), " "))
	if f.failStart && filepath.Base(name) == "systemctl" && len(args) > 0 && args[0] == "start" {
		return os.ErrPermission
	}
	return nil
}

func (f *fakeRunner) Output(name string, args ...string) (string, error) {
	if f.outputs != nil {
		if value, ok := f.outputs[name]; ok {
			return value, nil
		}
	}
	return "", nil
}

func (f *fakeRunner) joined() string { return strings.Join(f.runCalls, "\n") }

func TestApplyStagesAndEnablesTheService(t *testing.T) {
	root := t.TempDir()
	paths := testPaths(root)
	staged := filepath.Join(root, "staged")
	stagedBin := filepath.Join(staged, "bin")
	stagedRules := filepath.Join(staged, "rules")
	for _, dir := range []string{stagedBin, stagedRules} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(stagedBin, "sing-box"), []byte("binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stagedBin, "libcronet.so"), []byte("library"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stagedRules, "a.srs"), []byte("rules"), 0o644); err != nil {
		t.Fatal(err)
	}
	config := filepath.Join(staged, "config.json")
	if err := os.WriteFile(config, []byte(`{}`), 0o600); err != nil {
		t.Fatal(err)
	}

	runner := &fakeRunner{}
	opts := Options{
		Paths:     paths,
		Config:    config,
		BinDir:    stagedBin,
		RulesDir:  stagedRules,
		OwnerUID:  1000,
		OwnerUser: "alice",
		Runner:    runner,
		Euid:      func() int { return 0 },
		LookPath:  func(name string) (string, error) { return "/usr/sbin/" + name, nil },
		LookupUser: func(string) (*user.User, error) {
			return &user.User{Uid: "61000", Gid: "61000", Username: ServiceUser}, nil
		},
		LookupGroup: func(string) (*user.Group, error) {
			return &user.Group{Gid: "61000", Name: ServiceGroup}, nil
		},
		Chown:  func(string, int, int) error { return nil },
		Verify: func(Paths, Runner, Tools) error { return nil },
	}
	if err := Apply(opts); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{
		paths.Binary, paths.Config, paths.Unit, paths.Owner, paths.BinaryHash, paths.Active,
	} {
		if _, err := os.Stat(path); err != nil {
			t.Errorf("missing %s: %v", path, err)
		}
	}
	if content, err := os.ReadFile(paths.Unit); err != nil || !strings.Contains(string(content), "ExecStart=") {
		t.Fatalf("unit: %v %q", err, content)
	}
	if owner, err := ReadOwner(paths.Owner); err != nil || owner.UID != 1000 {
		t.Fatalf("owner: %#v %v", owner, err)
	}
	if info, err := os.Stat(filepath.Dir(paths.ConfigDir)); err != nil || info.Mode().Perm() != 0o755 {
		t.Fatalf("config parent: %v %v", info, err)
	}
	joined := runner.joined()
	for _, want := range []string{
		"systemctl daemon-reload",
		"systemctl start " + ServiceName,
		"systemctl enable " + ServiceName,
		"sing-box check",
		"ip tuntap add",
	} {
		if !strings.Contains(joined, want) {
			t.Errorf("commands lack %q:\n%s", want, joined)
		}
	}

	runner.failStart = true
	if err := os.Remove(paths.Active); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	if err := Apply(opts); err == nil {
		t.Fatal("expected a failed activation")
	}
	if _, err := os.Stat(paths.Active); !os.IsNotExist(err) {
		t.Fatalf("active marker survived a failed activation: %v", err)
	}
}

func TestStopRemovesTheMarker(t *testing.T) {
	root := t.TempDir()
	paths := testPaths(root)
	if err := os.MkdirAll(filepath.Dir(paths.Active), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(paths.Active, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	runner := &fakeRunner{}
	if err := Stop(StopOptions{
		Paths:    paths,
		Runner:   runner,
		Euid:     func() int { return 0 },
		LookPath: func(name string) (string, error) { return "/usr/sbin/" + name, nil },
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(paths.Active); !os.IsNotExist(err) {
		t.Fatalf("active marker survived stop: %v", err)
	}
	if !strings.Contains(runner.joined(), "systemctl disable --now "+ServiceName) {
		t.Fatalf("commands = %s", runner.joined())
	}
}

func testPaths(root string) Paths {
	state := filepath.Join(root, "var/lib/sbc-tun")
	config := filepath.Join(root, "etc/sbc/tun")
	binary := filepath.Join(root, "usr/local/lib/sbc-tun")
	return Paths{
		BinaryDir:  binary,
		Binary:     filepath.Join(binary, "sing-box"),
		ConfigDir:  config,
		Config:     filepath.Join(config, "config.json"),
		Cache:      filepath.Join(state, "cache.db"),
		RulesDir:   filepath.Join(config, "rules"),
		StateDir:   state,
		Owner:      filepath.Join(state, "owner"),
		BinaryHash: filepath.Join(state, "sing-box.sha256"),
		Unit:       filepath.Join(root, "etc/systemd/system/sbc-tun.service"),
		Active:     filepath.Join(root, "run/sbc-tun/active"),
	}
}

func TestPrepareCopiesTheBinaryAndRuleSnapshots(t *testing.T) {
	layout := paths.Layout{Config: t.TempDir(), Data: t.TempDir()}
	if err := os.MkdirAll(layout.Bin(), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(layout.SingBox(), []byte("binary"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(layout.Bin(), "libcronet.so"), []byte("library"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(layout.Data, "rules"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(layout.Data, "rules", "a.srs"), []byte("rules"), 0o600); err != nil {
		t.Fatal(err)
	}
	config := []byte(`{"experimental": {"clash_api": {}, "cache_file": {}}, "route": {"rule_set": [{"initial_path": "rules/a.srs"}]}}`)
	staged, cleanup, err := Prepare(layout, config, Local{
		APIPort: 63001, APISecret: "secret",
		CachePath: "/var/lib/sbc-tun/cache.db", RulesDir: "/etc/sbc/tun/rules",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer cleanup()
	for _, path := range []string{
		staged.Config,
		filepath.Join(staged.BinDir, "sing-box"),
		filepath.Join(staged.BinDir, "libcronet.so"),
		filepath.Join(staged.RulesDir, "a.srs"),
	} {
		if _, err := os.Stat(path); err != nil {
			t.Errorf("missing %s: %v", path, err)
		}
	}
	port, secret, err := ReadAPI(staged.Config)
	if err != nil || port != 63001 || secret != "secret" {
		t.Fatalf("ReadAPI = %d %q %v", port, secret, err)
	}
}
