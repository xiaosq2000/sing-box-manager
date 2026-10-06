//go:build linux || darwin

package desktop

import (
	"os"
	"path/filepath"
	"syscall"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// flock also serializes separate sbc processes. The kernel releases the lock
// after a crash. Keep the file in place when unlocking to avoid inode races.
func lockUnixPrivacy(path string) (func(), error) {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, err
	}
	fd, err := syscall.Open(path, syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0600)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	if err := syscall.Flock(fd, syscall.LOCK_EX); err != nil {
		file.Close()
		return nil, i18n.Errorf("could not lock WebRTC settings: %w", err)
	}
	return func() { syscall.Flock(fd, syscall.LOCK_UN); file.Close() }, nil
}
