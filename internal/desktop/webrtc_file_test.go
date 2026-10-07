package desktop

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestWebRTCRegularFileValidation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "settings")
	const content = "fixture settings\n"
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	info, err := regularFileInfo(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() != int64(len(content)) {
		t.Fatalf("regular file metadata: %v, %v", info, err)
	}
	data, err := privateFile(path)
	if err != nil || string(data) != content {
		t.Fatalf("regular file contents: %q, %v", data, err)
	}

	for _, kind := range []string{"missing", "directory", "symlink", "dangling symlink"} {
		t.Run(kind, func(t *testing.T) {
			candidate := filepath.Join(t.TempDir(), "settings")
			switch kind {
			case "directory":
				if err := os.Mkdir(candidate, 0700); err != nil {
					t.Fatal(err)
				}
			case "symlink", "dangling symlink":
				target := path
				if kind == "dangling symlink" {
					target = filepath.Join(dir, "missing")
				}
				if err := os.Symlink(target, candidate); err != nil {
					t.Skip("symbolic links unavailable:", err)
				}
			}
			info, statErr := regularFileInfo(candidate)
			data, readErr := privateFile(candidate)
			if info != nil || data != nil || statErr == nil || readErr == nil {
				t.Fatalf("invalid file accepted: %v, %q, %v, %v", info, data, statErr, readErr)
			}
			for _, err := range []error{statErr, readErr} {
				if kind == "missing" {
					if !os.IsNotExist(err) {
						t.Errorf("missing file error changed: %v", err)
					}
				} else if !strings.Contains(err.Error(), "not a regular file") {
					t.Errorf("non-regular file error changed: %v", err)
				}
			}
		})
	}
}
