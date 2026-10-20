package region

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func testLayout(t *testing.T) paths.Layout {
	t.Helper()
	root := t.TempDir()
	return paths.Layout{
		Config: filepath.Join(root, "config", "sbc"),
		Data:   filepath.Join(root, "data", "sbc"),
	}
}

func testManager(t *testing.T, layout paths.Layout, locales []string) (*Manager, *[]string) {
	t.Helper()
	zoneRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(zoneRoot, "UTC"), []byte("zone"), 0o644); err != nil {
		t.Fatal(err)
	}
	calls := &[]string{}
	env := map[string]string{}
	return &Manager{
		StateFile:        layout.RegionState(),
		ModeFile:         layout.RegionMode(),
		ShellFile:        layout.RegionEnv(),
		EnvironmentDFile: layout.RegionEnvironmentD(),
		ZoneRoots:        []string{zoneRoot},
		Locales:          func() ([]string, error) { return locales, nil },
		Run: func(name string, args ...string) error {
			*calls = append(*calls, name+" "+strings.Join(args, " "))
			return nil
		},
		Getenv:    func(name string) string { return env[name] },
		Supported: true,
	}, calls
}

func TestProfileNormalize(t *testing.T) {
	profile, err := (Profile{Timezone: " UTC ", Language: " zh-cn "}).Normalize()
	if err != nil {
		t.Fatal(err)
	}
	if profile.Timezone != "UTC" || profile.Language != "zh-CN" {
		t.Fatalf("unexpected profile: %#v", profile)
	}
	locale, err := Locale(profile.Language)
	if err != nil || locale != "zh_CN.UTF-8" {
		t.Fatalf("locale = %q, %v", locale, err)
	}
	list, err := LanguageList(profile.Language)
	if err != nil || list != "zh_CN:zh" {
		t.Fatalf("language list = %q, %v", list, err)
	}
}

func TestProfileNormalizeRejectsUnsafeValues(t *testing.T) {
	for _, profile := range []Profile{
		{Timezone: "", Language: "zh-CN"},
		{Timezone: "../etc/passwd", Language: "zh-CN"},
		{Timezone: "/etc/passwd", Language: "zh-CN"},
		{Timezone: "Asia/../UTC", Language: "zh-CN"},
		{Timezone: "Asia/./Shanghai", Language: "zh-CN"},
		{Timezone: "posix/UTC", Language: "zh-CN"},
		{Timezone: "right/UTC", Language: "zh-CN"},
		{Timezone: "localtime", Language: "zh-CN"},
		{Timezone: "UTC", Language: "zh"},
		{Timezone: "UTC", Language: "zh-Hans"},
		{Timezone: "UTC", Language: "zh_CN"},
	} {
		if _, err := profile.Normalize(); err == nil {
			t.Fatalf("accepted unsafe profile %#v", profile)
		}
	}
}

func TestApplyWritesBothComponents(t *testing.T) {
	layout := testLayout(t)
	manager, calls := testManager(t, layout, []string{"C.utf8", "zh_CN.utf8"})

	result, err := manager.Apply(
		State{Node: "node.example", Profile: Profile{Timezone: "UTC", Language: "zh-CN"}},
		Mode{Timezone: true, Language: true},
	)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Mode.Timezone || !result.Mode.Language {
		t.Fatalf("nothing applied: %#v", result)
	}
	shellData, err := os.ReadFile(layout.RegionEnv())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(shellData), "export TZ='UTC'") ||
		!strings.Contains(string(shellData), "export LANG='zh_CN.UTF-8'") ||
		!strings.Contains(string(shellData), "export LANGUAGE='zh_CN:zh'") {
		t.Fatalf("unexpected shell file:\n%s", shellData)
	}
	environmentData, err := os.ReadFile(layout.RegionEnvironmentD())
	if err != nil {
		t.Fatal(err)
	}
	if string(environmentData) != "TZ=UTC\nLANG=zh_CN.UTF-8\nLANGUAGE=zh_CN:zh\nLC_MESSAGES=zh_CN.UTF-8\n" {
		t.Fatalf("unexpected environment.d file:\n%s", environmentData)
	}
	joined := strings.Join(*calls, "\n")
	if !strings.Contains(joined, "systemctl --user set-environment TZ=UTC LANG=zh_CN.UTF-8 LANGUAGE=zh_CN:zh LC_MESSAGES=zh_CN.UTF-8") {
		t.Fatalf("set-environment not called:\n%s", joined)
	}

	status, err := manager.Status()
	if err != nil {
		t.Fatal(err)
	}
	if !status.FilesCurrent || !status.TimezoneAvailable || !status.LanguageAvailable {
		t.Fatalf("unexpected status: %#v", status)
	}
}

func TestApplyKeepsTimezoneWhenLanguageIsMissing(t *testing.T) {
	layout := testLayout(t)
	manager, _ := testManager(t, layout, []string{"C.utf8", "en_US.utf8"})

	result, err := manager.Apply(
		State{Node: "node.example", Profile: Profile{Timezone: "UTC", Language: "zh-CN"}},
		Mode{Timezone: true, Language: true},
	)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Mode.Timezone || result.Mode.Language {
		t.Fatalf("unexpected mode: %#v", result.Mode)
	}
	if result.LanguageError == nil {
		t.Fatal("expected a language error")
	}
	shellData, _ := os.ReadFile(layout.RegionEnv())
	if !strings.Contains(string(shellData), "export TZ='UTC'") {
		t.Fatalf("timezone missing:\n%s", shellData)
	}
	if !strings.Contains(string(shellData), "unset _sbc_region_lang") {
		t.Fatalf("language was not cleared:\n%s", shellData)
	}
	status, err := manager.Status()
	if err != nil {
		t.Fatal(err)
	}
	if status.LanguageAvailable || status.LanguageError == nil {
		t.Fatalf("unexpected status: %#v", status)
	}
}

func TestApplyKeepsLanguageWhenTimezoneIsMissing(t *testing.T) {
	layout := testLayout(t)
	manager, _ := testManager(t, layout, []string{"C.utf8", "zh_CN.utf8"})
	manager.ZoneRoots = []string{t.TempDir()}

	result, err := manager.Apply(
		State{Profile: Profile{Timezone: "UTC", Language: "zh-CN"}},
		Mode{Timezone: true, Language: true},
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.TimezoneError == nil || result.Mode.Timezone || !result.Mode.Language {
		t.Fatalf("unexpected result: %#v", result)
	}
	environmentData, _ := os.ReadFile(layout.RegionEnvironmentD())
	if strings.Contains(string(environmentData), "TZ=") || !strings.Contains(string(environmentData), "LANG=zh_CN.UTF-8") {
		t.Fatalf("unexpected environment.d file:\n%s", environmentData)
	}
}

func TestDisableKeepsTheCachedProfileAndRemovesTheChoice(t *testing.T) {
	layout := testLayout(t)
	manager, calls := testManager(t, layout, []string{"C.utf8", "zh_CN.utf8"})
	if _, err := manager.Apply(
		State{Node: "node.example", Profile: Profile{Timezone: "UTC", Language: "zh-CN"}},
		Mode{Timezone: true, Language: true},
	); err != nil {
		t.Fatal(err)
	}
	*calls = nil
	if _, err := manager.Disable(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(layout.RegionMode()); !os.IsNotExist(err) {
		t.Fatal("mode file remains")
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
	joined := strings.Join(*calls, "\n")
	if !strings.Contains(joined, "unset-environment TZ LANG LANGUAGE LC_MESSAGES") {
		t.Fatalf("unset-environment not called:\n%s", joined)
	}
	state, mode, err := manager.Load()
	if err != nil {
		t.Fatal(err)
	}
	if state == nil || state.Node != "node.example" || mode.Any() {
		t.Fatalf("unexpected saved state: %#v %#v", state, mode)
	}
}

func TestRemoveDeletesTheCache(t *testing.T) {
	layout := testLayout(t)
	manager, _ := testManager(t, layout, []string{"C.utf8"})
	if _, err := manager.Apply(
		State{Profile: Profile{Timezone: "UTC", Language: "en-US"}},
		Mode{Timezone: true},
	); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.Remove(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(layout.RegionState()); !os.IsNotExist(err) {
		t.Fatal("cached profile remains")
	}
}

func TestStatusReportsAStaleFileSet(t *testing.T) {
	layout := testLayout(t)
	manager, _ := testManager(t, layout, []string{"C.utf8"})
	if _, err := manager.Apply(
		State{Profile: Profile{Timezone: "UTC", Language: "en-US"}},
		Mode{Timezone: true, Language: true},
	); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(layout.RegionEnv(), []byte("export TZ='Europe/Berlin'\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	status, err := manager.Status()
	if err != nil {
		t.Fatal(err)
	}
	if status.FilesCurrent {
		t.Fatal("stale shell file reported as current")
	}
}

func TestModeWithNoComponentsRemovesFiles(t *testing.T) {
	layout := testLayout(t)
	manager, _ := testManager(t, layout, []string{"C.utf8"})
	if _, err := manager.Apply(
		State{Profile: Profile{Timezone: "UTC", Language: "en-US"}},
		Mode{Timezone: true, Language: true},
	); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.Apply(
		State{Profile: Profile{Timezone: "UTC", Language: "en-US"}},
		Mode{},
	); err != nil {
		t.Fatal(err)
	}
	if fileExists(layout.RegionEnv()) || fileExists(layout.RegionEnvironmentD()) {
		t.Fatal("files remain after clearing the mode")
	}
}

func TestApplyTimezoneOnlyLeavesTheUserManagerLanguageAlone(t *testing.T) {
	layout := testLayout(t)
	manager, calls := testManager(t, layout, []string{"C.utf8", "en_US.utf8"})
	if _, err := manager.Apply(
		State{Profile: Profile{Timezone: "UTC", Language: "en-US"}},
		Mode{Timezone: true},
	); err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(*calls, "\n")
	if !strings.Contains(joined, "set-environment TZ=UTC") {
		t.Fatalf("timezone was not set:\n%s", joined)
	}
	if strings.Contains(joined, "unset-environment TZ LANG") || strings.Contains(joined, "unset-environment LANG") {
		t.Fatalf("language was cleared even though sbc never set it:\n%s", joined)
	}
}

func TestTurningOffLanguageClearsOnlyLanguage(t *testing.T) {
	layout := testLayout(t)
	manager, calls := testManager(t, layout, []string{"C.utf8", "en_US.utf8"})
	if _, err := manager.Apply(
		State{Profile: Profile{Timezone: "UTC", Language: "en-US"}},
		Mode{Timezone: true, Language: true},
	); err != nil {
		t.Fatal(err)
	}
	*calls = nil
	if _, err := manager.Apply(
		State{Profile: Profile{Timezone: "UTC", Language: "en-US"}},
		Mode{Timezone: true},
	); err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(*calls, "\n")
	if !strings.Contains(joined, "set-environment TZ=UTC") ||
		!strings.Contains(joined, "unset-environment LANG LANGUAGE LC_MESSAGES") {
		t.Fatalf("unexpected calls:\n%s", joined)
	}
	if strings.Contains(joined, "unset-environment TZ") {
		t.Fatalf("timezone was cleared:\n%s", joined)
	}
}

func TestDisableClearsOnlyComponentsThatWereOn(t *testing.T) {
	layout := testLayout(t)
	manager, calls := testManager(t, layout, []string{"C.utf8", "en_US.utf8"})
	if _, err := manager.Apply(
		State{Profile: Profile{Timezone: "UTC", Language: "en-US"}},
		Mode{Timezone: true},
	); err != nil {
		t.Fatal(err)
	}
	*calls = nil
	if _, err := manager.Disable(); err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(*calls, "\n")
	if !strings.Contains(joined, "unset-environment TZ") {
		t.Fatalf("timezone was not cleared:\n%s", joined)
	}
	if strings.Contains(joined, "LANG") {
		t.Fatalf("language was cleared even though it was off:\n%s", joined)
	}
}
