package tui

import (
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/colorprofile"
)

func TestCJKDisplayWidthAndKeyValueAlignment(t *testing.T) {
	th := NewTheme(colorprofile.NoTTY)
	enRows := [][2]string{
		{"service:", "running"},
		{"proxy:", "127.0.0.1:2080"},
		{"shells:", "off"},
		{"desktop:", "off (GNOME)"},
		{"docker:", "off"},
		{"region:", "on (Asia/Tokyo, ja-JP)"},
		{"traffic:", "700.00 MiB up"},
		{"refresh:", "auto on, interval 6h"},
		{"upgrade:", "auto on, interval 6h"},
		{"route:", "china-direct"},
		{"protocol:", "trojan"},
	}
	for _, r := range enRows {
		line := FormatKV(th, r[0], StatusKeyWidth, r[1])
		prefix := strings.TrimSuffix(line, r[1])
		if got := DisplayWidth(prefix); got != StatusKeyWidth {
			t.Errorf("English key %q padded width = %d, want %d (line=%q)", r[0], got, StatusKeyWidth, line)
		}
	}

	zhRows := [][2]string{
		{"服务：", "运行中"},
		{"代理：", "127.0.0.1:2080"},
		{"shell：", "已关闭"},
		{"桌面：", "已关闭（GNOME）"},
		{"Docker：", "已关闭"},
		{"区域匹配：", "开启（Asia/Tokyo，ja-JP）"},
		{"流量：", "本计费周期上传 700.00 MiB"},
		{"refresh：", "自动 开启，间隔 6h"},
		{"upgrade：", "自动 开启，间隔 6h"},
		{"路由：", "china-direct"},
		{"协议：", "trojan"},
	}
	for _, r := range zhRows {
		line := FormatKV(th, r[0], StatusKeyWidth, r[1])
		prefix := strings.TrimSuffix(line, r[1])
		if got := DisplayWidth(prefix); got != StatusKeyWidth {
			t.Errorf("Chinese key %q padded width = %d, want %d (line=%q)", r[0], got, StatusKeyWidth, line)
		}
	}
}

func TestRenderTableAlignsMixedChineseAndASCIIColumns(t *testing.T) {
	th := NewTheme(colorprofile.NoTTY)
	headers := []string{"目标 / 来源", "首响应", "响应次数", "HTTP", "接管", "路径", "出口 IP / 速率"}
	rows := [][]string{
		{"IPify IPv4", "12.4 ms", "1/1", "200", "已验证", "proxy/trojan", "203.0.113.7"},
		{"ident.me IPv6", "-", "-", "-", "按策略跳过，未验证", "-", "-"},
		{"baidu (www.baidu.com)", "8.1 ms", "3/3", "200", "未验证", "direct", "未知"},
	}
	rendered := RenderTableTheme(th, 0, headers, rows, DiagnosticCellStyler)
	lines := strings.Split(strings.TrimSuffix(rendered, "\n"), "\n")
	if len(lines) < 6 {
		t.Fatalf("expected at least 6 table lines, got %d:\n%s", len(lines), rendered)
	}
	wantWidth := DisplayWidth(lines[0])
	for i, line := range lines {
		if got := DisplayWidth(line); got != wantWidth {
			t.Errorf("line %d display width = %d, want %d:\n%s\nfull table:\n%s", i, got, wantWidth, line, rendered)
		}
	}
}

func TestSelectorModelNavigationAndSelection(t *testing.T) {
	th := NewTheme(colorprofile.NoTTY)
	items := []SelectorItem{
		{Value: "direct", Description: "允许的流量全部直连，包括 AI"},
		{Value: "china-direct", Description: "已知中国目标直连，其余走代理", Current: true},
		{Value: "global", Description: "除局域网与直连例外外全部走代理"},
	}
	m := NewSelectorModel(th, "选择路由策略", "（当前）", "↑/↓ 移动 • enter 切换 • esc 取消", items)
	if m.Cursor != 1 {
		t.Fatalf("initial cursor = %d, want 1", m.Cursor)
	}
	frame := m.RenderFrame()
	if !strings.Contains(frame, "> china-direct （当前）") || !strings.Contains(frame, "允许的流量全部直连") {
		t.Fatalf("unexpected frame:\n%s", frame)
	}
	next, _ := m.Update(tea.KeyPressMsg{Code: tea.KeyDown})
	m = next.(SelectorModel)
	if m.Cursor != 2 {
		t.Fatalf("cursor after down = %d, want 2", m.Cursor)
	}
	next, _ = m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})
	m = next.(SelectorModel)
	if !m.Done || m.Canceled || m.Chosen != "global" {
		t.Fatalf("after enter: done=%v canceled=%v chosen=%q", m.Done, m.Canceled, m.Chosen)
	}

	mCancel := NewSelectorModel(th, "选择路由策略", "（当前）", "", items)
	next, _ = mCancel.Update(tea.KeyPressMsg{Code: tea.KeyEscape})
	mCancel = next.(SelectorModel)
	if !mCancel.Done || !mCancel.Canceled || mCancel.Chosen != "" {
		t.Fatalf("after esc: done=%v canceled=%v chosen=%q", mCancel.Done, mCancel.Canceled, mCancel.Chosen)
	}
}

func TestLiveTableModelUpdatesAndInterrupts(t *testing.T) {
	th := NewTheme(colorprofile.NoTTY)
	headers := []string{"目标 / 来源", "首响应", "响应次数"}
	rows := [][]string{{"baidu (www.baidu.com)", "-", "0/1"}}
	interrupted := false
	m := NewLiveTableModel(th, "范围：sbc；模式：mixed", headers, rows, map[int]bool{0: true}, "检测中...", func() {
		interrupted = true
	})
	frame := m.RenderFrame()
	if !strings.Contains(frame, "检测中...") {
		t.Fatalf("expected pending label in frame:\n%s", frame)
	}
	next, _ := m.Update(LiveTableUpdateMsg{
		Rows:    [][]string{{"baidu (www.baidu.com)", "10.0 ms", "1/1"}},
		Pending: map[int]bool{},
	})
	m = next.(LiveTableModel)
	frame = m.RenderFrame()
	if !strings.Contains(frame, "10.0 ms") || strings.Contains(frame, "检测中...") {
		t.Fatalf("expected updated latency in frame:\n%s", frame)
	}
	next, _ = m.Update(tea.KeyPressMsg{Code: 'c', Mod: tea.ModCtrl})
	m = next.(LiveTableModel)
	if !m.Interrupted || !m.Done || !interrupted {
		t.Fatalf("expected interrupt: model=%+v callback=%v", m, interrupted)
	}
}
