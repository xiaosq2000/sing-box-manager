package cli

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

// languageHarness isolates the language file and the variables that select a
// language, so a test never reads this machine's settings.
func languageHarness(t *testing.T) (Env, *bytes.Buffer, *bytes.Buffer, paths.Layout) {
	t.Helper()
	root := t.TempDir()
	layout := paths.Layout{Config: filepath.Join(root, "config"), Data: filepath.Join(root, "data")}
	out, errOut := &bytes.Buffer{}, &bytes.Buffer{}
	env := Env{
		Version: "1.2.3", OS: runtime.GOOS, Stdin: strings.NewReader(""),
		Stdout: out, Stderr: errOut,
		Layout: func() (paths.Layout, error) { return layout, nil },
	}
	t.Setenv("SBC_LANG", "")
	t.Setenv("LC_ALL", "")
	t.Setenv("LC_MESSAGES", "")
	t.Setenv("LANG", "en_US.UTF-8")
	i18n.SetChinese(false)
	t.Cleanup(func() { i18n.SetChinese(false) })
	return env, out, errOut, layout
}

func TestLangShowsSavesAndClearsTheChoice(t *testing.T) {
	env, out, errOut, layout := languageHarness(t)

	runLangCommand := func(args ...string) (string, string) {
		t.Helper()
		out.Reset()
		errOut.Reset()
		if code := Run(env, args); code != 0 {
			t.Fatalf("%v: code %d, stderr %q", args, code, errOut.String())
		}
		return out.String(), errOut.String()
	}

	got, stderr := runLangCommand("lang")
	if got != "Language: English (locale)\n" || stderr != "" {
		t.Fatalf("initial query: stdout %q, stderr %q", got, stderr)
	}

	got, _ = runLangCommand("lang", "zh")
	if got != "CLI 语言已设置为简体中文。\n" {
		t.Fatalf("set Chinese: %q", got)
	}
	data, err := os.ReadFile(layout.LanguageFile())
	if err != nil || string(data) != "zh\n" {
		t.Fatalf("saved language %q, error %v", data, err)
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(layout.LanguageFile())
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("mode %v", info.Mode().Perm())
		}
	}

	got, _ = runLangCommand("lang")
	if got != "语言：    简体中文（已保存设置）\n" {
		t.Fatalf("Chinese query: %q", got)
	}

	got, _ = runLangCommand("lang", "auto")
	if got != "The CLI language follows the locale; no saved choice remains.\n" {
		t.Fatalf("clear setting: %q", got)
	}
	if _, err := os.Stat(layout.LanguageFile()); !os.IsNotExist(err) {
		t.Fatalf("setting remains: %v", err)
	}

	got, _ = runLangCommand("lang")
	if got != "Language: English (locale)\n" {
		t.Fatalf("query after auto: %q", got)
	}

	got, _ = runLangCommand("lang", "en")
	if got != "CLI language set to English.\n" {
		t.Fatalf("set English: %q", got)
	}
	got, _ = runLangCommand("lang")
	if got != "Language: English (saved setting)\n" {
		t.Fatalf("English query: %q", got)
	}
}

func TestLangShowsTheLocaleAndTheDefault(t *testing.T) {
	env, out, errOut, _ := languageHarness(t)
	for _, c := range []struct {
		lang string
		want string
	}{
		{"zh_CN.UTF-8", "语言：    简体中文（系统 locale）\n"},
		{"", "Language: English (default)\n"},
	} {
		t.Setenv("LANG", c.lang)
		out.Reset()
		if code := Run(env, []string{"lang"}); code != 0 {
			t.Fatalf("LANG=%q: code %d, stderr %q", c.lang, code, errOut.String())
		}
		if got := out.String(); got != c.want {
			t.Errorf("LANG=%q: got %q, want %q", c.lang, got, c.want)
		}
	}
}

func TestSBCLangOverridesTheSavedChoice(t *testing.T) {
	env, out, errOut, layout := languageHarness(t)
	if code := Run(env, []string{"lang", "zh"}); code != 0 {
		t.Fatalf("save Chinese: code %d, stderr %q", code, errOut.String())
	}
	t.Setenv("SBC_LANG", "en")

	out.Reset()
	errOut.Reset()
	if code := Run(env, []string{"lang"}); code != 0 {
		t.Fatalf("query: code %d, stderr %q", code, errOut.String())
	}
	if got := out.String(); got != "Language:     English (SBC_LANG)\nSaved choice: Simplified Chinese\n" {
		t.Fatalf("query: %q", got)
	}

	out.Reset()
	if code := Run(env, []string{"lang", "zh"}); code != 0 {
		t.Fatalf("save Chinese under SBC_LANG: code %d, stderr %q", code, errOut.String())
	}
	if got := out.String(); got != "Saved Simplified Chinese, but SBC_LANG currently overrides it.\n" {
		t.Fatalf("confirmation: %q", got)
	}
	data, err := os.ReadFile(layout.LanguageFile())
	if err != nil || string(data) != "zh\n" {
		t.Fatalf("saved language %q, error %v", data, err)
	}

	t.Setenv("SBC_LANG", "")
	out.Reset()
	if code := Run(env, []string{"lang"}); code != 0 {
		t.Fatalf("query without SBC_LANG: code %d, stderr %q", code, errOut.String())
	}
	if got := out.String(); got != "语言：    简体中文（已保存设置）\n" {
		t.Fatalf("query without SBC_LANG: %q", got)
	}
}

func TestApplyLanguageLoadsTheSavedChoiceBeforeCommands(t *testing.T) {
	_, _, _, layout := languageHarness(t)
	if err := writeLanguage(layout.LanguageFile(), languageChinese); err != nil {
		t.Fatal(err)
	}
	if err := applyLanguage(layout); err != nil {
		t.Fatal(err)
	}
	if got := i18n.Pick("English", "简体中文"); got != "简体中文" {
		t.Fatalf("startup language %q", got)
	}

	t.Setenv("SBC_LANG", "en")
	if err := applyLanguage(layout); err != nil {
		t.Fatal(err)
	}
	if got := i18n.Pick("English", "简体中文"); got != "English" {
		t.Fatalf("SBC_LANG did not override the saved choice: %q", got)
	}
}

func TestLangRepairsACorruptSavedChoice(t *testing.T) {
	env, out, errOut, layout := languageHarness(t)
	path := layout.LanguageFile()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("fr\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	out.Reset()
	errOut.Reset()
	if code := Run(env, []string{"lang"}); code != 1 {
		t.Fatalf("query: code %d, stdout %q, stderr %q", code, out.String(), errOut.String())
	}
	for _, want := range []string{"invalid", "sbc lang auto"} {
		if !strings.Contains(errOut.String(), want) {
			t.Errorf("stderr %q does not contain %q", errOut.String(), want)
		}
	}
	if err := applyLanguage(layout); err == nil {
		t.Fatal("startup accepted a corrupt saved choice")
	}

	out.Reset()
	if code := Run(env, []string{"lang", "en"}); code != 0 {
		t.Fatalf("repair with en: code %d, stderr %q", code, errOut.String())
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "en\n" {
		t.Fatalf("saved language %q, error %v", data, err)
	}

	if err := os.WriteFile(path, []byte("fr\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	out.Reset()
	if code := Run(env, []string{"lang", "auto"}); code != 0 {
		t.Fatalf("repair with auto: code %d, stderr %q", code, errOut.String())
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("corrupt setting remains: %v", err)
	}
}

func TestReadLanguageAcceptsAUTF8BOMAndAuto(t *testing.T) {
	dir := t.TempDir()
	cases := []struct {
		name    string
		content string
		want    string
	}{
		{"missing", "", languageAuto},
		{"empty", "", languageAuto},
		{"bom", "\ufeffzh\r\n", languageChinese},
		{"auto", "auto\n", languageAuto},
		{"english", "en\n", languageEnglish},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			path := filepath.Join(dir, c.name)
			if c.name != "missing" {
				if err := os.WriteFile(path, []byte(c.content), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			got, err := readLanguage(path)
			if err != nil || got != c.want {
				t.Fatalf("got %q, error %v, want %q", got, err, c.want)
			}
		})
	}
}
