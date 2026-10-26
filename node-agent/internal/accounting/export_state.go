package accounting

import (
	"context"
	"database/sql"
	"errors"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/traffic"
)

// PendingCounters returns at most limit counter buckets whose current local
// version differs from the acknowledged transport version. Selection uses the
// actual usage rows in deterministic (day, credential) order, so acknowledged
// rows do not consume page slots and dirty keys behind a long acknowledged
// prefix stay reachable.
func (s *Store) PendingCounters(ctx context.Context, limit int) ([]traffic.CounterSnapshot, error) {
	if limit < 1 || limit > traffic.MaxPageItems {
		return nil, errors.New("counter page limit out of range")
	}
	rows, err := s.db.QueryContext(ctx, `
SELECT usage.credential, usage.day, usage.version, usage.upload, usage.download
FROM usage
LEFT JOIN counter_export_ack
  ON counter_export_ack.credential = usage.credential AND counter_export_ack.day = usage.day
WHERE counter_export_ack.version IS NULL OR counter_export_ack.version <> usage.version
ORDER BY usage.day, usage.credential
LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	pending := []traffic.CounterSnapshot{}
	for rows.Next() {
		var credential, day string
		var version, upload, download int64
		if err := rows.Scan(&credential, &day, &version, &upload, &download); err != nil {
			return nil, err
		}
		snapshot, err := traffic.MakeCounterSnapshot(s.source, credential, traffic.Date(day), version, upload, download)
		if err != nil {
			return nil, err
		}
		pending = append(pending, snapshot)
	}
	return pending, rows.Err()
}

// AcknowledgeCounters records the current local version and wire digest for
// every receipt that exactly matches the reconstructed current usage row. It
// validates the complete request and response through the actual codec before
// opening the acknowledgement transaction. Older, ahead and mismatched
// receipts stay pending; missing rows and acknowledgement downgrades create no
// row. The returned count reflects only acknowledgements committed together.
func (s *Store) AcknowledgeCounters(ctx context.Context, request traffic.Request, response traffic.ReceiptResponse) (int, error) {
	if _, err := traffic.EncodeRequest(request); err != nil {
		return 0, err
	}
	for _, snapshot := range request.Snapshots {
		if snapshot.Kind() != traffic.KindCounter {
			return 0, errors.New("counter export page contains a non-counter snapshot")
		}
		if snapshot.Key().SourceID != s.source {
			return 0, errors.New("counter export source mismatch")
		}
	}
	if _, err := traffic.EncodeReceiptResponse(response, request); err != nil {
		return 0, err
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	acknowledged := 0
	for _, receipt := range response.Receipts {
		var version, upload, download int64
		err := tx.QueryRowContext(ctx,
			`SELECT version, upload, download FROM usage WHERE credential = ? AND day = ?`,
			receipt.CredentialID, string(receipt.AccountingDate),
		).Scan(&version, &upload, &download)
		if errors.Is(err, sql.ErrNoRows) {
			continue
		}
		if err != nil {
			return 0, err
		}
		current, err := traffic.MakeCounterSnapshot(s.source, receipt.CredentialID, receipt.AccountingDate, version, upload, download)
		if err != nil {
			return 0, err
		}
		if receipt.Version != current.Version || receipt.Digest != current.Digest {
			continue
		}

		var acknowledgedVersion int64
		var acknowledgedDigest string
		err = tx.QueryRowContext(ctx,
			`SELECT version, digest FROM counter_export_ack WHERE credential = ? AND day = ?`,
			receipt.CredentialID, string(receipt.AccountingDate),
		).Scan(&acknowledgedVersion, &acknowledgedDigest)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO counter_export_ack(credential, day, version, digest) VALUES(?, ?, ?, ?)`,
				receipt.CredentialID, string(receipt.AccountingDate), current.Version, current.Digest,
			); err != nil {
				return 0, err
			}
		case err != nil:
			return 0, err
		case acknowledgedVersion > current.Version:
			continue
		case acknowledgedVersion == current.Version && acknowledgedDigest == current.Digest:
			// The exact acknowledgement is already durable; only the result
			// count changes for this idempotent request.
		default:
			if _, err := tx.ExecContext(ctx,
				`UPDATE counter_export_ack SET version = ?, digest = ? WHERE credential = ? AND day = ?`,
				current.Version, current.Digest, receipt.CredentialID, string(receipt.AccountingDate),
			); err != nil {
				return 0, err
			}
		}
		acknowledged++
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return acknowledged, nil
}
