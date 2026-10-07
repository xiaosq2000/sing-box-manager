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

type firefoxSaved struct {
	Version  int                 `json:"version"`
	Original map[string][]string `json:"original"`
}

func readFirefoxSaved(profile string) (*firefoxSaved, error) {
	data, err := privateFile(filepath.Join(profile, firefoxStateName))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var saved firefoxSaved
	if json.Unmarshal(data, &saved) != nil || saved.Version != 1 || len(saved.Original) != len(firefoxPrefs) {
		return nil, i18n.New("the WebRTC policy ownership record is invalid; cleanup cannot safely continue")
	}
	for _, pref := range firefoxPrefs {
		name, _, _ := firefoxPreference(pref)
		lines, ok := saved.Original[name]
		if !ok {
			return nil, i18n.New("the WebRTC policy ownership record is invalid; cleanup cannot safely continue")
		}
		for _, line := range lines {
			key, _, valid := firefoxPreference(line)
			if !valid || key != name || strings.ContainsAny(line, "\r\n") {
				return nil, i18n.New("the WebRTC policy ownership record is invalid; cleanup cannot safely continue")
			}
		}
	}
	return &saved, nil
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
		return nil // Legacy ownership remains usable for cleanup.
	}
	for _, pref := range firefoxPrefs {
		name, _, _ := firefoxPreference(pref)
		if strings.Contains(text, name) {
			return nil // Never override a user.js preference from another source.
		}
	}
	saved, err := readFirefoxSaved(profile)
	if err != nil {
		return err
	}
	if saved == nil {
		prefs, err := readFirefoxFile(profile, "prefs.js")
		if err != nil {
			return err
		}
		saved = &firefoxSaved{Version: 1, Original: map[string][]string{}}
		for _, pref := range firefoxPrefs {
			name, _, _ := firefoxPreference(pref)
			saved.Original[name] = nil
		}
		for _, line := range strings.Split(string(prefs), "\n") {
			if name, _, ok := firefoxPreference(line); ok {
				saved.Original[name] = append(saved.Original[name], strings.TrimSuffix(line, "\r"))
			}
		}
		data, err := json.Marshal(saved)
		if err != nil {
			return err
		}
		// Persist the original values before user.js can change prefs.js.
		if err := writeFirefoxUserJS(filepath.Join(profile, firefoxStateName), data); err != nil {
			return err
		}
	}
	if len(text) > 0 && !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
	text += firefoxMarker + "\n" + strings.Join(firefoxPrefs, "\n") + "\n"
	return writeFirefoxUserJS(filepath.Join(profile, "user.js"), []byte(text))
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

func revertFirefoxProfile(profile string) error {
	content, err := readFirefoxFile(profile, "user.js")
	if err != nil {
		return err
	}
	saved, err := readFirefoxSaved(profile)
	if err != nil {
		return err
	}
	text := string(content)
	if !strings.Contains(text, firefoxMarker) && saved == nil {
		return nil
	}
	unlock, err := lockFirefoxProfile(profile)
	if err != nil {
		return err
	}
	defer unlock()
	remaining, owned := stripFirefoxManaged(text)
	if saved != nil && !strings.Contains(text, firefoxMarker) {
		// Retry after user.js removal or interrupted setup.
		for name := range saved.Original {
			owned[name] = true
		}
	}
	// Conservatively preserve preferences mentioned outside the managed block,
	// including custom JavaScript syntax which our saved-value parser ignores.
	for name := range owned {
		if strings.Contains(remaining, name) {
			delete(owned, name)
		}
	}
	prefs, err := readFirefoxFile(profile, "prefs.js")
	if err != nil {
		return err // Keep the managed block and snapshot available for retry.
	}
	lines := strings.Split(string(prefs), "\n")
	for _, line := range lines {
		if name, value, ok := firefoxPreference(line); ok && value != "true" {
			delete(owned, name) // Preserve externally changed saved values.
		}
	}
	var restored []string
	restoredNames := map[string]bool{}
	for _, line := range lines {
		name, value, ok := firefoxPreference(line)
		if ok && owned[name] && value == "true" {
			if saved != nil && !restoredNames[name] {
				restored = append(restored, saved.Original[name]...)
				restoredNames[name] = true
			}
			continue // Legacy blocks have no snapshot: reset to browser defaults.
		}
		restored = append(restored, line)
	}
	data := []byte(strings.Join(restored, "\n"))
	if !bytes.Equal(data, prefs) {
		if err := writeFirefoxUserJS(filepath.Join(profile, "prefs.js"), data); err != nil {
			return err
		}
	}
	if remaining != text {
		path := filepath.Join(profile, "user.js")
		if strings.TrimSpace(remaining) == "" {
			err = os.Remove(path)
		} else {
			err = writeFirefoxUserJS(path, []byte(remaining))
		}
		if err != nil {
			return err
		}
	}
	if saved != nil {
		return os.Remove(filepath.Join(profile, firefoxStateName))
	}
	return nil
}
