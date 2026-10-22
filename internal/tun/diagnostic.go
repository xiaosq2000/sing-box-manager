package tun

import "encoding/json"

// DiagnosticMetadata describes the source configuration before root stages it.
// Unknown policy remains unknown. Diagnostics never read the privileged config.
func DiagnosticMetadata(config []byte) (tag, ipv6Policy string) {
	var root struct {
		Inbounds []struct {
			Type string `json:"type"`
			Tag  string `json:"tag"`
		} `json:"inbounds"`
		DNS struct {
			Strategy string `json:"strategy"`
		} `json:"dns"`
		Route struct {
			Rules []map[string]any `json:"rules"`
		} `json:"route"`
	}
	if json.Unmarshal(config, &root) != nil {
		return "", ""
	}
	for _, inbound := range root.Inbounds {
		if inbound.Type == "tun" {
			if tag != "" {
				return "", ""
			}
			tag = inbound.Tag
		}
	}
	if root.DNS.Strategy != "ipv4_only" {
		return tag, ""
	}
	for _, rule := range root.Route.Rules {
		// An earlier public route makes the broad rejection claim unsafe.
		if _, routed := rule["outbound"]; routed && rule["ip_is_private"] != true {
			return tag, ""
		}
		if len(rule) == 2 && rule["ip_version"] == float64(6) && rule["action"] == "reject" {
			return tag, "public-ipv6-disabled-v1"
		}
	}
	return tag, ""
}
