package winsettings

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestNativeWebRTCMutexChild(t *testing.T) {
	name := os.Getenv("SBC_TEST_WEBRTC_MUTEX")
	if name == "" {
		return
	}
	fmt.Println("waiting")
	unlock, err := lockWebRTCMutex(name)
	if err != nil {
		t.Fatal(err)
	}
	if os.Getenv("SBC_TEST_WEBRTC_ABANDON") == "1" {
		fmt.Println("acquired")
		if _, err := bufio.NewReader(os.Stdin).ReadByte(); err != nil {
			t.Fatal(err)
		}
		os.Exit(0) // Exercise recovery when an owning process exits without release.
	}
	unlock()
}

// These tests use a disposable mutex name and never change browser settings.
func TestNativeWebRTCMutexSerializesProcesses(t *testing.T) {
	for _, abandon := range []bool{false, true} {
		t.Run(fmt.Sprintf("abandon=%v", abandon), func(t *testing.T) {
			name := `Local\sbc-tests.WebRTC.Operation.` + filepath.Base(t.TempDir()) + fmt.Sprint(os.Getpid(), time.Now().UnixNano())
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestNativeWebRTCMutexChild$")
			cmd.Env = append(os.Environ(), "SBC_TEST_WEBRTC_MUTEX="+name)
			if abandon {
				cmd.Env = append(cmd.Env, "SBC_TEST_WEBRTC_ABANDON=1")
			}
			stdout, err := cmd.StdoutPipe()
			if err != nil {
				t.Fatal(err)
			}
			stdin, err := cmd.StdinPipe()
			if err != nil {
				t.Fatal(err)
			}
			defer stdin.Close()
			var stderr bytes.Buffer
			cmd.Stderr = &stderr
			var release func()
			if !abandon {
				unlock, err := lockWebRTCMutex(name)
				if err != nil {
					t.Fatal(err)
				}
				release = sync.OnceFunc(unlock)
				defer release()
			}
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			scanner := bufio.NewScanner(stdout)
			if !scanner.Scan() || scanner.Text() != "waiting" {
				t.Fatal("child did not attempt acquisition")
			}
			if abandon {
				if !scanner.Scan() || scanner.Text() != "acquired" {
					t.Fatal("child did not acquire the mutex")
				}
			}
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			if abandon {
				acquired := make(chan error, 1)
				go func() {
					unlock, err := lockWebRTCMutex(name)
					if err == nil {
						unlock()
					}
					acquired <- err
				}()
				select {
				case err := <-acquired:
					t.Fatalf("acquired a mutex still owned by the child: %v", err)
				case <-time.After(100 * time.Millisecond):
				}
				if _, err := stdin.Write([]byte{1}); err != nil {
					t.Fatal(err)
				}
				awaitWebRTCOperation(t, acquired)
			} else {
				select {
				case err := <-done:
					t.Fatalf("child bypassed the held mutex: %v, %s", err, &stderr)
				case <-time.After(100 * time.Millisecond):
				}
				release()
			}
			awaitWebRTCOperation(t, done)
			// A fresh acquisition must work after normal release or abandonment.
			unlock, err := lockWebRTCMutex(name)
			if err != nil {
				t.Fatal(err)
			}
			unlock()
		})
	}
}
