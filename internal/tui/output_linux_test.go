//go:build linux

package tui

import (
	"errors"
	"fmt"
	"io"
	"os"
	"reflect"
	"testing"

	tea "charm.land/bubbletea/v2"
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
