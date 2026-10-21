package tun

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const selectionConfig = `{"inbounds":[{"type":"tun","tag":"tun"}],"outbounds":[{"tag":"proxy","type":"selector","outbounds":["trojan","naive"],"default":"trojan"}],"route":{"rules":[{"clash_mode":"china"},{"clash_mode":"global"},{"clash_mode":"direct"}]},"experimental":{"clash_api":{"default_mode":"global"},"cache_file":{"enabled":true,"cache_id":"old"}}}`

func TestMaintenanceSelectionUsesFreshDefaultsBeforeStartup(t *testing.T) {
	id := strings.Repeat("2", 32)
	selection := Selection{Mode: "china-direct", Protocol: "naive"}
	patched, _, err := Patch([]byte(selectionConfig), Local{Selection: selection, CacheID: "sbc-tun-" + id})
	if err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(t.TempDir(), "config.json")
	if err = os.WriteFile(file, patched, 0o600); err != nil {
		t.Fatal(err)
	}
	if err = checkSelection(file, selection, id); err != nil {
		t.Fatal(err)
	}
	var root struct {
		Experimental struct {
			Clash struct {
				Mode string `json:"default_mode"`
			} `json:"clash_api"`
			Cache struct {
				ID string `json:"cache_id"`
			} `json:"cache_file"`
		} `json:"experimental"`
		Outbounds []struct{ Default string } `json:"outbounds"`
	}
	if json.Unmarshal(patched, &root) != nil || root.Experimental.Clash.Mode != "china" || root.Outbounds[0].Default != "naive" || root.Experimental.Cache.ID != "sbc-tun-"+id {
		t.Fatal("startup defaults did not preserve the current selection")
	}
	if strings.Contains(selectionConfig, "sbc-tun-") {
		t.Fatal("patch changed its input")
	}
	for _, bad := range []struct {
		selection Selection
		cache     string
	}{
		{Selection{Mode: "removed-mode", Protocol: "naive"}, "new"},
		{Selection{Mode: "china-direct", Protocol: "removed-protocol"}, "new"},
		{selection, ""},
	} {
		if _, _, err := Patch([]byte(selectionConfig), Local{Selection: bad.selection, CacheID: bad.cache}); err == nil {
			t.Fatal("invalid selection passed preparation")
		}
	}
	for _, bad := range []string{strings.Replace(string(patched), "sbc-tun-"+id, "stale", 1), strings.Replace(string(patched), `"default": "naive"`, `"default": "trojan"`, 1)} {
		if err = os.WriteFile(file, []byte(bad), 0o600); err != nil {
			t.Fatal(err)
		}
		if err = checkSelection(file, selection, id); err == nil {
			t.Fatal("stale startup state passed root validation")
		}
	}
}

func TestSelectionVerificationNeverChangesThePolicy(t *testing.T) {
	for _, matching := range []bool{true, false} {
		t.Run(map[bool]string{true: "matching", false: "wrong-route"}[matching], func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet {
					t.Error("verification attempted a policy change")
					w.WriteHeader(400)
					return
				}
				switch r.URL.Path {
				case "/version":
					w.Write([]byte(`{}`))
				case "/configs":
					mode := "global"
					if matching {
						mode = "china"
					}
					json.NewEncoder(w).Encode(map[string]any{"mode": mode})
				case "/proxies/proxy":
					w.Write([]byte(`{"now":"naive"}`))
				default:
					t.Error("unexpected probe request")
					w.WriteHeader(400)
				}
			}))
			defer server.Close()
			config := filepath.Join(t.TempDir(), "config.json")
			data, _ := json.Marshal(map[string]any{"experimental": map[string]any{"clash_api": map[string]string{"external_controller": strings.TrimPrefix(server.URL, "http://"), "secret": "fixture-secret"}}})
			if err := os.WriteFile(config, data, 0o600); err != nil {
				t.Fatal(err)
			}
			err := verifySelection(Paths{Config: config}, Selection{Mode: "china-direct", Protocol: "naive"})
			if matching != (err == nil) {
				t.Fatalf("selection check: %v", err)
			}
		})
	}
}

func TestSourceFingerprintIncludesRuleContents(t *testing.T) {
	config := []byte(`{"experimental":{},"route":{"rule_set":[{"type":"remote","tag":"fixture","initial_path":"rules/fixture.srs"}]}}`)
	first, err := SourceFingerprint(config, func(string) (string, error) { return "first", nil })
	if err != nil {
		t.Fatal(err)
	}
	second, err := SourceFingerprint(config, func(string) (string, error) { return "second", nil })
	if err != nil || first == second {
		t.Fatal("rule content change was invisible to update checks")
	}
}
