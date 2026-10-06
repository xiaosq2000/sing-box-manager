package winsettings

import (
	"errors"
	"testing"
)

func TestWebRTCEdgeUsesSupportedPolicyAndCleansOwnedLegacyValue(t *testing.T) {
	for _, action := range []string{"on", "off", "uninstall"} {
		t.Run(action, func(t *testing.T) {
			w, registry, _ := webRTCFixture(t)
			// State written by e4a200f2. Use literal names to catch a shared
			// policy-name mistake instead of deriving expectations from code.
			registry.put(EdgePolicyKey, "WebRtcIPHandling", Value{"disable_non_proxied_udp", "String"})
			registry.put(WebRTCMetadataKey, "Edge", Value{"1", "DWord"})
			var err error
			if action == "uninstall" {
				err = w.Cleanup()
			} else {
				err = w.Set(action == "on")
			}
			if err != nil {
				t.Fatal(err)
			}
			if registry.values[EdgePolicyKey]["WebRtcIPHandling"].Kind != "" || registry.values[WebRTCMetadataKey]["Edge"].Kind != "" {
				t.Fatal("obsolete owned Edge policy remains")
			}
			want := Value{}
			if action == "on" {
				want = Value{"disable_non_proxied_udp", "String"}
			}
			if registry.values[EdgePolicyKey]["WebRtcLocalhostIpHandling"] != want {
				t.Fatal("Edge's supported policy does not match the requested state")
			}
			if err := w.Cleanup(); err != nil {
				t.Fatal(err)
			}
			if registry.values[EdgePolicyKey]["WebRtcLocalhostIpHandling"].Kind != "" {
				t.Fatal("cleanup left the new owned Edge policy")
			}
		})
	}
}

func TestWebRTCEdgeMigrationPreservesUnownedAndChangedValues(t *testing.T) {
	for _, test := range []struct {
		name   string
		legacy Value
		owned  bool
	}{
		{"unowned", Value{"disable_non_proxied_udp", "String"}, false},
		{"changed value", Value{"default", "String"}, true},
		{"changed type", Value{"disable_non_proxied_udp", "ExpandString"}, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			w, registry, _ := webRTCFixture(t)
			manual := Value{"disable_non_proxied_udp", "String"}
			registry.put(EdgePolicyKey, "WebRtcLocalhostIpHandling", manual)
			registry.put(EdgePolicyKey, "WebRtcIPHandling", test.legacy)
			if test.owned {
				registry.put(WebRTCMetadataKey, "Edge", Value{"1", "DWord"})
			}
			if err := w.Set(true); err != nil {
				t.Fatal(err)
			}
			if err := w.Set(false); err != nil {
				t.Fatal(err)
			}
			if registry.values[EdgePolicyKey]["WebRtcLocalhostIpHandling"] != manual || registry.values[EdgePolicyKey]["WebRtcIPHandling"] != test.legacy {
				t.Fatal("migration or cleanup changed a pre-existing or externally changed value")
			}
			if registry.values[WebRTCMetadataKey][WebRTCEdgeOwnerName].Kind != "" || registry.values[WebRTCMetadataKey]["Edge"].Kind != "" {
				t.Fatal("migration retained or transferred legacy ownership")
			}
		})
	}
}

func TestWebRTCEdgeMigrationRetriesLegacyCleanupWhenActivePoliciesExist(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	if err := w.Ensure(); err != nil {
		t.Fatal(err)
	}
	registry.put(EdgePolicyKey, "WebRtcIPHandling", Value{"disable_non_proxied_udp", "String"})
	registry.put(WebRTCMetadataKey, "Edge", Value{"1", "DWord"})
	failure := errors.New("fixture: legacy deletion denied")
	registry.fail = func(key, name string, value Value) error {
		if key == EdgePolicyKey && name == "WebRtcIPHandling" && value.Kind == "" {
			return failure
		}
		return nil
	}
	if err := w.Ensure(); !errors.Is(err, failure) {
		t.Fatalf("migration hid its cleanup failure: %v", err)
	}
	if registry.values[WebRTCMetadataKey]["Edge"].Kind == "" {
		t.Fatal("failed migration lost the cleanup intent")
	}
	registry.fail = nil
	if err := w.Ensure(); err != nil {
		t.Fatal(err)
	}
	if len(*changes) != 3 || registry.values[EdgePolicyKey]["WebRtcIPHandling"].Kind != "" {
		t.Fatal("existing supported policies prevented legacy cleanup retry")
	}
	if err := w.Ensure(); err != nil || len(*changes) != 3 {
		t.Fatalf("completed migration requested another helper: %v", err)
	}
}
