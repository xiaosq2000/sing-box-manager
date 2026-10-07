//go:build !linux && !darwin && !windows

package desktop

import "github.com/xiaosq2000/sing-box-manager/internal/i18n"

func lockFirefoxProfile(string) (func(), error) {
	return nil, i18n.New("WebRTC settings are unavailable on this platform")
}
