package desktop

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLinuxCleanupRemovesExactLegacyPolicyWithoutOn(t *testing.T) {
	p := linuxFixture(t)
	legacy := filepath.Join(p.policies[0].location, "webrtc.json")
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
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	file, _ := p.linuxFiles(p.policies[0])
	if _, err := os.Stat(file); !os.IsNotExist(err) {
		t.Fatal("automatic setup reversed opt-out")
	}
}

func TestLinuxWebRTCOffCleansLegacyPolicyAndFirefoxWithoutManualEdits(t *testing.T) {
	p := linuxFixture(t)
	p.policies[0].name = "WebRtcIPHandling"
	file := filepath.Join(p.policies[0].location, "webrtc.json")
	if err := os.WriteFile(file, []byte(legacyLinuxPolicyData), 0644); err != nil {
		t.Fatal(err)
	}
	profile := filepath.Join(firefoxDataDirs("linux")[0], "Profiles", "fixture")
	if err := os.MkdirAll(profile, 0700); err != nil {
		t.Fatal(err)
	}
	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
	writeFirefoxFixture(t, profile, "prefs.js", strings.Join(firefoxPrefs, "\n")+"\n")
	privacy := &browserPrivacy{WebRTCSettings: p, goos: "linux"}
	if err := privacy.SetWebRTC(false); err != nil {
		t.Fatal(err)
	}
	if remaining, err := privacy.RemainingWebRTC(); err != nil || len(remaining) != 0 {
		t.Fatalf("off left manual work: %v, %v", remaining, err)
	}
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(profile, "user.js")); !os.IsNotExist(err) {
		t.Fatal("proxy repair recreated Firefox protection after opt-out")
	}
}

func TestLinuxLegacyCleanupPreservesOtherPolicyFiles(t *testing.T) {
	for _, test := range []struct{ name, filename, data string }{
		{"other filename", "manual.json", legacyLinuxPolicyData},
		{"changed value", "webrtc.json", `{"WebRtcIPHandling":"default"}`},
		{"additional policy", "webrtc.json", `{"WebRtcIPHandling": "disable_non_proxied_udp", "OtherPolicy": true}`},
		{"different formatting", "webrtc.json", `{"WebRtcIPHandling":"disable_non_proxied_udp"}`},
	} {
		t.Run(test.name, func(t *testing.T) {
			p := linuxFixture(t)
			file := filepath.Join(p.policies[0].location, test.filename)
			if err := os.WriteFile(file, []byte(test.data), 0644); err != nil {
				t.Fatal(err)
			}
			p.run = func(string, ...string) (string, error) {
				t.Fatal("foreign policy requested sudo")
				return "", nil
			}
			if err := p.Set(false); err != nil {
				t.Fatal(err)
			}
			got, err := os.ReadFile(file)
			if err != nil || string(got) != test.data {
				t.Fatalf("foreign policy changed: %q, %v", got, err)
			}
		})
	}
}

func TestLinuxLegacyCleanupFailureAndRaceCanRetry(t *testing.T) {
	for _, scenario := range []string{"declined sudo", "ignored removal", "changed before sudo", "symlink before sudo"} {
		t.Run(scenario, func(t *testing.T) {
			p := linuxFixture(t)
			file := filepath.Join(p.policies[0].location, "webrtc.json")
			if err := os.WriteFile(file, []byte(legacyLinuxPolicyData), 0644); err != nil {
				t.Fatal(err)
			}
			runner := p.run
			p.run = func(name string, args ...string) (string, error) {
				switch scenario {
				case "declined sudo":
					return "", errors.New("sudo declined")
				case "ignored removal":
					return "", nil
				case "changed before sudo":
					if err := os.WriteFile(file, []byte(`{"OtherPolicy":true}`), 0644); err != nil {
						t.Fatal(err)
					}
				case "symlink before sudo":
					target := filepath.Join(p.policies[0].location, "manual.json")
					if err := os.Rename(file, target); err != nil {
						t.Fatal(err)
					}
					if err := os.Symlink(target, file); err != nil {
						t.Fatal(err)
					}
				}
				return runner(name, args...)
			}
			if err := p.Set(false); err == nil || !strings.Contains(err.Error(), "automatic WebRTC setup is now off") {
				t.Fatalf("cleanup claimed success: %v", err)
			}
			if _, err := os.Lstat(file); err != nil {
				t.Fatal("failed cleanup removed policy")
			}
			if err := p.Ensure(); err != nil {
				t.Fatal("failed cleanup lost opt-out:", err)
			}
			if err := os.Remove(file); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(file, []byte(legacyLinuxPolicyData), 0644); err != nil {
				t.Fatal(err)
			}
			// The symlink fixture also has a foreign file which must survive.
			p.run = runner
			if err := p.Cleanup(); err != nil {
				t.Fatal("retry:", err)
			}
			if _, err := os.Stat(file); !os.IsNotExist(err) {
				t.Fatal("retry did not remove legacy file")
			}
		})
	}
}
