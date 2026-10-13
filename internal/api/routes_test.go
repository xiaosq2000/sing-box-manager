package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestRouteAliasesWorkWithOldAndNewConfigs(t *testing.T) {
	for _, modes := range [][]string{{"china", "ai", "gfw", "global"}, {"china-direct", "china", "ai-only", "ai", "gfw-proxy", "gfw", "global", "direct"}} {
		current := "china"
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodGet {
				json.NewEncoder(w).Encode(map[string]any{"mode": current, "mode-list": modes})
			} else {
				var body map[string]string
				json.NewDecoder(r.Body).Decode(&body)
				current = body["mode"]
			}
		}))
		client := New(0, "fixture")
		client.base = server.URL
		mode, choices, err := client.Modes()
		want := []string{"ai-only", "china-direct", "gfw-proxy", "global"}
		if len(modes) > 4 {
			want = []string{"ai-only", "china-direct", "direct", "gfw-proxy", "global"}
		}
		if err != nil || mode != "china-direct" || !reflect.DeepEqual(choices, want) {
			t.Fatalf("%s %v %v", mode, choices, err)
		}
		for _, input := range []string{"ai", "ai-only", "gfw", "gfw-proxy", "china", "china-direct"} {
			if err := client.SetMode(input); err != nil {
				t.Fatal(err)
			}
		}
		wantMode := "china"
		if len(modes) > 4 {
			wantMode = "china-direct"
		}
		if current != wantMode {
			t.Fatalf("selected %s, want %s", current, wantMode)
		}
		server.Close()
	}
}
