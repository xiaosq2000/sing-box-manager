//go:build e2e

package e2e

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// This standalone entry uses an explicit local binary and changes no services,
// proxy settings or user files. Native lifecycle CI also calls the same check.
func TestSnellV6Loopback(t *testing.T) {
	binary := os.Getenv("SBM_TEST_SING_BOX")
	if binary == "" {
		t.Skip("set SBM_TEST_SING_BOX to test Snell without installing the client")
	}
	testSnellV6(t, binary)
}

func testSnellV6(t *testing.T, binary string) {
	t.Helper()
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "snell-v6-origin")
	}))
	defer site.Close()
	nodePort := freePort(t)
	startSnellFixture(t, binary, fmt.Sprintf(`{
"inbounds":[{"type":"snell","tag":"snell","listen":"127.0.0.1","listen_port":%d,
"version":6,"psk":"synthetic-shared-snell-psk","mode":"default",
"users":[{"name":"snell:alice","userkey":"synthetic-snell-alice"},{"name":"snell:bob","userkey":"synthetic-snell-bob"}]}],
"outbounds":[{"type":"direct"}]}`, nodePort), nodePort)
	for _, test := range []struct {
		name, key string
		valid     bool
	}{
		{"alice", "synthetic-snell-alice", true},
		{"bob", "synthetic-snell-bob", true},
		{"wrong", "synthetic-wrong-key", false},
		{"missing", "", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			port := freePort(t)
			startSnellFixture(t, binary, fmt.Sprintf(`{
"inbounds":[{"type":"mixed","listen":"127.0.0.1","listen_port":%d}],
"outbounds":[{"type":"snell","server":"127.0.0.1","server_port":%d,"version":6,
"psk":"synthetic-shared-snell-psk","userkey":%q,"mode":"default","reuse":false}]}`, port, nodePort, test.key), port)
			proxy, _ := url.Parse("http://127.0.0.1:" + strconv.Itoa(port))
			transport := &http.Transport{Proxy: http.ProxyURL(proxy), DisableKeepAlives: true}
			defer transport.CloseIdleConnections()
			client := &http.Client{Transport: transport, Timeout: 3 * time.Second}
			response, err := client.Get(site.URL)
			valid := test.valid
			if err != nil {
				if valid {
					t.Fatal("valid Snell user could not reach the fixture origin")
				}
				return
			}
			defer response.Body.Close()
			body, err := io.ReadAll(response.Body)
			if err != nil && valid {
				t.Fatal(err)
			}
			reached := response.StatusCode == http.StatusOK && strings.Contains(string(body), "snell-v6-origin")
			if reached != valid {
				t.Fatalf("Snell user authorization: reached=%v, authorized=%v", reached, valid)
			}
		})
	}
}

func startSnellFixture(t *testing.T, binary, config string, port int) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	if err := os.WriteFile(path, []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	process := exec.Command(binary, "run", "-D", dir, "-c", path)
	if err := process.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { process.Process.Kill(); process.Wait() })
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		connection, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), 100*time.Millisecond)
		if err == nil {
			connection.Close()
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("Snell fixture did not start")
}
