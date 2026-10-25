package accounting

import (
	"context"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestAccountingFilesStayPrivateUnderOpenUmask(t *testing.T) {
	previous := syscall.Umask(0)
	defer syscall.Umask(previous)
	path := filepath.Join(t.TempDir(), "traffic.sqlite3")
	store, err := Open(path, "source")
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	for _, suffix := range []string{"", "-wal", "-shm"} {
		info, err := os.Stat(path + suffix)
		if err != nil || info.Mode().Perm() != 0600 {
			t.Fatalf("accounting file is not private: %v", err)
		}
	}
}

func TestCoverageSurvivesReopenWithoutDuplicateUsageOrGaps(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "traffic.sqlite3")
	store, err := Open(path, "source")
	if err != nil {
		t.Fatal(err)
	}
	for _, sample := range []struct {
		invocation string
		count      int64
	}{{"old", 20}, {"old", 20}, {"new", 7}, {"new", 7}} {
		if err := store.Record(ctx, sample.invocation, "credential", 0, sample.count, time.Now()); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.Gap(ctx, "old"); err != nil {
		t.Fatal(err)
	}
	store.Close()
	store, err = Open(path, "source")
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	rows, err := store.Snapshots(ctx)
	if err != nil || len(rows) != 1 || rows[0].Download != 27 || rows[0].Version != 2 {
		t.Fatalf("usage replayed: %v %v", rows, err)
	}
	gaps, err := store.GapCount(ctx)
	if err != nil || gaps != 1 {
		t.Fatalf("coverage replayed: %d %v", gaps, err)
	}
}

func TestFinishedInvocationDoesNotFabricateGapOrPermitMissingCheckpoint(t *testing.T) {
	ctx := context.Background()
	store, err := Open(filepath.Join(t.TempDir(), "traffic.sqlite3"), "source")
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if store.Finish(ctx, "unknown") == nil {
		t.Fatal("missing final checkpoint accepted")
	}
	if err := store.Record(ctx, "old", "credential", 0, 20, time.Now()); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := store.Finish(ctx, "old"); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.Gap(ctx, "old"); err != nil {
		t.Fatal(err)
	}
	if err := store.Record(ctx, "new", "credential", 0, 7, time.Now()); err != nil {
		t.Fatal(err)
	}
	gaps, err := store.GapCount(ctx)
	if err != nil || gaps != 0 {
		t.Fatalf("finished invocation marked uncertain: %d %v", gaps, err)
	}
}
