package desktop

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func firefoxProfileFixture(t *testing.T) (string, string) {
	t.Helper()
	base := t.TempDir()
	profile := filepath.Join(base, "Profiles", "fixture")
	if err := os.MkdirAll(profile, 0700); err != nil {
		t.Fatal(err)
	}
	return base, profile
}

func writeFirefoxFixture(t *testing.T, profile, name, text string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(profile, name), []byte(text), 0600); err != nil {
		t.Fatal(err)
	}
}

func writeLegacyFirefoxSnapshot(t *testing.T, profile string) {
	t.Helper()
	original := map[string][]string{}
	for _, pref := range firefoxPrefs {
		name, _, _ := firefoxPreference(pref)
		original[name] = []string{pref}
	}
	data, err := json.Marshal(map[string]any{"version": 1, "original": original})
	if err != nil {
		t.Fatal(err)
	}
	writeFirefoxFixture(t, profile, legacyFirefoxStateName, string(data))
}

func TestFirefoxCleanupResetsSavedPreferences(t *testing.T) {
	for _, scenario := range []string{"fresh setup", "legacy block", "orphaned", "stale snapshot", "corrupt snapshot"} {
		t.Run(scenario, func(t *testing.T) {
			base, profile := firefoxProfileFixture(t)
			unrelated := "// Firefox saved preferences\nuser_pref(\"browser.tabs.warnOnClose\", false);\n"
			custom := "// custom\nuser_pref(\"browser.startup.page\", 3);\n"
			managed := strings.Join(firefoxPrefs, "\n") + "\n"
			writeFirefoxFixture(t, profile, "prefs.js", unrelated+managed)
			writeFirefoxFixture(t, profile, "user.js", custom)
			switch scenario {
			case "fresh setup":
				if err := applyFirefoxProfiles(base); err != nil {
					t.Fatal(err)
				}
			case "legacy block":
				writeFirefoxFixture(t, profile, "user.js", custom+firefoxMarker+"\n"+managed)
			case "stale snapshot":
				writeLegacyFirefoxSnapshot(t, profile)
			case "corrupt snapshot":
				writeFirefoxFixture(t, profile, legacyFirefoxStateName, "invalid snapshot")
			}
			if err := revertFirefoxProfiles(base); err != nil {
				t.Fatal(err)
			}
			prefs, err := os.ReadFile(filepath.Join(profile, "prefs.js"))
			if err != nil || string(prefs) != unrelated {
				t.Fatalf("saved preferences: %q, %v; want %q", prefs, err, unrelated)
			}
			user, err := os.ReadFile(filepath.Join(profile, "user.js"))
			if err != nil || string(user) != custom {
				t.Fatalf("custom preferences: %q, %v", user, err)
			}
			if _, err := os.Stat(filepath.Join(profile, legacyFirefoxStateName)); !os.IsNotExist(err) {
				t.Fatal("cleanup retained snapshot:", err)
			}
			if err := revertFirefoxProfiles(base); err != nil {
				t.Fatal("second cleanup:", err)
			}
		})
	}
}

func TestFirefoxCleanupResetsOnlyTheFourExactPreferences(t *testing.T) {
	for _, value := range []string{"true", "false", "null", "42", `"custom"`} {
		t.Run(value, func(t *testing.T) {
			base, profile := firefoxProfileFixture(t)
			unrelated := "// user preferences\r\n" +
				"user_pref(\"media.peerconnection.enabled\", false);\r\n" +
				"user_pref(\"media.peerconnection.ice.proxy_only_extra\", true);\r\n" +
				"user_pref(\"browser.startup.page\", 3);\r\n"
			var selected strings.Builder
			for _, pref := range firefoxPrefs {
				line := strings.Replace(pref, "true", value, 1)
				selected.WriteString(" \t" + line + " // custom value\r\n")
				name, _, _ := firefoxPreference(pref)
				selected.WriteString(strings.Replace(line, `"`+name+`"`, `'`+name+`'`, 1) + "\r\n")
			}
			for _, name := range []string{"prefs.js", "user.js"} {
				writeFirefoxFixture(t, profile, name, unrelated+selected.String()+firefoxMarker+"\r\n")
			}
			if err := revertFirefoxProfiles(base); err != nil {
				t.Fatal(err)
			}
			for _, name := range []string{"prefs.js", "user.js"} {
				got, err := os.ReadFile(filepath.Join(profile, name))
				if err != nil || string(got) != unrelated {
					t.Fatalf("%s: %q, %v; want %q", name, got, err, unrelated)
				}
			}
		})
	}
}

func TestFirefoxCleanupResetsOrphansAcrossPlatformLocations(t *testing.T) {
	for _, goos := range []string{"linux", "darwin", "windows"} {
		t.Run(goos, func(t *testing.T) {
			t.Setenv("HOME", t.TempDir())
			t.Setenv("APPDATA", t.TempDir())
			unrelated := "user_pref(\"browser.startup.page\", 3);\n"
			var profiles []string
			for _, dir := range firefoxDataDirs(goos) {
				profile := filepath.Join(dir, "fixture.default")
				if err := os.MkdirAll(profile, 0700); err != nil {
					t.Fatal(err)
				}
				writeFirefoxFixture(t, profile, "prefs.js", unrelated+strings.Join(firefoxPrefs, "\n")+"\n")
				profiles = append(profiles, profile)
			}
			if err := revertFirefoxWebRTC(goos); err != nil {
				t.Fatal(err)
			}
			if remaining, err := remainingFirefox(goos, nil); err != nil || len(remaining) != 0 {
				t.Fatalf("orphan cleanup left manual work: %v, %v", remaining, err)
			}
			for _, profile := range profiles {
				got, err := os.ReadFile(filepath.Join(profile, "prefs.js"))
				if err != nil || string(got) != unrelated {
					t.Fatalf("profile %s: %q, %v", profile, got, err)
				}
			}
		})
	}
}

func TestFirefoxCleanupLeavesCleanProfileUnchanged(t *testing.T) {
	base, profile := firefoxProfileFixture(t)
	text := "user_pref(\"browser.startup.page\", 3);\n"
	writeFirefoxFixture(t, profile, "prefs.js", text)
	if err := revertFirefoxProfiles(base); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(profile)
	if err != nil || len(entries) != 1 || entries[0].Name() != "prefs.js" {
		t.Fatalf("unchanged cleanup created profile files: %v, %v", entries, err)
	}
	got, err := os.ReadFile(filepath.Join(profile, "prefs.js"))
	if err != nil || string(got) != text {
		t.Fatalf("unrelated preferences changed: %q, %v", got, err)
	}
}

func TestFirefoxSetupPreservesEveryForeignUserPreference(t *testing.T) {
	for _, pref := range firefoxPrefs {
		base, profile := firefoxProfileFixture(t)
		text := strings.Replace(pref, "true", "false", 1) + "\n"
		writeFirefoxFixture(t, profile, "user.js", text)
		if err := applyFirefoxProfiles(base); err != nil {
			t.Fatal(err)
		}
		got, err := os.ReadFile(filepath.Join(profile, "user.js"))
		if err != nil || string(got) != text {
			t.Fatalf("foreign preference changed during setup: %q, %v", got, err)
		}
		if _, err := os.Stat(filepath.Join(profile, legacyFirefoxStateName)); !os.IsNotExist(err) {
			t.Fatal("setup created an obsolete snapshot")
		}
	}
}

func TestFirefoxCleanupFailureRetainsStateForRetry(t *testing.T) {
	base, profile := firefoxProfileFixture(t)
	if err := applyFirefoxProfiles(base); err != nil {
		t.Fatal(err)
	}
	writeLegacyFirefoxSnapshot(t, profile)
	prefs := filepath.Join(profile, "prefs.js")
	if err := os.Mkdir(prefs, 0700); err != nil {
		t.Fatal(err)
	}
	if err := revertFirefoxProfiles(base); err == nil {
		t.Fatal("saved preference read failure hidden")
	}
	for _, name := range []string{"user.js", legacyFirefoxStateName} {
		if _, err := os.Stat(filepath.Join(profile, name)); err != nil {
			t.Fatal("lost cleanup state:", err)
		}
	}
	if err := os.Remove(prefs); err != nil {
		t.Fatal(err)
	}
	writeFirefoxFixture(t, profile, "prefs.js", strings.Join(firefoxPrefs, "\n")+"\n")
	// Also model interruption after user.js cleanup, before snapshot deletion.
	if err := os.Remove(filepath.Join(profile, "user.js")); err != nil {
		t.Fatal(err)
	}
	if err := revertFirefoxProfiles(base); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(prefs)
	if err != nil || strings.Contains(string(got), "media.peerconnection") {
		t.Fatalf("retry retained saved preferences: %q, %v", got, err)
	}
}

func TestFirefoxCleanupRefusesSymlinkedSavedPreferences(t *testing.T) {
	base, profile := firefoxProfileFixture(t)
	if err := applyFirefoxProfiles(base); err != nil {
		t.Fatal(err)
	}
	writeLegacyFirefoxSnapshot(t, profile)
	target := filepath.Join(t.TempDir(), "prefs.js")
	if err := os.WriteFile(target, []byte(firefoxPrefs[0]+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(profile, "prefs.js")); err != nil {
		t.Skip("symbolic links unavailable:", err)
	}
	if err := revertFirefoxProfiles(base); err == nil {
		t.Fatal("symlinked preferences accepted")
	}
	got, err := os.ReadFile(target)
	if err != nil || string(got) != firefoxPrefs[0]+"\n" {
		t.Fatalf("symlink target changed: %q, %v", got, err)
	}
	for _, name := range []string{"user.js", legacyFirefoxStateName} {
		if _, err := os.Stat(filepath.Join(profile, name)); err != nil {
			t.Fatal("symlink rejection lost cleanup state:", err)
		}
	}
}

func TestFirefoxCleanupRefusesNonRegularLegacySnapshot(t *testing.T) {
	base, profile := firefoxProfileFixture(t)
	writeFirefoxFixture(t, profile, "prefs.js", firefoxPrefs[0]+"\n")
	if err := os.Mkdir(filepath.Join(profile, legacyFirefoxStateName), 0700); err != nil {
		t.Fatal(err)
	}
	if err := revertFirefoxProfiles(base); err == nil {
		t.Fatal("non-regular snapshot accepted")
	}
	got, err := os.ReadFile(filepath.Join(profile, "prefs.js"))
	if err != nil || string(got) != firefoxPrefs[0]+"\n" {
		t.Fatalf("failed preflight changed saved preferences: %q, %v", got, err)
	}
}

// A subprocess is necessary: POSIX record locks belong to a process, not a
// descriptor. This also exercises the Windows exclusive profile handle in CI.
func TestFirefoxProfileLockChild(t *testing.T) {
	profile := os.Getenv("SBC_TEST_FIREFOX_LOCK_PROFILE")
	if profile == "" {
		return
	}
	unlock, err := lockFirefoxProfile(profile)
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	writeFirefoxFixture(t, profile, "ready", "ready")
	buf := make([]byte, 1)
	_, _ = os.Stdin.Read(buf)
}

func waitFirefoxLockFixture(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("timed out waiting for Firefox profile lock fixture")
}

func TestFirefoxRunningProfileCleanupFailsSafelyAndRetries(t *testing.T) {
	for _, scenario := range []string{"managed", "orphaned"} {
		t.Run(scenario, func(t *testing.T) {
			base, profile := firefoxProfileFixture(t)
			if scenario == "managed" {
				if err := applyFirefoxProfiles(base); err != nil {
					t.Fatal(err)
				}
			}
			writeFirefoxFixture(t, profile, "prefs.js", firefoxPrefs[0]+"\n")
			cmd := exec.Command(os.Args[0], "-test.run=^TestFirefoxProfileLockChild$")
			cmd.Env = append(os.Environ(), "SBC_TEST_FIREFOX_LOCK_PROFILE="+profile)
			input, err := cmd.StdinPipe()
			if err != nil {
				t.Fatal(err)
			}
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				input.Close()
				_ = cmd.Process.Kill()
				_ = cmd.Wait()
			})
			waitFirefoxLockFixture(t, filepath.Join(profile, "ready"))
			if err := revertFirefoxProfiles(base); err == nil || !strings.Contains(err.Error(), "close Firefox and retry") {
				t.Fatalf("running profile not protected: %v", err)
			}
			got, err := os.ReadFile(filepath.Join(profile, "prefs.js"))
			if err != nil || string(got) != firefoxPrefs[0]+"\n" {
				t.Fatalf("failed cleanup changed preferences: %q, %v", got, err)
			}
			input.Close()
			if err := cmd.Wait(); err != nil {
				t.Fatal(err)
			}
			if err := revertFirefoxProfiles(base); err != nil {
				t.Fatal("retry with closed Firefox:", err)
			}
			got, err = os.ReadFile(filepath.Join(profile, "prefs.js"))
			if err != nil || hasFirefoxProtection(got) {
				t.Fatalf("retry retained WebRTC preferences: %q, %v", got, err)
			}
		})
	}
}
