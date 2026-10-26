package exporter

// This file composes the production exporter and accounting store against the
// private Python/PostgreSQL fixture. It is test-only: the exporter package
// itself must not import accounting.

import (
	"context"
	"crypto/x509"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/accounting"
	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/traffic"
	_ "modernc.org/sqlite"
)

const (
	fixtureInvocation = "counter-export-fixture"
	fixtureEnv        = "SBM_COUNTER_EXPORT_FIXTURE"
)

type counterExportRow struct {
	CredentialID    string `json:"credential_id"`
	Upload          int64  `json:"upload"`
	Download        int64  `json:"download"`
	UpdatedUpload   int64  `json:"updated_upload"`
	UpdatedDownload int64  `json:"updated_download"`
	Changed         bool   `json:"changed"`
}

type counterExportFixture struct {
	Origin        string             `json:"origin"`
	CAFile        string             `json:"ca_file"`
	Token         string             `json:"token"`
	RejectedToken string             `json:"rejected_token"`
	SQLitePath    string             `json:"sqlite_path"`
	EvidencePath  string             `json:"evidence_path"`
	SourceID      string             `json:"source_id"`
	Day           string             `json:"day"`
	PageSize      int                `json:"page_size"`
	Scenario      string             `json:"scenario"`
	FailureMode   string             `json:"failure_mode"`
	Rows          []counterExportRow `json:"rows"`

	now time.Time
}

type counterExportPending struct {
	CredentialID   string `json:"credential_id"`
	AccountingDate string `json:"accounting_date"`
	Version        int64  `json:"version"`
	Upload         int64  `json:"upload"`
	Download       int64  `json:"download"`
	Digest         string `json:"digest"`
}

type counterExportStep struct {
	Name         string                 `json:"name"`
	Selected     int                    `json:"selected"`
	Acknowledged int                    `json:"acknowledged"`
	Error        string                 `json:"error,omitempty"`
	Pending      []counterExportPending `json:"pending"`
}

type counterExportUsage struct {
	CredentialID string `json:"credential_id"`
	Day          string `json:"day"`
	Version      int64  `json:"version"`
	Upload       int64  `json:"upload"`
	Download     int64  `json:"download"`
}

type counterExportAck struct {
	CredentialID string `json:"credential_id"`
	Day          string `json:"day"`
	Version      int64  `json:"version"`
	Digest       string `json:"digest"`
}

type counterExportEvidence struct {
	Scenario string               `json:"scenario"`
	Steps    []counterExportStep  `json:"steps"`
	Usage    []counterExportUsage `json:"usage"`
	Acks     []counterExportAck   `json:"acks"`
}

type counterExportRaceStore struct {
	*accounting.Store
	fixture counterExportFixture
	raced   bool
}

func (s *counterExportRaceStore) PendingCounters(ctx context.Context, limit int) ([]traffic.CounterSnapshot, error) {
	page, err := s.Store.PendingCounters(ctx, limit)
	if err != nil || s.raced {
		return page, err
	}
	s.raced = true
	if err := updateChangedRows(s.Store, s.fixture); err != nil {
		return nil, err
	}
	return page, nil
}

func loadCounterExportFixture(t *testing.T) counterExportFixture {
	t.Helper()
	path := os.Getenv(fixtureEnv)
	if path == "" {
		t.Skip("private counter export fixture is not configured")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture descriptor: %v", err)
	}
	var fixture counterExportFixture
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatalf("decode fixture descriptor: %v", err)
	}
	if fixture.Origin == "" || fixture.CAFile == "" || fixture.Token == "" ||
		fixture.SQLitePath == "" || fixture.EvidencePath == "" ||
		fixture.SourceID == "" || fixture.Day == "" || len(fixture.Rows) == 0 {
		t.Fatal("fixture descriptor is incomplete")
	}
	fixture.now, err = time.Parse("2006-01-02", fixture.Day)
	if err != nil {
		t.Fatalf("parse fixture day: %v", err)
	}
	if fixture.PageSize == 0 {
		fixture.PageSize = traffic.MaxPageItems
	}
	return fixture
}

func fixtureRootCAs(t *testing.T, fixture counterExportFixture) *x509.CertPool {
	t.Helper()
	raw, err := os.ReadFile(fixture.CAFile)
	if err != nil {
		t.Fatalf("read fixture CA: %v", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(raw) {
		t.Fatal("fixture CA contains no certificate")
	}
	return roots
}

func openCounterExportStore(t *testing.T, fixture counterExportFixture) *accounting.Store {
	t.Helper()
	store, err := accounting.Open(fixture.SQLitePath, fixture.SourceID)
	if err != nil {
		t.Fatalf("open fixture accounting store: %v", err)
	}
	return store
}

func seedInitialRows(t *testing.T, store *accounting.Store, fixture counterExportFixture) {
	t.Helper()
	for _, row := range fixture.Rows {
		if err := store.Record(
			context.Background(),
			fixtureInvocation,
			row.CredentialID,
			row.Upload,
			row.Download,
			fixture.now,
		); err != nil {
			t.Fatalf("seed fixture counter row: %v", err)
		}
	}
}

func updateChangedRows(store *accounting.Store, fixture counterExportFixture) error {
	for _, row := range fixture.Rows {
		if !row.Changed {
			continue
		}
		if row.UpdatedUpload < row.Upload || row.UpdatedDownload < row.Download {
			return errors.New("fixture update regresses counters")
		}
		if err := store.Record(
			context.Background(),
			fixtureInvocation,
			row.CredentialID,
			row.UpdatedUpload,
			row.UpdatedDownload,
			fixture.now,
		); err != nil {
			return err
		}
	}
	return nil
}

func counterExportClient(t *testing.T, store CounterStore, fixture counterExportFixture, roots *x509.CertPool, token string) *Client {
	t.Helper()
	client, err := New(store, Options{
		Origin:   fixture.Origin,
		Token:    token,
		RootCAs:  roots,
		PageSize: fixture.PageSize,
	})
	if err != nil {
		t.Fatalf("create fixture exporter: %v", err)
	}
	return client
}

func captureCounterExportStep(t *testing.T, name string, result Result, err error, store CounterStore) counterExportStep {
	t.Helper()
	step := counterExportStep{
		Name:         name,
		Selected:     result.Selected,
		Acknowledged: result.Acknowledged,
		Pending:      []counterExportPending{},
	}
	if err != nil {
		step.Error = err.Error()
	}
	page, pageErr := store.PendingCounters(context.Background(), traffic.MaxPageItems)
	if pageErr != nil {
		t.Fatalf("capture pending state after %s: %v", name, pageErr)
	}
	for _, snapshot := range page {
		step.Pending = append(step.Pending, counterExportPending{
			CredentialID:   snapshot.CredentialID,
			AccountingDate: string(snapshot.AccountingDate),
			Version:        snapshot.Version,
			Upload:         snapshot.Upload,
			Download:       snapshot.Download,
			Digest:         snapshot.Digest,
		})
	}
	return step
}

func requireFixtureResult(t *testing.T, step counterExportStep, selected, acknowledged, pending int) {
	t.Helper()
	if step.Selected != selected || step.Acknowledged != acknowledged {
		t.Fatalf("%s result = selected %d acknowledged %d, want %d/%d",
			step.Name, step.Selected, step.Acknowledged, selected, acknowledged)
	}
	if len(step.Pending) != pending {
		t.Fatalf("%s pending rows = %d, want %d", step.Name, len(step.Pending), pending)
	}
}

func requireFixtureError(t *testing.T, fixture counterExportFixture, err, expected error, code string) {
	t.Helper()
	if err == nil {
		t.Fatal("fixture export unexpectedly succeeded")
	}
	if !errors.Is(err, expected) {
		t.Fatalf("fixture export error = %v, want %v", err, expected)
	}
	if code != "" && !strings.Contains(err.Error(), code) {
		t.Fatalf("fixture export error lacks %q: %v", code, err)
	}
	for _, secret := range []string{fixture.Token, fixture.RejectedToken} {
		if secret != "" && strings.Contains(err.Error(), secret) {
			t.Fatal("fixture export error leaked the bearer token")
		}
	}
}

func changedCounterExportRows(fixture counterExportFixture) int {
	count := 0
	for _, row := range fixture.Rows {
		if row.Changed {
			count++
		}
	}
	return count
}

func readCounterExportState(t *testing.T, path string) ([]counterExportUsage, []counterExportAck) {
	t.Helper()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("open fixture SQLite state: %v", err)
	}
	defer db.Close()
	usageRows, err := db.Query(`SELECT credential, day, version, upload, download FROM usage ORDER BY day, credential`)
	if err != nil {
		t.Fatalf("read fixture usage: %v", err)
	}
	usage := []counterExportUsage{}
	for usageRows.Next() {
		var row counterExportUsage
		if err := usageRows.Scan(&row.CredentialID, &row.Day, &row.Version, &row.Upload, &row.Download); err != nil {
			usageRows.Close()
			t.Fatalf("scan fixture usage: %v", err)
		}
		usage = append(usage, row)
	}
	if err := usageRows.Err(); err != nil {
		usageRows.Close()
		t.Fatalf("iterate fixture usage: %v", err)
	}
	if err := usageRows.Close(); err != nil {
		t.Fatalf("close fixture usage rows: %v", err)
	}
	ackRows, err := db.Query(`SELECT credential, day, version, digest FROM counter_export_ack ORDER BY day, credential`)
	if err != nil {
		t.Fatalf("read fixture acknowledgements: %v", err)
	}
	acks := []counterExportAck{}
	for ackRows.Next() {
		var row counterExportAck
		if err := ackRows.Scan(&row.CredentialID, &row.Day, &row.Version, &row.Digest); err != nil {
			ackRows.Close()
			t.Fatalf("scan fixture acknowledgement: %v", err)
		}
		acks = append(acks, row)
	}
	if err := ackRows.Err(); err != nil {
		ackRows.Close()
		t.Fatalf("iterate fixture acknowledgements: %v", err)
	}
	if err := ackRows.Close(); err != nil {
		t.Fatalf("close fixture acknowledgement rows: %v", err)
	}
	return usage, acks
}

func writeCounterExportEvidence(t *testing.T, path string, evidence counterExportEvidence) {
	t.Helper()
	raw, err := json.MarshalIndent(evidence, "", "  ")
	if err != nil {
		t.Fatalf("encode fixture evidence: %v", err)
	}
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatalf("write fixture evidence: %v", err)
	}
}

func closeCounterExportStore(t *testing.T, store **accounting.Store) {
	t.Helper()
	if store == nil || *store == nil {
		return
	}
	if err := (*store).Close(); err != nil {
		t.Fatalf("close fixture accounting store: %v", err)
	}
	*store = nil
}

func TestPostgresCounterExportComposition(t *testing.T) {
	fixture := loadCounterExportFixture(t)
	roots := fixtureRootCAs(t, fixture)
	var evidence counterExportEvidence
	switch fixture.Scenario {
	case "closed_loop":
		evidence = runCounterExportClosedLoop(t, fixture, roots)
	case "lost_reply_retry":
		evidence = runCounterExportLostReply(t, fixture, roots)
	case "stale_selection":
		evidence = runCounterExportStaleSelection(t, fixture, roots)
	case "rejected_response":
		evidence = runCounterExportRejectedResponse(t, fixture, roots)
	case "auth_rejected":
		evidence = runCounterExportAuthRejected(t, fixture, roots)
	default:
		t.Fatalf("unknown fixture scenario %q", fixture.Scenario)
	}
	writeCounterExportEvidence(t, fixture.EvidencePath, evidence)
}

func runCounterExportClosedLoop(t *testing.T, fixture counterExportFixture, roots *x509.CertPool) counterExportEvidence {
	t.Helper()
	ctx := context.Background()
	store := openCounterExportStore(t, fixture)
	seedInitialRows(t, store, fixture)
	client := counterExportClient(t, store, fixture, roots, fixture.Token)
	result, err := client.ExportOnce(ctx)
	if err != nil {
		t.Fatalf("initial export: %v", err)
	}
	steps := []counterExportStep{captureCounterExportStep(t, "initial export", result, err, store)}
	requireFixtureResult(t, steps[0], len(fixture.Rows), len(fixture.Rows), 0)
	client.CloseIdleConnections()
	closeCounterExportStore(t, &store)

	store = openCounterExportStore(t, fixture)
	t.Cleanup(func() { closeCounterExportStore(t, &store) })
	client = counterExportClient(t, store, fixture, roots, fixture.Token)
	result, err = client.ExportOnce(ctx)
	if err != nil {
		t.Fatalf("reopened export: %v", err)
	}
	steps = append(steps, captureCounterExportStep(t, "reopened export", result, err, store))
	requireFixtureResult(t, steps[1], 0, 0, 0)

	if err := updateChangedRows(store, fixture); err != nil {
		t.Fatalf("update fixture counter row: %v", err)
	}
	result, err = client.ExportOnce(ctx)
	if err != nil {
		t.Fatalf("updated export: %v", err)
	}
	steps = append(steps, captureCounterExportStep(t, "updated export", result, err, store))
	requireFixtureResult(t, steps[2], changedCounterExportRows(fixture), changedCounterExportRows(fixture), 0)
	client.CloseIdleConnections()
	closeCounterExportStore(t, &store)
	usage, acks := readCounterExportState(t, fixture.SQLitePath)
	return counterExportEvidence{Scenario: fixture.Scenario, Steps: steps, Usage: usage, Acks: acks}
}

func runCounterExportLostReply(t *testing.T, fixture counterExportFixture, roots *x509.CertPool) counterExportEvidence {
	t.Helper()
	ctx := context.Background()
	store := openCounterExportStore(t, fixture)
	t.Cleanup(func() { closeCounterExportStore(t, &store) })
	seedInitialRows(t, store, fixture)
	client := counterExportClient(t, store, fixture, roots, fixture.Token)
	result, err := client.ExportOnce(ctx)
	requireFixtureError(t, fixture, err, ErrRequest, "")
	steps := []counterExportStep{captureCounterExportStep(t, "lost reply", result, err, store)}
	requireFixtureResult(t, steps[0], len(fixture.Rows), 0, len(fixture.Rows))
	result, err = client.ExportOnce(ctx)
	if err != nil {
		t.Fatalf("retry export: %v", err)
	}
	steps = append(steps, captureCounterExportStep(t, "retry", result, err, store))
	requireFixtureResult(t, steps[1], len(fixture.Rows), len(fixture.Rows), 0)
	client.CloseIdleConnections()
	closeCounterExportStore(t, &store)
	usage, acks := readCounterExportState(t, fixture.SQLitePath)
	return counterExportEvidence{Scenario: fixture.Scenario, Steps: steps, Usage: usage, Acks: acks}
}

func runCounterExportStaleSelection(t *testing.T, fixture counterExportFixture, roots *x509.CertPool) counterExportEvidence {
	t.Helper()
	ctx := context.Background()
	store := openCounterExportStore(t, fixture)
	t.Cleanup(func() { closeCounterExportStore(t, &store) })
	seedInitialRows(t, store, fixture)
	racing := &counterExportRaceStore{Store: store, fixture: fixture}
	client := counterExportClient(t, racing, fixture, roots, fixture.Token)
	result, err := client.ExportOnce(ctx)
	if err != nil {
		t.Fatalf("stale selection export: %v", err)
	}
	steps := []counterExportStep{captureCounterExportStep(t, "older receipt", result, err, racing)}
	requireFixtureResult(t, steps[0], len(fixture.Rows), changedCounterExportRows(fixture), changedCounterExportRows(fixture))
	result, err = client.ExportOnce(ctx)
	if err != nil {
		t.Fatalf("newer selection export: %v", err)
	}
	steps = append(steps, captureCounterExportStep(t, "newer export", result, err, racing))
	requireFixtureResult(t, steps[1], changedCounterExportRows(fixture), changedCounterExportRows(fixture), 0)
	client.CloseIdleConnections()
	closeCounterExportStore(t, &store)
	usage, acks := readCounterExportState(t, fixture.SQLitePath)
	return counterExportEvidence{Scenario: fixture.Scenario, Steps: steps, Usage: usage, Acks: acks}
}

func runCounterExportRejectedResponse(t *testing.T, fixture counterExportFixture, roots *x509.CertPool) counterExportEvidence {
	t.Helper()
	ctx := context.Background()
	store := openCounterExportStore(t, fixture)
	t.Cleanup(func() { closeCounterExportStore(t, &store) })
	seedInitialRows(t, store, fixture)
	client := counterExportClient(t, store, fixture, roots, fixture.Token)
	result, err := client.ExportOnce(ctx)
	switch fixture.FailureMode {
	case "wrong_coverage", "truncated":
		requireFixtureError(t, fixture, err, ErrResponseInvalid, "")
	case "error_status":
		requireFixtureError(t, fixture, err, ErrRequestStatus, "status 500")
	default:
		t.Fatalf("unknown failure mode %q", fixture.FailureMode)
	}
	steps := []counterExportStep{captureCounterExportStep(t, "rejected response", result, err, store)}
	requireFixtureResult(t, steps[0], len(fixture.Rows), 0, len(fixture.Rows))
	result, err = client.ExportOnce(ctx)
	if err != nil {
		t.Fatalf("retry export: %v", err)
	}
	steps = append(steps, captureCounterExportStep(t, "retry", result, err, store))
	requireFixtureResult(t, steps[1], len(fixture.Rows), len(fixture.Rows), 0)
	client.CloseIdleConnections()
	closeCounterExportStore(t, &store)
	usage, acks := readCounterExportState(t, fixture.SQLitePath)
	return counterExportEvidence{Scenario: fixture.Scenario, Steps: steps, Usage: usage, Acks: acks}
}

func runCounterExportAuthRejected(t *testing.T, fixture counterExportFixture, roots *x509.CertPool) counterExportEvidence {
	t.Helper()
	ctx := context.Background()
	store := openCounterExportStore(t, fixture)
	t.Cleanup(func() { closeCounterExportStore(t, &store) })
	seedInitialRows(t, store, fixture)
	rejected := counterExportClient(t, store, fixture, roots, fixture.RejectedToken)
	result, err := rejected.ExportOnce(ctx)
	requireFixtureError(t, fixture, err, ErrRequestStatus, "status 401")
	steps := []counterExportStep{captureCounterExportStep(t, "fixture auth rejected", result, err, store)}
	requireFixtureResult(t, steps[0], len(fixture.Rows), 0, len(fixture.Rows))
	rejected.CloseIdleConnections()
	client := counterExportClient(t, store, fixture, roots, fixture.Token)
	result, err = client.ExportOnce(ctx)
	if err != nil {
		t.Fatalf("authorized retry export: %v", err)
	}
	steps = append(steps, captureCounterExportStep(t, "authorized retry", result, err, store))
	requireFixtureResult(t, steps[1], len(fixture.Rows), len(fixture.Rows), 0)
	client.CloseIdleConnections()
	closeCounterExportStore(t, &store)
	usage, acks := readCounterExportState(t, fixture.SQLitePath)
	return counterExportEvidence{Scenario: fixture.Scenario, Steps: steps, Usage: usage, Acks: acks}
}
