package cli

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

func aiSites(t *testing.T, h *harness, safe bool) *[]string {
	t.Helper()
	seen := h.useSites(t, map[string]string{
		"ipify4.test":  "203.0.113.7",
		"ident4.test":  "198.51.100.20",
		"ipify6.test":  "2001:db8::7",
		"ident6.test":  "2001:db8::7",
		"gemini.test":  `,2,1,200,"USA"`,
		"chatgpt.test": "ip=203.0.113.7\nloc=US\n",
		"claude.test":  "<html>Welcome</html>",
	})
	h.env.Sites.ChatGPT = "http://chatgpt.test/cdn-cgi/trace"
	h.env.Sites.Claude = "http://claude.test/"
	if safe {
		h.writeLocal(t, func(local *singbox.Local) { local.SpeedPort = local.ListenPort })
		data, _ := os.ReadFile(h.layout.ConfigFile())
		var root map[string]any
		json.Unmarshal(data, &root)
		root["outbounds"] = append(root["outbounds"].([]any),
			map[string]any{"type": "trojan", "tag": "trojan"},
			map[string]any{"type": "naive", "tag": "naive"})
		data, _ = json.Marshal(root)
		if err := paths.WriteFile(h.layout.ConfigFile(), data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return seen
}

func TestAIProbesAreExplicitAndDirectAccessRequiresOverride(t *testing.T) {
	for _, entry := range []struct {
		name, mode string
		args       []string
		safe       bool
		requests   int
		want       string
	}{
		{"default", "china", []string{"ip"}, true, 4, "china-direct"},
		{"proxy", "china", []string{"ip", "--ai"}, true, 7, "AI checks use the remote proxy"},
		{"dual stack", "china", []string{"ip", "--dual-stack", "--ai"}, true, 10, "IPify"},
		{"direct skipped", "direct", []string{"ip", "--ai"}, true, 4, "skipped: direct route"},
		{"unknown skipped", "china", []string{"ip", "--ai"}, false, 4, "skipped: proxy-only inlet unavailable"},
		{"direct permitted", "direct", []string{"ip", "--ai", "--allow-direct-ai"}, false, 7, "Direct network IP exposure is permitted"},
	} {
		t.Run(entry.name, func(t *testing.T) {
			h := newHarness(t)
			h.api.mode = entry.mode
			seen := aiSites(t, h, entry.safe)
			code := Run(h.env, entry.args)
			if code != 0 || len(*seen) != entry.requests || !strings.Contains(h.out.String(), entry.want) {
				t.Fatalf("code %d, requests %d, output %s%s", code, len(*seen), h.out, h.errOut)
			}
			if entry.requests >= 7 {
				for _, text := range []string{"gemini:", "reported region USA", "chatgpt:", "edge country US", "claude:", "region unknown"} {
					if !strings.Contains(h.out.String(), text) {
						t.Errorf("missing %q in %s", text, h.out)
					}
				}
			}
			if entry.name == "default" && strings.Contains(h.out.String(), "gemini:") {
				t.Fatal("default command displayed an AI check")
			}
		})
	}
}

func TestAIUsesTheSeparateProxyOnlyInletWithoutSwitching(t *testing.T) {
	h := newHarness(t)
	generalRequests := aiSites(t, h, true)
	var lock sync.Mutex
	var aiRequests []string
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lock.Lock()
		aiRequests = append(aiRequests, r.URL.Host)
		lock.Unlock()
		switch r.URL.Host {
		case "gemini.test":
			io.WriteString(w, `,2,1,200,"USA"`)
		case "chatgpt.test":
			io.WriteString(w, "ip=203.0.113.7\nloc=US\n")
		case "claude.test":
			io.WriteString(w, "<html>Welcome</html>")
		default:
			t.Errorf("unexpected diagnostic request: %s", r.URL)
		}
	}))
	defer proxy.Close()
	address, _ := url.Parse(proxy.URL)
	port, _ := strconv.Atoi(address.Port())
	h.writeLocal(t, func(local *singbox.Local) { local.SpeedPort = port })
	if code := Run(h.env, []string{"ip", "--ai"}); code != 0 {
		t.Fatalf("code %d: %s%s", code, h.out, h.errOut)
	}
	if len(*generalRequests) != 4 || len(aiRequests) != 3 || h.api.mode != "china" || h.api.protocol != "trojan" || len(h.api.speedTests) != 0 {
		t.Fatalf("general %d, AI %v, mode %s, protocol %s", len(*generalRequests), aiRequests, h.api.mode, h.api.protocol)
	}
}

func TestAIProxyFailureNeverRetriesThroughTheOrdinaryInlet(t *testing.T) {
	h := newHarness(t)
	generalRequests := aiSites(t, h, true)
	proxy := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	address, _ := url.Parse(proxy.URL)
	port, _ := strconv.Atoi(address.Port())
	proxy.Close()
	h.writeLocal(t, func(local *singbox.Local) { local.SpeedPort = port })
	if code := Run(h.env, []string{"ip", "--ai"}); code != 1 {
		t.Fatalf("code %d: %s%s", code, h.out, h.errOut)
	}
	if len(*generalRequests) != 4 {
		t.Fatalf("AI requests reached the ordinary inlet: %d requests", len(*generalRequests))
	}
}

func TestDirectAIOverrideAndRouteListValidateBeforeActions(t *testing.T) {
	for _, args := range [][]string{{"ip", "--allow-direct-ai"}, {"ip", "--ai=false", "--allow-direct-ai"}, {"route", "--list", "direct"}} {
		env, _, _ := inertEnv(t)
		if code := Run(env, args); code != 2 {
			t.Fatalf("%v: code %d", args, code)
		}
	}
}

func TestRouteListIsOfflineAndDescribesCanonicalChoices(t *testing.T) {
	h := newHarness(t)
	data, _ := os.ReadFile(h.layout.ConfigFile())
	var root map[string]any
	json.Unmarshal(data, &root)
	root["route"] = map[string]any{"rules": []any{
		map[string]any{"clash_mode": []string{"china-direct", "china"}},
		map[string]any{"clash_mode": "direct"},
	}}
	data, _ = json.Marshal(root)
	paths.WriteFile(h.layout.ConfigFile(), data, 0o600)
	h.env.Service = nil
	if code := Run(h.env, []string{"route", "--list"}); code != 0 {
		t.Fatal(code)
	}
	if !strings.Contains(h.out.String(), "china-direct") || !strings.Contains(h.out.String(), "Direct all allowed traffic") || strings.Contains(h.out.String(), "china ") {
		t.Fatalf("got %s", h.out)
	}
}
