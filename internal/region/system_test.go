package region

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type systemFixture struct {
	paths    SystemPaths
	zoneRoot string
	calls    []string
	locales  []string
	failTZ   bool
	failLoc  bool
}

func newSystemFixture(t *testing.T) *systemFixture {
	t.Helper()
	root := t.TempDir()
	zoneRoot := filepath.Join(root, "zoneinfo")
	for _, zone := range []string{"Asia/Shanghai", "Asia/Singapore", "Europe/Berlin", "UTC"} {
		path := filepath.Join(zoneRoot, zone)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(zone), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	etc := filepath.Join(root, "etc")
	if err := os.MkdirAll(etc, 0o755); err != nil {
		t.Fatal(err)
	}
	paths := SystemPaths{
		Localtime:     filepath.Join(etc, "localtime"),
		Timezone:      filepath.Join(etc, "timezone"),
		LocaleDefault: filepath.Join(etc, "default", "locale"),
		LocaleConf:    filepath.Join(etc, "locale.conf"),
		ZoneRoot:      zoneRoot,
		StateDir:      filepath.Join(root, "state"),
		StateFile:     filepath.Join(root, "state", "state.json"),
		BackupDir:     filepath.Join(root, "state", "backup"),
	}
	if err := os.MkdirAll(filepath.Dir(paths.LocaleDefault), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := replaceSymlink(paths.Localtime, filepath.Join(zoneRoot, "Asia/Singapore")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(paths.LocaleDefault, []byte("LANG=en_US.UTF-8\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(paths.Timezone, []byte("Asia/Singapore\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return &systemFixture{
		paths:    paths,
		zoneRoot: zoneRoot,
		locales:  []string{"C.utf8", "en_US.utf8", "zh_CN.utf8", "de_DE.utf8"},
	}
}

func (f *systemFixture) run(name string, args ...string) error {
	f.calls = append(f.calls, filepath.Base(name)+" "+strings.Join(args, " "))
	switch filepath.Base(name) {
	case "timedatectl":
		if f.failTZ {
			return errors.New("timedatectl failed")
		}
		zone := args[len(args)-1]
		// timedatectl writes a relative link on systemd-based systems.
		target, err := filepath.Rel(filepath.Dir(f.paths.Localtime), filepath.Join(f.paths.ZoneRoot, zone))
		if err != nil {
			return err
		}
		return replaceSymlink(f.paths.Localtime, target)
	case "update-locale":
		if f.failLoc {
			return errors.New("update-locale failed")
		}
		var file string
		var values []string
		for _, arg := range args {
			if strings.HasPrefix(arg, "--locale-file=") {
				file = strings.TrimPrefix(arg, "--locale-file=")
			} else {
				values = append(values, arg)
			}
		}
		return os.WriteFile(file, []byte(strings.Join(values, "\n")+"\n"), 0o644)
	default:
		return errors.New("unexpected command " + name)
	}
}

func (f *systemFixture) lookPath(name string) (string, error) {
	switch name {
	case "timedatectl", "update-locale":
		return "/usr/bin/" + name, nil
	default:
		return "", os.ErrNotExist
	}
}

func (f *systemFixture) apply(t *testing.T, timezone, language bool) (SystemApplyResult, error) {
	t.Helper()
	return SystemApply(SystemApplyOptions{
		Paths:    f.paths,
		Profile:  Profile{Timezone: "Asia/Shanghai", Language: "zh-CN"},
		Timezone: timezone,
		Language: language,
		Run:      f.run,
		LookPath: f.lookPath,
		Locales:  func() ([]string, error) { return f.locales, nil },
		Euid:     func() int { return 0 },
		Now:      func() time.Time { return time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC) },
	})
}

func (f *systemFixture) disable(t *testing.T, timezone, language bool) (SystemDisableResult, error) {
	t.Helper()
	return SystemDisable(SystemDisableOptions{
		Paths:    f.paths,
		Timezone: timezone,
		Language: language,
		Euid:     func() int { return 0 },
		Now:      func() time.Time { return time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC) },
	})
}

func TestSystemApplyWritesAndRestoresBothComponents(t *testing.T) {
	f := newSystemFixture(t)
	result, err := f.apply(t, true, true)
	if err != nil {
		t.Fatal(err)
	}
	if !result.TimezoneApplied || !result.LanguageApplied {
		t.Fatalf("unexpected result: %#v", result)
	}
	status, err := SystemStatusOf(f.paths)
	if err != nil {
		t.Fatal(err)
	}
	if !status.TimezoneOwned() || !status.LanguageOwned() {
		t.Fatalf("components are not owned: %#v", status.State)
	}
	if !status.TimezoneMatches() || !status.LanguageMatches() {
		t.Fatalf("current values do not match: %#v", status)
	}
	if status.CurrentTimezone != "Asia/Shanghai" || status.CurrentLanguage != "zh_CN.UTF-8" {
		t.Fatalf("unexpected current values: %#v", status)
	}
	state, err := loadSystemState(f.paths)
	if err != nil {
		t.Fatal(err)
	}
	if state.Timezone.Target != "Asia/Shanghai" ||
		state.Timezone.Original.Kind != "symlink" ||
		state.Timezone.Original.Target != filepath.Join(f.zoneRoot, "Asia/Singapore") {
		t.Fatalf("unexpected timezone state: %#v", state.Timezone)
	}
	if state.Language.Original.SHA256 == "" || state.Language.Written.SHA256 == "" {
		t.Fatalf("unexpected language state: %#v", state.Language)
	}

	disabled, err := f.disable(t, false, false)
	if err != nil {
		t.Fatal(err)
	}
	if !disabled.TimezoneRestored || !disabled.LanguageRestored {
		t.Fatalf("unexpected disable result: %#v", disabled)
	}
	target, err := os.Readlink(f.paths.Localtime)
	if err != nil || target != filepath.Join(f.zoneRoot, "Asia/Singapore") {
		t.Fatalf("localtime = %q, %v", target, err)
	}
	data, err := os.ReadFile(f.paths.LocaleDefault)
	if err != nil || string(data) != "LANG=en_US.UTF-8\n" {
		t.Fatalf("locale file = %q, %v", data, err)
	}
	if _, err := os.Stat(f.paths.StateFile); !os.IsNotExist(err) {
		t.Fatal("state file remains")
	}
}

func TestSystemApplyTimezoneAcceptsToolLinks(t *testing.T) {
	for _, kind := range []string{"absolute", "relative", "unchanged relative"} {
		t.Run(kind, func(t *testing.T) {
			f := newSystemFixture(t)
			target := filepath.Join(f.zoneRoot, "Asia/Shanghai")
			if kind != "absolute" {
				var err error
				target, err = filepath.Rel(filepath.Dir(f.paths.Localtime), target)
				if err != nil {
					t.Fatal(err)
				}
			}
			if kind == "unchanged relative" {
				if err := replaceSymlink(f.paths.Localtime, target); err != nil {
					t.Fatal(err)
				}
			}
			original, err := readSystemValue(f.paths.Localtime)
			if err != nil {
				t.Fatal(err)
			}
			calls := 0
			opts := SystemApplyOptions{
				Paths:    f.paths,
				Profile:  Profile{Timezone: "Asia/Shanghai", Language: "zh-CN"},
				Timezone: true,
				Run: func(string, ...string) error {
					calls++
					if kind == "unchanged relative" {
						return nil
					}
					return replaceSymlink(f.paths.Localtime, target)
				},
				LookPath: f.lookPath,
				Euid:     func() int { return 0 },
			}
			for range 2 {
				result, err := SystemApply(opts)
				if err != nil || !result.TimezoneApplied || result.TimezoneError != nil {
					t.Fatalf("apply: %#v, %v", result, err)
				}
			}
			if calls != 1 {
				t.Fatalf("timedatectl ran %d times; want 1", calls)
			}
			status, err := SystemStatusOf(f.paths)
			if err != nil {
				t.Fatal(err)
			}
			if !status.TimezoneMatches() || status.CurrentTimezone != "Asia/Shanghai" ||
				status.State.Timezone.Written.Target != target {
				t.Fatalf("unexpected status: %#v", status)
			}
			disabled, err := f.disable(t, true, false)
			if err != nil || !disabled.TimezoneRestored {
				t.Fatalf("disable: %#v, %v", disabled, err)
			}
			got, err := readSystemValue(f.paths.Localtime)
			if err != nil || !sameSystemValue(got, original) {
				t.Fatalf("restored localtime = %#v, %v; want %#v", got, err, original)
			}
		})
	}
}

func TestSystemApplyTimezoneRollsBackWrongToolResults(t *testing.T) {
	for _, kind := range []string{"wrong relative", "file", "missing", "unchanged"} {
		t.Run(kind, func(t *testing.T) {
			f := newSystemFixture(t)
			original, err := readSystemValue(f.paths.Localtime)
			if err != nil {
				t.Fatal(err)
			}
			result, err := SystemApply(SystemApplyOptions{
				Paths:    f.paths,
				Profile:  Profile{Timezone: "Asia/Shanghai", Language: "zh-CN"},
				Timezone: true,
				Language: true,
				Run: func(name string, args ...string) error {
					if filepath.Base(name) != "timedatectl" {
						return f.run(name, args...)
					}
					switch kind {
					case "wrong relative":
						return replaceSymlink(f.paths.Localtime, "../zoneinfo/Europe/Berlin")
					case "file", "missing":
						if err := os.Remove(f.paths.Localtime); err != nil {
							return err
						}
						if kind == "file" {
							return os.WriteFile(f.paths.Localtime, []byte("wrong zone"), 0o644)
						}
					}
					return nil
				},
				LookPath: f.lookPath,
				Locales:  func() ([]string, error) { return f.locales, nil },
				Euid:     func() int { return 0 },
			})
			if err != nil || result.TimezoneApplied || result.TimezoneError == nil || !result.LanguageApplied {
				t.Fatalf("apply: %#v, %v", result, err)
			}
			got, err := readSystemValue(f.paths.Localtime)
			if err != nil || !sameSystemValue(got, original) {
				t.Fatalf("rolled back localtime = %#v, %v; want %#v", got, err, original)
			}
			state, err := loadSystemState(f.paths)
			if err != nil || state == nil || state.Timezone != nil || state.Language == nil {
				t.Fatalf("state = %#v, %v", state, err)
			}
		})
	}
}

func TestSystemApplyFallsBackWithoutTools(t *testing.T) {
	f := newSystemFixture(t)
	result, err := SystemApply(SystemApplyOptions{
		Paths:    f.paths,
		Profile:  Profile{Timezone: "Asia/Shanghai", Language: "zh-CN"},
		Timezone: true,
		Language: true,
		Run:      f.run,
		LookPath: func(string) (string, error) { return "", os.ErrNotExist },
		Locales:  func() ([]string, error) { return f.locales, nil },
		Euid:     func() int { return 0 },
	})
	if err != nil {
		t.Fatal(err)
	}
	if !result.TimezoneApplied || !result.LanguageApplied || len(f.calls) != 0 {
		t.Fatalf("unexpected result: %#v, calls %v", result, f.calls)
	}
	status, err := SystemStatusOf(f.paths)
	if err != nil {
		t.Fatal(err)
	}
	if !status.TimezoneMatches() || !status.LanguageMatches() {
		t.Fatalf("fallback values do not match: %#v", status)
	}
	if _, err := f.disable(t, false, false); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(f.paths.LocaleDefault)
	if string(data) != "LANG=en_US.UTF-8\n" {
		t.Fatalf("locale file = %q", data)
	}
}

func TestSystemDisableLeavesExternalChanges(t *testing.T) {
	f := newSystemFixture(t)
	if _, err := f.apply(t, true, true); err != nil {
		t.Fatal(err)
	}
	if err := replaceSymlink(f.paths.Localtime, filepath.Join(f.zoneRoot, "Europe/Berlin")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(f.paths.LocaleDefault, []byte("LANG=de_DE.UTF-8\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	result, err := f.disable(t, false, false)
	if err != nil {
		t.Fatal(err)
	}
	if !result.TimezoneKept || !result.LanguageKept {
		t.Fatalf("external changes were not kept: %#v", result)
	}
	target, _ := os.Readlink(f.paths.Localtime)
	if target != filepath.Join(f.zoneRoot, "Europe/Berlin") {
		t.Fatalf("localtime = %q", target)
	}
	data, _ := os.ReadFile(f.paths.LocaleDefault)
	if string(data) != "LANG=de_DE.UTF-8\n" {
		t.Fatalf("locale file = %q", data)
	}
	if _, err := os.Stat(f.paths.StateFile); !os.IsNotExist(err) {
		t.Fatal("state file remains")
	}
}

func TestSystemApplyRestoresARegularLocaltimeFile(t *testing.T) {
	f := newSystemFixture(t)
	if err := os.Remove(f.paths.Localtime); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(f.paths.Localtime, []byte("original-zone-data"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := f.apply(t, true, false); err != nil {
		t.Fatal(err)
	}
	if _, err := f.disable(t, false, false); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(f.paths.Localtime)
	if err != nil || string(data) != "original-zone-data" {
		t.Fatalf("localtime = %q, %v", data, err)
	}
	info, err := os.Lstat(f.paths.Localtime)
	if err != nil || !info.Mode().IsRegular() {
		t.Fatalf("localtime is not a regular file: %v", info)
	}
}

func TestSystemApplyKeepsTheOriginalAcrossTargetChanges(t *testing.T) {
	f := newSystemFixture(t)
	if _, err := f.apply(t, true, false); err != nil {
		t.Fatal(err)
	}
	result, err := SystemApply(SystemApplyOptions{
		Paths:    f.paths,
		Profile:  Profile{Timezone: "Europe/Berlin", Language: "zh-CN"},
		Timezone: true,
		Run:      f.run,
		LookPath: f.lookPath,
		Locales:  func() ([]string, error) { return f.locales, nil },
		Euid:     func() int { return 0 },
	})
	if err != nil || !result.TimezoneApplied {
		t.Fatalf("second apply: %#v, %v", result, err)
	}
	if _, err := f.disable(t, false, false); err != nil {
		t.Fatal(err)
	}
	target, _ := os.Readlink(f.paths.Localtime)
	if target != filepath.Join(f.zoneRoot, "Asia/Singapore") {
		t.Fatalf("localtime = %q", target)
	}
}

func TestSystemApplyRebaselinesAnExternalChange(t *testing.T) {
	f := newSystemFixture(t)
	if _, err := f.apply(t, true, false); err != nil {
		t.Fatal(err)
	}
	if err := replaceSymlink(f.paths.Localtime, filepath.Join(f.zoneRoot, "Europe/Berlin")); err != nil {
		t.Fatal(err)
	}
	if _, err := f.apply(t, true, false); err != nil {
		t.Fatal(err)
	}
	if _, err := f.disable(t, false, false); err != nil {
		t.Fatal(err)
	}
	target, _ := os.Readlink(f.paths.Localtime)
	if target != filepath.Join(f.zoneRoot, "Europe/Berlin") {
		t.Fatalf("localtime = %q", target)
	}
}

func TestSystemApplyKeepsTimezoneWhenLocaleIsMissing(t *testing.T) {
	f := newSystemFixture(t)
	f.locales = []string{"C.utf8", "en_US.utf8"}
	result, err := f.apply(t, true, true)
	if err != nil {
		t.Fatal(err)
	}
	if !result.TimezoneApplied || result.LanguageApplied || result.LanguageError == nil {
		t.Fatalf("unexpected result: %#v", result)
	}
	state, err := loadSystemState(f.paths)
	if err != nil {
		t.Fatal(err)
	}
	if state.Timezone == nil || state.Language != nil {
		t.Fatalf("unexpected state: %#v", state)
	}
	data, _ := os.ReadFile(f.paths.LocaleDefault)
	if string(data) != "LANG=en_US.UTF-8\n" {
		t.Fatalf("locale file changed: %q", data)
	}
}

func TestSystemApplyRequiresRoot(t *testing.T) {
	f := newSystemFixture(t)
	_, err := SystemApply(SystemApplyOptions{
		Paths:    f.paths,
		Profile:  Profile{Timezone: "Asia/Shanghai", Language: "zh-CN"},
		Timezone: true,
		Euid:     func() int { return 1000 },
	})
	if err == nil || !strings.Contains(err.Error(), "root") {
		t.Fatalf("error = %v", err)
	}
}

func TestSystemApplyFollowsTheLocaleSymlink(t *testing.T) {
	f := newSystemFixture(t)
	if err := os.Remove(f.paths.LocaleDefault); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(f.paths.LocaleConf, []byte("LANG=en_US.UTF-8\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(f.paths.LocaleConf, f.paths.LocaleDefault); err != nil {
		t.Fatal(err)
	}
	if _, err := f.apply(t, false, true); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(f.paths.LocaleConf)
	if localeValue(data, "LANG") != "zh_CN.UTF-8" {
		t.Fatalf("locale.conf = %q", data)
	}
	if _, err := f.disable(t, false, false); err != nil {
		t.Fatal(err)
	}
	data, _ = os.ReadFile(f.paths.LocaleConf)
	if string(data) != "LANG=en_US.UTF-8\n" {
		t.Fatalf("locale.conf = %q", data)
	}
	if info, err := os.Lstat(f.paths.LocaleDefault); err != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatalf("locale symlink was replaced: %v", info)
	}
}

func TestSystemApplyRejectsAStateSymlink(t *testing.T) {
	f := newSystemFixture(t)
	if err := os.MkdirAll(f.paths.StateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(f.paths.StateDir, "real.json"), f.paths.StateFile); err != nil {
		t.Fatal(err)
	}
	_, err := f.apply(t, true, false)
	if err == nil || !strings.Contains(err.Error(), "regular file") {
		t.Fatalf("error = %v", err)
	}
}
