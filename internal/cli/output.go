package cli

import (
	"bytes"
	"fmt"

	"github.com/xiaosq2000/sing-box-manager/internal/tui"
)

// withPanel collects a read-only summary before it calculates the key width.
// Commands that read input or emit machine-readable output must not use it.
func withPanel(env Env, run func(Env) int) int {
	var text bytes.Buffer
	buffered := env
	buffered.Stdout = &text
	code := run(buffered)
	if text.Len() > 0 {
		if _, err := fmt.Fprint(env.Stdout, tui.RenderPanel(env.Stdout, env.Getenv, text.String())); err != nil {
			return 1
		}
	}
	return code
}
