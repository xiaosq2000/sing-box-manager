package winsettings

import (
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"syscall"
	"unicode/utf16"
	"unsafe"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Resolve the Windows-owned directory through the OS, not PATH, SystemRoot or
// a directory beside sbc. This also works when UAC uses another administrator.
func webRTCSystemPowerShell() (string, error) {
	getSystemDirectory := syscall.NewLazyDLL("kernel32.dll").NewProc("GetSystemDirectoryW")
	buffer := make([]uint16, 32768)
	length, _, _ := getSystemDirectory.Call(uintptr(unsafe.Pointer(&buffer[0])), uintptr(len(buffer)))
	if length == 0 || length >= uintptr(len(buffer)) {
		return "", i18n.New("Windows could not locate the trusted system PowerShell executable")
	}
	return filepath.Join(syscall.UTF16ToString(buffer[:length]), "WindowsPowerShell", "v1.0", "powershell.exe"), nil
}

func changeWebRTCPolicies(action string) error {
	// os/user on Windows obtains Uid from the current process token. A UAC
	// administrator's token must never be used to choose the browser hive.
	account, err := user.Current()
	if err != nil || !validWebRTCSID(account.Uid) {
		return i18n.New("Windows could not determine a valid SID for the original user")
	}
	if _, err := syscall.StringToSid(account.Uid); err != nil {
		return i18n.New("Windows could not determine a valid SID for the original user")
	}
	script, err := webRTCScript(account.Uid, action)
	if err != nil {
		return err
	}
	executable, err := webRTCSystemPowerShell()
	if err != nil {
		return err
	}
	encoded := encodedWebRTCCommand(script)
	launcher := webRTCLauncher(executable, encoded)
	// CreateProcess has a 32767 UTF-16-unit command-line limit. Check the
	// escaped launcher and the elevated child, including paths and switches.
	for _, arguments := range []string{
		syscall.EscapeArg(executable) + " -NoProfile -NonInteractive -Command " + syscall.EscapeArg(launcher),
		syscall.EscapeArg(executable) + " -NoProfile -NonInteractive -EncodedCommand " + encoded,
	} {
		if len(utf16.Encode([]rune(arguments))) >= 32767 {
			return i18n.New("the embedded WebRTC policy command exceeds the Windows argument limit")
		}
	}
	// Do not time out the launcher while its elevated child is still changing
	// policies. Wait for UAC approval/cancellation and the child's exit status.
	command := exec.Command(executable, "-NoProfile", "-NonInteractive", "-Command", launcher)
	command.Dir = filepath.Dir(executable)
	command.Env = webRTCEnvironment(os.Environ())
	if err := command.Run(); err != nil {
		if exit, ok := err.(*exec.ExitError); ok && exit.ExitCode() == 2 {
			return i18n.New("WebRTC policy administrator approval was cancelled; retry when you can approve the registry-only prompt")
		}
		return i18n.New("Windows could not change the WebRTC policies; approve the registry-only administrator prompt and retry")
	}
	return nil
}
