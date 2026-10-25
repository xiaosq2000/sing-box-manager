package traffic

import (
	"strings"
	"testing"
)

func TestCanonicalVectors(t *testing.T) {
	fixture := loadFixture(t)
	for _, vector := range fixture.CanonicalVectors {
		t.Run(vector.Name, func(t *testing.T) {
			snapshot := vector.Snapshot.typed(t)
			payload, err := CanonicalPayload(snapshot)
			if err != nil {
				t.Fatalf("canonical payload: %v", err)
			}
			if string(payload) != vector.PayloadCanonical {
				t.Fatalf("payload bytes = %q, want %q", payload, vector.PayloadCanonical)
			}
			canonical, err := CanonicalSnapshot(snapshot)
			if err != nil {
				t.Fatalf("canonical snapshot: %v", err)
			}
			if string(canonical) != vector.SnapshotCanonical {
				t.Fatalf("snapshot bytes = %q, want %q", canonical, vector.SnapshotCanonical)
			}
			digest, err := SnapshotDigest(snapshot)
			if err != nil {
				t.Fatalf("snapshot digest: %v", err)
			}
			if digest != vector.Snapshot.Digest {
				t.Fatalf("digest = %s, want %s", digest, vector.Snapshot.Digest)
			}

			raw := append([]byte(`{"schema":1,"snapshots":[`), canonical...)
			raw = append(raw, ']', '}')
			parsed, err := ParseRequest(raw)
			if err != nil {
				t.Fatalf("parse request: %v", err)
			}
			if len(parsed.Snapshots) != 1 {
				t.Fatalf("parsed %d snapshots, want 1", len(parsed.Snapshots))
			}
			parsedCanonical, err := CanonicalSnapshot(parsed.Snapshots[0])
			if err != nil {
				t.Fatalf("parsed canonical snapshot: %v", err)
			}
			if string(parsedCanonical) != vector.SnapshotCanonical {
				t.Fatalf("parsed snapshot bytes = %q, want %q", parsedCanonical, vector.SnapshotCanonical)
			}
			if parsed.Snapshots[0].Key() != snapshot.Key() {
				t.Fatalf("parsed key = %+v, want %+v", parsed.Snapshots[0].Key(), snapshot.Key())
			}
		})
	}
}

func TestCounterAnchor(t *testing.T) {
	fixture := loadFixture(t)
	counter := vectorByName(t, fixture, "counter_v1").Snapshot.typed(t)
	payload, err := CanonicalPayload(counter)
	if err != nil {
		t.Fatalf("canonical payload: %v", err)
	}
	canonical, err := CanonicalSnapshot(counter)
	if err != nil {
		t.Fatalf("canonical snapshot: %v", err)
	}
	if len(payload) != fixture.Bounds.CounterV1PayloadBytes {
		t.Fatalf("payload bytes = %d, want %d", len(payload), fixture.Bounds.CounterV1PayloadBytes)
	}
	if len(canonical) != fixture.Bounds.CounterV1SnapshotBytes {
		t.Fatalf("snapshot bytes = %d, want %d", len(canonical), fixture.Bounds.CounterV1SnapshotBytes)
	}
	maximum := vectorByName(t, fixture, "counter_max_v1").Snapshot.typed(t)
	maximumCanonical, err := CanonicalSnapshot(maximum)
	if err != nil {
		t.Fatalf("maximum canonical snapshot: %v", err)
	}
	if len(maximumCanonical) != fixture.Bounds.CounterMaxSnapshotBytes {
		t.Fatalf("maximum snapshot bytes = %d, want %d", len(maximumCanonical), fixture.Bounds.CounterMaxSnapshotBytes)
	}
}

func TestRequestEnvelopeVectors(t *testing.T) {
	fixture := loadFixture(t)
	for _, vector := range fixture.RequestVectors {
		t.Run(vector.Name, func(t *testing.T) {
			snapshots := make([]Snapshot, len(vector.Snapshots))
			for index, name := range vector.Snapshots {
				snapshots[index] = vectorByName(t, fixture, name).Snapshot.typed(t)
			}
			request, err := MakeRequest(snapshots...)
			if err != nil {
				t.Fatalf("make request: %v", err)
			}
			encoded, err := EncodeRequest(request)
			if err != nil {
				t.Fatalf("encode request: %v", err)
			}
			if string(encoded) != vector.RequestCanonical {
				t.Fatalf("request bytes = %q, want %q", encoded, vector.RequestCanonical)
			}
			parsed, err := ParseRequest([]byte(vector.RequestCanonical))
			if err != nil {
				t.Fatalf("parse request: %v", err)
			}
			reencoded, err := EncodeRequest(parsed)
			if err != nil {
				t.Fatalf("re-encode request: %v", err)
			}
			if string(reencoded) != vector.RequestCanonical {
				t.Fatalf("re-encoded request bytes = %q, want %q", reencoded, vector.RequestCanonical)
			}
		})
	}
}

func TestEquivalenceVectors(t *testing.T) {
	fixture := loadFixture(t)
	for _, vector := range fixture.EquivalenceVectors {
		t.Run(vector.Name, func(t *testing.T) {
			request, err := ParseRequest([]byte(vector.RequestRaw))
			if err != nil {
				t.Fatalf("parse request: %v", err)
			}
			if len(request.Snapshots) != 1 {
				t.Fatalf("parsed %d snapshots, want 1", len(request.Snapshots))
			}
			snapshot := request.Snapshots[0]
			target := vectorByName(t, fixture, vector.Vector)
			payload, err := CanonicalPayload(snapshot)
			if err != nil {
				t.Fatalf("canonical payload: %v", err)
			}
			if string(payload) != target.PayloadCanonical {
				t.Fatalf("payload bytes = %q, want %q", payload, target.PayloadCanonical)
			}
			canonical, err := CanonicalSnapshot(snapshot)
			if err != nil {
				t.Fatalf("canonical snapshot: %v", err)
			}
			if string(canonical) != target.SnapshotCanonical {
				t.Fatalf("snapshot bytes = %q, want %q", canonical, target.SnapshotCanonical)
			}
			digest, ok := snapshotDigestValue(snapshot)
			if !ok {
				t.Fatal("snapshot digest value missing")
			}
			if digest != target.Snapshot.Digest {
				t.Fatalf("digest = %s, want %s", digest, target.Snapshot.Digest)
			}
		})
	}
}

func TestCanonicalJSONRule(t *testing.T) {
	encoded, err := canonicalJSONBytes(map[string]any{
		"b": "é<&>",
		"a": "\u2028\u2029",
		"n": int64(1),
	})
	if err != nil {
		t.Fatalf("canonical JSON: %v", err)
	}
	want := `{"a":"\u2028\u2029","b":"é<&>","n":1}`
	if string(encoded) != want {
		t.Fatalf("canonical JSON = %q, want %q", encoded, want)
	}
	encoded, err = canonicalJSONBytes(map[string]any{"c": "\x00\x01\b\f\n\r\t"})
	if err != nil {
		t.Fatalf("canonical controls: %v", err)
	}
	if string(encoded) != `{"c":"\u0000\u0001\b\f\n\r\t"}` {
		t.Fatalf("canonical controls = %q", encoded)
	}
}

func TestUnicodeNamesArePreserved(t *testing.T) {
	fixture := loadFixture(t)
	names := []string{
		"café.example",
		"cafe\u0301.example",
		" spaced name ",
		"replace\uFFFDok",
		"line\u2028sep\u2029end",
		"control\u0001\b\f\n\r\t",
	}
	entries := make([]Entry, len(names))
	for index, name := range names {
		entries[index] = Entry{Name: name, Upload: 1, Download: 2, Connections: 3}
	}
	snapshot, err := MakeAttributionSnapshot(
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		Date(fixture.Identities["accounting_date"]),
		1,
		DetailExact,
		entries,
		nil,
	)
	if err != nil {
		t.Fatalf("make attribution snapshot: %v", err)
	}
	canonical, err := CanonicalSnapshot(snapshot)
	if err != nil {
		t.Fatalf("canonical snapshot: %v", err)
	}
	text := string(canonical)
	for _, name := range names {
		if name == "line\u2028sep\u2029end" || name == "control\u0001\b\f\n\r\t" {
			continue
		}
		if !strings.Contains(text, name) {
			t.Errorf("canonical snapshot lost name %q", name)
		}
	}
	if strings.Contains(text, "\u2028") || strings.Contains(text, "\u2029") {
		t.Error("canonical snapshot contains literal line separators")
	}
	if !strings.Contains(text, `\u2028`) || !strings.Contains(text, `\u2029`) {
		t.Error("canonical snapshot lacks line separator escapes")
	}

	request, err := MakeRequest(snapshot)
	if err != nil {
		t.Fatalf("make unicode request: %v", err)
	}
	encoded, err := EncodeRequest(request)
	if err != nil {
		t.Fatalf("encode unicode request: %v", err)
	}
	parsed, err := ParseRequest(encoded)
	if err != nil {
		t.Fatalf("parse unicode request: %v", err)
	}
	parsedSnapshot, ok := parsed.Snapshots[0].(AttributionSnapshot)
	if !ok {
		t.Fatalf("parsed snapshot type = %T", parsed.Snapshots[0])
	}
	replacementFound := false
	for _, entry := range parsedSnapshot.Domains {
		if entry.Name == "replace\uFFFDok" {
			replacementFound = true
		}
	}
	if !replacementFound {
		t.Fatal("parsed snapshot lost the literal U+FFFD name")
	}

	composed, err := MakeAttributionSnapshot(
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		Date(fixture.Identities["accounting_date"]),
		1,
		DetailExact,
		[]Entry{{Name: "café.example", Upload: 1, Download: 2, Connections: 3}},
		nil,
	)
	if err != nil {
		t.Fatalf("make composed snapshot: %v", err)
	}
	decomposed, err := MakeAttributionSnapshot(
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		Date(fixture.Identities["accounting_date"]),
		1,
		DetailExact,
		[]Entry{{Name: "cafe\u0301.example", Upload: 1, Download: 2, Connections: 3}},
		nil,
	)
	if err != nil {
		t.Fatalf("make decomposed snapshot: %v", err)
	}
	if composed.Digest == decomposed.Digest {
		t.Fatal("NFC and NFD names produced the same digest")
	}
}
