//go:build e2e && windows

package e2e

import (
	"context"
	"os/exec"
	"strconv"
	"time"
)

func prepareBrowserProcess(_ *exec.Cmd) {}
func stopBrowserProcess(command *exec.Cmd) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "taskkill.exe", "/PID", strconv.Itoa(command.Process.Pid), "/T", "/F").Run()
}
