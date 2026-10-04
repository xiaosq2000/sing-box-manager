package cli

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func run(args ...string) (code int, stdout, stderr string) {
	var out, errOut bytes.Buffer
	code = Run(DefaultEnv("1.2.3", strings.NewReader(""), &out, &errOut), args)
	return code, out.String(), errOut.String()
}

func TestVersionPrintsTheBuildVersion(t *testing.T) {
	for _, args := range [][]string{{"version"}, {"--version"}} {
		code, stdout, stderr := run(args...)
		if code != 0 || stdout != "sbc 1.2.3\n" || stderr != "" {
			t.Errorf("sbc %v: got code %d, stdout %q, stderr %q", args, code, stdout, stderr)
		}
	}
}

func TestHelpAndNoArgumentsPrintUsage(t *testing.T) {
	for _, args := range [][]string{nil, {"help"}, {"-h"}, {"--help"}} {
		code, stdout, stderr := run(args...)
		if code != 0 || !strings.HasPrefix(stdout, "Usage: sbc <command>\n") || stderr != "" {
			t.Errorf("sbc %v: got code %d, stdout %q, stderr %q", args, code, stdout, stderr)
		}
	}
}

func TestUnknownCommandFailsWithAHint(t *testing.T) {
	code, stdout, stderr := run("bogus")
	if code != 2 || stdout != "" {
		t.Fatalf("got code %d, stdout %q", code, stdout)
	}
	for _, want := range []string{`unknown command "bogus"`, "sbc help"} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr %q does not contain %q", stderr, want)
		}
	}
}

func TestInstallNeedsALinkOnStandardInput(t *testing.T) {
	var out, errOut bytes.Buffer
	env := DefaultEnv("1.2.3", strings.NewReader(""), &out, &errOut)
	env.Layout = func() (paths.Layout, error) { return paths.Layout{Config: t.TempDir(), Data: t.TempDir()}, nil }

	if code := Run(env, []string{"install"}); code != 1 {
		t.Fatalf("got code %d", code)
	}
	if !strings.Contains(errOut.String(), "no subscription link was given") {
		t.Errorf("stderr %q", errOut.String())
	}

	env.Stdin = strings.NewReader("https://vpn.example.com/files/x\n")
	errOut.Reset()
	Run(env, []string{"install"})
	if !strings.Contains(errOut.String(), "not a subscription link") {
		t.Errorf("stderr %q", errOut.String())
	}
}

func TestRunHandsTheProcessToSingBox(t *testing.T) {
	layout := paths.Layout{Config: "/config", Data: "/data"}
	var got []string
	env := DefaultEnv("1.2.3", strings.NewReader(""), &bytes.Buffer{}, &bytes.Buffer{})
	env.Layout = func() (paths.Layout, error) { return layout, nil }
	env.Exec = func(binary string, args []string, _ io.Writer, _ string) error {
		got = append([]string{binary}, args...)
		return nil
	}

	if code := Run(env, []string{"run"}); code != 0 {
		t.Fatalf("got code %d", code)
	}
	want := []string{"/data/bin/sing-box", "/data/bin/sing-box", "run", "-D", "/data", "-c", "/config/config.json"}
	if strings.Join(got, " ") != strings.Join(want, " ") {
		t.Errorf("exec %v", got)
	}
}

func TestRunOnWindowsKeepsSingBoxOutputInTheLogAndDropsRetiredPrograms(t *testing.T) {
	root := t.TempDir()
	layout := paths.Layout{Config: root, Data: root, Exe: ".exe"}
	os.MkdirAll(layout.Bin(), 0o755)
	os.MkdirAll(layout.CLIDir(), 0o755)
	for _, retired := range []string{layout.SingBox() + ".old", layout.SBC() + ".old"} {
		os.WriteFile(retired, []byte("old"), 0o755)
	}
	// A log past the limit starts over.
	os.WriteFile(layout.Log(), bytes.Repeat([]byte("x"), logLimit+1), 0o600)
	env := DefaultEnv("1.2.3", strings.NewReader(""), &bytes.Buffer{}, &bytes.Buffer{})
	env.OS = "windows"
	env.Layout = func() (paths.Layout, error) { return layout, nil }
	env.Exec = func(binary string, args []string, output io.Writer, pidFile string) error {
		fmt.Fprintf(output, "%s %s\n", filepath.Base(binary), strings.Join(args[1:], " "))
		if pidFile != layout.PIDFile() {
			t.Errorf("pid file %s", pidFile)
		}
		return nil
	}

	if code := Run(env, []string{"run"}); code != 0 {
		t.Fatalf("got code %d", code)
	}

	log, _ := os.ReadFile(layout.Log())
	if string(log) != "sing-box.exe run -D "+root+" -c "+layout.ConfigFile()+"\n" {
		t.Errorf("log %q", log)
	}
	for _, retired := range []string{layout.SingBox() + ".old", layout.SBC() + ".old"} {
		if _, err := os.Stat(retired); !os.IsNotExist(err) {
			t.Errorf("%s stayed", retired)
		}
	}
}
