//go:build !windows

package winsettings

// Other platforms use WebRTC only in fixtures. The shared Go mutex suffices.
func lockWebRTCProcess() (func(), error) {
	return func() {}, nil
}
