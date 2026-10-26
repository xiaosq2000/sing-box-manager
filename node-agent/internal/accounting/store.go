// Package accounting implements a transactional G0 counter checkpoint and bucket.
package accounting

import (
	"context"
	"database/sql"
	"errors"
	"math"
	"os"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/contract"
	_ "modernc.org/sqlite"
)

type Store struct {
	db     *sql.DB
	source string
}

func Open(path, source string) (*Store, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	err = file.Chmod(0600)
	closeErr := file.Close()
	if err != nil {
		return nil, err
	}
	if closeErr != nil {
		return nil, closeErr
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	for _, query := range []string{
		"PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "PRAGMA busy_timeout=5000",
		`CREATE TABLE IF NOT EXISTS identity (source TEXT PRIMARY KEY)`,
		`CREATE TABLE IF NOT EXISTS checkpoints (invocation TEXT, credential TEXT, upload INTEGER NOT NULL, download INTEGER NOT NULL, PRIMARY KEY(invocation,credential))`,
		`CREATE TABLE IF NOT EXISTS usage (credential TEXT, day TEXT, version INTEGER NOT NULL, upload INTEGER NOT NULL, download INTEGER NOT NULL, PRIMARY KEY(credential,day))`,
		`CREATE TABLE IF NOT EXISTS counter_export_ack (credential TEXT NOT NULL, day TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0), digest TEXT NOT NULL CHECK(length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*'), PRIMARY KEY(credential,day))`,
		`CREATE TABLE IF NOT EXISTS finished (invocation TEXT PRIMARY KEY)`,
		`CREATE TABLE IF NOT EXISTS gaps (invocation TEXT PRIMARY KEY, reason TEXT NOT NULL)`,
	} {
		if _, err := db.Exec(query); err != nil {
			db.Close()
			return nil, err
		}
	}
	if _, err := db.Exec("INSERT INTO identity(source) SELECT ? WHERE NOT EXISTS(SELECT 1 FROM identity)", source); err != nil {
		db.Close()
		return nil, err
	}
	var stored string
	if err := db.QueryRow("SELECT source FROM identity").Scan(&stored); err != nil || stored != source {
		db.Close()
		return nil, errors.New("accounting source mismatch")
	}
	return &Store{db: db, source: source}, nil
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) Record(ctx context.Context, invocation, credential string, upload, download int64, now time.Time) error {
	if invocation == "" || credential == "" || upload < 0 || download < 0 {
		return errors.New("invalid counter identity or values")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var lastUp, lastDown int64
	err = tx.QueryRowContext(ctx, "SELECT upload,download FROM checkpoints WHERE invocation=? AND credential=?", invocation, credential).Scan(&lastUp, &lastDown)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	if upload < lastUp || download < lastDown {
		return errors.New("counter regression within one invocation")
	}
	// An invocation without a confirmed final stop can have an unobserved tail.
	if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO gaps SELECT DISTINCT invocation,'unobserved_termination' FROM checkpoints WHERE credential=? AND invocation<>? AND invocation NOT IN (SELECT invocation FROM finished)`, credential, invocation); err != nil {
		return err
	}
	du, dd := upload-lastUp, download-lastDown
	if _, err := tx.ExecContext(ctx, `INSERT INTO checkpoints VALUES(?,?,?,?) ON CONFLICT(invocation,credential) DO UPDATE SET upload=excluded.upload,download=excluded.download`, invocation, credential, upload, download); err != nil {
		return err
	}
	if du != 0 || dd != 0 {
		day := now.UTC().Format("2006-01-02")
		var oldUp, oldDown, version int64
		err = tx.QueryRowContext(ctx, "SELECT upload,download,version FROM usage WHERE credential=? AND day=?", credential, day).Scan(&oldUp, &oldDown, &version)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if oldUp > math.MaxInt64-du || oldDown > math.MaxInt64-dd || version == math.MaxInt64 {
			return errors.New("accounting overflow")
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO usage VALUES(?,?,?,?,?) ON CONFLICT(credential,day) DO UPDATE SET version=excluded.version,upload=excluded.upload,download=excluded.download`, credential, day, version+1, oldUp+du, oldDown+dd); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// Finish records termination only after the final sample commits and systemd confirms an empty service.
func (s *Store) Finish(ctx context.Context, invocation string) error {
	result, err := s.db.ExecContext(ctx, `INSERT INTO finished SELECT DISTINCT invocation FROM checkpoints WHERE invocation=? ON CONFLICT(invocation) DO UPDATE SET invocation=excluded.invocation`, invocation)
	if err != nil {
		return err
	}
	if count, err := result.RowsAffected(); err != nil || count != 1 {
		return errors.New("final checkpoint unavailable")
	}
	return nil
}

// Gap retains uncertainty, not an invented byte count. Repeated observations are idempotent.
func (s *Store) Gap(ctx context.Context, invocation string) error {
	if invocation == "" {
		return errors.New("missing gap identity")
	}
	_, err := s.db.ExecContext(ctx, `INSERT OR IGNORE INTO gaps SELECT ?,'unobserved_termination' WHERE NOT EXISTS(SELECT 1 FROM finished WHERE invocation=?)`, invocation, invocation)
	return err
}

func (s *Store) GapCount(ctx context.Context) (int64, error) {
	var count int64
	err := s.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM gaps").Scan(&count)
	return count, err
}

func (s *Store) Snapshots(ctx context.Context) ([]contract.Snapshot, error) {
	rows, err := s.db.QueryContext(ctx, "SELECT credential,day,version,upload,download FROM usage ORDER BY day,credential LIMIT 100")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []contract.Snapshot{}
	for rows.Next() {
		r := contract.Snapshot{SourceID: s.source}
		if err := rows.Scan(&r.CredentialID, &r.UsageDate, &r.Version, &r.Upload, &r.Download); err != nil {
			return nil, err
		}
		r.Digest = r.ComputeDigest()
		result = append(result, r)
	}
	return result, rows.Err()
}
