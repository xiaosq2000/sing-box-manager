//go:build e2e && !windows

package e2e

import (
	"os/exec"
	"syscall"
)

func prepareBrowserProcess(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}
func stopBrowserProcess(command *exec.Cmd) error {
	return syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
}
