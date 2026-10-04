//go:build !windows

package cli

import (
	"io"
	"os"
	"syscall"
)

// execProcess replaces sbc with the program, which keeps sbc's standard
// streams and process id, so output and pidFile go unused.
func execProcess(binary string, args []string, _ io.Writer, _ string) error {
	return syscall.Exec(binary, args, os.Environ())
}

// deleteWhenExited is for Windows, where a running program cannot delete
// itself. Unix removes the directory outright.
func deleteWhenExited(string) error { return nil }
