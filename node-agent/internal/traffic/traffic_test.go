package traffic

import (
	"errors"
	"testing"
)

func TestParseDate(t *testing.T) {
	for _, value := range []string{"0001-01-01", "2024-02-29", "9999-12-31"} {
		date, err := ParseDate(value)
		if err != nil {
			t.Fatalf("parse date %s: %v", value, err)
		}
		if date.String() != value {
			t.Fatalf("date string = %q, want %q", date, value)
		}
	}
	for _, value := range []string{"0000-01-01", "2023-02-29", "2026-13-01", "2026-10-23T00:00:00Z"} {
		_, err := ParseDate(value)
		requireErrorCode(t, err, ErrInvalidRequest)
	}
}

func TestErrorCodeOf(t *testing.T) {
	_, err := ParseRequest([]byte(`{"schema":2,"snapshots":[]}`))
	code, ok := ErrorCodeOf(err)
	if !ok || code != ErrUnsupportedSchema {
		t.Fatalf("ErrorCodeOf = %q/%v, want %q/true", code, ok, ErrUnsupportedSchema)
	}
	if _, ok := ErrorCodeOf(errors.New("other")); ok {
		t.Fatal("ErrorCodeOf accepted a non-contract error")
	}
}

func TestCanonicalHelpersRejectMissingOrMalformedSnapshots(t *testing.T) {
	var missing Snapshot
	if _, err := CanonicalPayload(missing); err == nil {
		t.Fatal("CanonicalPayload accepted a missing snapshot")
	} else {
		requireErrorCode(t, err, ErrInvalidRequest)
	}
	if _, err := SnapshotDigest(missing); err == nil {
		t.Fatal("SnapshotDigest accepted a missing snapshot")
	} else {
		requireErrorCode(t, err, ErrInvalidRequest)
	}
	counter, err := MakeCounterSnapshot(
		"6f1d0e0a-2f4b-4a5e-9c1d-0b7e2a9c3d41",
		"1c9b2f7e-8a3d-4e6f-b5c2-7d0e9a1b3c58",
		"2026-10-23",
		1,
		1,
		2,
	)
	if err != nil {
		t.Fatalf("make counter: %v", err)
	}
	counter.Digest = "F86A29EBF7D81FF621359780B4917566D49F2D606DD4940942D75F22EC02D28B"
	if _, err := CanonicalSnapshot(counter); err == nil {
		t.Fatal("CanonicalSnapshot accepted an uppercase digest")
	} else {
		requireErrorCode(t, err, ErrInvalidRequest)
	}
}

func TestPointerSnapshotsAreAccepted(t *testing.T) {
	counter, err := MakeCounterSnapshot(
		"6f1d0e0a-2f4b-4a5e-9c1d-0b7e2a9c3d41",
		"1c9b2f7e-8a3d-4e6f-b5c2-7d0e9a1b3c58",
		"2026-10-23",
		1,
		1,
		2,
	)
	if err != nil {
		t.Fatalf("make counter: %v", err)
	}
	valuePayload, err := CanonicalPayload(counter)
	if err != nil {
		t.Fatalf("canonical value payload: %v", err)
	}
	pointerPayload, err := CanonicalPayload(&counter)
	if err != nil {
		t.Fatalf("canonical pointer payload: %v", err)
	}
	if string(valuePayload) != string(pointerPayload) {
		t.Fatalf("pointer payload = %q, want %q", pointerPayload, valuePayload)
	}
	request, err := MakeRequest(&counter)
	if err != nil {
		t.Fatalf("make pointer request: %v", err)
	}
	encoded, err := EncodeRequest(request)
	if err != nil {
		t.Fatalf("encode pointer request: %v", err)
	}
	if _, err := ParseRequest(encoded); err != nil {
		t.Fatalf("parse pointer request: %v", err)
	}

	var missing *CounterSnapshot
	if _, err := CanonicalPayload(missing); err == nil {
		t.Fatal("CanonicalPayload accepted a typed nil pointer")
	} else {
		requireErrorCode(t, err, ErrInvalidRequest)
	}
	if _, err := MakeRequest(missing); err == nil {
		t.Fatal("MakeRequest accepted a typed nil pointer")
	} else {
		requireErrorCode(t, err, ErrInvalidRequest)
	}
}
