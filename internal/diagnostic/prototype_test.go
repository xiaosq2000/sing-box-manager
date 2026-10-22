package diagnostic

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
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
	"sync/atomic"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

// TestD0RealEngineObserver measures best-effort evidence without holding a
// client request open for inspection. Only the fixture's slow case delays data.
func TestD0RealEngineObserver(t *testing.T) {
	binary := os.Getenv("SBM_TEST_SING_BOX")
	if binary == "" {
		binary = filepath.Join("..", "..", ".cache", "sing-box-manager", "bin", "sing-box")
	}
	binary, _ = filepath.Abs(binary)
	if _, err := os.Stat(binary); err != nil {
		t.Skip("real engine not available")
	}
	site := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/empty" {
			w.WriteHeader(204)
			return
		}
		if r.URL.Path == "/slow" {
			time.Sleep(50 * time.Millisecond)
		}
		w.Header().Set("Connection", "close")
		fmt.Fprint(w, "fixture response")
	}))
	defer site.Close()
	proxyPort, _ := singbox.FreePort(0)
	apiPort, _ := singbox.FreePort(0)
	config := map[string]any{
		"log":          map[string]any{"disabled": true},
		"inbounds":     []any{map[string]any{"type": "mixed", "tag": "d0-inlet", "listen": "127.0.0.1", "listen_port": proxyPort}},
		"outbounds":    []any{map[string]any{"type": "direct", "tag": "direct"}},
		"experimental": map[string]any{"clash_api": map[string]any{"external_controller": "127.0.0.1:" + strconv.Itoa(apiPort), "secret": "fixture-diag-key"}},
	}
	dir := t.TempDir()
	data, _ := json.Marshal(config)
	path := filepath.Join(dir, "config.json")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(binary, "run", "-D", dir, "-c", path)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	client := api.New(apiPort, "fixture-diag-key")
	if err := client.WaitReady(5 * time.Second); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	observer, err := NewObserver(ctx, "mixed/d0-inlet", nil, client.ConnectionsContext)
	if err != nil {
		t.Fatal(err)
	}
	defer observer.Stop()
	roots := x509.NewCertPool()
	roots.AddCert(site.Certificate())
	proxy, _ := url.Parse("http://127.0.0.1:" + strconv.Itoa(proxyPort))
	for _, name := range []string{"slow", "fast", "empty"} {
		var indices []int
		for range 12 {
			var index atomic.Int32
			index.Store(-1)
			transport := &http.Transport{Proxy: http.ProxyURL(proxy), TLSClientConfig: &tls.Config{RootCAs: roots}, DisableKeepAlives: true,
				DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
					conn, err := (&net.Dialer{}).DialContext(ctx, network, address)
					if err == nil {
						u, _ := url.Parse(site.URL)
						var tracked int
						conn, tracked = observer.Wrap(conn, u.Hostname(), u.Port())
						index.Store(int32(tracked))
					}
					return conn, err
				}}
			web := &http.Client{Transport: transport, Timeout: 2 * time.Second}
			response, err := web.Get(site.URL + "/" + name)
			if err != nil {
				t.Fatal(err)
			}
			_, err = io.Copy(io.Discard, response.Body)
			response.Body.Close()
			transport.CloseIdleConnections()
			if err != nil {
				t.Fatal(err)
			}
			indices = append(indices, int(index.Load()))
		}
		verified := 0
		for _, index := range indices {
			evidence := observer.Evidence(index)
			if evidence.Verified {
				verified++
				if evidence.Path != "direct" {
					t.Fatalf("wrong path: %+v", evidence)
				}
			}
		}
		t.Logf("D0 %s: %d/%d sockets verified", name, verified, len(indices))
		if name == "slow" && verified != len(indices) {
			t.Fatal("normal-lived connections lack evidence")
		}
	}
}
