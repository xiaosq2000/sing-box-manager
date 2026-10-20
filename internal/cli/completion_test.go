package cli

import (
	"bytes"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/shell"
)

const completionConfig = `{
  "inbounds": [{"users": [{"username": "fixture-user", "password": "fixture-password"}]}],
  "outbounds": [
    {"type": "selector", "tag": "proxy", "outbounds": ["trojan", "naive", "trojan", "hysteria2", "bad\nname", "$(bad)", "--bad"]},
    {"type": "trojan", "tag": "trojan", "password": "fixture-password"},
    {"type": "selector", "tag": "speed-test", "outbounds": ["not-a-protocol-choice"]}
  ],
  "route": {"rules": [{"clash_mode": "gfw"}, {"type": "logical", "rules": [{"clash_mode": "global"}]}]},
  "dns": {"rules": [{"clash_mode": ["china", "ai"]}]},
  "experimental": {"clash_api": {"default_mode": "china", "external_controller": "127.0.0.1:1", "secret": "fixture-api-secret"}}
}`

func completionNames(t *testing.T, env Env, out *bytes.Buffer, args ...string) []string {
	t.Helper()
	out.Reset()
	if code := Run(env, append([]string{"__complete"}, args...)); code != 0 {
		t.Fatalf("completion %v: code %d", args, code)
	}
	var names []string
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		if strings.HasPrefix(line, ":") {
			if line != ":4" {
				t.Errorf("completion permitted file suggestions: %q", line)
			}
			continue
		}
		name, _, _ := strings.Cut(line, "\t")
		names = append(names, name)
	}
	return names
}

func TestCompletionFixedChoicesAndFlagsNeedNoInstallation(t *testing.T) {
	env, out, _ := inertEnv(t)
	for _, entry := range []struct{ args, want []string }{
		{[]string{"pro"}, []string{"protocol"}},
		{[]string{"env", ""}, []string{"on", "off"}},
		{[]string{"lang", ""}, []string{"en", "zh", "auto"}},
		{[]string{"privacy", "browser", "o"}, []string{"on", "off"}},
		{[]string{"privacy", "browser", "l"}, []string{"language"}},
		{[]string{"privacy", "browser", "w"}, []string{"webrtc"}},
		{[]string{"privacy", "browser", "language", "o"}, []string{"on", "off"}},
		{[]string{"privacy", "browser", "webrtc", "o"}, []string{"on", "off"}},
		{[]string{"privacy", "region", "o"}, []string{"on", "off"}},
		{[]string{"refresh", ""}, []string{"auto", "interval"}},
		{[]string{"upgrade", "auto", ""}, []string{"on", "off"}},
		{[]string{"refresh", "interval", ""}, nil},
		{[]string{"init", "z"}, []string{"zsh"}},
		{[]string{"link", ""}, []string{"set"}},
		{[]string{"completion", "p"}, []string{"powershell"}},
		{[]string{"install", "--shell", ""}, []string{"bash", "zsh"}},
		{[]string{"install", "--auth=o"}, []string{"on", "off"}},
		{[]string{"port", ""}, nil},
		{[]string{"env", "on", ""}, nil},
	} {
		got := completionNames(t, env, out, entry.args...)
		if !slices.Equal(got, entry.want) {
			t.Errorf("%v: got %v, want %v", entry.args, got, entry.want)
		}
	}
	got := completionNames(t, env, out, "install", "--")
	for _, name := range []string{"--auth", "--no-rc", "--protocol", "--route", "--shell", "--help"} {
		if !slices.Contains(got, name) {
			t.Errorf("missing flag %s in %v", name, got)
		}
	}
}

func TestCompletionInstalledChoicesAreOfflineAndPrivate(t *testing.T) {
	env, out, errOut := inertEnv(t)
	layout := paths.Layout{Config: t.TempDir()}
	env.Layout = func() (paths.Layout, error) { return layout, nil }
	if err := paths.WriteFile(layout.ConfigFile(), []byte(completionConfig), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, entry := range []struct{ args, want []string }{
		{[]string{"route", ""}, []string{"ai-only", "china-direct", "gfw-proxy", "global"}},
		{[]string{"route", "g"}, []string{"gfw-proxy", "global"}},
		{[]string{"protocol", ""}, []string{"hysteria2", "naive", "trojan"}},
		{[]string{"install", "--route", "a"}, []string{"ai-only"}},
		{[]string{"install", "--protocol", "n"}, []string{"naive"}},
		{[]string{"route", "china", ""}, nil},
	} {
		got := completionNames(t, env, out, entry.args...)
		if !slices.Equal(got, entry.want) {
			t.Errorf("%v: got %v, want %v", entry.args, got, entry.want)
		}
		if errOut.Len() != 0 {
			t.Fatalf("completion wrote to stderr: %s", errOut)
		}
		for _, secret := range []string{"fixture-password", "fixture-api-secret", "fixture-user", "127.0.0.1", "bad"} {
			if strings.Contains(out.String()+errOut.String(), secret) {
				t.Fatalf("completion disclosed %q", secret)
			}
		}
	}
	data, _ := os.ReadFile(layout.ConfigFile())
	if string(data) != completionConfig {
		t.Fatal("completion changed the config")
	}
}

func TestCompletionMissingOrInvalidConfigIsQuiet(t *testing.T) {
	env, out, errOut := inertEnv(t)
	layout := paths.Layout{Config: t.TempDir()}
	env.Layout = func() (paths.Layout, error) { return layout, nil }
	for _, data := range []string{"", "{ fixture-password", `{"outbounds": "fixture-password"}`} {
		if data != "" {
			if err := paths.WriteFile(layout.ConfigFile(), []byte(data), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		errOut.Reset()
		if got := completionNames(t, env, out, "protocol", ""); len(got) != 0 || strings.Contains(errOut.String(), "fixture-password") {
			t.Fatalf("invalid config: names %v, stderr %q", got, errOut)
		}
	}
	os.Remove(layout.ConfigFile())
	os.Mkdir(layout.ConfigFile(), 0o700)
	if got := completionNames(t, env, out, "route", ""); len(got) != 0 {
		t.Fatal(got)
	}
	env.Layout = func() (paths.Layout, error) { return paths.Layout{}, errors.New("fixture-password") }
	if got := completionNames(t, env, out, "route", ""); len(got) != 0 || strings.Contains(out.String()+errOut.String(), "fixture-password") {
		t.Fatal(got)
	}
}

func TestCompletionRejectsOversizedAndSymlinkConfigs(t *testing.T) {
	env, out, errOut := inertEnv(t)
	layout := paths.Layout{Config: t.TempDir()}
	env.Layout = func() (paths.Layout, error) { return layout, nil }
	large := `{"padding":"` + strings.Repeat("x", 8<<20) + `","experimental":{"clash_api":{"default_mode":"china"}}}`
	if err := paths.WriteFile(layout.ConfigFile(), []byte(large), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := completionNames(t, env, out, "route", ""); len(got) != 0 || errOut.Len() != 0 {
		t.Fatalf("oversized config: names %v, stderr %s", got, errOut)
	}
	if runtime.GOOS != "windows" {
		if err := os.Remove(layout.ConfigFile()); err != nil {
			t.Fatal(err)
		}
		target := filepath.Join(layout.Config, "target.json")
		if err := os.WriteFile(target, []byte(completionConfig), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(target, layout.ConfigFile()); err != nil {
			t.Fatal(err)
		}
		if got := completionNames(t, env, out, "route", ""); len(got) != 0 || errOut.Len() != 0 {
			t.Fatalf("symlink config: names %v, stderr %s", got, errOut)
		}
	}
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }

func TestCompletionScriptGenerationNeedsNoInstallation(t *testing.T) {
	env, out, errOut := inertEnv(t)
	for _, name := range []string{"bash", "zsh", "powershell"} {
		out.Reset()
		if code := Run(env, []string{"completion", name}); code != 0 || !strings.Contains(out.String(), "__complete") || errOut.Len() != 0 {
			t.Fatalf("%s: code %d, stderr %s", name, code, errOut)
		}
		if strings.Contains(out.String(), "fish") {
			t.Fatal("generated an unsupported shell")
		}
	}
	env.Stdout = failingWriter{}
	if code := Run(env, []string{"completion", "bash"}); code != 1 || !strings.Contains(errOut.String(), "closed pipe") {
		t.Fatal("generation ignored a write failure")
	}
}

// Native script checks use a real sbc, a scratch home, and a stopped fixture
// config. They do not use the user's rc files, profiles, services, or settings.
func completionBinary(t *testing.T) (string, []string) {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("Go not on PATH")
	}
	home := t.TempDir()
	config, data := filepath.Join(home, "config", "sbc"), filepath.Join(home, "data", "sbc")
	if runtime.GOOS == "darwin" {
		config = filepath.Join(home, "Library", "Application Support", "sbc")
		data = config
	}
	if runtime.GOOS == "windows" {
		config = filepath.Join(home, "app", "sbc")
		data = config
	}
	layout := paths.Layout{Config: config, Data: data}
	if runtime.GOOS == "windows" {
		layout.Exe = ".exe"
	}
	if err := paths.WriteFile(layout.ConfigFile(), []byte(completionConfig), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(layout.CLIDir(), 0o700); err != nil {
		t.Fatal(err)
	}
	command := exec.Command("go", "build", "-o", layout.SBC(), "../../cmd/sbc")
	command.Env = append(os.Environ(), "GOPROXY=off", "CGO_ENABLED=0", "GOTOOLCHAIN=local")
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, output)
	}
	env := append(os.Environ(), "HOME="+home, "USERPROFILE="+home, "ZDOTDIR="+home,
		"XDG_CONFIG_HOME="+filepath.Join(home, "config"), "XDG_DATA_HOME="+filepath.Join(home, "data"),
		"LOCALAPPDATA="+filepath.Join(home, "app"), "SBC_LANG=en", "SBC_PROXY=off",
		"PATH="+layout.CLIDir()+string(os.PathListSeparator)+os.Getenv("PATH"))
	return layout.SBC(), env
}

func TestCompletionNativeUnixShells(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix shells")
	}
	binary, env := completionBinary(t)
	var shells []struct{ name, path string }
	for _, name := range []string{"bash", "zsh"} {
		if path, err := exec.LookPath(name); err == nil {
			shells = append(shells, struct{ name, path string }{name, path})
		}
	}
	if runtime.GOOS == "darwin" {
		// Test the stock Bash 3.2 even when Homebrew's Bash is first on PATH.
		shells = append(shells, struct{ name, path string }{"bash", "/bin/bash"})
	}
	for _, entry := range shells {
		name, path := entry.name, entry.path
		t.Run(path, func(t *testing.T) {
			command := exec.Command(binary, "completion", name)
			command.Env = env
			code, err := command.Output()
			if err != nil {
				t.Fatal(err)
			}
			file := filepath.Join(t.TempDir(), "completion.sh")
			os.WriteFile(file, code, 0o600)
			if name == "bash" {
				script := `. ` + shell.Quote(file) + `
# compopt can only run inside an actual readline completion callback.
compopt() { :; }
# Filename fallback must stay off even on Bash without compopt.
[[ $(complete -p sbc) != *'-o default'* ]] || exit 1
COMP_WORDS=(sbc pro); COMP_CWORD=1; __start_sbc; printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(sbc route c); COMP_CWORD=2; __start_sbc; printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(sbc ip --a); COMP_CWORD=2; __start_sbc; printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(sbc install --shell z); COMP_CWORD=3; __start_sbc; printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(sbc install --shell = z); COMP_CWORD=4; __start_sbc; printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(sbc install --route = g); COMP_CWORD=4; __start_sbc; printf '%s\n' "${COMPREPLY[@]}"
COMP_WORDS=(sbc install --shell z ignored); COMP_CWORD=3; __start_sbc; printf '%s\n' "${COMPREPLY[@]}"
`
				command = exec.Command(path, "--noprofile", "--norc", "-c", script)
				command.Env = env
				output, err := command.CombinedOutput()
				if err != nil || string(output) != "protocol\nchina-direct\n--ai\n--allow-direct-ai\nzsh\nzsh\ngfw-proxy\nglobal\nzsh\n" {
					t.Fatalf("Bash completion: %v\n%s", err, output)
				}
			} else {
				// Stub only Zsh's display function. Cobra's actual parser and
				// sbc's completion endpoint still produce the choices.
				script := `autoload -Uz compinit; compinit -D
. ` + shell.Quote(file) + `
_describe() { local -a choices; choices=("${(@P)2}"); print -rl -- "${choices[@]}"; }
words=(sbc route c); CURRENT=3; _sbc
`
				command = exec.Command(path, "-f", "-c", script)
				command.Env = env
				output, err := command.CombinedOutput()
				if err != nil || !strings.Contains(string(output), "china-direct") || !strings.Contains(string(output), "Direct recognized China destinations") {
					t.Fatalf("Zsh completion: %v\n%s", err, output)
				}
			}

			command = exec.Command(binary, "init", name)
			command.Env = env
			code, err = command.Output()
			if err != nil {
				t.Fatal(err)
			}
			os.WriteFile(file, code, 0o600)
			check := "complete -p sbc\n"
			args := []string{"--noprofile", "--norc", "-i", "-c"}
			if name == "zsh" {
				args = []string{"-f", "-i", "-c"}
				check = `[[ ${_comps[sbc]} == _sbc ]]`
			}
			command = exec.Command(path, append(args, ". "+shell.Quote(file)+"\n. "+shell.Quote(file)+"\n"+check)...)
			command.Env = env
			if output, err := command.CombinedOutput(); err != nil {
				t.Fatalf("interactive init: %v\n%s", err, output)
			}
			check = "if complete -p sbc >/dev/null 2>&1; then exit 1; fi"
			args = []string{"--noprofile", "--norc", "-c"}
			if name == "zsh" {
				args = []string{"-f", "-c"}
				check = `(( ! $+functions[_sbc] ))`
			}
			command = exec.Command(path, append(args, ". "+shell.Quote(file)+"\n"+check)...)
			command.Env = env
			if output, err := command.CombinedOutput(); err != nil {
				t.Fatalf("noninteractive init loaded completion: %v\n%s", err, output)
			}
		})
	}
}

func TestCompletionNativePowerShell(t *testing.T) {
	var shells []string
	for _, name := range []string{"powershell", "pwsh"} {
		if path, err := exec.LookPath(name); err == nil {
			shells = append(shells, path)
		}
	}
	if len(shells) == 0 {
		t.Skip("PowerShell not on PATH")
	}
	_, env := completionBinary(t)
	for _, name := range shells {
		t.Run(filepath.Base(name), func(t *testing.T) {
			for _, mode := range []string{"TabCompleteNext", "Complete", "MenuComplete"} {
				t.Run(mode, func(t *testing.T) {
					script := `$ErrorActionPreference = 'Stop'
Set-PSReadLineKeyHandler -Key Tab -Function __MODE__
sbc completion powershell | Out-String | Invoke-Expression
foreach ($line in @('sbc pro', 'sbc route c', 'sbc route g', 'sbc ip --a', 'sbc install --shell z', 'sbc install --shell=z', 'sbc.exe protocol n')) {
    $result = [System.Management.Automation.CommandCompletion]::CompleteInput($line, $line.Length, $null)
    if ($line -eq 'sbc route c' -and $result.CompletionMatches[0].ToolTip -notmatch 'Direct recognized China destinations') { throw 'Route tooltip missing' }
    if ($line -eq 'sbc ip --a' -and $result.CompletionMatches[0].ToolTip -notmatch 'Also probe Gemini, ChatGPT, and Claude') { throw 'Flag tooltip missing' }
    $result.CompletionMatches | ForEach-Object {
        $text = $_.CompletionText.Trim()
        if ('__MODE__' -eq 'Complete' -and $result.CompletionMatches.Count -gt 1) {
            # Cobra's Bash-like mode puts descriptions in the display text.
            # Verify that format before extracting names for the shared check.
            $pattern = '^(?<name>\S+)\s+\(' + [regex]::Escape($_.ToolTip.Trim()) + '\)$'
            if ($text -notmatch $pattern) { throw "Completion description missing: $text" }
            $Matches['name']
        } else {
            $text
        }
    }
}
`
					script = strings.ReplaceAll(script, "__MODE__", mode)
					if runtime.GOOS != "windows" {
						script = strings.ReplaceAll(script, "sbc.exe", "sbc")
					}
					command := exec.Command(name, "-NoProfile", "-NonInteractive", "-Command", script)
					command.Env = slices.DeleteFunc(slices.Clone(env), func(value string) bool { return strings.HasPrefix(strings.ToUpper(value), "PSMODULEPATH=") })
					output, err := command.CombinedOutput()
					got := strings.ReplaceAll(string(output), "\r\n", "\n")
					if err != nil || got != "protocol\nchina-direct\ngfw-proxy\nglobal\n--ai\n--allow-direct-ai\nzsh\n--shell=zsh\nnaive\n" {
						t.Fatalf("PowerShell completion: %v\n%s", err, output)
					}
				})
			}
		})
	}
}
