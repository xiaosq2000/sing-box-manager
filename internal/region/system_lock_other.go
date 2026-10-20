//go:build !linux

package region

import "sync"

// The system scope runs only on Linux; this lock keeps the package buildable
// elsewhere.
var systemRegionMu sync.Mutex

func lockSystemRegion(string) (func(), error) {
	systemRegionMu.Lock()
	return systemRegionMu.Unlock, nil
}
