package shell

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func shells(t *testing.T) []string {
	var found []string
	for _, name := range []string{"bash", "zsh"} {
		if _, err := exec.LookPath(name); err == nil {
			found = append(found, name)
		}
	}
	if len(found) == 0 {
		t.Skip("neither bash nor zsh is installed")
	}
	return found
}

type setup struct {
	dir, cli, env, hint, init string
}

// newSetup writes `sbc init` output for shellName, with a space in the paths
// as macOS's Application Support has, and a fake sbc on its PATH entry.
func newSetup(t *testing.T, shellName string) setup {
	t.Helper()
	dir := t.TempDir()
	s := setup{
		dir:  dir,
		cli:  filepath.Join(dir, "Application Support", "cli"),
		env:  filepath.Join(dir, "env.sh"),
		hint: filepath.Join(dir, "hint"),
		init: filepath.Join(dir, "init.sh"),
	}
	os.MkdirAll(s.cli, 0o755)
	os.WriteFile(filepath.Join(s.cli, "sbc"), []byte("#!/bin/sh\necho fake sbc \"$@\"\n"), 0o755)
	code, err := Init(shellName, s.cli, s.env, s.hint)
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(s.init, []byte(code), 0o644)
	return s
}

func (s setup) run(t *testing.T, shellName, script string, extra ...string) string {
	t.Helper()
	args := append(extra, "-c", ". "+Quote(s.init)+"\n"+script)
	command := exec.Command(shellName, args...)
	// Keep the user's own rc files out of the interactive runs.
	command.Env = []string{"PATH=/usr/bin:/bin", "HOME=" + s.dir, "ZDOTDIR=" + s.dir}
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("%s: %v\n%s", shellName, err, output)
	}
	return string(output)
}

func TestInitPutsSbcOnPathAndRegistersTheHookOnce(t *testing.T) {
	for _, shellName := range shells(t) {
		s := newSetup(t, shellName)
		hooks := `echo "$PROMPT_COMMAND"`
		if shellName == "zsh" {
			hooks = `echo "${precmd_functions[@]}"`
		}

		output := s.run(t, shellName, ". "+Quote(s.init)+"\nsbc status\n"+
			`echo "$PATH" | tr : '\n' | grep -c cli`+"\n"+hooks)

		lines := strings.Split(strings.TrimSpace(output), "\n")
		if len(lines) != 3 || lines[0] != "fake sbc status" || lines[1] != "1" || strings.Count(lines[2], "_sbc_apply") != 1 {
			t.Errorf("%s printed:\n%s", shellName, output)
		}
	}
}

func TestEachPromptAppliesOnAndOff(t *testing.T) {
	for _, shellName := range shells(t) {
		s := newSetup(t, shellName)
		os.WriteFile(filepath.Join(s.dir, "on"), []byte(EnvOn(4321, "", "")), 0o644)

		// _sbc_apply is what each prompt runs.
		output := s.run(t, shellName, `
echo "start: ${http_proxy-unset}"
cp `+Quote(filepath.Join(s.dir, "on"))+` `+Quote(s.env)+`
_sbc_apply
echo "on: $http_proxy $SOCKS_PROXY"
rm `+Quote(s.env)+`
_sbc_apply
echo "off: ${http_proxy-unset} ${no_proxy-unset}"`)

		want := "start: unset\non: http://127.0.0.1:4321 socks5://127.0.0.1:4321\noff: unset unset\n"
		if output != want {
			t.Errorf("%s printed:\n%s", shellName, output)
		}
	}
}

func TestTheHookLeavesProxiesItDidNotSetAndHonorsTheOptOut(t *testing.T) {
	for _, shellName := range shells(t) {
		s := newSetup(t, shellName)

		own := s.run(t, shellName, `
export http_proxy=http://corporate:3128
_sbc_apply
echo "$http_proxy"`)
		if own != "http://corporate:3128\n" {
			t.Errorf("%s cleared a proxy sbc did not set: %q", shellName, own)
		}

		os.WriteFile(s.env, []byte(EnvOn(4321, "", "")), 0o644)
		optOut := s.run(t, shellName, `
export SBC_PROXY=off
_sbc_apply
echo "${http_proxy-unset}"`)
		if optOut != "unset\n" {
			t.Errorf("%s with SBC_PROXY=off: %q", shellName, optOut)
		}
	}
}

func TestOnlyInteractiveShellsPrintTheUpgradeHint(t *testing.T) {
	for _, shellName := range shells(t) {
		s := newSetup(t, shellName)
		os.WriteFile(s.hint, nil, 0o644)

		if output := s.run(t, shellName, "true"); strings.Contains(output, "sbc upgrade") {
			t.Errorf("%s printed the hint in a script: %q", shellName, output)
		}
		if output := s.run(t, shellName, "true", "-i"); !strings.Contains(output, "run 'sbc upgrade'") {
			t.Errorf("%s did not print the hint interactively: %q", shellName, output)
		}
	}
}

func TestEnvOnPutsThePasswordInTheURLs(t *testing.T) {
	lines := EnvOn(4321, "sbc", "pw")
	for _, want := range []string{
		"export https_proxy='http://sbc:pw@127.0.0.1:4321'\n",
		"export SOCKS_PROXY='socks5://sbc:pw@127.0.0.1:4321'\n",
		"export no_proxy='" + NoProxy + "'\n",
	} {
		if !strings.Contains(lines, want) {
			t.Errorf("missing %q in\n%s", want, lines)
		}
	}
}

func TestInitRejectsOtherShells(t *testing.T) {
	if _, err := Init("fish", "/cli", "/env", "/hint"); err == nil {
		t.Error("fish was accepted")
	}
}

func TestTheRCBlockIsAddedOnceAndRemovedCleanly(t *testing.T) {
	rc := filepath.Join(t.TempDir(), ".bashrc")
	os.WriteFile(rc, []byte("alias ll='ls -l'"), 0o644)

	for range 2 {
		if err := AddBlock(rc, "/home/me/.local/share/sbc/cli/sbc", "bash"); err != nil {
			t.Fatal(err)
		}
	}
	text, _ := os.ReadFile(rc)
	want := "alias ll='ls -l'\n" + blockStart + "\n" +
		`if [ -x '/home/me/.local/share/sbc/cli/sbc' ]; then eval "$('/home/me/.local/share/sbc/cli/sbc' init bash)"; fi` +
		"\n" + blockEnd + "\n"
	if string(text) != want {
		t.Errorf("after adding:\n%s", text)
	}

	if err := RemoveBlock(rc); err != nil {
		t.Fatal(err)
	}
	text, _ = os.ReadFile(rc)
	if string(text) != "alias ll='ls -l'\n" {
		t.Errorf("after removing: %q", text)
	}
	if err := RemoveBlock(filepath.Join(t.TempDir(), "missing")); err != nil {
		t.Errorf("a missing rc file: %v", err)
	}
}

func TestRCFileFollowsWhatEachShellReads(t *testing.T) {
	bash := ".bashrc"
	if runtime.GOOS == "darwin" {
		bash = ".bash_profile"
	}
	for shellName, want := range map[string]string{"bash": bash, "zsh": ".zshrc"} {
		if got, err := RCFile("/home/me", shellName); err != nil || got != filepath.Join("/home/me", want) {
			t.Errorf("%s: got %q, %v", shellName, got, err)
		}
	}
	if _, err := RCFile("/home/me", "fish"); err == nil {
		t.Error("fish was accepted")
	}
}

func TestQuoteSurvivesQuotes(t *testing.T) {
	output, err := exec.Command("sh", "-c", "printf %s "+Quote(`it's "a" $path`)).Output()
	if err != nil || string(output) != `it's "a" $path` {
		t.Errorf("got %q, %v", output, err)
	}
}
