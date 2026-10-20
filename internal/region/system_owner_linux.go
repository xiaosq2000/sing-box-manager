//go:build linux

package region

import (
	"os"
	"syscall"
)

// ownedByRoot reports whether the file belongs to the root account.
func ownedByRoot(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == 0
}
