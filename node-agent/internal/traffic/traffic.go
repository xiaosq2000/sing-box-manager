// Package traffic implements the pure node-traffic-v1 wire codec.
//
// The package owns typed snapshots and receipts, their canonical payload and
// digest, request and receipt parsing, producers and exact receipt coverage.
// HTTP transport, persistence, ingestion and export scheduling are out of
// scope. Errors carry only the fixed wire codes from the shared contract.
package traffic

import "errors"

const (
	// SchemaVersion is the only accepted wire schema.
	SchemaVersion int64 = 1
	// MaxRawBodyBytes bounds one request or response body in bytes.
	MaxRawBodyBytes = 1024 * 1024
	// MaxSnapshotBytes bounds one canonical snapshot including its digest.
	MaxSnapshotBytes = 768 * 1024
	// MaxPageItems bounds snapshots or receipts in one page.
	MaxPageItems = 1024
	// MaxNamedIdentities bounds named attribution entries across both arrays.
	MaxNamedIdentities = 10_000
	// MaxInt64 is the largest accepted counter, version and entry value.
	MaxInt64 int64 = 1<<63 - 1
)

// ErrorCode is one fixed client-visible contract error code.
type ErrorCode string

const (
	ErrInvalidRequest         ErrorCode = "invalid_request"
	ErrUnsupportedSchema      ErrorCode = "unsupported_schema"
	ErrDuplicateSnapshotKey   ErrorCode = "duplicate_snapshot_key"
	ErrDuplicateName          ErrorCode = "duplicate_name"
	ErrSnapshotDigestMismatch ErrorCode = "snapshot_digest_mismatch"
	ErrRequestTooLarge        ErrorCode = "request_too_large"
	ErrSnapshotTooLarge       ErrorCode = "snapshot_too_large"
	ErrReceiptKeyMismatch     ErrorCode = "receipt_key_mismatch"
)

// Error carries one fixed contract code and never the raw payload.
type Error struct {
	code ErrorCode
}

// Error returns the fixed wire code.
func (e *Error) Error() string { return string(e.code) }

// Code returns the fixed wire code.
func (e *Error) Code() ErrorCode { return e.code }

// ErrorCodeOf extracts a contract code from err.
func ErrorCodeOf(err error) (ErrorCode, bool) {
	var contractError *Error
	if !errors.As(err, &contractError) {
		return "", false
	}
	return contractError.code, true
}

func contractError(code ErrorCode) error { return &Error{code: code} }

// Kind distinguishes the two complete snapshot kinds.
type Kind string

const (
	KindCounter     Kind = "counter"
	KindAttribution Kind = "attribution"
)

// Detail describes the completeness of an attribution bucket.
type Detail string

const (
	DetailExact     Detail = "exact"
	DetailCompacted Detail = "compacted"
	DetailDegraded  Detail = "degraded"
)

// Date is a canonical Gregorian accounting date in YYYY-MM-DD form.
type Date string

// String returns the canonical date text.
func (d Date) String() string { return string(d) }

// ParseDate validates a canonical date with a year from 1 through 9999.
func ParseDate(value string) (Date, error) {
	if !validDate(value) {
		return "", contractError(ErrInvalidRequest)
	}
	return Date(value), nil
}

// SnapshotKey is the immutable identity of one versioned snapshot bucket.
type SnapshotKey struct {
	SourceID       string
	CredentialID   string
	Kind           Kind
	AccountingDate Date
}

// Entry is one named attribution value.
type Entry struct {
	Name        string
	Upload      int64
	Download    int64
	Connections int64
}

// Snapshot is a complete counter or attribution snapshot.
type Snapshot interface {
	Kind() Kind
	Key() SnapshotKey
	isSnapshot()
}

// CounterSnapshot is the authoritative counter bucket for one key.
type CounterSnapshot struct {
	SourceID       string
	CredentialID   string
	AccountingDate Date
	Version        int64
	Upload         int64
	Download       int64
	Digest         string
}

// Kind returns KindCounter.
func (s CounterSnapshot) Kind() Kind { return KindCounter }

// Key returns the immutable bucket identity.
func (s CounterSnapshot) Key() SnapshotKey {
	return SnapshotKey{
		SourceID:       s.SourceID,
		CredentialID:   s.CredentialID,
		Kind:           KindCounter,
		AccountingDate: s.AccountingDate,
	}
}

func (CounterSnapshot) isSnapshot() {}

// AttributionSnapshot is a complete derived attribution bucket for one key.
type AttributionSnapshot struct {
	SourceID       string
	CredentialID   string
	AccountingDate Date
	Version        int64
	Detail         Detail
	Domains        []Entry
	Destinations   []Entry
	Digest         string
}

// Kind returns KindAttribution.
func (s AttributionSnapshot) Kind() Kind { return KindAttribution }

// Key returns the immutable bucket identity.
func (s AttributionSnapshot) Key() SnapshotKey {
	return SnapshotKey{
		SourceID:       s.SourceID,
		CredentialID:   s.CredentialID,
		Kind:           KindAttribution,
		AccountingDate: s.AccountingDate,
	}
}

func (AttributionSnapshot) isSnapshot() {}

// Request is one bounded page of snapshots.
type Request struct {
	Schema    int64
	Snapshots []Snapshot
}

// Receipt is stored key/version/digest state for one bucket.
type Receipt struct {
	SourceID       string
	CredentialID   string
	Kind           Kind
	AccountingDate Date
	Version        int64
	Digest         string
}

// Key returns the immutable bucket identity.
func (r Receipt) Key() SnapshotKey {
	return SnapshotKey{
		SourceID:       r.SourceID,
		CredentialID:   r.CredentialID,
		Kind:           r.Kind,
		AccountingDate: r.AccountingDate,
	}
}

// ReceiptResponse is one bounded page of stored receipts.
type ReceiptResponse struct {
	Schema   int64
	Receipts []Receipt
}
