//go:build !linux

package region

import "os"

// ownedByRoot is not checked outside Linux, where the system scope is
// unavailable.
func ownedByRoot(os.FileInfo) bool { return true }
