// Package shell writes what bash and zsh need to use sbc: the proxy variables,
// the `sbc init` hook, and one block in the user's rc file.
package shell

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// NoProxy lists destinations that never go through the proxy.
const NoProxy = "localhost,127.0.0.0/8,::1,host.docker.internal"

var proxyVariables = []string{"http_proxy", "https_proxy", "ftp_proxy", "socks_proxy", "all_proxy", "no_proxy"}

// Quote returns text as one single-quoted shell word.
func Quote(text string) string {
	return "'" + strings.ReplaceAll(text, "'", `'\''`) + "'"
}

// EnvOn returns the lines that point a shell at the local proxy. A username
// and password, when the proxy asks for them, go in the URLs.
func EnvOn(port int, username, password string) string {
	values := Values(port, username, password)
	var lines strings.Builder
	for _, name := range proxyNames() {
		fmt.Fprintf(&lines, "export %s=%s\n", name, Quote(values[name]))
	}
	return lines.String()
}

// EnvOff returns the lines that clear every proxy variable.
func EnvOff() string {
	names := make([]string, 0, 2*len(proxyVariables))
	for _, name := range proxyVariables {
		names = append(names, name, strings.ToUpper(name))
	}
	return "unset " + strings.Join(names, " ") + "\n"
}

// Init returns the shell code `sbc init` prints for bash, including 3.2, or
// zsh. It puts sbc on PATH and adds a prompt hook that applies `sbc on` and
// `sbc off` to every open shell at its next prompt. The same hook loads the
// region choice, which is independent of the proxy. A program cannot change
// the shell that started it, so this hook is the whole shell integration.
// `export SBC_PROXY=off` keeps one shell off.
func Init(shellName, cliDir, envFile, regionFile, hintFile string, state ProxyState) (string, error) {
	var hook string
	switch shellName {
	case "bash":
		hook = `case ";${PROMPT_COMMAND-};" in
*";_sbc_apply;"*) ;;
*) PROMPT_COMMAND="_sbc_apply${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
`
	case "zsh":
		hook = "autoload -Uz add-zsh-hook && add-zsh-hook precmd _sbc_apply\n"
	default:
		return "", i18n.Errorf("unsupported shell %q; use bash or zsh", shellName)
	}
	cli, env, region, hint := Quote(cliDir), Quote(envFile), Quote(regionFile), Quote(hintFile)
	notice := Quote(i18n.T("sbc: a newer version is available; run 'sbc upgrade'"))
	unexportFunctions := ""
	if shellName == "bash" {
		unexportFunctions = "export -n -f _sbc_apply _sbc_apply_local _sbc_private _sbc_capture_off _sbc_valid_deadline _sbc_before_proxy _sbc_forget_proxy _sbc_remember_proxy _sbc_clear_proxy\n"
	}
	return `# sbc shell integration, printed by 'sbc init ` + shellName + `'.
case $- in
*a*) set +a; unset _sbc_init_allexport; _sbc_init_allexport=1 ;;
*) unset _sbc_init_allexport ;;
esac
unset _sbc_applied
case ":$PATH:" in
*:` + cli + `:*) ;;
*) PATH=` + cli + `:$PATH; export PATH ;;
esac
` + proxyHelpers(shellName, state) + `_sbc_apply() {
    case $- in
    *a*) set +a; _sbc_apply_local; set -a ;;
    *) _sbc_apply_local ;;
    esac
}
_sbc_apply_local() {
    _sbc_private
    if [ "${SBC_PROXY-}" != off ] && _sbc_capture_off && [ -f ` + env + ` ]; then
        _sbc_before_proxy
        if [ -r ` + env + ` ] && . ` + env + `; then
            _sbc_remember_proxy
        fi
    else
        _sbc_clear_proxy
    fi
    _sbc_forget_proxy
    if [ -f ` + region + ` ]; then
        . ` + region + `
        case "${_sbc_region_prev-}" in
        *tz*) case "${_sbc_region_tz-}" in 1) ;; *) unset TZ ;; esac ;;
        esac
        case "${_sbc_region_prev-}" in
        *lang*) case "${_sbc_region_lang-}" in 1) ;; *) unset LANG LANGUAGE LC_MESSAGES ;; esac ;;
        esac
        _sbc_region_prev="${_sbc_region_tz:+tz}${_sbc_region_lang:+lang}"
    elif [ -n "${_sbc_region_prev-}" ]; then
        case "$_sbc_region_prev" in *tz*) unset TZ ;; esac
        case "$_sbc_region_prev" in *lang*) unset LANG LANGUAGE LC_MESSAGES ;; esac
        unset _sbc_region_prev _sbc_region_tz _sbc_region_lang
    fi
}
` + unexportFunctions + hook + `_sbc_apply
case $- in
*i*)
    if [ -f ` + hint + ` ]; then
        printf '%s\n' ` + notice + ` >&2
    fi
    ;;
esac
case "${_sbc_init_allexport-}" in
1) unset _sbc_init_allexport; set -a ;;
*) unset _sbc_init_allexport ;;
esac
`, nil
}

const (
	blockStart = "# >>> sbc >>>"
	blockEnd   = "# <<< sbc <<<"
)

// RCFile returns the rc file for a shell name. Terminals on macOS start bash
// as a login shell, which reads .bash_profile rather than .bashrc.
func RCFile(home, shellName string) (string, error) {
	switch shellName {
	case "bash":
		if runtime.GOOS == "darwin" {
			return filepath.Join(home, ".bash_profile"), nil
		}
		return filepath.Join(home, ".bashrc"), nil
	case "zsh":
		return filepath.Join(home, ".zshrc"), nil
	default:
		return "", i18n.Errorf("unsupported shell %q; use bash or zsh", shellName)
	}
}

// DetectShell returns bash or zsh from $SHELL, preferring bash.
func DetectShell() string {
	if filepath.Base(os.Getenv("SHELL")) == "zsh" {
		return "zsh"
	}
	return "bash"
}

// AddBlock makes rcPath run `sbc init`, once.
func AddBlock(rcPath, sbc, shellName string) error {
	current, err := os.ReadFile(rcPath)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	text := removeBlock(string(current))
	if text != "" && !strings.HasSuffix(text, "\n") {
		text += "\n"
	}
	text += blockStart + "\n" + InitLine(sbc, shellName) + "\n" + blockEnd + "\n"
	return os.WriteFile(rcPath, []byte(text), 0o644)
}

// InitLine is the rc line that sets a shell up, for dotfiles that sbc does not
// edit itself.
func InitLine(sbc, shellName string) string {
	return "if [ -x " + Quote(sbc) + " ]; then eval \"$(" + Quote(sbc) + " init " + shellName + ")\"; fi"
}

// RemoveBlock takes sbc's block out of rcPath, leaving everything else.
func RemoveBlock(rcPath string) error {
	current, err := os.ReadFile(rcPath)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	text := removeBlock(string(current))
	if text == string(current) {
		return nil
	}
	return os.WriteFile(rcPath, []byte(text), 0o644)
}

func removeBlock(text string) string {
	var kept []string
	inside := false
	for _, line := range strings.SplitAfter(text, "\n") {
		switch strings.TrimRight(line, "\n") {
		case blockStart:
			inside = true
			continue
		case blockEnd:
			inside = false
			continue
		}
		if !inside {
			kept = append(kept, line)
		}
	}
	return strings.Join(kept, "")
}
