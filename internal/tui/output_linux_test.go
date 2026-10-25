//go:build linux

package tui

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
	"golang.org/x/sys/unix"
)

func testTerminal(t *testing.T) (*os.File, *os.File, *unix.Termios) {
	t.Helper()
	fd, err := unix.Open("/dev/ptmx", unix.O_RDWR|unix.O_NOCTTY|unix.O_NONBLOCK, 0)
	if err != nil {
		t.Fatal(err)
	}
	master := os.NewFile(uintptr(fd), "pty-master")
	t.Cleanup(func() { master.Close() })
	if err := unix.IoctlSetPointerInt(fd, unix.TIOCSPTLCK, 0); err != nil {
		t.Fatal(err)
	}
	n, err := unix.IoctlGetInt(fd, unix.TIOCGPTN)
	if err != nil {
		t.Fatal(err)
	}
	slave, err := os.OpenFile(fmt.Sprintf("/dev/pts/%d", n), os.O_RDWR|unix.O_NOCTTY, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { slave.Close() })
	if err := unix.IoctlSetWinsize(int(slave.Fd()), unix.TIOCSWINSZ, &unix.Winsize{Row: 24, Col: 80}); err != nil {
		t.Fatal(err)
	}
	before, err := unix.IoctlGetTermios(int(slave.Fd()), unix.TCGETS)
	if err != nil {
		t.Fatal(err)
	}
	return master, slave, before
}

// failSizeWriter lets output detection succeed, then fails the size read after
// raw mode starts. It does not close or damage the real terminal descriptor.
type failSizeWriter struct {
	*os.File
	unusable *os.File
	rawCalls int
	injected bool
}

func (f *failSizeWriter) Fd() uintptr {
	fd := f.File.Fd()
	state, err := unix.IoctlGetTermios(int(fd), unix.TCGETS)
	if err == nil && state.Lflag&unix.ICANON == 0 {
		f.rawCalls++
		if f.rawCalls == 2 {
			f.injected = true
			return f.unusable.Fd()
		}
	}
	return fd
}

type immediateQuit struct{ tea.Model }

func (m immediateQuit) Init() tea.Cmd { return tea.Quit }

func TestProgramRestoresRawModeAfterStartupFailure(t *testing.T) {
	_, slave, before := testTerminal(t)
	null, err := os.Open(os.DevNull)
	if err != nil {
		t.Fatal(err)
	}
	defer null.Close()
	out := NewOutput(slave, slave, func(key string) string {
		if key == "TERM" {
			return "xterm"
		}
		return ""
	})
	defer out.Close()
	writer := &failSizeWriter{File: slave, unusable: null}
	out.out = writer
	model := NewSelectorModel(out.Theme, "", "", "", []SelectorItem{{Value: "one"}})
	program, err := out.Program(immediateQuit{model})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := program.Run(); err == nil {
		t.Fatal("startup failure returned success")
	}
	if !writer.injected {
		t.Fatal("failure did not occur after entering raw mode")
	}
	after, err := unix.IoctlGetTermios(int(slave.Fd()), unix.TCGETS)
	if err != nil || !reflect.DeepEqual(before, after) {
		t.Fatalf("raw mode was not restored: %v", err)
	}
	// Sending after failed startup must return, not wait for a missing event loop.
	program.Send(LiveTableDoneMsg{})
}

type recordingTerminalWriter struct {
	*os.File
	mu     sync.Mutex
	buf    bytes.Buffer
	reject string
}

func (w *recordingTerminalWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.buf.Write(p)
	if string(p) == w.reject {
		return 0, io.ErrClosedPipe
	}
	return w.File.Write(p)
}

func (w *recordingTerminalWriter) text() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.buf.String()
}

func TestProgramClearsInlineViewBeforeFinalReport(t *testing.T) {
	for _, size := range []unix.Winsize{{Col: 80, Row: 24}, {Col: 40, Row: 3}, {Col: 1, Row: 1}} {
		for _, interrupted := range []bool{false, true} {
			for _, failClear := range []bool{false, true} {
				t.Run(fmt.Sprintf("%dx%d/interrupted=%t/fail-clear=%t", size.Col, size.Row, interrupted, failClear), func(t *testing.T) {
					_, slave, original := testTerminal(t)
					if err := unix.IoctlSetWinsize(int(slave.Fd()), unix.TIOCSWINSZ, &size); err != nil {
						t.Fatal(err)
					}
					out := NewOutput(slave, slave, func(key string) string {
						if key == "TERM" {
							return "xterm"
						}
						return ""
					})
					defer out.Close()
					writer := &recordingTerminalWriter{File: slave}
					out.out = writer
					model := NewLiveTableModel(out.Theme, "scope: sbc", []string{"Target", "Result"}, [][]string{{"pending-target", "testing"}}, nil, "testing", nil)
					model.Width, model.Height = int(size.Col), int(size.Row)
					rows := strings.Count(model.RenderFrame(), "\n") + 1
					want := "\r"
					if rows > 1 {
						want += ansi.CursorUp(rows - 1)
					}
					want += ansi.EraseScreenBelow
					if failClear {
						writer.reject = want
					}
					marker := "scope: sbc"
					if size.Col == 1 {
						marker = "<"
					}
					program, err := out.Program(model)
					if err != nil {
						t.Fatal(err)
					}
					done := make(chan error, 1)
					go func() { _, err := program.Run(); done <- err }()
					t.Cleanup(func() { program.Quit() })
					deadline := time.Now().Add(3 * time.Second)
					for !strings.Contains(writer.text(), marker) && time.Now().Before(deadline) {
						time.Sleep(10 * time.Millisecond)
					}
					before := writer.text()
					if !strings.Contains(before, marker) {
						t.Fatal("live view did not render")
					}
					if interrupted {
						program.Send(tea.KeyPressMsg{Code: 'c', Mod: tea.ModCtrl})
					} else {
						program.Send(LiveTableDoneMsg{})
					}
					select {
					case err := <-done:
						if failClear && !errors.Is(err, io.ErrClosedPipe) || !failClear && err != nil {
							t.Fatalf("clear failure=%t: %v", failClear, err)
						}
					case <-time.After(3 * time.Second):
						t.Fatal("live view did not stop")
					}
					cleanup := writer.text()[len(before):]
					if !strings.HasSuffix(cleanup, want) {
						t.Fatalf("inline view did not clear from its first row: got %q, want suffix %q", cleanup, want)
					}
					if strings.Contains(cleanup, ansi.EraseEntireScreen) || strings.Contains(cleanup, ansi.CursorHomePosition) {
						t.Fatal("cleanup erased earlier terminal history")
					}
					after, err := unix.IoctlGetTermios(int(slave.Fd()), unix.TCGETS)
					if err != nil || !reflect.DeepEqual(original, after) {
						t.Fatal("terminal modes were not restored")
					}
				})
			}
		}
	}
}

type failedTerminalWriter struct{ *os.File }

func (failedTerminalWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }

func TestProgramReportsWriteFailureAndRestoresModes(t *testing.T) {
	_, slave, before := testTerminal(t)
	out := NewOutput(slave, slave, func(key string) string {
		if key == "TERM" {
			return "xterm"
		}
		return ""
	})
	out.out = failedTerminalWriter{slave}
	model := NewSelectorModel(out.Theme, "", "", "", []SelectorItem{{Value: "one"}})
	// No key or quit command is supplied. The write failure must stop the UI.
	program, err := out.Program(model)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := program.Run(); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("write failure returned %v", err)
	}
	if err := out.Close(); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatal("output lost its write error")
	}
	after, err := unix.IoctlGetTermios(int(slave.Fd()), unix.TCGETS)
	if err != nil || !reflect.DeepEqual(before, after) {
		t.Fatalf("raw mode was not restored: %v", err)
	}
}
