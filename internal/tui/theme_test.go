package tui

import (
	"bytes"
	"errors"
	"io"
	"strings"
	"testing"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/colorprofile"
	"github.com/charmbracelet/x/ansi"
)

func TestThemeUsesSemanticANSISlots(t *testing.T) {
	for _, profile := range []colorprofile.Profile{colorprofile.Unknown, colorprofile.NoTTY, colorprofile.ASCII,
		colorprofile.ANSI, colorprofile.ANSI256, colorprofile.TrueColor} {
		t.Run(profile.String(), func(t *testing.T) {
			th := NewTheme(profile)
			for _, role := range []struct {
				name  string
				style lipgloss.Style
				sgr   string
			}{
				{"primary", th.Primary, "36"}, {"success", th.Success, "32"},
				{"warning", th.Warning, "33"}, {"danger", th.Danger, "31"},
				{"header", th.Header, "1"}, {"key", th.Key, "1"},
				{"body", th.Base, ""}, {"muted", th.Muted, ""}, {"border", th.Border, ""},
			} {
				text := role.style.Render("值 value")
				if ansi.Strip(text) != "值 value" {
					t.Fatalf("%s changed content: %q", role.name, text)
				}
				if th.Styled() && role.sgr != "" {
					if !strings.Contains(text, "\x1b["+role.sgr+"m") {
						t.Fatalf("%s lost its ANSI slot: %q", role.name, text)
					}
				} else if strings.Contains(text, "\x1b") {
					t.Fatalf("%s emitted unexpected styling: %q", role.name, text)
				}
				if strings.Contains(text, "38;") || strings.Contains(text, "48;") || strings.Contains(text, "\x1b]") {
					t.Fatalf("%s does not inherit the terminal palette/background: %q", role.name, text)
				}
			}
		})
	}
}

func TestThemePlainContentParity(t *testing.T) {
	text := "服务：运行中\n代理：关闭\n错误：连接失败\nrefresh: missing\n"
	plain := NewTheme(colorprofile.NoTTY)
	colored := NewTheme(colorprofile.TrueColor)
	for _, width := range []int{0, 20, 40, 80} {
		for _, render := range []func(Theme) string{
			func(th Theme) string { return RenderPanel(th, width, text) },
			func(th Theme) string { return FormatColonLine(th, text, StatusKeyWidth) },
			func(th Theme) string {
				return RenderTableTheme(th, width, []string{"目标", "响应"}, [][]string{{"example.test", "1/1"}}, DiagnosticCellStyler)
			},
		} {
			want, got := render(plain), render(colored)
			if strings.Contains(want, "\x1b") || ansi.Strip(got) != want {
				t.Fatalf("style changed content at width %d:\nplain %q\ncolor %q", width, want, got)
			}
		}
	}
}

func TestTerminalEnvironmentUsesOnlyInjectedValues(t *testing.T) {
	t.Setenv("COLORTERM", "truecolor")
	t.Setenv("NO_COLOR", "1")
	env := terminalEnvironment(func(key string) string {
		if key == "TERM" {
			return "xterm"
		}
		return ""
	})
	if len(env) != 1 || env.getenv("TERM") != "xterm" || env.getenv("NO_COLOR") != "" || env.getenv("COLORTERM") != "" {
		t.Fatalf("injected environment inherited process values: %v", env)
	}
	out := NewOutput(nil, &bytes.Buffer{}, func(string) string { return "1" })
	if out.Theme.Styled() || out.Interactive() {
		t.Fatal("force-color variables enabled a buffer terminal")
	}
}

type oneFailedWrite struct {
	bytes.Buffer
	failed bool
}

func (w *oneFailedWrite) Write(p []byte) (int, error) {
	if !w.failed {
		w.failed = true
		return 0, io.ErrClosedPipe
	}
	return w.Buffer.Write(p)
}

func TestOutputRemembersWriteFailureAndStillAttemptsCleanup(t *testing.T) {
	writer := &oneFailedWrite{}
	out := NewOutput(nil, writer, nil)
	if _, err := out.Write([]byte("frame")); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatal(err)
	}
	if _, err := out.Write([]byte("cleanup")); err != nil {
		t.Fatal(err)
	}
	if writer.String() != "cleanup" || !errors.Is(out.Close(), io.ErrClosedPipe) {
		t.Fatal("write failure was lost or prevented cleanup")
	}
}
