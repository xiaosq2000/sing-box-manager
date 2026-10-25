package traffic

import (
	"bytes"
	"fmt"
	"slices"
	"strings"
	"testing"
)

func TestBoundsFixture(t *testing.T) {
	fixture := loadFixture(t)
	bounds := fixture.Bounds
	if bounds.RawBodyBytes != MaxRawBodyBytes ||
		bounds.SnapshotBytes != MaxSnapshotBytes ||
		bounds.PageItems != MaxPageItems ||
		bounds.NamedIdentities != MaxNamedIdentities {
		t.Fatalf("fixture bounds do not match codec constants: %+v", bounds)
	}
	if bounds.CounterV1PayloadBytes != 197 ||
		bounds.CounterV1SnapshotBytes != 273 ||
		bounds.CounterMaxSnapshotBytes != 303 {
		t.Fatalf("unexpected counter anchors: %+v", bounds)
	}
	if bounds.ResponseOverflowCode != string(ErrRequestTooLarge) {
		t.Fatalf("response overflow code = %q", bounds.ResponseOverflowCode)
	}
	if !slices.Equal(bounds.AggregateSlots.Domains, []string{"__other__", "__unattributed__", "__ip__"}) {
		t.Fatalf("domain aggregate slots = %v", bounds.AggregateSlots.Domains)
	}
	if !slices.Equal(bounds.AggregateSlots.Destinations, []string{"__other__", "__unattributed__"}) {
		t.Fatalf("destination aggregate slots = %v", bounds.AggregateSlots.Destinations)
	}
}

func TestNamedIdentityBoundaries(t *testing.T) {
	fixture := loadFixture(t)
	for _, testCase := range fixture.NamedIdentityBoundary.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			snapshot := boundarySnapshot(t, fixture, testCase)
			named := namedIdentityCount(fixture, snapshot)
			if named != testCase.ExpectedNamedIdentities {
				t.Fatalf("named identities = %d, want %d", named, testCase.ExpectedNamedIdentities)
			}
			canonical, err := CanonicalSnapshot(snapshot)
			if err != nil {
				t.Fatalf("canonical snapshot: %v", err)
			}
			if len(canonical) != testCase.ExpectedSnapshotBytes {
				t.Fatalf("snapshot bytes = %d, want %d", len(canonical), testCase.ExpectedSnapshotBytes)
			}
			if len(canonical) >= MaxSnapshotBytes {
				t.Fatalf("boundary recipe is not below the snapshot bound: %d", len(canonical))
			}
			raw := encodeUncheckedRequest(t, snapshot)
			if len(raw) != testCase.ExpectedRequestBytes {
				t.Fatalf("request bytes = %d, want %d", len(raw), testCase.ExpectedRequestBytes)
			}
			if len(raw) >= MaxRawBodyBytes {
				t.Fatalf("boundary recipe is not below the raw bound: %d", len(raw))
			}

			switch testCase.ExpectedResult {
			case "accepted":
				parsed, err := ParseRequest(raw)
				if err != nil {
					t.Fatalf("parse accepted boundary: %v", err)
				}
				if len(parsed.Snapshots) != 1 {
					t.Fatalf("parsed %d snapshots, want 1", len(parsed.Snapshots))
				}
				built, err := MakeAttributionSnapshot(
					snapshot.SourceID,
					snapshot.CredentialID,
					snapshot.AccountingDate,
					snapshot.Version,
					snapshot.Detail,
					snapshot.Domains,
					snapshot.Destinations,
				)
				if err != nil {
					t.Fatalf("make accepted boundary: %v", err)
				}
				if built.Digest != snapshot.Digest {
					t.Fatalf("built digest = %s, want %s", built.Digest, snapshot.Digest)
				}
			case "invalid_request":
				_, err := ParseRequest(raw)
				requireErrorCode(t, err, ErrInvalidRequest)
				_, err = MakeAttributionSnapshot(
					snapshot.SourceID,
					snapshot.CredentialID,
					snapshot.AccountingDate,
					snapshot.Version,
					snapshot.Detail,
					snapshot.Domains,
					snapshot.Destinations,
				)
				requireErrorCode(t, err, ErrInvalidRequest)
			default:
				t.Fatalf("unknown expected result %q", testCase.ExpectedResult)
			}
		})
	}
}

func TestSnapshotByteBoundary(t *testing.T) {
	atLimit := attributionAtSize(t, MaxSnapshotBytes, "2026-10-23")
	canonical, err := CanonicalSnapshot(atLimit)
	if err != nil {
		t.Fatalf("canonical at-limit snapshot: %v", err)
	}
	if len(canonical) != MaxSnapshotBytes {
		t.Fatalf("snapshot bytes = %d, want %d", len(canonical), MaxSnapshotBytes)
	}
	request, err := MakeRequest(atLimit)
	if err != nil {
		t.Fatalf("make at-limit request: %v", err)
	}
	encoded, err := EncodeRequest(request)
	if err != nil {
		t.Fatalf("encode at-limit request: %v", err)
	}
	if len(encoded) >= MaxRawBodyBytes {
		t.Fatalf("at-limit request unexpectedly exceeds the raw bound: %d", len(encoded))
	}
	if _, err := ParseRequest(encoded); err != nil {
		t.Fatalf("parse at-limit request: %v", err)
	}

	overLimit := attributionAtSize(t, MaxSnapshotBytes+1, "2026-10-23")
	_, err = MakeRequest(overLimit)
	requireErrorCode(t, err, ErrSnapshotTooLarge)
	_, err = ParseRequest(encodeUncheckedRequest(t, overLimit))
	requireErrorCode(t, err, ErrSnapshotTooLarge)
}

func TestFullCanonicalEnvelopeBoundary(t *testing.T) {
	first := attributionAtSize(t, MaxSnapshotBytes, "2026-10-23")
	probe := attributionAtSize(t, 400, "2026-10-24")
	probeRequest, err := MakeRequest(first, probe)
	if err != nil {
		t.Fatalf("make probe request: %v", err)
	}
	probeEncoded, err := EncodeRequest(probeRequest)
	if err != nil {
		t.Fatalf("encode probe request: %v", err)
	}
	overhead := len(probeEncoded) - 400
	secondSize := MaxRawBodyBytes - overhead
	second := attributionAtSize(t, secondSize, "2026-10-24")
	request, err := MakeRequest(first, second)
	if err != nil {
		t.Fatalf("make full-bound request: %v", err)
	}
	encoded, err := EncodeRequest(request)
	if err != nil {
		t.Fatalf("encode full-bound request: %v", err)
	}
	if len(encoded) != MaxRawBodyBytes {
		t.Fatalf("request bytes = %d, want %d", len(encoded), MaxRawBodyBytes)
	}
	if _, err := ParseRequest(encoded); err != nil {
		t.Fatalf("parse full-bound request: %v", err)
	}

	overSecond := attributionAtSize(t, secondSize+1, "2026-10-24")
	_, err = MakeRequest(first, overSecond)
	requireErrorCode(t, err, ErrRequestTooLarge)
	_, err = ParseRequest(encodeUncheckedRequest(t, first, overSecond))
	requireErrorCode(t, err, ErrRequestTooLarge)
}

func TestRawRequestAndReceiptBodyBounds(t *testing.T) {
	fixture := loadFixture(t)
	requestBytes := []byte(fixture.RequestVectors[0].RequestCanonical)
	requestAtLimit := append([]byte(nil), requestBytes...)
	requestAtLimit = append(requestAtLimit, bytes.Repeat([]byte(" "), MaxRawBodyBytes-len(requestBytes))...)
	if len(requestAtLimit) != MaxRawBodyBytes {
		t.Fatalf("request at-limit length = %d", len(requestAtLimit))
	}
	if _, err := ParseRequest(requestAtLimit); err != nil {
		t.Fatalf("parse request at raw bound: %v", err)
	}
	_, err := ParseRequest(append(append([]byte(nil), requestAtLimit...), ' '))
	requireErrorCode(t, err, ErrRequestTooLarge)

	receiptRequest, err := ParseRequest([]byte(fixture.ReceiptVectors[0].RequestRaw))
	if err != nil {
		t.Fatalf("parse receipt request: %v", err)
	}
	responseBytes := []byte(fixture.ReceiptVectors[0].ResponseRaw)
	responseAtLimit := append([]byte(nil), responseBytes...)
	responseAtLimit = append(responseAtLimit, bytes.Repeat([]byte(" "), MaxRawBodyBytes-len(responseBytes))...)
	if len(responseAtLimit) != MaxRawBodyBytes {
		t.Fatalf("response at-limit length = %d", len(responseAtLimit))
	}
	if _, err := ParseReceiptResponse(responseAtLimit, receiptRequest); err != nil {
		t.Fatalf("parse response at raw bound: %v", err)
	}
	_, err = ParseReceiptResponse(append(append([]byte(nil), responseAtLimit...), ' '), receiptRequest)
	requireErrorCode(t, err, ErrRequestTooLarge)
}

func TestAttributionBucketReplacementHasNoImmutabilityRule(t *testing.T) {
	fixture := loadFixture(t)
	first := vectorByName(t, fixture, "attribution_v1").Snapshot.typed(t)
	removed := vectorByName(t, fixture, "attribution_v2_removed").Snapshot.typed(t)
	firstSnapshot, ok := first.(AttributionSnapshot)
	if !ok {
		t.Fatal("attribution_v1 is not an attribution snapshot")
	}
	removedSnapshot, ok := removed.(AttributionSnapshot)
	if !ok {
		t.Fatal("attribution_v2_removed is not an attribution snapshot")
	}
	if len(removedSnapshot.Domains) >= len(firstSnapshot.Domains) {
		t.Fatal("shrink fixture did not remove a domain")
	}
	if removedSnapshot.Digest == firstSnapshot.Digest {
		t.Fatal("whole-bucket replacement retained the old digest")
	}
	for _, snapshot := range []Snapshot{firstSnapshot, removedSnapshot} {
		raw := encodeUncheckedRequest(t, snapshot)
		if _, err := ParseRequest(raw); err != nil {
			t.Fatalf("parse replacement snapshot: %v", err)
		}
	}
}

func boundarySnapshot(t *testing.T, fixture *fixtureFile, testCase fixtureNamedCase) AttributionSnapshot {
	t.Helper()
	recipe := fixture.NamedIdentityBoundary.GeneratedName
	domainNames := generatedIdentityNames(recipe.Prefix, recipe.Width, testCase.GeneratedDomains)
	domainNames = append(domainNames, testCase.ExtraDomains...)
	destinationNames := generatedIdentityNames(recipe.Prefix, recipe.Width, testCase.GeneratedDestinations)
	destinationNames = append(destinationNames, testCase.ExtraDestinations...)
	snapshot := AttributionSnapshot{
		SourceID:       fixture.Identities["source_id"],
		CredentialID:   fixture.Identities["credential_id"],
		AccountingDate: Date(fixture.Identities["accounting_date"]),
		Version:        1,
		Detail:         DetailExact,
		Domains:        zeroValueEntries(domainNames),
		Destinations:   zeroValueEntries(destinationNames),
	}
	digest, err := SnapshotDigest(snapshot)
	if err != nil {
		t.Fatalf("boundary digest: %v", err)
	}
	snapshot.Digest = digest
	return snapshot
}

func generatedIdentityNames(prefix string, width, count int) []string {
	names := make([]string, count)
	for index := 0; index < count; index++ {
		names[index] = fmt.Sprintf("%s%0*d", prefix, width, index)
	}
	return names
}

func zeroValueEntries(names []string) []Entry {
	entries := make([]Entry, len(names))
	for index, name := range names {
		entries[index] = Entry{Name: name}
	}
	sortEntries(entries)
	return entries
}

func namedIdentityCount(fixture *fixtureFile, snapshot AttributionSnapshot) int {
	domainAggregates := make(map[string]struct{}, len(fixture.Bounds.AggregateSlots.Domains))
	for _, name := range fixture.Bounds.AggregateSlots.Domains {
		domainAggregates[name] = struct{}{}
	}
	destinationAggregates := make(map[string]struct{}, len(fixture.Bounds.AggregateSlots.Destinations))
	for _, name := range fixture.Bounds.AggregateSlots.Destinations {
		destinationAggregates[name] = struct{}{}
	}
	named := 0
	for _, entry := range snapshot.Domains {
		if _, aggregate := domainAggregates[entry.Name]; !aggregate {
			named++
		}
	}
	for _, entry := range snapshot.Destinations {
		if _, aggregate := destinationAggregates[entry.Name]; !aggregate {
			named++
		}
	}
	return named
}

func attributionAtSize(t *testing.T, size int, accountingDate string) AttributionSnapshot {
	t.Helper()
	fixture := loadFixture(t)
	build := func(name string) AttributionSnapshot {
		snapshot, err := MakeAttributionSnapshot(
			fixture.Identities["source_id"],
			fixture.Identities["credential_id"],
			Date(accountingDate),
			1,
			DetailExact,
			[]Entry{{Name: name, Upload: 1, Download: 2, Connections: 3}},
			nil,
		)
		if err != nil {
			t.Fatalf("build sized snapshot: %v", err)
		}
		return snapshot
	}
	probe := build("a")
	probeEncoded, err := CanonicalSnapshot(probe)
	if err != nil {
		t.Fatalf("canonical probe: %v", err)
	}
	nameLength := 1 + size - len(probeEncoded)
	if nameLength < 1 {
		t.Fatalf("requested snapshot size %d is too small", size)
	}
	snapshot := build(strings.Repeat("a", nameLength))
	encoded, err := CanonicalSnapshot(snapshot)
	if err != nil {
		t.Fatalf("canonical sized snapshot: %v", err)
	}
	if len(encoded) != size {
		t.Fatalf("snapshot bytes = %d, want %d", len(encoded), size)
	}
	return snapshot
}

func TestCanonicalEnvelopeBoundFromRawBody(t *testing.T) {
	first := attributionAtSize(t, MaxSnapshotBytes, "2026-10-23")
	firstCanonical, err := CanonicalSnapshot(first)
	if err != nil {
		t.Fatalf("canonical first snapshot: %v", err)
	}

	const separators = 13_000
	base := attributionWithName(t, strings.Repeat("\u2028", separators), "2026-10-24")
	baseCanonical, err := CanonicalSnapshot(base)
	if err != nil {
		t.Fatalf("canonical base snapshot: %v", err)
	}
	padding := 300_000 - len(baseCanonical)
	if padding < 1 {
		t.Fatalf("unexpected base snapshot size %d", len(baseCanonical))
	}
	second := attributionWithName(t, strings.Repeat("\u2028", separators)+strings.Repeat("a", padding), "2026-10-24")
	secondCanonical, err := CanonicalSnapshot(second)
	if err != nil {
		t.Fatalf("canonical second snapshot: %v", err)
	}
	if len(secondCanonical) != 300_000 {
		t.Fatalf("second snapshot bytes = %d, want 300000", len(secondCanonical))
	}
	rawSecond := bytes.ReplaceAll(secondCanonical, []byte(`\u2028`), []byte("\u2028"))
	if count := bytes.Count(secondCanonical, []byte(`\u2028`)); count != separators {
		t.Fatalf("line-separator escapes = %d, want %d", count, separators)
	}

	raw := append([]byte(`{"schema":1,"snapshots":[`), firstCanonical...)
	raw = append(raw, ',')
	raw = append(raw, rawSecond...)
	raw = append(raw, ']', '}')
	canonicalEnvelopeSize := len(firstCanonical) + len(secondCanonical) + 28
	if len(raw) >= MaxRawBodyBytes {
		t.Fatalf("raw request bytes = %d, want below %d", len(raw), MaxRawBodyBytes)
	}
	if canonicalEnvelopeSize <= MaxRawBodyBytes {
		t.Fatalf("canonical envelope bytes = %d, want above %d", canonicalEnvelopeSize, MaxRawBodyBytes)
	}
	_, err = ParseRequest(raw)
	requireErrorCode(t, err, ErrRequestTooLarge)
}

func attributionWithName(t *testing.T, name, accountingDate string) AttributionSnapshot {
	t.Helper()
	fixture := loadFixture(t)
	snapshot, err := MakeAttributionSnapshot(
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		Date(accountingDate),
		1,
		DetailExact,
		[]Entry{{Name: name, Upload: 1, Download: 2, Connections: 3}},
		nil,
	)
	if err != nil {
		t.Fatalf("make attribution with name: %v", err)
	}
	return snapshot
}
