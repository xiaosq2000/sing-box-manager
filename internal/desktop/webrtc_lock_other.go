//go:build !linux && !darwin

package desktop

import "sync"

// Unix backends run only with fixture runners on other platforms.
var unixPrivacyMu sync.Mutex

func lockUnixPrivacy(_ string) (func(), error) {
	unixPrivacyMu.Lock()
	return unixPrivacyMu.Unlock, nil
}
