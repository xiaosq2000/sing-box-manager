package accounting

import (
	"context"
	"fmt"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/contract"
	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/traffic"
)

var exportDay = time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC)

func testUUID(index int) string {
	text := fmt.Sprintf("%032x", index)
	return text[0:8] + "-" + text[8:12] + "-" + text[12:16] + "-" + text[16:20] + "-" + text[20:32]
}

func openExportStore(t *testing.T, source string) *Store {
	t.Helper()
	return openExportStoreAt(t, filepath.Join(t.TempDir(), "traffic.sqlite3"), source)
}

func openExportStoreAt(t *testing.T, path, source string) *Store {
	t.Helper()
	store, err := Open(path, source)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

func recordSample(t *testing.T, store *Store, credential string, day time.Time, upload, download int64) {
	t.Helper()
	if err := store.Record(context.Background(), "export-engine", credential, upload, download, day); err != nil {
		t.Fatal(err)
	}
}

// seedFinalityAndGap commits durable finality and gap rows without adding a
// usage bucket, so ledger captures contain nonempty rows that an in-place
// mutation or deletion would reveal.
func seedFinalityAndGap(t *testing.T, store *Store) {
	t.Helper()
	ctx := context.Background()
	if err := store.Record(ctx, "finished-engine", testUUID(99), 0, 0, exportDay); err != nil {
		t.Fatal(err)
	}
	if err := store.Finish(ctx, "finished-engine"); err != nil {
		t.Fatal(err)
	}
	if err := store.Gap(ctx, "gap-engine"); err != nil {
		t.Fatal(err)
	}
}

func requireFinalityAndGap(t *testing.T, state ledgerState) {
	t.Helper()
	if !reflect.DeepEqual(state.finished, []string{"finished-engine"}) {
		t.Fatalf("finished rows = %#v", state.finished)
	}
	if !reflect.DeepEqual(state.gaps, []string{"gap-engine|unobserved_termination"}) {
		t.Fatalf("gap rows = %#v", state.gaps)
	}
}

func pendingPage(t *testing.T, store *Store, limit int) []traffic.CounterSnapshot {
	t.Helper()
	pending, err := store.PendingCounters(context.Background(), limit)
	if err != nil {
		t.Fatal(err)
	}
	return pending
}

func receiptFor(counter traffic.CounterSnapshot) traffic.Receipt {
	return traffic.Receipt{
		SourceID:       counter.SourceID,
		CredentialID:   counter.CredentialID,
		Kind:           traffic.KindCounter,
		AccountingDate: counter.AccountingDate,
		Version:        counter.Version,
		Digest:         counter.Digest,
	}
}

func pageFor(t *testing.T, counters []traffic.CounterSnapshot) (traffic.Request, traffic.ReceiptResponse) {
	t.Helper()
	snapshots := make([]traffic.Snapshot, len(counters))
	receipts := make([]traffic.Receipt, len(counters))
	for index, counter := range counters {
		snapshots[index] = counter
		receipts[index] = receiptFor(counter)
	}
	request, err := traffic.MakeRequest(snapshots...)
	if err != nil {
		t.Fatal(err)
	}
	return request, traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: receipts}
}

func acknowledgePage(t *testing.T, store *Store, counters []traffic.CounterSnapshot) int {
	t.Helper()
	request, response := pageFor(t, counters)
	count, err := store.AcknowledgeCounters(context.Background(), request, response)
	if err != nil {
		t.Fatal(err)
	}
	return count
}

func queryStrings(t *testing.T, store *Store, query string) []string {
	t.Helper()
	rows, err := store.db.Query(query)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		t.Fatal(err)
	}
	result := []string{}
	for rows.Next() {
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for index := range values {
			pointers[index] = &values[index]
		}
		if err := rows.Scan(pointers...); err != nil {
			t.Fatal(err)
		}
		parts := make([]string, len(values))
		for index, value := range values {
			parts[index] = fmt.Sprint(value)
		}
		result = append(result, strings.Join(parts, "|"))
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return result
}

type ledgerState struct {
	checkpoints []string
	usage       []string
	gaps        []string
	finished    []string
}

func captureLedger(t *testing.T, store *Store) ledgerState {
	t.Helper()
	return ledgerState{
		checkpoints: queryStrings(t, store, `SELECT invocation, credential, upload, download FROM checkpoints ORDER BY invocation, credential`),
		usage:       queryStrings(t, store, `SELECT credential, day, version, upload, download FROM usage ORDER BY credential, day`),
		gaps:        queryStrings(t, store, `SELECT invocation, reason FROM gaps ORDER BY invocation`),
		finished:    queryStrings(t, store, `SELECT invocation FROM finished ORDER BY invocation`),
	}
}

func ackRows(t *testing.T, store *Store) []string {
	t.Helper()
	return queryStrings(t, store, `SELECT credential, day, version, digest FROM counter_export_ack ORDER BY credential, day`)
}

func requireCount(t *testing.T, store *Store, query string, want int64) {
	t.Helper()
	var count int64
	if err := store.db.QueryRow(query).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != want {
		t.Fatalf("count = %d, want %d", count, want)
	}
}

func TestPendingCountersEmptyLimitsAndInvalidIdentities(t *testing.T) {
	ctx := context.Background()
	source := testUUID(9000)
	store := openExportStore(t, source)
	for _, limit := range []int{0, -1, traffic.MaxPageItems + 1} {
		if _, err := store.PendingCounters(ctx, limit); err == nil {
			t.Fatalf("limit %d accepted", limit)
		}
	}
	pending, err := store.PendingCounters(ctx, traffic.MaxPageItems)
	if err != nil || pending == nil || len(pending) != 0 {
		t.Fatalf("empty page = %#v, %v", pending, err)
	}

	recordSample(t, store, "credential-one", exportDay, 1, 2)
	if _, err := store.PendingCounters(ctx, 1); err == nil {
		t.Fatal("invalid credential identity selected")
	}

	invalidSource := openExportStore(t, "source-one")
	recordSample(t, invalidSource, testUUID(1), exportDay, 1, 2)
	if _, err := invalidSource.PendingCounters(ctx, 1); err == nil {
		t.Fatal("invalid source identity selected")
	}
}

func TestPendingCountersDeterministicBoundedPagesAndDraining(t *testing.T) {
	store := openExportStore(t, testUUID(9001))
	dayOne := exportDay
	dayTwo := exportDay.Add(24 * time.Hour)
	for index := 1; index <= 60; index++ {
		recordSample(t, store, testUUID(index), dayOne, int64(index), int64(index*2))
	}
	for index := 61; index <= 120; index++ {
		recordSample(t, store, testUUID(index), dayTwo, int64(index), int64(index*2))
	}

	first := pendingPage(t, store, 100)
	if len(first) != 100 {
		t.Fatalf("first page length = %d", len(first))
	}
	for index, counter := range first {
		wantCredential := testUUID(index + 1)
		wantDay := dayOne
		if index >= 60 {
			wantDay = dayTwo
		}
		if counter.CredentialID != wantCredential || counter.AccountingDate != traffic.Date(wantDay.Format("2006-01-02")) {
			t.Fatalf("first page item %d = (%s, %s)", index, counter.CredentialID, counter.AccountingDate)
		}
	}
	if count := acknowledgePage(t, store, first); count != 100 {
		t.Fatalf("acknowledged = %d", count)
	}

	second := pendingPage(t, store, 100)
	if len(second) != 20 {
		t.Fatalf("second page length = %d", len(second))
	}
	for index, counter := range second {
		if counter.CredentialID != testUUID(101+index) {
			t.Fatalf("second page item %d = %s", index, counter.CredentialID)
		}
	}
	if count := acknowledgePage(t, store, second); count != 20 {
		t.Fatalf("acknowledged = %d", count)
	}
	if remaining := pendingPage(t, store, 1); len(remaining) != 0 {
		t.Fatalf("drained page = %#v", remaining)
	}
}

func TestPendingCountersDrainsMoreThanOneMaxPage(t *testing.T) {
	store := openExportStore(t, testUUID(9002))
	for index := 1; index <= traffic.MaxPageItems+1; index++ {
		recordSample(t, store, testUUID(index), exportDay, int64(index), int64(index*2))
	}

	first := pendingPage(t, store, traffic.MaxPageItems)
	if len(first) != traffic.MaxPageItems {
		t.Fatalf("first page length = %d", len(first))
	}
	if first[0].CredentialID != testUUID(1) || first[len(first)-1].CredentialID != testUUID(traffic.MaxPageItems) {
		t.Fatalf("first page bounds = (%s, %s)", first[0].CredentialID, first[len(first)-1].CredentialID)
	}
	if count := acknowledgePage(t, store, first); count != traffic.MaxPageItems {
		t.Fatalf("acknowledged = %d", count)
	}

	last := pendingPage(t, store, traffic.MaxPageItems)
	if len(last) != 1 || last[0].CredentialID != testUUID(traffic.MaxPageItems+1) {
		t.Fatalf("last page = %#v", last)
	}
	if count := acknowledgePage(t, store, last); count != 1 {
		t.Fatalf("acknowledged = %d", count)
	}
	if remaining := pendingPage(t, store, traffic.MaxPageItems); len(remaining) != 0 {
		t.Fatalf("drained page = %#v", remaining)
	}
}

func TestPendingCountersDirtyKeyAfterAcknowledgedPrefix(t *testing.T) {
	store := openExportStore(t, testUUID(9003))
	for index := 1; index <= 150; index++ {
		recordSample(t, store, testUUID(index), exportDay, int64(index), int64(index*2))
	}

	prefix := pendingPage(t, store, 100)
	if len(prefix) != 100 {
		t.Fatalf("prefix page length = %d", len(prefix))
	}
	if count := acknowledgePage(t, store, prefix); count != 100 {
		t.Fatalf("acknowledged = %d", count)
	}

	remaining := pendingPage(t, store, 100)
	if len(remaining) != 50 || remaining[0].CredentialID != testUUID(101) {
		t.Fatalf("remaining page = %#v", remaining)
	}
	if count := acknowledgePage(t, store, remaining); count != 50 {
		t.Fatalf("acknowledged = %d", count)
	}

	recordSample(t, store, testUUID(1), exportDay, 10_000, 20_000)
	dirty := pendingPage(t, store, 1)
	if len(dirty) != 1 || dirty[0].CredentialID != testUUID(1) || dirty[0].Version != 2 {
		t.Fatalf("dirty head = %#v", dirty)
	}
	if count := acknowledgePage(t, store, dirty); count != 1 {
		t.Fatalf("acknowledged = %d", count)
	}

	recordSample(t, store, testUUID(150), exportDay, 20_000, 40_000)
	dirtyTail := pendingPage(t, store, 1)
	if len(dirtyTail) != 1 || dirtyTail[0].CredentialID != testUUID(150) || dirtyTail[0].Version != 2 {
		t.Fatalf("dirty tail = %#v", dirtyTail)
	}
	if count := acknowledgePage(t, store, dirtyTail); count != 1 {
		t.Fatalf("acknowledged = %d", count)
	}

	recordSample(t, store, testUUID(200), exportDay.Add(24*time.Hour), 1, 2)
	newDay := pendingPage(t, store, 1)
	if len(newDay) != 1 || newDay[0].CredentialID != testUUID(200) {
		t.Fatalf("new day page = %#v", newDay)
	}
}

func TestPendingCountersUsesWireV1Digest(t *testing.T) {
	source := testUUID(9004)
	store := openExportStore(t, source)
	credential := testUUID(1)
	recordSample(t, store, credential, exportDay, 1024, 2048)

	pending := pendingPage(t, store, 1)
	if len(pending) != 1 {
		t.Fatalf("pending = %#v", pending)
	}
	want, err := traffic.MakeCounterSnapshot(source, credential, traffic.Date("2026-10-23"), 1, 1024, 2048)
	if err != nil {
		t.Fatal(err)
	}
	if pending[0] != want {
		t.Fatalf("pending = %#v, want %#v", pending[0], want)
	}
	legacy := contract.Snapshot{
		SourceID:     source,
		CredentialID: credential,
		UsageDate:    "2026-10-23",
		Version:      1,
		Upload:       1024,
		Download:     2048,
	}
	legacy.Digest = legacy.ComputeDigest()
	if err := legacy.Validate(); err != nil {
		t.Fatal(err)
	}
	if pending[0].Digest == legacy.Digest {
		t.Fatal("pending page reused the legacy G0 digest")
	}
	request, _ := pageFor(t, pending)
	if _, err := traffic.EncodeRequest(request); err != nil {
		t.Fatalf("pending page is not wire-valid: %v", err)
	}
}

func TestAcknowledgeCountersExactIdempotentAndReopened(t *testing.T) {
	path := filepath.Join(t.TempDir(), "traffic.sqlite3")
	source := testUUID(9005)
	store := openExportStoreAt(t, path, source)
	recordSample(t, store, testUUID(1), exportDay, 10, 20)
	recordSample(t, store, testUUID(2), exportDay, 30, 40)
	seedFinalityAndGap(t, store)

	before := captureLedger(t, store)
	requireFinalityAndGap(t, before)
	page := pendingPage(t, store, 10)
	if len(page) != 2 {
		t.Fatalf("pending = %#v", page)
	}
	if count := acknowledgePage(t, store, page); count != 2 {
		t.Fatalf("acknowledged = %d", count)
	}
	if after := captureLedger(t, store); !reflect.DeepEqual(before, after) {
		t.Fatalf("ack changed the counter ledger:\nbefore=%#v\nafter=%#v", before, after)
	}
	if pending := pendingPage(t, store, 10); len(pending) != 0 {
		t.Fatalf("pending after ack = %#v", pending)
	}
	firstRows := ackRows(t, store)
	if len(firstRows) != 2 {
		t.Fatalf("ack rows = %#v", firstRows)
	}

	request, response := pageFor(t, page)
	if count, err := store.AcknowledgeCounters(context.Background(), request, response); err != nil || count != 2 {
		t.Fatalf("idempotent ack = %d, %v", count, err)
	}
	if rows := ackRows(t, store); !reflect.DeepEqual(firstRows, rows) {
		t.Fatalf("idempotent ack changed rows:\nfirst=%#v\nsecond=%#v", firstRows, rows)
	}
	if after := captureLedger(t, store); !reflect.DeepEqual(before, after) {
		t.Fatalf("idempotent ack changed the ledger:\nbefore=%#v\nafter=%#v", before, after)
	}

	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	store = openExportStoreAt(t, path, source)
	if pending := pendingPage(t, store, 10); len(pending) != 0 {
		t.Fatalf("reopened pending = %#v", pending)
	}
	if count, err := store.AcknowledgeCounters(context.Background(), request, response); err != nil || count != 2 {
		t.Fatalf("reopened idempotent ack = %d, %v", count, err)
	}
	if rows := ackRows(t, store); !reflect.DeepEqual(firstRows, rows) {
		t.Fatalf("reopened ack changed rows:\nfirst=%#v\nreopened=%#v", firstRows, rows)
	}
	if after := captureLedger(t, store); !reflect.DeepEqual(before, after) {
		t.Fatalf("reopened ack changed the ledger:\nbefore=%#v\nafter=%#v", before, after)
	}

	joined := queryStrings(t, store, `
SELECT usage.credential, usage.day, counter_export_ack.version, counter_export_ack.digest, usage.upload, usage.download
FROM counter_export_ack JOIN usage
  ON counter_export_ack.credential = usage.credential AND counter_export_ack.day = usage.day
ORDER BY usage.credential, usage.day`)
	if len(joined) != 2 {
		t.Fatalf("joined ack rows = %#v", joined)
	}
	for _, counter := range page {
		want, err := traffic.MakeCounterSnapshot(source, counter.CredentialID, counter.AccountingDate, counter.Version, counter.Upload, counter.Download)
		if err != nil {
			t.Fatal(err)
		}
		var version int64
		var digest string
		if err := store.db.QueryRow(`SELECT version, digest FROM counter_export_ack WHERE credential = ? AND day = ?`, counter.CredentialID, string(counter.AccountingDate)).Scan(&version, &digest); err != nil {
			t.Fatal(err)
		}
		if version != want.Version || digest != want.Digest {
			t.Fatalf("stored ack = (%d, %s), want (%d, %s)", version, digest, want.Version, want.Digest)
		}
	}
}

func TestAcknowledgeCountersRejectsInvalidPagesWithoutMutation(t *testing.T) {
	source := testUUID(9006)
	otherSource := testUUID(9007)
	credential := testUUID(1)

	validSnapshot := func(t *testing.T) traffic.CounterSnapshot {
		t.Helper()
		snapshot, err := traffic.MakeCounterSnapshot(source, credential, traffic.Date("2026-10-23"), 1, 10, 20)
		if err != nil {
			t.Fatal(err)
		}
		return snapshot
	}
	otherSnapshot := func(t *testing.T) traffic.CounterSnapshot {
		t.Helper()
		snapshot, err := traffic.MakeCounterSnapshot(otherSource, credential, traffic.Date("2026-10-23"), 1, 10, 20)
		if err != nil {
			t.Fatal(err)
		}
		return snapshot
	}
	tests := []struct {
		name              string
		wantErrorContains string
		request           func(t *testing.T) traffic.Request
		response          func(t *testing.T, request traffic.Request) traffic.ReceiptResponse
	}{
		{
			name: "request version",
			request: func(t *testing.T) traffic.Request {
				snapshot := validSnapshot(t)
				snapshot.Version = 0
				return traffic.Request{Schema: traffic.SchemaVersion, Snapshots: []traffic.Snapshot{snapshot}}
			},
			response: func(t *testing.T, request traffic.Request) traffic.ReceiptResponse {
				return traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: []traffic.Receipt{receiptFor(validSnapshot(t))}}
			},
		},
		{
			name: "request negative upload",
			request: func(t *testing.T) traffic.Request {
				snapshot := validSnapshot(t)
				snapshot.Upload = -1
				return traffic.Request{Schema: traffic.SchemaVersion, Snapshots: []traffic.Snapshot{snapshot}}
			},
			response: func(t *testing.T, request traffic.Request) traffic.ReceiptResponse {
				return traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: []traffic.Receipt{receiptFor(validSnapshot(t))}}
			},
		},
		{
			name: "response version",
			request: func(t *testing.T) traffic.Request {
				return traffic.Request{Schema: traffic.SchemaVersion, Snapshots: []traffic.Snapshot{validSnapshot(t)}}
			},
			response: func(t *testing.T, request traffic.Request) traffic.ReceiptResponse {
				receipt := receiptFor(validSnapshot(t))
				receipt.Version = 0
				return traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: []traffic.Receipt{receipt}}
			},
		},
		{
			name: "response malformed digest",
			request: func(t *testing.T) traffic.Request {
				return traffic.Request{Schema: traffic.SchemaVersion, Snapshots: []traffic.Snapshot{validSnapshot(t)}}
			},
			response: func(t *testing.T, request traffic.Request) traffic.ReceiptResponse {
				receipt := receiptFor(validSnapshot(t))
				receipt.Digest = strings.Repeat("0", 63)
				return traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: []traffic.Receipt{receipt}}
			},
		},
		{
			name:              "source mismatch",
			wantErrorContains: "source mismatch",
			request: func(t *testing.T) traffic.Request {
				return traffic.Request{Schema: traffic.SchemaVersion, Snapshots: []traffic.Snapshot{otherSnapshot(t)}}
			},
			response: func(t *testing.T, request traffic.Request) traffic.ReceiptResponse {
				if _, err := traffic.EncodeRequest(request); err != nil {
					t.Fatalf("source mismatch request is not codec-valid: %v", err)
				}
				response := traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: []traffic.Receipt{receiptFor(otherSnapshot(t))}}
				if _, err := traffic.EncodeReceiptResponse(response, request); err != nil {
					t.Fatalf("source mismatch response is not exact-cover codec-valid: %v", err)
				}
				return response
			},
		},
		{
			name: "mixed kinds",
			request: func(t *testing.T) traffic.Request {
				attribution, err := traffic.MakeAttributionSnapshot(source, testUUID(2), traffic.Date("2026-10-23"), 1, traffic.DetailExact, nil, nil)
				if err != nil {
					t.Fatal(err)
				}
				return traffic.Request{Schema: traffic.SchemaVersion, Snapshots: []traffic.Snapshot{validSnapshot(t), attribution}}
			},
			response: func(t *testing.T, request traffic.Request) traffic.ReceiptResponse {
				attribution, err := traffic.MakeAttributionSnapshot(source, testUUID(2), traffic.Date("2026-10-23"), 1, traffic.DetailExact, nil, nil)
				if err != nil {
					t.Fatal(err)
				}
				return traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: []traffic.Receipt{
					receiptFor(validSnapshot(t)),
					{SourceID: attribution.SourceID, CredentialID: attribution.CredentialID, Kind: attribution.Kind(), AccountingDate: attribution.AccountingDate, Version: attribution.Version, Digest: attribution.Digest},
				}}
			},
		},
		{
			name: "response mixed kind",
			request: func(t *testing.T) traffic.Request {
				return traffic.Request{Schema: traffic.SchemaVersion, Snapshots: []traffic.Snapshot{validSnapshot(t)}}
			},
			response: func(t *testing.T, request traffic.Request) traffic.ReceiptResponse {
				counter := validSnapshot(t)
				response := traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: []traffic.Receipt{
					{SourceID: counter.SourceID, CredentialID: counter.CredentialID, Kind: traffic.KindAttribution, AccountingDate: counter.AccountingDate, Version: counter.Version, Digest: counter.Digest},
				}}
				_, err := traffic.EncodeReceiptResponse(response, request)
				if code, ok := traffic.ErrorCodeOf(err); !ok || code != traffic.ErrReceiptKeyMismatch {
					t.Fatalf("mixed-kind response codec error = %v, want %s", err, traffic.ErrReceiptKeyMismatch)
				}
				return response
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			store := openExportStore(t, source)
			recordSample(t, store, credential, exportDay, 10, 20)
			request := test.request(t)
			response := test.response(t, request)
			count, err := store.AcknowledgeCounters(context.Background(), request, response)
			if err == nil || count != 0 {
				t.Fatalf("ack = %d, %v; want error and zero", count, err)
			}
			if test.wantErrorContains != "" && !strings.Contains(err.Error(), test.wantErrorContains) {
				t.Fatalf("ack error = %v, want containing %q", err, test.wantErrorContains)
			}
			if rows := ackRows(t, store); len(rows) != 0 {
				t.Fatalf("invalid page created ack rows: %#v", rows)
			}
			if pending := pendingPage(t, store, 10); len(pending) != 1 || pending[0].CredentialID != credential {
				t.Fatalf("invalid page changed pending state: %#v", pending)
			}
		})
	}
}

func TestAcknowledgeCountersRejectsIncompleteCoverageWithoutPartialAck(t *testing.T) {
	store := openExportStore(t, testUUID(9008))
	recordSample(t, store, testUUID(1), exportDay, 10, 20)
	recordSample(t, store, testUUID(2), exportDay, 30, 40)
	page := pendingPage(t, store, 10)
	if len(page) != 2 {
		t.Fatalf("pending = %#v", page)
	}
	request, response := pageFor(t, page)
	response.Receipts = response.Receipts[:1]
	if count, err := store.AcknowledgeCounters(context.Background(), request, response); err == nil || count != 0 {
		t.Fatalf("short coverage ack = %d, %v", count, err)
	}
	if rows := ackRows(t, store); len(rows) != 0 {
		t.Fatalf("partial coverage created ack rows: %#v", rows)
	}

	extra := receiptFor(traffic.CounterSnapshot{SourceID: page[0].SourceID, CredentialID: testUUID(3), AccountingDate: page[0].AccountingDate, Version: 1, Digest: strings.Repeat("a", 64)})
	response.Receipts = append(response.Receipts, extra)
	if count, err := store.AcknowledgeCounters(context.Background(), request, response); err == nil || count != 0 {
		t.Fatalf("extra coverage ack = %d, %v", count, err)
	}
	if rows := ackRows(t, store); len(rows) != 0 {
		t.Fatalf("extra coverage created ack rows: %#v", rows)
	}

	duplicate := traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: []traffic.Receipt{response.Receipts[0], response.Receipts[0]}}
	if count, err := store.AcknowledgeCounters(context.Background(), request, duplicate); err == nil || count != 0 {
		t.Fatalf("duplicate coverage ack = %d, %v", count, err)
	}
	if rows := ackRows(t, store); len(rows) != 0 {
		t.Fatalf("duplicate coverage created ack rows: %#v", rows)
	}
}

func TestAcknowledgeCountersStaleAheadAndWrongDigestStayPending(t *testing.T) {
	source := testUUID(9009)
	credential := testUUID(1)
	store := openExportStore(t, source)
	recordSample(t, store, credential, exportDay, 10, 20)
	stale, err := traffic.MakeCounterSnapshot(source, credential, traffic.Date("2026-10-23"), 1, 10, 20)
	if err != nil {
		t.Fatal(err)
	}
	recordSample(t, store, credential, exportDay, 30, 40)
	current := pendingPage(t, store, 1)
	if len(current) != 1 || current[0].Version != 2 {
		t.Fatalf("current = %#v", current)
	}
	ahead, err := traffic.MakeCounterSnapshot(source, credential, traffic.Date("2026-10-23"), 3, 50, 60)
	if err != nil {
		t.Fatal(err)
	}
	request, _ := pageFor(t, current)
	cases := []struct {
		name    string
		receipt traffic.Receipt
	}{
		{name: "stale", receipt: receiptFor(stale)},
		{name: "ahead", receipt: receiptFor(ahead)},
		{name: "wrong digest", receipt: traffic.Receipt{SourceID: current[0].SourceID, CredentialID: current[0].CredentialID, Kind: traffic.KindCounter, AccountingDate: current[0].AccountingDate, Version: current[0].Version, Digest: strings.Repeat("0", 64)}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			response := traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: []traffic.Receipt{test.receipt}}
			count, err := store.AcknowledgeCounters(context.Background(), request, response)
			if err != nil || count != 0 {
				t.Fatalf("ack = %d, %v", count, err)
			}
			if rows := ackRows(t, store); len(rows) != 0 {
				t.Fatalf("mismatched receipt created ack rows: %#v", rows)
			}
			if pending := pendingPage(t, store, 1); len(pending) != 1 || pending[0].Version != 2 {
				t.Fatalf("mismatched receipt changed pending state: %#v", pending)
			}
		})
	}
	if count := acknowledgePage(t, store, current); count != 1 {
		t.Fatalf("exact ack = %d", count)
	}
	if pending := pendingPage(t, store, 1); len(pending) != 0 {
		t.Fatalf("exact ack left pending state: %#v", pending)
	}
}

func TestAcknowledgeCountersLocalUpdateRace(t *testing.T) {
	source := testUUID(9010)
	credential := testUUID(1)
	store := openExportStore(t, source)
	recordSample(t, store, credential, exportDay, 10, 20)

	oldPage := pendingPage(t, store, 1)
	recordSample(t, store, credential, exportDay, 30, 40)
	if count := acknowledgePage(t, store, oldPage); count != 0 {
		t.Fatalf("stale page acknowledged = %d", count)
	}
	if rows := ackRows(t, store); len(rows) != 0 {
		t.Fatalf("stale page created ack rows: %#v", rows)
	}
	current := pendingPage(t, store, 1)
	if len(current) != 1 || current[0].Version != 2 {
		t.Fatalf("current after update = %#v", current)
	}

	request, response := pageFor(t, current)
	recordSample(t, store, credential, exportDay, 50, 60)
	newer := pendingPage(t, store, 1)
	if len(newer) != 1 || newer[0].Version != 3 {
		t.Fatalf("newer state = %#v", newer)
	}
	response.Receipts = []traffic.Receipt{receiptFor(newer[0])}
	if count, err := store.AcknowledgeCounters(context.Background(), request, response); err != nil || count != 1 {
		t.Fatalf("newer receipt ack = %d, %v", count, err)
	}
	if pending := pendingPage(t, store, 1); len(pending) != 0 {
		t.Fatalf("newer receipt left pending state: %#v", pending)
	}
	var version int64
	var digest string
	if err := store.db.QueryRow(`SELECT version, digest FROM counter_export_ack`).Scan(&version, &digest); err != nil {
		t.Fatal(err)
	}
	if version != newer[0].Version || digest != newer[0].Digest {
		t.Fatalf("ack = (%d, %s), want (%d, %s)", version, digest, newer[0].Version, newer[0].Digest)
	}

	recordSample(t, store, credential, exportDay, 50, 60)
	if pending := pendingPage(t, store, 1); len(pending) != 0 {
		t.Fatalf("zero-delta sample changed pending state: %#v", pending)
	}
}

func TestAcknowledgeCountersMissingCurrentRowCreatesNoOrphan(t *testing.T) {
	source := testUUID(9011)
	store := openExportStore(t, source)
	existing := testUUID(1)
	missing := testUUID(2)
	recordSample(t, store, existing, exportDay, 10, 20)
	page := pendingPage(t, store, 1)
	if len(page) != 1 {
		t.Fatalf("pending = %#v", page)
	}
	absent, err := traffic.MakeCounterSnapshot(source, missing, traffic.Date("2026-10-23"), 1, 5, 7)
	if err != nil {
		t.Fatal(err)
	}
	request, response := pageFor(t, []traffic.CounterSnapshot{page[0], absent})
	count, err := store.AcknowledgeCounters(context.Background(), request, response)
	if err != nil || count != 1 {
		t.Fatalf("ack = %d, %v", count, err)
	}
	rows := ackRows(t, store)
	if len(rows) != 1 || !strings.HasPrefix(rows[0], existing) {
		t.Fatalf("ack rows = %#v", rows)
	}
	if pending := pendingPage(t, store, 10); len(pending) != 0 {
		t.Fatalf("pending = %#v", pending)
	}
	if count, err := store.AcknowledgeCounters(context.Background(), request, response); err != nil || count != 1 {
		t.Fatalf("repeated ack = %d, %v", count, err)
	}
	if rows := ackRows(t, store); len(rows) != 1 {
		t.Fatalf("repeated ack rows = %#v", rows)
	}
}

func TestAcknowledgeCountersPartialWriteRollsBack(t *testing.T) {
	store := openExportStore(t, testUUID(9012))
	for index := 1; index <= 2; index++ {
		recordSample(t, store, testUUID(index), exportDay, int64(index*10), int64(index*20))
	}
	page := pendingPage(t, store, 10)
	if len(page) != 2 {
		t.Fatalf("pending = %#v", page)
	}
	seedFinalityAndGap(t, store)
	before := captureLedger(t, store)
	requireFinalityAndGap(t, before)
	trigger := fmt.Sprintf(`CREATE TRIGGER inject_ack_failure BEFORE INSERT ON counter_export_ack WHEN NEW.credential = '%s' BEGIN SELECT RAISE(ABORT, 'injected ack failure'); END`, page[1].CredentialID)
	if _, err := store.db.Exec(trigger); err != nil {
		t.Fatal(err)
	}
	request, response := pageFor(t, page)
	count, err := store.AcknowledgeCounters(context.Background(), request, response)
	if err == nil || count != 0 {
		t.Fatalf("injected ack = %d, %v", count, err)
	}
	if rows := ackRows(t, store); len(rows) != 0 {
		t.Fatalf("partial write survived rollback: %#v", rows)
	}
	if after := captureLedger(t, store); !reflect.DeepEqual(before, after) {
		t.Fatalf("failed ack changed the ledger:\nbefore=%#v\nafter=%#v", before, after)
	}
	if _, err := store.db.Exec(`DROP TRIGGER inject_ack_failure`); err != nil {
		t.Fatal(err)
	}
	if count := acknowledgePage(t, store, page); count != 2 {
		t.Fatalf("retry ack = %d", count)
	}
	if rows := ackRows(t, store); len(rows) != 2 {
		t.Fatalf("retry ack rows = %#v", rows)
	}
}

func TestAcknowledgeCountersNeverDowngradesAcknowledgement(t *testing.T) {
	source := testUUID(9014)
	credential := testUUID(1)
	day := traffic.Date("2026-10-23")
	store := openExportStore(t, source)
	recordSample(t, store, credential, exportDay, 10, 20)
	recordSample(t, store, credential, exportDay, 30, 40)
	current := pendingPage(t, store, 1)
	if len(current) != 1 || current[0].Version != 2 {
		t.Fatalf("current = %#v", current)
	}
	if count := acknowledgePage(t, store, current); count != 1 {
		t.Fatalf("acknowledged = %d", count)
	}
	if _, err := store.db.Exec(`UPDATE usage SET version = 1, upload = 10, download = 20 WHERE credential = ? AND day = ?`, credential, string(day)); err != nil {
		t.Fatal(err)
	}
	stale, err := traffic.MakeCounterSnapshot(source, credential, day, 1, 10, 20)
	if err != nil {
		t.Fatal(err)
	}
	pending := pendingPage(t, store, 1)
	if len(pending) != 1 || pending[0] != stale {
		t.Fatalf("restored pending = %#v", pending)
	}
	request, response := pageFor(t, pending)
	count, err := store.AcknowledgeCounters(context.Background(), request, response)
	if err != nil || count != 0 {
		t.Fatalf("downgrade ack = %d, %v", count, err)
	}
	var version int64
	var digest string
	if err := store.db.QueryRow(`SELECT version, digest FROM counter_export_ack`).Scan(&version, &digest); err != nil {
		t.Fatal(err)
	}
	if version != current[0].Version || digest != current[0].Digest {
		t.Fatalf("ack downgraded to (%d, %s)", version, digest)
	}
}

func TestCounterExportAckTableStoresOnlyCurrentMetadata(t *testing.T) {
	store := openExportStore(t, testUUID(9013))
	columns := queryStrings(t, store, `SELECT name FROM pragma_table_info('counter_export_ack') ORDER BY cid`)
	if want := []string{"credential", "day", "version", "digest"}; !reflect.DeepEqual(columns, want) {
		t.Fatalf("ack columns = %#v, want %#v", columns, want)
	}
	credential := testUUID(1)
	day := exportDay.Format("2006-01-02")
	digest := strings.Repeat("a", 64)
	invalid := []struct {
		name string
		args []any
	}{
		{name: "zero version", args: []any{credential, day, 0, digest}},
		{name: "negative version", args: []any{credential, day, -1, digest}},
		{name: "null version", args: []any{credential, day, nil, digest}},
		{name: "null credential", args: []any{nil, day, 1, digest}},
		{name: "null day", args: []any{credential, nil, 1, digest}},
		{name: "null digest", args: []any{credential, day, 1, nil}},
		{name: "short digest", args: []any{credential, day, 1, strings.Repeat("a", 63)}},
		{name: "uppercase digest", args: []any{credential, day, 1, strings.Repeat("A", 64)}},
		{name: "non-hex digest", args: []any{credential, day, 1, strings.Repeat("g", 64)}},
	}
	for _, test := range invalid {
		t.Run(test.name, func(t *testing.T) {
			if _, err := store.db.Exec(`INSERT INTO counter_export_ack(credential, day, version, digest) VALUES(?, ?, ?, ?)`, test.args...); err == nil {
				t.Fatal("invalid acknowledgement metadata accepted")
			}
		})
	}
	if _, err := store.db.Exec(`INSERT INTO counter_export_ack(credential, day, version, digest) VALUES(?, ?, ?, ?)`, credential, day, 1, digest); err != nil {
		t.Fatal(err)
	}
	requireCount(t, store, `SELECT COUNT(*) FROM counter_export_ack`, 1)
	if _, err := store.db.Exec(`INSERT INTO counter_export_ack(credential, day, version, digest) VALUES(?, ?, ?, ?)`, credential, day, 1, digest); err == nil {
		t.Fatal("duplicate acknowledgement key accepted")
	}
	if _, err := store.db.Exec(`DELETE FROM counter_export_ack`); err != nil {
		t.Fatal(err)
	}
	requireCount(t, store, `SELECT COUNT(*) FROM counter_export_ack`, 0)
}
