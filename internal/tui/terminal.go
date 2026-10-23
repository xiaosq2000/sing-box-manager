package tui

import (
	"io"
	"os"
	"strings"
	"unicode"

	"github.com/charmbracelet/x/ansi"
	"github.com/charmbracelet/x/term"
)

func lookup(getenv func(string) string, key string) string {
	if getenv != nil {
		return getenv(key)
	}
	return os.Getenv(key)
}

// IsTerminal tests the file descriptor, not the device file type.
func IsTerminal(v any) bool {
	f, ok := v.(*os.File)
	return ok && f != nil && term.IsTerminal(f.Fd())
}

// Interactive requires terminal input, terminal output, and cursor control.
// NO_COLOR changes colors only. It does not disable interaction.
func Interactive(in io.Reader, out io.Writer, getenv func(string) string) bool {
	return IsTerminal(in) && IsTerminal(out) && lookup(getenv, "TERM") != "dumb"
}

// TerminalSize returns zero dimensions for files and pipes.
func TerminalSize(w io.Writer) (int, int) {
	if f, ok := w.(*os.File); ok && IsTerminal(f) {
		width, height, err := term.GetSize(f.Fd())
		if err == nil {
			return width, height
		}
	}
	return 0, 0
}

// SafeText removes terminal commands and format controls from one display cell.
// Keep the original value separately for API requests.
func SafeText(s string) string {
	return cleanText(s, false)
}

// SafeLines keeps intentional line breaks in formatted human output.
func SafeLines(s string) string {
	return cleanText(s, true)
}

func cleanText(s string, lines bool) string {
	return strings.Map(func(r rune) rune {
		if r == '\n' && lines {
			return r
		}
		if r == '\n' || r == '\t' || r == '\r' {
			return ' '
		}
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			return -1
		}
		return r
	}, ansi.Strip(s))
}

// FitLines wraps text without removing visible content.
func FitLines(s string, width int) string {
	if width <= 0 {
		return s
	}
	return ansi.Hardwrap(s, width, false)
}
