package cli

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

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
		t.Fatal(h.out)
	}
}
