//go:build linux

package region

import (
	"os"
	"path/filepath"
	"syscall"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// lockSystemRegion serializes separate root-side sbc processes. The kernel
// releases the lock after a crash. Keep the file in place when unlocking to
// avoid inode races.
func lockSystemRegion(path string) (func(), error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	fd, err := syscall.Open(path, syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0o600)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	if err := syscall.Flock(fd, syscall.LOCK_EX); err != nil {
		_ = file.Close()
		return nil, i18n.Errorf("could not lock the system region settings: %w", err)
	}
	return func() {
		_ = syscall.Flock(fd, syscall.LOCK_UN)
		_ = file.Close()
	}, nil
}
