package cli

import (
	"bytes"
	"io"
	"strings"
	"testing"

	"github.com/charmbracelet/colorprofile"
	"github.com/xiaosq2000/sing-box-manager/internal/tui"
)

func TestHumanOutputContextIsSharedAndBuffersStayPlain(t *testing.T) {
	var text bytes.Buffer
	reads := 0
	env := Env{Stdout: &text, outputState: &humanOutput{}, Getenv: func(string) string { reads++; return "" }}
	out := env.output()
	initialReads := reads
	for range 3 {
		printStatusLine(env, "service: running\nroute: china\n")
	}
	if env.output() != out || reads != initialReads {
		t.Fatal("line formatting rebuilt the output context")
	}
	text.Reset()
	out.Theme = tui.NewTheme(colorprofile.ANSI)
	code := withPanel(env, func(buffered Env) int {
		if buffered.output() == out || buffered.output().Theme.Styled() {
			t.Fatal("buffer inherited the real output theme")
		}
		printStatusLine(buffered, "service: running\nlong privacy setting: off\n")
		return 0
	})
	if code != 0 || strings.Count(text.String(), "\x1b[1m") != 2 {
		t.Fatalf("panel styled more than once: %q", text.String())
	}
}

type failedHumanWriter struct{ short bool }

func (w failedHumanWriter) Write(p []byte) (int, error) {
	if w.short {
		return len(p) / 2, nil
	}
	return 0, io.ErrClosedPipe
}

func TestHumanOutputWriteFailureReturnsFailure(t *testing.T) {
	for _, short := range []bool{false, true} {
		env, _, errOut := inertEnv(t)
		env.Stdout = failedHumanWriter{short: short}
		if code := Run(env, []string{"help"}); code != 1 || errOut.Len() == 0 {
			t.Fatalf("short=%v: write failure returned %d, stderr %q", short, code, errOut)
		}
	}
}

func TestMachineOutputDoesNotInitializeHumanContext(t *testing.T) {
	for _, args := range [][]string{{"version"}, {"completion", "bash"}} {
		env, _, _ := inertEnv(t)
		env.Getenv = func(key string) string { t.Fatalf("%v initialized terminal environment %s", args, key); return "" }
		if code := Run(env, args); code != 0 {
			t.Fatal(code)
		}
	}
}
