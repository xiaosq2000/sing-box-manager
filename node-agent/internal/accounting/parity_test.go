package accounting

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/contract"
)

// The fixture is shared with tests/test_node_g2_counter_parity.py.
const parityFixtureName = "node-g2-counter-parity.json"

type parityFixture struct {
	Schema    int                          `json:"schema"`
	SourceID  string                       `json:"source_id"`
	Protocols []string                     `json:"protocols"`
	Accounts  map[string]map[string]string `json:"accounts"`
	Cases     []parityCase                 `json:"cases"`
}

type parityCase struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	Samples     []paritySample `json:"samples"`
	Expected    struct {
		Python parityExpectation `json:"python"`
		Go     parityExpectation `json:"go"`
	} `json:"expected"`
	Divergence string `json:"divergence"`
}

type paritySample struct {
	At         string `json:"at"`
	Invocation string `json:"invocation"`
	// UptimeSeconds is adapter input for the Python oracle. The Go target
	// identifies a process by Invocation instead.
	UptimeSeconds int64                          `json:"uptime_seconds"`
	Counters      map[string]map[string][2]int64 `json:"counters"`
	ReopenBefore  bool                           `json:"reopen_before"`
	FinishAfter   bool                           `json:"finish_after"`
}

type parityExpectation struct {
	Ledger          map[string][2]int64 `json:"ledger"`
	Versions        map[string]int64    `json:"versions"`
	RejectedSamples []int               `json:"rejected_samples"`
	GapCount        int64               `json:"gap_count"`
}

type parityCredential struct {
	account  string
	protocol string
}

type parityState struct {
	snapshots map[string]contract.Snapshot
	ledger    map[string][2]int64
	versions  map[string]int64
}

func TestSharedCounterParityCases(t *testing.T) {
	fixture := loadParityFixture(t)
	fixture.validate(t)
	index := fixture.credentialIndex(t)
	for _, testCase := range fixture.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			runParityCase(t, fixture, index, testCase)
		})
	}
}

func runParityCase(
	t *testing.T,
	fixture parityFixture,
	index map[string]parityCredential,
	testCase parityCase,
) {
	t.Helper()
	ctx := context.Background()
	databasePath := filepath.Join(t.TempDir(), "traffic.sqlite3")
	store, err := Open(databasePath, fixture.SourceID)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = store.Close() }()

	rejected := map[int]bool{}
	for _, sampleIndex := range testCase.Expected.Go.RejectedSamples {
		rejected[sampleIndex] = true
	}
	var previous parityState
	for sampleIndex, sample := range testCase.Samples {
		if sample.ReopenBefore {
			if err := store.Close(); err != nil {
				t.Fatal(err)
			}
			store, err = Open(databasePath, fixture.SourceID)
			if err != nil {
				t.Fatal(err)
			}
		}
		at, err := time.Parse(time.RFC3339, sample.At)
		if err != nil {
			t.Fatalf("sample %d time: %v", sampleIndex, err)
		}
		recordErrors := recordParitySample(ctx, store, fixture, sample, at)
		if rejected[sampleIndex] {
			if len(recordErrors) != len(fixture.Accounts)*len(fixture.Protocols) {
				t.Fatalf("sample %d rejected %d of %d counters", sampleIndex, len(recordErrors), len(fixture.Accounts)*len(fixture.Protocols))
			}
			for _, recordErr := range recordErrors {
				if !strings.Contains(recordErr.Error(), "counter regression within one invocation") {
					t.Fatalf("sample %d rejected with unexpected error: %v", sampleIndex, recordErr)
				}
			}
		} else if len(recordErrors) != 0 {
			t.Fatalf("sample %d: %v", sampleIndex, recordErrors[0])
		}
		if sample.FinishAfter {
			if err := store.Finish(ctx, sample.Invocation); err != nil {
				t.Fatal(err)
			}
		}
		snapshots, err := store.Snapshots(ctx)
		if err != nil {
			t.Fatal(err)
		}
		current := readParityState(t, index, snapshots)
		assertParitySnapshotContract(t, previous, current)
		previous = current
	}

	if !reflect.DeepEqual(previous.ledger, testCase.Expected.Go.Ledger) {
		t.Fatalf("ledger mismatch\n got: %v\nwant: %v", previous.ledger, testCase.Expected.Go.Ledger)
	}
	if !reflect.DeepEqual(previous.versions, testCase.Expected.Go.Versions) {
		t.Fatalf("version mismatch\n got: %v\nwant: %v", previous.versions, testCase.Expected.Go.Versions)
	}
	gaps, err := store.GapCount(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if gaps != testCase.Expected.Go.GapCount {
		t.Fatalf("gap count = %d, want %d", gaps, testCase.Expected.Go.GapCount)
	}
	pythonLedger := testCase.Expected.Python.Ledger
	if (testCase.Divergence == "") != reflect.DeepEqual(pythonLedger, testCase.Expected.Go.Ledger) {
		t.Fatalf("case %q must document a divergence exactly when the Python and Go ledgers differ", testCase.Name)
	}
}

func recordParitySample(
	ctx context.Context,
	store *Store,
	fixture parityFixture,
	sample paritySample,
	at time.Time,
) []error {
	var recordErrors []error
	for account, protocols := range fixture.Accounts {
		for _, protocol := range fixture.Protocols {
			values := sample.Counters[account][protocol]
			if err := store.Record(ctx, sample.Invocation, protocols[protocol], values[0], values[1], at); err != nil {
				recordErrors = append(recordErrors, fmt.Errorf("%s/%s: %w", account, protocol, err))
			}
		}
	}
	return recordErrors
}

func readParityState(
	t *testing.T,
	index map[string]parityCredential,
	snapshots []contract.Snapshot,
) parityState {
	t.Helper()
	state := parityState{
		snapshots: make(map[string]contract.Snapshot, len(snapshots)),
		ledger:    make(map[string][2]int64, len(snapshots)),
		versions:  make(map[string]int64, len(snapshots)),
	}
	for _, snapshot := range snapshots {
		credential, ok := index[snapshot.CredentialID]
		if !ok {
			t.Fatalf("unknown credential in snapshot: %q", snapshot.CredentialID)
		}
		key := snapshot.UsageDate + "|" + credential.account + "|" + credential.protocol
		if _, exists := state.snapshots[key]; exists {
			t.Fatalf("duplicate snapshot key %q", key)
		}
		state.snapshots[key] = snapshot
		state.ledger[key] = [2]int64{snapshot.Upload, snapshot.Download}
		state.versions[key] = snapshot.Version
	}
	return state
}

func assertParitySnapshotContract(t *testing.T, previous, current parityState) {
	t.Helper()
	for key, snapshot := range current.snapshots {
		if err := snapshot.Validate(); err != nil {
			t.Fatalf("snapshot %q invalid: %v", key, err)
		}
		old, exists := previous.snapshots[key]
		if !exists {
			continue
		}
		samePayload := old.SourceID == snapshot.SourceID &&
			old.CredentialID == snapshot.CredentialID &&
			old.UsageDate == snapshot.UsageDate &&
			old.Version == snapshot.Version &&
			old.Upload == snapshot.Upload &&
			old.Download == snapshot.Download
		if samePayload != (old.Digest == snapshot.Digest) {
			t.Fatalf("digest contract mismatch for %q: same payload %t, same digest %t", key, samePayload, old.Digest == snapshot.Digest)
		}
	}
	for key := range previous.snapshots {
		if _, exists := current.snapshots[key]; !exists {
			t.Fatalf("snapshot %q disappeared", key)
		}
	}
}

func (fixture parityFixture) credentialIndex(t *testing.T) map[string]parityCredential {
	t.Helper()
	index := make(map[string]parityCredential, len(fixture.Accounts)*len(fixture.Protocols))
	for account, protocols := range fixture.Accounts {
		for protocol, credential := range protocols {
			if credential == "" {
				t.Fatalf("empty credential for %s/%s", account, protocol)
			}
			if _, exists := index[credential]; exists {
				t.Fatalf("duplicate credential %q", credential)
			}
			index[credential] = parityCredential{account: account, protocol: protocol}
		}
	}
	return index
}

func (fixture parityFixture) validate(t *testing.T) {
	t.Helper()
	if fixture.Schema != 1 {
		t.Fatalf("fixture schema = %d, want 1", fixture.Schema)
	}
	if fixture.SourceID == "" {
		t.Fatal("fixture source ID is empty")
	}
	if len(fixture.Accounts) != 2 || len(fixture.Protocols) != 4 {
		t.Fatalf("fixture matrix = %d accounts x %d protocols, want 2 x 4", len(fixture.Accounts), len(fixture.Protocols))
	}
	for account, protocols := range fixture.Accounts {
		if len(protocols) != len(fixture.Protocols) {
			t.Fatalf("account %s does not cover every protocol", account)
		}
		for _, protocol := range fixture.Protocols {
			if protocols[protocol] == "" {
				t.Fatalf("account %s has no credential for %s", account, protocol)
			}
		}
	}
	for _, testCase := range fixture.Cases {
		if testCase.Name == "" || len(testCase.Samples) == 0 {
			t.Fatalf("case has no name or samples: %+v", testCase)
		}
		for sampleIndex, sample := range testCase.Samples {
			if _, err := time.Parse(time.RFC3339, sample.At); err != nil {
				t.Fatalf("case %s sample %d time: %v", testCase.Name, sampleIndex, err)
			}
			if sample.Invocation == "" {
				t.Fatalf("case %s sample %d has no invocation", testCase.Name, sampleIndex)
			}
			if len(sample.Counters) != len(fixture.Accounts) {
				t.Fatalf("case %s sample %d does not cover every account", testCase.Name, sampleIndex)
			}
			for account, protocols := range sample.Counters {
				if len(protocols) != len(fixture.Protocols) {
					t.Fatalf("case %s sample %d account %s does not cover every protocol", testCase.Name, sampleIndex, account)
				}
			}
		}
		for _, sampleIndex := range testCase.Expected.Go.RejectedSamples {
			if sampleIndex < 0 || sampleIndex >= len(testCase.Samples) {
				t.Fatalf("case %s rejected sample index %d is out of range", testCase.Name, sampleIndex)
			}
		}
		if len(testCase.Expected.Go.Ledger) == 0 || len(testCase.Expected.Python.Ledger) == 0 {
			t.Fatalf("case %s has no expected ledger", testCase.Name)
		}
	}
}

func loadParityFixture(t *testing.T) parityFixture {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("locate parity fixture source")
	}
	path := filepath.Join(filepath.Dir(source), "..", "..", "..", "tests", "fixtures", parityFixtureName)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fixture parityFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	return fixture
}
