//go:build !windows

package maintenance

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestEngineLeaseExecHelper(t *testing.T) {
	mode := os.Getenv("SBC_ENGINE_LEASE_HELPER")
	if mode == "" {
		return
	}
	layout := paths.Layout{Config: os.Getenv("SBC_ENGINE_LEASE_CONFIG")}
	if mode == "before" {
		unlock, err := Lock(layout)
		if err != nil {
			t.Fatal(err)
		}
		defer unlock()
		permission, err := Permission(layout, true)
		if err != nil {
			t.Fatal(err)
		}
		defer permission.Close()
		engine, err := LockEngine(layout)
		if err != nil {
			t.Fatal(err)
		}
		defer engine.Close()
		if err = engine.KeepAcrossExec(); err != nil {
			t.Fatal(err)
		}
		os.Setenv("SBC_ENGINE_LEASE_HELPER", "after")
		if err = syscall.Exec(os.Args[0], []string{os.Args[0], "-test.run=^TestEngineLeaseExecHelper$"}, os.Environ()); err != nil {
			t.Fatal(err)
		}
	}
	if busy, err := EngineBusy(layout); err != nil || !busy {
		t.Fatalf("exec lost engine lease: %t %v", busy, err)
	}
	unlock, err := Lock(layout)
	if err != nil {
		t.Fatalf("exec inherited operation lock: %v", err)
	}
	unlock()
	if granted, err := StartGranted(layout); err != nil || granted {
		t.Fatalf("exec inherited permission: %t %v", granted, err)
	}
	fmt.Println("engine-ready")
	var input [1]byte
	os.Stdin.Read(input[:])
	os.Exit(0)
}

func TestEngineLeaseSurvivesExecAndReleasesOnExit(t *testing.T) {
	for _, kill := range []bool{false, true} {
		t.Run(fmt.Sprint(kill), func(t *testing.T) {
			layout := paths.Layout{Config: filepath.Join(t.TempDir(), "config")}
			cmd := exec.Command(os.Args[0], "-test.run=^TestEngineLeaseExecHelper$")
			cmd.Env = append(os.Environ(), "SBC_ENGINE_LEASE_HELPER=before", "SBC_ENGINE_LEASE_CONFIG="+layout.Config)
			input, _ := cmd.StdinPipe()
			output, _ := cmd.StdoutPipe()
			cmd.Stderr = os.Stderr
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { cmd.Process.Kill(); cmd.Wait() })
			line, err := bufio.NewReader(output).ReadString('\n')
			if err != nil || line != "engine-ready\n" {
				t.Fatalf("helper: %q %v", line, err)
			}
			if busy, err := EngineBusy(layout); err != nil || !busy {
				t.Fatalf("not held across exec: %t %v", busy, err)
			}
			if kill {
				cmd.Process.Kill()
			} else {
				input.Close()
			}
			err = cmd.Wait()
			if !kill && err != nil {
				t.Fatal(err)
			}
			if busy, err := EngineBusy(layout); err != nil || busy {
				t.Fatalf("exit retained lease: %t %v", busy, err)
			}
		})
	}
}
