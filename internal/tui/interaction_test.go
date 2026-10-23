package tui

import (
	"fmt"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/colorprofile"
)

func TestSelectorPagesThroughOversizedItem(t *testing.T) {
	for _, width := range []int{4, 20, 40, 80, 160} {
		for _, height := range []int{1, 2, 3, 8} {
			t.Run(fmt.Sprintf("%dx%d", width, height), func(t *testing.T) {
				m := NewSelectorModel(NewTheme(colorprofile.NoTTY), "", "(current)", "", []SelectorItem{
					{Value: "one", Description: strings.Repeat("中文 mixed description ", 20) + "尾部 TAIL", Current: true},
					{Value: "two", Description: "next"},
				})
				next, _ := m.Update(tea.WindowSizeMsg{Width: width, Height: height})
				m = next.(SelectorModel)
				lines, starts := m.itemLines()
				end := starts[1]
				seen := make([]bool, end)
				for range end + 1 {
					frame := m.RenderFrame()
					assertFits(t, frame, width, height)
					if m.Chosen != "" || m.Cursor != 0 {
						t.Fatal("paging changed selection")
					}
					for i := m.offset; i < min(end, m.offset+m.contentHeight()); i++ {
						if strings.Contains(frame, lines[i]) {
							seen[i] = true
						}
					}
					next, _ = m.Update(tea.KeyPressMsg{Code: tea.KeyPgDown})
					m = next.(SelectorModel)
				}
				for i, visible := range seen {
					if !visible {
						t.Fatalf("item line %d is unreachable: %q", i, lines[i])
					}
				}
				for range end + 1 {
					next, _ = m.Update(tea.KeyPressMsg{Code: tea.KeyPgUp})
					m = next.(SelectorModel)
				}
				if m.offset != 0 {
					t.Fatal("Page Up did not restore the first line")
				}
				next, _ = m.Update(tea.KeyPressMsg{Code: tea.KeyDown})
				m = next.(SelectorModel)
				if m.Cursor != 1 || m.itemOffset != 0 {
					t.Fatal("new item retained the old scroll offset")
				}
			})
		}
	}
}

func TestTinyViewsRetainDataAfterResize(t *testing.T) {
	th := NewTheme(colorprofile.NoTTY)
	selector := NewSelectorModel(th, "选择路由", "(current)", "help", []SelectorItem{{Value: "route", Description: "中文说明"}})
	live := NewLiveTableModel(th, "检查范围", []string{"目标", "响应"}, [][]string{{"example.test", "中文结果"}}, nil, "testing", nil)
	for _, size := range []tea.WindowSizeMsg{{Width: 1, Height: 1}, {Width: 2, Height: 2}, {Width: 3, Height: 1}, {Width: 80, Height: 24}} {
		next, _ := selector.Update(size)
		selector = next.(SelectorModel)
		next, _ = live.Update(size)
		live = next.(LiveTableModel)
		assertFits(t, selector.RenderFrame(), size.Width, size.Height)
		assertFits(t, live.RenderFrame(), size.Width, size.Height)
	}
	if !strings.Contains(selector.RenderFrame(), "中文说明") || !strings.Contains(live.RenderFrame(), "中文结果") {
		t.Fatal("tiny window discarded model data")
	}
}

func TestControlsNeedNoPatchedFontAndKeepInlineView(t *testing.T) {
	th := NewTheme(colorprofile.NoTTY)
	selector := NewSelectorModel(th, "", "", "", []SelectorItem{{Value: "route"}})
	if !strings.HasPrefix(selector.RenderFrame(), "> route") {
		t.Fatal("focus lacks a plain marker")
	}
	live := NewLiveTableModel(th, "", nil, nil, nil, "testing", nil)
	for _, frame := range live.Spinner.Spinner.Frames {
		for _, r := range frame {
			if r < 32 || r > 126 {
				t.Fatalf("spinner needs a non-ASCII glyph: %q", frame)
			}
		}
	}
	for _, view := range []tea.View{selector.View(), live.View()} {
		if view.AltScreen || view.BackgroundColor != nil || view.ForegroundColor != nil || view.MouseMode != tea.MouseModeNone {
			t.Fatal("inline view overrides terminal state or colors")
		}
	}
}
