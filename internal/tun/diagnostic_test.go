package tun

import "testing"

func TestDiagnosticMetadataDoesNotInventGenerationPolicy(t *testing.T) {
	for _, item := range []struct{ config, tag, policy string }{
		{`{"inbounds":[{"type":"tun","tag":"fixture"}],"dns":{"strategy":"ipv4_only"},"route":{"rules":[{"ip_is_private":true,"outbound":"direct"},{"ip_version":6,"action":"reject"}]}}`, "fixture", "public-ipv6-disabled-v1"},
		{`{"inbounds":[{"type":"tun","tag":"other"}],"dns":{"strategy":"prefer_ipv4"},"route":{"rules":[{"ip_version":6,"action":"reject"}]}}`, "other", ""},
		{`{"inbounds":[{"type":"tun","tag":"fixture"}],"dns":{"strategy":"ipv4_only"},"route":{"rules":[{"outbound":"direct"},{"ip_version":6,"action":"reject"}]}}`, "fixture", ""},
		{`{"inbounds":[{"type":"tun","tag":"fixture"}],"dns":{"strategy":"ipv4_only"},"route":{"rules":[{"domain":"narrow.test","ip_version":6,"action":"reject"}]}}`, "fixture", ""},
		{`invalid`, "", ""},
	} {
		tag, policy := DiagnosticMetadata([]byte(item.config))
		if tag != item.tag || policy != item.policy {
			t.Fatalf("%q %q from %s", tag, policy, item.config)
		}
	}
}
