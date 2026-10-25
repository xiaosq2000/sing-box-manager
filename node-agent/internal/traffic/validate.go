package traffic

import "unicode/utf8"

func validUUID(value string) bool {
	if len(value) != 36 {
		return false
	}
	for index := 0; index < len(value); index++ {
		switch index {
		case 8, 13, 18, 23:
			if value[index] != '-' {
				return false
			}
		default:
			if !lowerHexDigit(value[index]) {
				return false
			}
		}
	}
	return true
}

func validDigest(value string) bool {
	if len(value) != 64 {
		return false
	}
	for index := 0; index < len(value); index++ {
		if !lowerHexDigit(value[index]) {
			return false
		}
	}
	return true
}

func lowerHexDigit(value byte) bool {
	return value >= '0' && value <= '9' || value >= 'a' && value <= 'f'
}

func validDate(value string) bool {
	if len(value) != 10 || value[4] != '-' || value[7] != '-' {
		return false
	}
	year, ok := decimalInt(value[0:4])
	if !ok || year < 1 {
		return false
	}
	month, ok := decimalInt(value[5:7])
	if !ok || month < 1 || month > 12 {
		return false
	}
	day, ok := decimalInt(value[8:10])
	if !ok || day < 1 || day > daysInMonth(year, month) {
		return false
	}
	return true
}

func decimalInt(value string) (int, bool) {
	result := 0
	for index := 0; index < len(value); index++ {
		character := value[index]
		if character < '0' || character > '9' {
			return 0, false
		}
		result = result*10 + int(character-'0')
	}
	return result, true
}

func daysInMonth(year, month int) int {
	switch month {
	case 2:
		if year%4 == 0 && (year%100 != 0 || year%400 == 0) {
			return 29
		}
		return 28
	case 4, 6, 9, 11:
		return 30
	default:
		return 31
	}
}

func isDomainAggregate(name string) bool {
	switch name {
	case "__other__", "__unattributed__", "__ip__":
		return true
	default:
		return false
	}
}

func isDestinationAggregate(name string) bool {
	switch name {
	case "__other__", "__unattributed__":
		return true
	default:
		return false
	}
}

func normalizeSnapshot(snapshot Snapshot) (Snapshot, error) {
	switch value := snapshot.(type) {
	case CounterSnapshot, AttributionSnapshot:
		return value, nil
	case *CounterSnapshot:
		if value == nil {
			return nil, contractError(ErrInvalidRequest)
		}
		return *value, nil
	case *AttributionSnapshot:
		if value == nil {
			return nil, contractError(ErrInvalidRequest)
		}
		return *value, nil
	default:
		return nil, contractError(ErrInvalidRequest)
	}
}

// validateEntries checks one attribution array and returns its named-identity
// count. Duplicate names are rejected per array; the two arrays are separate
// sets.
func validateEntries(entries []Entry, destination bool) (int, error) {
	seen := make(map[string]struct{}, len(entries))
	named := 0
	for _, entry := range entries {
		if entry.Name == "" || !utf8.ValidString(entry.Name) {
			return 0, contractError(ErrInvalidRequest)
		}
		if entry.Upload < 0 || entry.Download < 0 || entry.Connections < 0 {
			return 0, contractError(ErrInvalidRequest)
		}
		if _, duplicate := seen[entry.Name]; duplicate {
			return 0, contractError(ErrDuplicateName)
		}
		seen[entry.Name] = struct{}{}
		aggregate := isDomainAggregate(entry.Name)
		if destination {
			aggregate = isDestinationAggregate(entry.Name)
		}
		if !aggregate {
			named++
		}
	}
	return named, nil
}

func validateIdentity(sourceID, credentialID string, accountingDate Date, version int64) error {
	if !validUUID(sourceID) || !validUUID(credentialID) ||
		!validDate(string(accountingDate)) || version < 1 {
		return contractError(ErrInvalidRequest)
	}
	return nil
}

// validateSnapshotPayload checks the fields covered by the digest payload.
// It deliberately does not enforce the named-identity ceiling; that ceiling is
// a page admission rule and is checked by validateSnapshotInput.
func validateSnapshotPayload(snapshot Snapshot) error {
	normalized, err := normalizeSnapshot(snapshot)
	if err != nil {
		return err
	}
	switch value := normalized.(type) {
	case CounterSnapshot:
		if err := validateIdentity(value.SourceID, value.CredentialID, value.AccountingDate, value.Version); err != nil {
			return err
		}
		if value.Upload < 0 || value.Download < 0 {
			return contractError(ErrInvalidRequest)
		}
		return nil
	case AttributionSnapshot:
		if err := validateIdentity(value.SourceID, value.CredentialID, value.AccountingDate, value.Version); err != nil {
			return err
		}
		switch value.Detail {
		case DetailExact, DetailCompacted, DetailDegraded:
		default:
			return contractError(ErrInvalidRequest)
		}
		if _, err := validateEntries(value.Domains, false); err != nil {
			return err
		}
		if _, err := validateEntries(value.Destinations, true); err != nil {
			return err
		}
		return nil
	default:
		return contractError(ErrInvalidRequest)
	}
}

func validateNamedIdentityCount(snapshot Snapshot) error {
	normalized, err := normalizeSnapshot(snapshot)
	if err != nil {
		return err
	}
	value, ok := normalized.(AttributionSnapshot)
	if !ok {
		return nil
	}
	domains, err := validateEntries(value.Domains, false)
	if err != nil {
		return err
	}
	destinations, err := validateEntries(value.Destinations, true)
	if err != nil {
		return err
	}
	if domains+destinations > MaxNamedIdentities {
		return contractError(ErrInvalidRequest)
	}
	return nil
}

// validateSnapshotInput checks a producer snapshot before its digest is set.
func validateSnapshotInput(snapshot Snapshot) error {
	if err := validateSnapshotPayload(snapshot); err != nil {
		return err
	}
	return validateNamedIdentityCount(snapshot)
}

func snapshotDigestValue(snapshot Snapshot) (string, bool) {
	normalized, err := normalizeSnapshot(snapshot)
	if err != nil {
		return "", false
	}
	switch value := normalized.(type) {
	case CounterSnapshot:
		return value.Digest, true
	case AttributionSnapshot:
		return value.Digest, true
	default:
		return "", false
	}
}

// validateSnapshot checks every field a parsed or produced snapshot needs.
func validateSnapshot(snapshot Snapshot) error {
	if err := validateSnapshotInput(snapshot); err != nil {
		return err
	}
	digest, ok := snapshotDigestValue(snapshot)
	if !ok || !validDigest(digest) {
		return contractError(ErrInvalidRequest)
	}
	return nil
}

func validateReceipt(receipt Receipt) error {
	if err := validateIdentity(receipt.SourceID, receipt.CredentialID, receipt.AccountingDate, receipt.Version); err != nil {
		return err
	}
	if receipt.Kind != KindCounter && receipt.Kind != KindAttribution {
		return contractError(ErrInvalidRequest)
	}
	if !validDigest(receipt.Digest) {
		return contractError(ErrInvalidRequest)
	}
	return nil
}
