//go:build e2e

package e2e

import (
	"bytes"
	"encoding/json"
	"fmt"
	"html"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/browserlanguage"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func browserLanguageCommand(browser, root, page string) *exec.Cmd {
	command := browserWebRTCCommand(browser, root, page)
	// Dump after the page's synchronous script. No language override is allowed.
	command.Args = append(command.Args[:len(command.Args)-1], "--dump-dom", page)
	return command
}

func TestBrowserLanguageProbeCommand(t *testing.T) {
	command := browserLanguageCommand("fixture-browser", "fixture-root", "http://127.0.0.1:12345/")
	for _, arg := range command.Args[1:] {
		if strings.HasPrefix(arg, "--lang") || strings.Contains(strings.ToLower(arg), "accept-language") || arg == "--no-sandbox" || arg == "--incognito" {
			t.Fatalf("probe overrides language, mode or sandbox: %s", arg)
		}
	}
}

// This native check uses a disposable profile, never the runner's personal
// profile. It needs the same disposable Linux runner gate as the lifecycle.
func TestChromeLanguagePreferences(t *testing.T) {
	if runtime.GOOS != "linux" || os.Getenv("SBC_E2E") != "1" {
		t.Skip("requires an explicitly enabled disposable Linux runner")
	}
	chrome := unixChrome(t)
	base := t.TempDir()
	root := filepath.Join(base, "google-chrome")
	prefs := filepath.Join(root, "Default", "Preferences")
	original := `{"intl":{"accept_languages":"zh-CN,zh","selected_languages":"zh-CN,zh"}}`
	if err := paths.WriteFile(prefs, []byte(original), 0o600); err != nil {
		t.Fatal(err)
	}
	manager := browserlanguage.New(filepath.Join(base, "sbc", "browser-language.json"), base)
	checkChromeLanguages(t, chrome, root, "zh-CN", "zh")
	if _, err := manager.Enable(browserlanguage.Target{Node: "node.example", Language: "en-US"}); err != nil {
		t.Fatal(err)
	}
	checkChromeLanguages(t, chrome, root, "en-US", "en")
	// A real browser exit can write unrelated preferences. Restoration must
	// still retain ownership of the two language fields.
	if _, err := manager.Disable(); err != nil {
		t.Fatal(err)
	}
	checkChromeLanguages(t, chrome, root, "zh-CN", "zh")
}

func checkChromeLanguages(t *testing.T, chrome, root, primary, base string) {
	t.Helper()
	headers := make(chan string, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		select {
		case headers <- r.Header.Get("Accept-Language"):
		default:
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		fmt.Fprint(w, `<!doctype html><body><script>document.body.textContent = JSON.stringify({language: navigator.language, languages: navigator.languages});</script>`)
	}))
	defer server.Close()
	command := browserLanguageCommand(chrome, root, server.URL+"/")
	var dom bytes.Buffer
	var output browserOutput
	command.Stdout, command.Stderr = &dom, &output
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	var exitErr error
	go func() { exitErr = command.Wait(); close(done) }()
	select {
	case <-done:
		if exitErr != nil {
			t.Fatalf("Chrome language probe failed: %v\n%s", exitErr, output.String())
		}
	case <-time.After(time.Minute):
		stopErr := stopAndWaitForBrowser(command, done)
		t.Fatalf("Chrome language probe timed out: cleanup=%v\n%s", stopErr, output.String())
	}
	_, body, ok := strings.Cut(dom.String(), "<body>")
	if !ok {
		t.Fatalf("probe returned no body: %s\n%s", dom.String(), output.String())
	}
	body, _, _ = strings.Cut(body, "</body>")
	var result struct {
		Language  string   `json:"language"`
		Languages []string `json:"languages"`
	}
	if err := json.Unmarshal([]byte(html.UnescapeString(body)), &result); err != nil {
		t.Fatalf("invalid language probe result: %v, %s", err, body)
	}
	if result.Language != primary || len(result.Languages) == 0 || result.Languages[0] != primary {
		t.Fatalf("wrong JavaScript language: %+v, want %s", result, primary)
	}
	// Chrome versions can reduce the exposed list. No unrelated language is
	// allowed in either JavaScript or the actual HTTP request.
	for _, language := range result.Languages {
		if language != primary && language != base {
			t.Fatalf("unrelated JavaScript language: %+v", result)
		}
	}
	select {
	case header := <-headers:
		for index, item := range strings.Split(header, ",") {
			language, _, _ := strings.Cut(strings.TrimSpace(item), ";")
			if (index == 0 && language != primary) || (language != primary && language != base) {
				t.Fatalf("wrong Accept-Language: %q, want only %s,%s", header, primary, base)
			}
		}
	default:
		t.Fatal("Chrome did not request the language probe page")
	}
}
