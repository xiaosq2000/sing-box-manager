// Package powershell starts Windows PowerShell with its own module search path.
package powershell

import (
	"context"
	"os"
	"os/exec"
	"strings"
)

// Command keeps the caller's environment except PSModulePath. PowerShell 7
// normally fixes that variable when starting 5.1 directly, but cannot do so
// when a native program such as sbc starts 5.1 on its behalf.
func Command(ctx context.Context, args ...string) *exec.Cmd {
	command := exec.CommandContext(ctx, "powershell.exe", args...)
	command.Env = []string{}
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if !strings.EqualFold(name, "PSModulePath") {
			command.Env = append(command.Env, entry)
		}
	}
	return command
}
