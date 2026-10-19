//go:build !windows

package maintenance

import (
	"errors"
	"os"
	"runtime"
	"syscall"
)

func lockFile(path string, shared bool) (*Lease, error) {
	fd, err := syscall.Open(path, syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0o600)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	mode := syscall.LOCK_EX
	if shared {
		mode = syscall.LOCK_SH
	}
	if err = syscall.Flock(fd, mode|syscall.LOCK_NB); err != nil {
		file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, ErrBusy
		}
		return nil, err
	}
	return &Lease{file: file, release: func() { syscall.Flock(fd, syscall.LOCK_UN); file.Close() }}, nil
}

// KeepAcrossExec is called after all setup subprocesses finish. The release
// closure retains file, so its finalizer cannot close the inherited descriptor.
func (l *Lease) KeepAcrossExec() error {
	if !l.engine || l.release == nil {
		return syscall.EINVAL
	}
	_, _, errno := syscall.Syscall(syscall.SYS_FCNTL, l.file.Fd(), syscall.F_SETFD, 0)
	runtime.KeepAlive(l)
	if errno != 0 {
		return errno
	}
	return nil
}
