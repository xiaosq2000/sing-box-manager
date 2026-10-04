//go:build windows

package cli

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"unsafe"

	"github.com/xiaosq2000/sing-box-manager/internal/powershell"
)

var (
	kernel32                 = syscall.NewLazyDLL("kernel32.dll")
	createJobObject          = kernel32.NewProc("CreateJobObjectW")
	setInformationJobObject  = kernel32.NewProc("SetInformationJobObject")
	assignProcessToJobObject = kernel32.NewProc("AssignProcessToJobObject")
)

const (
	jobObjectExtendedLimitInformation = 9
	jobObjectLimitKillOnJobClose      = 0x2000
	createNoWindow                    = 0x08000000
)

// The JOBOBJECT_EXTENDED_LIMIT_INFORMATION structure, laid out as in the
// Windows headers.
type jobExtendedLimit struct {
	PerProcessUserTimeLimit int64
	PerJobUserTimeLimit     int64
	LimitFlags              uint32
	MinimumWorkingSetSize   uintptr
	MaximumWorkingSetSize   uintptr
	ActiveProcessLimit      uint32
	Affinity                uintptr
	PriorityClass           uint32
	SchedulingClass         uint32
	IoCounters              [6]uint64
	ProcessMemoryLimit      uintptr
	JobMemoryLimit          uintptr
	PeakProcessMemoryUsed   uintptr
	PeakJobMemoryUsed       uintptr
}

// joinJob puts sbc in a job object that ends every process in it when sbc
// exits, so sing-box, started as a child, never outlives sbc. The handle
// stays open for sbc's life, since closing it would end the job.
func joinJob() error {
	handle, _, err := createJobObject.Call(0, 0)
	if handle == 0 {
		return err
	}
	limits := jobExtendedLimit{LimitFlags: jobObjectLimitKillOnJobClose}
	if ok, _, err := setInformationJobObject.Call(handle, jobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&limits)), unsafe.Sizeof(limits)); ok == 0 {
		return err
	}
	process, _ := syscall.GetCurrentProcess()
	if ok, _, err := assignProcessToJobObject.Call(handle, uintptr(process)); ok == 0 {
		return err
	}
	return nil
}

// execProcess runs the program as a child and waits for it, since Windows
// cannot replace a process. Its output goes to output, which is the log file
// when no console shows it, and its process id to pidFile, which the service
// manager reads to wait for it after stopping sbc.
func execProcess(binary string, args []string, output io.Writer, pidFile string) error {
	if err := joinJob(); err != nil {
		fmt.Fprintf(output, "sbc: cannot tie sing-box's life to its own: %v\n", err)
	}
	command := exec.Command(binary, args[1:]...)
	command.Stdout, command.Stderr = output, output
	if err := command.Start(); err != nil {
		return err
	}
	if err := os.WriteFile(pidFile, []byte(strconv.Itoa(command.Process.Pid)+"\n"), 0o600); err != nil {
		fmt.Fprintf(output, "sbc: cannot record the sing-box process: %v\n", err)
	}
	err := command.Wait()
	os.Remove(pidFile)
	return err
}

// deleteWhenExited removes dir once this process, which runs from it, has
// exited, as Windows refuses to delete a running program.
func deleteWhenExited(dir string) error {
	script := "Start-Sleep -Seconds 3; Remove-Item -LiteralPath '" + strings.ReplaceAll(dir, "'", "''") + "' -Recurse -Force"
	command := powershell.Command(context.Background(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script)
	command.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow, HideWindow: true}
	return command.Start()
}
