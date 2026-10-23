package tui

import (
	"fmt"
	"strings"

	"charm.land/bubbles/v2/spinner"
	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
)

type LiveTableUpdateMsg struct {
	Rows    [][]string
	Pending map[int]bool
}

type LiveTableDoneMsg struct{}

// LiveTableModel renders a scrollable inline diagnostic view.
// The caller prints the complete final report after this view stops.
type LiveTableModel struct {
	HeaderLine, TestingLabel, HelpText string
	Headers                            []string
	Rows                               [][]string
	Pending                            map[int]bool
	Spinner                            spinner.Model
	Theme                              Theme
	Done, Interrupted                  bool
	OnInterrupt                        func()
	Width, Height                      int
	offset                             int
}

func NewLiveTableModel(th Theme, headerLine string, headers []string, rows [][]string, pending map[int]bool, testingLabel string, onInterrupt func()) LiveTableModel {
	sp := spinner.New()
	sp.Spinner = spinner.Line
	sp.Style = th.Primary
	return LiveTableModel{HeaderLine: headerLine, Headers: headers, Rows: rows, Pending: pending,
		TestingLabel: testingLabel, Spinner: sp, Theme: th, OnInterrupt: onInterrupt}
}

func (m LiveTableModel) Init() tea.Cmd { return m.Spinner.Tick }

func (m LiveTableModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.Width, m.Height = msg.Width, msg.Height
	case tea.KeyPressMsg:
		switch msg.String() {
		case "ctrl+c":
			m.Interrupted, m.Done = true, true
			if m.OnInterrupt != nil {
				m.OnInterrupt()
			}
			return m, tea.Quit
		case "down", "j":
			m.offset++
		case "up", "k":
			m.offset--
		case "pgdown", "ctrl+f":
			m.offset += max(1, m.Height-2)
		case "pgup", "ctrl+b":
			m.offset -= max(1, m.Height-2)
		case "home":
			m.offset = 0
		case "end":
			m.offset = len(m.lines())
		}
	case LiveTableUpdateMsg:
		m.Rows, m.Pending = msg.Rows, msg.Pending
	case LiveTableDoneMsg:
		m.Done = true
		return m, tea.Quit
	case spinner.TickMsg:
		var cmd tea.Cmd
		m.Spinner, cmd = m.Spinner.Update(msg)
		return m, cmd
	}
	m.offset = max(0, min(m.offset, max(0, len(m.lines())-m.contentHeight())))
	return m, nil
}

func (m LiveTableModel) View() tea.View {
	if m.Done {
		return tea.NewView("")
	}
	return tea.NewView(m.RenderFrame())
}

func (m LiveTableModel) lines() []string {
	var b strings.Builder
	if m.HeaderLine != "" {
		b.WriteString(FitLines(m.Theme.Header.Render(SafeText(m.HeaderLine)), m.Width))
		b.WriteByte('\n')
	}
	rows := make([][]string, len(m.Rows))
	for i, row := range m.Rows {
		rows[i] = append([]string(nil), row...)
		if m.Pending[i] && len(row) > 1 {
			rows[i][1] = strings.TrimSpace(m.Spinner.View() + " " + m.TestingLabel)
		}
	}
	b.WriteString(RenderTableTheme(m.Theme, m.Width, m.Headers, rows, DiagnosticCellStyler))
	return strings.Split(strings.TrimSuffix(b.String(), "\n"), "\n")
}

func (m LiveTableModel) contentHeight() int {
	if m.Height <= 0 {
		return len(m.lines())
	}
	return max(1, m.Height-1)
}

func (m LiveTableModel) RenderFrame() string {
	if m.Width > 0 && m.Width < 4 {
		return ansi.Truncate("<>", m.Width, "")
	}
	lines := m.lines()
	h := m.contentHeight()
	start := max(0, min(m.offset, max(0, len(lines)-h)))
	end := min(len(lines), start+h)
	frame := strings.Join(lines[start:end], "\n")
	if m.Height > 1 && len(lines) > h {
		footer := fmt.Sprintf("%d–%d/%d %s", start+1, end, len(lines), SafeText(m.HelpText))
		frame += "\n" + m.Theme.Muted.Render(ansi.Truncate(footer, max(1, m.Width), "…"))
	}
	return frame
}
