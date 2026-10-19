package maintenance

import (
	"errors"
	"os"
	"path/filepath"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

type busyError struct{}

func (busyError) Error() string {
	return i18n.T("another sbc maintenance or configuration operation is running. Try again after it finishes")
}

var ErrBusy error = busyError{}

// Lease owns an OS lock. Close is idempotent. Only an engine lease may inherit
// its descriptor across Unix exec, immediately before executing the engine.
type Lease struct {
	file    *os.File
	release func()
	engine  bool
}

func (l *Lease) Close() {
	if l != nil && l.release != nil {
		l.release()
		l.release = nil
	}
}

func acquire(l paths.Layout, suffix string, shared bool) (*Lease, error) {
	if err := os.MkdirAll(filepath.Dir(l.Config), 0o700); err != nil {
		return nil, err
	}
	return lockFile(l.Config+suffix, shared)
}

// Lock is nonblocking and is released by the OS if its process exits.
func Lock(l paths.Layout) (func(), error) {
	lease, err := acquire(l, ".operation.lock", false)
	if err != nil {
		return nil, err
	}
	return lease.Close, nil
}

func LockEngine(l paths.Layout) (*Lease, error) {
	lease, err := acquire(l, ".engine.lock", false)
	if err == nil {
		lease.engine = true
	}
	return lease, err
}

func EngineBusy(l paths.Layout) (bool, error) {
	lease, err := LockEngine(l)
	if errors.Is(err, ErrBusy) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	lease.Close()
	return false, nil
}

// Permission must be shared before a mutator checks engine startup and writes
// live files. Exclusive permission grants startup only with a complete file set.
// A short retry accommodates shared probes and process-exit descriptor cleanup.
func Permission(l paths.Layout, grant bool) (*Lease, error) {
	deadline := time.Now().Add(time.Second)
	for {
		lease, err := acquire(l, ".startup.lock", !grant)
		if !errors.Is(err, ErrBusy) || time.Now().After(deadline) {
			return lease, err
		}
		time.Sleep(time.Millisecond)
	}
}

// StartGranted must be called while the wrapper owns the engine lease. A shared
// probe cannot impersonate an exclusive grant, even with concurrent probes.
func StartGranted(l paths.Layout) (bool, error) {
	lease, err := acquire(l, ".startup.lock", true)
	if errors.Is(err, ErrBusy) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	lease.Close()
	return false, nil
}
