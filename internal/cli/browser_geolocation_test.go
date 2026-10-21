package cli

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

// CLI fixtures never inspect host policy directories or execute sudo. The
// desktop package separately executes the real payload in temporary directories.
func isolateBrowserGeolocation(t *testing.T, env *Env, layout paths.Layout) *desktop.ChromeGeolocation {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	config, err := filepath.EvalSymlinks(layout.Config)
	if err != nil {
		t.Fatal(err)
	}
	manager, err := desktop.NewChromeGeolocation("linux", filepath.Join(config, "geolocation-mode"), nil)
	if err != nil {
		t.Fatal(err)
	}
	manager.PolicyDir, manager.UID = filepath.Join(root, "policies"), "1001"
	manager.Run = func(name string, args ...string) (string, error) {
		if name != "sudo" || len(args) != 7 || args[5] != manager.PolicyDir {
			t.Fatalf("unexpected policy command: %s %v", name, args)
		}
		file := filepath.Join(manager.PolicyDir, "sbc-geolocation-1001.json")
		marker := filepath.Join(manager.PolicyDir, ".sbc-geolocation-1001.owner")
		if args[4] == "on" {
			if err := paths.WriteFile(marker, []byte("sbc-geolocation-v1:1001\n"), 0o644); err != nil {
				return "", err
			}
			return "", paths.WriteFile(file, []byte("{\"DefaultGeolocationSetting\":2}\n"), 0o644)
		}
		if err := os.Remove(file); err != nil && !os.IsNotExist(err) {
			return "", err
		}
		return "", os.Remove(marker)
	}
	env.BrowserGeolocation = func(paths.Layout) (*desktop.ChromeGeolocation, error) { return manager, nil }
	return manager
}

func TestBrowserGeolocationNeedsNoSubscriptionOrDesktop(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	os.Remove(h.layout.ConfigFile())
	h.env.Desktop = func() (desktop.Desktop, error) {
		t.Fatal("geolocation inspected desktop settings")
		return nil, nil
	}
	manager, _ := h.env.BrowserGeolocation(h.layout)
	for _, action := range []string{"on", "off"} {
		if code := Run(h.env, []string{"privacy", "browser", "geolocation", action}); code != 0 {
			t.Fatal(h.errOut)
		}
		status, err := manager.Status()
		if err != nil || status.Enabled != (action == "on") || status.Configured != status.Enabled {
			t.Fatalf("%s: %+v, %v", action, status, err)
		}
	}
	for _, text := range []string{"all accounts", "Restart Chrome", "IP-based location", "Existing website permissions were not changed"} {
		if !strings.Contains(h.out.String(), text) {
			t.Fatalf("missing %q: %s", text, h.out)
		}
	}
}

func TestBrowserGeolocationStatusIsReadOnlyAndDistinguishesSavedChoice(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	manager, _ := h.env.BrowserGeolocation(h.layout)
	if code := Run(h.env, []string{"privacy", "browser", "geolocation", "on"}); code != 0 {
		t.Fatal(h.errOut)
	}
	os.Remove(filepath.Join(manager.PolicyDir, "sbc-geolocation-1001.json"))
	manager.Run = func(string, ...string) (string, error) {
		t.Fatal("status requested sudo")
		return "", nil
	}
	h.out.Reset()
	if code := Run(h.env, []string{"privacy", "browser", "geolocation"}); code != 1 {
		t.Fatalf("missing policy status code: %d", code)
	}
	for _, text := range []string{"saved choice: on", "no local blocking value found", "ownership record: present", "do not prove Chrome's effective policy"} {
		if !strings.Contains(h.out.String(), text) {
			t.Fatalf("missing %q: %s", text, h.out)
		}
	}
}

func TestBrowserGeolocationStatusReportsConflictsWithoutURLValues(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	manager, _ := h.env.BrowserGeolocation(h.layout)
	path := filepath.Join(manager.PolicyDir, "external.json")
	paths.WriteFile(path, []byte(`{"GeolocationAllowedForUrls":["https://private.example.com"]}`), 0o644)
	if code := Run(h.env, []string{"privacy", "browser", "geolocation", "status"}); code != 1 {
		t.Fatalf("conflict status: %d", code)
	}
	if !strings.Contains(h.out.String(), "conflicts") || !strings.Contains(h.out.String(), path) || strings.Contains(h.out.String(), "private.example.com") {
		t.Fatalf("diagnostics: %s", h.out)
	}
}

func TestBrowserCompositeAttemptsAllThreeWhenGeolocationFails(t *testing.T) {
	h := newHarness(t)
	language, _ := browserCLIFixture(t, h)
	privacy := h.useDesktop()
	manager, _ := h.env.BrowserGeolocation(h.layout)
	manager.Run = func(string, ...string) (string, error) { return "", errors.New("fixture geolocation failure") }
	if code := Run(h.env, []string{"privacy", "browser", "on"}); code != 1 {
		t.Fatalf("composite code: %d", code)
	}
	status, err := language.Status()
	if err != nil || status.Current != 1 || len(privacy.privacyCalls) != 1 || !privacy.privacyCalls[0] {
		t.Fatalf("successful components were not retained: %+v, %v", status, err)
	}
	if !strings.Contains(h.errOut.String(), "fixture geolocation failure") {
		t.Fatal("missing component failure")
	}
}

func TestBrowserCompositeEnablesGeolocationWithoutNodeMetadata(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	h.useDesktop()
	if code := Run(h.env, []string{"privacy", "browser", "on"}); code != 1 {
		t.Fatalf("missing language metadata code: %d", code)
	}
	manager, _ := h.env.BrowserGeolocation(h.layout)
	status, err := manager.Status()
	if err != nil || !status.Enabled || !status.Configured {
		t.Fatalf("language failure blocked geolocation: %+v, %v", status, err)
	}
}

func TestProxyTogglesDoNotEnableGeolocation(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	h.useDesktop()
	manager, _ := h.env.BrowserGeolocation(h.layout)
	manager.Run = func(string, ...string) (string, error) {
		t.Fatal("proxy toggle changed geolocation")
		return "", nil
	}
	for _, args := range [][]string{{"desktop", "on"}, {"off"}, {"on"}, {"desktop", "off"}} {
		if code := Run(h.env, args); code != 0 {
			t.Fatalf("%v: %s", args, h.errOut)
		}
	}
	if _, err := os.Stat(manager.StateFile); !os.IsNotExist(err) {
		t.Fatal("proxy toggles saved a geolocation choice")
	}
}

func TestUninstallRetainsClientAfterGeolocationCleanupFailure(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	manager, _ := h.env.BrowserGeolocation(h.layout)
	if code := Run(h.env, []string{"privacy", "browser", "geolocation", "on"}); code != 0 {
		t.Fatal(h.errOut)
	}
	run := manager.Run
	manager.Run = func(string, ...string) (string, error) { return "", errors.New("fixture cleanup denied") }
	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 1 {
		t.Fatalf("uninstall code: %d", code)
	}
	if _, err := os.Stat(manager.StateFile); err != nil || !h.svc.active {
		t.Fatal("failed cleanup removed state or stopped the service")
	}
	if !strings.Contains(h.errOut.String(), "geolocation cleanup failed") {
		t.Fatal(h.errOut)
	}
	manager.Run = run
	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 0 {
		t.Fatal(h.errOut)
	}
	if _, err := os.Stat(filepath.Join(manager.PolicyDir, "sbc-geolocation-1001.json")); !os.IsNotExist(err) {
		t.Fatal("uninstall left the owned policy")
	}
}

func TestBrowserGeolocationIsLinuxOnly(t *testing.T) {
	for _, goos := range []string{"darwin", "windows"} {
		for _, action := range []string{"status", "on", "off"} {
			env, _, errOut := inertEnv(t)
			env.OS = goos
			if code := Run(env, []string{"privacy", "browser", "geolocation", action}); code != 1 || !strings.Contains(errOut.String(), "Linux only") {
				t.Fatalf("%s: %d, %s", goos, code, errOut)
			}
		}
	}
}

func TestBrowserGeolocationChineseMessages(t *testing.T) {
	i18n.SetChinese(true)
	defer i18n.SetChinese(false)
	h := newHarness(t)
	h.env.OS = "linux"
	if code := Run(h.env, []string{"privacy", "browser", "geolocation"}); code != 0 || !strings.Contains(h.out.String(), "定位") || strings.Contains(h.out.String(), "saved choice") {
		t.Fatalf("Chinese status: %d, %s, %s", code, h.out, h.errOut)
	}
}
