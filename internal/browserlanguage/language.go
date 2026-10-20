// Package browserlanguage manages the saved language preferences of Linux Chrome.
package browserlanguage

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/region"
)

var channels = []string{"google-chrome", "google-chrome-beta", "google-chrome-unstable", "google-chrome-canary"}
var profileName = regexp.MustCompile(`^(Default|Profile [0-9]+)$`)
var fields = []string{"accept_languages", "selected_languages"}

// Target is the node language, independent of the system region mode.
type Target struct {
	Node     string `json:"node"`
	Language string `json:"language"`
}

// Record saves only language fields, not a copy of the personal profile.
// Previous protects ownership if a target update stops before its file write.
type Record struct {
	Original map[string]json.RawMessage `json:"original"`
	HadIntl  bool                       `json:"had_intl"`
	Written  string                     `json:"written"`
	Previous string                     `json:"previous,omitempty"`
}

// State is a write-ahead recovery record. Its absence means disabled.
type State struct {
	Version int               `json:"version"`
	Enabled bool              `json:"enabled"`
	Target  *Target           `json:"target,omitempty"`
	Records map[string]Record `json:"profiles"`
}

// Status distinguishes the saved choice from pending application or cleanup.
type Status struct {
	State      State
	Current    int
	Pending    int
	NoProfiles bool
}

// Result reports profile operations without exposing their language values.
type Result struct {
	Changed   []string
	Preserved []string
}

// Manager writes only standard personal profiles below ConfigHome. Tests use
// temporary paths, including PolicyDirs, and can replace Write for fault checks.
type Manager struct {
	StateFile  string
	ConfigHome string
	PolicyDirs []string
	Write      func(string, []byte, os.FileMode) error
}

// New does not read or change any file.
func New(stateFile, configHome string) *Manager {
	return &Manager{
		StateFile: stateFile, ConfigHome: configHome,
		PolicyDirs: []string{"/etc/opt/chrome/policies/managed", "/etc/opt/chrome/policies/recommended"},
	}
}

// Languages returns Chrome's ordered, comma-separated preference value.
func Languages(language string) (string, error) {
	locale, err := region.Locale(language)
	if err != nil {
		return "", err
	}
	locale = strings.TrimSuffix(locale, ".UTF-8")
	language = strings.ReplaceAll(locale, "_", "-")
	base, _, _ := strings.Cut(language, "-")
	return language + "," + base, nil
}

func (m *Manager) load() (State, error) {
	state := State{Version: 1, Records: map[string]Record{}}
	if _, err := os.Lstat(m.StateFile); os.IsNotExist(err) {
		return state, nil
	} else if err != nil {
		return state, err
	}
	data, _, err := readFile(m.StateFile, 4<<20)
	if os.IsNotExist(err) {
		return state, nil
	}
	if err != nil {
		return state, err
	}
	if err := ownedFile(m.StateFile); err != nil {
		return state, err
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if !validRecordJSON(data) || decoder.Decode(&state) != nil || decoder.Decode(new(any)) != io.EOF || state.Version != 1 || state.Records == nil {
		return state, invalidRecord()
	}
	if state.Target != nil {
		if _, err := Languages(state.Target.Language); err != nil {
			return state, err
		}
	}
	for id, record := range state.Records {
		if _, _, err := m.profilePath(id); err != nil {
			return state, err
		}
		if record.Original == nil || (!record.HadIntl && len(record.Original) != 0) || !validWritten(record.Written) || (record.Previous != "" && !validWritten(record.Previous)) {
			return state, invalidRecord()
		}
		for key, value := range record.Original {
			_, isString := stringValue(value)
			if (key != fields[0] && key != fields[1]) || !isString {
				return state, invalidRecord()
			}
		}
	}
	return state, nil
}

// Typed decoding rejects unknown fields. This also rejects duplicate keys in
// every recovery object, so a damaged record cannot silently replace originals.
func validRecordJSON(data []byte) bool {
	root, err := object(data)
	if err != nil {
		return false
	}
	if value, ok := root["target"]; ok && !bytes.Equal(value, []byte("null")) {
		if _, err := object(value); err != nil {
			return false
		}
	}
	records, err := object(root["profiles"])
	if err != nil {
		return false
	}
	for _, value := range records {
		record, err := object(value)
		if err != nil {
			return false
		}
		if _, err := object(record["original"]); err != nil {
			return false
		}
	}
	return true
}

func invalidRecord() error {
	return i18n.New("the Chrome language recovery record is invalid. Keep it and restore the language manually")
}

func validWritten(value string) bool {
	language, _, ok := strings.Cut(value, ",")
	want, err := Languages(language)
	return ok && err == nil && value == want
}

func (m *Manager) save(state State) error {
	data, err := json.Marshal(state)
	if err != nil {
		return err
	}
	return m.write(m.StateFile, append(data, '\n'), 0o600)
}

// NoteTarget changes only the recovery record, never Chrome preferences.
// Missing metadata in a routine refresh must not erase the last known target.
func (m *Manager) NoteTarget(target *Target, dropMissing bool) (bool, error) {
	if _, err := os.Lstat(m.StateFile); os.IsNotExist(err) {
		return false, nil
	} else if err != nil {
		return false, err
	}
	unlock, err := lock(m.StateFile + ".lock")
	if err != nil {
		return false, err
	}
	defer unlock()
	state, err := m.load()
	if err != nil || (!state.Enabled && len(state.Records) == 0) {
		return false, err
	}
	if target == nil && !dropMissing {
		return false, nil
	}
	if target != nil {
		if _, err := Languages(target.Language); err != nil {
			return false, err
		}
	}
	changed := (state.Target == nil) != (target == nil)
	if target != nil && state.Target != nil {
		changed = *target != *state.Target
	}
	if !changed {
		return false, nil
	}
	state.Target = target
	return true, m.save(state)
}

// Enable records intent before writing each profile. It never terminates Chrome.
func (m *Manager) Enable(target Target) (Result, error) {
	var result Result
	want, err := Languages(target.Language)
	if err != nil {
		return result, err
	}
	unlock, err := lock(m.StateFile + ".lock")
	if err != nil {
		return result, err
	}
	defer unlock()
	state, err := m.load()
	if err != nil {
		return result, err
	}
	// Do not replace a recovery record until pending cleanup finishes.
	if !state.Enabled && len(state.Records) != 0 {
		return result, i18n.New("Chrome language cleanup is pending. Run 'sbc privacy browser language off' first")
	}
	state.Enabled, state.Target = true, &target
	if err := m.save(state); err != nil {
		return result, err
	}
	if err := m.checkPolicies(); err != nil {
		return result, err
	}
	ids, err := m.discover()
	if err != nil {
		return result, err
	}
	if len(ids) == 0 {
		return result, i18n.New("sbc found no standard Chrome personal profile. Start Chrome once. Exit Chrome and retry")
	}
	var failures []error
	for _, id := range ids {
		if err := m.applyProfile(&state, id, want); err != nil {
			failures = append(failures, fmt.Errorf("%s: %w", id, err))
		} else {
			result.Changed = append(result.Changed, id)
		}
	}
	return result, errors.Join(failures...)
}

func (m *Manager) applyProfile(state *State, id, want string) error {
	root, path, err := m.profilePath(id)
	if err != nil {
		return err
	}
	if err := closed(root); err != nil {
		return err
	}
	prefs, err := readPreferences(path)
	if err != nil {
		return err
	}
	if hasForcedLanguages(prefs.intl) {
		return policyConflict()
	}
	record, owned := state.Records[id]
	if owned {
		for _, key := range fields {
			if !matchesWritten(prefs.intl[key], record) && !equalValue(prefs.intl[key], record.Original[key]) {
				return i18n.New("Chrome languages changed outside sbc. Run 'sbc privacy browser language off' before applying a new target")
			}
		}
	} else {
		record = Record{Original: map[string]json.RawMessage{}, HadIntl: prefs.hadIntl}
		for _, key := range fields {
			if value, ok := prefs.intl[key]; ok {
				if _, isString := stringValue(value); !isString {
					return i18n.New("the Chrome language preference is not a string. sbc changed nothing")
				}
				record.Original[key] = value
			}
		}
	}
	if record.Previous != "" && record.Written != want {
		return i18n.New("a Chrome language change is incomplete. Run 'sbc privacy browser language off' before applying another target")
	}
	if record.Written != want {
		record.Previous, record.Written = record.Written, want
	}
	state.Records[id] = record
	// Keep originals and both ownership values durable before the profile write.
	if err := m.save(*state); err != nil {
		return err
	}
	value, _ := json.Marshal(want)
	for _, key := range fields {
		prefs.intl[key] = value
	}
	if err := m.writePreferences(root, path, prefs, false); err != nil {
		return err
	}
	record.Previous = ""
	state.Records[id] = record
	return m.save(*state)
}

// Disable records the opt-out before restoration. Failed restores retain their
// records so uninstall can stop without losing recovery data.
func (m *Manager) Disable() (Result, error) {
	var result Result
	// Absence already means off. Do not create state, locks or directories for
	// users who never enabled this component, including during uninstall.
	if _, err := os.Lstat(m.StateFile); os.IsNotExist(err) {
		return result, nil
	} else if err != nil {
		return result, err
	}
	unlock, err := lock(m.StateFile + ".lock")
	if err != nil {
		return result, err
	}
	defer unlock()
	state, err := m.load()
	if err != nil {
		return result, err
	}
	state.Enabled = false
	if err := m.save(state); err != nil {
		return result, err
	}
	var failures []error
	for _, id := range recordIDs(state) {
		preserved, err := m.restoreProfile(id, state.Records[id])
		if err != nil {
			failures = append(failures, fmt.Errorf("%s: %w", id, err))
			continue
		}
		delete(state.Records, id)
		if err := m.save(state); err != nil {
			return result, errors.Join(append(failures, err)...)
		}
		result.Changed = append(result.Changed, id)
		if preserved {
			result.Preserved = append(result.Preserved, id)
		}
	}
	return result, errors.Join(failures...)
}

func (m *Manager) restoreProfile(id string, record Record) (bool, error) {
	root, path, err := m.profilePath(id)
	if err != nil {
		return false, err
	}
	// Deleted profiles need no restoration. Never recreate their directories.
	if _, err := os.Lstat(path); os.IsNotExist(err) {
		return false, nil
	}
	if err := closed(root); err != nil {
		return false, err
	}
	prefs, err := readPreferences(path)
	if err != nil {
		return false, err
	}
	preserved, changed := false, false
	for _, key := range fields {
		value := prefs.intl[key]
		if equalValue(value, record.Original[key]) {
			continue
		}
		if !matchesWritten(value, record) {
			preserved = true
			continue
		}
		changed = true
		if original, exists := record.Original[key]; exists {
			prefs.intl[key] = original
		} else {
			delete(prefs.intl, key)
		}
	}
	if !changed {
		return preserved, nil
	}
	return preserved, m.writePreferences(root, path, prefs, !record.HadIntl && len(prefs.intl) == 0)
}

// Status reads only. In particular, it creates no lock file or directory.
func (m *Manager) Status() (Status, error) {
	state, err := m.load()
	status := Status{State: state}
	if err != nil {
		return status, err
	}
	if !state.Enabled {
		status.Pending = len(state.Records)
		return status, nil
	}
	ids, err := m.discover()
	if err != nil {
		return status, err
	}
	seen := map[string]bool{}
	for _, id := range ids {
		seen[id] = true
		_, path, _ := m.profilePath(id)
		prefs, readErr := readPreferences(path)
		want := ""
		if state.Target != nil {
			want, _ = Languages(state.Target.Language)
		}
		record, owned := state.Records[id]
		current := readErr == nil && owned && want != "" && record.Written == want && record.Previous == "" && !hasForcedLanguages(prefs.intl)
		for _, key := range fields {
			current = current && matchesText(prefs.intl[key], want)
		}
		if current {
			status.Current++
		} else {
			status.Pending++
		}
	}
	for id := range state.Records {
		if !seen[id] {
			status.Pending++
		}
	}
	status.NoProfiles = len(ids) == 0
	if err := m.checkPolicies(); err != nil {
		return status, err
	}
	return status, nil
}

func (m *Manager) profilePath(id string) (string, string, error) {
	channel, profile, ok := strings.Cut(id, "/")
	known := false
	for _, name := range channels {
		known = known || channel == name
	}
	if !known || !ok || !profileName.MatchString(profile) {
		return "", "", i18n.New("the Chrome language recovery record contains an unsafe profile path")
	}
	root := filepath.Join(m.ConfigHome, channel)
	return root, filepath.Join(root, profile, "Preferences"), nil
}

func (m *Manager) discover() ([]string, error) {
	var ids []string
	for _, channel := range channels {
		root := filepath.Join(m.ConfigHome, channel)
		if err := safePath(root); os.IsNotExist(err) {
			continue
		} else if err != nil {
			return nil, err
		}
		entries, err := os.ReadDir(root)
		if err != nil {
			return nil, err
		}
		for _, entry := range entries {
			if !profileName.MatchString(entry.Name()) {
				continue
			}
			path := filepath.Join(root, entry.Name(), "Preferences")
			if err := safePath(path); os.IsNotExist(err) {
				continue
			} else if err != nil {
				return nil, err
			}
			ids = append(ids, channel+"/"+entry.Name())
		}
	}
	sort.Strings(ids)
	return ids, nil
}

func (m *Manager) checkPolicies() error {
	for _, dir := range m.PolicyDirs {
		entries, err := os.ReadDir(dir)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		for _, entry := range entries {
			if !strings.HasSuffix(entry.Name(), ".json") {
				continue
			}
			data, _, err := readFile(filepath.Join(dir, entry.Name()), 4<<20)
			if err != nil {
				return err
			}
			values, err := object(data)
			if err != nil {
				return i18n.New("a Chrome policy file is invalid. Review browser policies before changing languages")
			}
			if _, exists := values["ForcedLanguages"]; exists {
				return policyConflict()
			}
		}
	}
	return nil
}

func policyConflict() error {
	return i18n.New("a Chrome language policy controls this profile. Review the policy before changing its languages")
}

func recordIDs(state State) []string {
	ids := make([]string, 0, len(state.Records))
	for id := range state.Records {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

func hasForcedLanguages(intl map[string]json.RawMessage) bool {
	value, exists := intl["forced_languages"]
	if !exists {
		return false
	}
	var languages []string
	return json.Unmarshal(value, &languages) != nil || languages == nil || len(languages) != 0
}

func stringValue(value json.RawMessage) (string, bool) {
	var text *string
	if json.Unmarshal(value, &text) != nil || text == nil {
		return "", false
	}
	return *text, true
}

func matchesText(value json.RawMessage, want string) bool {
	text, ok := stringValue(value)
	return ok && text == want
}

func matchesWritten(value json.RawMessage, record Record) bool {
	return matchesText(value, record.Written) || (record.Previous != "" && matchesText(value, record.Previous))
}

func equalValue(left, right json.RawMessage) bool {
	if len(left) == 0 || len(right) == 0 {
		return len(left) == len(right)
	}
	a, leftOK := stringValue(left)
	b, rightOK := stringValue(right)
	return leftOK && rightOK && a == b
}
