package cli

import (
	"slices"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestCompletionNewFlagsAreOfflineAndCommandSpecific(t *testing.T) {
	env, out, _ := inertEnv(t)
	for _, entry := range []struct {
		command string
		want    []string
	}{
		{"ip", []string{"--ai", "--allow-direct-ai", "--help"}},
		{"route", []string{"--list", "--help"}},
	} {
		got := completionNames(t, env, out, entry.command, "--")
		for _, flag := range entry.want {
			if !slices.Contains(got, flag) {
				t.Errorf("%s: missing %s in %v", entry.command, flag, got)
			}
		}
	}
	got := completionNames(t, env, out, "protocol", "--")
	if slices.Contains(got, "--ai") || slices.Contains(got, "--list") {
		t.Fatal(got)
	}
	for _, flag := range []string{"--ai=", "--allow-direct-ai="} {
		if got := completionNames(t, env, out, "ip", flag); len(got) != 0 {
			t.Fatal(got)
		}
	}
}

func TestRouteCompletionDescriptionsAreLocalizedAndHideAliases(t *testing.T) {
	env, out, _ := inertEnv(t)
	layout := paths.Layout{Config: t.TempDir()}
	env.Layout = func() (paths.Layout, error) { return layout, nil }
	paths.WriteFile(layout.ConfigFile(), []byte(completionConfig), 0o600)
	for _, args := range [][]string{{"route", ""}, {"install", "--route", ""}, {"install", "--route="}} {
		completionNames(t, env, out, args...)
		if !strings.Contains(out.String(), "ai-only\tProxy listed AI destinations") || strings.Contains(out.String(), "\nai\t") {
			t.Fatalf("%v: %s", args, out)
		}
	}
	i18n.SetChinese(true)
	defer i18n.SetChinese(false)
	completionNames(t, env, out, "route", "a")
	if !strings.Contains(out.String(), "ai-only\t列表中的 AI") {
		t.Fatal(out)
	}
}
