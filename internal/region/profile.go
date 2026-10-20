// Package region matches the desktop time zone and language to the region the
// portal assigns to the active node.
package region

import (
	"path"
	"regexp"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Profile is the time zone and language one node asks clients to present.
type Profile struct {
	Timezone string `json:"timezone"`
	Language string `json:"language"`
}

// State is the profile the portal last sent, with the node it belongs to.
type State struct {
	Node    string  `json:"node"`
	Profile Profile `json:"profile"`
}

// Mode says which parts of the profile the user enabled independently.
type Mode struct {
	Timezone bool `json:"timezone"`
	Language bool `json:"language"`
}

// Any reports whether at least one component is enabled.
func (m Mode) Any() bool { return m.Timezone || m.Language }

var (
	timezonePattern = regexp.MustCompile(`^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$`)
	languagePattern = regexp.MustCompile(`^([A-Za-z]{2,3})-([A-Za-z]{2}|[0-9]{3})$`)
)

// Normalize trims and validates a profile, and writes the language in its
// canonical case. It rejects names that could escape the zoneinfo directory.
func (p Profile) Normalize() (Profile, error) {
	timezone := strings.TrimSpace(p.Timezone)
	if err := validateTimezoneName(timezone); err != nil {
		return Profile{}, err
	}
	language, region, err := splitLanguage(p.Language)
	if err != nil {
		return Profile{}, err
	}
	return Profile{Timezone: timezone, Language: language + "-" + region}, nil
}

func validateTimezoneName(value string) error {
	if value == "" || value == "Local" || value == "localtime" {
		return i18n.Errorf("time zone %q is not a valid IANA name", value)
	}
	if strings.HasPrefix(value, "posix/") || strings.HasPrefix(value, "right/") {
		return i18n.Errorf("time zone %q is not a valid IANA name", value)
	}
	if !timezonePattern.MatchString(value) || path.Clean(value) != value {
		return i18n.Errorf("time zone %q is not a valid IANA name", value)
	}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." || part == ".." {
			return i18n.Errorf("time zone %q is not a valid IANA name", value)
		}
	}
	if _, err := time.LoadLocation(value); err != nil {
		return i18n.Errorf("time zone %q is not a valid IANA name", value)
	}
	return nil
}

func splitLanguage(value string) (string, string, error) {
	value = strings.TrimSpace(value)
	match := languagePattern.FindStringSubmatch(value)
	if match == nil {
		return "", "", i18n.Errorf("language %q must look like zh-CN, with a region", value)
	}
	return strings.ToLower(match[1]), strings.ToUpper(match[2]), nil
}

// Locale returns the POSIX locale for a BCP 47 tag, such as de_DE.UTF-8.
func Locale(language string) (string, error) {
	lang, region, err := splitLanguage(language)
	if err != nil {
		return "", err
	}
	return lang + "_" + region + ".UTF-8", nil
}

// LanguageList returns the gettext LANGUAGE value for a BCP 47 tag, such as
// de_DE:de.
func LanguageList(language string) (string, error) {
	lang, region, err := splitLanguage(language)
	if err != nil {
		return "", err
	}
	return lang + "_" + region + ":" + lang, nil
}

// localeKey makes the spellings locale -a uses comparable.
func localeKey(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	return strings.ReplaceAll(value, ".utf8", ".utf-8")
}

// localeAvailable reports whether locale -a listed the POSIX locale.
func localeAvailable(locale string, available []string) bool {
	wanted := localeKey(locale)
	for _, name := range available {
		if localeKey(name) == wanted {
			return true
		}
	}
	return false
}

func missingLocale(locale string) error {
	return i18n.Errorf(
		"the locale %s is not generated; for example, run 'sudo locale-gen %s', then retry",
		locale,
		locale,
	)
}
