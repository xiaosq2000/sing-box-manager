package singbox

import "encoding/json"

// ProxyOnlyProbePort validates the managed diagnostic inlet before AI requests.
// Unknown configs fail closed. The first rule must send the inlet straight to
// a selector whose choices are all remote proxy outbounds, never direct.
func ProxyOnlyProbePort(config []byte) (int, bool) {
	var root struct {
		Inbounds []struct {
			Type   string `json:"type"`
			Tag    string `json:"tag"`
			Listen string `json:"listen"`
			Port   int    `json:"listen_port"`
		} `json:"inbounds"`
		Outbounds []struct {
			Type    string   `json:"type"`
			Tag     string   `json:"tag"`
			Choices []string `json:"outbounds"`
		} `json:"outbounds"`
		Route struct {
			Rules []map[string]json.RawMessage `json:"rules"`
		} `json:"route"`
	}
	if json.Unmarshal(config, &root) != nil || len(root.Route.Rules) == 0 {
		return 0, false
	}
	first := root.Route.Rules[0]
	if len(first) != 2 {
		return 0, false
	}
	var target string
	var inlets []string
	if json.Unmarshal(first["outbound"], &target) != nil || target != SpeedTestTag ||
		json.Unmarshal(first["inbound"], &inlets) != nil || len(inlets) != 1 || inlets[0] != SpeedTestTag {
		return 0, false
	}
	remote := map[string]bool{}
	var choices []string
	for _, outbound := range root.Outbounds {
		if outbound.Tag == SpeedTestTag && outbound.Type == "selector" {
			choices = outbound.Choices
		}
		switch outbound.Type {
		case "trojan", "hysteria2", "naive", "snell", "socks", "http":
			remote[outbound.Tag] = true
		}
	}
	if len(choices) == 0 {
		return 0, false
	}
	for _, tag := range choices {
		if !remote[tag] {
			return 0, false
		}
	}
	for _, inbound := range root.Inbounds {
		if inbound.Tag == SpeedTestTag && inbound.Type == "mixed" && inbound.Listen == "127.0.0.1" && inbound.Port > 0 && inbound.Port <= 65535 {
			return inbound.Port, true
		}
	}
	return 0, false
}
