package tun

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
)

func requestFixture(t *testing.T, handler http.Handler, capturedSocket bool, tls bool) (*api.Client, string) {
	t.Helper()
	var mu sync.Mutex
	var peer string
	target := httptest.NewUnstartedServer(handler)
	target.Config.ConnState = func(conn net.Conn, state http.ConnState) {
		if state == http.StateNew {
			mu.Lock()
			peer = conn.RemoteAddr().String()
			mu.Unlock()
		}
	}
	if tls {
		target.StartTLS()
	} else {
		target.Start()
	}
	t.Cleanup(target.Close)
	page, err := url.Parse(target.URL)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/connections" {
			t.Errorf("probe changed API state: %s %s", r.Method, r.URL.Path)
			w.WriteHeader(500)
			return
		}
		mu.Lock()
		remote := peer
		mu.Unlock()
		connections := []api.Connection{}
		if remote != "" && capturedSocket {
			host, port, _ := net.SplitHostPort(remote)
			connections = append(connections, api.Connection{ID: "target", Metadata: api.ConnectionMetadata{
				Type: "tun/tun-in", Network: "tcp", SourceIP: host, SourcePort: port,
				DestinationIP: page.Hostname(), DestinationPort: page.Port(),
			}})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"connections": connections})
	}))
	t.Cleanup(server.Close)
	address, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(address.Port())
	return api.New(port, "fixture"), target.URL
}

func TestVerifyRequestUsesTheCapturedSocketWithoutProxyOrRedirect(t *testing.T) {
	var count atomic.Int32
	client, page := requestFixture(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		count.Add(1)
		w.Header().Set("Location", "https://unlisted.invalid/")
		w.WriteHeader(http.StatusFound)
	}), true, false)
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	if err := verifyRequest(client, "tun-in", page); err != nil {
		t.Fatal(err)
	}
	if count.Load() != 1 {
		t.Fatalf("requests=%d", count.Load())
	}
}

func TestVerifyRequestRejectsUncapturedDirectSuccess(t *testing.T) {
	var count atomic.Int32
	client, page := requestFixture(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { count.Add(1) }), false, false)
	if err := verifyRequest(client, "tun-in", page); err == nil || !strings.Contains(err.Error(), "did not capture") {
		t.Fatalf("error=%v", err)
	}
	if count.Load() != 0 {
		t.Fatal("uncaptured socket sent an application request")
	}
}

func TestVerifyRequestDoesNotBypassTLSValidation(t *testing.T) {
	client, page := requestFixture(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}), true, true)
	if err := verifyRequest(client, "tun-in", page); err == nil || !strings.Contains(err.Error(), "certificate") {
		t.Fatalf("error=%v", err)
	}
}
