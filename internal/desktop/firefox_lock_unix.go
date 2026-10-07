//go:build linux || darwin

package desktop

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Firefox uses a POSIX record lock on .parentlock. Hold the same lock during
// cleanup, so Firefox cannot race or overwrite the restored preferences.
func lockFirefoxProfile(profile string) (func(), error) {
	path := filepath.Join(profile, ".parentlock")
	if info, err := os.Lstat(path); err == nil && !info.Mode().IsRegular() {
		return nil, i18n.Errorf("WebRTC settings path is not a regular file: %s", path)
	} else if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	lock := syscall.Flock_t{Type: syscall.F_WRLCK, Whence: 0}
	if err := syscall.FcntlFlock(file.Fd(), syscall.F_SETLK, &lock); err != nil {
		file.Close()
		if errors.Is(err, syscall.EACCES) || errors.Is(err, syscall.EAGAIN) {
			return nil, i18n.Errorf("Firefox profile is in use; close Firefox and retry the command: %s", profile)
		}
		return nil, err
	}
	return func() { file.Close() }, nil
}
