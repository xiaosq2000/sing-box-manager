package traffic

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

const (
	fixtureRelativePath = "../../../tests/fixtures/node-traffic-v1.json"
	fixtureFrozenSHA256 = "12f098d86018b5a82303eac188b418efde35d8c2935960277665a079ded52efb"
)

type fixtureFile struct {
	Schema                int                    `json:"schema"`
	Contract              string                 `json:"contract"`
	Identities            map[string]string      `json:"identities"`
	Bounds                fixtureBounds          `json:"bounds"`
	RequestVectors        []fixtureRequestVector `json:"request_vectors"`
	NamedIdentityBoundary fixtureNamedBoundaries `json:"named_identity_boundaries"`
	CanonicalVectors      []fixtureCanonical     `json:"canonical_vectors"`
	EquivalenceVectors    []fixtureEquivalence   `json:"equivalence_vectors"`
	RequestRejections     []fixtureRejection     `json:"request_rejections"`
	ReceiptVectors        []fixtureReceiptVector `json:"receipt_vectors"`
	ReceiptRejections     []fixtureReceiptReject `json:"receipt_rejections"`
}

type fixtureBounds struct {
	RawBodyBytes            int `json:"raw_body_bytes"`
	SnapshotBytes           int `json:"snapshot_bytes"`
	PageItems               int `json:"page_items"`
	CounterV1PayloadBytes   int `json:"counter_v1_payload_bytes"`
	CounterV1SnapshotBytes  int `json:"counter_v1_snapshot_bytes"`
	CounterMaxSnapshotBytes int `json:"counter_max_snapshot_bytes"`
	NamedIdentities         int `json:"named_identities"`
	AggregateSlots          struct {
		Domains      []string `json:"domains"`
		Destinations []string `json:"destinations"`
	} `json:"aggregate_slots"`
	ResponseOverflowCode string `json:"response_overflow_code"`
}

type fixtureCanonical struct {
	Name              string          `json:"name"`
	Snapshot          fixtureSnapshot `json:"snapshot"`
	PayloadCanonical  string          `json:"payload_canonical"`
	SnapshotCanonical string          `json:"snapshot_canonical"`
}

type fixtureSnapshot struct {
	SourceID       string         `json:"source_id"`
	CredentialID   string         `json:"credential_id"`
	Kind           string         `json:"kind"`
	AccountingDate string         `json:"accounting_date"`
	Version        int64          `json:"version"`
	Upload         *int64         `json:"upload"`
	Download       *int64         `json:"download"`
	Detail         string         `json:"detail"`
	Domains        []fixtureEntry `json:"domains"`
	Destinations   []fixtureEntry `json:"destinations"`
	Digest         string         `json:"digest"`
}

type fixtureEntry struct {
	Name        string `json:"name"`
	Upload      int64  `json:"upload"`
	Download    int64  `json:"download"`
	Connections int64  `json:"connections"`
}

type fixtureEquivalence struct {
	Name       string `json:"name"`
	Vector     string `json:"vector"`
	RequestRaw string `json:"request_raw"`
}

type fixtureRequestVector struct {
	Name             string   `json:"name"`
	Snapshots        []string `json:"snapshots"`
	RequestCanonical string   `json:"request_canonical"`
}

type fixtureRejection struct {
	Name   string `json:"name"`
	Raw    string `json:"raw"`
	RawHex string `json:"raw_hex"`
	Code   string `json:"code"`
}

type fixtureReceiptVector struct {
	Name              string `json:"name"`
	RequestRaw        string `json:"request_raw"`
	ResponseRaw       string `json:"response_raw"`
	ResponseCanonical string `json:"response_canonical"`
}

type fixtureReceiptReject struct {
	Name        string `json:"name"`
	RequestRaw  string `json:"request_raw"`
	ResponseRaw string `json:"response_raw"`
	Code        string `json:"code"`
}

type fixtureNamedBoundaries struct {
	GeneratedName struct {
		Prefix string `json:"prefix"`
		Width  int    `json:"width"`
	} `json:"generated_name"`
	Cases []fixtureNamedCase `json:"cases"`
}

type fixtureNamedCase struct {
	Name                    string   `json:"name"`
	GeneratedDomains        int      `json:"generated_domains"`
	GeneratedDestinations   int      `json:"generated_destinations"`
	ExtraDomains            []string `json:"extra_domains"`
	ExtraDestinations       []string `json:"extra_destinations"`
	ExpectedNamedIdentities int      `json:"expected_named_identities"`
	ExpectedSnapshotBytes   int      `json:"expected_snapshot_bytes"`
	ExpectedRequestBytes    int      `json:"expected_request_bytes"`
	ExpectedResult          string   `json:"expected_result"`
}

func loadFixture(t *testing.T) *fixtureFile {
	t.Helper()
	raw, err := os.ReadFile(filepath.FromSlash(fixtureRelativePath))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var fixture fixtureFile
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatalf("decode fixture: %v", err)
	}
	return &fixture
}

func TestFixtureSHA256(t *testing.T) {
	raw, err := os.ReadFile(filepath.FromSlash(fixtureRelativePath))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	sum := sha256.Sum256(raw)
	if got := hex.EncodeToString(sum[:]); got != fixtureFrozenSHA256 {
		t.Fatalf("fixture SHA-256 = %s, want %s", got, fixtureFrozenSHA256)
	}
}

func TestFixtureSections(t *testing.T) {
	fixture := loadFixture(t)
	if fixture.Schema != 1 || fixture.Contract != "node-traffic-v1" {
		t.Fatalf("fixture header = %d/%q", fixture.Schema, fixture.Contract)
	}
	sections := map[string]int{
		"canonical_vectors":      len(fixture.CanonicalVectors),
		"equivalence_vectors":    len(fixture.EquivalenceVectors),
		"request_vectors":        len(fixture.RequestVectors),
		"request_rejections":     len(fixture.RequestRejections),
		"receipt_vectors":        len(fixture.ReceiptVectors),
		"receipt_rejections":     len(fixture.ReceiptRejections),
		"named_identity_recipes": len(fixture.NamedIdentityBoundary.Cases),
	}
	want := map[string]int{
		"canonical_vectors":      12,
		"equivalence_vectors":    7,
		"request_vectors":        2,
		"request_rejections":     52,
		"receipt_vectors":        2,
		"receipt_rejections":     15,
		"named_identity_recipes": 6,
	}
	for name, count := range want {
		if sections[name] != count {
			t.Errorf("%s = %d, want %d", name, sections[name], count)
		}
	}
	if fixture.NamedIdentityBoundary.GeneratedName.Prefix != "n" ||
		fixture.NamedIdentityBoundary.GeneratedName.Width != 5 {
		t.Fatalf("unexpected generated-name recipe: %+v", fixture.NamedIdentityBoundary.GeneratedName)
	}
}

func (snapshot fixtureSnapshot) typed(t *testing.T) Snapshot {
	t.Helper()
	switch Kind(snapshot.Kind) {
	case KindCounter:
		if snapshot.Upload == nil || snapshot.Download == nil {
			t.Fatalf("counter snapshot %q lacks counters", snapshot.SourceID)
		}
		return CounterSnapshot{
			SourceID:       snapshot.SourceID,
			CredentialID:   snapshot.CredentialID,
			AccountingDate: Date(snapshot.AccountingDate),
			Version:        snapshot.Version,
			Upload:         *snapshot.Upload,
			Download:       *snapshot.Download,
			Digest:         snapshot.Digest,
		}
	case KindAttribution:
		return AttributionSnapshot{
			SourceID:       snapshot.SourceID,
			CredentialID:   snapshot.CredentialID,
			AccountingDate: Date(snapshot.AccountingDate),
			Version:        snapshot.Version,
			Detail:         Detail(snapshot.Detail),
			Domains:        typedEntries(snapshot.Domains),
			Destinations:   typedEntries(snapshot.Destinations),
			Digest:         snapshot.Digest,
		}
	default:
		t.Fatalf("unknown fixture snapshot kind %q", snapshot.Kind)
		return nil
	}
}

func typedEntries(entries []fixtureEntry) []Entry {
	result := make([]Entry, len(entries))
	for index, entry := range entries {
		result[index] = Entry{
			Name:        entry.Name,
			Upload:      entry.Upload,
			Download:    entry.Download,
			Connections: entry.Connections,
		}
	}
	return result
}

func vectorByName(t *testing.T, fixture *fixtureFile, name string) fixtureCanonical {
	t.Helper()
	for _, vector := range fixture.CanonicalVectors {
		if vector.Name == name {
			return vector
		}
	}
	t.Fatalf("canonical vector %q not found", name)
	return fixtureCanonical{}
}

func rejectionBytes(t *testing.T, raw, rawHex string) []byte {
	t.Helper()
	if rawHex == "" {
		return []byte(raw)
	}
	decoded, err := hex.DecodeString(rawHex)
	if err != nil {
		t.Fatalf("decode raw_hex: %v", err)
	}
	return decoded
}

func requireErrorCode(t *testing.T, err error, want ErrorCode) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected %s, got nil", want)
	}
	var contractError *Error
	if !errors.As(err, &contractError) {
		t.Fatalf("expected *Error, got %T: %v", err, err)
	}
	if contractError.Code() != want {
		t.Fatalf("error code = %s, want %s", contractError.Code(), want)
	}
	if err.Error() != string(want) {
		t.Fatalf("error text = %q, want %q", err.Error(), want)
	}
}

// encodeUncheckedRequest builds the canonical envelope without applying page
// admission bounds. It is used only to feed over-limit bodies to the parser.
func encodeUncheckedRequest(t *testing.T, snapshots ...Snapshot) []byte {
	t.Helper()
	raw := []byte(`{"schema":1,"snapshots":[`)
	for index, snapshot := range snapshots {
		if index > 0 {
			raw = append(raw, ',')
		}
		encoded, err := CanonicalSnapshot(snapshot)
		if err != nil {
			t.Fatalf("canonical snapshot: %v", err)
		}
		raw = append(raw, encoded...)
	}
	return append(raw, ']', '}')
}

// encodeUncheckedReceiptResponse is the receipt counterpart for page-count
// rejection checks.
func encodeUncheckedReceiptResponse(t *testing.T, response ReceiptResponse) []byte {
	t.Helper()
	raw := []byte(`{"schema":1,"receipts":[`)
	for index, receipt := range response.Receipts {
		if index > 0 {
			raw = append(raw, ',')
		}
		encoded, err := canonicalJSONBytes(receiptDocument(receipt))
		if err != nil {
			t.Fatalf("canonical receipt: %v", err)
		}
		raw = append(raw, encoded...)
	}
	return append(raw, ']', '}')
}
