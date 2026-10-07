//go:build e2e

package e2e

import (
	"slices"
	"strings"
	"sync"
	"testing"
)

// These fixtures neither launch a browser nor change host settings.
func TestWebRTCBrowserCommand(t *testing.T) {
	const pageURL = "http://127.0.0.1:12345"
	command := browserWebRTCCommand("fixture-browser", "fixture-profile", pageURL)
	if command.Args[0] != "fixture-browser" || command.Args[len(command.Args)-1] != pageURL {
		t.Fatalf("probe must launch the requested browser and loopback page: %v", command.Args)
	}
	for _, required := range []string{
		"--headless=new", "--user-data-dir=fixture-profile", "--no-first-run",
		"--disable-features=msEdgeFirstRunExperience",
		"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
	} {
		if !slices.Contains(command.Args, required) {
			t.Errorf("probe is missing startup argument %s", required)
		}
	}
	for _, argument := range command.Args[1 : len(command.Args)-1] {
		if argument == "--no-sandbox" || strings.Contains(strings.ToLower(argument), "webrtc") || strings.HasPrefix(argument, "--proxy-") {
			t.Errorf("probe must not disable sandboxing or override WebRTC/proxy policy: %s", argument)
		}
	}
}

func TestWebRTCBrowserOutput(t *testing.T) {
	var output browserOutput
	var writers sync.WaitGroup
	const line = "fixture browser log\n"
	for range 8 {
		writers.Go(func() {
			for range 100 {
				output.Write([]byte(line))
				// A timeout can read the log while the browser is still writing.
				if snapshot := output.String(); len(snapshot)%len(line) != 0 {
					t.Errorf("log snapshot contains a partial write: %q", snapshot)
				}
			}
		})
	}
	writers.Wait()
	if got, want := output.String(), strings.Repeat(line, 800); got != want {
		t.Fatalf("browser log lost output: got %d bytes, want %d", len(got), len(want))
	}
}
