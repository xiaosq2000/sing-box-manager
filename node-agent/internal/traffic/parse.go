package traffic

import (
	"encoding/json"
	"strconv"
)

var (
	requestRootFields  = []string{"schema", "snapshots"}
	responseRootFields = []string{"schema", "receipts"}
	counterFields      = []string{"source_id", "credential_id", "kind", "accounting_date", "version", "upload", "download", "digest"}
	attributionFields  = []string{"source_id", "credential_id", "kind", "accounting_date", "version", "detail", "domains", "destinations", "digest"}
	entryFields        = []string{"name", "upload", "download", "connections"}
	receiptFields      = []string{"source_id", "credential_id", "kind", "accounting_date", "version", "digest"}
)

// ParseRequest parses and fully validates one bounded request body.
func ParseRequest(raw []byte) (Request, error) {
	document, err := parseDocument(raw)
	if err != nil {
		return Request{}, err
	}
	object, ok := document.(map[string]any)
	if !ok {
		return Request{}, contractError(ErrInvalidRequest)
	}
	schema, err := parseSchema(object)
	if err != nil {
		return Request{}, err
	}
	if err := requireExactFields(object, requestRootFields); err != nil {
		return Request{}, err
	}
	array, ok := object["snapshots"].([]any)
	if !ok {
		return Request{}, contractError(ErrInvalidRequest)
	}
	snapshots := make([]Snapshot, len(array))
	for index, value := range array {
		snapshot, err := parseSnapshot(value)
		if err != nil {
			return Request{}, err
		}
		snapshots[index] = snapshot
	}
	request := Request{Schema: schema, Snapshots: snapshots}
	if err := validateRequest(request); err != nil {
		return Request{}, err
	}
	return request, nil
}

// ParseReceiptResponse parses and validates one bounded receipt body against
// the request it answers.
func ParseReceiptResponse(raw []byte, request Request) (ReceiptResponse, error) {
	document, err := parseDocument(raw)
	if err != nil {
		return ReceiptResponse{}, err
	}
	object, ok := document.(map[string]any)
	if !ok {
		return ReceiptResponse{}, contractError(ErrInvalidRequest)
	}
	schema, err := parseSchema(object)
	if err != nil {
		return ReceiptResponse{}, err
	}
	if err := requireExactFields(object, responseRootFields); err != nil {
		return ReceiptResponse{}, err
	}
	array, ok := object["receipts"].([]any)
	if !ok {
		return ReceiptResponse{}, contractError(ErrInvalidRequest)
	}
	receipts := make([]Receipt, len(array))
	for index, value := range array {
		receipt, err := parseReceipt(value)
		if err != nil {
			return ReceiptResponse{}, err
		}
		receipts[index] = receipt
	}
	response := ReceiptResponse{Schema: schema, Receipts: receipts}
	if err := validateReceiptResponse(response); err != nil {
		return ReceiptResponse{}, err
	}
	if err := ValidateReceiptCoverage(request, response); err != nil {
		return ReceiptResponse{}, err
	}
	encoded, err := canonicalReceiptResponseBytes(response)
	if err != nil {
		return ReceiptResponse{}, err
	}
	if len(encoded) > MaxRawBodyBytes {
		return ReceiptResponse{}, contractError(ErrRequestTooLarge)
	}
	return response, nil
}

// MakeRequest validates one bounded request page built from produced
// snapshots.
func MakeRequest(snapshots ...Snapshot) (Request, error) {
	request := Request{Schema: SchemaVersion, Snapshots: append([]Snapshot(nil), snapshots...)}
	if err := validateRequest(request); err != nil {
		return Request{}, err
	}
	return request, nil
}

// EncodeRequest validates and returns the canonical request envelope bytes.
func EncodeRequest(request Request) ([]byte, error) {
	if err := validateRequest(request); err != nil {
		return nil, err
	}
	return canonicalRequestBytes(request)
}

// EncodeReceiptResponse validates exact coverage and returns the canonical
// receipt response bytes.
func EncodeReceiptResponse(response ReceiptResponse, request Request) ([]byte, error) {
	if err := validateReceiptResponse(response); err != nil {
		return nil, err
	}
	if err := ValidateReceiptCoverage(request, response); err != nil {
		return nil, err
	}
	encoded, err := canonicalReceiptResponseBytes(response)
	if err != nil {
		return nil, err
	}
	if len(encoded) > MaxRawBodyBytes {
		return nil, contractError(ErrRequestTooLarge)
	}
	return encoded, nil
}

// ValidateReceiptCoverage requires the receipt key set to equal the request
// key set before any acknowledgement. Duplicate request keys are rejected
// first; duplicate or missing receipt keys are a receipt mismatch.
func ValidateReceiptCoverage(request Request, response ReceiptResponse) error {
	expected := make(map[SnapshotKey]struct{}, len(request.Snapshots))
	for _, snapshot := range request.Snapshots {
		normalized, err := normalizeSnapshot(snapshot)
		if err != nil {
			return err
		}
		key := normalized.Key()
		if _, duplicate := expected[key]; duplicate {
			return contractError(ErrDuplicateSnapshotKey)
		}
		expected[key] = struct{}{}
	}
	seen := make(map[SnapshotKey]struct{}, len(response.Receipts))
	for _, receipt := range response.Receipts {
		key := receipt.Key()
		if _, duplicate := seen[key]; duplicate {
			return contractError(ErrReceiptKeyMismatch)
		}
		seen[key] = struct{}{}
	}
	if len(seen) != len(expected) {
		return contractError(ErrReceiptKeyMismatch)
	}
	for key := range expected {
		if _, present := seen[key]; !present {
			return contractError(ErrReceiptKeyMismatch)
		}
	}
	return nil
}

func validateRequest(request Request) error {
	if request.Schema != SchemaVersion {
		return contractError(ErrUnsupportedSchema)
	}
	for _, snapshot := range request.Snapshots {
		if err := validateSnapshot(snapshot); err != nil {
			return err
		}
	}
	if len(request.Snapshots) < 1 || len(request.Snapshots) > MaxPageItems {
		return contractError(ErrInvalidRequest)
	}
	keys := make(map[SnapshotKey]struct{}, len(request.Snapshots))
	for _, snapshot := range request.Snapshots {
		key := snapshot.Key()
		if _, duplicate := keys[key]; duplicate {
			return contractError(ErrDuplicateSnapshotKey)
		}
		keys[key] = struct{}{}
	}
	for _, snapshot := range request.Snapshots {
		canonical, err := CanonicalSnapshot(snapshot)
		if err != nil {
			return err
		}
		if len(canonical) > MaxSnapshotBytes {
			return contractError(ErrSnapshotTooLarge)
		}
		digest, err := SnapshotDigest(snapshot)
		if err != nil {
			return err
		}
		expected, _ := snapshotDigestValue(snapshot)
		if digest != expected {
			return contractError(ErrSnapshotDigestMismatch)
		}
	}
	encoded, err := canonicalRequestBytes(request)
	if err != nil {
		return err
	}
	if len(encoded) > MaxRawBodyBytes {
		return contractError(ErrRequestTooLarge)
	}
	return nil
}

func validateReceiptResponse(response ReceiptResponse) error {
	if response.Schema != SchemaVersion {
		return contractError(ErrUnsupportedSchema)
	}
	for _, receipt := range response.Receipts {
		if err := validateReceipt(receipt); err != nil {
			return err
		}
	}
	if len(response.Receipts) < 1 || len(response.Receipts) > MaxPageItems {
		return contractError(ErrInvalidRequest)
	}
	return nil
}

func parseSchema(object map[string]any) (int64, error) {
	value, present := object["schema"]
	if !present {
		return 0, contractError(ErrInvalidRequest)
	}
	number, ok := value.(json.Number)
	if !ok {
		return 0, contractError(ErrInvalidRequest)
	}
	text := string(number)
	if !isJSONIntegerText(text) {
		return 0, contractError(ErrInvalidRequest)
	}
	if text != "1" {
		return 0, contractError(ErrUnsupportedSchema)
	}
	return SchemaVersion, nil
}

func isJSONIntegerText(value string) bool {
	if value == "" {
		return false
	}
	start := 0
	if value[0] == '-' {
		start = 1
		if len(value) == 1 {
			return false
		}
	}
	for index := start; index < len(value); index++ {
		if value[index] < '0' || value[index] > '9' {
			return false
		}
	}
	return true
}

func parseSnapshot(value any) (Snapshot, error) {
	object, ok := value.(map[string]any)
	if !ok {
		return nil, contractError(ErrInvalidRequest)
	}
	kindValue, present := object["kind"]
	if !present {
		return nil, contractError(ErrInvalidRequest)
	}
	kindText, ok := kindValue.(string)
	if !ok {
		return nil, contractError(ErrInvalidRequest)
	}
	switch Kind(kindText) {
	case KindCounter:
		if err := requireExactFields(object, counterFields); err != nil {
			return nil, err
		}
		identity, err := parseIdentity(object)
		if err != nil {
			return nil, err
		}
		upload, err := readNonNegativeInt64(object, "upload")
		if err != nil {
			return nil, err
		}
		download, err := readNonNegativeInt64(object, "download")
		if err != nil {
			return nil, err
		}
		snapshot := CounterSnapshot{
			SourceID:       identity.sourceID,
			CredentialID:   identity.credentialID,
			AccountingDate: identity.accountingDate,
			Version:        identity.version,
			Upload:         upload,
			Download:       download,
			Digest:         identity.digest,
		}
		if err := validateSnapshot(snapshot); err != nil {
			return nil, err
		}
		return snapshot, nil
	case KindAttribution:
		if err := requireExactFields(object, attributionFields); err != nil {
			return nil, err
		}
		identity, err := parseIdentity(object)
		if err != nil {
			return nil, err
		}
		detailText, err := readString(object, "detail")
		if err != nil {
			return nil, err
		}
		domains, err := parseEntries(object, "domains", false)
		if err != nil {
			return nil, err
		}
		destinations, err := parseEntries(object, "destinations", true)
		if err != nil {
			return nil, err
		}
		snapshot := AttributionSnapshot{
			SourceID:       identity.sourceID,
			CredentialID:   identity.credentialID,
			AccountingDate: identity.accountingDate,
			Version:        identity.version,
			Detail:         Detail(detailText),
			Domains:        domains,
			Destinations:   destinations,
			Digest:         identity.digest,
		}
		if err := validateSnapshot(snapshot); err != nil {
			return nil, err
		}
		return snapshot, nil
	default:
		return nil, contractError(ErrInvalidRequest)
	}
}

type parsedIdentity struct {
	sourceID       string
	credentialID   string
	accountingDate Date
	version        int64
	digest         string
}

func parseIdentity(object map[string]any) (parsedIdentity, error) {
	sourceID, err := readString(object, "source_id")
	if err != nil {
		return parsedIdentity{}, err
	}
	credentialID, err := readString(object, "credential_id")
	if err != nil {
		return parsedIdentity{}, err
	}
	accountingText, err := readString(object, "accounting_date")
	if err != nil {
		return parsedIdentity{}, err
	}
	version, err := readInt64(object, "version")
	if err != nil {
		return parsedIdentity{}, err
	}
	digest, err := readString(object, "digest")
	if err != nil {
		return parsedIdentity{}, err
	}
	identity := parsedIdentity{
		sourceID:       sourceID,
		credentialID:   credentialID,
		accountingDate: Date(accountingText),
		version:        version,
		digest:         digest,
	}
	if !validUUID(sourceID) || !validUUID(credentialID) ||
		!validDate(accountingText) || version < 1 || !validDigest(digest) {
		return parsedIdentity{}, contractError(ErrInvalidRequest)
	}
	return identity, nil
}

func parseEntries(object map[string]any, field string, destination bool) ([]Entry, error) {
	value, present := object[field]
	if !present {
		return nil, contractError(ErrInvalidRequest)
	}
	array, ok := value.([]any)
	if !ok {
		return nil, contractError(ErrInvalidRequest)
	}
	entries := make([]Entry, len(array))
	for index, item := range array {
		entryObject, ok := item.(map[string]any)
		if !ok {
			return nil, contractError(ErrInvalidRequest)
		}
		if err := requireExactFields(entryObject, entryFields); err != nil {
			return nil, err
		}
		name, err := readString(entryObject, "name")
		if err != nil {
			return nil, err
		}
		upload, err := readNonNegativeInt64(entryObject, "upload")
		if err != nil {
			return nil, err
		}
		download, err := readNonNegativeInt64(entryObject, "download")
		if err != nil {
			return nil, err
		}
		connections, err := readNonNegativeInt64(entryObject, "connections")
		if err != nil {
			return nil, err
		}
		entries[index] = Entry{
			Name:        name,
			Upload:      upload,
			Download:    download,
			Connections: connections,
		}
	}
	if _, err := validateEntries(entries, destination); err != nil {
		return nil, err
	}
	sortEntries(entries)
	return entries, nil
}

func parseReceipt(value any) (Receipt, error) {
	object, ok := value.(map[string]any)
	if !ok {
		return Receipt{}, contractError(ErrInvalidRequest)
	}
	if err := requireExactFields(object, receiptFields); err != nil {
		return Receipt{}, err
	}
	kindText, err := readString(object, "kind")
	if err != nil {
		return Receipt{}, err
	}
	sourceID, err := readString(object, "source_id")
	if err != nil {
		return Receipt{}, err
	}
	credentialID, err := readString(object, "credential_id")
	if err != nil {
		return Receipt{}, err
	}
	accountingText, err := readString(object, "accounting_date")
	if err != nil {
		return Receipt{}, err
	}
	version, err := readInt64(object, "version")
	if err != nil {
		return Receipt{}, err
	}
	digest, err := readString(object, "digest")
	if err != nil {
		return Receipt{}, err
	}
	receipt := Receipt{
		SourceID:       sourceID,
		CredentialID:   credentialID,
		Kind:           Kind(kindText),
		AccountingDate: Date(accountingText),
		Version:        version,
		Digest:         digest,
	}
	if err := validateReceipt(receipt); err != nil {
		return Receipt{}, err
	}
	return receipt, nil
}

func requireExactFields(object map[string]any, fields []string) error {
	if len(object) != len(fields) {
		return contractError(ErrInvalidRequest)
	}
	for _, field := range fields {
		if _, present := object[field]; !present {
			return contractError(ErrInvalidRequest)
		}
	}
	return nil
}

func readString(object map[string]any, field string) (string, error) {
	value, present := object[field]
	if !present {
		return "", contractError(ErrInvalidRequest)
	}
	text, ok := value.(string)
	if !ok {
		return "", contractError(ErrInvalidRequest)
	}
	return text, nil
}

func readInt64(object map[string]any, field string) (int64, error) {
	value, present := object[field]
	if !present {
		return 0, contractError(ErrInvalidRequest)
	}
	number, ok := value.(json.Number)
	if !ok {
		return 0, contractError(ErrInvalidRequest)
	}
	parsed, err := strconv.ParseInt(string(number), 10, 64)
	if err != nil {
		return 0, contractError(ErrInvalidRequest)
	}
	return parsed, nil
}

func readNonNegativeInt64(object map[string]any, field string) (int64, error) {
	value, err := readInt64(object, field)
	if err != nil {
		return 0, err
	}
	if value < 0 {
		return 0, contractError(ErrInvalidRequest)
	}
	return value, nil
}
