//go:build linux

package cli

import (
	"regexp"
	"strings"
	"testing"
)

func TestTerminalForegroundOutputDoesNotProbeBackground(t *testing.T) {
	for _, kind := range []string{"help", "shell", "completion", "ip-json"} {
		for _, color := range []string{"", "1"} {
			t.Run(kind+"/no-color="+color, func(t *testing.T) {
				s := startTerminal(t, kind, "pty", "pty", "xterm-256color", color, 120, 30, terminalSettings{foreground: true})
				if result := s.finish(t); result.Code != 0 {
					t.Fatalf("command result: %+v", result)
				}
				text := s.output.text()
				if strings.Contains(text, "\x1b]11;") || strings.Contains(text, "\x1b[6n") {
					t.Fatalf("command probed the foreground terminal: %q", text)
				}
				if (kind != "help" || color != "") && strings.Contains(text, "\x1b") {
					t.Fatalf("machine or plain output emitted terminal controls: %q", text)
				}
			})
		}
	}
}

func TestTerminalForegroundColorPolicy(t *testing.T) {
	for _, tc := range []struct {
		name, term, color, output string
		env                       map[string]string
		styled                    bool
	}{
		{name: "truecolor", term: "xterm-256color", styled: true},
		{name: "256-color", term: "xterm-256color", env: map[string]string{"COLORTERM": ""}, styled: true},
		{name: "16-color", term: "xterm", env: map[string]string{"COLORTERM": ""}, styled: true},
		{name: "light-background", term: "xterm", env: map[string]string{"COLORFGBG": "0;15"}, styled: true},
		{name: "dark-background", term: "xterm", env: map[string]string{"COLORFGBG": "15;0"}, styled: true},
		{name: "no-color", term: "xterm", color: "1"},
		{name: "no-color-zero", term: "xterm", color: "0"},
		{name: "clicolor-off", term: "xterm", env: map[string]string{"CLICOLOR": "0"}},
		{name: "force-clicolor", term: "xterm", env: map[string]string{"CLICOLOR": "0", "CLICOLOR_FORCE": "2"}, styled: true},
		{name: "dumb-force", term: "dumb", env: map[string]string{"CLICOLOR_FORCE": "1"}},
		{name: "pipe-force", term: "xterm", output: "pipe", env: map[string]string{"CLICOLOR_FORCE": "1"}},
		{name: "unknown", env: map[string]string{"COLORTERM": ""}},
		{name: "injected-no-color", term: "xterm", env: map[string]string{"SBC_TTY_INJECT_NO_COLOR": "1"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			output := tc.output
			if output == "" {
				output = "pty"
			}
			s := startTerminal(t, "help", "pty", output, tc.term, tc.color, 120, 30, terminalSettings{foreground: true, env: tc.env})
			if result := s.finish(t); result.Code != 0 {
				t.Fatal(result)
			}
			text := s.output.text()
			hasSGR := regexp.MustCompile(`\x1b\[[0-9;:]*m`).MatchString(text)
			if hasSGR != tc.styled {
				t.Fatalf("styled=%v, want %v: %q", hasSGR, tc.styled, text)
			}
			if tc.styled && !strings.Contains(text, "\x1b[36m") {
				t.Fatalf("help did not reference the user's cyan slot: %q", text)
			}
			if strings.Contains(text, "\x1b]") || strings.Contains(text, "\x1b[6n") || strings.Contains(text, "38;") || strings.Contains(text, "48;") {
				t.Fatalf("help probed or overrode the theme: %q", text)
			}
			if !tc.styled && strings.Contains(text, "\x1b") {
				t.Fatalf("plain help emitted controls: %q", text)
			}
		})
	}
}

func TestTerminalForegroundPlainSelectorKeepsInteraction(t *testing.T) {
	for _, tc := range []struct {
		name, term, color string
		env               map[string]string
	}{
		{name: "no-color", term: "xterm", color: "1"},
		{name: "no-color-zero", term: "xterm", color: "0"},
		{name: "unknown-color", env: map[string]string{"COLORTERM": ""}},
		{name: "clicolor-off", term: "xterm", env: map[string]string{"CLICOLOR": "0"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := startTerminal(t, "", "pty", "pty", tc.term, tc.color, 80, 24, terminalSettings{foreground: true, env: tc.env})
			s.waitText(t, "> ")
			s.master.Write([]byte("\x1b[B\r"))
			if result := s.finish(t); result.Code != 0 || result.Mode != "gfw" {
				t.Fatal(result)
			}
			if regexp.MustCompile(`\x1b\[[0-9;:]*m`).MatchString(s.output.text()) {
				t.Fatal("plain selector emitted SGR")
			}
		})
	}
}
