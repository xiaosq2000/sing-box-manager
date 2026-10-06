package desktop

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFirefoxProfilesDiscoveryAndUserJS(t *testing.T) {
	tempDir := t.TempDir()

	// Create profile structure
	profile1 := filepath.Join(tempDir, "Profiles", "profile1.default-release")
	profile2 := filepath.Join(tempDir, "Profiles", "profile2.custom")
	if err := os.MkdirAll(profile1, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(profile2, 0755); err != nil {
		t.Fatal(err)
	}

	// Write profiles.ini
	iniContent := `
[General]
StartWithLastProfile=1

[Profile0]
Name=default-release
IsRelative=1
Path=Profiles/profile1.default-release
Default=1

[Profile1]
Name=custom
IsRelative=1
Path=Profiles/profile2.custom
`
	if err := os.WriteFile(filepath.Join(tempDir, "profiles.ini"), []byte(iniContent), 0644); err != nil {
		t.Fatal(err)
	}

	// Existing user.js in profile2 with other preferences
	initialProfile2UserJS := `// custom user settings
user_pref("browser.tabs.warnOnClose", false);
`
	if err := os.WriteFile(filepath.Join(profile2, "user.js"), []byte(initialProfile2UserJS), 0644); err != nil {
		t.Fatal(err)
	}

	// 1. Discovery
	profiles, err := findFirefoxProfiles(tempDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(profiles) != 2 {
		t.Fatalf("expected 2 profiles, got %d: %v", len(profiles), profiles)
	}

	// 2. Apply
	if err := applyFirefoxProfiles(tempDir); err != nil {
		t.Fatalf("apply: %v", err)
	}

	// Check profile1: created user.js with marker and prefs
	p1Content, err := os.ReadFile(filepath.Join(profile1, "user.js"))
	if err != nil {
		t.Fatalf("profile1 user.js: %v", err)
	}
	if !strings.Contains(string(p1Content), firefoxMarker) || !strings.Contains(string(p1Content), "media.peerconnection.ice.proxy_only") || !strings.Contains(string(p1Content), "media.peerconnection.ice.no_host") {
		t.Fatalf("profile1 missing marker or pref:\n%s", string(p1Content))
	}

	// Check profile2: preserved custom settings and appended marker and prefs
	p2Content, err := os.ReadFile(filepath.Join(profile2, "user.js"))
	if err != nil {
		t.Fatalf("profile2 user.js: %v", err)
	}
	if !strings.Contains(string(p2Content), "browser.tabs.warnOnClose") {
		t.Fatalf("profile2 custom pref erased:\n%s", string(p2Content))
	}
	if !strings.Contains(string(p2Content), firefoxMarker) || !strings.Contains(string(p2Content), "media.peerconnection.ice.proxy_only") || !strings.Contains(string(p2Content), "media.peerconnection.ice.no_host") {
		t.Fatalf("profile2 missing marker or pref:\n%s", string(p2Content))
	}

	// Idempotency: applying again should not duplicate
	if err := applyFirefoxProfiles(tempDir); err != nil {
		t.Fatalf("apply again: %v", err)
	}
	p1ContentAgain, _ := os.ReadFile(filepath.Join(profile1, "user.js"))
	if strings.Count(string(p1ContentAgain), firefoxMarker) != 1 {
		t.Fatalf("marker duplicated in profile1:\n%s", string(p1ContentAgain))
	}

	// 3. Revert
	if err := revertFirefoxProfiles(tempDir); err != nil {
		t.Fatalf("revert: %v", err)
	}

	// Profile1 had only sbc settings, so user.js should be deleted
	if _, err := os.Stat(filepath.Join(profile1, "user.js")); !os.IsNotExist(err) {
		t.Fatalf("profile1 user.js should have been deleted, err: %v", err)
	}

	// Profile2 had other settings, so user.js should remain with original settings
	p2Reverted, err := os.ReadFile(filepath.Join(profile2, "user.js"))
	if err != nil {
		t.Fatalf("profile2 user.js missing after revert: %v", err)
	}
	if strings.Contains(string(p2Reverted), firefoxMarker) || strings.Contains(string(p2Reverted), "media.peerconnection.ice.proxy_only") {
		t.Fatalf("profile2 still contains sbc settings:\n%s", string(p2Reverted))
	}
	if !strings.Contains(string(p2Reverted), "browser.tabs.warnOnClose") {
		t.Fatalf("profile2 original settings erased:\n%s", string(p2Reverted))
	}
}

func TestFirefoxProfilesPreservesForeignPreference(t *testing.T) {
	tempDir := t.TempDir()
	profile := filepath.Join(tempDir, "Profiles", "foreign")
	if err := os.MkdirAll(profile, 0755); err != nil {
		t.Fatal(err)
	}
	// User had already configured proxy_only without sbc marker
	foreignUserJS := `user_pref("media.peerconnection.ice.proxy_only", false);`
	if err := os.WriteFile(filepath.Join(profile, "user.js"), []byte(foreignUserJS), 0644); err != nil {
		t.Fatal(err)
	}
	if err := applyFirefoxProfiles(tempDir); err != nil {
		t.Fatal(err)
	}
	content, _ := os.ReadFile(filepath.Join(profile, "user.js"))
	if string(content) != foreignUserJS {
		t.Fatalf("foreign preference modified: %s", string(content))
	}
	if err := revertFirefoxProfiles(tempDir); err != nil {
		t.Fatal(err)
	}
	contentAfter, _ := os.ReadFile(filepath.Join(profile, "user.js"))
	if string(contentAfter) != foreignUserJS {
		t.Fatalf("foreign preference modified after revert: %s", string(contentAfter))
	}
}

func TestFirefoxProfileFailuresAreReported(t *testing.T) {
	base := t.TempDir()
	profile := filepath.Join(base, "Profiles", "fixture")
	// A directory at the file path gives a portable read failure, including
	// when tests run as an administrator or root.
	if err := os.MkdirAll(filepath.Join(profile, "user.js"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := applyFirefoxProfiles(base); err == nil {
		t.Fatal("setup ignored an unreadable user.js")
	}
	if err := revertFirefoxProfiles(base); err == nil {
		t.Fatal("cleanup reported success without reading user.js")
	}
	if err := writeFirefoxUserJS(filepath.Join(profile, "user.js"), []byte("fixture")); err == nil {
		t.Fatal("replacement moved an unusable target aside")
	}
	entries, err := os.ReadDir(profile)
	if err != nil || len(entries) != 1 || entries[0].Name() != "user.js" || !entries[0].IsDir() {
		t.Fatalf("failed replacement left staging files or changed the original: %v, %v", entries, err)
	}
	badBase := filepath.Join(t.TempDir(), "not-a-directory")
	if err := os.WriteFile(badBase, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := revertFirefoxProfiles(badBase); err == nil {
		t.Fatal("cleanup ignored a profile discovery failure")
	}
}

func TestFirefoxProfilesContinueAfterErrors(t *testing.T) {
	for _, discoveryFailure := range []bool{false, true} {
		t.Run(map[bool]string{false: "profile reads", true: "discovery and profile reads"}[discoveryFailure], func(t *testing.T) {
			base := t.TempDir()
			var failedPaths []string
			if discoveryFailure {
				path := filepath.Join(base, "profiles.ini")
				if err := os.Mkdir(path, 0700); err != nil {
					t.Fatal(err)
				}
				failedPaths = append(failedPaths, path)
			}
			for _, name := range []string{"a-broken", "b-broken", "c-healthy"} {
				profile := filepath.Join(base, "Profiles", name)
				if err := os.MkdirAll(profile, 0700); err != nil {
					t.Fatal(err)
				}
				if name != "c-healthy" {
					path := filepath.Join(profile, "user.js")
					if err := os.Mkdir(path, 0700); err != nil {
						t.Fatal(err)
					}
					failedPaths = append(failedPaths, path)
				}
			}
			checkErrors := func(err error) {
				t.Helper()
				for _, path := range failedPaths {
					if err == nil || !strings.Contains(err.Error(), path) {
						t.Errorf("missing error for %s: %v", path, err)
					}
				}
			}
			checkErrors(applyFirefoxProfiles(base))
			healthy := filepath.Join(base, "Profiles", "c-healthy", "user.js")
			data, err := os.ReadFile(healthy)
			if err != nil || !strings.Contains(string(data), firefoxMarker) {
				t.Fatalf("healthy profile was not protected: %s, %v", data, err)
			}
			checkErrors(revertFirefoxProfiles(base))
			if _, err := os.Stat(healthy); !os.IsNotExist(err) {
				t.Fatalf("healthy profile was not cleaned: %v", err)
			}
		})
	}
}

func TestFirefoxDataDirs(t *testing.T) {
	t.Setenv("HOME", "/custom/home")
	t.Setenv("APPDATA", `C:\Users\User\AppData\Roaming`)

	dirsLinux := firefoxDataDirs("linux")
	if len(dirsLinux) != 3 {
		t.Fatalf("expected 3 linux dirs, got %d: %v", len(dirsLinux), dirsLinux)
	}
	expectedLinux := []string{
		filepath.Join("/custom/home", ".mozilla", "firefox"),
		filepath.Join("/custom/home", "snap", "firefox", "common", ".mozilla", "firefox"),
		filepath.Join("/custom/home", ".var", "app", "org.mozilla.firefox", ".mozilla", "firefox"),
	}
	for i, exp := range expectedLinux {
		if dirsLinux[i] != exp {
			t.Fatalf("unexpected linux dirs[%d]: got %q, want %q", i, dirsLinux[i], exp)
		}
	}

	dirsMac := firefoxDataDirs("darwin")
	expectedMac := filepath.Join("/custom/home", "Library", "Application Support", "Firefox")
	if len(dirsMac) != 1 || dirsMac[0] != expectedMac {
		t.Fatalf("unexpected darwin dirs: got %v, want %q", dirsMac, expectedMac)
	}

	dirsWin := firefoxDataDirs("windows")
	expectedWin := filepath.Join(`C:\Users\User\AppData\Roaming`, "Mozilla", "Firefox")
	if len(dirsWin) != 1 || dirsWin[0] != expectedWin {
		t.Fatalf("unexpected windows dirs: got %v, want %q", dirsWin, expectedWin)
	}
}
