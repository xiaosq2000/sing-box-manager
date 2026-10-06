package desktop

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/browserprivacy"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

func fixturePrivacy(t *testing.T, goos string) *unixPrivacy {
	t.Helper()
	t.Setenv("HOME", t.TempDir())
	p := &unixPrivacy{goos: goos, uid: "1001"}
	state := filepath.Join(t.TempDir(), "webrtc-mode")
	p.Manager = browserprivacy.Manager{StateFile: state, Lock: func() (func(), error) { return lockUnixPrivacy(state + ".lock") }, Enable: p.enable, Remove: p.remove}
	return p
}

// Run the exact embedded elevated payload without sudo, inside a fixture tree.
func linuxFixture(t *testing.T) *unixPrivacy {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the Unix payload needs /bin/sh")
	}
	p := fixturePrivacy(t, "linux")
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	p.policies = []unixPolicy{{location: dir, name: winsettings.EdgeWebRtcPolicyName}}
	p.run = func(name string, args ...string) (string, error) {
		if name != "sudo" || args[0] != "/bin/sh" || args[2] != linuxPolicyScript {
			t.Fatalf("unexpected command: %s %v", name, args)
		}
		output, err := exec.Command(args[0], args[1:]...).CombinedOutput()
		if err != nil {
			return "", fmt.Errorf("payload: %w: %s", err, output)
		}
		return string(output), nil
	}
	return p
}
func TestLinuxWebRTCLifecycle(t *testing.T) {
	p := linuxFixture(t)
	file, owner := p.linuxFiles(p.policies[0])
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(file)
	if err != nil || !strings.Contains(string(data), `"WebRtcLocalhostIpHandling"`) {
		t.Fatalf("Edge policy: %s %v", data, err)
	}
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(file); err != nil {
		t.Fatal(err)
	}
	if err := p.Ensure(); err != nil {
		t.Fatal("repair failed:", err)
	}
	if err := p.Set(false); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{file, owner} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("cleanup left %s: %v", path, err)
		}
	}
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(file); !os.IsNotExist(err) {
		t.Fatal("Ensure reversed the opt-out")
	}
	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
	if err := p.Cleanup(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(file); !os.IsNotExist(err) {
		t.Fatal("uninstall left a policy")
	}
}
func TestLinuxWebRTCPreservesManualAndChangedPolicies(t *testing.T) {
	for _, scenario := range []string{"matching", "conflict", "changed", "reserved", "other-user", "invalid-owner", "symlink"} {
		t.Run(scenario, func(t *testing.T) {
			p := linuxFixture(t)
			policy := p.policies[0]
			file, owner := p.linuxFiles(policy)
			target := filepath.Join(policy.location, "manual.json")
			want := linuxPolicyData(policy)
			data := want
			if scenario == "conflict" {
				data = []byte(`{"WebRtcLocalhostIpHandling":"default"}`)
			}
			if scenario == "changed" || scenario == "invalid-owner" {
				if err := p.Set(true); err != nil {
					t.Fatal(err)
				}
				target = file
				if scenario == "changed" {
					data = []byte(`{"WebRtcLocalhostIpHandling":"default","OtherPolicy":true}`)
				} else {
					os.WriteFile(owner, []byte("foreign"), 0644)
				}
			}
			if scenario == "reserved" {
				target = file
				data = []byte(`{"OtherPolicy":true}`)
			}
			if scenario == "other-user" {
				other := *p
				other.uid = "1002"
				if err := other.Set(true); err != nil {
					t.Fatal(err)
				}
				target, _ = other.linuxFiles(policy)
			}
			if err := os.WriteFile(target, data, 0644); err != nil {
				t.Fatal(err)
			}
			if scenario == "symlink" {
				if err := os.Symlink(target, file); err != nil {
					t.Fatal(err)
				}
			}
			err := p.Set(true)
			shouldFail := scenario == "conflict" || scenario == "changed" || scenario == "reserved" || scenario == "invalid-owner" || scenario == "symlink"
			if (err != nil) != shouldFail {
				t.Fatalf("enable: %v", err)
			}
			err = p.Set(false)
			if scenario == "invalid-owner" {
				if err == nil {
					t.Fatal("invalid owner accepted")
				}
			} else if err != nil {
				t.Fatal(err)
			}
			got, err := os.ReadFile(target)
			if err != nil || string(got) != string(data) {
				t.Fatalf("foreign data changed: %q %v", got, err)
			}
		})
	}
}
func TestUnixWebRTCFailureKeepsOptOutAndRetryState(t *testing.T) {
	p := linuxFixture(t)
	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
	runner := p.run
	p.run = func(string, ...string) (string, error) { return "", errors.New("sudo declined") }
	if err := p.Set(false); err == nil {
		t.Fatal("cleanup failure hidden")
	}
	if err := p.Ensure(); err != nil {
		t.Fatal("opt-out did not survive failed cleanup:", err)
	}
	p.run = runner
	if err := p.Set(false); err != nil {
		t.Fatal(err)
	}
	p.run = func(string, ...string) (string, error) { return "", nil }
	if err := p.Set(true); err == nil {
		t.Fatal("unverified writes reported success")
	}
	if data, err := os.ReadFile(p.StateFile); err != nil || string(data) != "off\n" {
		t.Fatal("failed enable removed opt-out")
	}
	p.run = runner
	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
}

type macDefaultFixture struct {
	values       map[string]string
	fail, ignore bool
	reloads      int
	failReload   bool
}

func (f *macDefaultFixture) run(name string, args ...string) (string, error) {
	if f.fail {
		return "", errors.New("permission denied")
	}
	if name == "/usr/bin/plutil" {
		domain := strings.TrimSuffix(args[len(args)-1], ".plist")
		values := map[string]string{}
		for key, value := range f.values {
			if name, ok := strings.CutPrefix(key, domain+"/"); ok {
				values[name] = value
			}
		}
		data, err := json.Marshal(values)
		return string(data), err
	}
	if name == "/usr/bin/env" {
		args = args[2:]
	} else if name == "sudo" {
		if args[0] == "/usr/bin/killall" {
			f.reloads++
			if f.failReload {
				return "", errors.New("reload denied")
			}
			return "", nil
		}
		if args[0] == "/bin/mkdir" || args[0] == "/bin/chmod" {
			return "", nil
		}
		if args[0] == "/usr/libexec/PlistBuddy" {
			action := strings.Fields(args[2])
			domain, key := strings.TrimSuffix(args[3], ".plist"), strings.TrimPrefix(action[1], ":")
			if f.ignore {
				return "", nil
			}
			if action[0] == "Delete" {
				delete(f.values, domain+"/"+key)
			} else {
				f.values[domain+"/"+key] = action[len(action)-1]
			}
			return "", os.WriteFile(domain+".plist", []byte("fixture"), 0644)
		}
		args = args[1:]
	} else if name != "/usr/bin/defaults" {
		return "", fmt.Errorf("unexpected %s", name)
	}
	key := args[1] + "/" + args[2]
	switch args[0] {
	case "read", "read-type":
		value, ok := f.values[key]
		if !ok {
			return "", fmt.Errorf("The domain/default pair of (%s, %s) does not exist", args[1], args[2])
		}
		if args[0] == "read-type" {
			return "Type is string\n", nil
		}
		return value, nil
	case "write":
		if !f.ignore {
			f.values[key] = args[4]
			if filepath.IsAbs(args[1]) {
				if err := os.WriteFile(args[1]+".plist", []byte("fixture"), 0644); err != nil {
					return "", err
				}
			}
		}
	case "delete":
		if !f.ignore {
			delete(f.values, key)
		}
	default:
		return "", fmt.Errorf("unexpected defaults operation %s", args[0])
	}
	return "", nil
}
func TestMacWebRTCOwnershipAndFailures(t *testing.T) {
	p := fixturePrivacy(t, "darwin")
	p.policies = unixBrowserPolicies("darwin")
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for i := range p.policies {
		p.policies[i].location = filepath.Join(base, filepath.Base(p.policies[i].location))
		if err := os.WriteFile(p.policies[i].location+".plist", []byte("fixture"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	f := &macDefaultFixture{values: map[string]string{}}
	p.run = f.run
	chrome := filepath.Join(base, "com.google.Chrome") + "/WebRtcIPHandling"
	edge := filepath.Join(base, "com.microsoft.Edge") + "/WebRtcLocalhostIpHandling"
	legacy := filepath.Join(base, "com.microsoft.Edge") + "/WebRtcIPHandling"
	brave := filepath.Join(base, "com.brave.Browser") + "/WebRtcIPHandling"
	f.values[chrome] = winsettings.WebRtcDisableNonProxiedUDP
	f.values[legacy] = winsettings.WebRtcDisableNonProxiedUDP
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	if f.values[edge] != winsettings.WebRtcDisableNonProxiedUDP {
		t.Fatal("wrong Edge preference")
	}
	f.values[brave] = "default"
	if err := p.Ensure(); err == nil {
		t.Fatal("policy conflict hidden")
	}
	if err := p.Set(false); err != nil {
		t.Fatal(err)
	}
	if len(f.values) != 3 || f.values[chrome] == "" || f.values[legacy] == "" || f.values[brave] != "default" {
		t.Fatalf("cleanup changed manual policies: %v", f.values)
	}
	delete(f.values, brave)
	f.ignore = true
	if err := p.Set(true); err == nil {
		t.Fatal("silent write failure hidden")
	}
	f.ignore = false
	f.fail = true
	if err := p.Set(true); err == nil {
		t.Fatal("permission error counted as absence")
	}
	f.fail = false
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	if _, ok := f.values[edge]; ok {
		t.Fatal("failed enable removed opt-out")
	}
	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
	f.fail = true
	if err := p.Cleanup(); err == nil {
		t.Fatal("cleanup failure hidden")
	}
	f.fail = false
	if err := p.Cleanup(); err != nil {
		t.Fatal(err)
	}
	f.failReload = true
	if err := p.Set(true); err == nil {
		t.Fatal("failed reload reported success")
	}
	if _, err := os.Stat(p.StateFile + ".reload"); err != nil {
		t.Fatal("failed reload lost retry marker")
	}
	f.failReload = false
	before := f.reloads
	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
	if f.reloads != before+1 {
		t.Fatal("unchanged policies did not retry failed reload")
	}
	before = f.reloads
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	if f.reloads != before {
		t.Fatal("unchanged Ensure restarted preference service")
	}
	if _, err := os.Stat(p.StateFile + ".reload"); !os.IsNotExist(err) {
		t.Fatal("successful reload retained pending marker")
	}

}
func TestUnixDesktopKeepsPrivacyAcrossToggles(t *testing.T) {
	for _, goos := range []string{"linux", "darwin"} {
		t.Run(goos, func(t *testing.T) {
			privacy := &fakeWebRTC{}
			var desk Desktop
			if goos == "linux" {
				desk = &GNOME{Run: newGSettings(map[string]string{}).run, Privacy: privacy}
			} else {
				desk = &MacOS{Run: (&fakeMac{proxies: map[string]string{}}).run, Privacy: privacy}
			}
			if err := desk.On(1080); err != nil {
				t.Fatal(err)
			}
			if err := desk.Off(); err != nil {
				t.Fatal(err)
			}
			if !privacy.enabled || privacy.cleanups != 0 {
				t.Fatal("proxy off changed privacy")
			}
			privacy.Set(false)
			if err := desk.On(1080); err != nil {
				t.Fatal(err)
			}
			if privacy.enabled {
				t.Fatal("proxy on reversed opt-out")
			}
			privacy.err = errors.New("policy write denied")
			if err := desk.On(1080); err == nil {
				t.Fatal("proxy on ignored policy failure")
			}
			if err := desk.Off(); err != nil {
				t.Fatal("policy error prevented proxy off:", err)
			}
		})
	}
}
func TestUnixPrivacySerializesSeparateManagers(t *testing.T) {
	p := fixturePrivacy(t, "linux")
	entered, resume := make(chan struct{}), make(chan struct{})
	p.Enable = func() error { close(entered); <-resume; return nil }
	done := make(chan error, 1)
	go func() { done <- p.Ensure() }()
	<-entered
	other := *p
	cleaned := make(chan struct{})
	other.Remove = func() error { close(cleaned); return nil }
	disabled := make(chan error, 1)
	go func() { disabled <- other.Set(false) }()
	select {
	case <-cleaned:
		t.Error("cleanup overtook setup")
	case <-time.After(30 * time.Millisecond):
	}
	close(resume)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if err := <-disabled; err != nil {
		t.Fatal(err)
	}
	if err := other.Ensure(); err != nil {
		t.Fatal(err)
	}
}
