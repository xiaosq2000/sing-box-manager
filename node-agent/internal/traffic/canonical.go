package traffic

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"slices"
	"strings"
)

// CanonicalPayload returns the compact sorted UTF-8 payload that the digest
// covers: the snapshot without its digest field.
func CanonicalPayload(snapshot Snapshot) ([]byte, error) {
	document, err := snapshotPayloadDocument(snapshot)
	if err != nil {
		return nil, err
	}
	return canonicalJSONBytes(document)
}

// SnapshotDigest returns the lowercase SHA-256 digest of the canonical payload.
func SnapshotDigest(snapshot Snapshot) (string, error) {
	payload, err := CanonicalPayload(snapshot)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:]), nil
}

// CanonicalSnapshot returns the compact sorted UTF-8 snapshot including its
// digest field.
func CanonicalSnapshot(snapshot Snapshot) ([]byte, error) {
	document, err := snapshotPayloadDocument(snapshot)
	if err != nil {
		return nil, err
	}
	digest, ok := snapshotDigestValue(snapshot)
	if !ok || !validDigest(digest) {
		return nil, contractError(ErrInvalidRequest)
	}
	document["digest"] = digest
	return canonicalJSONBytes(document)
}

func snapshotPayloadDocument(snapshot Snapshot) (map[string]any, error) {
	normalized, err := normalizeSnapshot(snapshot)
	if err != nil {
		return nil, err
	}
	if err := validateSnapshotPayload(normalized); err != nil {
		return nil, err
	}
	switch value := normalized.(type) {
	case CounterSnapshot:
		return map[string]any{
			"source_id":       value.SourceID,
			"credential_id":   value.CredentialID,
			"kind":            KindCounter,
			"accounting_date": value.AccountingDate,
			"version":         value.Version,
			"upload":          value.Upload,
			"download":        value.Download,
		}, nil
	case AttributionSnapshot:
		return map[string]any{
			"source_id":       value.SourceID,
			"credential_id":   value.CredentialID,
			"kind":            KindAttribution,
			"accounting_date": value.AccountingDate,
			"version":         value.Version,
			"detail":          value.Detail,
			"domains":         entryDocuments(value.Domains),
			"destinations":    entryDocuments(value.Destinations),
		}, nil
	default:
		return nil, contractError(ErrInvalidRequest)
	}
}

func entryDocuments(entries []Entry) []any {
	ordered := slices.Clone(entries)
	slices.SortFunc(ordered, func(left, right Entry) int {
		return strings.Compare(left.Name, right.Name)
	})
	documents := make([]any, len(ordered))
	for index, entry := range ordered {
		documents[index] = map[string]any{
			"name":        entry.Name,
			"upload":      entry.Upload,
			"download":    entry.Download,
			"connections": entry.Connections,
		}
	}
	return documents
}

func canonicalRequestBytes(request Request) ([]byte, error) {
	snapshots := make([]any, len(request.Snapshots))
	for index, snapshot := range request.Snapshots {
		document, err := snapshotPayloadDocument(snapshot)
		if err != nil {
			return nil, err
		}
		digest, ok := snapshotDigestValue(snapshot)
		if !ok || !validDigest(digest) {
			return nil, contractError(ErrInvalidRequest)
		}
		document["digest"] = digest
		snapshots[index] = document
	}
	return canonicalJSONBytes(map[string]any{
		"schema":    request.Schema,
		"snapshots": snapshots,
	})
}

func canonicalReceiptResponseBytes(response ReceiptResponse) ([]byte, error) {
	receipts := make([]any, len(response.Receipts))
	for index, receipt := range response.Receipts {
		receipts[index] = receiptDocument(receipt)
	}
	return canonicalJSONBytes(map[string]any{
		"schema":   response.Schema,
		"receipts": receipts,
	})
}

func receiptDocument(receipt Receipt) map[string]any {
	return map[string]any{
		"source_id":       receipt.SourceID,
		"credential_id":   receipt.CredentialID,
		"kind":            receipt.Kind,
		"accounting_date": receipt.AccountingDate,
		"version":         receipt.Version,
		"digest":          receipt.Digest,
	}
}

func canonicalJSONBytes(value any) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, contractError(ErrInvalidRequest)
	}
	encoded := buffer.Bytes()
	if len(encoded) == 0 || encoded[len(encoded)-1] != '\n' {
		return nil, contractError(ErrInvalidRequest)
	}
	return encoded[:len(encoded)-1], nil
}

// MakeCounterSnapshot validates a counter snapshot and sets its canonical
// digest.
func MakeCounterSnapshot(
	sourceID, credentialID string,
	accountingDate Date,
	version, upload, download int64,
) (CounterSnapshot, error) {
	snapshot := CounterSnapshot{
		SourceID:       sourceID,
		CredentialID:   credentialID,
		AccountingDate: accountingDate,
		Version:        version,
		Upload:         upload,
		Download:       download,
	}
	if err := validateSnapshotInput(snapshot); err != nil {
		return CounterSnapshot{}, err
	}
	digest, err := SnapshotDigest(snapshot)
	if err != nil {
		return CounterSnapshot{}, err
	}
	snapshot.Digest = digest
	return snapshot, nil
}

// MakeAttributionSnapshot validates an attribution snapshot, sorts both arrays
// by name and sets its canonical digest.
func MakeAttributionSnapshot(
	sourceID, credentialID string,
	accountingDate Date,
	version int64,
	detail Detail,
	domains, destinations []Entry,
) (AttributionSnapshot, error) {
	domains = slices.Clone(domains)
	if domains == nil {
		domains = []Entry{}
	}
	destinations = slices.Clone(destinations)
	if destinations == nil {
		destinations = []Entry{}
	}
	snapshot := AttributionSnapshot{
		SourceID:       sourceID,
		CredentialID:   credentialID,
		AccountingDate: accountingDate,
		Version:        version,
		Detail:         detail,
		Domains:        domains,
		Destinations:   destinations,
	}
	if err := validateSnapshotInput(snapshot); err != nil {
		return AttributionSnapshot{}, err
	}
	sortEntries(snapshot.Domains)
	sortEntries(snapshot.Destinations)
	digest, err := SnapshotDigest(snapshot)
	if err != nil {
		return AttributionSnapshot{}, err
	}
	snapshot.Digest = digest
	return snapshot, nil
}

func sortEntries(entries []Entry) {
	slices.SortFunc(entries, func(left, right Entry) int {
		return strings.Compare(left.Name, right.Name)
	})
}
