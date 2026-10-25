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
	frameRows                     int
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
	// Program clears this frame after the renderer stops.
	return tea.NewView(m.RenderFrame())
}

func (m SelectorModel) itemLines() ([]string, []int) {
	var lines []string
	starts := make([]int, len(m.Items))
	maxWidth := 0
	labels := make([]string, len(m.Items))
	for i, item := range m.Items {
		labels[i] = SafeText(item.Value)
		if item.Current {
			labels[i] += " " + SafeText(m.CurrentBadge)
		}
		maxWidth = max(maxWidth, DisplayWidth(labels[i]))
	}
	for i, item := range m.Items {
		starts[i] = len(lines)
		width := 0
		if m.Width > 0 {
			width = max(2, m.Width-2)
		}
		wrapped := wrapSelectorItem(labels[i], SafeText(item.Description), maxWidth, width)
		for j, line := range wrapped {
			prefix := "  "
			if i == m.Cursor && j == 0 {
				prefix = "> "
			}
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

// wrapSelectorItem reserves the focus gutter separately from label and text.
// Continuations align with the description, not the start of the label.
func wrapSelectorItem(label, description string, labelWidth, width int) []string {
	if description == "" {
		return strings.Split(ansi.Wrap(label, width, ""), "\n")
	}
	if width <= 0 {
		return []string{PadRight(label, labelWidth) + "  " + description}
	}
	descriptionWidth := width - labelWidth - 2
	if descriptionWidth >= 20 {
		lines := strings.Split(ansi.Wrap(description, descriptionWidth, ""), "\n")
		lines[0] = PadRight(label, labelWidth) + "  " + lines[0]
		indent := strings.Repeat(" ", labelWidth+2)
		for i := 1; i < len(lines); i++ {
			lines[i] = indent + lines[i]
		}
		return lines
	}

	// Stack narrow items, leaving at least two cells for a CJK character.
	lines := strings.Split(ansi.Wrap(label, width, ""), "\n")
	indent := min(2, max(0, width-2))
	for _, line := range strings.Split(ansi.Wrap(description, width-indent, ""), "\n") {
		lines = append(lines, strings.Repeat(" ", indent)+line)
	}
	return lines
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
	defer func() {
		m.frameRows = max(m.frameRows, strings.Count(m.renderFrame(), "\n")+1)
		if m.Height > 0 {
			m.frameRows = min(m.frameRows, m.Height)
		}
	}()
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
	frame := m.renderFrame()
	rows := m.frameRows
	if m.Height > 0 {
		rows = min(rows, m.Height)
	}
	// Do not shrink the allocated inline region when wider text needs fewer
	// rows. The pinned renderer can otherwise leave old rows above the view.
	return frame + strings.Repeat("\n", max(0, rows-strings.Count(frame, "\n")-1))
}

func (m SelectorModel) renderFrame() string {
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
	rendered := RenderTableTheme(th, 0, []string{title, ""}, rows, nil)
	if width <= 0 || DisplayWidth(rendered) <= width {
		return rendered
	}
	if width < 4 {
		return RenderTableTheme(th, width, []string{title, ""}, rows, nil)
	}
	model := SelectorModel{Theme: th, Width: width, CurrentBadge: currentBadge, Items: items, Cursor: -1}
	lines, _ := model.itemLines()
	if title != "" {
		lines = append([]string{th.Header.Render(ansi.Wrap(SafeText(title), width, ""))}, lines...)
	}
	return strings.Join(lines, "\n") + "\n"
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
