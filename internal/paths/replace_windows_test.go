package paths

import (
	"bufio"
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestReplaceMappedExecutableAndRestoreWhileBothVersionsRun(t *testing.T) {
	if os.Getenv("SBC_MAPPED_EXECUTABLE_TEST") == "1" {
		fmt.Println("ready")
		var b [1]byte
		os.Stdin.Read(b[:])
		return
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	original, err := os.ReadFile(self)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "sbc.exe")
	if err = WriteFile(path, original, 0o755); err != nil {
		t.Fatal(err)
	}
	start := func() {
		child := exec.Command(path, "-test.run=^TestReplaceMappedExecutableAndRestoreWhileBothVersionsRun$")
		child.Env = append(os.Environ(), "SBC_MAPPED_EXECUTABLE_TEST=1")
		input, err := child.StdinPipe()
		if err != nil {
			t.Fatal(err)
		}
		output, err := child.StdoutPipe()
		if err != nil {
			t.Fatal(err)
		}
		if err = child.Start(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			input.Close()
			if err := child.Wait(); err != nil {
				t.Error(err)
			}
		})
		if line, err := bufio.NewReader(output).ReadString('\n'); err != nil || line != "ready\n" {
			t.Fatalf("child: %q %v", line, err)
		}
	}
	start()
	candidate := append(bytes.Clone(original), []byte("candidate fixture")...)
	if err = WriteFile(path, candidate, 0o755); err != nil {
		t.Fatal(err)
	}
	start()
	if err = WriteFile(path, original, 0o755); err != nil {
		t.Fatalf("rollback with two mapped versions: %v", err)
	}
	if data, err := os.ReadFile(path); err != nil || !bytes.Equal(data, original) {
		t.Fatalf("rollback: %v", err)
	}
	retired, _ := filepath.Glob(path + ".old-*")
	if len(retired) != 2 {
		t.Fatalf("retired executables: %v", retired)
	}
}
