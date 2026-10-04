package desktop

import (
	"bufio"
	"bytes"
	"os"
	"path/filepath"
	"strings"
)

const (
	firefoxMarker = "// Managed by sbc - WebRTC leak prevention"
)

var firefoxPrefs = []string{
	`user_pref("media.peerconnection.ice.no_host", true);`,
	`user_pref("media.peerconnection.ice.default_address_only", true);`,
	`user_pref("media.peerconnection.ice.proxy_only", true);`,
	`user_pref("media.peerconnection.ice.proxy_only_if_behind_proxy", true);`,
}

// firefoxDataDirs returns the Firefox application data directories for the given OS.
// On Linux, it checks traditional ~/.mozilla/firefox, Ubuntu Snap, and Flatpak directories.
func firefoxDataDirs(goos string) []string {
	switch goos {
	case "windows":
		if appdata := os.Getenv("APPDATA"); appdata != "" {
			return []string{filepath.Join(appdata, "Mozilla", "Firefox")}
		}
	case "darwin":
		if home := os.Getenv("HOME"); home != "" {
			return []string{filepath.Join(home, "Library", "Application Support", "Firefox")}
		}
	case "linux":
		if home := os.Getenv("HOME"); home != "" {
			return []string{
				filepath.Join(home, ".mozilla", "firefox"),
				filepath.Join(home, "snap", "firefox", "common", ".mozilla", "firefox"),
				filepath.Join(home, ".var", "app", "org.mozilla.firefox", ".mozilla", "firefox"),
			}
		}
	}
	return nil
}

// applyFirefoxWebRTC injects the WebRTC proxy-only setting into user.js across all found Firefox installations.
func applyFirefoxWebRTC(goos string) error {
	for _, dir := range firefoxDataDirs(goos) {
		_ = applyFirefoxProfiles(dir)
	}
	return nil
}

// revertFirefoxWebRTC removes sbc-managed settings from user.js across all found Firefox installations.
func revertFirefoxWebRTC(goos string) error {
	for _, dir := range firefoxDataDirs(goos) {
		_ = revertFirefoxProfiles(dir)
	}
	return nil
}

// findFirefoxProfiles finds all profile directories under baseDir.
// It checks profiles.ini if present, and scans for subdirectories containing prefs.js.
func findFirefoxProfiles(baseDir string) []string {
	if baseDir == "" {
		return nil
	}
	info, err := os.Stat(baseDir)
	if err != nil || !info.IsDir() {
		return nil
	}
	seen := map[string]bool{}
	var profiles []string

	add := func(path string) {
		clean := filepath.Clean(path)
		if !seen[clean] {
			if st, err := os.Stat(clean); err == nil && st.IsDir() {
				seen[clean] = true
				profiles = append(profiles, clean)
			}
		}
	}

	// 1. Check profiles.ini
	iniPath := filepath.Join(baseDir, "profiles.ini")
	if data, err := os.ReadFile(iniPath); err == nil {
		scanner := bufio.NewScanner(bytes.NewReader(data))
		isRelative := true
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if strings.HasPrefix(line, "IsRelative=") {
				isRelative = strings.TrimPrefix(line, "IsRelative=") != "0"
			} else if strings.HasPrefix(line, "Path=") {
				rel := strings.TrimPrefix(line, "Path=")
				if isRelative {
					add(filepath.Join(baseDir, filepath.FromSlash(rel)))
				} else {
					add(filepath.FromSlash(rel))
				}
				isRelative = true
			}
		}
	}

	// 2. Check Profiles/ subfolder
	profilesDir := filepath.Join(baseDir, "Profiles")
	if entries, err := os.ReadDir(profilesDir); err == nil {
		for _, entry := range entries {
			if entry.IsDir() {
				add(filepath.Join(profilesDir, entry.Name()))
			}
		}
	}

	// 3. Check direct child directories containing prefs.js
	if entries, err := os.ReadDir(baseDir); err == nil {
		for _, entry := range entries {
			if entry.IsDir() {
				prefsFile := filepath.Join(baseDir, entry.Name(), "prefs.js")
				if _, err := os.Stat(prefsFile); err == nil {
					add(filepath.Join(baseDir, entry.Name()))
				}
			}
		}
	}

	return profiles
}

// applyFirefoxProfiles injects WebRTC leak prevention settings into user.js for all profiles under baseDir.
func applyFirefoxProfiles(baseDir string) error {
	profiles := findFirefoxProfiles(baseDir)
	for _, profile := range profiles {
		userJSPath := filepath.Join(profile, "user.js")
		content, err := os.ReadFile(userJSPath)
		if err != nil && !os.IsNotExist(err) {
			continue
		}
		text := string(content)
		if strings.Contains(text, "media.peerconnection.ice.proxy_only") || strings.Contains(text, firefoxMarker) {
			continue
		}
		var newContent strings.Builder
		if len(text) > 0 {
			newContent.WriteString(text)
			if !strings.HasSuffix(text, "\n") {
				newContent.WriteString("\n")
			}
		}
		newContent.WriteString(firefoxMarker + "\n")
		for _, pref := range firefoxPrefs {
			newContent.WriteString(pref + "\n")
		}
		_ = os.WriteFile(userJSPath, []byte(newContent.String()), 0600)
	}
	return nil
}

// revertFirefoxProfiles removes sbc-managed settings from user.js for all profiles under baseDir.
func revertFirefoxProfiles(baseDir string) error {
	profiles := findFirefoxProfiles(baseDir)
	for _, profile := range profiles {
		userJSPath := filepath.Join(profile, "user.js")
		content, err := os.ReadFile(userJSPath)
		if err != nil {
			continue
		}
		text := string(content)
		if !strings.Contains(text, firefoxMarker) {
			continue
		}
		prefMap := make(map[string]bool)
		for _, pref := range firefoxPrefs {
			prefMap[strings.TrimSpace(pref)] = true
		}
		var lines []string
		inManagedBlock := false
		for _, line := range strings.Split(text, "\n") {
			trimmed := strings.TrimSpace(line)
			if trimmed == firefoxMarker {
				inManagedBlock = true
				continue
			}
			if inManagedBlock && prefMap[trimmed] {
				continue
			}
			inManagedBlock = false
			lines = append(lines, line)
		}
		remaining := strings.TrimSpace(strings.Join(lines, "\n"))
		if remaining == "" {
			_ = os.Remove(userJSPath)
		} else {
			_ = os.WriteFile(userJSPath, []byte(strings.Join(lines, "\n")), 0600)
		}
	}
	return nil
}
