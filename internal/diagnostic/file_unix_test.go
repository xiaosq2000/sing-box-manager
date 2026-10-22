//go:build !windows

package diagnostic

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestMetadataFIFOAndLinksNeverBlockOpening(t *testing.T) {
	fifo := filepath.Join(t.TempDir(), "metadata")
	if err := syscall.Mkfifo(fifo, 0o600); err != nil {
		t.Fatal(err)
	}
	link := fifo + "-link"
	if err := os.Symlink(fifo, link); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{fifo, link} {
		done := make(chan error, 1)
		go func() { _, err := ReadRegular(path, 1024); done <- err }()
		select {
		case err := <-done:
			if err == nil {
				t.Fatal("accepted special metadata")
			}
		case <-time.After(time.Second):
			t.Fatal("special metadata blocked open")
		}
	}
	// This is the replacement-race case: the platform open itself must be
	// nonblocking even if a FIFO appears after the initial regular-file check.
	done := make(chan error, 1)
	go func() {
		file, err := openRegular(fifo)
		if err == nil {
			file.Close()
		}
		done <- err
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("FIFO replacement blocked platform open")
	}
}
