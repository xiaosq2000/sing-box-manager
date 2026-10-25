package traffic

import (
	"strings"
	"testing"
)

func TestReceiptVectors(t *testing.T) {
	fixture := loadFixture(t)
	for _, vector := range fixture.ReceiptVectors {
		t.Run(vector.Name, func(t *testing.T) {
			request, err := ParseRequest([]byte(vector.RequestRaw))
			if err != nil {
				t.Fatalf("parse request: %v", err)
			}
			response, err := ParseReceiptResponse([]byte(vector.ResponseRaw), request)
			if err != nil {
				t.Fatalf("parse receipt response: %v", err)
			}
			if err := ValidateReceiptCoverage(request, response); err != nil {
				t.Fatalf("validate coverage: %v", err)
			}
			encoded, err := EncodeReceiptResponse(response, request)
			if err != nil {
				t.Fatalf("encode receipt response: %v", err)
			}
			if string(encoded) != vector.ResponseCanonical {
				t.Fatalf("receipt response bytes = %q, want %q", encoded, vector.ResponseCanonical)
			}
			parsed, err := ParseReceiptResponse([]byte(vector.ResponseCanonical), request)
			if err != nil {
				t.Fatalf("parse canonical receipt response: %v", err)
			}
			if err := ValidateReceiptCoverage(request, parsed); err != nil {
				t.Fatalf("validate canonical coverage: %v", err)
			}
		})
	}
}

func TestReceiptRejectionVectors(t *testing.T) {
	fixture := loadFixture(t)
	for _, vector := range fixture.ReceiptRejections {
		t.Run(vector.Name, func(t *testing.T) {
			request, err := ParseRequest([]byte(vector.RequestRaw))
			if err != nil {
				t.Fatalf("parse request: %v", err)
			}
			_, err = ParseReceiptResponse([]byte(vector.ResponseRaw), request)
			requireErrorCode(t, err, ErrorCode(vector.Code))
		})
	}
}

func TestStoredReceiptsNeedNotEchoSubmittedBucket(t *testing.T) {
	fixture := loadFixture(t)
	vector := fixture.ReceiptVectors[0]
	request, err := ParseRequest([]byte(vector.RequestRaw))
	if err != nil {
		t.Fatalf("parse request: %v", err)
	}
	response, err := ParseReceiptResponse([]byte(vector.ResponseRaw), request)
	if err != nil {
		t.Fatalf("parse response: %v", err)
	}
	var submission CounterSnapshot
	for _, snapshot := range request.Snapshots {
		if counter, ok := snapshot.(CounterSnapshot); ok {
			submission = counter
		}
	}
	var receipt Receipt
	for _, candidate := range response.Receipts {
		if candidate.Kind == KindCounter {
			receipt = candidate
		}
	}
	if submission.Version != 1 || receipt.Version != 2 {
		t.Fatalf("versions = %d/%d, want 1/2", submission.Version, receipt.Version)
	}
	if receipt.Digest == submission.Digest {
		t.Fatal("stored receipt unexpectedly echoed the submitted digest")
	}
	if submission.Digest != vectorByName(t, fixture, "counter_v1").Snapshot.Digest {
		t.Fatalf("submitted digest = %s", submission.Digest)
	}
}

func TestCoverageRejectsDirectDuplicateRequestKeys(t *testing.T) {
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
	request := Request{Schema: SchemaVersion, Snapshots: []Snapshot{counter, counter}}
	response := ReceiptResponse{
		Schema: SchemaVersion,
		Receipts: []Receipt{{
			SourceID:       counter.SourceID,
			CredentialID:   counter.CredentialID,
			Kind:           KindCounter,
			AccountingDate: counter.AccountingDate,
			Version:        counter.Version,
			Digest:         counter.Digest,
		}},
	}
	requireErrorCode(t, ValidateReceiptCoverage(request, response), ErrDuplicateSnapshotKey)
	_, err = EncodeReceiptResponse(response, request)
	requireErrorCode(t, err, ErrDuplicateSnapshotKey)
	raw := encodeUncheckedReceiptResponse(t, response)
	_, err = ParseReceiptResponse(raw, request)
	requireErrorCode(t, err, ErrDuplicateSnapshotKey)
}

func TestCoverageRejectsDirectDuplicateReceiptKeys(t *testing.T) {
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
	request, err := MakeRequest(counter)
	if err != nil {
		t.Fatalf("make request: %v", err)
	}
	receipt := Receipt{
		SourceID:       counter.SourceID,
		CredentialID:   counter.CredentialID,
		Kind:           KindCounter,
		AccountingDate: counter.AccountingDate,
		Version:        counter.Version,
		Digest:         counter.Digest,
	}
	response := ReceiptResponse{Schema: SchemaVersion, Receipts: []Receipt{receipt, receipt}}
	requireErrorCode(t, ValidateReceiptCoverage(request, response), ErrReceiptKeyMismatch)
	_, err = EncodeReceiptResponse(response, request)
	requireErrorCode(t, err, ErrReceiptKeyMismatch)
	_, err = ParseReceiptResponse(encodeUncheckedReceiptResponse(t, response), request)
	requireErrorCode(t, err, ErrReceiptKeyMismatch)
}

func TestReceiptSchemaStrictness(t *testing.T) {
	fixture := loadFixture(t)
	request, err := ParseRequest([]byte(fixture.ReceiptVectors[0].RequestRaw))
	if err != nil {
		t.Fatalf("parse request: %v", err)
	}
	base := fixture.ReceiptVectors[0].ResponseCanonical
	for _, test := range []struct {
		name string
		from string
		to   string
		code ErrorCode
	}{
		{name: "negative", from: `"schema":1`, to: `"schema":-1`, code: ErrUnsupportedSchema},
		{name: "maximum", from: `"schema":1`, to: `"schema":9223372036854775807`, code: ErrUnsupportedSchema},
		{name: "overflow", from: `"schema":1`, to: `"schema":9223372036854775808`, code: ErrUnsupportedSchema},
		{name: "float", from: `"schema":1`, to: `"schema":1.0`, code: ErrInvalidRequest},
	} {
		t.Run(test.name, func(t *testing.T) {
			raw := strings.Replace(base, test.from, test.to, 1)
			_, err := ParseReceiptResponse([]byte(raw), request)
			requireErrorCode(t, err, test.code)
		})
	}
}
