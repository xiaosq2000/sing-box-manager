package desktop

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/browserprivacy"
)

func chromeFixture(t *testing.T) *chromePrivacy {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the Unix payload needs /bin/sh")
	}
	t.Setenv("HOME", t.TempDir())
	dir := t.TempDir()
	p := &chromePrivacy{
		uid:    "1001",
		policy: unixPolicy{location: dir, name: "WebRtcIPHandling", programs: []string{"google-chrome"}},
	}
	state := filepath.Join(t.TempDir(), "webrtc-mode")
	p.Manager = browserprivacy.Manager{
		StateFile: state,
		Lock:      func() (func(), error) { return lockUnixPrivacy(state + ".lock") },
		Enable:    p.enable,
		Remove:    p.remove,
	}
	p.run = func(name string, args ...string) (string, error) {
		if name != "sudo" || args[0] != "/bin/sh" || args[2] != linuxPolicyScript {
			t.Fatalf("unexpected command: %s %v", name, args)
		}
		output, err := exec.Command(args[0], args[1:]...).CombinedOutput()
		if err != nil {
			return "", &commandError{err: err, output: string(output)}
		}
		return string(output), nil
	}
	return p
}

type commandError struct {
	err    error
	output string
}

func (e *commandError) Error() string { return e.err.Error() + ": " + e.output }

func TestChromeWebRTCLifecycle(t *testing.T) {
	p := chromeFixture(t)
	file, owner := p.linuxFiles(p.policy)
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	var values map[string]string
	if err := json.Unmarshal(data, &values); err != nil || values["WebRtcIPHandling"] != chromePolicyValue {
		t.Fatalf("unexpected Chrome policy: %s, %v", data, err)
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
		t.Fatal("automatic setup reversed opt-out")
	}
	optedOut, err := p.OptedOut()
	if err != nil || !optedOut {
		t.Fatalf("opt-out state: %t, %v", optedOut, err)
	}
	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
	optedOut, err = p.OptedOut()
	if err != nil || optedOut {
		t.Fatalf("opt-in state: %t, %v", optedOut, err)
	}
}

func TestChromeCleanupRemovesTheExactLegacyPolicy(t *testing.T) {
	p := chromeFixture(t)
	legacy := filepath.Join(p.policy.location, "webrtc.json")
	if err := os.WriteFile(legacy, []byte(legacyLinuxPolicyData), 0644); err != nil {
		t.Fatal(err)
	}
	if err := p.Set(false); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(legacy); !os.IsNotExist(err) {
		t.Fatal("legacy policy was retained:", err)
	}
	if remaining, err := p.Remaining(); err != nil || len(remaining) != 0 {
		t.Fatalf("cleanup left manual work: %v, %v", remaining, err)
	}
}

func TestChromeCleanupPreservesForeignPolicyFiles(t *testing.T) {
	for _, test := range []struct{ name, filename, data string }{
		{"other filename", "manual.json", legacyLinuxPolicyData},
		{"changed value", "webrtc.json", `{"WebRtcIPHandling":"default"}`},
		{"additional policy", "webrtc.json", `{"WebRtcIPHandling": "disable_non_proxied_udp", "OtherPolicy": true}`},
		{"different formatting", "webrtc.json", `{"WebRtcIPHandling":"disable_non_proxied_udp"}`},
	} {
		t.Run(test.name, func(t *testing.T) {
			p := chromeFixture(t)
			path := filepath.Join(p.policy.location, test.filename)
			if err := os.WriteFile(path, []byte(test.data), 0644); err != nil {
				t.Fatal(err)
			}
			if err := p.Set(false); err != nil {
				t.Fatal(err)
			}
			if _, err := os.Stat(path); err != nil {
				t.Fatalf("foreign policy was removed: %v", err)
			}
		})
	}
}

func TestChromeConflictsWithForeignPolicy(t *testing.T) {
	p := chromeFixture(t)
	path := filepath.Join(p.policy.location, "manual.json")
	if err := os.WriteFile(path, []byte(`{"WebRtcIPHandling":"default"}`), 0644); err != nil {
		t.Fatal(err)
	}
	if err := p.Ensure(); err == nil || !strings.Contains(err.Error(), "conflicts") {
		t.Fatalf("conflict was not reported: %v", err)
	}
}

func TestChromeRefusesAnUnownedReservedFile(t *testing.T) {
	p := chromeFixture(t)
	file, _ := p.linuxFiles(p.policy)
	if err := os.WriteFile(file, []byte(`{"WebRtcIPHandling":"disable_non_proxied_udp"}`), 0644); err != nil {
		t.Fatal(err)
	}
	if err := p.Ensure(); err == nil || !strings.Contains(err.Error(), "without ownership") {
		t.Fatalf("unowned file was accepted: %v", err)
	}
}

func TestChromeRemainingReportsOnlyTheManagedPolicy(t *testing.T) {
	p := chromeFixture(t)
	if err := os.WriteFile(filepath.Join(p.policy.location, "other.json"), []byte(`{"OtherPolicy":true}`), 0644); err != nil {
		t.Fatal(err)
	}
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	remaining, err := p.Remaining()
	if err != nil {
		t.Fatal(err)
	}
	if len(remaining) != 1 || !strings.Contains(remaining[0], "sbc-webrtc-1001.json") {
		t.Fatalf("remaining policies: %v", remaining)
	}
}
