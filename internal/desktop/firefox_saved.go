package desktop

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Older clients saved original values here. Off now resets the four preferences
// instead of restoring potentially stale protection from this snapshot.
const legacyFirefoxStateName = ".sbc-webrtc.json"

var firefoxPreferencePattern = regexp.MustCompile(`^\s*user_pref\(\s*["'](media\.peerconnection\.ice\.(?:no_host|default_address_only|proxy_only|proxy_only_if_behind_proxy))["']\s*,\s*(.*?)\s*\)\s*;\s*(?://.*)?$`)

func firefoxPreference(line string) (name, value string, ok bool) {
	match := firefoxPreferencePattern.FindStringSubmatch(line)
	if match == nil {
		return "", "", false
	}
	value = strings.TrimSpace(match[2])
	return match[1], value, json.Valid([]byte(value))
}

func hasFirefoxProtection(data []byte) bool {
	for _, line := range strings.Split(string(data), "\n") {
		if _, value, ok := firefoxPreference(line); ok && value == "true" {
			return true
		}
	}
	return false
}

func readFirefoxFile(profile, name string) ([]byte, error) {
	data, err := privateFile(filepath.Join(profile, name))
	if os.IsNotExist(err) {
		return nil, nil
	}
	return data, err
}

func applyFirefoxProfile(profile string) error {
	content, err := readFirefoxFile(profile, "user.js")
	if err != nil {
		return err
	}
	text := string(content)
	if strings.Contains(text, firefoxMarker) {
		return nil
	}
	for _, pref := range firefoxPrefs {
		name, _, _ := firefoxPreference(pref)
		if strings.Contains(text, name) {
			return nil // Setup does not override existing user.js preferences.
		}
	}
	if len(text) > 0 && !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
	text += firefoxMarker + "\n" + strings.Join(firefoxPrefs, "\n") + "\n"
	return writeFirefoxFile(filepath.Join(profile, "user.js"), []byte(text))
}

func resetFirefoxPreferences(content []byte) []byte {
	var lines []string
	for _, line := range strings.Split(string(content), "\n") {
		if strings.TrimSpace(line) == firefoxMarker {
			continue
		}
		if _, _, known := firefoxPreference(line); known {
			continue // Off resets all four preferences, regardless of ownership or value.
		}
		lines = append(lines, line)
	}
	return []byte(strings.Join(lines, "\n"))
}

func legacyFirefoxSnapshotExists(profile string) (bool, error) {
	path := filepath.Join(profile, legacyFirefoxStateName)
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if !info.Mode().IsRegular() {
		return false, i18n.Errorf("WebRTC settings path is not a regular file: %s", path)
	}
	return true, nil
}

func firefoxProfileNeedsReset(profile string) (bool, error) {
	needed, err := legacyFirefoxSnapshotExists(profile)
	if err != nil {
		return false, err
	}
	for _, name := range []string{"prefs.js", "user.js"} {
		content, err := readFirefoxFile(profile, name)
		if err != nil {
			return false, err
		}
		needed = needed || !bytes.Equal(content, resetFirefoxPreferences(content))
	}
	return needed, nil
}

func revertFirefoxProfile(profile string) error {
	needed, err := firefoxProfileNeedsReset(profile)
	if err != nil || !needed {
		return err
	}
	unlock, err := lockFirefoxProfile(profile)
	if err != nil {
		return err
	}
	defer unlock()
	// Read again under the browser's lock. Reset prefs.js first so failure leaves
	// user.js and any obsolete snapshot available for retry.
	for _, name := range []string{"prefs.js", "user.js"} {
		content, err := readFirefoxFile(profile, name)
		if err != nil {
			return err
		}
		data := resetFirefoxPreferences(content)
		if bytes.Equal(data, content) {
			continue
		}
		path := filepath.Join(profile, name)
		if name == "user.js" && len(bytes.TrimSpace(data)) == 0 {
			err = os.Remove(path)
		} else {
			err = writeFirefoxFile(path, data)
		}
		if err != nil {
			return err
		}
	}
	legacy, err := legacyFirefoxSnapshotExists(profile)
	if err != nil {
		return err
	}
	if legacy {
		return os.Remove(filepath.Join(profile, legacyFirefoxStateName))
	}
	return nil
}
