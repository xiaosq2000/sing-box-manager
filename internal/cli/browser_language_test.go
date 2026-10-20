package cli

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/browserlanguage"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/region"
)

// Every CLI fixture gets scratch Chrome roots and policies. No fallback may
// inspect a developer's real profiles, even when the command fails early.
func isolateBrowserLanguage(t *testing.T, env *Env, layout paths.Layout) *browserlanguage.Manager {
	t.Helper()
	if err := os.MkdirAll(layout.Config, 0o700); err != nil {
		t.Fatal(err)
	}
	config, err := filepath.EvalSymlinks(layout.Config)
	if err != nil {
		t.Fatal(err)
	}
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	manager := browserlanguage.New(filepath.Join(config, "browser-language.json"), filepath.Join(root, "chrome"))
	manager.PolicyDirs = []string{filepath.Join(root, "policies")}
	env.BrowserLanguage = func(paths.Layout) (*browserlanguage.Manager, error) { return manager, nil }
	return manager
}

func browserCLIFixture(t *testing.T, h *harness) (*browserlanguage.Manager, string) {
	t.Helper()
	h.env.OS = "linux"
	manager, err := h.env.BrowserLanguage(h.layout)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(manager.ConfigHome, "google-chrome", "Default", "Preferences")
	if err := paths.WriteFile(path, []byte(`{"intl":{"accept_languages":"zh-CN,zh","selected_languages":"zh-CN,zh"},"unrelated":true}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := regionFor(h.env, h.layout).StoreState(region.State{
		Node: "node.example", Profile: region.Profile{Timezone: "UTC", Language: "en-US"},
	}); err != nil {
		t.Fatal(err)
	}
	return manager, path
}

func TestBrowserLanguageIsIndependentOfWebRTCAndLocaleAvailability(t *testing.T) {
	h := newHarness(t)
	manager, path := browserCLIFixture(t, h)
	privacy := h.useDesktop()
	// A broken region choice must not prevent reading the cached node profile.
	paths.WriteFile(h.layout.RegionMode(), []byte("invalid"), 0o600)
	if code := Run(h.env, []string{"privacy", "browser", "language", "on"}); code != 0 {
		t.Fatal(h.errOut)
	}
	data, _ := os.ReadFile(path)
	if !bytes.Contains(data, []byte(`"selected_languages":"en-US,en"`)) || len(privacy.privacyCalls) != 0 {
		t.Fatalf("language or WebRTC: %s, %+v", data, privacy.privacyCalls)
	}
	if !strings.Contains(h.out.String(), "sync") {
		t.Fatal("missing sync warning")
	}
	if _, err := os.Stat(h.layout.RegionEnv()); !os.IsNotExist(err) {
		t.Fatal("browser languages enabled system region matching")
	}
	status, err := manager.Status()
	if err != nil || status.Current != 1 {
		t.Fatalf("status: %+v, %v", status, err)
	}
	if code := Run(h.env, []string{"privacy", "browser", "language", "off"}); code != 0 {
		t.Fatal(h.errOut)
	}
	data, _ = os.ReadFile(path)
	if !bytes.Contains(data, []byte(`"selected_languages":"zh-CN,zh"`)) {
		t.Fatal("off did not restore languages")
	}
}

func TestBrowserCompositeReportsPartialSuccessInBothDirections(t *testing.T) {
	for _, languageFails := range []bool{true, false} {
		h := newHarness(t)
		manager, path := browserCLIFixture(t, h)
		privacy := h.useDesktop()
		if languageFails {
			os.Symlink("fixture-host-123456", filepath.Join(filepath.Dir(filepath.Dir(path)), "SingletonLock"))
		} else {
			privacy.privacyErr = errors.New("fixture WebRTC failure")
		}
		if code := Run(h.env, []string{"privacy", "browser", "on"}); code != 1 {
			t.Fatalf("partial success code: %d", code)
		}
		if len(privacy.privacyCalls) != 1 || !privacy.privacyCalls[0] {
			t.Fatal("WebRTC was not attempted")
		}
		status, err := manager.Status()
		if err != nil || (status.Current == 1) == languageFails {
			t.Fatalf("language not independently attempted: %+v, %v", status, err)
		}
		if languageFails {
			lockPath := filepath.Join(filepath.Dir(filepath.Dir(path)), "SingletonLock")
			os.Remove(lockPath)
			if _, err := manager.Enable(browserlanguage.Target{Node: "node.example", Language: "en-US"}); err != nil {
				t.Fatal(err)
			}
			os.Symlink("fixture-host-123456", lockPath)
		}
		if code := Run(h.env, []string{"privacy", "browser", "off"}); code != 1 {
			t.Fatal("failed language restore reported overall success")
		}
		if len(privacy.privacyCalls) != 2 || privacy.privacyCalls[1] {
			t.Fatal("WebRTC off was not attempted")
		}
		status, err = manager.Status()
		if err != nil || status.State.Enabled {
			t.Fatalf("off intent lost: %+v, %v", status, err)
		}
	}
}

func TestBrowserCompositeStillEnablesWebRTCWithoutNodeMetadata(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	privacy := h.useDesktop()
	if code := Run(h.env, []string{"privacy", "browser", "on"}); code != 1 {
		t.Fatalf("code %d", code)
	}
	if len(privacy.privacyCalls) != 1 || !privacy.privacyCalls[0] {
		t.Fatal("missing metadata blocked WebRTC")
	}
}

func TestBrowserLanguageRefreshOnlyMarksTheTarget(t *testing.T) {
	for _, system := range []bool{false, true} {
		h := newHarness(t)
		manager, path := browserCLIFixture(t, h)
		if code := Run(h.env, []string{"privacy", "browser", "language", "on"}); code != 0 {
			t.Fatal(h.errOut)
		}
		if system {
			regionFor(h.env, h.layout).SaveMode(region.Mode{Language: true, Scope: region.ScopeSystem})
		}
		before, _ := os.ReadFile(path)
		envelope := &install.Envelope{DefaultNode: "new.example", Nodes: []install.Node{
			{Name: "new.example", Region: &region.Profile{Timezone: "UTC", Language: "de-DE"}},
		}}
		refreshRegion(h.env, h.layout, envelope, false)
		after, _ := os.ReadFile(path)
		if !bytes.Equal(before, after) {
			t.Fatal("refresh changed Chrome preferences")
		}
		status, err := manager.Status()
		if err != nil || status.State.Target == nil || status.State.Target.Language != "de-DE" || status.Pending != 1 {
			t.Fatalf("refresh status: %+v, %v", status, err)
		}
		if !strings.Contains(h.out.String(), "Chrome language target changed") {
			t.Fatal("missing pending application notice")
		}
		refreshRegion(h.env, h.layout, &install.Envelope{}, false)
		status, err = manager.Status()
		if err != nil || status.State.Target == nil {
			t.Fatal("transient missing metadata discarded the target")
		}
		refreshRegion(h.env, h.layout, &install.Envelope{}, true)
		status, err = manager.Status()
		if err != nil || status.State.Target != nil {
			t.Fatal("new subscription retained the old target")
		}
	}
}

func TestUninstallKeepsBrowserLanguageRecoveryUntilChromeExits(t *testing.T) {
	h := newHarness(t)
	manager, path := browserCLIFixture(t, h)
	if code := Run(h.env, []string{"privacy", "browser", "language", "on"}); code != 0 {
		t.Fatal(h.errOut)
	}
	lockPath := filepath.Join(filepath.Dir(filepath.Dir(path)), "SingletonLock")
	os.Symlink("fixture-host-123456", lockPath)
	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 1 {
		t.Fatalf("uninstall code: %d", code)
	}
	if _, err := os.Stat(manager.StateFile); err != nil || !h.svc.active {
		t.Fatal("failed cleanup discarded recovery or stopped the proxy")
	}
	os.Remove(lockPath)
	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 0 {
		t.Fatal(h.errOut)
	}
	data, _ := os.ReadFile(path)
	if !bytes.Contains(data, []byte(`"selected_languages":"zh-CN,zh"`)) || !bytes.Contains(data, []byte(`"unrelated":true`)) {
		t.Fatalf("uninstall did not restore: %s", data)
	}
}

func TestBrowserLanguageIsLinuxOnly(t *testing.T) {
	for _, goos := range []string{"darwin", "windows"} {
		for _, action := range []string{"status", "on", "off"} {
			env, _, errOut := inertEnv(t)
			env.OS = goos
			if code := Run(env, []string{"privacy", "browser", "language", action}); code != 1 || !strings.Contains(errOut.String(), "Linux only") {
				t.Fatalf("%s: %d, %s", goos, code, errOut)
			}
		}
	}
}
