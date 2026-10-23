package cli

import (
	"bytes"
	"fmt"

	"github.com/xiaosq2000/sing-box-manager/internal/tui"
)

// humanOutput belongs to one Run invocation, not a reusable Env or global cache.
type humanOutput struct{ stdout *tui.Output }

func (env Env) output() *tui.Output {
	if env.outputState == nil {
		return tui.NewOutput(env.Stdin, env.Stdout, env.Getenv)
	}
	if env.outputState.stdout == nil {
		env.outputState.stdout = tui.NewOutput(env.Stdin, env.Stdout, env.Getenv)
	}
	return env.outputState.stdout
}

func printTable(env Env, headers []string, rows [][]string, styler tui.CellStyler) {
	out := env.output()
	width, _ := out.Size()
	fmt.Fprint(out, tui.RenderTableTheme(out.Theme, width, headers, rows, styler))
}

// withPanel collects a read-only summary before it calculates the key width.
// Commands that read input or emit machine-readable output must not use it.
func withPanel(env Env, run func(Env) int) int {
	var text bytes.Buffer
	buffered := env
	buffered.Stdout = &text
	// Buffer formatting must not retain the real terminal's styling context.
	buffered.outputState = &humanOutput{}
	code := run(buffered)
	if text.Len() > 0 {
		out := env.output()
		width, _ := out.Size()
		if _, err := fmt.Fprint(out, tui.RenderPanel(out.Theme, width, text.String())); err != nil {
			return 1
		}
	}
	return code
}
