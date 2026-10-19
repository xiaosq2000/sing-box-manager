package maintenance

import (
	"os"
	"syscall"
	"unsafe"
)

func lockFile(path string) (func(), error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	kernel := syscall.NewLazyDLL("kernel32.dll")
	lock := kernel.NewProc("LockFileEx")
	unlock := kernel.NewProc("UnlockFileEx")
	overlapped := new(syscall.Overlapped)
	ok, _, callErr := lock.Call(file.Fd(), 3, 0, 1, 0, uintptr(unsafe.Pointer(overlapped)))
	if ok == 0 {
		file.Close()
		if callErr == syscall.Errno(33) {
			return nil, ErrBusy
		}
		return nil, callErr
	}
	return func() { unlock.Call(file.Fd(), 0, 1, 0, uintptr(unsafe.Pointer(overlapped))); file.Close() }, nil
}
