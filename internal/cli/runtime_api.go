package cli

import (
	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

func captureOff(capture tun.CaptureState) bool {
	return capture.Status == "off" && (capture.Phase == "" || capture.Phase == "off")
}

func activeTunGeneration(capture tun.CaptureState) bool {
	return capture.Status == "active" && capture.Phase == "active" && capture.Transaction != ""
}

// selectionAPI chooses one runtime before any API call. A TUN error must never
// select the mixed API, even if an old mixed endpoint still answers.
func selectionAPI(env Env, layout paths.Layout) (*api.Client, error) {
	capture := tunCapture(env)
	if captureOff(capture) {
		client, _, err := localAPI(layout)
		return client, err
	}
	return activeTunAPI(layout, capture)
}

// activeTunAPI uses only the owner's saved state, not the mixed configuration
// or the privileged engine configuration. It performs no recovery or sudo.
func activeTunAPI(layout paths.Layout, capture tun.CaptureState) (*api.Client, error) {
	if !activeTunGeneration(capture) {
		return nil, i18n.New("TUN commands need a committed active generation. Run 'sbc tun off' to repair dirty state.")
	}
	state, err := tun.ReadState(layout)
	if err != nil || state.APIPort < 1 || state.APIPort > 65535 || state.APISecret == "" || state.Interface != tun.InterfaceName {
		return nil, i18n.New("The active TUN has no valid saved API state for this user. Run 'sbc tun off' as its owner.")
	}
	return api.New(state.APIPort, state.APISecret), nil
}
