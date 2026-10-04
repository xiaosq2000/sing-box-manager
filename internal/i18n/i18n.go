// Package i18n puts sbc's messages in the user's language: Simplified Chinese
// where the locale asks for it, and English otherwise. The English message is
// the key, so a message without a translation still reads in English.
package i18n

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
)

// Chinese reports whether messages are in Simplified Chinese. SBC_LANG decides
// when it is set: zh for Chinese, anything else for English. Otherwise the
// first of LC_ALL, LC_MESSAGES and LANG that is set decides, as it does for
// other programs, and zh_CN or zh_SG means Chinese.
func Chinese(getenv func(string) string) bool {
	if lang := getenv("SBC_LANG"); lang != "" {
		return strings.HasPrefix(lang, "zh")
	}
	for _, name := range []string{"LC_ALL", "LC_MESSAGES", "LANG"} {
		if value := getenv(name); value != "" {
			return strings.HasPrefix(value, "zh_CN") || strings.HasPrefix(value, "zh_SG")
		}
	}
	return false
}

// chinese is decided once, before any message is made. Tests see English
// whatever the locale of the machine that runs them, unless they ask for
// Chinese.
var chinese = !testing.Testing() && Chinese(os.Getenv)

// SetChinese switches the language, for tests.
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
