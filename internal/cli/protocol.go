package cli

import "github.com/xiaosq2000/sing-box-manager/internal/i18n"

// protocolDescription describes managed protocol tags, not arbitrary API labels.
func protocolDescription(name string) string {
	switch name {
	case "snell":
		return i18n.T("Aims to reduce recognizable traffic patterns at the cost of some extra traffic.")
	case "trojan":
		return i18n.T("The GFW may detect and block this traffic, so this project does not recommend it.")
	case "hysteria2":
		return i18n.T("Prioritizes speed on unstable networks but may use more data, fail to connect if the network blocks UDP, or lose speed if it throttles UDP.")
	case "naive":
		return i18n.T("Mimics real browser connections to reduce detection risk, possibly at the cost of some speed.")
	default:
		return ""
	}
}
