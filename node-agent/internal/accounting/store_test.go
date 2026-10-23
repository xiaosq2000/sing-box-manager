package accounting

import (
	"context"
	"path/filepath"
	"testing"
	"time"
)

func TestCheckpointAndBucketSurviveRetryAndRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "traffic.sqlite3")
	ctx := context.Background()
	now := time.Date(2026, 10, 23, 23, 59, 0, 0, time.UTC)
	store, err := Open(path, "source-one")
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range []int64{100, 100, 150} {
		if err := store.Record(ctx, "engine-one", "credential-one", value, value*2, now); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.Record(ctx, "engine-one", "credential-one", 149, 300, now); err == nil {
		t.Fatal("accepted counter regression")
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	store, err = Open(path, "source-one")
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Record(ctx, "engine-one", "credential-one", 170, 340, now.Add(2*time.Minute)); err != nil {
		t.Fatal(err)
	}
	if err := store.Record(ctx, "engine-two", "credential-one", 7, 14, now.Add(2*time.Minute)); err != nil {
		t.Fatal(err)
	}
	rows, err := store.Snapshots(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 || rows[0].Upload != 150 || rows[0].Download != 300 || rows[0].Version != 2 || rows[1].Upload != 27 || rows[1].Download != 54 {
		t.Fatalf("unexpected buckets: %+v", rows)
	}
	for _, row := range rows {
		if err := row.Validate(); err != nil {
			t.Fatal(err)
		}
	}
}

func TestSourceCannotBeReusedWithAnotherIdentity(t *testing.T) {
	path := filepath.Join(t.TempDir(), "traffic.sqlite3")
	store, err := Open(path, "one")
	if err != nil {
		t.Fatal(err)
	}
	store.Close()
	if _, err := Open(path, "two"); err == nil {
		t.Fatal("source mismatch accepted")
	}
}

func TestZeroAndInvalidSamplesDoNotCreateUsage(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "traffic.sqlite3"), "source")
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	ctx := context.Background()
	if err := store.Record(ctx, "engine", "credential", 0, 0, time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := store.Record(ctx, "engine", "credential", 1, -1, time.Now()); err == nil {
		t.Fatal("negative sample accepted")
	}
	rows, err := store.Snapshots(ctx)
	if err != nil || len(rows) != 0 {
		t.Fatalf("unexpected usage %v: %v", rows, err)
	}
}
