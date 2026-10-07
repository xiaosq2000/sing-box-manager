package desktop

import (
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

func TestFirefoxCleanupRestoresSavedPreferences(t *testing.T) {
	for _, legacy := range []bool{false, true} {
		name := "snapshot"
		if legacy {
			name = "legacy block"
		}
		t.Run(name, func(t *testing.T) {
			base, profile := firefoxProfileFixture(t)
			unrelated := "// Firefox saved preferences\nuser_pref(\"browser.tabs.warnOnClose\", false);\n"
			original := unrelated + strings.Replace(firefoxPrefs[0], "true", "false", 1) + "\n" + firefoxPrefs[1] + "\n"
			custom := "// custom\nuser_pref(\"browser.startup.page\", 3);\n"
			managed := strings.Join(firefoxPrefs, "\n") + "\n"
			writeFirefoxFixture(t, profile, "prefs.js", original)
			writeFirefoxFixture(t, profile, "user.js", custom)
			if legacy {
				writeFirefoxFixture(t, profile, "user.js", custom+firefoxMarker+"\n"+managed)
			} else if err := applyFirefoxProfiles(base); err != nil {
				t.Fatal(err)
			}
			// Model Firefox copying user.js into prefs.js at startup.
			writeFirefoxFixture(t, profile, "prefs.js", unrelated+managed)
			if err := revertFirefoxProfiles(base); err != nil {
				t.Fatal(err)
			}
			want := original
			if legacy {
				want = unrelated
			}
			got, err := os.ReadFile(filepath.Join(profile, "prefs.js"))
			if err != nil || string(got) != want {
				t.Fatalf("saved preferences: %q, %v; want %q", got, err, want)
			}
			user, err := os.ReadFile(filepath.Join(profile, "user.js"))
			if err != nil || string(user) != custom {
				t.Fatalf("custom preferences: %q, %v", user, err)
			}
			if _, err := os.Stat(filepath.Join(profile, firefoxStateName)); !os.IsNotExist(err) {
				t.Fatal("cleanup retained snapshot:", err)
			}
			if err := revertFirefoxProfiles(base); err != nil {
				t.Fatal("second cleanup:", err)
			}
		})
	}
}

func TestFirefoxCleanupPreservesChangedAndUnownedSavedPreferences(t *testing.T) {
	for _, scenario := range []string{"unowned", "changed saved value", "foreign user.js", "changed block"} {
		t.Run(scenario, func(t *testing.T) {
			base, profile := firefoxProfileFixture(t)
			if scenario != "unowned" {
				if err := applyFirefoxProfiles(base); err != nil {
					t.Fatal(err)
				}
			}
			want := firefoxPrefs[0] + "\n"
			switch scenario {
			case "changed saved value":
				want = strings.Replace(want, "true", "false", 1)
			case "foreign user.js":
				user, err := os.ReadFile(filepath.Join(profile, "user.js"))
				if err != nil {
					t.Fatal(err)
				}
				writeFirefoxFixture(t, profile, "user.js", firefoxPrefs[0]+"\n"+string(user))
			case "changed block":
				writeFirefoxFixture(t, profile, "user.js", firefoxMarker+"\n"+strings.Replace(firefoxPrefs[0], "true", "false", 1)+"\n")
			}
			writeFirefoxFixture(t, profile, "prefs.js", want)
			if err := revertFirefoxProfiles(base); err != nil {
				t.Fatal(err)
			}
			got, err := os.ReadFile(filepath.Join(profile, "prefs.js"))
			if err != nil || string(got) != want {
				t.Fatalf("foreign preference changed: %q, %v", got, err)
			}
		})
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
			t.Fatalf("foreign preference changed: %q, %v", got, err)
		}
		if _, err := os.Stat(filepath.Join(profile, firefoxStateName)); !os.IsNotExist(err) {
			t.Fatal("foreign preference was claimed")
		}
	}
}

func TestFirefoxCleanupFailureRetainsOwnershipForRetry(t *testing.T) {
	base, profile := firefoxProfileFixture(t)
	if err := applyFirefoxProfiles(base); err != nil {
		t.Fatal(err)
	}
	prefs := filepath.Join(profile, "prefs.js")
	if err := os.Mkdir(prefs, 0700); err != nil {
		t.Fatal(err)
	}
	if err := revertFirefoxProfiles(base); err == nil {
		t.Fatal("saved preference read failure hidden")
	}
	for _, name := range []string{"user.js", firefoxStateName} {
		if _, err := os.Stat(filepath.Join(profile, name)); err != nil {
			t.Fatal("lost cleanup ownership:", err)
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
	for _, name := range []string{"user.js", firefoxStateName} {
		if _, err := os.Stat(filepath.Join(profile, name)); err != nil {
			t.Fatal("symlink rejection lost ownership:", err)
		}
	}
}

func TestFirefoxCleanupRejectsInvalidSnapshot(t *testing.T) {
	base, profile := firefoxProfileFixture(t)
	if err := applyFirefoxProfiles(base); err != nil {
		t.Fatal(err)
	}
	writeFirefoxFixture(t, profile, firefoxStateName, `{"version":2,"original":{}}`)
	writeFirefoxFixture(t, profile, "prefs.js", firefoxPrefs[0]+"\n")
	if err := revertFirefoxProfiles(base); err == nil {
		t.Fatal("invalid ownership accepted")
	}
	if _, err := os.Stat(filepath.Join(profile, "user.js")); err != nil {
		t.Fatal("invalid ownership removed managed block")
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
	base, profile := firefoxProfileFixture(t)
	if err := applyFirefoxProfiles(base); err != nil {
		t.Fatal(err)
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
	for _, name := range []string{"user.js", "prefs.js", firefoxStateName} {
		if _, err := os.Stat(filepath.Join(profile, name)); err != nil {
			t.Fatalf("failed cleanup changed %s: %v", name, err)
		}
	}
	input.Close()
	if err := cmd.Wait(); err != nil {
		t.Fatal(err)
	}
	if err := revertFirefoxProfiles(base); err != nil {
		t.Fatal("retry with closed Firefox:", err)
	}
}
