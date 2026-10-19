package maintenance

import (
	"os"
	"path/filepath"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

type busyError struct{}

func (busyError) Error() string {
	return i18n.T("another sbc maintenance or configuration operation is running. Try again after it finishes")
}

var ErrBusy error = busyError{}

// Lock is nonblocking and is released by the OS if its process exits.
func Lock(l paths.Layout) (func(), error) {
	if err := os.MkdirAll(filepath.Dir(l.Config), 0o700); err != nil {
		return nil, err
	}
	return lockFile(l.Config + ".operation.lock")
}
