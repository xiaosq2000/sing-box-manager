//go:build !windows

package browserlanguage

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func fixture(t *testing.T) *Manager {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	m := New(filepath.Join(root, "sbc", "browser-language.json"), filepath.Join(root, "chrome"))
	m.PolicyDirs = []string{filepath.Join(root, "policies")}
	return m
}

func profile(t *testing.T, m *Manager, id, data string) string {
	t.Helper()
	_, path, err := m.profilePath(id)
	if err != nil {
		t.Fatal(err)
	}
	if err := atomicWrite(path, []byte(data), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func contents(t *testing.T, path string) []byte {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func jsonEqual(t *testing.T, got []byte, want string) {
	t.Helper()
	var a, b any
	decode := func(data []byte, target *any) error {
		d := json.NewDecoder(bytes.NewReader(data))
		d.UseNumber()
		return d.Decode(target)
	}
	if decode(got, &a) != nil || decode([]byte(want), &b) != nil || !reflect.DeepEqual(a, b) {
		t.Fatalf("JSON mismatch: %s != %s", got, want)
	}
}

var english = Target{Node: "node.example", Language: "en-US"}
var german = Target{Node: "node.example", Language: "de-DE"}

func TestLanguages(t *testing.T) {
	for input, want := range map[string]string{"en-US": "en-US,en", " zh-cn ": "zh-CN,zh", "de-DE": "de-DE,de", "es-419": "es-419,es"} {
		got, err := Languages(input)
		if err != nil || got != want {
			t.Fatalf("%s: %s, %v", input, got, err)
		}
	}
	for _, input := range []string{"en", "en_US", "../en-US", "en-US,zh-CN"} {
		if _, err := Languages(input); err == nil {
			t.Fatalf("accepted %q", input)
		}
	}
}

func TestEnableAndRestoreOnlyLanguageFields(t *testing.T) {
	for _, original := range []string{
		`{"unrelated":{"number":9007199254740993,"list":[true,null]},"intl":{"accept_languages":"zh-CN,zh","selected_languages":"zh-CN,zh","other":17}}`,
		`{"intl":{"accept_languages":"zh-CN"},"other":1}`,
		`{"intl":{},"other":1}`,
		`{"other":1}`,
	} {
		t.Run(original, func(t *testing.T) {
			m := fixture(t)
			path := profile(t, m, "google-chrome/Default", original)
			if _, err := m.Enable(english); err != nil {
				t.Fatal(err)
			}
			prefs, _ := readPreferences(path)
			for _, field := range fields {
				if !matchesText(prefs.intl[field], "en-US,en") {
					t.Fatal("language not replaced")
				}
			}
			if strings.Contains(string(contents(t, m.StateFile)), "unrelated") {
				t.Fatal("recovery record copied personal preferences")
			}
			for _, path := range []string{path, m.StateFile} {
				info, _ := os.Stat(path)
				if info.Mode().Perm() != 0o600 {
					t.Fatalf("permissions: %s %v", path, info.Mode())
				}
			}
			status, err := m.Status()
			if err != nil || status.Current != 1 || status.Pending != 0 {
				t.Fatalf("status: %+v, %v", status, err)
			}
			if _, err := m.Enable(english); err != nil {
				t.Fatal(err)
			}
			if _, err := m.Disable(); err != nil {
				t.Fatal(err)
			}
			jsonEqual(t, contents(t, path), original)
		})
	}
}

func TestStandardProfilesAndNewProfiles(t *testing.T) {
	m := fixture(t)
	for _, id := range []string{"google-chrome/Default", "google-chrome/Profile 2", "google-chrome-beta/Default", "google-chrome-unstable/Profile 1"} {
		profile(t, m, id, `{}`)
	}
	for _, name := range []string{"Guest Profile", "System Profile", "Custom", "Profile xyz"} {
		path := filepath.Join(m.ConfigHome, "google-chrome", name, "Preferences")
		if err := atomicWrite(path, []byte(`{"untouched":true}`), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	result, err := m.Enable(english)
	if err != nil || len(result.Changed) != 4 {
		t.Fatalf("result: %+v, %v", result, err)
	}
	newPath := profile(t, m, "google-chrome/Profile 3", `{}`)
	status, err := m.Status()
	if err != nil || status.Current != 4 || status.Pending != 1 {
		t.Fatalf("status: %+v, %v", status, err)
	}
	jsonEqual(t, contents(t, newPath), `{}`)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, contents(t, newPath), `{}`)
}

func TestStatusAndDisabledRefreshChangeNothing(t *testing.T) {
	m := fixture(t)
	status, err := m.Status()
	if err != nil || status.State.Enabled {
		t.Fatalf("status: %+v, %v", status, err)
	}
	if changed, err := m.NoteTarget(&english, true); err != nil || changed {
		t.Fatalf("disabled refresh: %v, %v", changed, err)
	}
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Dir(m.StateFile)); !os.IsNotExist(err) {
		t.Fatal("query, disabled refresh or absent-state cleanup created state")
	}
}

func TestRefreshDefersApplicationAndKeepsOriginals(t *testing.T) {
	m := fixture(t)
	original := `{"intl":{"selected_languages":"zh-CN,zh","accept_languages":"zh-CN,zh"}}`
	path := profile(t, m, "google-chrome/Default", original)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	before := contents(t, path)
	if changed, err := m.NoteTarget(&german, false); err != nil || !changed {
		t.Fatalf("note target: %v, %v", changed, err)
	}
	if !bytes.Equal(before, contents(t, path)) {
		t.Fatal("refresh wrote Chrome preferences")
	}
	status, err := m.Status()
	if err != nil || status.Current != 0 || status.Pending != 1 {
		t.Fatalf("status: %+v, %v", status, err)
	}
	if changed, err := m.NoteTarget(nil, false); err != nil || changed {
		t.Fatal("transient missing target was not retained")
	}
	if _, err := m.Enable(german); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, contents(t, path), original)
}

func TestNewSubscriptionWithoutRegionDoesNotRestoreAutomatically(t *testing.T) {
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", `{}`)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	before := contents(t, path)
	if changed, err := m.NoteTarget(nil, true); err != nil || !changed {
		t.Fatalf("note: %v, %v", changed, err)
	}
	status, err := m.Status()
	if err != nil || status.State.Target != nil || status.Pending != 1 || !bytes.Equal(before, contents(t, path)) {
		t.Fatalf("missing target: %+v, %v", status, err)
	}
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, contents(t, path), `{}`)
}

func TestRunningChromeBlocksApplyAndRetainsCleanup(t *testing.T) {
	for _, name := range []string{"SingletonLock", "SingletonSocket", "SingletonCookie"} {
		t.Run(name, func(t *testing.T) {
			m := fixture(t)
			path := profile(t, m, "google-chrome/Default", `{}`)
			if _, err := m.Enable(english); err != nil {
				t.Fatal(err)
			}
			lockPath := filepath.Join(m.ConfigHome, "google-chrome", name)
			if err := os.Symlink("fixture-host-123456", lockPath); err != nil {
				t.Fatal(err)
			}
			before := contents(t, path)
			if _, err := m.Enable(german); err == nil || !strings.Contains(err.Error(), "Exit Chrome") {
				t.Fatalf("running apply: %v", err)
			}
			if _, err := m.Disable(); err == nil {
				t.Fatal("running cleanup succeeded")
			}
			status, err := m.Status()
			if err != nil || status.State.Enabled || status.Pending != 1 || !bytes.Equal(before, contents(t, path)) {
				t.Fatalf("cleanup status: %+v, %v", status, err)
			}
			if _, err := m.Enable(english); err == nil {
				t.Fatal("enable discarded pending cleanup")
			}
			os.Remove(lockPath)
			if _, err := m.Disable(); err != nil {
				t.Fatal(err)
			}
			jsonEqual(t, contents(t, path), `{}`)
		})
	}
}

func TestForeignLanguageChangesArePreservedPerField(t *testing.T) {
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", `{"intl":{"accept_languages":"zh-CN,zh","selected_languages":"zh-CN,zh"}}`)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	if err := atomicWrite(path, []byte(`{"intl":{"accept_languages":"fr-FR,fr","selected_languages":"en-US,en"},"new":9007199254740993}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Enable(german); err == nil || !strings.Contains(err.Error(), "outside sbc") {
		t.Fatalf("external change: %v", err)
	}
	result, err := m.Disable()
	if err != nil || len(result.Preserved) != 1 {
		t.Fatalf("preserved: %+v, %v", result, err)
	}
	jsonEqual(t, contents(t, path), `{"intl":{"accept_languages":"fr-FR,fr","selected_languages":"zh-CN,zh"},"new":9007199254740993}`)
}

func TestProfileWriteFailureAndInterruptedUpdateAreRecoverable(t *testing.T) {
	for _, afterWrite := range []bool{false, true} {
		t.Run(map[bool]string{false: "before write", true: "after write"}[afterWrite], func(t *testing.T) {
			m := fixture(t)
			original := `{"intl":{"accept_languages":"zh-CN,zh","selected_languages":"zh-CN,zh"}}`
			path := profile(t, m, "google-chrome/Default", original)
			if _, err := m.Enable(english); err != nil {
				t.Fatal(err)
			}
			written := false
			m.Write = func(file string, data []byte, mode os.FileMode) error {
				if file == path {
					if !afterWrite {
						return errors.New("fixture write failure")
					}
					written = true
				} else if written && file == m.StateFile {
					return errors.New("fixture journal failure")
				}
				return atomicWrite(file, data, mode)
			}
			if _, err := m.Enable(german); err == nil {
				t.Fatal("write failure was ignored")
			}
			m.Write = nil
			if _, err := m.Enable(Target{Node: "third.example", Language: "fr-FR"}); err == nil {
				t.Fatal("another update discarded interrupted ownership")
			}
			if _, err := m.Disable(); err != nil {
				t.Fatal(err)
			}
			jsonEqual(t, contents(t, path), original)
		})
	}
}

func TestInitialWriteFailureAndConcurrentEdit(t *testing.T) {
	for _, concurrent := range []bool{false, true} {
		m := fixture(t)
		path := profile(t, m, "google-chrome/Default", `{}`)
		m.Write = func(file string, data []byte, mode os.FileMode) error {
			if !concurrent && file == path {
				return errors.New("fixture write failure")
			}
			if concurrent && file == m.StateFile && bytes.Contains(data, []byte(`"google-chrome/Default"`)) {
				if err := atomicWrite(path, []byte(`{"changed":true}`), 0o600); err != nil {
					return err
				}
			}
			return atomicWrite(file, data, mode)
		}
		if _, err := m.Enable(english); err == nil {
			t.Fatal("failed or concurrent write succeeded")
		}
		m.Write = nil
		if _, err := m.Disable(); err != nil {
			t.Fatal(err)
		}
		if concurrent {
			jsonEqual(t, contents(t, path), `{"changed":true}`)
		} else {
			jsonEqual(t, contents(t, path), `{}`)
		}
	}
}

func TestPoliciesAndInvalidPreferencesDoNotGetOverwritten(t *testing.T) {
	for _, data := range []string{`null`, `{`, `{} {}`, `{"intl":null}`, `{"intl":{},"intl":{}}`, `{"intl":{"accept_languages":"en","accept_languages":"zh"}}`, `{"intl":{"selected_languages":null}}`, `{"intl":{"forced_languages":["fr"]}}`} {
		m := fixture(t)
		path := profile(t, m, "google-chrome/Default", data)
		if _, err := m.Enable(english); err == nil {
			t.Fatalf("accepted invalid or managed preferences: %s", data)
		}
		if string(contents(t, path)) != data {
			t.Fatal("invalid preferences overwritten")
		}
	}
	for _, policy := range []string{`{"ForcedLanguages":["fr-FR"]}`, `{"ForcedLanguages":["en-US","en"]}`, `not-json`} {
		m := fixture(t)
		path := profile(t, m, "google-chrome/Default", `{}`)
		if err := atomicWrite(filepath.Join(m.PolicyDirs[0], "admin.json"), []byte(policy), 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := m.Enable(english); err == nil {
			t.Fatalf("policy ignored: %s", policy)
		}
		jsonEqual(t, contents(t, path), `{}`)
	}
}

func TestPartialSuccessNoProfilesDeletedProfilesAndUnsafeRecovery(t *testing.T) {
	m := fixture(t)
	if _, err := m.Enable(english); err == nil {
		t.Fatal("missing profiles reported success")
	}
	path := profile(t, m, "google-chrome/Default", `{}`)
	profile(t, m, "google-chrome/Profile 1", `invalid`)
	result, err := m.Enable(english)
	if err == nil || len(result.Changed) != 1 {
		t.Fatalf("partial apply: %+v, %v", result, err)
	}
	os.Remove(path)
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("deleted profile was recreated")
	}
	for _, data := range []string{`invalid`, `{"version":2,"profiles":{}}`, `{"version":1,"profiles":{"../Default":{"original":{},"written":"en-US,en"}}}`} {
		if err := atomicWrite(m.StateFile, []byte(data), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := m.Disable(); err == nil {
			t.Fatal("invalid recovery record discarded")
		}
		if string(contents(t, m.StateFile)) != data {
			t.Fatal("invalid recovery record overwritten")
		}
	}
}

func TestSymlinksAndReadOnlyFilesAreRefused(t *testing.T) {
	for _, component := range []string{"Preferences", "Default", "google-chrome", "browser-language.json", "browser-language.json.lock"} {
		m := fixture(t)
		path := profile(t, m, "google-chrome/Default", `{}`)
		var source string
		switch component {
		case "Preferences":
			source = path
		case "Default":
			source = filepath.Dir(path)
		case "google-chrome":
			source = filepath.Dir(filepath.Dir(path))
		default:
			source = filepath.Join(filepath.Dir(m.StateFile), component)
			if err := atomicWrite(source, []byte(`{}`), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		destination := source + ".aside"
		if err := os.Rename(source, destination); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(destination, source); err != nil {
			t.Fatal(err)
		}
		if _, err := m.Enable(english); err == nil {
			t.Fatalf("followed symbolic link: %s", component)
		}
	}
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", `{}`)
	os.Chmod(path, 0o400)
	defer os.Chmod(path, 0o600)
	if _, err := m.Enable(english); err == nil {
		t.Fatal("overwrote a read-only profile")
	}
}

func TestParallelManagersKeepTheFirstOriginals(t *testing.T) {
	m := fixture(t)
	original := `{"intl":{"accept_languages":"zh-CN","selected_languages":"zh-CN"}}`
	path := profile(t, m, "google-chrome/Default", original)
	failures := make(chan error, 2)
	var group sync.WaitGroup
	for range 2 {
		group.Go(func() {
			other := *m
			_, err := other.Enable(english)
			failures <- err
		})
	}
	group.Wait()
	close(failures)
	for err := range failures {
		if err != nil {
			t.Fatal(err)
		}
	}
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, contents(t, path), original)
}

func TestFailedCleanupJournalRetainsRecovery(t *testing.T) {
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", `{}`)
	if _, err := m.Enable(english); err != nil {
		t.Fatal(err)
	}
	restored := false
	m.Write = func(file string, data []byte, mode os.FileMode) error {
		if file == path {
			restored = true
		} else if restored && file == m.StateFile {
			return errors.New("fixture cleanup journal failure")
		}
		return atomicWrite(file, data, mode)
	}
	if _, err := m.Disable(); err == nil {
		t.Fatal("cleanup journal failure was ignored")
	}
	m.Write = nil
	status, err := m.Status()
	if err != nil || status.State.Enabled || status.Pending != 1 {
		t.Fatalf("recovery lost: %+v, %v", status, err)
	}
	if _, err := m.Disable(); err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, contents(t, path), `{}`)
}

func TestNonRegularFilesNeverBlockReads(t *testing.T) {
	m := fixture(t)
	path := profile(t, m, "google-chrome/Default", `{}`)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(path, 0o600); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := readPreferences(path); done <- err }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("accepted a FIFO as preferences")
		}
	case <-time.After(time.Second):
		t.Fatal("reading a FIFO blocked")
	}
}
