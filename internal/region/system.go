package region

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

const (
	systemStateVersion = 1
	systemStateName    = "state.json"
	systemBackupDir    = "backup"
)

// SystemPaths names the fixed system locations sbc manages. Tests replace them
// with a temporary tree.
type SystemPaths struct {
	Localtime     string
	Timezone      string
	LocaleDefault string
	LocaleConf    string
	ZoneRoot      string
	StateDir      string
	StateFile     string
	BackupDir     string
}

// DefaultSystemPaths returns the paths on this machine.
func DefaultSystemPaths() SystemPaths {
	state := "/var/lib/sbc/region"
	return SystemPaths{
		Localtime:     "/etc/localtime",
		Timezone:      "/etc/timezone",
		LocaleDefault: "/etc/default/locale",
		LocaleConf:    "/etc/locale.conf",
		ZoneRoot:      "/usr/share/zoneinfo",
		StateDir:      state,
		StateFile:     filepath.Join(state, systemStateName),
		BackupDir:     filepath.Join(state, systemBackupDir),
	}
}

// SystemState is the root-owned record of the system values sbc replaced.
type SystemState struct {
	Version  int              `json:"version"`
	Profile  Profile          `json:"profile"`
	Timezone *SystemComponent `json:"timezone,omitempty"`
	Language *SystemComponent `json:"language,omitempty"`
	Updated  string           `json:"updated,omitempty"`
}

// SystemComponent is one system value sbc owns.
type SystemComponent struct {
	// Target is the IANA time zone or the BCP 47 language sbc applied.
	Target string `json:"target"`
	// Pending is set while a change is in progress. Cleanup then verifies the
	// target value instead of the exact written content.
	Pending  bool        `json:"pending,omitempty"`
	Original SystemValue `json:"original"`
	Written  SystemValue `json:"written"`
}

// SystemValue is a file, a symbolic link or an absent path.
type SystemValue struct {
	Kind   string `json:"kind"`
	Target string `json:"target,omitempty"`
	Mode   uint32 `json:"mode,omitempty"`
	SHA256 string `json:"sha256,omitempty"`
	Backup string `json:"backup,omitempty"`
}

// SystemStatus is what sbc reads without root: the saved state and the current
// system values.
type SystemStatus struct {
	State           *SystemState
	TimezoneCurrent SystemValue
	LanguageCurrent SystemValue
	LocalePath      string
	CurrentTimezone string
	CurrentLanguage string
}

// SystemApplyOptions configures one root-side change.
type SystemApplyOptions struct {
	Paths    SystemPaths
	Profile  Profile
	Timezone bool
	Language bool
	Run      func(name string, args ...string) error
	LookPath func(name string) (string, error)
	Locales  func() ([]string, error)
	Euid     func() int
	Now      func() time.Time
}

// SystemApplyResult reports each component separately.
type SystemApplyResult struct {
	TimezoneApplied bool
	LanguageApplied bool
	TimezoneWarning error
	LanguageWarning error
	TimezoneError   error
	LanguageError   error
}

// SystemDisableOptions configures one root-side restore.
type SystemDisableOptions struct {
	Paths    SystemPaths
	Timezone bool
	Language bool
	Euid     func() int
	Now      func() time.Time
}

// SystemDisableResult reports each component separately.
type SystemDisableResult struct {
	TimezoneRestored bool
	LanguageRestored bool
	TimezoneKept     bool
	LanguageKept     bool
	TimezoneError    error
	LanguageError    error
}

// CheckSystemTimezone reports whether an IANA time zone is installed for the
// whole system.
func CheckSystemTimezone(paths SystemPaths, zone string) error {
	return validateSystemTimezone(paths, zone)
}

// CheckSystemLocale reports whether the locale for a BCP 47 language is
// generated on this machine.
func CheckSystemLocale(language string) error {
	available, err := systemLocales()
	if err != nil {
		return i18n.Errorf("could not list the installed locales: %w", err)
	}
	return CheckLocale(language, available)
}

// SystemStateExists reports whether a root-owned system region record exists.
// It is readable without root.
func SystemStateExists(paths SystemPaths) bool {
	info, err := os.Lstat(paths.StateFile)
	return err == nil && info.Mode().IsRegular()
}

// SystemStatusOf reads the saved state and the current system values.
func SystemStatusOf(paths SystemPaths) (SystemStatus, error) {
	var status SystemStatus
	if err := checkSystemPaths(paths); err != nil {
		return status, err
	}
	// The state directory matters only when a state file must be trusted.
	if _, err := os.Lstat(paths.StateFile); err == nil {
		if err := checkStateDir(paths); err != nil {
			return status, err
		}
	} else if !os.IsNotExist(err) {
		return status, err
	}
	state, err := loadSystemState(paths)
	if err != nil {
		return status, err
	}
	status.State = state
	current, _, err := captureSystemValue(paths.Localtime)
	if err != nil {
		return status, err
	}
	status.TimezoneCurrent = current
	status.CurrentTimezone = timezoneName(paths, current)
	_, effective, err := localeFilePath(paths)
	if err != nil {
		return status, err
	}
	status.LocalePath = effective
	localeValueCurrent, data, err := captureSystemValue(effective)
	if err != nil {
		return status, err
	}
	status.LanguageCurrent = localeValueCurrent
	status.CurrentLanguage = localeValue(data, "LANG")
	return status, nil
}

// TimezoneOwned reports whether the saved state claims the system time zone.
func (s SystemStatus) TimezoneOwned() bool { return s.State != nil && s.State.Timezone != nil }

// LanguageOwned reports whether the saved state claims the system language.
func (s SystemStatus) LanguageOwned() bool { return s.State != nil && s.State.Language != nil }

// TimezoneMatches reports whether the current system time zone is the value
// sbc wrote. A pending component never matches.
func (s SystemStatus) TimezoneMatches() bool {
	if !s.TimezoneOwned() || s.State.Timezone.Pending {
		return false
	}
	return sameSystemValue(s.TimezoneCurrent, s.State.Timezone.Written)
}

// LanguageMatches reports whether the current system locale file is the value
// sbc wrote. A pending component never matches.
func (s SystemStatus) LanguageMatches() bool {
	if !s.LanguageOwned() || s.State.Language.Pending {
		return false
	}
	return sameSystemValue(s.LanguageCurrent, s.State.Language.Written)
}

// SystemApply writes the requested system components. It must run as root.
func SystemApply(opts SystemApplyOptions) (SystemApplyResult, error) {
	opts = withSystemApplyDefaults(opts)
	if opts.Euid() != 0 {
		return SystemApplyResult{}, i18n.New("system region changes must run as root")
	}
	if !opts.Timezone && !opts.Language {
		return SystemApplyResult{}, i18n.New("no system region component was requested")
	}
	profile, err := opts.Profile.Normalize()
	if err != nil {
		return SystemApplyResult{}, err
	}
	if err := checkSystemPaths(opts.Paths); err != nil {
		return SystemApplyResult{}, err
	}
	if err := checkStateDir(opts.Paths); err != nil {
		return SystemApplyResult{}, err
	}
	unlock, err := lockSystemRegion(filepath.Join(opts.Paths.StateDir, "lock"))
	if err != nil {
		return SystemApplyResult{}, err
	}
	defer unlock()
	state, err := loadSystemState(opts.Paths)
	if err != nil {
		return SystemApplyResult{}, err
	}
	if state == nil {
		state = &SystemState{Version: systemStateVersion}
	}
	state.Profile = profile
	var result SystemApplyResult
	if opts.Timezone {
		warning, err := applySystemTimezone(opts, state, profile.Timezone)
		if err != nil {
			result.TimezoneError = err
		} else {
			result.TimezoneApplied = true
			result.TimezoneWarning = warning
		}
	}
	if opts.Language {
		warning, err := applySystemLanguage(opts, state, profile.Language)
		if err != nil {
			result.LanguageError = err
		} else {
			result.LanguageApplied = true
			result.LanguageWarning = warning
		}
	}
	if state.Timezone == nil && state.Language == nil {
		if err := removeSystemState(opts.Paths); err != nil {
			return result, err
		}
		return result, nil
	}
	state.Updated = opts.Now().UTC().Format(time.RFC3339)
	if err := saveSystemState(opts.Paths, state); err != nil {
		return result, err
	}
	return result, nil
}

// SystemDisable restores the requested system components. It must run as root.
// With neither component selected it restores both.
func SystemDisable(opts SystemDisableOptions) (SystemDisableResult, error) {
	opts = withSystemDisableDefaults(opts)
	if opts.Euid() != 0 {
		return SystemDisableResult{}, i18n.New("system region changes must run as root")
	}
	if err := checkSystemPaths(opts.Paths); err != nil {
		return SystemDisableResult{}, err
	}
	if _, err := os.Lstat(opts.Paths.StateFile); os.IsNotExist(err) {
		return SystemDisableResult{}, nil
	} else if err != nil {
		return SystemDisableResult{}, err
	}
	if err := checkStateDir(opts.Paths); err != nil {
		return SystemDisableResult{}, err
	}
	unlock, err := lockSystemRegion(filepath.Join(opts.Paths.StateDir, "lock"))
	if err != nil {
		return SystemDisableResult{}, err
	}
	defer unlock()
	state, err := loadSystemState(opts.Paths)
	if err != nil {
		return SystemDisableResult{}, err
	}
	if state == nil {
		return SystemDisableResult{}, nil
	}
	all := !opts.Timezone && !opts.Language
	var result SystemDisableResult
	if opts.Timezone || all {
		disableSystemTimezone(opts.Paths, state, &result)
	}
	if opts.Language || all {
		disableSystemLanguage(opts.Paths, state, &result)
	}
	if state.Timezone == nil && state.Language == nil {
		if err := removeSystemState(opts.Paths); err != nil {
			return result, err
		}
		return result, nil
	}
	state.Updated = opts.Now().UTC().Format(time.RFC3339)
	if err := saveSystemState(opts.Paths, state); err != nil {
		return result, err
	}
	return result, nil
}

func applySystemTimezone(opts SystemApplyOptions, state *SystemState, zone string) (error, error) {
	if err := validateSystemTimezone(opts.Paths, zone); err != nil {
		return nil, err
	}
	current, currentData, err := captureSystemValue(opts.Paths.Localtime)
	if err != nil {
		return nil, err
	}
	old := state.Timezone
	component := &SystemComponent{Target: zone}
	if old != nil && sameSystemValue(current, old.Written) {
		component.Original = old.Original
		if old.Target == zone && !old.Pending {
			component.Written = old.Written
			state.Timezone = component
			return nil, nil
		}
	} else {
		original, err := snapshotValue(opts.Paths, current, currentData, "localtime")
		if err != nil {
			return nil, err
		}
		component.Original = original
	}
	zonePath := filepath.Join(opts.Paths.ZoneRoot, zone)
	component.Written = SystemValue{Kind: "symlink", Target: zonePath}
	component.Pending = true
	state.Timezone = component
	state.Profile.Timezone = zone
	if err := saveSystemState(opts.Paths, state); err != nil {
		return nil, err
	}

	var warning error
	written := false
	tools := resolveSystemTools(opts.LookPath)
	if tools.Timedatectl != "" {
		if err := opts.Run(tools.Timedatectl, "set-timezone", zone); err == nil {
			written = true
		} else {
			warning = err
		}
	}
	if !written {
		if err := replaceSymlink(opts.Paths.Localtime, zonePath); err != nil {
			return warning, rollbackTimezone(opts.Paths, state, old, current, currentData, err)
		}
	}
	got, err := readSystemValue(opts.Paths.Localtime)
	if err != nil {
		return warning, rollbackTimezone(opts.Paths, state, old, current, currentData, err)
	}
	if !sameSystemValue(got, component.Written) {
		return warning, rollbackTimezone(opts.Paths, state, old, current, currentData,
			i18n.Errorf("the system time zone did not change to %s", zone))
	}
	component.Written = got
	component.Pending = false
	state.Timezone = component
	if err := saveSystemState(opts.Paths, state); err != nil {
		return warning, err
	}
	return warning, nil
}

func applySystemLanguage(opts SystemApplyOptions, state *SystemState, language string) (error, error) {
	locale, err := Locale(language)
	if err != nil {
		return nil, err
	}
	list, err := LanguageList(language)
	if err != nil {
		return nil, err
	}
	available, err := opts.Locales()
	if err != nil {
		return nil, i18n.Errorf("could not list the installed locales: %w", err)
	}
	if !localeAvailable(locale, available) {
		return nil, missingLocale(locale)
	}
	toolPath, effective, err := localeFilePath(opts.Paths)
	if err != nil {
		return nil, err
	}
	current, currentData, err := captureSystemValue(effective)
	if err != nil {
		return nil, err
	}
	old := state.Language
	component := &SystemComponent{Target: language}
	if old != nil && sameSystemValue(current, old.Written) {
		component.Original = old.Original
		if old.Target == language && !old.Pending {
			component.Written = old.Written
			state.Language = component
			return nil, nil
		}
	} else {
		original, err := snapshotValue(opts.Paths, current, currentData, "locale")
		if err != nil {
			return nil, err
		}
		component.Original = original
	}
	component.Pending = true
	component.Written = current
	state.Language = component
	state.Profile.Language = language
	if err := saveSystemState(opts.Paths, state); err != nil {
		return nil, err
	}

	tools := resolveSystemTools(opts.LookPath)
	if tools.UpdateLocale != "" {
		if err := opts.Run(tools.UpdateLocale, "--locale-file="+toolPath,
			"LANG="+locale, "LANGUAGE="+list, "LC_MESSAGES="+locale); err != nil {
			return nil, rollbackLanguage(opts.Paths, effective, state, old, current, currentData, err)
		}
	} else {
		mode := os.FileMode(0o644)
		if current.Kind == "file" {
			mode = os.FileMode(current.Mode)
		}
		if err := writeLocaleFile(effective, locale, list, mode); err != nil {
			return nil, rollbackLanguage(opts.Paths, effective, state, old, current, currentData, err)
		}
	}
	got, data, err := captureSystemValue(effective)
	if err != nil {
		return nil, rollbackLanguage(opts.Paths, effective, state, old, current, currentData, err)
	}
	if got.Kind != "file" ||
		localeValue(data, "LANG") != locale ||
		localeValue(data, "LANGUAGE") != list ||
		localeValue(data, "LC_MESSAGES") != locale {
		return nil, rollbackLanguage(opts.Paths, effective, state, old, current, currentData,
			i18n.Errorf("the system language did not change to %s", language))
	}
	component.Written = got
	component.Pending = false
	state.Language = component
	if err := saveSystemState(opts.Paths, state); err != nil {
		return nil, err
	}
	return nil, nil
}

func rollbackTimezone(paths SystemPaths, state *SystemState, old *SystemComponent, current SystemValue, data []byte, cause error) error {
	restoreErr := restoreCaptured(paths.Localtime, current, data)
	if restoreErr != nil {
		// Keep the new pending record: a later off can still try to restore
		// the original value from its backup.
		_ = saveSystemState(paths, state)
		return errors.Join(cause, i18n.Errorf("the system time zone could not be restored; inspect %s", paths.Localtime), restoreErr)
	}
	if old != nil {
		state.Timezone = old
	} else {
		state.Timezone = nil
	}
	_ = saveSystemState(paths, state)
	return cause
}

func rollbackLanguage(paths SystemPaths, effective string, state *SystemState, old *SystemComponent, current SystemValue, data []byte, cause error) error {
	restoreErr := restoreCaptured(effective, current, data)
	if restoreErr != nil {
		// Keep the new pending record: a later off can still try to restore
		// the original value from its backup.
		_ = saveSystemState(paths, state)
		return errors.Join(cause, i18n.Errorf("the system locale file could not be restored; inspect %s", effective), restoreErr)
	}
	if old != nil {
		state.Language = old
	} else {
		state.Language = nil
	}
	_ = saveSystemState(paths, state)
	return cause
}

func disableSystemTimezone(paths SystemPaths, state *SystemState, result *SystemDisableResult) {
	component := state.Timezone
	if component == nil {
		return
	}
	current, _, err := captureSystemValue(paths.Localtime)
	if err != nil {
		result.TimezoneError = err
		return
	}
	if !timezoneOwned(paths, current, component) {
		state.Timezone = nil
		removeBackup(paths, component.Original)
		if !sameSystemValue(current, component.Original) {
			result.TimezoneKept = true
		}
		return
	}
	if err := restoreSystemValue(paths, paths.Localtime, component.Original); err != nil {
		result.TimezoneError = err
		return
	}
	state.Timezone = nil
	removeBackup(paths, component.Original)
	result.TimezoneRestored = true
}

func disableSystemLanguage(paths SystemPaths, state *SystemState, result *SystemDisableResult) {
	component := state.Language
	if component == nil {
		return
	}
	_, effective, err := localeFilePath(paths)
	if err != nil {
		result.LanguageError = err
		return
	}
	current, data, err := captureSystemValue(effective)
	if err != nil {
		result.LanguageError = err
		return
	}
	if !languageOwned(current, data, component) {
		state.Language = nil
		removeBackup(paths, component.Original)
		if !sameSystemValue(current, component.Original) {
			result.LanguageKept = true
		}
		return
	}
	if err := restoreSystemValue(paths, effective, component.Original); err != nil {
		result.LanguageError = err
		return
	}
	state.Language = nil
	removeBackup(paths, component.Original)
	result.LanguageRestored = true
}

func timezoneOwned(paths SystemPaths, current SystemValue, component *SystemComponent) bool {
	if sameSystemValue(current, component.Written) {
		return true
	}
	if !component.Pending || current.Kind != "symlink" {
		return false
	}
	target := current.Target
	if !filepath.IsAbs(target) {
		target = filepath.Join(filepath.Dir(paths.Localtime), target)
	}
	return filepath.Clean(target) == filepath.Join(filepath.Clean(paths.ZoneRoot), component.Target)
}

func languageOwned(current SystemValue, data []byte, component *SystemComponent) bool {
	if sameSystemValue(current, component.Written) {
		return true
	}
	if !component.Pending {
		return false
	}
	locale, err := Locale(component.Target)
	return err == nil && localeValue(data, "LANG") == locale
}

func withSystemApplyDefaults(opts SystemApplyOptions) SystemApplyOptions {
	if opts.Run == nil {
		opts.Run = runSystemCommand
	}
	if opts.LookPath == nil {
		opts.LookPath = systemLookPath
	}
	if opts.Locales == nil {
		opts.Locales = systemLocales
	}
	if opts.Euid == nil {
		opts.Euid = os.Geteuid
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	return opts
}

func withSystemDisableDefaults(opts SystemDisableOptions) SystemDisableOptions {
	if opts.Euid == nil {
		opts.Euid = os.Geteuid
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	return opts
}

func loadSystemState(paths SystemPaths) (*SystemState, error) {
	if err := checkStateFile(paths.StateFile); err != nil {
		return nil, err
	}
	data, err := os.ReadFile(paths.StateFile)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, i18n.Errorf("could not read the system region state: %w", err)
	}
	var state SystemState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, i18n.New("the system region state is invalid; inspect /var/lib/sbc/region and restore the system settings manually")
	}
	if state.Version != systemStateVersion {
		return nil, i18n.New("the system region state has an unsupported version; update sbc or restore the system settings manually")
	}
	if err := validateTimezoneSyntax(state.Profile.Timezone); err != nil {
		return nil, i18n.Errorf("the system region state is invalid: %w", err)
	}
	if _, _, err := splitLanguage(state.Profile.Language); err != nil {
		return nil, i18n.Errorf("the system region state is invalid: %w", err)
	}
	if err := validateSystemState(state); err != nil {
		return nil, err
	}
	return &state, nil
}

func validateSystemState(state SystemState) error {
	validateValue := func(name string, value SystemValue) error {
		switch value.Kind {
		case "missing":
			return nil
		case "symlink":
			if value.Target == "" {
				return i18n.Errorf("the saved %s symbolic link target is empty", name)
			}
		case "file":
			if value.SHA256 == "" {
				return i18n.Errorf("the saved %s file hash is empty", name)
			}
			if value.Backup != "" && filepath.Base(value.Backup) != value.Backup {
				return i18n.Errorf("the saved %s backup path is invalid", name)
			}
		default:
			return i18n.Errorf("the saved %s value kind %q is unknown", name, value.Kind)
		}
		return nil
	}
	if state.Timezone != nil {
		if err := validateTimezoneSyntax(state.Timezone.Target); err != nil {
			return err
		}
		if err := validateValue("time zone original", state.Timezone.Original); err != nil {
			return err
		}
		if err := validateValue("time zone written", state.Timezone.Written); err != nil {
			return err
		}
	}
	if state.Language != nil {
		if _, err := Locale(state.Language.Target); err != nil {
			return err
		}
		if err := validateValue("language original", state.Language.Original); err != nil {
			return err
		}
		if err := validateValue("language written", state.Language.Written); err != nil {
			return err
		}
	}
	return nil
}

func saveSystemState(paths SystemPaths, state *SystemState) error {
	if err := checkStateDir(paths); err != nil {
		return err
	}
	if err := os.MkdirAll(paths.StateDir, 0o755); err != nil {
		return err
	}
	// Umask values such as 077 would hide the state from status.
	if err := os.Chmod(paths.StateDir, 0o755); err != nil {
		return err
	}
	state.Version = systemStateVersion
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	if err := writeFileAtomic(paths.StateFile, append(data, '\n'), 0o644); err != nil {
		return i18n.Errorf("could not write the system region state: %w", err)
	}
	return nil
}

func removeSystemState(paths SystemPaths) error {
	var result error
	if err := os.Remove(paths.StateFile); err != nil && !os.IsNotExist(err) {
		result = errors.Join(result, err)
	}
	if err := os.RemoveAll(paths.BackupDir); err != nil {
		result = errors.Join(result, err)
	}
	// The parent /var/lib/sbc stays; this removes only the empty region
	// directory and ignores a directory another process still uses.
	_ = os.Remove(paths.StateDir)
	return result
}

func checkSystemPaths(paths SystemPaths) error {
	required := map[string]string{
		"localtime":      paths.Localtime,
		"timezone":       paths.Timezone,
		"locale default": paths.LocaleDefault,
		"locale conf":    paths.LocaleConf,
		"zone root":      paths.ZoneRoot,
		"state dir":      paths.StateDir,
		"state file":     paths.StateFile,
		"backup dir":     paths.BackupDir,
	}
	for name, value := range required {
		if value == "" {
			return i18n.Errorf("the system region path %s is not set", name)
		}
	}
	if filepath.Dir(filepath.Clean(paths.StateFile)) != filepath.Clean(paths.StateDir) ||
		filepath.Dir(filepath.Clean(paths.BackupDir)) != filepath.Clean(paths.StateDir) {
		return i18n.New("the system region state path is invalid")
	}
	return nil
}

func checkStateDir(paths SystemPaths) error {
	info, err := os.Lstat(paths.StateDir)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return i18n.Errorf("the system region state directory %s is not a directory", paths.StateDir)
	}
	if paths.StateDir == DefaultSystemPaths().StateDir {
		if !ownedByRoot(info) {
			return i18n.Errorf("the system region state directory %s is not owned by root", paths.StateDir)
		}
		if info.Mode().Perm()&0o022 != 0 {
			return i18n.Errorf("the system region state directory %s is writable by other users", paths.StateDir)
		}
	}
	return nil
}

func checkStateFile(path string) error {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return i18n.Errorf("the system region state file %s is not a regular file", path)
	}
	if path == DefaultSystemPaths().StateFile {
		if !ownedByRoot(info) {
			return i18n.Errorf("the system region state file %s is not owned by root", path)
		}
		if info.Mode().Perm()&0o022 != 0 {
			return i18n.Errorf("the system region state file %s is writable by other users", path)
		}
	}
	return nil
}

func validateSystemTimezone(paths SystemPaths, zone string) error {
	// Use the system zone root, not a user TZDIR or ZONEINFO value.
	if err := validateTimezoneSyntax(zone); err != nil {
		return err
	}
	info, err := os.Stat(filepath.Join(paths.ZoneRoot, zone))
	if err != nil || !info.Mode().IsRegular() {
		return i18n.Errorf("time zone %s is not installed; install the tzdata package and retry", zone)
	}
	return nil
}

// localeFilePath returns the path to pass to update-locale and the file whose
// bytes sbc backs up. On releases where /etc/default/locale is a symbolic link
// to /etc/locale.conf, both differ.
func localeFilePath(paths SystemPaths) (string, string, error) {
	if info, err := os.Lstat(paths.LocaleDefault); err == nil {
		if info.Mode()&os.ModeSymlink != 0 {
			resolved, err := filepath.EvalSymlinks(paths.LocaleDefault)
			if err != nil {
				return "", "", i18n.Errorf("the system locale file %s is a broken symbolic link", paths.LocaleDefault)
			}
			return paths.LocaleDefault, resolved, nil
		}
		return paths.LocaleDefault, paths.LocaleDefault, nil
	} else if !os.IsNotExist(err) {
		return "", "", err
	}
	if _, err := os.Lstat(paths.LocaleConf); err == nil {
		return paths.LocaleConf, paths.LocaleConf, nil
	}
	return paths.LocaleDefault, paths.LocaleDefault, nil
}

// captureSystemValue reads a path and, for a regular file, its contents.
func captureSystemValue(path string) (SystemValue, []byte, error) {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return SystemValue{Kind: "missing"}, nil, nil
	}
	if err != nil {
		return SystemValue{}, nil, err
	}
	switch {
	case info.Mode()&os.ModeSymlink != 0:
		target, err := os.Readlink(path)
		if err != nil {
			return SystemValue{}, nil, err
		}
		return SystemValue{Kind: "symlink", Target: target}, nil, nil
	case info.Mode().IsRegular():
		data, err := os.ReadFile(path)
		if err != nil {
			return SystemValue{}, nil, err
		}
		return SystemValue{Kind: "file", Mode: uint32(info.Mode().Perm()), SHA256: hashBytes(data)}, data, nil
	default:
		return SystemValue{}, nil, i18n.Errorf("%s is not a regular file or symbolic link", path)
	}
}

func readSystemValue(path string) (SystemValue, error) {
	value, _, err := captureSystemValue(path)
	return value, err
}

func sameSystemValue(a, b SystemValue) bool {
	if a.Kind != b.Kind {
		return false
	}
	switch a.Kind {
	case "missing":
		return true
	case "symlink":
		return a.Target == b.Target
	case "file":
		return a.SHA256 == b.SHA256
	default:
		return false
	}
}

func snapshotValue(paths SystemPaths, current SystemValue, data []byte, name string) (SystemValue, error) {
	backup := filepath.Join(paths.BackupDir, name)
	if current.Kind != "file" {
		if err := os.Remove(backup); err != nil && !os.IsNotExist(err) {
			return SystemValue{}, err
		}
		return current, nil
	}
	if err := os.MkdirAll(paths.BackupDir, 0o700); err != nil {
		return SystemValue{}, err
	}
	if err := writeFileAtomic(backup, data, 0o600); err != nil {
		return SystemValue{}, err
	}
	current.Backup = name
	return current, nil
}

func removeBackup(paths SystemPaths, value SystemValue) {
	if value.Backup == "" {
		return
	}
	_ = os.Remove(filepath.Join(paths.BackupDir, value.Backup))
}

func restoreSystemValue(paths SystemPaths, path string, value SystemValue) error {
	switch value.Kind {
	case "missing":
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	case "symlink":
		return replaceSymlink(path, value.Target)
	case "file":
		data, err := os.ReadFile(filepath.Join(paths.BackupDir, value.Backup))
		if err != nil {
			return i18n.Errorf("could not read the saved system region backup: %w", err)
		}
		if hashBytes(data) != value.SHA256 {
			return i18n.New("the saved system region backup is invalid")
		}
		return writeFileAtomic(path, data, os.FileMode(value.Mode))
	default:
		return i18n.Errorf("unknown saved system value kind %q", value.Kind)
	}
}

func restoreCaptured(path string, value SystemValue, data []byte) error {
	switch value.Kind {
	case "missing":
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	case "symlink":
		return replaceSymlink(path, value.Target)
	case "file":
		if hashBytes(data) != value.SHA256 {
			return i18n.New("the captured system value changed while sbc was working")
		}
		return writeFileAtomic(path, data, os.FileMode(value.Mode))
	default:
		return i18n.Errorf("unknown captured system value kind %q", value.Kind)
	}
}

func writeLocaleFile(path, locale, list string, mode os.FileMode) error {
	data, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	var lines []string
	if len(data) != 0 {
		lines = strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	}
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "LANG=") ||
			strings.HasPrefix(trimmed, "LANGUAGE=") ||
			strings.HasPrefix(trimmed, "LC_MESSAGES=") {
			lines[i] = "# " + line
		}
	}
	lines = append(lines, "LANG="+locale, "LANGUAGE="+list, "LC_MESSAGES="+locale)
	return writeFileAtomic(path, []byte(strings.Join(lines, "\n")+"\n"), mode)
}

func localeValue(data []byte, name string) string {
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if value, ok := strings.CutPrefix(line, name+"="); ok {
			return strings.Trim(strings.TrimSpace(value), `"'`)
		}
	}
	return ""
}

func timezoneName(paths SystemPaths, value SystemValue) string {
	switch value.Kind {
	case "missing":
		return ""
	case "file":
		return i18n.T("a custom file")
	case "symlink":
		target := value.Target
		if !filepath.IsAbs(target) {
			target = filepath.Join(filepath.Dir(paths.Localtime), target)
		}
		clean := filepath.Clean(target)
		zoneRoot := filepath.Clean(paths.ZoneRoot)
		if rel, ok := strings.CutPrefix(clean, zoneRoot+"/"); ok {
			return rel
		}
		if resolved, err := filepath.EvalSymlinks(paths.Localtime); err == nil {
			if rel, ok := strings.CutPrefix(resolved, zoneRoot+"/"); ok {
				return rel
			}
		}
		return target
	default:
		return ""
	}
}

func hashBytes(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func replaceSymlink(path, target string) error {
	dir := filepath.Dir(path)
	temp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".sbc-*")
	if err != nil {
		return err
	}
	name := temp.Name()
	if err := temp.Close(); err != nil {
		_ = os.Remove(name)
		return err
	}
	if err := os.Remove(name); err != nil {
		return err
	}
	if err := os.Symlink(target, name); err != nil {
		return err
	}
	if err := os.Rename(name, path); err != nil {
		_ = os.Remove(name)
		return err
	}
	return nil
}

func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	dir := filepath.Dir(path)
	temp, err := os.CreateTemp(dir, ".sbc-region-*")
	if err != nil {
		return err
	}
	name := temp.Name()
	defer func() { _ = os.Remove(name) }()
	if _, err := temp.Write(data); err != nil {
		_ = temp.Close()
		return err
	}
	if err := temp.Chmod(mode); err != nil {
		_ = temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}

type SystemTools struct {
	Timedatectl  string
	UpdateLocale string
}

func resolveSystemTools(look func(string) (string, error)) SystemTools {
	if look == nil {
		look = systemLookPath
	}
	var tools SystemTools
	if path, err := look("timedatectl"); err == nil {
		tools.Timedatectl = path
	}
	if path, err := look("update-locale"); err == nil {
		tools.UpdateLocale = path
	}
	return tools
}

func systemLookPath(name string) (string, error) {
	for _, dir := range []string{"/usr/sbin", "/usr/bin", "/sbin", "/bin"} {
		path := filepath.Join(dir, name)
		info, err := os.Stat(path)
		if err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0o111 != 0 {
			return path, nil
		}
	}
	return "", exec.ErrNotFound
}

func systemEnvironment() []string {
	return []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C"}
}

func runSystemCommand(name string, args ...string) error {
	command := exec.Command(name, args...)
	command.Env = systemEnvironment()
	output, err := command.CombinedOutput()
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			message = err.Error()
		}
		return i18n.Errorf("%s failed: %s", name, message)
	}
	return nil
}

func systemLocales() ([]string, error) {
	command := exec.Command("locale", "-a")
	command.Env = systemEnvironment()
	output, err := command.Output()
	if err != nil {
		return nil, err
	}
	var locales []string
	for _, line := range strings.Split(string(output), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			locales = append(locales, line)
		}
	}
	return locales, nil
}
