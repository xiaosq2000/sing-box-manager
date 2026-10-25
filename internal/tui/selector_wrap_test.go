package tui

import (
	"fmt"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/colorprofile"
	"github.com/charmbracelet/x/ansi"
)

var selectorDescriptions = []string{
	"侧重网络波动时的传输速度，但可能更耗流量，若所在网络屏蔽或限速 UDP，可能无法连接或速度受限。",
	"Prioritizes speed on unstable networks but may use more data, fail to connect if the network blocks UDP, or lose speed if it throttles UDP.",
}

func TestSelectorWrapsDescriptionsWithHangingIndent(t *testing.T) {
	for _, profile := range []colorprofile.Profile{colorprofile.NoTTY, colorprofile.ANSI256} {
		for _, width := range []int{60, 80, 100} {
			for language, description := range selectorDescriptions {
				t.Run(fmt.Sprintf("%d/%d/%d", profile, width, language), func(t *testing.T) {
					m := NewSelectorModel(NewTheme(profile), "", "（当前）", "", []SelectorItem{
						{Value: "snell", Current: true},
						{Value: "hysteria2", Description: description},
						{Value: "naive"},
					})
					m.Width, m.Cursor = width, 1
					lines, starts := m.itemLines()
					wrapped := lines[starts[1]:starts[2]]
					labelWidth := DisplayWidth("snell （当前）")
					firstPrefix := "> " + PadRight("hysteria2", labelWidth) + "  "
					continuation := strings.Repeat(" ", DisplayWidth(firstPrefix))
					if len(wrapped) < 2 {
						t.Fatalf("fixture did not wrap: %q", wrapped)
					}
					var bodies []string
					for i, line := range wrapped {
						prefix := continuation
						if i == 0 {
							prefix = firstPrefix
						}
						plain := ansi.Strip(line)
						if !strings.HasPrefix(plain, prefix) {
							t.Fatalf("line %d did not align with its description: %q, want prefix %q", i, plain, prefix)
						}
						bodies = append(bodies, strings.TrimPrefix(plain, prefix))
					}
					assertWrappedDescription(t, description, bodies, width-DisplayWidth(firstPrefix))
					frame := ansi.Strip(m.RenderFrame())
					if strings.Count(frame, "> ") != 1 {
						t.Fatalf("continuation repeated the focus marker: %q", frame)
					}
					assertFits(t, m.RenderFrame(), width, 0)
				})
			}
		}
	}
}

func TestSelectorStacksDescriptionsInNarrowWindows(t *testing.T) {
	for _, width := range []int{4, 6, 20, 30} {
		for language, description := range selectorDescriptions {
			t.Run(fmt.Sprintf("%d/%d", width, language), func(t *testing.T) {
				m := NewSelectorModel(NewTheme(colorprofile.NoTTY), "", "", "", []SelectorItem{
					{Value: "hysteria2", Description: description},
				})
				m.Width = width
				lines, _ := m.itemLines()
				// The label can itself occupy several lines in a tiny window.
				labelLines := strings.Split(ansi.Wrap("hysteria2", width-2, ""), "\n")
				indent := strings.Repeat(" ", min(4, width-2))
				var bodies []string
				for _, line := range lines[len(labelLines):] {
					if !strings.HasPrefix(line, indent) {
						t.Fatalf("description was not indented below its label: %q", line)
					}
					bodies = append(bodies, strings.TrimPrefix(line, indent))
				}
				assertWrappedDescription(t, description, bodies, width-len(indent))
				if strings.Count(m.RenderFrame(), "> ") != 1 {
					t.Fatal("wrapped label or description repeated the focus marker")
				}
				assertFits(t, m.RenderFrame(), width, 0)
			})
		}
	}
}

func TestSelectorResizeRetainsItsInlineFootprint(t *testing.T) {
	m := NewSelectorModel(NewTheme(colorprofile.NoTTY), "Protocols", "(current)", "help", []SelectorItem{
		{Value: "hysteria2", Description: selectorDescriptions[1], Current: true},
	})
	for _, size := range []tea.WindowSizeMsg{
		{Width: 30, Height: 24}, {Width: 80, Height: 24}, {Width: 160, Height: 24},
		{Width: 4, Height: 3}, {Width: 80, Height: 24},
	} {
		previousRows := strings.Count(m.RenderFrame(), "\n") + 1
		next, _ := m.Update(size)
		m = next.(SelectorModel)
		frame := m.RenderFrame()
		rows := strings.Count(frame, "\n") + 1
		if rows < min(previousRows, size.Height) {
			t.Fatalf("resizing shrank the inline footprint from %d to %d rows: %q", previousRows, rows, frame)
		}
		if m.Cursor != 0 || m.Chosen != "" || m.Done {
			t.Fatal("resizing changed the selection")
		}
		assertFits(t, frame, size.Width, size.Height)
	}
}

func TestStaticChoicesShareDescriptionWrapping(t *testing.T) {
	for _, width := range []int{20, 80} {
		for language, description := range selectorDescriptions {
			t.Run(fmt.Sprintf("%d/%d", width, language), func(t *testing.T) {
				th := NewTheme(colorprofile.NoTTY)
				items := []SelectorItem{{Value: "hysteria2", Description: description, Current: true}}
				m := NewSelectorModel(th, "Protocols", "(current)", "", items)
				m.Width, m.Cursor = width, -1
				got := RenderChoices(th, width, "Protocols", "(current)", items)
				want := m.RenderFrame() + "\n"
				if got != want {
					t.Fatalf("static fallback differs from the selector layout:\ngot: %s\nwant: %s", got, want)
				}
				assertFits(t, strings.TrimSuffix(got, "\n"), width, 0)
			})
		}
	}
}

func assertWrappedDescription(t *testing.T, description string, lines []string, width int) {
	t.Helper()
	compact := func(s string) string { return strings.Join(strings.Fields(s), "") }
	if compact(strings.Join(lines, "")) != compact(description) {
		t.Fatalf("wrapping changed the description: %q", lines)
	}
	for _, word := range strings.Fields(description) {
		if DisplayWidth(word) > width {
			continue // An overlong token must split to fit the window.
		}
		found := false
		for _, line := range lines {
			found = found || strings.Contains(line, word)
		}
		if !found {
			t.Errorf("wrapping split the word %q: %q", word, lines)
		}
	}
}
