package cli

import (
	"fmt"
	"os"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/shell"
)

func shellProxyState(env Env, layout paths.Layout) shell.ProxyState {
	var state shell.ProxyState
	if local, err := readLocal(layout); err == nil {
		state.Known = shell.Values(local.ListenPort, local.Username, local.Password)
	}
	if env.OS == "linux" && env.TunPaths != nil {
		p := env.TunPaths()
		state.ControlDir, state.Active, state.ConfigDir = p.ControlDir(), p.Active, p.ConfigDir
	}
	return state
}

func proxyResidue(env Env, layout paths.Layout) (owned, other []string) {
	getenv := env.Getenv
	if getenv == nil {
		getenv = os.Getenv
	}
	return shell.Residue(getenv, shellProxyState(env, layout).Known)
}

func warnProxyEnvironment(env Env, layout paths.Layout) {
	owned, other := proxyResidue(env, layout)
	printProxyResidue(env, owned, other)
}

func printProxyResidue(env Env, owned, other []string) {
	if len(owned) != 0 {
		fmt.Fprintf(env.Stderr, i18n.T("sbc: this command inherits Mixed proxy variables: %s. Clean the source environment, then restart affected programs. Other processes were not checked.\n"), strings.Join(owned, ", "))
	}
	if len(other) != 0 {
		fmt.Fprintf(env.Stderr, i18n.T("sbc: this command inherits unrelated or unknown proxy variables: %s. Automatic cleanup preserves them. Check their source before using transparent TUN.\n"), strings.Join(other, ", "))
	}
}

func warnMixedMigration(env Env, layout paths.Layout) {
	owned, other := proxyResidue(env, layout)
	_, fileErr := os.Stat(layout.EnvFile())
	mixed := fileErr == nil
	if manager, err := env.Service(layout.SBC()); err == nil && manager.Active() {
		mixed = true
	}
	if mixed || len(owned) != 0 {
		fmt.Fprintln(env.Stderr, i18n.T("Running programs can retain Mixed proxy settings after the switch to TUN. Clean the environment used to start affected programs. Then restart those programs."))
	}
	printProxyResidue(env, owned, other)
}
