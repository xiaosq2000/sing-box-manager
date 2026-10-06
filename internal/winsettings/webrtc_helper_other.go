//go:build !windows

package winsettings

import "github.com/xiaosq2000/sing-box-manager/internal/i18n"

func changeWebRTCPolicies(_ string) error {
	return i18n.New("the native WebRTC policy helper is available only on Windows")
}
