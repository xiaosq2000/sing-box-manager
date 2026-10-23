package tui

import (
	"bytes"
	"fmt"
	"os"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/colorprofile"
	"github.com/charmbracelet/x/ansi"
)

func TestTerminalDetectionRejectsFilesPipesAndNull(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "output")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	null, err := os.OpenFile(os.DevNull, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer null.Close()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer w.Close()
	for _, v := range []any{nil, file, null, r, w, &bytes.Buffer{}} {
		if IsTerminal(v) {
			t.Fatalf("non-terminal accepted: %T", v)
		}
	}
}

func TestSelectorRequiresEnterAndPreservesRawValue(t *testing.T) {
	raw := "trojan\x1b]52;c;ZmFrZQ==\a\u202e\nnext"
	m := NewSelectorModel(NewTheme(colorprofile.NoTTY), "Protocol", "(current)", "help", []SelectorItem{{Value: raw}})
	next, cmd := m.Update(tea.KeyPressMsg{Code: ' ', Text: " "})
	m = next.(SelectorModel)
	if m.Done || m.Chosen != "" || cmd != nil {
		t.Fatal("space submitted a selection")
	}
	frame := m.RenderFrame()
	if strings.ContainsAny(frame, "\x1b\a\u202e") || strings.Contains(frame, "\nnext") {
		t.Fatalf("unsafe frame: %q", frame)
	}
	next, _ = m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})
	if next.(SelectorModel).Chosen != raw {
		t.Fatal("display sanitization changed the API value")
	}
	for _, key := range []tea.KeyPressMsg{{Code: tea.KeyEscape}, {Code: 'q', Text: "q"}, {Code: 'c', Mod: tea.ModCtrl}} {
		model := NewSelectorModel(NewTheme(colorprofile.NoTTY), "", "", "", []SelectorItem{{Value: raw}})
		next, _ = model.Update(key)
		if next.(SelectorModel).Chosen != "" {
			t.Fatal("cancel selected a value")
		}
	}
}

func TestSelectorResizeKeepsCursorVisible(t *testing.T) {
	items := make([]SelectorItem, 24)
	for i := range items {
		items[i] = SelectorItem{Value: fmt.Sprintf("route-%02d", i), Description: "中文说明 mixed English description"}
	}
	m := NewSelectorModel(NewTheme(colorprofile.NoTTY), "路由策略", "（当前）", "help", items)
	for _, size := range []tea.WindowSizeMsg{{Width: 120, Height: 30}, {Width: 40, Height: 3}, {Width: 20, Height: 6}} {
		next, _ := m.Update(size)
		m = next.(SelectorModel)
		for range items {
			frame := m.RenderFrame()
			if !strings.Contains(frame, "> "+items[m.Cursor].Value) {
				t.Fatalf("cursor hidden at %d: %q", m.Cursor, frame)
			}
			assertFits(t, frame, size.Width, size.Height)
			next, _ = m.Update(tea.KeyPressMsg{Code: tea.KeyDown})
			m = next.(SelectorModel)
		}
	}
}

func TestAdaptiveTableKeepsAllFieldsAndRemovesControls(t *testing.T) {
	th := NewTheme(colorprofile.NoTTY)
	headers := []string{"目标", "首响应", "响应次数", "HTTP", "接管", "路径", "出口 IP"}
	rows := [][]string{{"target.test\x1b[2J\u202e\nspoof", "10.0 ms", "1/1", "200", "已验证", "proxy/trojan", "203.0.113.7"}}
	for _, width := range []int{40, 80, 160} {
		out := RenderTableTheme(th, width, headers, rows, DiagnosticCellStyler)
		assertFits(t, strings.TrimSuffix(out, "\n"), width, 0)
		if strings.ContainsAny(out, "\x1b\u202e") {
			t.Fatalf("unsafe table: %q", out)
		}
		for _, value := range []string{"200", "已验证", "203.0.113.7", "proxy/trojan"} {
			if !strings.Contains(out, value) {
				t.Fatalf("lost %q: %s", value, out)
			}
		}
	}
}

func TestLiveTableScrollResizeAndThemeParity(t *testing.T) {
	th := NewTheme(colorprofile.ANSI256)
	rows := make([][]string, 24)
	for i := range rows {
		rows[i] = []string{fmt.Sprintf("target-%02d", i), "12.0 ms", "1/1", "200", "verified", "proxy/trojan", "203.0.113.7"}
	}
	headers := []string{"Target", "Response", "Responses", "HTTP", "Capture", "Path", "IP"}
	m := NewLiveTableModel(th, "scope: sbc", headers, rows, nil, "testing", nil)
	for _, size := range []tea.WindowSizeMsg{{Width: 160, Height: 30}, {Width: 80, Height: 24}, {Width: 40, Height: 3}} {
		next, _ := m.Update(size)
		m = next.(LiveTableModel)
		assertFits(t, m.RenderFrame(), size.Width, size.Height)
	}
	next, _ := m.Update(tea.WindowSizeMsg{Width: 80, Height: 24})
	m = next.(LiveTableModel)
	frame := m.RenderFrame()
	if !strings.Contains(frame, "\x1b[") || !strings.Contains(frame, "203.0.113.7") {
		t.Fatal("live view lost colors or the IP")
	}
	next, _ = m.Update(tea.KeyPressMsg{Code: tea.KeyEnd})
	m = next.(LiveTableModel)
	if !strings.Contains(ansi.Strip(m.RenderFrame()), "target-23") {
		t.Fatal("last target is not accessible")
	}
	m.Theme = NewTheme(colorprofile.NoTTY)
	if strings.Contains(m.RenderFrame(), "\x1b") {
		t.Fatal("plain theme emitted ANSI")
	}
}

func TestPanelAlignsValuesAndPreservesNotices(t *testing.T) {
	for _, text := range []string{
		"Browser protection: on\nChrome languages: off\nChrome geolocation saved choice: on\nRegion matching: off\n",
		"浏览器保护：开启\nChrome 语言：关闭\nChrome 定位防护已保存选项：开启\n地区匹配：关闭\n",
	} {
		out := RenderPanel(NewTheme(colorprofile.NoTTY), 0, text)
		column := -1
		for _, line := range strings.Split(strings.TrimSuffix(out, "\n"), "\n") {
			key, value, _, ok := splitColonKV(line)
			if !ok {
				t.Fatalf("not a panel row: %q", line)
			}
			prefix := strings.TrimSuffix(line, value)
			if column < 0 {
				column = DisplayWidth(prefix)
			}
			if DisplayWidth(prefix) != column {
				t.Fatalf("unaligned key %q: %s", key, out)
			}
		}
	}
	notice := "Local files do not prove Chrome's policy. Check chrome://policy.\n"
	if got := RenderPanel(NewTheme(colorprofile.NoTTY), 0, notice); got != notice {
		t.Fatalf("notice changed: %q", got)
	}
}

func TestSelectorStartupErrorIsReturned(t *testing.T) {
	in, err := os.Open(os.DevNull)
	if err != nil {
		t.Fatal(err)
	}
	in.Close()
	_, _, err = RunSelector(NewOutput(in, &bytes.Buffer{}, nil), "", "", "", nil)
	if err == nil {
		t.Fatal("closed input reported success")
	}
}

func assertFits(t *testing.T, frame string, width, height int) {
	t.Helper()
	lines := strings.Split(frame, "\n")
	if height > 0 && len(lines) > height {
		t.Fatalf("height %d exceeds %d: %q", len(lines), height, frame)
	}
	for _, line := range lines {
		if DisplayWidth(line) > width {
			t.Fatalf("width %d exceeds %d: %q", DisplayWidth(line), width, line)
		}
	}
}
