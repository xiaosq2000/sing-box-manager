//go:build !linux

package desktop

import "sync"

// The Chrome backend runs only on Linux; this lock keeps the package buildable elsewhere.
var chromePrivacyMu sync.Mutex

func lockUnixPrivacy(_ string) (func(), error) {
	chromePrivacyMu.Lock()
	return chromePrivacyMu.Unlock, nil
}
