package region

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/shell"
)

// Manager owns the user-scoped region files. Its fields are settable so tests
// can use a temporary home and a fake systemctl.
type Manager struct {
	StateFile        string
	ModeFile         string
	ShellFile        string
	EnvironmentDFile string
	ZoneRoots        []string
	Locales          func() ([]string, error)
	Run              func(name string, args ...string) error
	Getenv           func(string) string
	Supported        bool
}

// New returns the manager for one user's layout.
func New(layout paths.Layout, getenv func(string) string) *Manager {
	roots := []string{"/usr/share/zoneinfo"}
	if dir := getenv("TZDIR"); dir != "" {
		roots = []string{dir}
	}
	return &Manager{
		StateFile:        layout.RegionState(),
		ModeFile:         layout.RegionMode(),
		ShellFile:        layout.RegionEnv(),
		EnvironmentDFile: layout.RegionEnvironmentD(),
		ZoneRoots:        roots,
		Locales:          systemLocales,
		Run:              runCommand,
		Getenv:           getenv,
		Supported:        runtime.GOOS == "linux",
	}
}

// Status describes what is saved, what is available, and whether the files
// still match the saved choice.
type Status struct {
	Mode              Mode
	State             *State
	TimezoneAvailable bool
	TimezoneError     error
	LanguageAvailable bool
	LanguageError     error
	Locale            string
	FilesCurrent      bool
	MissingState      bool
	LCAll             string
	Updated           time.Time
}

// ApplyResult reports which components took effect. A component can fail
// independently, so a call can apply the time zone and report a language error.
type ApplyResult struct {
	Mode           Mode
	TimezoneError  error
	LanguageError  error
	ManagerWarning error
	LCAll          string
}

// Load reads the last profile and the saved mode.
func (m *Manager) Load() (*State, Mode, error) {
	state, err := m.loadState()
	if err != nil {
		return nil, Mode{}, err
	}
	mode, err := m.loadMode()
	if err != nil {
		return nil, Mode{}, err
	}
	if state != nil {
		normalized, err := state.Profile.Normalize()
		if err != nil {
			return nil, Mode{}, i18n.Errorf("the saved region profile is invalid; run 'sbc refresh': %w", err)
		}
		state.Profile = normalized
	}
	return state, mode, nil
}

// StoreState saves the profile the portal sent, without enabling it.
func (m *Manager) StoreState(state State) error {
	normalized, err := state.Profile.Normalize()
	if err != nil {
		return err
	}
	state.Profile = normalized
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	return paths.WriteFile(m.StateFile, append(data, '\n'), 0o600)
}

// Apply enables the requested components. A component whose local data is
// unavailable stays off, and the returned result names the reason.
func (m *Manager) Apply(state State, requested Mode) (ApplyResult, error) {
	if !m.Supported {
		return ApplyResult{}, i18n.New("region matching is available on Linux only")
	}
	normalized, err := state.Profile.Normalize()
	if err != nil {
		return ApplyResult{}, err
	}
	state.Profile = normalized
	if err := m.StoreState(state); err != nil {
		return ApplyResult{}, err
	}
	oldMode, err := m.loadMode()
	if err != nil {
		// A corrupt mode file still leaves variables to clear. Clearing every
		// component is the safe cleanup when its contents are unknown.
		oldMode = Mode{Timezone: true, Language: true}
	} else if !oldMode.Any() && (fileExists(m.ShellFile) || fileExists(m.EnvironmentDFile)) {
		// Leftover files may have set variables that the mode no longer names.
		oldMode = Mode{Timezone: true, Language: true}
	}
	result := ApplyResult{LCAll: m.Getenv("LC_ALL")}
	applied := Mode{}
	if requested.Timezone {
		if err := m.checkTimezone(normalized.Timezone); err != nil {
			result.TimezoneError = err
		} else {
			applied.Timezone = true
		}
	}
	if requested.Language {
		locale, err := Locale(normalized.Language)
		if err != nil {
			result.LanguageError = err
		} else if err := m.checkLocale(locale); err != nil {
			result.LanguageError = err
		} else {
			applied.Language = true
		}
	}
	result.Mode = applied
	if err := m.saveMode(applied); err != nil {
		return result, err
	}
	if applied.Any() {
		if err := m.writeShell(applied, normalized); err != nil {
			return result, err
		}
		if err := m.writeEnvironmentD(applied, normalized); err != nil {
			return result, err
		}
	} else if err := m.removeFiles(); err != nil {
		return result, err
	}
	if err := m.updateManagerEnvironment(applied, oldMode, normalized); err != nil {
		result.ManagerWarning = err
	}
	return result, nil
}

// Disable removes the saved choice and the files that apply it. The last
// profile stays cached so 'on' works without another download.
func (m *Manager) Disable() (ApplyResult, error) {
	if !m.Supported {
		return ApplyResult{}, i18n.New("region matching is available on Linux only")
	}
	result := ApplyResult{LCAll: m.Getenv("LC_ALL")}
	mode, err := m.loadMode()
	if err != nil {
		// A corrupt mode file still leaves owned files to remove. Clearing all
		// four variables is the safe cleanup when its contents are unknown.
		mode = Mode{Timezone: true, Language: true}
	}
	hadFiles := fileExists(m.ShellFile) || fileExists(m.EnvironmentDFile)
	if !mode.Any() && hadFiles {
		// Files without a mode may have set variables; clear every owned one.
		mode = Mode{Timezone: true, Language: true}
	}
	if err := m.saveMode(Mode{}); err != nil {
		return result, err
	}
	if err := m.removeFiles(); err != nil {
		return result, err
	}
	if mode.Any() {
		if err := m.updateManagerEnvironment(Mode{}, mode, Profile{}); err != nil {
			result.ManagerWarning = err
		}
	}
	return result, nil
}

// Remove deletes everything sbc owns for this feature, including the cached
// profile. Uninstall uses it.
func (m *Manager) Remove() (ApplyResult, error) {
	result, err := m.Disable()
	if err != nil {
		return result, err
	}
	if err := os.Remove(m.StateFile); err != nil && !os.IsNotExist(err) {
		return result, err
	}
	return result, nil
}

// Status reports the saved choice and whether the local environment can apply
// it. It changes nothing.
func (m *Manager) Status() (Status, error) {
	if !m.Supported {
		return Status{}, i18n.New("region matching is available on Linux only")
	}
	state, mode, err := m.Load()
	if err != nil {
		return Status{}, err
	}
	status := Status{
		Mode:  mode,
		State: state,
		LCAll: m.Getenv("LC_ALL"),
	}
	if state != nil {
		if info, err := os.Stat(m.StateFile); err == nil {
			status.Updated = info.ModTime()
		}
	}
	if state == nil {
		status.MissingState = mode.Any()
		status.FilesCurrent = !fileExists(m.ShellFile) && !fileExists(m.EnvironmentDFile)
		return status, nil
	}
	status.TimezoneAvailable = true
	if err := m.checkTimezone(state.Profile.Timezone); err != nil {
		status.TimezoneAvailable = false
		status.TimezoneError = err
	}
	locale, err := Locale(state.Profile.Language)
	if err != nil {
		status.LanguageError = err
	} else {
		status.Locale = locale
		if err := m.checkLocale(locale); err != nil {
			status.LanguageError = err
		} else {
			status.LanguageAvailable = true
		}
	}
	status.FilesCurrent = m.filesCurrent(mode, state.Profile)
	return status, nil
}

func (m *Manager) loadState() (*State, error) {
	data, err := os.ReadFile(m.StateFile)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, i18n.Errorf("could not read the saved region profile: %w", err)
	}
	var state State
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, i18n.Errorf("could not read the saved region profile; run 'sbc refresh': %w", err)
	}
	return &state, nil
}

func (m *Manager) loadMode() (Mode, error) {
	data, err := os.ReadFile(m.ModeFile)
	if os.IsNotExist(err) {
		return Mode{}, nil
	}
	if err != nil {
		return Mode{}, i18n.Errorf("could not read the region mode: %w", err)
	}
	var mode Mode
	if err := json.Unmarshal(data, &mode); err != nil {
		return Mode{}, i18n.Errorf("could not read the region mode; run 'sbc privacy region off': %w", err)
	}
	return mode, nil
}

func (m *Manager) saveMode(mode Mode) error {
	if !mode.Any() {
		if err := os.Remove(m.ModeFile); err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	}
	data, err := json.Marshal(mode)
	if err != nil {
		return err
	}
	return paths.WriteFile(m.ModeFile, append(data, '\n'), 0o600)
}

func (m *Manager) writeShell(mode Mode, profile Profile) error {
	return paths.WriteFile(m.ShellFile, []byte(shellContent(mode, profile)), 0o644)
}

func (m *Manager) writeEnvironmentD(mode Mode, profile Profile) error {
	return paths.WriteFile(m.EnvironmentDFile, []byte(environmentContent(mode, profile)), 0o644)
}

func (m *Manager) removeFiles() error {
	var result error
	for _, path := range []string{m.ShellFile, m.EnvironmentDFile} {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			result = errors.Join(result, err)
		}
	}
	return result
}

func (m *Manager) checkTimezone(value string) error {
	if err := validateTimezoneName(value); err != nil {
		return err
	}
	for _, root := range m.ZoneRoots {
		info, err := os.Stat(filepath.Join(root, value))
		if err == nil && info.Mode().IsRegular() {
			return nil
		}
	}
	return i18n.Errorf("time zone %s is not installed; install the tzdata package and retry", value)
}

func (m *Manager) checkLocale(locale string) error {
	available, err := m.Locales()
	if err != nil {
		return i18n.Errorf("could not list the installed locales: %w", err)
	}
	if !localeAvailable(locale, available) {
		return missingLocale(locale)
	}
	return nil
}

func (m *Manager) updateManagerEnvironment(mode, oldMode Mode, profile Profile) error {
	var enabled, disabled []string
	if mode.Timezone {
		enabled = append(enabled, "TZ="+profile.Timezone)
	} else if oldMode.Timezone {
		disabled = append(disabled, "TZ")
	}
	if mode.Language {
		locale, err := Locale(profile.Language)
		if err != nil {
			return err
		}
		list, err := LanguageList(profile.Language)
		if err != nil {
			return err
		}
		enabled = append(enabled, "LANG="+locale, "LANGUAGE="+list, "LC_MESSAGES="+locale)
	} else if oldMode.Language {
		disabled = append(disabled, "LANG", "LANGUAGE", "LC_MESSAGES")
	}
	var result error
	if len(enabled) > 0 {
		args := append([]string{"--user", "set-environment"}, enabled...)
		if err := m.Run("systemctl", args...); err != nil {
			result = errors.Join(result, err)
		}
	}
	if len(disabled) > 0 {
		args := append([]string{"--user", "unset-environment"}, disabled...)
		if err := m.Run("systemctl", args...); err != nil {
			result = errors.Join(result, err)
		}
	}
	return result
}

func (m *Manager) filesCurrent(mode Mode, profile Profile) bool {
	if !mode.Any() {
		return !fileExists(m.ShellFile) && !fileExists(m.EnvironmentDFile)
	}
	if !fileExists(m.ShellFile) || !fileExists(m.EnvironmentDFile) {
		return false
	}
	shellData, err := os.ReadFile(m.ShellFile)
	if err != nil || string(shellData) != shellContent(mode, profile) {
		return false
	}
	environmentData, err := os.ReadFile(m.EnvironmentDFile)
	return err == nil && string(environmentData) == environmentContent(mode, profile)
}

func shellContent(mode Mode, profile Profile) string {
	var text strings.Builder
	if mode.Timezone {
		text.WriteString("_sbc_region_tz=1\n")
		fmt.Fprintf(&text, "export TZ=%s\n", shell.Quote(profile.Timezone))
	} else {
		text.WriteString("unset _sbc_region_tz\n")
	}
	if mode.Language {
		text.WriteString("_sbc_region_lang=1\n")
		locale, _ := Locale(profile.Language)
		list, _ := LanguageList(profile.Language)
		fmt.Fprintf(&text, "export LANG=%s\n", shell.Quote(locale))
		fmt.Fprintf(&text, "export LANGUAGE=%s\n", shell.Quote(list))
		fmt.Fprintf(&text, "export LC_MESSAGES=%s\n", shell.Quote(locale))
	} else {
		text.WriteString("unset _sbc_region_lang\n")
	}
	return text.String()
}

func environmentContent(mode Mode, profile Profile) string {
	var text strings.Builder
	if mode.Timezone {
		fmt.Fprintf(&text, "TZ=%s\n", profile.Timezone)
	}
	if mode.Language {
		locale, _ := Locale(profile.Language)
		list, _ := LanguageList(profile.Language)
		fmt.Fprintf(&text, "LANG=%s\n", locale)
		fmt.Fprintf(&text, "LANGUAGE=%s\n", list)
		fmt.Fprintf(&text, "LC_MESSAGES=%s\n", locale)
	}
	return text.String()
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

func systemLocales() ([]string, error) {
	output, err := exec.Command("locale", "-a").Output()
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

func runCommand(name string, args ...string) error {
	command := exec.Command(name, args...)
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return i18n.Errorf("%s failed: %s", name, message)
	}
	return nil
}
