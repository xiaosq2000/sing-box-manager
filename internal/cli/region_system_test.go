package cli

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/region"
)

type systemCLIFixture struct {
	layout    paths.Layout
	manager   *region.Manager
	paths     region.SystemPaths
	zoneRoot  string
	locales   []string
	sudoCalls []string
}

func newSystemCLIFixture(t *testing.T) *systemCLIFixture {
	t.Helper()
	root := t.TempDir()
	zoneRoot := filepath.Join(root, "zoneinfo")
	for _, zone := range []string{"Asia/Shanghai", "Asia/Singapore", "Europe/Berlin", "UTC"} {
		path := filepath.Join(zoneRoot, zone)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(zone), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	etc := filepath.Join(root, "etc")
	if err := os.MkdirAll(filepath.Join(etc, "default"), 0o755); err != nil {
		t.Fatal(err)
	}
	paths := region.SystemPaths{
		Localtime:     filepath.Join(etc, "localtime"),
		Timezone:      filepath.Join(etc, "timezone"),
		LocaleDefault: filepath.Join(etc, "default", "locale"),
		LocaleConf:    filepath.Join(etc, "locale.conf"),
		ZoneRoot:      zoneRoot,
		StateDir:      filepath.Join(root, "state"),
		StateFile:     filepath.Join(root, "state", "state.json"),
		BackupDir:     filepath.Join(root, "state", "backup"),
	}
	tmp := paths.Localtime + ".tmp"
	if err := os.Symlink(filepath.Join(zoneRoot, "Asia/Singapore"), tmp); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, paths.Localtime); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(paths.LocaleDefault, []byte("LANG=en_US.UTF-8\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	layout := systemLayout(root)
	manager := systemManager(layout, zoneRoot)
	return &systemCLIFixture{
		layout:   layout,
		manager:  manager,
		paths:    paths,
		zoneRoot: zoneRoot,
		locales:  []string{"C.utf8", "en_US.utf8", "zh_CN.utf8", "de_DE.utf8"},
	}
}

func systemLayout(root string) paths.Layout {
	return paths.Layout{Config: filepath.Join(root, "config", "sbc"), Data: filepath.Join(root, "data", "sbc")}
}

func systemManager(layout paths.Layout, zoneRoot string) *region.Manager {
	return &region.Manager{
		StateFile:        layout.RegionState(),
		ModeFile:         layout.RegionMode(),
		ShellFile:        layout.RegionEnv(),
		EnvironmentDFile: layout.RegionEnvironmentD(),
		ZoneRoots:        []string{zoneRoot},
		Locales:          func() ([]string, error) { return []string{"C.utf8", "en_US.utf8", "zh_CN.utf8", "de_DE.utf8"}, nil },
		Run:              func(string, ...string) error { return nil },
		Getenv:           func(string) string { return "" },
		Supported:        true,
	}
}

func (f *systemCLIFixture) tool(name string, args ...string) error {
	switch filepath.Base(name) {
	case "timedatectl":
		zone := args[len(args)-1]
		tmp := f.paths.Localtime + ".tmp"
		_ = os.Remove(tmp)
		target, err := filepath.Rel(filepath.Dir(f.paths.Localtime), filepath.Join(f.zoneRoot, zone))
		if err != nil {
			return err
		}
		if err := os.Symlink(target, tmp); err != nil {
			return err
		}
		return os.Rename(tmp, f.paths.Localtime)
	case "update-locale":
		var file string
		var values []string
		for _, arg := range args {
			if strings.HasPrefix(arg, "--locale-file=") {
				file = strings.TrimPrefix(arg, "--locale-file=")
			} else {
				values = append(values, arg)
			}
		}
		return os.WriteFile(file, []byte(strings.Join(values, "\n")+"\n"), 0o644)
	default:
		return fmt.Errorf("unexpected tool %s", name)
	}
}

func (f *systemCLIFixture) sudo(_ string, args []string) error {
	f.sudoCalls = append(f.sudoCalls, strings.Join(args, " "))
	if len(args) == 0 {
		return errors.New("missing hidden action")
	}
	switch args[0] {
	case "_region-system-on":
		timezone, language := "", ""
		timezoneOn, languageOn := false, false
		for i := 1; i < len(args); i++ {
			switch args[i] {
			case "--timezone-value":
				timezone = args[i+1]
				i++
			case "--language-value":
				language = args[i+1]
				i++
			case "--timezone":
				timezoneOn = true
			case "--language":
				languageOn = true
			}
		}
		if !timezoneOn && !languageOn {
			timezoneOn, languageOn = true, true
		}
		result, err := region.SystemApply(region.SystemApplyOptions{
			Paths:    f.paths,
			Profile:  region.Profile{Timezone: timezone, Language: language},
			Timezone: timezoneOn,
			Language: languageOn,
			Run:      f.tool,
			LookPath: func(name string) (string, error) { return "/usr/bin/" + name, nil },
			Locales:  func() ([]string, error) { return f.locales, nil },
			Euid:     func() int { return 0 },
		})
		if err != nil {
			return err
		}
		if result.TimezoneError != nil {
			return result.TimezoneError
		}
		return result.LanguageError
	case "_region-system-off":
		timezone, language := false, false
		for _, arg := range args[1:] {
			if arg == "--timezone" {
				timezone = true
			}
			if arg == "--language" {
				language = true
			}
		}
		result, err := region.SystemDisable(region.SystemDisableOptions{
			Paths:    f.paths,
			Timezone: timezone,
			Language: language,
			Euid:     func() int { return 0 },
		})
		if err != nil {
			return err
		}
		if result.TimezoneError != nil {
			return result.TimezoneError
		}
		return result.LanguageError
	default:
		return fmt.Errorf("unknown hidden action %s", args[0])
	}
}

func (f *systemCLIFixture) env(t *testing.T) (Env, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	var out, errOut bytes.Buffer
	env := DefaultEnv("1.2.3", strings.NewReader(""), &out, &errOut)
	env.OS = "linux"
	env.Layout = func() (paths.Layout, error) { return f.layout, nil }
	env.Region = func(paths.Layout) *region.Manager { return f.manager }
	env.SystemRegion = func() region.SystemPaths { return f.paths }
	env.Sudo = f.sudo
	env.Self = func() (string, error) { return "/usr/bin/sbc", nil }
	return env, &out, &errOut
}

func (f *systemCLIFixture) storeProfile(t *testing.T) {
	t.Helper()
	if err := f.manager.StoreState(region.State{
		Node:    "node.example",
		Profile: region.Profile{Timezone: "Asia/Shanghai", Language: "zh-CN"},
	}); err != nil {
		t.Fatal(err)
	}
}

func TestPrivacyRegionSystemOn(t *testing.T) {
	f := newSystemCLIFixture(t)
	f.storeProfile(t)
	env, out, errOut := f.env(t)

	if code := Run(env, []string{"privacy", "region", "on", "--system", "--yes"}); code != 0 {
		t.Fatalf("code %d, stdout %q, stderr %q", code, out.String(), errOut.String())
	}
	if len(f.sudoCalls) != 1 || !strings.Contains(f.sudoCalls[0], "_region-system-on") {
		t.Fatalf("sudo calls %v", f.sudoCalls)
	}
	mode, err := f.manager.LoadMode()
	if err != nil {
		t.Fatal(err)
	}
	if mode.EffectiveScope() != region.ScopeSystem || !mode.Timezone || !mode.Language {
		t.Fatalf("mode = %#v", mode)
	}
	target, err := filepath.EvalSymlinks(f.paths.Localtime)
	if err != nil || target != filepath.Join(f.zoneRoot, "Asia/Shanghai") {
		t.Fatalf("localtime = %q, %v", target, err)
	}
	data, _ := os.ReadFile(f.paths.LocaleDefault)
	if !strings.Contains(string(data), "LANG=zh_CN.UTF-8") {
		t.Fatalf("locale file = %q", data)
	}
	out.Reset()
	if code := Run(env, []string{"privacy", "region"}); code != 0 {
		t.Fatalf("status code %d, stderr %q", code, errOut.String())
	}
	if !strings.Contains(out.String(), "(system)") || !strings.Contains(out.String(), "System time zone: current") {
		t.Fatalf("status = %q", out.String())
	}
}

func TestPrivacyRegionSystemRequiresYes(t *testing.T) {
	f := newSystemCLIFixture(t)
	f.storeProfile(t)
	env, _, errOut := f.env(t)

	if code := Run(env, []string{"privacy", "region", "on", "--system"}); code != 1 {
		t.Fatalf("code %d, stderr %q", code, errOut.String())
	}
	if len(f.sudoCalls) != 0 {
		t.Fatalf("sudo ran without --yes: %v", f.sudoCalls)
	}
	if !strings.Contains(errOut.String(), "--yes") {
		t.Fatalf("stderr %q", errOut.String())
	}
}

func TestPrivacyRegionSystemRejectsScopeSwitch(t *testing.T) {
	f := newSystemCLIFixture(t)
	f.storeProfile(t)
	if _, err := f.manager.Apply(region.State{
		Profile: region.Profile{Timezone: "Asia/Shanghai", Language: "zh-CN"},
	}, region.Mode{Timezone: true, Language: true}); err != nil {
		t.Fatal(err)
	}
	env, _, errOut := f.env(t)

	if code := Run(env, []string{"privacy", "region", "on", "--system", "--yes"}); code != 1 {
		t.Fatalf("code %d, stderr %q", code, errOut.String())
	}
	if len(f.sudoCalls) != 0 {
		t.Fatalf("sudo ran during a scope conflict: %v", f.sudoCalls)
	}
	if !strings.Contains(errOut.String(), "before using --system") {
		t.Fatalf("stderr %q", errOut.String())
	}
}

func TestPrivacyRegionSystemOff(t *testing.T) {
	f := newSystemCLIFixture(t)
	f.storeProfile(t)
	env, out, errOut := f.env(t)
	if code := Run(env, []string{"privacy", "region", "on", "--system", "--yes"}); code != 0 {
		t.Fatalf("on: code %d, stderr %q", code, errOut.String())
	}
	out.Reset()
	if code := Run(env, []string{"privacy", "region", "off", "--yes"}); code != 0 {
		t.Fatalf("off: code %d, stdout %q, stderr %q", code, out.String(), errOut.String())
	}
	target, err := os.Readlink(f.paths.Localtime)
	if err != nil || target != filepath.Join(f.zoneRoot, "Asia/Singapore") {
		t.Fatalf("localtime = %q, %v", target, err)
	}
	data, _ := os.ReadFile(f.paths.LocaleDefault)
	if string(data) != "LANG=en_US.UTF-8\n" {
		t.Fatalf("locale file = %q", data)
	}
	if _, err := os.Stat(f.manager.ModeFile); !os.IsNotExist(err) {
		t.Fatal("mode file remains")
	}
	if _, err := os.Stat(f.paths.StateFile); !os.IsNotExist(err) {
		t.Fatal("system state remains")
	}
}

func TestRefreshSystemRegionDoesNotUseSudo(t *testing.T) {
	f := newSystemCLIFixture(t)
	f.storeProfile(t)
	env, out, errOut := f.env(t)
	if code := Run(env, []string{"privacy", "region", "on", "--system", "--yes"}); code != 0 {
		t.Fatalf("on: code %d, stderr %q", code, errOut.String())
	}
	f.sudoCalls = nil
	out.Reset()
	envelope := &install.Envelope{Nodes: []install.Node{{
		Name:   "node.example",
		Region: &region.Profile{Timezone: "Europe/Berlin", Language: "de-DE"},
	}}}
	refreshRegion(env, f.layout, envelope, false)
	if len(f.sudoCalls) != 0 {
		t.Fatalf("refresh used sudo: %v", f.sudoCalls)
	}
	state, _, err := f.manager.Load()
	if err != nil {
		t.Fatal(err)
	}
	if state.Pending != region.PendingApply {
		t.Fatalf("pending = %q, state %#v", state.Pending, state)
	}
	if !strings.Contains(out.String(), "sbc privacy region on") {
		t.Fatalf("stdout %q", out.String())
	}
}

func TestUninstallRestoresSystemRegion(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	f := newSystemCLIFixture(t)
	f.manager = systemManager(h.layout, f.zoneRoot)
	f.layout = h.layout
	f.storeProfile(t)
	h.env.Region = func(paths.Layout) *region.Manager { return f.manager }
	h.env.SystemRegion = func() region.SystemPaths { return f.paths }
	h.env.Sudo = f.sudo
	h.env.Self = func() (string, error) { return "/usr/bin/sbc", nil }
	if code := Run(h.env, []string{"privacy", "region", "on", "--system", "--yes"}); code != 0 {
		t.Fatalf("on: code %d, stderr %q", code, h.errOut.String())
	}
	f.sudoCalls = nil
	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 0 {
		t.Fatalf("uninstall: code %d, stdout %q, stderr %q", code, h.out.String(), h.errOut.String())
	}
	if len(f.sudoCalls) != 1 || !strings.Contains(f.sudoCalls[0], "_region-system-off") {
		t.Fatalf("sudo calls %v", f.sudoCalls)
	}
	if _, err := os.Stat(f.paths.StateFile); !os.IsNotExist(err) {
		t.Fatal("system state remains after uninstall")
	}
	if target, err := os.Readlink(f.paths.Localtime); err != nil || target != filepath.Join(f.zoneRoot, "Asia/Singapore") {
		t.Fatalf("localtime = %q, %v", target, err)
	}
}

func TestStoreInstallSystemRegionDoesNotUseSudo(t *testing.T) {
	f := newSystemCLIFixture(t)
	f.storeProfile(t)
	env, out, errOut := f.env(t)
	if code := Run(env, []string{"privacy", "region", "on", "--system", "--yes"}); code != 0 {
		t.Fatalf("on: code %d, stderr %q", code, errOut.String())
	}
	f.sudoCalls = nil
	out.Reset()
	envelope := &install.Envelope{Nodes: []install.Node{{
		Name:   "node.example",
		Region: &region.Profile{Timezone: "Europe/Berlin", Language: "zh-CN"},
	}}}
	storeInstallRegion(env, f.layout, envelope)
	if len(f.sudoCalls) != 0 {
		t.Fatalf("install used sudo: %v", f.sudoCalls)
	}
	state, _, err := f.manager.Load()
	if err != nil {
		t.Fatal(err)
	}
	if state.Pending != region.PendingApply {
		t.Fatalf("pending = %q, state %#v", state.Pending, state)
	}
	if !strings.Contains(out.String(), "system region target changed") {
		t.Fatalf("stdout %q", out.String())
	}
}

func TestRefreshSystemRegionMarksRemoval(t *testing.T) {
	f := newSystemCLIFixture(t)
	f.storeProfile(t)
	env, out, errOut := f.env(t)
	if code := Run(env, []string{"privacy", "region", "on", "--system", "--yes"}); code != 0 {
		t.Fatalf("on: code %d, stderr %q", code, errOut.String())
	}
	f.sudoCalls = nil
	out.Reset()
	refreshRegion(env, f.layout, &install.Envelope{}, true)
	if len(f.sudoCalls) != 0 {
		t.Fatalf("refresh used sudo: %v", f.sudoCalls)
	}
	state, _, err := f.manager.Load()
	if err != nil {
		t.Fatal(err)
	}
	if state.Pending != region.PendingRemove {
		t.Fatalf("pending = %q, state %#v", state.Pending, state)
	}
	if !strings.Contains(out.String(), "restore the system settings") {
		t.Fatalf("stdout %q", out.String())
	}
	if _, err := os.Stat(f.paths.StateFile); err != nil {
		t.Fatalf("system state was removed: %v", err)
	}
}

func TestPrivacyRegionSystemClearsStaleUserFiles(t *testing.T) {
	f := newSystemCLIFixture(t)
	f.storeProfile(t)
	if _, err := f.manager.Apply(region.State{
		Node:    "node.example",
		Profile: region.Profile{Timezone: "Asia/Shanghai", Language: "zh-CN"},
	}, region.Mode{Timezone: true, Language: true}); err != nil {
		t.Fatal(err)
	}
	// A lost mode file leaves the account-level files behind.
	if err := os.Remove(f.manager.ModeFile); err != nil {
		t.Fatal(err)
	}
	env, _, errOut := f.env(t)
	if code := Run(env, []string{"privacy", "region", "on", "--system", "--yes"}); code != 0 {
		t.Fatalf("code %d, stderr %q", code, errOut.String())
	}
	if _, err := os.Stat(f.manager.ShellFile); !os.IsNotExist(err) {
		t.Fatal("shell file remains")
	}
	if _, err := os.Stat(f.manager.EnvironmentDFile); !os.IsNotExist(err) {
		t.Fatal("environment.d file remains")
	}
	mode, err := f.manager.LoadMode()
	if err != nil {
		t.Fatal(err)
	}
	if !mode.System() || !mode.Timezone || !mode.Language {
		t.Fatalf("mode = %#v", mode)
	}
}
