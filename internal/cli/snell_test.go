package cli

import (
	"slices"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func TestCompletionIncludesSnellFromInstalledSelector(t *testing.T) {
	env, out, _ := inertEnv(t)
	layout := paths.Layout{Config: t.TempDir()}
	env.Layout = func() (paths.Layout, error) { return layout, nil }
	config := `{"outbounds":[{"type":"selector","tag":"proxy","outbounds":["trojan","snell"]}]}`
	if err := paths.WriteFile(layout.ConfigFile(), []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{{"protocol", "s"}, {"install", "--protocol", "s"}, {"install", "--protocol=s"}} {
		if names := completionNames(t, env, out, args...); !slices.Contains(names, "snell") {
			t.Fatalf("%v: missing Snell in %v", args, names)
		}
	}
}
