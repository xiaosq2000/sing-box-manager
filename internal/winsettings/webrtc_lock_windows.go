package winsettings

import (
	"os/user"
	"runtime"
	"syscall"
	"unsafe"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

func lockWebRTCProcess() (func(), error) {
	account, err := user.Current()
	if err != nil || !validWebRTCSID(account.Uid) {
		return nil, i18n.New("Windows could not determine a valid SID for the original user")
	}
	// Share across sessions and installations for this user. This mutex differs
	// from the registry helper's mutex, which the elevated child acquires.
	return lockWebRTCMutex(`Global\sbc.WebRTC.Operation.` + account.Uid)
}

func lockWebRTCMutex(name string) (func(), error) {
	nameUTF16, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		return nil, err
	}
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	createMutex := kernel32.NewProc("CreateMutexExW")
	releaseMutex := kernel32.NewProc("ReleaseMutex")
	// Request only SYNCHRONIZE and MUTEX_MODIFY_STATE when opening the object.
	handle, _, err := createMutex.Call(0, uintptr(unsafe.Pointer(nameUTF16)), 0, 0x00100001)
	if handle == 0 {
		return nil, err
	}
	// Windows assigns mutex ownership to a thread, so release on that thread.
	runtime.LockOSThread()
	status, err := syscall.WaitForSingleObject(syscall.Handle(handle), syscall.INFINITE)
	if err == nil && status != syscall.WAIT_OBJECT_0 && status != syscall.WAIT_ABANDONED {
		err = syscall.EINVAL
	}
	if err != nil {
		syscall.CloseHandle(syscall.Handle(handle))
		runtime.UnlockOSThread()
		return nil, err
	}
	// An abandoned mutex transfers ownership. The caller rechecks mode state,
	// policies and ownership records before completing or retrying any change.
	return func() {
		releaseMutex.Call(handle)
		syscall.CloseHandle(syscall.Handle(handle))
		runtime.UnlockOSThread()
	}, nil
}
