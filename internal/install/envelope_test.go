package install

import (
	"encoding/json"
	"testing"
)

func TestEnvelopeClientRegionUsesTheDefaultNode(t *testing.T) {
	var envelope Envelope
	if err := json.Unmarshal([]byte(`{
		"version": 1,
		"default_node": "node-b",
		"nodes": [
			{"name": "node-a", "protocols": ["trojan"], "region": {"timezone": "UTC", "language": "en-US"}},
			{"name": "node-b", "protocols": ["trojan"], "region": {"timezone": "Europe/Berlin", "language": "de-DE"}}
		]
	}`), &envelope); err != nil {
		t.Fatal(err)
	}

	profile, node, err := envelope.ClientRegion()
	if err != nil {
		t.Fatal(err)
	}
	if node != "node-b" || profile == nil || profile.Timezone != "Europe/Berlin" || profile.Language != "de-DE" {
		t.Fatalf("unexpected region: %#v %q", profile, node)
	}
}

func TestEnvelopeClientRegionSupportsASingleOldNode(t *testing.T) {
	var envelope Envelope
	if err := json.Unmarshal([]byte(`{
		"version": 1,
		"nodes": [{"name": "node-a", "protocols": ["trojan"], "region": {"timezone": "UTC", "language": "en-US"}}]
	}`), &envelope); err != nil {
		t.Fatal(err)
	}

	profile, node, err := envelope.ClientRegion()
	if err != nil {
		t.Fatal(err)
	}
	if node != "node-a" || profile == nil || profile.Timezone != "UTC" {
		t.Fatalf("unexpected region: %#v %q", profile, node)
	}
}

func TestEnvelopeClientRegionIsOptional(t *testing.T) {
	var envelope Envelope
	if err := json.Unmarshal([]byte(`{
		"version": 1,
		"default_node": "node-a",
		"nodes": [{"name": "node-a", "protocols": ["trojan"]}]
	}`), &envelope); err != nil {
		t.Fatal(err)
	}

	profile, node, err := envelope.ClientRegion()
	if err != nil {
		t.Fatal(err)
	}
	if node != "node-a" || profile != nil {
		t.Fatalf("unexpected region: %#v %q", profile, node)
	}
}

func TestEnvelopeClientRegionRejectsAmbiguousNodes(t *testing.T) {
	var envelope Envelope
	if err := json.Unmarshal([]byte(`{
		"version": 1,
		"nodes": [
			{"name": "node-a", "protocols": ["trojan"]},
			{"name": "node-b", "protocols": ["trojan"]}
		]
	}`), &envelope); err != nil {
		t.Fatal(err)
	}
	if _, _, err := envelope.ClientRegion(); err == nil {
		t.Fatal("accepted multiple nodes without a default")
	}
}

func TestEnvelopeClientRegionRejectsAMissingDefault(t *testing.T) {
	var envelope Envelope
	if err := json.Unmarshal([]byte(`{
		"version": 1,
		"default_node": "node-z",
		"nodes": [{"name": "node-a", "protocols": ["trojan"]}]
	}`), &envelope); err != nil {
		t.Fatal(err)
	}
	if _, _, err := envelope.ClientRegion(); err == nil {
		t.Fatal("accepted a missing default node")
	}
}
