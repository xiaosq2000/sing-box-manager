package tui

import (
	"io"
	"strings"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/lipgloss/table"
)

// CellStyler returns a Lipgloss style for a data cell at (row, col).
type CellStyler func(row, col int, value string, th Theme) lipgloss.Style

// RenderTable renders a rounded-border table with CJK-aware column alignment.
// When w is not a terminal or NO_COLOR is set, ANSI codes are omitted while
// preserving box-drawing borders and exact display-width padding.
func RenderTable(w io.Writer, getenv func(string) string, headers []string, rows [][]string, styler CellStyler) string {
	width, _ := TerminalSize(w)
	return RenderTableTheme(NewTheme(w, getenv), width, headers, rows, styler)
}

// RenderTableTheme keeps the caller's styles and terminal capabilities.
// A narrow terminal uses stacked fields instead of hidden columns.
func RenderTableTheme(th Theme, width int, headers []string, rows [][]string, styler CellStyler) string {
	headers = cleanCells(headers)
	cleanRows := make([][]string, len(rows))
	for i, row := range rows {
		cleanRows[i] = cleanCells(row)
	}
	rows = cleanRows
	tbl := table.New().
		Border(lipgloss.RoundedBorder()).
		BorderStyle(th.Border).
		Wrap(false).
		Headers(headers...).
		Rows(rows...).
		StyleFunc(func(row, col int) lipgloss.Style {
			if row == table.HeaderRow {
				return th.Header.Padding(0, 1)
			}
			base := th.Base.Padding(0, 1)
			if styler != nil && row >= 0 && row < len(rows) && col >= 0 && col < len(rows[row]) {
				return styler(row, col, rows[row][col], th).Padding(0, 1)
			}
			return base
		})
	rendered := tbl.Render()
	if width <= 0 || DisplayWidth(rendered) <= width {
		return rendered + "\n"
	}
	var b strings.Builder
	for r, row := range rows {
		for c, value := range row {
			label := ""
			if c < len(headers) {
				label = headers[c]
			}
			style := th.Base
			if styler != nil {
				style = styler(r, c, value, th)
			}
			b.WriteString(FitLines(th.Key.Render(label)+": "+style.Render(value), width))
			b.WriteByte('\n')
		}
		if r < len(rows)-1 {
			b.WriteString(th.Border.Render(strings.Repeat("─", width)))
			b.WriteByte('\n')
		}
	}
	return b.String()
}

func cleanCells(cells []string) []string {
	result := make([]string, len(cells))
	for i, cell := range cells {
		result[i] = SafeText(cell)
	}
	return result
}

// DiagnosticCellStyler applies semantic colors to the 7-column diagnostic table
// (Target/source, First response, Responses, HTTP, Capture, Path, Exit IP/rate).
func DiagnosticCellStyler(_ int, col int, value string, th Theme) lipgloss.Style {
	if !th.Colored {
		return th.Base
	}
	trimmed := strings.TrimSpace(value)
	switch col {
	case 0: // Target / source
		return th.Base.Bold(true)
	case 1: // First response
		if trimmed == "-" {
			return th.Muted
		}
		return th.Primary
	case 2: // Responses (e.g. 1/1, 0/1)
		if before, after, ok := strings.Cut(trimmed, "/"); ok {
			if before == "0" {
				return th.Danger
			}
			if before != after {
				return th.Warning
			}
			return th.Success
		}
		return th.Muted
	case 3: // HTTP status
		switch {
		case trimmed == "-":
			return th.Muted
		case strings.HasPrefix(trimmed, "2") && !strings.Contains(trimmed, "/4") && !strings.Contains(trimmed, "/5") && !strings.Contains(trimmed, "/3"):
			return th.Success
		case strings.HasPrefix(trimmed, "3"):
			return th.Warning
		default:
			return th.Danger
		}
	case 4: // Capture
		switch trimmed {
		case "verified", "已验证":
			return th.Success
		case "unverified", "未验证":
			return th.Warning
		default:
			return th.Muted
		}
	case 5: // Path
		if trimmed == "-" || trimmed == "unknown" || trimmed == "未知" {
			return th.Muted
		}
		return th.Base
	case 6: // Exit IP / rate
		if trimmed == "-" || trimmed == "unknown" || trimmed == "未知" {
			return th.Muted
		}
		return th.Primary.Bold(true)
	default:
		return th.Base
	}
}
