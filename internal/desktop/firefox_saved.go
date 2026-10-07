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

const firefoxStateName = ".sbc-webrtc.json"

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

type firefoxSnapshot struct {
	Version  int                 `json:"version"`
	Original map[string][]string `json:"original"`
}

func (snapshot *firefoxSnapshot) valid() bool {
	if snapshot.Version != 1 || len(snapshot.Original) != len(firefoxPrefs) {
		return false
	}
	for _, pref := range firefoxPrefs {
		name, _, _ := firefoxPreference(pref)
		lines, present := snapshot.Original[name]
		if !present {
			return false
		}
		for _, line := range lines {
			key, _, valid := firefoxPreference(line)
			if !valid || key != name || strings.ContainsAny(line, "\r\n") {
				return false
			}
		}
	}
	return true
}

func readFirefoxSnapshot(profile string) (*firefoxSnapshot, error) {
	data, err := privateFile(filepath.Join(profile, firefoxStateName))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var snapshot firefoxSnapshot
	if json.Unmarshal(data, &snapshot) != nil || !snapshot.valid() {
		return nil, i18n.New("the WebRTC policy ownership record is invalid; cleanup cannot safely continue")
	}
	return &snapshot, nil
}

func readFirefoxFile(profile, name string) ([]byte, error) {
	data, err := privateFile(filepath.Join(profile, name))
	if os.IsNotExist(err) {
		return nil, nil
	}
	return data, err
}

func saveFirefoxSnapshot(profile string) error {
	snapshot, err := readFirefoxSnapshot(profile)
	if err != nil {
		return err
	}
	if snapshot != nil {
		return nil // Keep the original values after interrupted setup.
	}
	prefs, err := readFirefoxFile(profile, "prefs.js")
	if err != nil {
		return err
	}
	snapshot = &firefoxSnapshot{Version: 1, Original: map[string][]string{}}
	for _, pref := range firefoxPrefs {
		name, _, _ := firefoxPreference(pref)
		snapshot.Original[name] = nil
	}
	for _, line := range strings.Split(string(prefs), "\n") {
		if name, _, ok := firefoxPreference(line); ok {
			snapshot.Original[name] = append(snapshot.Original[name], strings.TrimSuffix(line, "\r"))
		}
	}
	data, err := json.Marshal(snapshot)
	if err != nil {
		return err
	}
	return writeFirefoxFile(filepath.Join(profile, firefoxStateName), data)
}

func applyFirefoxProfile(profile string) error {
	content, err := readFirefoxFile(profile, "user.js")
	if err != nil {
		return err
	}
	text := string(content)
	if strings.Contains(text, firefoxMarker) {
		return nil // Legacy ownership remains usable for cleanup.
	}
	for _, pref := range firefoxPrefs {
		name, _, _ := firefoxPreference(pref)
		if strings.Contains(text, name) {
			return nil // Never override a user.js preference from another source.
		}
	}
	// Persist the original values before user.js can change prefs.js.
	if err := saveFirefoxSnapshot(profile); err != nil {
		return err
	}
	if len(text) > 0 && !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
	text += firefoxMarker + "\n" + strings.Join(firefoxPrefs, "\n") + "\n"
	return writeFirefoxFile(filepath.Join(profile, "user.js"), []byte(text))
}

func stripFirefoxManaged(text string) (string, map[string]bool) {
	owned := map[string]bool{}
	var lines []string
	inBlock := false
	for _, line := range strings.Split(text, "\n") {
		if strings.TrimSpace(line) == firefoxMarker {
			inBlock = true
			continue
		}
		if name, value, ok := firefoxPreference(line); inBlock && ok && value == "true" {
			owned[name] = true
			continue
		}
		inBlock = false
		lines = append(lines, line)
	}
	return strings.Join(lines, "\n"), owned
}

func restoreFirefoxPreferences(prefs []byte, userJS string, owned map[string]bool, snapshot *firefoxSnapshot) []byte {
	replacements := map[string][]string{}
	for name, managed := range owned {
		// Also preserve custom JavaScript syntax outside the managed block.
		if !managed || strings.Contains(userJS, name) {
			continue
		}
		replacements[name] = nil // Legacy blocks reset to browser defaults.
		if snapshot != nil {
			replacements[name] = snapshot.Original[name]
		}
	}
	lines := strings.Split(string(prefs), "\n")
	for _, line := range lines {
		if name, value, ok := firefoxPreference(line); ok && value != "true" {
			delete(replacements, name) // Preserve externally changed saved values.
		}
	}
	var restored []string
	for _, line := range lines {
		name, value, valid := firefoxPreference(line)
		original, replace := replacements[name]
		if valid && replace && value == "true" {
			restored = append(restored, original...)
			// Restore once, but still remove any duplicate managed entries.
			replacements[name] = nil
			continue
		}
		restored = append(restored, line)
	}
	return []byte(strings.Join(restored, "\n"))
}

func revertFirefoxProfile(profile string) error {
	content, err := readFirefoxFile(profile, "user.js")
	if err != nil {
		return err
	}
	snapshot, err := readFirefoxSnapshot(profile)
	if err != nil {
		return err
	}
	text := string(content)
	managed := strings.Contains(text, firefoxMarker)
	if !managed && snapshot == nil {
		return nil
	}
	unlock, err := lockFirefoxProfile(profile)
	if err != nil {
		return err
	}
	defer unlock()
	remaining, owned := stripFirefoxManaged(text)
	if !managed && snapshot != nil {
		// Retry after user.js removal or interrupted setup.
		for name := range snapshot.Original {
			owned[name] = true
		}
	}
	prefs, err := readFirefoxFile(profile, "prefs.js")
	if err != nil {
		return err // Keep the managed block and snapshot available for retry.
	}
	data := restoreFirefoxPreferences(prefs, remaining, owned, snapshot)
	if !bytes.Equal(data, prefs) {
		if err := writeFirefoxFile(filepath.Join(profile, "prefs.js"), data); err != nil {
			return err
		}
	}
	if remaining != text {
		path := filepath.Join(profile, "user.js")
		if strings.TrimSpace(remaining) == "" {
			err = os.Remove(path)
		} else {
			err = writeFirefoxFile(path, []byte(remaining))
		}
		if err != nil {
			return err
		}
	}
	if snapshot != nil {
		return os.Remove(filepath.Join(profile, firefoxStateName))
	}
	return nil
}
