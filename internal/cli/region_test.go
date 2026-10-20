package cli

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/region"
)

func regionLayout(t *testing.T) paths.Layout {
	t.Helper()
	root := t.TempDir()
	return paths.Layout{
		Config: filepath.Join(root, "config", "sbc"),
		Data:   filepath.Join(root, "data", "sbc"),
	}
}

func regionTestManager(t *testing.T, layout paths.Layout, locales []string) *region.Manager {
	t.Helper()
	zoneRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(zoneRoot, "UTC"), []byte("zone"), 0o644); err != nil {
		t.Fatal(err)
	}
	return &region.Manager{
		StateFile:        layout.RegionState(),
		ModeFile:         layout.RegionMode(),
		ShellFile:        layout.RegionEnv(),
		EnvironmentDFile: layout.RegionEnvironmentD(),
		ZoneRoots:        []string{zoneRoot},
		Locales:          func() ([]string, error) { return locales, nil },
		Run:              func(string, ...string) error { return nil },
		Getenv:           func(string) string { return "" },
		Supported:        true,
	}
}

func regionTestEnv(t *testing.T, layout paths.Layout, manager *region.Manager) (Env, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	var out, errOut bytes.Buffer
	env := DefaultEnv("1.2.3", strings.NewReader(""), &out, &errOut)
	env.OS = "linux"
	env.Layout = func() (paths.Layout, error) { return layout, nil }
	env.Region = func(paths.Layout) *region.Manager { return manager }
	isolateBrowserLanguage(t, &env, layout)
	system := newSystemCLIFixture(t)
	env.SystemRegion = func() region.SystemPaths { return system.paths }
	env.Sudo = nil
	return env, &out, &errOut
}

func TestPrivacyRegionOnAppliesTheCachedProfile(t *testing.T) {
	layout := regionLayout(t)
	manager := regionTestManager(t, layout, []string{"C.utf8", "zh_CN.utf8"})
	if err := manager.StoreState(region.State{
		Node:    "node.example",
		Profile: region.Profile{Timezone: "UTC", Language: "zh-CN"},
	}); err != nil {
		t.Fatal(err)
	}
	env, out, errOut := regionTestEnv(t, layout, manager)

	if code := Run(env, []string{"privacy", "region", "on"}); code != 0 {
		t.Fatalf("code %d, stderr %q", code, errOut.String())
	}
	if !strings.Contains(out.String(), "The time zone is now set") ||
		!strings.Contains(out.String(), "The language is now set") {
		t.Fatalf("stdout %q", out.String())
	}
	shellData, err := os.ReadFile(layout.RegionEnv())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(shellData), "export TZ='UTC'") ||
		!strings.Contains(string(shellData), "export LANG='zh_CN.UTF-8'") {
		t.Fatalf("shell file:\n%s", shellData)
	}
}

func TestPrivacyRegionOnKeepsTimezoneWhenTheLocaleIsMissing(t *testing.T) {
	layout := regionLayout(t)
	manager := regionTestManager(t, layout, []string{"C.utf8"})
	if err := manager.StoreState(region.State{
		Profile: region.Profile{Timezone: "UTC", Language: "zh-CN"},
	}); err != nil {
		t.Fatal(err)
	}
	env, out, errOut := regionTestEnv(t, layout, manager)

	if code := Run(env, []string{"privacy", "region", "on"}); code != 1 {
		t.Fatalf("code %d, stdout %q, stderr %q", code, out.String(), errOut.String())
	}
	if !strings.Contains(errOut.String(), "not generated") {
		t.Fatalf("stderr %q", errOut.String())
	}
	shellData, err := os.ReadFile(layout.RegionEnv())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(shellData), "export TZ='UTC'") ||
		!strings.Contains(string(shellData), "unset _sbc_region_lang") {
		t.Fatalf("shell file:\n%s", shellData)
	}
}

func TestPrivacyRegionOffRemovesTheChoiceAndKeepsTheProfile(t *testing.T) {
	layout := regionLayout(t)
	manager := regionTestManager(t, layout, []string{"C.utf8", "zh_CN.utf8"})
	if _, err := manager.Apply(region.State{
		Node:    "node.example",
		Profile: region.Profile{Timezone: "UTC", Language: "zh-CN"},
	}, region.Mode{Timezone: true, Language: true}); err != nil {
		t.Fatal(err)
	}
	env, out, errOut := regionTestEnv(t, layout, manager)

	if code := Run(env, []string{"privacy", "region", "off"}); code != 0 {
		t.Fatalf("code %d, stderr %q", code, errOut.String())
	}
	if !strings.Contains(out.String(), "Region matching is off") {
		t.Fatalf("stdout %q", out.String())
	}
	if _, err := os.Stat(layout.RegionEnv()); !os.IsNotExist(err) {
		t.Fatal("shell file remains")
	}
	if _, err := os.Stat(layout.RegionEnvironmentD()); !os.IsNotExist(err) {
		t.Fatal("environment.d file remains")
	}
	if _, err := os.Stat(layout.RegionState()); err != nil {
		t.Fatalf("cached profile was removed: %v", err)
	}
}

func TestPrivacyStatusShowsTheTargetAndFiles(t *testing.T) {
	layout := regionLayout(t)
	manager := regionTestManager(t, layout, []string{"C.utf8", "zh_CN.utf8"})
	if _, err := manager.Apply(region.State{
		Node:    "node.example",
		Profile: region.Profile{Timezone: "UTC", Language: "zh-CN"},
	}, region.Mode{Timezone: true, Language: true}); err != nil {
		t.Fatal(err)
	}
	env, out, errOut := regionTestEnv(t, layout, manager)

	if code := Run(env, []string{"privacy", "region"}); code != 0 {
		t.Fatalf("code %d, stderr %q", code, errOut.String())
	}
	text := out.String()
	for _, want := range []string{"Region: on", "Node: node.example", "Time zone: available", "Files: current"} {
		if !strings.Contains(text, want) {
			t.Errorf("stdout %q does not contain %q", text, want)
		}
	}
}

func TestPrivacyRegionIsLinuxOnly(t *testing.T) {
	layout := regionLayout(t)
	manager := regionTestManager(t, layout, []string{"C.utf8"})
	env, _, errOut := regionTestEnv(t, layout, manager)
	env.OS = "darwin"

	if code := Run(env, []string{"privacy", "region", "on"}); code != 1 {
		t.Fatalf("code %d", code)
	}
	if !strings.Contains(errOut.String(), "Linux only") {
		t.Fatalf("stderr %q", errOut.String())
	}
}

func TestRefreshKeepsTheLastProfileWhenThePortalOmitsTheRegion(t *testing.T) {
	layout := regionLayout(t)
	manager := regionTestManager(t, layout, []string{"C.utf8", "zh_CN.utf8"})
	if _, err := manager.Apply(region.State{
		Node:    "node.example",
		Profile: region.Profile{Timezone: "UTC", Language: "zh-CN"},
	}, region.Mode{Timezone: true, Language: true}); err != nil {
		t.Fatal(err)
	}
	env, _, errOut := regionTestEnv(t, layout, manager)

	refreshRegion(env, layout, &install.Envelope{}, false)

	if !strings.Contains(errOut.String(), "keeping the last saved profile") {
		t.Fatalf("stderr %q", errOut.String())
	}
	if _, err := os.Stat(layout.RegionEnv()); err != nil {
		t.Fatalf("shell file was removed: %v", err)
	}
}

func TestLinkRefreshDisablesRegionWhenThePortalOmitsIt(t *testing.T) {
	layout := regionLayout(t)
	manager := regionTestManager(t, layout, []string{"C.utf8", "zh_CN.utf8"})
	if _, err := manager.Apply(region.State{
		Node:    "node.example",
		Profile: region.Profile{Timezone: "UTC", Language: "zh-CN"},
	}, region.Mode{Timezone: true, Language: true}); err != nil {
		t.Fatal(err)
	}
	env, out, errOut := regionTestEnv(t, layout, manager)

	refreshRegion(env, layout, &install.Envelope{}, true)

	if !strings.Contains(out.String(), "region matching is off") {
		t.Fatalf("stdout %q, stderr %q", out.String(), errOut.String())
	}
	if _, err := os.Stat(layout.RegionEnv()); !os.IsNotExist(err) {
		t.Fatal("shell file remains")
	}
	if _, err := os.Stat(layout.RegionState()); !os.IsNotExist(err) {
		t.Fatal("cached profile remains")
	}
}

func TestRegionForFollowsTheEnvPlatform(t *testing.T) {
	layout := regionLayout(t)
	env := DefaultEnv("1.2.3", strings.NewReader(""), &bytes.Buffer{}, &bytes.Buffer{})
	env.OS = "linux"
	env.Region = nil
	if manager := regionFor(env, layout); !manager.Supported {
		t.Fatal("forced Linux environment produced an unsupported region manager")
	}
	env.OS = "darwin"
	if manager := regionFor(env, layout); manager.Supported {
		t.Fatal("darwin environment produced a supported region manager")
	}
}
