package maintenance

import (
	"os"
	"syscall"
	"unsafe"
)

var (
	kernel = syscall.NewLazyDLL("kernel32.dll")
	lock   = kernel.NewProc("LockFileEx")
	unlock = kernel.NewProc("UnlockFileEx")
)

func lockFile(path string, shared bool) (*Lease, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	flags := uintptr(3) // FAIL_IMMEDIATELY | EXCLUSIVE_LOCK
	if shared {
		flags = 1
	}
	overlapped := new(syscall.Overlapped)
	ok, _, callErr := lock.Call(file.Fd(), flags, 0, 1, 0, uintptr(unsafe.Pointer(overlapped)))
	if ok == 0 {
		file.Close()
		if callErr == syscall.Errno(33) {
			return nil, ErrBusy
		}
		return nil, callErr
	}
	return &Lease{file: file, release: func() { unlock.Call(file.Fd(), 0, 1, 0, uintptr(unsafe.Pointer(overlapped))); file.Close() }}, nil
}

// Windows keeps the lease in its existing wrapper, not in the child process.
func (l *Lease) KeepAcrossExec() error {
	if !l.engine || l.release == nil {
		return syscall.EINVAL
	}
	return nil
}
