//go:build windows

package cli

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestWindowsEngineJobHelper(t *testing.T) {
	switch os.Getenv("SBC_JOB_HELPER") {
	case "engine":
		fmt.Println("engine-ready")
		for {
			time.Sleep(time.Second)
		}
	case "wrapper":
		layout := paths.Layout{Config: os.Getenv("SBC_JOB_CONFIG")}
		engine, err := maintenance.LockEngine(layout)
		if err != nil {
			t.Fatal(err)
		}
		defer engine.Close()
		os.Setenv("SBC_JOB_HELPER", "engine")
		if err := execProcess(os.Args[0], []string{os.Args[0], "-test.run=^TestWindowsEngineJobHelper$"}, os.Stdout, os.Getenv("SBC_JOB_PID")); err != nil {
			t.Fatal(err)
		}
	}
}

func TestWindowsEngineJobStopsChildWhenWrapperDies(t *testing.T) {
	root := t.TempDir()
	layout := paths.Layout{Config: filepath.Join(root, "config")}
	pidFile := filepath.Join(root, "child.pid")
	cmd := exec.Command(os.Args[0], "-test.run=^TestWindowsEngineJobHelper$")
	cmd.Env = append(os.Environ(), "SBC_JOB_HELPER=wrapper", "SBC_JOB_CONFIG="+layout.Config, "SBC_JOB_PID="+pidFile)
	output, _ := cmd.StdoutPipe()
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cmd.Process.Kill(); cmd.Wait() })
	line, err := bufio.NewReader(output).ReadString('\n')
	if err != nil || line != "engine-ready\n" {
		t.Fatalf("job helper: %q %v", line, err)
	}
	var pid int
	deadline := time.Now().Add(5 * time.Second)
	for pid == 0 && time.Now().Before(deadline) {
		data, _ := os.ReadFile(pidFile)
		pid, _ = strconv.Atoi(strings.TrimSpace(string(data)))
		time.Sleep(time.Millisecond)
	}
	if pid == 0 {
		t.Fatal("child PID missing")
	}
	child, err := os.FindProcess(pid)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { child.Kill(); child.Release() })
	if busy, err := maintenance.EngineBusy(layout); err != nil || !busy {
		t.Fatalf("wrapper lost engine lease: %t %v", busy, err)
	}
	cmd.Process.Kill()
	cmd.Wait()
	exited := make(chan error, 1)
	go func() { _, err := child.Wait(); exited <- err }()
	select {
	case err := <-exited:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("wrapper left an orphan engine")
	}
	if busy, err := maintenance.EngineBusy(layout); err != nil || busy {
		t.Fatalf("exit retained engine lease: %t %v", busy, err)
	}
}
