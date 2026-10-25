package traffic

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestRequestRejectionVectors(t *testing.T) {
	fixture := loadFixture(t)
	for _, vector := range fixture.RequestRejections {
		t.Run(vector.Name, func(t *testing.T) {
			_, err := ParseRequest(rejectionBytes(t, vector.Raw, vector.RawHex))
			requireErrorCode(t, err, ErrorCode(vector.Code))
		})
	}
}

func TestRequestErrorsDoNotEchoPayload(t *testing.T) {
	fixture := loadFixture(t)
	var rejection fixtureRejection
	for _, candidate := range fixture.RequestRejections {
		if candidate.Name == "digest_mismatch" {
			rejection = candidate
			break
		}
	}
	if rejection.Name == "" {
		t.Fatal("digest_mismatch rejection not found")
	}
	raw := rejectionBytes(t, rejection.Raw, rejection.RawHex)
	_, err := ParseRequest(raw)
	requireErrorCode(t, err, ErrSnapshotDigestMismatch)
	message := err.Error()
	if strings.Contains(message, fixture.Identities["source_id"]) ||
		strings.Contains(message, "f86a29eb") ||
		strings.Contains(message, string(raw)) {
		t.Fatalf("error message leaked the payload: %q", message)
	}
}

func TestProducerBuildersRoundTrip(t *testing.T) {
	fixture := loadFixture(t)
	counter, err := MakeCounterSnapshot(
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		Date(fixture.Identities["accounting_date"]),
		1,
		1024,
		2048,
	)
	if err != nil {
		t.Fatalf("make counter: %v", err)
	}
	if counter.Digest != vectorByName(t, fixture, "counter_v1").Snapshot.Digest {
		t.Fatalf("counter digest = %s", counter.Digest)
	}
	attribution, err := MakeAttributionSnapshot(
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		Date(fixture.Identities["accounting_date"]),
		1,
		DetailExact,
		[]Entry{
			{Name: "b.example.com", Upload: 5, Download: 6, Connections: 1},
			{Name: "a.example.com", Upload: 100, Download: 200, Connections: 2},
		},
		nil,
	)
	if err != nil {
		t.Fatalf("make attribution: %v", err)
	}
	if got := []string{attribution.Domains[0].Name, attribution.Domains[1].Name}; got[0] != "a.example.com" || got[1] != "b.example.com" {
		t.Fatalf("sorted domains = %v", got)
	}

	request, err := MakeRequest(counter, attribution)
	if err != nil {
		t.Fatalf("make request: %v", err)
	}
	encoded, err := EncodeRequest(request)
	if err != nil {
		t.Fatalf("encode request: %v", err)
	}
	parsed, err := ParseRequest(encoded)
	if err != nil {
		t.Fatalf("parse request: %v", err)
	}
	if len(parsed.Snapshots) != 2 {
		t.Fatalf("parsed %d snapshots, want 2", len(parsed.Snapshots))
	}
	response := ReceiptResponse{Schema: SchemaVersion}
	for _, snapshot := range parsed.Snapshots {
		receipt := Receipt{
			SourceID:       snapshot.Key().SourceID,
			CredentialID:   snapshot.Key().CredentialID,
			Kind:           snapshot.Kind(),
			AccountingDate: snapshot.Key().AccountingDate,
			Version:        snapshotVersion(t, snapshot),
			Digest:         snapshotDigest(t, snapshot),
		}
		response.Receipts = append(response.Receipts, receipt)
	}
	encodedResponse, err := EncodeReceiptResponse(response, parsed)
	if err != nil {
		t.Fatalf("encode receipt response: %v", err)
	}
	if _, err := ParseReceiptResponse(encodedResponse, parsed); err != nil {
		t.Fatalf("parse receipt response: %v", err)
	}
}

func TestProducerRejectsDuplicateKeysAndBadDigests(t *testing.T) {
	fixture := loadFixture(t)
	counter, err := MakeCounterSnapshot(
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		Date(fixture.Identities["accounting_date"]),
		1,
		1,
		2,
	)
	if err != nil {
		t.Fatalf("make counter: %v", err)
	}
	if _, err := MakeRequest(counter, counter); err == nil {
		t.Fatal("duplicate snapshot keys were accepted")
	} else {
		requireErrorCode(t, err, ErrDuplicateSnapshotKey)
	}
	counter.Digest = strings.Repeat("0", 64)
	_, err = MakeRequest(counter)
	requireErrorCode(t, err, ErrSnapshotDigestMismatch)

	duplicate := Entry{Name: "a.example.com", Upload: 1, Download: 2, Connections: 3}
	_, err = MakeAttributionSnapshot(
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		Date(fixture.Identities["accounting_date"]),
		1,
		DetailExact,
		[]Entry{duplicate, duplicate},
		nil,
	)
	requireErrorCode(t, err, ErrDuplicateName)
}

func TestProducerAcceptsRealDatesAndInt64Extremes(t *testing.T) {
	fixture := loadFixture(t)
	for _, value := range []string{"0001-01-01", "2024-02-29", "9999-12-31"} {
		snapshot, err := MakeCounterSnapshot(
			fixture.Identities["source_id"],
			fixture.Identities["credential_id"],
			Date(value),
			MaxInt64,
			MaxInt64,
			MaxInt64,
		)
		if err != nil {
			t.Fatalf("date %s: %v", value, err)
		}
		if snapshot.Version != MaxInt64 || snapshot.Upload != MaxInt64 || snapshot.Download != MaxInt64 {
			t.Fatalf("date %s lost int64 extremes: %+v", value, snapshot)
		}
	}
	for _, value := range []string{
		"0000-01-01",
		"2023-02-29",
		"2026-13-01",
		"2026-00-01",
		"2026-01-00",
		"2026-10-23T00:00:00Z",
		"2026-1-1",
	} {
		_, err := MakeCounterSnapshot(
			fixture.Identities["source_id"],
			fixture.Identities["credential_id"],
			Date(value),
			1,
			1,
			2,
		)
		requireErrorCode(t, err, ErrInvalidRequest)
	}
	for _, test := range []struct {
		name     string
		version  int64
		upload   int64
		download int64
	}{
		{name: "version zero", version: 0, upload: 1, download: 2},
		{name: "negative upload", version: 1, upload: -1, download: 2},
		{name: "negative download", version: 1, upload: 1, download: -2},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := MakeCounterSnapshot(
				fixture.Identities["source_id"],
				fixture.Identities["credential_id"],
				Date(fixture.Identities["accounting_date"]),
				test.version,
				test.upload,
				test.download,
			)
			requireErrorCode(t, err, ErrInvalidRequest)
		})
	}
}

func TestSchemaStrictness(t *testing.T) {
	fixture := loadFixture(t)
	base := fixture.RequestVectors[0].RequestCanonical
	for _, test := range []struct {
		name string
		from string
		to   string
		code ErrorCode
	}{
		{name: "negative", from: `"schema":1`, to: `"schema":-1`, code: ErrUnsupportedSchema},
		{name: "maximum", from: `"schema":1`, to: `"schema":9223372036854775807`, code: ErrUnsupportedSchema},
		{name: "overflow", from: `"schema":1`, to: `"schema":9223372036854775808`, code: ErrUnsupportedSchema},
		{name: "negative overflow", from: `"schema":1`, to: `"schema":-9223372036854775809`, code: ErrUnsupportedSchema},
		{name: "negative zero", from: `"schema":1`, to: `"schema":-0`, code: ErrUnsupportedSchema},
		{name: "float", from: `"schema":1`, to: `"schema":1.0`, code: ErrInvalidRequest},
		{name: "exponent", from: `"schema":1`, to: `"schema":1e0`, code: ErrInvalidRequest},
		{name: "string", from: `"schema":1`, to: `"schema":"1"`, code: ErrInvalidRequest},
		{name: "bool", from: `"schema":1`, to: `"schema":true`, code: ErrInvalidRequest},
		{name: "null", from: `"schema":1`, to: `"schema":null`, code: ErrInvalidRequest},
		{name: "version float", from: `"version":1`, to: `"version":1.0`, code: ErrInvalidRequest},
		{name: "version exponent", from: `"version":1`, to: `"version":1e3`, code: ErrInvalidRequest},
		{name: "version string", from: `"version":1`, to: `"version":"1"`, code: ErrInvalidRequest},
	} {
		t.Run(test.name, func(t *testing.T) {
			raw := strings.Replace(base, test.from, test.to, 1)
			_, err := ParseRequest([]byte(raw))
			requireErrorCode(t, err, test.code)
		})
	}
}

func TestSurrogateEscapes(t *testing.T) {
	fixture := loadFixture(t)
	build := func(nameJSON string) []byte {
		return []byte(fmt.Sprintf(
			`{"schema":1,"snapshots":[{"source_id":%q,"credential_id":%q,"kind":"attribution","accounting_date":%q,"version":1,"detail":"exact","domains":[{"name":%s,"upload":1,"download":2,"connections":3}],"destinations":[],"digest":%q}]}`,
			fixture.Identities["source_id"],
			fixture.Identities["credential_id"],
			fixture.Identities["accounting_date"],
			nameJSON,
			strings.Repeat("0", 64),
		))
	}
	for _, test := range []struct {
		name string
		raw  []byte
		code ErrorCode
	}{
		{name: "lone low", raw: build(`"\udc00"`), code: ErrInvalidRequest},
		{name: "lone high", raw: build(`"\ud800"`), code: ErrInvalidRequest},
		{name: "high then high", raw: build(`"\ud800\ud800"`), code: ErrInvalidRequest},
		{name: "high then text", raw: build(`"\ud800x"`), code: ErrInvalidRequest},
		{name: "valid pair", raw: build(`"\ud800\udc00"`), code: ErrSnapshotDigestMismatch},
		{name: "escaped backslash", raw: build(`"\\ud800"`), code: ErrSnapshotDigestMismatch},
		{name: "replacement escape", raw: build(`"\ufffd"`), code: ErrSnapshotDigestMismatch},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := ParseRequest(test.raw)
			requireErrorCode(t, err, test.code)
		})
	}
}

func TestInvalidUTF8NameIsRejected(t *testing.T) {
	fixture := loadFixture(t)
	invalidName := string([]byte{'b', 'a', 'd', 0xff, 'n', 'a', 'm', 'e'})
	raw := fmt.Sprintf(
		`{"schema":1,"snapshots":[{"source_id":%q,"credential_id":%q,"kind":"attribution","accounting_date":%q,"version":1,"detail":"exact","domains":[{"name":"%s","upload":1,"download":2,"connections":3}],"destinations":[],"digest":%q}]}`,
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		fixture.Identities["accounting_date"],
		invalidName,
		strings.Repeat("0", 64),
	)
	_, err := ParseRequest([]byte(raw))
	requireErrorCode(t, err, ErrInvalidRequest)
}

func TestDuplicateNamesAcrossArraysAreAllowed(t *testing.T) {
	fixture := loadFixture(t)
	entry := Entry{Name: "same.example", Upload: 1, Download: 2, Connections: 3}
	snapshot, err := MakeAttributionSnapshot(
		fixture.Identities["source_id"],
		fixture.Identities["credential_id"],
		Date(fixture.Identities["accounting_date"]),
		1,
		DetailExact,
		[]Entry{entry},
		[]Entry{entry},
	)
	if err != nil {
		t.Fatalf("same name across arrays was rejected: %v", err)
	}
	if _, err := MakeRequest(snapshot); err != nil {
		t.Fatalf("request with same name across arrays was rejected: %v", err)
	}
}

func TestPageItemBoundary(t *testing.T) {
	snapshots := counterPage(t, MaxPageItems)
	request, err := MakeRequest(snapshots...)
	if err != nil {
		t.Fatalf("make maximum page: %v", err)
	}
	encoded, err := EncodeRequest(request)
	if err != nil {
		t.Fatalf("encode maximum page: %v", err)
	}
	if _, err := ParseRequest(encoded); err != nil {
		t.Fatalf("parse maximum page: %v", err)
	}

	extra := counterPage(t, MaxPageItems+1)
	_, err = MakeRequest(extra...)
	requireErrorCode(t, err, ErrInvalidRequest)
	_, err = ParseRequest(encodeUncheckedRequest(t, extra...))
	requireErrorCode(t, err, ErrInvalidRequest)

	receipts := receiptsFor(t, snapshots)
	response := ReceiptResponse{Schema: SchemaVersion, Receipts: receipts}
	encodedResponse, err := EncodeReceiptResponse(response, request)
	if err != nil {
		t.Fatalf("encode maximum receipt page: %v", err)
	}
	if _, err := ParseReceiptResponse(encodedResponse, request); err != nil {
		t.Fatalf("parse maximum receipt page: %v", err)
	}

	extraReceipts := receiptsFor(t, extra)
	overResponse := ReceiptResponse{Schema: SchemaVersion, Receipts: extraReceipts}
	_, err = EncodeReceiptResponse(overResponse, request)
	requireErrorCode(t, err, ErrInvalidRequest)
	_, err = ParseReceiptResponse(encodeUncheckedReceiptResponse(t, overResponse), request)
	requireErrorCode(t, err, ErrInvalidRequest)
}

func counterPage(t *testing.T, count int) []Snapshot {
	t.Helper()
	fixture := loadFixture(t)
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	snapshots := make([]Snapshot, count)
	for index := 0; index < count; index++ {
		snapshot, err := MakeCounterSnapshot(
			fixture.Identities["source_id"],
			fixture.Identities["credential_id"],
			Date(start.AddDate(0, 0, index).Format("2006-01-02")),
			1,
			int64(index),
			int64(index),
		)
		if err != nil {
			t.Fatalf("make counter %d: %v", index, err)
		}
		snapshots[index] = snapshot
	}
	return snapshots
}

func receiptsFor(t *testing.T, snapshots []Snapshot) []Receipt {
	t.Helper()
	receipts := make([]Receipt, len(snapshots))
	for index, snapshot := range snapshots {
		receipts[index] = Receipt{
			SourceID:       snapshot.Key().SourceID,
			CredentialID:   snapshot.Key().CredentialID,
			Kind:           snapshot.Kind(),
			AccountingDate: snapshot.Key().AccountingDate,
			Version:        snapshotVersion(t, snapshot),
			Digest:         snapshotDigest(t, snapshot),
		}
	}
	return receipts
}

func snapshotVersion(t *testing.T, snapshot Snapshot) int64 {
	t.Helper()
	switch value := snapshot.(type) {
	case CounterSnapshot:
		return value.Version
	case AttributionSnapshot:
		return value.Version
	default:
		t.Fatalf("unknown snapshot type %T", snapshot)
		return 0
	}
}

func snapshotDigest(t *testing.T, snapshot Snapshot) string {
	t.Helper()
	digest, ok := snapshotDigestValue(snapshot)
	if !ok {
		t.Fatalf("unknown snapshot type %T", snapshot)
	}
	return digest
}
