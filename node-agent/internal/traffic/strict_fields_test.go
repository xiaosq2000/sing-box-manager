package traffic

import (
	"strings"
	"testing"
)

func TestJSONFieldNamesAreCaseSensitive(t *testing.T) {
	fixture := loadFixture(t)
	requestRaw := fixture.RequestVectors[1].RequestCanonical
	cases := []struct {
		name string
		from string
		to   string
	}{
		{name: "root schema", from: `"schema":`, to: `"Schema":`},
		{name: "root schema upper", from: `"schema":`, to: `"SCHEMA":`},
		{name: "root snapshots", from: `"snapshots":`, to: `"Snapshots":`},
		{name: "snapshot source", from: `"source_id":`, to: `"Source_ID":`},
		{name: "snapshot credential", from: `"credential_id":`, to: `"Credential_ID":`},
		{name: "snapshot kind", from: `"kind":`, to: `"Kind":`},
		{name: "snapshot date", from: `"accounting_date":`, to: `"Accounting_Date":`},
		{name: "snapshot version", from: `"version":`, to: `"Version":`},
		{name: "snapshot digest", from: `"digest":`, to: `"Digest":`},
		{name: "counter upload", from: `"upload":`, to: `"Upload":`},
		{name: "attribution detail", from: `"detail":`, to: `"Detail":`},
		{name: "entry name", from: `"name":`, to: `"Name":`},
		{name: "entry connections", from: `"connections":`, to: `"Connections":`},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			raw := strings.Replace(requestRaw, testCase.from, testCase.to, 1)
			if raw == requestRaw {
				t.Fatalf("field %s not found in request vector", testCase.from)
			}
			_, err := ParseRequest([]byte(raw))
			requireErrorCode(t, err, ErrInvalidRequest)
		})
	}
}

func TestReceiptFieldNamesAreCaseSensitive(t *testing.T) {
	fixture := loadFixture(t)
	vector := fixture.ReceiptVectors[0]
	request, err := ParseRequest([]byte(vector.RequestRaw))
	if err != nil {
		t.Fatalf("parse request: %v", err)
	}
	cases := []struct {
		name string
		from string
		to   string
	}{
		{name: "root receipts", from: `"receipts":`, to: `"Receipts":`},
		{name: "receipt source", from: `"source_id":`, to: `"Source_ID":`},
		{name: "receipt credential", from: `"credential_id":`, to: `"Credential_ID":`},
		{name: "receipt kind", from: `"kind":`, to: `"Kind":`},
		{name: "receipt date", from: `"accounting_date":`, to: `"Accounting_Date":`},
		{name: "receipt version", from: `"version":`, to: `"Version":`},
		{name: "receipt digest", from: `"digest":`, to: `"Digest":`},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			raw := strings.Replace(vector.ResponseCanonical, testCase.from, testCase.to, 1)
			if raw == vector.ResponseCanonical {
				t.Fatalf("field %s not found in receipt vector", testCase.from)
			}
			_, err := ParseReceiptResponse([]byte(raw), request)
			requireErrorCode(t, err, ErrInvalidRequest)
		})
	}
}

func TestEscapedExactFieldNamesAreAccepted(t *testing.T) {
	fixture := loadFixture(t)
	requestRaw := fixture.RequestVectors[1].RequestCanonical
	escaped := strings.NewReplacer(
		`"schema":`, `"\u0073chema":`,
		`"source_id":`, `"\u0073ource_id":`,
		`"name":`, `"\u006eame":`,
	).Replace(requestRaw)
	if escaped == requestRaw {
		t.Fatal("escaped request fields were not substituted")
	}
	request, err := ParseRequest([]byte(escaped))
	if err != nil {
		t.Fatalf("parse escaped request field names: %v", err)
	}
	encoded, err := EncodeRequest(request)
	if err != nil {
		t.Fatalf("encode escaped request field names: %v", err)
	}
	if string(encoded) != requestRaw {
		t.Fatalf("re-encoded request bytes = %q, want %q", encoded, requestRaw)
	}

	vector := fixture.ReceiptVectors[0]
	receiptRequest, err := ParseRequest([]byte(vector.RequestRaw))
	if err != nil {
		t.Fatalf("parse receipt request: %v", err)
	}
	escapedResponse := strings.NewReplacer(
		`"receipts":`, `"\u0072eceipts":`,
		`"version":`, `"\u0076ersion":`,
	).Replace(vector.ResponseCanonical)
	response, err := ParseReceiptResponse([]byte(escapedResponse), receiptRequest)
	if err != nil {
		t.Fatalf("parse escaped receipt field names: %v", err)
	}
	encodedResponse, err := EncodeReceiptResponse(response, receiptRequest)
	if err != nil {
		t.Fatalf("encode escaped receipt field names: %v", err)
	}
	if string(encodedResponse) != vector.ResponseCanonical {
		t.Fatalf("re-encoded receipt bytes = %q, want %q", encodedResponse, vector.ResponseCanonical)
	}
}
