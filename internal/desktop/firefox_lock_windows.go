//go:build windows

package desktop

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

func lockFirefoxProfile(profile string) (func(), error) {
	path := filepath.Join(profile, "parent.lock")
	if info, err := os.Lstat(path); err == nil && !info.Mode().IsRegular() {
		return nil, i18n.Errorf("WebRTC settings path is not a regular file: %s", path)
	} else if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	name, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return nil, err
	}
	// Match Firefox's exclusive sharing mode instead of trusting lock-file
	// existence: a closed profile can retain a stale parent.lock file.
	handle, err := syscall.CreateFile(name, syscall.GENERIC_READ|syscall.GENERIC_WRITE, 0, nil, syscall.OPEN_ALWAYS, syscall.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		if errors.Is(err, syscall.Errno(32)) { // ERROR_SHARING_VIOLATION
			return nil, i18n.Errorf("Firefox profile is in use; close Firefox and retry the command: %s", profile)
		}
		return nil, err
	}
	return func() { syscall.CloseHandle(handle) }, nil
}
