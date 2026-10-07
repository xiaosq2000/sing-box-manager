package desktop

import (
	"bufio"
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
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
	var result error
	for _, dir := range firefoxDataDirs(goos) {
		result = errors.Join(result, applyFirefoxProfiles(dir))
	}
	return result
}

// revertFirefoxWebRTC resets the four WebRTC preferences in discovered profiles.
func revertFirefoxWebRTC(goos string) error {
	var result error
	for _, dir := range firefoxDataDirs(goos) {
		result = errors.Join(result, revertFirefoxProfiles(dir))
	}
	return result
}

func remainingFirefox(goos string, remaining []string) ([]string, error) {
	for _, dir := range firefoxDataDirs(goos) {
		profiles, err := findFirefoxProfiles(dir)
		if err != nil {
			return nil, err
		}
		for _, profile := range profiles {
			for _, name := range []string{"user.js", "prefs.js"} {
				data, err := readFirefoxFile(profile, name)
				if err != nil {
					return nil, err
				}
				if hasFirefoxProtection(data) {
					remaining = append(remaining, filepath.Join(profile, name))
				}
			}
		}
	}
	return remaining, nil
}

// findFirefoxProfiles finds all profile directories under baseDir.
// It checks profiles.ini if present, and scans for subdirectories containing prefs.js.
func findFirefoxProfiles(baseDir string) ([]string, error) {
	if baseDir == "" {
		return nil, nil
	}
	info, err := os.Stat(baseDir)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !info.IsDir() {
		return nil, i18n.Errorf("%s is not a Firefox profile directory", baseDir)
	}
	seen := map[string]bool{}
	var profiles []string
	var discoveryErr error

	add := func(path string) {
		clean := filepath.Clean(path)
		if !seen[clean] {
			if st, err := os.Stat(clean); err == nil && st.IsDir() {
				seen[clean] = true
				profiles = append(profiles, clean)
			} else if err != nil && !os.IsNotExist(err) {
				discoveryErr = errors.Join(discoveryErr, err)
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
		discoveryErr = errors.Join(discoveryErr, scanner.Err())
	} else if !os.IsNotExist(err) {
		discoveryErr = errors.Join(discoveryErr, err)
	}

	// 2. Check Profiles/ subfolder
	profilesDir := filepath.Join(baseDir, "Profiles")
	if entries, err := os.ReadDir(profilesDir); err == nil {
		for _, entry := range entries {
			if entry.IsDir() {
				add(filepath.Join(profilesDir, entry.Name()))
			}
		}
	} else if !os.IsNotExist(err) {
		discoveryErr = errors.Join(discoveryErr, err)
	}

	// 3. Check direct child directories containing prefs.js
	if entries, err := os.ReadDir(baseDir); err == nil {
		for _, entry := range entries {
			if entry.IsDir() {
				prefsFile := filepath.Join(baseDir, entry.Name(), "prefs.js")
				if _, err := os.Stat(prefsFile); err == nil {
					add(filepath.Join(baseDir, entry.Name()))
				} else if !os.IsNotExist(err) {
					discoveryErr = errors.Join(discoveryErr, err)
				}
			}
		}
	} else {
		discoveryErr = errors.Join(discoveryErr, err)
	}

	return profiles, discoveryErr
}

// Replace only the profile file. Unlike executable replacement, a locked
// Firefox file must fail without moving the original to a leftover .old file.
func writeFirefoxFile(path string, data []byte) error {
	mode := os.FileMode(0600)
	if info, err := os.Lstat(path); err == nil {
		if !info.Mode().IsRegular() {
			return i18n.Errorf("WebRTC settings path is not a regular file: %s", path)
		}
		mode = info.Mode().Perm()
	} else if !os.IsNotExist(err) {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".sbc-webrtc-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if err = file.Chmod(mode); err == nil {
		_, err = file.Write(data)
	}
	if err == nil {
		err = file.Sync()
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}

func applyFirefoxProfiles(baseDir string) error {
	profiles, result := findFirefoxProfiles(baseDir)
	for _, profile := range profiles {
		result = errors.Join(result, applyFirefoxProfile(profile))
	}
	return result
}

// revertFirefoxProfiles resets saved preferences before removing obsolete state.
func revertFirefoxProfiles(baseDir string) error {
	profiles, result := findFirefoxProfiles(baseDir)
	for _, profile := range profiles {
		result = errors.Join(result, revertFirefoxProfile(profile))
	}
	return result
}
