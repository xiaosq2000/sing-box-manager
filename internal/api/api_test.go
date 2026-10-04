package api

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func realSingBox(t *testing.T) string {
	t.Helper()
	if path := os.Getenv("SBM_TEST_SING_BOX"); path != "" {
		return path
	}
	path, _ := filepath.Abs("../../.cache/sing-box-manager/bin/sing-box")
	if _, err := os.Stat(path); err != nil {
		t.Skip("run `pixi run fetch-sing-box` or set SBM_TEST_SING_BOX")
	}
	return path
}

func freePort(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port
}

// A config shaped like the portal's: routes as modes, protocols in a selector.
func startSingBox(t *testing.T) *Client {
	t.Helper()
	binary := realSingBox(t)
	dir := t.TempDir()
	port := freePort(t)
	config := map[string]any{
		"inbounds": []any{map[string]any{"type": "mixed", "tag": "mixed", "listen": "127.0.0.1", "listen_port": freePort(t)}},
		"outbounds": []any{
			map[string]any{"type": "selector", "tag": "proxy", "outbounds": []string{"trojan", "naive"}, "default": "trojan"},
			map[string]any{"type": "direct", "tag": "trojan"},
			map[string]any{"type": "direct", "tag": "naive"},
			map[string]any{"type": "direct", "tag": "direct"},
			// Nothing listens on the discard port, so a request through this fails.
			map[string]any{"type": "socks", "tag": "dead", "server": "127.0.0.1", "server_port": 9},
		},
		"route": map[string]any{"rules": []any{
			map[string]any{"clash_mode": "china", "outbound": "proxy"},
			map[string]any{"clash_mode": "gfw", "outbound": "direct"},
		}},
		"experimental": map[string]any{
			"cache_file": map[string]any{"enabled": true, "path": "cache.db"},
			"clash_api":  map[string]any{"default_mode": "china", "external_controller": net.JoinHostPort("127.0.0.1", itoa(port)), "secret": "s3cret"},
		},
	}
	data, _ := json.Marshal(config)
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, data, 0o600)
	process := exec.Command(binary, "run", "-D", dir, "-c", path)
	if err := process.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		process.Process.Kill()
		process.Wait()
	})
	client := New(port, "s3cret")
	if err := client.WaitReady(5 * time.Second); err != nil {
		t.Fatal(err)
	}
	return client
}

func itoa(n int) string { return jsonNumber(n) }

func jsonNumber(n int) string { data, _ := json.Marshal(n); return string(data) }

func TestModesSwitchAndRejectUnknownNames(t *testing.T) {
	client := startSingBox(t)

	mode, modes, err := client.Modes()
	if err != nil {
		t.Fatal(err)
	}
	if mode != "china" || strings.Join(modes, ",") != "china,gfw" {
		t.Fatalf("mode %q of %v", mode, modes)
	}
	if err := client.SetMode("gfw"); err != nil {
		t.Fatal(err)
	}
	if mode, _, _ := client.Modes(); mode != "gfw" {
		t.Errorf("mode %q after switching", mode)
	}
	if err := client.SetMode("cn"); err == nil || !strings.Contains(err.Error(), `unknown route "cn"`) {
		t.Errorf("got %v", err)
	}
}

func TestSelectorSwitchesAndRejectsUnknownProtocols(t *testing.T) {
	client := startSingBox(t)

	if err := client.Select(ProxySelector, "naive"); err != nil {
		t.Fatal(err)
	}
	now, all, err := client.Selected(ProxySelector)
	if err != nil || now != "naive" || strings.Join(all, ",") != "trojan,naive" {
		t.Fatalf("now %q of %v: %v", now, all, err)
	}
	if err := client.Select(ProxySelector, "vless"); err == nil || !strings.Contains(err.Error(), `unknown protocol "vless"`) {
		t.Errorf("got %v", err)
	}
}

func TestAWrongSecretAndAStoppedSingBoxAreReported(t *testing.T) {
	client := startSingBox(t)
	wrong := New(0, "wrong")
	wrong.base = client.base
	if _, _, err := wrong.Modes(); err == nil || !strings.Contains(err.Error(), "401") {
		t.Errorf("got %v", err)
	}
	if _, _, err := New(freePort(t), "s3cret").Modes(); err != ErrNotRunning {
		t.Errorf("got %v", err)
	}
}

func TestDelayReportsWhatSingBoxMeasured(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		query := r.URL.Query()
		if r.URL.Path != "/proxies/proxy/delay" || query.Get("url") != TestURL || query.Get("timeout") != "10000" || r.Header.Get("Authorization") != "Bearer s3cret" {
			http.Error(w, "unexpected request "+r.URL.String(), http.StatusBadRequest)
			return
		}
		w.Write([]byte(`{"delay":42}`))
	}))
	defer server.Close()
	client := New(0, "s3cret")
	client.base = server.URL

	delay, err := client.Delay(ProxySelector, 10*time.Second)
	if err != nil || delay != 42*time.Millisecond {
		t.Fatalf("delay %v, %v", delay, err)
	}
}

func TestDelayFailsWhenTheOutboundCannotConnect(t *testing.T) {
	client := startSingBox(t)

	_, err := client.Delay("dead", 2*time.Second)
	if err == nil || !strings.Contains(err.Error(), "dead did not reach "+TestURL) {
		t.Errorf("got %v", err)
	}
	if _, err := New(freePort(t), "s3cret").Delay(ProxySelector, time.Second); err != ErrNotRunning {
		t.Errorf("stopped sing-box: got %v", err)
	}
}
