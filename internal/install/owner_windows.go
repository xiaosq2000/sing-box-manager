//go:build windows

package install

import "io/fs"

func fileOwner(fs.FileInfo) (int, bool) { return 0, false }
