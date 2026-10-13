//go:build e2e

package e2e

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestWebRTCBrowserStartupRetry(t *testing.T) {
	startup := &browserStartupTimeout{fmt.Errorf("fixture startup timeout")}
	failure := fmt.Errorf("fixture probe failure")
	complete := browserWebRTCResult{Started: true, Complete: true}
	for _, test := range []struct {
		name     string
		errors   []error
		result   browserWebRTCResult
		attempts int
	}{
		{"startup then success", []error{startup, nil}, complete, 2},
		{"startup twice", []error{startup, startup}, browserWebRTCResult{}, 2},
		{"startup then probe failure", []error{startup, failure}, browserWebRTCResult{}, 2},
		{"page loaded then timeout", []error{failure}, browserWebRTCResult{}, 1},
		{"process exited", []error{failure}, browserWebRTCResult{}, 1},
		{"page loaded during shutdown", []error{fmt.Errorf("%v (page requested during shutdown)", startup)}, browserWebRTCResult{}, 1},
		{"cleanup failed", []error{fmt.Errorf("fixture cleanup failure: %w", startup)}, browserWebRTCResult{}, 1},
		{"ICE failure", []error{nil}, browserWebRTCResult{Started: true, Error: "fixture ICE failure"}, 1},
		{"incomplete gathering", []error{nil}, browserWebRTCResult{Started: true}, 1},
		{"success", []error{nil}, complete, 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			attempts, logs := 0, 0
			result, err := retryBrowserStartup(func() (browserWebRTCResult, error) {
				attempts++
				if attempts > len(test.errors) {
					t.Fatal("unexpected browser retry")
				}
				return test.result, test.errors[attempts-1]
			}, func(format string, args ...any) {
				logs++
				if !strings.Contains(fmt.Sprintf(format, args...), startup.Error()) {
					t.Fatal("startup retry lost the first attempt's diagnostic")
				}
			})
			if attempts != test.attempts || logs != test.attempts-1 || err != test.errors[attempts-1] || result.Error != test.result.Error || result.Complete != test.result.Complete {
				t.Fatalf("attempts=%d logs=%d result=%+v err=%v", attempts, logs, result, err)
			}
		})
	}
}

// These subprocesses stand in for a browser. They use loopback HTTP only and
// never install sbc, load a browser profile or change services and settings.
func TestWebRTCBrowserStartupTimeout(t *testing.T) {
	for _, mode := range []string{"before-page", "after-page", "result", "exit"} {
		t.Run(mode, func(t *testing.T) {
			var command *exec.Cmd
			factory := func(_, _, page string) *exec.Cmd {
				command = exec.Command(os.Args[0], "-test.run=^TestWebRTCBrowserStartupProcess$", "--", page, mode)
				command.Env = append(os.Environ(), "SBC_WEBRTC_BROWSER_FIXTURE=1")
				prepareBrowserProcess(command)
				return command
			}
			result, err := browserWebRTCAttempt(t, "fixture-browser", "stun:127.0.0.1:9", factory, 3*time.Second)
			_, retry := err.(*browserStartupTimeout)
			if retry != (mode == "before-page") {
				t.Fatalf("mode=%s retry=%t err=%v", mode, retry, err)
			}
			if command.ProcessState == nil {
				t.Fatal("browser attempt returned before its process stopped")
			}
			if mode == "result" {
				if err != nil || !result.Started || !result.Complete {
					t.Fatalf("fixture result=%+v err=%v", result, err)
				}
			} else if err == nil || !strings.Contains(err.Error(), "fixture browser output") {
				t.Fatalf("browser failure lost its diagnostic: %v", err)
			}
		})
	}
}

func TestWebRTCBrowserStartupProcess(t *testing.T) {
	if os.Getenv("SBC_WEBRTC_BROWSER_FIXTURE") != "1" {
		return
	}
	page, mode := os.Args[len(os.Args)-2], os.Args[len(os.Args)-1]
	fmt.Printf("fixture browser output (mode=%s)\n", mode)
	if mode == "before-page" || mode == "after-page" || mode == "result" {
		target := page
		if mode == "before-page" {
			// An unrelated root request must not mark the probe page as loaded.
			address, err := url.Parse(page)
			if err != nil {
				t.Fatal(err)
			}
			address.Path = "/"
			target = address.String()
		}
		response, err := http.Get(target)
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
	}
	if mode == "result" {
		response, err := http.Post(page+"/result", "application/json", strings.NewReader(`{"started":true,"complete":true}`))
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
	}
	if mode == "exit" {
		return
	}
	time.Sleep(time.Hour)
}
