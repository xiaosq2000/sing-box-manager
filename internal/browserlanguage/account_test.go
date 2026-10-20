//go:build !windows

package browserlanguage

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"strings"
	"testing"
)

const accountPreferences = `{"intl":{"accept_languages":"de-DE,de","selected_languages":"de-DE,de"},"account_values":{"intl":{"accept_languages":"fr-FR,fr","selected_languages":"fr-FR,fr","other":9007199254740993},"unrelated":{"enabled":true}},"unrelated":42}`

func TestAccountLanguageFieldsRoundTrip(t *testing.T) {
	for _, original := range []string{
		accountPreferences,
		`{"account_values":{"intl":{"selected_languages":"fr-FR,fr"},"other":42}}`,
		`{"account_values":{"intl":{"accept_languages":"fr-FR,fr"}}}`,
		`{"account_values":{"intl":{}}}`,
		`{"account_values":{"unrelated":9007199254740993}}`,
		`{"account_values":{}}`,
	} {
		t.Run(original, func(t *testing.T) {
			m := fixture(t)
			path := profile(t, m, "google-chrome/Default", original)
			before, err := readPreferences(path)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := m.Enable(english); err != nil {
				t.Fatal(err)
			}
			after, _ := readPreferences(path)
			count := 0
			for _, key := range fields {
				if _, exists := before.accountIntl[key]; exists {
					count++
					if !matchesText(after.accountIntl[key], "en-US,en") {
						t.Fatal("existing account language was not replaced")
					}
				} else if _, exists := after.accountIntl[key]; exists {
					t.Fatal("created a missing account language field")
				}
			}
			state, err := m.load()
			if err != nil {
				t.Fatal(err)
			}
			record := state.Records["google-chrome/Default"]
			if count == 0 {
				if record.Account != nil || state.Version != 1 {
					t.Fatal("adopted an account store without language fields")
				}
			} else if record.Account == nil || len(record.Account.Original) != count || state.Version != 2 {
				t.Fatal("account recovery fields or schema version are missing")
			}
			if bytes.Contains(contents(t, m.StateFile), []byte("unrelated")) || bytes.Contains(contents(t, m.StateFile), []byte("9007199254740993")) {
				t.Fatal("account recovery copied unrelated data")
			}
			status, err := m.Status()
			if err != nil || status.Current != 1 || status.Pending != 0 {
				t.Fatalf("status: %+v, %v", status, err)
			}
			if _, err := m.Enable(german); err != nil {
				t.Fatal(err)
			}
			if _, err := m.Disable(); err != nil {
				t.Fatal(err)
			}
			jsonEqual(t, contents(t, path), original)
		})
	}
}

func TestLocalOnlyRecordAdoptsAccountFieldsWithoutReplacingOriginals(t *testing.T) {
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", accountPreferences)
	prefs, _ := readPreferences(path)
	original := map[string]json.RawMessage{}
	for _, key := range fields {
		original[key] = prefs.intl[key]
		prefs.intl[key] = json.RawMessage(`"en-US,en"`)
	}
	// Reproduce a version-1 client that changed local fields but ignored the
	// existing account overlay. Its saved originals must survive migration.
	state := State{Enabled: true, Target: &english, Records: map[string]Record{
		"google-chrome/Default": {Original: original, HadIntl: true, Written: "en-US,en"},
	}}
	if err := m.save(state); err != nil {
		t.Fatal(err)
	}
	root, _, _ := m.profilePath("google-chrome/Default")
	if err := m.writePreferences(root, path, prefs, false); err != nil {
		t.Fatal(err)
	}
	status, err := m.Status()
	if err != nil || status.Current != 0 || status.Pending != 1 {
		t.Fatalf("unmanaged account overlay reported current: %+v, %v", status, err)
	}
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	state, err = m.load()
	if err != nil || state.Version != 2 || !reflect.DeepEqual(state.Records["google-chrome/Default"].Original, original) {
		t.Fatalf("migration lost local originals: %+v, %v", state, err)
	}
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, contents(t, path), accountPreferences)
}

func TestAccountExternalChangesArePreservedPerField(t *testing.T) {
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", accountPreferences)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	prefs, _ := readPreferences(path)
	prefs.accountIntl["accept_languages"] = json.RawMessage(`"it-IT,it"`)
	prefs.accountDirty = true
	root, _, _ := m.profilePath("google-chrome/Default")
	if err := m.writePreferences(root, path, prefs, false); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Enable(german); err == nil || !strings.Contains(err.Error(), "outside sbc") {
		t.Fatalf("overwrote external account languages: %v", err)
	}
	result, err := m.Disable()
	if err != nil || len(result.Preserved) != 1 {
		t.Fatalf("preserved: %+v, %v", result, err)
	}
	expected := strings.Replace(accountPreferences, `"account_values":{"intl":{"accept_languages":"fr-FR,fr"`, `"account_values":{"intl":{"accept_languages":"it-IT,it"`, 1)
	jsonEqual(t, contents(t, path), expected)
}

func TestNewAccountFieldsNeedExplicitApplication(t *testing.T) {
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", `{}`)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	current := `{"intl":{"accept_languages":"en-US,en","selected_languages":"en-US,en"},"account_values":{"intl":{"selected_languages":"fr-FR,fr"},"other":42}}`
	if err := atomicWrite(path, []byte(current), 0o600); err != nil {
		t.Fatal(err)
	}
	status, err := m.Status()
	if err != nil || status.Current != 0 || status.Pending != 1 {
		t.Fatalf("new account field was not pending: %+v, %v", status, err)
	}
	jsonEqual(t, contents(t, path), current)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, contents(t, path), `{"account_values":{"intl":{"selected_languages":"fr-FR,fr"},"other":42}}`)
}

func TestAdditionalAccountFieldKeepsExistingOriginals(t *testing.T) {
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", `{"account_values":{"intl":{"selected_languages":"fr-FR,fr"}}}`)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	current := `{"intl":{"accept_languages":"en-US,en","selected_languages":"en-US,en"},"account_values":{"intl":{"selected_languages":"en-US,en","accept_languages":"it-IT,it"}}}`
	if err := atomicWrite(path, []byte(current), 0o600); err != nil {
		t.Fatal(err)
	}
	status, err := m.Status()
	if err != nil || status.Current != 0 || status.Pending != 1 {
		t.Fatalf("additional account field was not pending: %+v, %v", status, err)
	}
	if changed, err := m.NoteTarget(&german, false); err != nil || !changed {
		t.Fatalf("deferred account target: %v, %v", changed, err)
	}
	jsonEqual(t, contents(t, path), current)
	if _, err := m.Enable(german); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, contents(t, path), `{"account_values":{"intl":{"selected_languages":"fr-FR,fr","accept_languages":"it-IT,it"}}}`)
}

func TestFailedAccountCleanupRetainsBothOriginals(t *testing.T) {
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", accountPreferences)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	m.Write = func(file string, data []byte, mode os.FileMode) error {
		if file == path {
			return errors.New("fixture account cleanup failure")
		}
		return atomicWrite(file, data, mode)
	}
	if _, err := m.Disable(); err == nil {
		t.Fatal("ignored account cleanup failure")
	}
	status, err := m.Status()
	if err != nil || status.State.Enabled || status.Pending != 1 || status.State.Version != 2 || status.State.Records["google-chrome/Default"].Account == nil {
		t.Fatalf("lost pending account recovery: %+v, %v", status, err)
	}
	if _, err := m.Enable(german); err == nil {
		t.Fatal("discarded account recovery during re-enable")
	}
	m.Write = nil
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, contents(t, path), accountPreferences)
}

func TestRemovedAccountDataIsNeverRecreated(t *testing.T) {
	for _, account := range []string{"", `,"account_values":{}`, `,"account_values":{"intl":{"other":42}}`} {
		m := fixture(t)
		path := profile(t, m, "google-chrome/Default", accountPreferences)
		if _, err := m.Enable(english); err != nil {
			t.Fatal(err)
		}
		current := `{"intl":{"accept_languages":"en-US,en","selected_languages":"en-US,en"}` + account + `}`
		if err := atomicWrite(path, []byte(current), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := m.Enable(english); err != nil {
			t.Fatal(err)
		}
		jsonEqual(t, contents(t, path), current)
		if _, err := m.Disable(); err != nil {
			t.Fatal(err)
		}
		jsonEqual(t, contents(t, path), `{"intl":{"accept_languages":"de-DE,de","selected_languages":"de-DE,de"}`+account+`}`)
	}
}

func TestAccountInterruptedUpdateRestoresBothStores(t *testing.T) {
	for _, afterWrite := range []bool{false, true} {
		m := fixture(t)
		path := profile(t, m, "google-chrome/Default", accountPreferences)
		if _, err := m.Enable(english); err != nil {
			t.Fatal(err)
		}
		written := false
		m.Write = func(file string, data []byte, mode os.FileMode) error {
			if file == path {
				if !afterWrite {
					return errors.New("fixture account preference failure")
				}
				written = true
			} else if written && file == m.StateFile {
				return errors.New("fixture account journal failure")
			}
			return atomicWrite(file, data, mode)
		}
		if _, err := m.Enable(german); err == nil {
			t.Fatal("ignored interrupted account write")
		}
		m.Write = nil
		if _, err := m.Disable(); err != nil {
			t.Fatal(err)
		}
		jsonEqual(t, contents(t, path), accountPreferences)
	}
}

func TestInvalidAccountPreferencesAndRecoveryStayUntouched(t *testing.T) {
	for _, account := range []string{`null`, `[]`, `{"intl":null}`, `{"intl":{},"intl":{}}`, `{"intl":{"selected_languages":null}}`, `{"intl":{"selected_languages":"fr","selected_languages":"it"}}`, `{"intl":{"forced_languages":["fr"]}}`} {
		m := fixture(t)
		original := `{"account_values":` + account + `}`
		path := profile(t, m, "google-chrome/Default", original)
		if _, err := m.Enable(english); err == nil {
			t.Fatalf("accepted invalid account preferences: %s", account)
		}
		if string(contents(t, path)) != original {
			t.Fatal("invalid account preferences were overwritten")
		}
	}
	for _, extra := range []string{
		`null`,
		`{"original":null}`,
		`{"original":{},"original":{"selected_languages":"fr"}}`,
		`{"original":{}}`,
		`{"original":{"selected_languages":null}}`,
		`{"original":{"selected_languages":"fr","selected_languages":"it"}}`,
		`{"original":{"unrelated":"fixture"}}`,
		`{"original":{"selected_languages":"fr"},"identity":"fixture"}`,
	} {
		m := fixture(t)
		state := `{"version":2,"profiles":{"google-chrome/Default":{"original":{},"had_intl":false,"written":"en-US,en","account_values":` + extra + `}}}`
		if err := atomicWrite(m.StateFile, []byte(state), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := m.Disable(); err == nil {
			t.Fatalf("accepted invalid account recovery: %s", extra)
		}
		if string(contents(t, m.StateFile)) != state {
			t.Fatal("invalid account recovery was discarded")
		}
	}
}
