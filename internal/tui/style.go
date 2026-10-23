// Package tui renders CJK-aligned tables, key-value panels, and interactive
// BubbleTea views for the sbc CLI.
package tui

import (
	"strings"
	"unicode"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/colorprofile"
)

// StatusKeyWidth is the display column width used by sbc status key labels in
// both English and Simplified Chinese.
const StatusKeyWidth = 10

// Theme contains semantic ANSI styles for one effective color profile.
type Theme struct {
	profile colorprofile.Profile
	Base    lipgloss.Style
	Header  lipgloss.Style
	Key     lipgloss.Style
	Primary lipgloss.Style
	Success lipgloss.Style
	Warning lipgloss.Style
	Danger  lipgloss.Style
	Muted   lipgloss.Style
	Border  lipgloss.Style
}

// NewTheme builds styles without reading the terminal or its background.
// ANSI slots retain the user's palette even on truecolor terminals.
func NewTheme(profile colorprofile.Profile) Theme {
	base := lipgloss.NewStyle()
	th := Theme{profile: profile, Base: base, Header: base, Key: base,
		Primary: base, Success: base, Warning: base, Danger: base, Muted: base, Border: base}
	if th.Styled() {
		th.Header, th.Key = base.Bold(true), base.Bold(true)
		th.Primary = base.Foreground(lipgloss.Cyan)
		th.Success = base.Foreground(lipgloss.Green)
		th.Warning = base.Foreground(lipgloss.Yellow)
		th.Danger = base.Foreground(lipgloss.Red)
	}
	return th
}

// Styled reports whether this profile permits colors and other SGR attributes.
func (th Theme) Styled() bool { return th.profile > colorprofile.ASCII }

// DisplayWidth returns the visible terminal column width of s, accounting for
// East Asian wide characters (2 cells per CJK ideograph or fullwidth mark) and
// ignoring ANSI escape sequences.
func DisplayWidth(s string) int {
	return lipgloss.Width(s)
}

// PadRight pads s with trailing spaces so its terminal display width is at
// least width columns.
func PadRight(s string, width int) string {
	w := DisplayWidth(s)
	if w >= width {
		return s
	}
	return s + strings.Repeat(" ", width-w)
}

// PadLeft pads s with leading spaces so its terminal display width is at least
// width columns.
func PadLeft(s string, width int) string {
	w := DisplayWidth(s)
	if w >= width {
		return s
	}
	return strings.Repeat(" ", width-w) + s
}

// FormatKV formats a key-value row with key padded to keyWidth display columns.
// Styled themes highlight the key and recognized status values.
// Plain themes retain the same aligned text.
func FormatKV(th Theme, key string, keyWidth int, value string) string {
	key, value = SafeText(key), SafeText(value)
	pad := 0
	if kw := DisplayWidth(key); kw < keyWidth {
		pad = keyWidth - kw
	}
	styledKey := key
	styledVal := value
	if th.Styled() {
		styledKey = th.Key.Render(key)
		styledVal = StyleStatusValue(th, value)
	}
	return styledKey + strings.Repeat(" ", pad) + styledVal
}

// FormatColonLine formats one or more newline-separated lines that start with a
// "key: value" or "键：值" label. When keyWidth > 0, each key is padded to
// keyWidth display columns so English and Chinese rows align vertically.
func FormatColonLine(th Theme, text string, keyWidth int) string {
	trailingNewline := strings.HasSuffix(text, "\n")
	rawLines := strings.Split(strings.TrimSuffix(SafeLines(text), "\n"), "\n")
	out := make([]string, 0, len(rawLines))
	for _, line := range rawLines {
		key, val, sep, ok := splitColonKV(line)
		if !ok {
			if th.Styled() {
				out = append(out, th.Warning.Render(line))
			} else {
				out = append(out, line)
			}
			continue
		}
		if keyWidth > 0 {
			out = append(out, FormatKV(th, key, keyWidth, val))
			continue
		}
		if th.Styled() {
			out = append(out, th.Key.Render(key)+sep+StyleStatusValue(th, val))
		} else {
			out = append(out, key+sep+val)
		}
	}
	res := strings.Join(out, "\n")
	if trailingNewline {
		res += "\n"
	}
	return res
}

func splitColonKV(line string) (key, val, sep string, ok bool) {
	idxASCII := strings.IndexByte(line, ':')
	idxFull := strings.Index(line, "：")
	if idxASCII < 0 && idxFull < 0 {
		return "", "", "", false
	}
	if idxFull >= 0 && (idxASCII < 0 || idxFull < idxASCII) {
		key = line[:idxFull+len("：")]
		val = strings.TrimLeft(line[idxFull+len("："):], " ")
		return key, val, "", isLabel(line[:idxFull])
	}
	key = line[:idxASCII+1]
	val = strings.TrimLeft(line[idxASCII+1:], " ")
	return key, val, " ", isLabel(line[:idxASCII])
}

func isLabel(s string) bool {
	if s == "" || strings.TrimSpace(s) != s {
		return false
	}
	for _, r := range s {
		if !unicode.IsLetter(r) && !unicode.IsDigit(r) && r != ' ' && r != '-' && r != '_' {
			return false
		}
	}
	return true
}

// StyleStatusValue highlights status tokens when the theme permits styling.
func StyleStatusValue(th Theme, value string) string {
	if !th.Styled() {
		return value
	}
	lower := strings.ToLower(strings.TrimSpace(value))
	switch {
	case lower == "running" || lower == "on" || lower == "active" || lower == "verified" || lower == "current" || lower == "available" ||
		strings.HasPrefix(lower, "running ") || strings.HasPrefix(lower, "active (") || strings.HasPrefix(lower, "on (") ||
		value == "运行中" || value == "开启" || value == "已开启" || value == "已验证" || value == "当前" || value == "可用" ||
		strings.HasPrefix(value, "运行中（") || strings.HasPrefix(value, "开启（") || strings.HasPrefix(value, "已开启（"):
		return th.Success.Render(value)
	case lower == "off" || lower == "stopped" || lower == "not set up" ||
		strings.HasPrefix(lower, "off (") ||
		value == "关闭" || value == "已关闭" || value == "已停止" || value == "未设置" ||
		strings.HasPrefix(value, "关闭（") || strings.HasPrefix(value, "已关闭（"):
		return th.Muted.Render(value)
	case strings.HasPrefix(lower, "dirty") || strings.Contains(lower, "failed") || strings.Contains(lower, "not reachable") ||
		strings.HasPrefix(value, "状态异常") || strings.Contains(value, "失败") || strings.Contains(value, "无法访问"):
		return th.Danger.Render(value)
	case strings.Contains(lower, "out of date") || strings.Contains(lower, "available; run") || strings.Contains(lower, "missing") ||
		strings.Contains(value, "已过期") || strings.Contains(value, "可用，运行") || strings.Contains(value, "有可用更新"):
		return th.Warning.Render(value)
	default:
		return value
	}
}
