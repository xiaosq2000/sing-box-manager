package winsettings

import (
	"fmt"
	"net/url"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/shell"
)

var environmentNames = []string{"HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "SOCKS_PROXY", "ALL_PROXY", "NO_PROXY", "Path"}

// Environment changes only the user's variables, never the machine's PATH.
type Environment struct{ Registry Registry }

func ProxyValues(port int, username, password string) map[string]Value {
	address := fmt.Sprintf("127.0.0.1:%d", port)
	if username != "" {
		address = url.UserPassword(username, password).String() + "@" + address
	}
	values := map[string]Value{}
	for _, name := range []string{"HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY"} {
		values[name] = Value{"http://" + address, "String"}
	}
	values["SOCKS_PROXY"] = Value{"socks5://" + address, "String"}
	values["NO_PROXY"] = Value{shell.NoProxy, "String"}
	return values
}

// On refuses to overwrite another proxy or a custom bypass list.
func (e *Environment) On(port int, username, password string) error {
	return e.Enable(ProxyValues(port, username, password), nil)
}

// Enable also accepts sbc's values from before a reinstall, so changing local
// authentication replaces our old credentials without taking over foreign edits.
func (e *Environment) Enable(wanted, previous map[string]Value) error {
	current, err := e.Registry.Read(EnvironmentKey, environmentNames)
	if err != nil {
		return err
	}
	for _, name := range environmentNames[:6] {
		if value := current[name].Text; value != "" && value != wanted[name].Text && value != previous[name].Text {
			return i18n.Errorf("Windows user variable %s belongs to another proxy; clear it in Environment Variables before running 'sbc on'", name)
		}
	}
	return e.Registry.Apply(EnvironmentKey, wanted, "environment")
}

// Move changes only variables still pointing at sbc's old port. Off uses a
// zero destination port and removes those variables. Foreign edits survive.
func (e *Environment) Move(oldPort, newPort int, username, password string) error {
	current, err := e.Registry.Read(EnvironmentKey, environmentNames)
	if err != nil {
		return err
	}
	old := ProxyValues(oldPort, username, password)
	next := ProxyValues(newPort, username, password)
	changes := map[string]Value{}
	for name, value := range old {
		if name != "NO_PROXY" && current[name].Text == value.Text {
			if newPort == 0 {
				changes[name] = Value{}
			} else {
				changes[name] = next[name]
			}
		}
	}
	if newPort == 0 && len(changes) > 0 && current["NO_PROXY"].Text == old["NO_PROXY"].Text {
		changes["NO_PROXY"] = Value{}
	}
	return e.Registry.Apply(EnvironmentKey, changes, "environment")
}

func pathIdentity(value string) string {
	return strings.ToLower(strings.TrimRight(strings.ReplaceAll(strings.Trim(strings.TrimSpace(value), `"`), "/", `\`), `\`))
}

// Path adds or removes exactly cliDir, preserving other entries and the
// registry type so %VARIABLE% entries are not expanded or truncated.
func (e *Environment) Path(cliDir string, add bool) error {
	current, err := e.Registry.Read(EnvironmentKey, []string{"Path"})
	if err != nil {
		return err
	}
	value := current["Path"]
	parts := []string{}
	found := false
	if value.Text != "" {
		for _, part := range strings.Split(value.Text, ";") {
			if pathIdentity(part) == pathIdentity(cliDir) {
				found = true
				if !add {
					continue
				}
			}
			parts = append(parts, part)
		}
	}
	if add && found || !add && !found {
		return nil
	}
	if add {
		parts = append(parts, cliDir)
	}
	value.Text = strings.Join(parts, ";")
	if value.Kind == "" {
		value.Kind = "ExpandString"
	}
	if !add && value.Text == "" {
		value = Value{}
	}
	return e.Registry.Apply(EnvironmentKey, map[string]Value{"Path": value}, "environment")
}

// PowerShellEnv prints commands for the current PowerShell session. sbc itself
// cannot change the environment of its parent process.
func PowerShellEnv(port int, username, password string, on bool) string {
	values := ProxyValues(port, username, password)
	var output strings.Builder
	for _, name := range environmentNames[:6] {
		if !on {
			fmt.Fprintf(&output, "Remove-Item Env:%s -ErrorAction SilentlyContinue\n", name)
		} else if value, ok := values[name]; ok {
			fmt.Fprintf(&output, "$env:%s = '%s'\n", name, strings.ReplaceAll(value.Text, "'", "''"))
		}
	}
	return output.String()
}

// State reads persistent settings instead of a marker, so manual changes are
// visible to status and service-stop hints.
func (e *Environment) State(port int, username, password string) (string, error) {
	current, err := e.Registry.Read(EnvironmentKey, environmentNames)
	if err != nil {
		return "", err
	}
	wanted := ProxyValues(port, username, password)
	found := false
	for _, name := range environmentNames[:5] {
		if value := current[name].Text; value != "" {
			if value != wanted[name].Text {
				return i18n.T("set to another proxy"), nil
			}
			found = true
		}
	}
	if found {
		return i18n.T("on"), nil
	}
	return i18n.T("off"), nil
}
