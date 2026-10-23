package tui

import (
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
)

// SelectorItem keeps API values separate from their display labels.
type SelectorItem struct {
	Value       string
	Description string
	Current     bool
}

// InterruptError preserves the process exit code after terminal restoration.
type InterruptError struct{ Code int }

func (e *InterruptError) Error() string { return fmt.Sprintf("interrupted (exit %d)", e.Code) }

type selectorSignalMsg int

// SelectorModel selects a route or protocol without changing the runtime.
// The caller applies Chosen only after Enter.
type SelectorModel struct {
	Title, CurrentBadge, HelpText string
	Items                         []SelectorItem
	Cursor                        int
	Chosen                        string
	Canceled, Done                bool
	InterruptCode                 int
	Theme                         Theme
	Width, Height                 int
	offset                        int
	itemOffset                    int
}

func NewSelectorModel(th Theme, title, currentBadge, helpText string, items []SelectorItem) SelectorModel {
	m := SelectorModel{Title: title, CurrentBadge: currentBadge, HelpText: helpText, Items: items, Theme: th}
	for i, item := range items {
		if item.Current {
			m.Cursor = i
			break
		}
	}
	m.ensureVisible()
	return m
}

func (m SelectorModel) Init() tea.Cmd { return nil }

func (m SelectorModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	if m.Done {
		return m, nil
	}
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.Width, m.Height = msg.Width, msg.Height
	case selectorSignalMsg:
		m.InterruptCode, m.Done = int(msg), true
		return m, tea.Quit
	case tea.KeyPressMsg:
		key := msg.String()
		switch key {
		case "up", "k", "ctrl+p", "down", "j", "ctrl+n", "home", "end":
			m.itemOffset = 0
		}
		switch key {
		case "up", "k", "ctrl+p":
			if len(m.Items) > 0 {
				m.Cursor = (m.Cursor - 1 + len(m.Items)) % len(m.Items)
			}
		case "down", "j", "ctrl+n":
			if len(m.Items) > 0 {
				m.Cursor = (m.Cursor + 1) % len(m.Items)
			}
		case "home":
			m.Cursor = 0
		case "end":
			m.Cursor = max(0, len(m.Items)-1)
		case "pgdown", "ctrl+f":
			m.itemOffset += max(1, m.contentHeight()-1)
		case "pgup", "ctrl+b":
			m.itemOffset -= max(1, m.contentHeight()-1)
		case "enter":
			if m.Cursor >= 0 && m.Cursor < len(m.Items) {
				m.Chosen = m.Items[m.Cursor].Value
			}
			m.Done = true
			return m, tea.Quit
		case "esc", "q":
			m.Canceled, m.Done = true, true
			return m, tea.Quit
		case "ctrl+c":
			m.InterruptCode, m.Done = 130, true
			return m, tea.Quit
		}
	}
	m.ensureVisible()
	return m, nil
}

func (m SelectorModel) View() tea.View {
	if m.Done {
		return tea.NewView("")
	}
	return tea.NewView(m.RenderFrame())
}

func (m SelectorModel) itemLines() ([]string, []int) {
	var lines []string
	starts := make([]int, len(m.Items))
	maxWidth := 0
	for _, item := range m.Items {
		label := SafeText(item.Value)
		if item.Current {
			label += " " + SafeText(m.CurrentBadge)
		}
		maxWidth = max(maxWidth, DisplayWidth(label))
	}
	for i, item := range m.Items {
		starts[i] = len(lines)
		prefix := "  "
		if i == m.Cursor {
			prefix = "> "
		}
		label := SafeText(item.Value)
		if item.Current {
			label += " " + SafeText(m.CurrentBadge)
		}
		text := PadRight(label, maxWidth)
		if item.Description != "" {
			text += "  " + SafeText(item.Description)
		}
		width := 0
		if m.Width > 0 {
			width = max(2, m.Width-2)
		}
		wrapped := strings.Split(FitLines(strings.TrimRight(text, " "), width), "\n")
		for _, line := range wrapped {
			line = prefix + line
			if m.Theme.Styled() {
				if i == m.Cursor {
					line = m.Theme.Primary.Bold(true).Render(line)
				} else if item.Current {
					line = m.Theme.Success.Render(line)
				}
			}
			lines = append(lines, line)
		}
	}
	return lines, starts
}

func (m SelectorModel) contentHeight() int {
	if m.Height <= 0 {
		return 0
	}
	h := m.Height
	if m.Title != "" && m.Height >= 3 {
		h--
	}
	if m.HelpText != "" && m.Height >= 2 {
		h--
	}
	return max(1, h)
}

func (m *SelectorModel) ensureVisible() {
	lines, starts := m.itemLines()
	h := m.contentHeight()
	if h == 0 || len(starts) == 0 {
		m.offset = 0
		return
	}
	start := starts[m.Cursor]
	end := len(lines)
	if m.Cursor+1 < len(starts) {
		end = starts[m.Cursor+1]
	}
	if end-start > h {
		m.itemOffset = max(0, min(m.itemOffset, end-start-h))
		m.offset = start + m.itemOffset
		return
	}
	m.itemOffset = 0
	if start < m.offset || start >= m.offset+h {
		m.offset = start
	} else if end > m.offset+h {
		m.offset = min(start, end-h)
	}
	m.offset = max(0, min(m.offset, max(0, len(lines)-h)))
}

// RenderFrame keeps the cursor visible, including after a terminal resize.
func (m SelectorModel) RenderFrame() string {
	if m.Width > 0 && m.Width < 4 {
		return ansi.Truncate("<>", m.Width, "")
	}
	lines, _ := m.itemLines()
	if h := m.contentHeight(); h > 0 {
		start := min(m.offset, len(lines))
		lines = lines[start:min(len(lines), start+h)]
	}
	var b strings.Builder
	if m.Title != "" && (m.Height == 0 || m.Height >= 3) {
		b.WriteString(m.Theme.Header.Render(ansi.Truncate(SafeText(m.Title), m.headerWidth(), "…")))
		b.WriteByte('\n')
	}
	b.WriteString(strings.Join(lines, "\n"))
	if m.HelpText != "" && (m.Height == 0 || m.Height >= 2) {
		b.WriteByte('\n')
		b.WriteString(m.Theme.Muted.Render(ansi.Truncate(SafeText(m.HelpText), m.headerWidth(), "…")))
	}
	return b.String()
}

func (m SelectorModel) headerWidth() int {
	if m.Width <= 0 {
		return 10000
	}
	return m.Width
}

// RenderChoices uses the same labels in static and interactive output.
func RenderChoices(th Theme, width int, title, currentBadge string, items []SelectorItem) string {
	rows := make([][]string, len(items))
	for i, item := range items {
		label := SafeText(item.Value)
		if item.Current {
			label += " " + SafeText(currentBadge)
		}
		rows[i] = []string{label, item.Description}
	}
	return RenderTableTheme(th, width, []string{title, ""}, rows, nil)
}

func RunSelector(output *Output, title, currentBadge, helpText string, items []SelectorItem) (string, bool, error) {
	model := NewSelectorModel(output.Theme, title, currentBadge, helpText, items)
	model.Width, model.Height = output.Size()
	model.ensureVisible()
	prog, err := output.Program(model)
	if err != nil {
		return "", false, err
	}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	done, watched := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(watched)
		select {
		case sig := <-signals:
			code := 130
			if sig == syscall.SIGTERM {
				code = 143
			}
			prog.Send(selectorSignalMsg(code))
		case <-done:
		}
	}()
	finalModel, err := prog.Run()
	close(done)
	<-watched
	if err != nil {
		return "", false, err
	}
	result := finalModel.(SelectorModel)
	if result.InterruptCode != 0 {
		return "", false, &InterruptError{Code: result.InterruptCode}
	}
	return result.Chosen, result.Canceled || result.Chosen == "", nil
}
