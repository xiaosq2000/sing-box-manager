//go:build e2e

package e2e

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

// checkWindowsWebRTC runs only inside the disposable Windows lifecycle.
// Both browsers must enforce policies repaired by sbc, without policy flags.
func checkWindowsWebRTC(t *testing.T, sbc string) {
	t.Helper()
	if runtime.GOOS != "windows" || os.Getenv("SBC_E2E") != "1" {
		t.Fatal("browser WebRTC checks require the disposable Windows lifecycle")
	}
	for _, browser := range []struct{ name, executable, directory, key, policy string }{
		{"Chrome", "chrome.exe", "Google/Chrome/Application", winsettings.ChromePolicyKey, winsettings.WebRtcPolicyName},
		{"Edge", "msedge.exe", "Microsoft/Edge/Application", winsettings.EdgePolicyKey, winsettings.EdgeWebRtcPolicyName},
	} {
		t.Run(browser.name, func(t *testing.T) {
			executable := windowsBrowser(t, browser.executable, browser.directory)
			registry := winsettings.PowerShell{}
			// The proxy stays on while its browser policy is missing.
			if err := registry.Apply(browser.key, map[string]winsettings.Value{browser.policy: {}}, ""); err != nil {
				t.Fatal(err)
			}
			values, err := registry.Read(browser.key, []string{browser.policy})
			if err != nil || len(values) != 0 {
				t.Fatalf("could not remove browser policy for the positive control: %v", err)
			}
			expect(t, run(t, nil, sbc, "desktop"), "on (Windows)")
			checkBrowserSTUN(t, executable, false)
			run(t, nil, sbc, "on")
			expect(t, run(t, nil, sbc, "desktop"), "on (Windows)")
			checkWindowsBrowserPolicies(t, true)
			// A fresh browser also satisfies Edge's restart requirement.
			checkBrowserSTUN(t, executable, true)
		})
	}
}

func windowsBrowser(t *testing.T, executable, directory string) string {
	t.Helper()
	if path, err := exec.LookPath(executable); err == nil {
		return path
	}
	for _, variable := range []string{"ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"} {
		if root := os.Getenv(variable); root != "" {
			path := filepath.Join(root, filepath.FromSlash(directory), executable)
			if info, err := os.Stat(path); err == nil && !info.IsDir() {
				return path
			}
		}
	}
	t.Fatalf("the disposable Windows runner needs %s for the WebRTC regression check", executable)
	return ""
}

const stunCookie = uint32(0x2112a442)
const stunFixtureIP = "203.0.113.42"

// The TEST-NET mapped address makes a server-reflexive candidate distinguishable
// from host candidates, even when the browser and STUN fixture share a machine.
func stunBindingResponse(request []byte, port int) []byte {
	if len(request) < 20 || binary.BigEndian.Uint16(request[:2]) != 0x0001 ||
		binary.BigEndian.Uint32(request[4:8]) != stunCookie ||
		int(binary.BigEndian.Uint16(request[2:4])) != len(request)-20 || (len(request)-20)%4 != 0 {
		return nil
	}
	response := make([]byte, 32)
	binary.BigEndian.PutUint16(response[:2], 0x0101)
	binary.BigEndian.PutUint16(response[2:4], 12)
	copy(response[4:20], request[4:20])
	binary.BigEndian.PutUint16(response[20:22], 0x0020) // XOR-MAPPED-ADDRESS
	binary.BigEndian.PutUint16(response[22:24], 8)
	response[25] = 0x01 // IPv4
	binary.BigEndian.PutUint16(response[26:28], uint16(port)^uint16(stunCookie>>16))
	binary.BigEndian.PutUint32(response[28:32], binary.BigEndian.Uint32(net.ParseIP(stunFixtureIP).To4())^stunCookie)
	return response
}

func startWebRTCSTUN(t *testing.T) (*net.UDPConn, *atomic.Int64) {
	t.Helper()
	connection, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	count := new(atomic.Int64)
	done := make(chan struct{})
	go func() {
		defer close(done)
		buffer := make([]byte, 2048)
		for {
			n, source, err := connection.ReadFromUDP(buffer)
			if err != nil {
				return
			}
			if response := stunBindingResponse(buffer[:n], source.Port); response != nil {
				count.Add(1)
				connection.WriteToUDP(response, source)
			}
		}
	}()
	t.Cleanup(func() {
		connection.Close()
		<-done
	})
	return connection, count
}

func checkBrowserSTUN(t *testing.T, browser string, protected bool) {
	t.Helper()
	// Each browser gets its own socket, page and fresh profile. Late control
	// packets cannot contaminate the protected run's count.
	stun, count := startWebRTCSTUN(t)
	result := runBrowserWebRTC(t, browser, "stun:"+stun.LocalAddr().String())
	if result.Error != "" || !result.Started || !result.Complete {
		t.Fatalf("Browser did not complete the WebRTC probe: %+v", result)
	}
	reflexive := false
	for _, candidate := range result.Candidates {
		if strings.Contains(candidate, " "+stunFixtureIP+" ") && strings.Contains(candidate, " typ srflx ") {
			reflexive = true
		}
	}
	if protected {
		if count.Load() != 0 || reflexive {
			t.Fatalf("protected browser sent direct STUN: %d binding requests, fixture candidate=%t", count.Load(), reflexive)
		}
	} else if count.Load() == 0 || !reflexive {
		t.Fatalf("unprotected browser control did not prove direct STUN: %d binding requests, fixture candidate=%t", count.Load(), reflexive)
	}
}

type browserWebRTCResult struct {
	Started    bool     `json:"started"`
	Complete   bool     `json:"complete"`
	Candidates []string `json:"candidates"`
	Error      string   `json:"error"`
}

func runBrowserWebRTC(t *testing.T, browser, stunURL string) browserWebRTCResult {
	t.Helper()
	page := fmt.Sprintf(`<!doctype html><meta charset="utf-8"><script>
(async () => {
  const result = {started: false, complete: false, candidates: [], error: ""};
  let peer, timer;
  try {
    peer = new RTCPeerConnection({iceServers: [{urls: %s}]});
    peer.onicecandidate = event => {
      if (event.candidate) result.candidates.push(event.candidate.candidate);
    };
    const gathered = new Promise(resolve => {
      peer.onicegatheringstatechange = () => {
        if (peer.iceGatheringState === "complete") resolve();
      };
    });
    peer.createDataChannel("loopback-stun-probe");
    await peer.setLocalDescription(await peer.createOffer());
    result.started = true;
    await Promise.race([gathered, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("ICE gathering timed out")), 45000);
    })]);
    result.complete = peer.iceGatheringState === "complete";
  } catch (error) {
    result.error = String(error);
  } finally {
    clearTimeout(timer);
    if (peer) peer.close();
  }
  await fetch("/result", {method: "POST", body: JSON.stringify(result)});
})();
</script>`, strconv.Quote(stunURL))
	results := make(chan browserWebRTCResult, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Header().Set("Cache-Control", "no-store")
			fmt.Fprint(w, page)
		case r.Method == http.MethodPost && r.URL.Path == "/result":
			var result browserWebRTCResult
			if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&result); err != nil {
				http.Error(w, "invalid probe result", http.StatusBadRequest)
				return
			}
			select {
			case results <- result:
			default:
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	command := exec.Command(browser,
		"--headless=new", "--user-data-dir="+t.TempDir(),
		"--no-first-run", "--no-default-browser-check", "--disable-background-networking",
		"--disable-component-update", "--disable-default-apps", "--disable-sync",
		"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost", server.URL)
	var output bytes.Buffer
	command.Stdout, command.Stderr = &output, &output
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	var exitErr error
	go func() {
		exitErr = command.Wait()
		close(done)
	}()
	defer func() {
		select {
		case <-done:
			return
		default:
		}
		// Kill only this browser's process tree, never other browser instances.
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := exec.CommandContext(ctx, "taskkill.exe", "/PID", strconv.Itoa(command.Process.Pid), "/T", "/F").Run(); err != nil {
			select {
			case <-done:
				return
			default:
				t.Errorf("stop disposable browser process tree: %v", err)
				command.Process.Kill()
			}
		}
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			t.Error("disposable browser did not exit after termination")
		}
	}()
	select {
	case result := <-results:
		return result
	case <-done:
		t.Fatalf("Browser exited before the WebRTC result: %v\n%s", exitErr, output.String())
	case <-time.After(60 * time.Second):
		t.Fatal("Browser did not report the local WebRTC result within 60 seconds")
	}
	return browserWebRTCResult{}
}

// This fixture-only check needs neither Windows nor SBC_E2E=1. It checks the
// local responder's wire format, not browser or Windows policy enforcement.
func TestWebRTCSTUNFixture(t *testing.T) {
	server, count := startWebRTCSTUN(t)
	client, err := net.DialUDP("udp4", nil, server.LocalAddr().(*net.UDPAddr))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if err := client.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
		t.Fatal(err)
	}
	request := make([]byte, 20)
	binary.BigEndian.PutUint16(request[:2], 0x0001)
	binary.BigEndian.PutUint32(request[4:8], stunCookie)
	copy(request[8:20], "fixture-txid")
	if _, err := client.Write(request); err != nil {
		t.Fatal(err)
	}
	response := make([]byte, 2048)
	n, err := client.Read(response)
	if err != nil {
		t.Fatal(err)
	}
	if n != 32 || count.Load() != 1 || binary.BigEndian.Uint16(response[:2]) != 0x0101 ||
		binary.BigEndian.Uint16(response[2:4]) != 12 || !bytes.Equal(response[4:20], request[4:20]) ||
		binary.BigEndian.Uint16(response[20:22]) != 0x0020 || binary.BigEndian.Uint16(response[22:24]) != 8 || response[25] != 1 {
		t.Fatalf("invalid STUN binding response: %x", response[:n])
	}
	port := binary.BigEndian.Uint16(response[26:28]) ^ uint16(stunCookie>>16)
	address := make(net.IP, 4)
	binary.BigEndian.PutUint32(address, binary.BigEndian.Uint32(response[28:32])^stunCookie)
	if address.String() != stunFixtureIP || int(port) != client.LocalAddr().(*net.UDPAddr).Port {
		t.Fatalf("invalid XOR-MAPPED-ADDRESS: %s:%d", address, port)
	}
	for _, invalid := range [][]byte{nil, request[:19], append(append([]byte{}, request...), 0), make([]byte, 20)} {
		if stunBindingResponse(invalid, 1234) != nil {
			t.Fatal("STUN fixture accepted an invalid binding request")
		}
	}
}
