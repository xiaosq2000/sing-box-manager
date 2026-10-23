package cli

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/diagnostic"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

// Reject CONNECT locally. Built-in selection tests must never dial public hosts.
func TestCheckCategorizedPresetSelectionIsOffline(t *testing.T) {
	for _, tc := range []struct {
		name, preset string
		saved        bool
	}{
		{name: "default"},
		{name: "explicit-default", preset: "default", saved: true},
		{name: "china", preset: "china"},
		{name: "global", preset: "global"},
		{name: "ai", preset: "ai"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t)
			const saved = `{"schema_version":1,"targets":[]}`
			if tc.saved {
				if err := paths.WriteFile(targetPath(h.layout), []byte(saved), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			var mu sync.Mutex
			seen := map[string]int{}
			proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodConnect {
					t.Error("expected CONNECT")
				}
				mu.Lock()
				seen[r.Host]++
				mu.Unlock()
				w.WriteHeader(http.StatusBadGateway)
			}))
			defer proxy.Close()
			u, _ := url.Parse(proxy.URL)
			port, _ := strconv.Atoi(u.Port())
			h.writeLocal(t, func(local *singbox.Local) { local.ListenPort = port })
			args := []string{"check", "--json"}
			if tc.preset != "" {
				args = append(args, "--preset", tc.preset)
			}
			if code := Run(h.env, args); code != 1 {
				t.Fatalf("rejected CONNECT returned %d: %s%s", code, h.out, h.errOut)
			}
			report := reportFrom(t, h)
			name := tc.preset
			if name == "" {
				name = "default"
			}
			list, _ := diagnostic.Preset(name)
			if !report.ContextValid || len(report.Observations) != len(list.Targets) {
				t.Fatal(h.out)
			}
			wantSeen := map[string]int{}
			for index, target := range list.Targets {
				row := report.Observations[index]
				if row.ID != target.ID || row.Host != diagnostic.Host(target.URL) || row.Category != target.Category || len(row.Samples) != 1 {
					t.Fatalf("row = %+v; target = %+v", row, target)
				}
				wantSeen[net.JoinHostPort(row.Host, "443")]++
			}
			mu.Lock()
			defer mu.Unlock()
			if !reflect.DeepEqual(seen, wantSeen) {
				t.Fatalf("CONNECT destinations = %v, want %v", seen, wantSeen)
			}
			if tc.saved {
				after, err := os.ReadFile(targetPath(h.layout))
				if err != nil || string(after) != saved {
					t.Fatal("explicit preset replaced the saved list", err)
				}
			} else if _, err := os.Stat(targetPath(h.layout)); !os.IsNotExist(err) {
				t.Fatal("preset check wrote a saved list")
			}
		})
	}
}

func TestCategorizedTargetListIsLocalizedOfflineAndPreservesSavedBytes(t *testing.T) {
	defer i18n.SetChinese(false)
	for _, chinese := range []bool{false, true} {
		i18n.SetChinese(chinese)
		h := newHarness(t)
		h.env.TunCapture = func() tun.CaptureState { t.Fatal("list inspected capture"); return tun.CaptureState{} }
		legacy := `{"schema_version":1,"targets":[{"id":"baidu","url":"https://www.baidu.com/favicon.ico"},{"id":"custom","url":"https://fixture.test/private-fixture?token=fixture"}]}`
		if err := paths.WriteFile(targetPath(h.layout), []byte(legacy), 0o600); err != nil {
			t.Fatal(err)
		}
		if code := Run(h.env, []string{"check", "targets", "list"}); code != 0 {
			t.Fatal(code, h.errOut)
		}
		labels := []string{"Category", "China", "Custom"}
		if chinese {
			labels = []string{"分类", "国内", "自定义"}
		}
		for _, label := range labels {
			if !strings.Contains(h.out.String(), label) {
				t.Fatalf("missing %q in %s", label, h.out)
			}
		}
		if strings.Contains(h.out.String(), "private-fixture") || strings.Contains(h.out.String(), "token=") {
			t.Fatal("list exposed a URL path or query")
		}
		after, err := os.ReadFile(targetPath(h.layout))
		if err != nil || string(after) != legacy {
			t.Fatal("list modified saved configuration", err)
		}
	}
}

func TestCategorizedTargetResetExportImport(t *testing.T) {
	h := newHarness(t)
	h.env.TunCapture = func() tun.CaptureState { t.Fatal("list inspected capture"); return tun.CaptureState{} }
	if code := Run(h.env, []string{"check", "targets", "reset"}); code != 0 {
		t.Fatal(code, h.errOut)
	}
	h.out.Reset()
	if code := Run(h.env, []string{"check", "targets", "export"}); code != 0 {
		t.Fatal(code, h.errOut)
	}
	var exported diagnostic.TargetList
	if err := json.Unmarshal(h.out.Bytes(), &exported); err != nil {
		t.Fatal(err)
	}
	want, _ := diagnostic.Preset("default")
	if !reflect.DeepEqual(exported, want) {
		t.Fatal("reset or export lost default categories", exported)
	}
	importFile := filepath.Join(t.TempDir(), "targets.json")
	if err := os.WriteFile(importFile, h.out.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	if code := Run(h.env, []string{"check", "targets", "remove", "google"}); code != 0 {
		t.Fatal(code, h.errOut)
	}
	if code := Run(h.env, []string{"check", "targets", "import", importFile}); code != 0 {
		t.Fatal(code, h.errOut)
	}
	got, err := diagnostic.ReadList(targetPath(h.layout), false)
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatal("import lost categories", got, err)
	}
}

func TestCheckCategoryAppearsInJSONAndBothHumanViews(t *testing.T) {
	defer i18n.SetChinese(false)
	h := newHarness(t)
	site, roots := diagnosticSite(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) }))
	h.env.DiagnosticRoots = roots
	tunnelProxy(t, h, site)
	u, _ := url.Parse(site.URL)
	list := diagnostic.TargetList{SchemaVersion: 1, Targets: []diagnostic.Target{{ID: "fixture", URL: "https://fixture.test:" + u.Port() + "/private-fixture?token=fixture", Category: "global"}}}
	data, err := json.Marshal(list)
	if err != nil {
		t.Fatal(err)
	}
	if err := paths.WriteFile(targetPath(h.layout), data, 0o600); err != nil {
		t.Fatal(err)
	}
	if code := Run(h.env, []string{"check", "--json"}); code != 0 {
		t.Fatal(code, h.out, h.errOut)
	}
	report := reportFrom(t, h)
	if report.SchemaVersion != 1 || len(report.Observations) != 1 || report.Observations[0].Category != "global" {
		t.Fatal(h.out)
	}
	for _, chinese := range []bool{false, true} {
		i18n.SetChinese(chinese)
		h.out.Reset()
		h.errOut.Reset()
		if code := Run(h.env, []string{"check"}); code != 0 {
			t.Fatal(code, h.out, h.errOut)
		}
		category, label := "Category", "International"
		if chinese {
			category, label = "分类", "国际"
		}
		if !strings.Contains(h.out.String(), category) || !strings.Contains(h.out.String(), label) {
			t.Fatal(h.out)
		}
		// The live progress view and final renderer share these headers and rows.
		rows, _ := buildDiagnosticTableRows(report.Observations)
		if diagnosticHeaders("check")[6] != category || rows[0][6] != label {
			t.Fatal("live/final category mismatch", rows)
		}
		if strings.Contains(h.out.String(), "private-fixture") || strings.Contains(h.out.String(), "token=") {
			t.Fatal("report exposed a URL path or query")
		}
	}
	after, err := os.ReadFile(targetPath(h.layout))
	if err != nil || string(after) != string(data) {
		t.Fatal("check modified the saved list", err)
	}
}
