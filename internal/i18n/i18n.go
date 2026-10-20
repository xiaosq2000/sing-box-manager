// Package i18n puts sbc's messages in the user's language: Simplified Chinese
// where SBC_LANG, a saved choice, or the locale asks for it, and English
// otherwise. The English message is the key, so a message without a
// translation still reads in English.
package i18n

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
)

// Source names what selected the current language.
type Source string

const (
	SourceSetting     Source = "setting"
	SourceEnvironment Source = "SBC_LANG"
	SourceLocale      Source = "locale"
	SourceDefault     Source = "default"
)

// Resolve reports whether messages are in Simplified Chinese and what selected
// the language. SBC_LANG decides when it is set: zh for Chinese, anything else
// for English. A saved setting of en or zh comes next. Otherwise the first of
// LC_ALL, LC_MESSAGES and LANG that is set decides, as it does for other
// programs, and zh_CN or zh_SG means Chinese.
func Resolve(getenv func(string) string, saved string) (bool, Source) {
	if lang := getenv("SBC_LANG"); lang != "" {
		return strings.HasPrefix(lang, "zh"), SourceEnvironment
	}
	switch saved {
	case "zh":
		return true, SourceSetting
	case "en":
		return false, SourceSetting
	}
	for _, name := range []string{"LC_ALL", "LC_MESSAGES", "LANG"} {
		if value := getenv(name); value != "" {
			return strings.HasPrefix(value, "zh_CN") || strings.HasPrefix(value, "zh_SG"), SourceLocale
		}
	}
	return false, SourceDefault
}

// Chinese reports whether messages are in Simplified Chinese under the
// environment alone.
func Chinese(getenv func(string) string) bool {
	on, _ := Resolve(getenv, "")
	return on
}

// Use selects the language for this process from the environment and a saved
// setting, and reports what selected it.
func Use(getenv func(string) string, saved string) Source {
	on, source := Resolve(getenv, saved)
	chinese = on
	return source
}

// chinese starts from the environment. Tests see English whatever the locale
// of the machine that runs them, unless they ask for Chinese.
var chinese = !testing.Testing() && Chinese(os.Getenv)

// SetChinese switches the language until the next Use.
func SetChinese(on bool) { chinese = on }

// T returns message in the user's language.
func T(message string) string {
	if chinese {
		if translated, ok := zh[message]; ok {
			return translated
		}
	}
	return message
}

// Pick returns zhText when messages are in Chinese, and enText otherwise. It
// serves long texts that live beside their code, such as the usage.
func Pick(enText, zhText string) string {
	if chinese {
		return zhText
	}
	return enText
}

// New returns an error whose text is message in the user's language.
func New(message string) error { return errors.New(T(message)) }

// Errorf formats an error from format in the user's language. Like
// fmt.Errorf, it wraps an error given for %w.
func Errorf(format string, args ...any) error { return fmt.Errorf(T(format), args...) }
