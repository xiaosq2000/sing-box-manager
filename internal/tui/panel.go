package tui

import (
	"strings"
)

// RenderPanel aligns values across the complete panel, not one line at a time.
func RenderPanel(th Theme, width int, text string) string {
	text = SafeLines(text)
	lines := strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	keyWidth := StatusKeyWidth
	for _, line := range lines {
		if key, _, _, ok := splitColonKV(line); ok && !strings.HasPrefix(line, " ") {
			keyWidth = max(keyWidth, DisplayWidth(key)+1)
		}
	}
	var out strings.Builder
	for _, line := range lines {
		key, value, _, ok := splitColonKV(line)
		if ok && !strings.HasPrefix(line, " ") {
			if width > 0 && keyWidth+10 > width {
				line = th.Key.Render(key) + "\n" + StyleStatusValue(th, value)
			} else {
				line = th.Key.Render(PadRight(key, keyWidth)) + StyleStatusValue(th, value)
			}
		}
		out.WriteString(FitLines(line, width))
		out.WriteByte('\n')
	}
	return out.String()
}
