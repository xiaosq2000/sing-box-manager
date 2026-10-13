package cli

import (
	"encoding/json"
	"io"
	"os"
	"slices"
	"strings"
	"unicode"

	"github.com/spf13/cobra"
	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

func fixedChoices(values ...string) cobra.CompletionFunc {
	return func(_ *cobra.Command, args []string, prefix string) ([]string, cobra.ShellCompDirective) {
		var matches []string
		if len(args) == 0 {
			for _, value := range values {
				if strings.HasPrefix(value, prefix) {
					matches = append(matches, value)
				}
			}
		}
		return matches, cobra.ShellCompDirectiveNoFileComp
	}
}

func configChoices(env Env, kind string) cobra.CompletionFunc {
	return func(cmd *cobra.Command, args []string, prefix string) ([]string, cobra.ShellCompDirective) {
		if len(args) != 0 {
			return nil, cobra.ShellCompDirectiveNoFileComp
		}
		return fixedChoices(installedChoices(env, kind)...)(cmd, args, prefix)
	}
}

// Completion reads only choice names. It never uses the Clash API, reads the
// subscription link, or reports config errors that could disclose credentials.
func installedChoices(env Env, kind string) []string {
	layout, err := env.Layout()
	if err != nil {
		return nil
	}
	info, err := os.Lstat(layout.ConfigFile())
	if err != nil || !info.Mode().IsRegular() {
		return nil
	}
	file, err := os.Open(layout.ConfigFile())
	if err != nil {
		return nil
	}
	defer file.Close()
	const limit = 8 << 20
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil || len(data) > limit {
		return nil
	}
	type rule struct {
		ClashMode json.RawMessage   `json:"clash_mode"`
		Rules     []json.RawMessage `json:"rules"`
	}
	var config struct {
		Outbounds []struct {
			Type      string   `json:"type"`
			Tag       string   `json:"tag"`
			Outbounds []string `json:"outbounds"`
		} `json:"outbounds"`
		Route struct {
			Rules []json.RawMessage `json:"rules"`
		} `json:"route"`
		DNS struct {
			Rules []json.RawMessage `json:"rules"`
		} `json:"dns"`
		Experimental struct {
			ClashAPI struct {
				DefaultMode string `json:"default_mode"`
			} `json:"clash_api"`
		} `json:"experimental"`
	}
	if json.Unmarshal(data, &config) != nil {
		return nil
	}
	var values []string
	if kind == "protocol" {
		for _, outbound := range config.Outbounds {
			if outbound.Type == "selector" && outbound.Tag == api.ProxySelector {
				values = append(values, outbound.Outbounds...)
			}
		}
	} else {
		values = append(values, config.Experimental.ClashAPI.DefaultMode)
		var collect func([]json.RawMessage, int)
		collect = func(rules []json.RawMessage, depth int) {
			if depth > 32 {
				return
			}
			for _, raw := range rules {
				var entry rule
				if json.Unmarshal(raw, &entry) != nil {
					continue
				}
				var mode string
				var modes []string
				if json.Unmarshal(entry.ClashMode, &mode) == nil {
					values = append(values, mode)
				} else if json.Unmarshal(entry.ClashMode, &modes) == nil {
					values = append(values, modes...)
				}
				collect(entry.Rules, depth+1)
			}
		}
		collect(config.Route.Rules, 0)
		collect(config.DNS.Rules, 0)
	}
	// Choice names enter shell scripts. Reject whitespace, control characters,
	// flag-like names and shell syntax rather than interpreting them.
	values = slices.DeleteFunc(values, func(value string) bool {
		if value == "" || strings.HasPrefix(value, "-") {
			return true
		}
		for _, char := range value {
			if !unicode.IsLetter(char) && !unicode.IsNumber(char) && !strings.ContainsRune("-_.", char) {
				return true
			}
		}
		return false
	})
	slices.Sort(values)
	return slices.Compact(values)
}

func writeCompletion(root *cobra.Command, name string, out io.Writer) error {
	switch name {
	case "bash":
		if err := root.GenBashCompletionV2(out, true); err != nil {
			return err
		}
		// Cobra's fallback still requires bash-completion's word helper. This
		// replacement permits bare Bash 3.2 too, without changing global helpers.
		_, err := io.WriteString(out, bashCompletionFallback)
		return err
	case "zsh":
		return root.GenZshCompletion(out)
	case "powershell":
		if err := root.GenPowerShellCompletionWithDesc(out); err != nil {
			return err
		}
		_, err := io.WriteString(out, "\nRegister-ArgumentCompleter -CommandName 'sbc.exe' -ScriptBlock ${__sbcCompleterBlock}\n")
		return err
	default:
		return i18n.New("completion takes bash, zsh, or powershell")
	}
}

const bashCompletionFallback = `
# Fallback for shells without the bash-completion package.
__sbc_init_completion() {
    COMPREPLY=()
    local i token previous="" count=0
    words=()
    for ((i=0; i<=COMP_CWORD; i++)); do
        token=${COMP_WORDS[i]}
        if ((count > 0)) && [[ $token == = || $token == : || $previous == = || $previous == : ]]; then
            words[count-1]+=$token
        else
            words[count]=$token
            count=$((count+1))
        fi
        previous=$token
    done
    cword=$((count-1))
    cur=${words[cword]}
    prev=""
    if ((cword > 0)); then prev=${words[cword-1]}; fi
}
# sbc has no filename arguments. Bash 3.2 cannot disable fallback with compopt.
complete -F __start_sbc sbc
`
