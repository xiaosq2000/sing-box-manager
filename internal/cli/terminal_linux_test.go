//go:build linux

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/charmbracelet/x/ansi"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/probe"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tui"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

type terminalResult struct {
	Code           int
	Mode, Protocol string
}

// TestTerminalHelper runs only in a child with synthetic installation state.
func TestTerminalHelper(t *testing.T) {
	resultFile := os.Getenv("SBC_TTY_RESULT")
	if resultFile == "" {
		return
	}
	h := newHarness(t)
	h.env.Stdin, h.env.Stdout, h.env.Stderr = os.Stdin, os.Stdout, os.Stderr
	h.env.Getenv = os.Getenv
	if os.Getenv("SBC_TTY_INJECT_NO_COLOR") == "1" {
		h.env.Getenv = func(key string) string {
			if key == "NO_COLOR" {
				return "1"
			}
			return os.Getenv(key)
		}
	}
	kind := os.Getenv("SBC_TTY_KIND")
	args := []string{"route"}
	switch kind {
	case "help":
		args = []string{"help"}
	case "shell":
		args = []string{"init", "bash"}
	case "completion":
		args = []string{"completion", "bash"}
	}
	if kind == "hostile" {
		h.api.protocols = []string{"trojan", "naive\x1b]52;c;ZmFrZQ==\a\u202e\nRoute: direct\r\t"}
		args = []string{"protocol"}
	}
	if kind == "locked" {
		unlock, err := maintenance.Lock(h.layout)
		if err != nil {
			t.Fatal(err)
		}
		defer unlock()
	}
	if kind == "dirty" {
		h.env.TunCapture = func() tun.CaptureState {
			if _, err := os.Stat(resultFile + ".gate"); err == nil {
				return tun.CaptureState{Status: "dirty", Phase: "dirty"}
			}
			return tun.CaptureState{Status: "off", Phase: "off"}
		}
	}
	if kind == "missing" || kind == "empty" {
		h.api.protocol = "retained-protocol"
		h.api.protocols = []string{"trojan", "naive"}
		if kind == "empty" {
			h.api.protocols = []string{}
		}
		args = []string{"protocol"}
	}
	if kind == "privacy" {
		i18n.SetChinese(true)
		defer i18n.SetChinese(false)
		args = []string{"privacy"}
	}
	if kind == "ip" || kind == "ip-json" {
		proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Host == "details.test" {
				fmt.Fprint(w, `{"success":true,"ip":"203.0.113.7"}`)
				return
			}
			select {
			case <-time.After(700 * time.Millisecond):
				fmt.Fprint(w, "203.0.113.7")
			case <-r.Context().Done():
			}
		}))
		defer proxy.Close()
		u, _ := url.Parse(proxy.URL)
		port, _ := strconv.Atoi(u.Port())
		h.writeLocal(t, func(local *singbox.Local) { local.ListenPort = port })
		h.env.Sites = probe.Sites{IPify4: "http://first.test/", IdentMe4: "http://second.test/", IPDetails: "http://details.test/"}
		args = []string{"ip"}
		if kind == "ip-json" {
			args = append(args, "--json")
		}
	}
	result := terminalResult{Code: Run(h.env, args)}
	if kind == "hostile" && result.Code == 0 {
		result.Code = Run(h.env, []string{"status"})
	}
	h.api.lock.Lock()
	result.Mode, result.Protocol = h.api.mode, h.api.protocol
	h.api.lock.Unlock()
	data, _ := json.Marshal(result)
	if err := os.WriteFile(resultFile, data, 0600); err != nil {
		t.Fatal(err)
	}
}

type terminalCapture struct {
	sync.Mutex
	buf        bytes.Buffer
	queryReply io.Writer
	queries    int
}

func (c *terminalCapture) Write(p []byte) (int, error) {
	c.Lock()
	defer c.Unlock()
	n, err := c.buf.Write(p)
	if c.queryReply != nil {
		// Emulate replies from a terminal, including queries sent during Go init.
		queries := bytes.Count(c.buf.Bytes(), []byte("\x1b[6n"))
		for c.queries < queries {
			fmt.Fprint(c.queryReply, "\x1b]11;rgb:0000/0000/0000\x1b\\\x1b[1;1R")
			c.queries++
		}
	}
	return n, err
}
func (c *terminalCapture) text() string { c.Lock(); defer c.Unlock(); return c.buf.String() }

type terminalSession struct {
	master, slave *os.File
	before        *unix.Termios
	cmd           *exec.Cmd
	output        *terminalCapture
	resultFile    string
	done          chan error
	readDone      chan struct{}
}

type terminalSettings struct {
	foreground bool
	env        map[string]string
}

func startTerminal(t *testing.T, kind, stdin, stdout, term, color string, width, height uint16, settings ...terminalSettings) *terminalSession {
	t.Helper()
	fd, err := unix.Open("/dev/ptmx", unix.O_RDWR|unix.O_NOCTTY|unix.O_NONBLOCK, 0)
	if err != nil {
		t.Fatal(err)
	}
	master := os.NewFile(uintptr(fd), "pty-master")
	t.Cleanup(func() { master.Close() })
	if err = unix.IoctlSetPointerInt(fd, unix.TIOCSPTLCK, 0); err != nil {
		t.Fatal(err)
	}
	n, err := unix.IoctlGetInt(fd, unix.TIOCGPTN)
	if err != nil {
		t.Fatal(err)
	}
	slave, err := os.OpenFile(fmt.Sprintf("/dev/pts/%d", n), os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { slave.Close() })
	if err = unix.IoctlSetWinsize(int(slave.Fd()), unix.TIOCSWINSZ, &unix.Winsize{Row: height, Col: width}); err != nil {
		t.Fatal(err)
	}
	before, err := unix.IoctlGetTermios(int(slave.Fd()), unix.TCGETS)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestTerminalHelper$", "-test.count=1")
	resultFile := filepath.Join(t.TempDir(), "result.json")
	cmd.Env = append(os.Environ(), "SBC_TTY_RESULT="+resultFile, "SBC_TTY_KIND="+kind, "TERM="+term, "NO_COLOR="+color, "COLORTERM=truecolor",
		"CI=", "CLICOLOR=", "CLICOLOR_FORCE=", "COLORFGBG=", "TERM_PROGRAM=", "GOOGLE_CLOUD_SHELL=", "SBC_LANG=en",
		"SBC_TTY_INJECT_NO_COLOR=", "TTY_FORCE=", "TEA_TRACE=", "TEA_DEBUG=")
	capture := &terminalCapture{}
	for _, setting := range settings {
		for key, value := range setting.env {
			cmd.Env = append(cmd.Env, key+"="+value)
		}
		if setting.foreground {
			cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true, Ctty: 0}
			capture.queryReply = master
		}
	}
	cmd.Stdin, cmd.Stdout, cmd.Stderr = slave, slave, slave
	if stdin == "null" {
		f, err := os.Open(os.DevNull)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { f.Close() })
		cmd.Stdin = f
	} else if stdin == "pipe" {
		cmd.Stdin = strings.NewReader("")
	}
	if stdout == "pipe" {
		cmd.Stdout, cmd.Stderr = capture, capture
	}
	if stdout == "null" {
		f, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { f.Close() })
		cmd.Stdout, cmd.Stderr = f, f
	}
	if err = cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cmd.Process.Kill() })
	s := &terminalSession{master: master, slave: slave, before: before, cmd: cmd, output: capture, resultFile: resultFile, done: make(chan error, 1), readDone: make(chan struct{})}
	go func() { defer close(s.readDone); io.Copy(capture, master) }()
	go func() { s.done <- cmd.Wait() }()
	return s
}

func (s *terminalSession) waitText(t *testing.T, text string) {
	t.Helper()
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		if strings.Contains(s.output.text(), text) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("terminal did not show %q: %q", text, s.output.text())
}
func (s *terminalSession) finish(t *testing.T) terminalResult {
	t.Helper()
	select {
	case err := <-s.done:
		if err != nil {
			t.Fatalf("terminal helper: %v: %q", err, s.output.text())
		}
	case <-time.After(5 * time.Second):
		t.Fatalf("terminal command hung: %q", s.output.text())
	}
	after, err := unix.IoctlGetTermios(int(s.slave.Fd()), unix.TCGETS)
	if err != nil || !reflect.DeepEqual(s.before, after) {
		t.Fatalf("terminal was not restored: %v", err)
	}
	s.slave.Close()
	select {
	case <-s.readDone:
	case <-time.After(time.Second):
		t.Fatal("terminal reader did not stop")
	}
	data, err := os.ReadFile(s.resultFile)
	if err != nil {
		t.Fatal(err)
	}
	var result terminalResult
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatal(err)
	}
	return result
}

func TestTerminalSelectorConfirmationAndSignals(t *testing.T) {
	for _, tc := range []struct {
		name, kind, keys string
		signal           os.Signal
		code             int
		mode             string
	}{
		{name: "enter", keys: "\x1b[B\r", mode: "gfw"},
		{name: "space-q", keys: "\x1b[B q", mode: "china"},
		{name: "escape", keys: "\x1b[B\x1b", mode: "china"},
		{name: "ctrl-c", keys: "\x03", code: 130, mode: "china"},
		{name: "sigint", signal: os.Interrupt, code: 130, mode: "china"},
		{name: "sigterm", signal: syscall.SIGTERM, code: 143, mode: "china"},
		{name: "locked", kind: "locked", keys: "\x1b[B\r", code: 1, mode: "china"},
		{name: "dirty", kind: "dirty", keys: "\x1b[B\r", code: 1, mode: "china"},
		{name: "hostile", kind: "hostile", keys: "\x1b[B\r", mode: "china"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := startTerminal(t, tc.kind, "pty", "pty", "xterm-256color", "", 120, 24)
			s.waitText(t, "> ")
			if tc.kind == "dirty" {
				if err := os.WriteFile(s.resultFile+".gate", nil, 0600); err != nil {
					t.Fatal(err)
				}
			}
			if tc.signal != nil {
				if err := s.cmd.Process.Signal(tc.signal); err != nil {
					t.Fatal(err)
				}
			} else {
				s.master.Write([]byte(tc.keys))
			}
			r := s.finish(t)
			if r.Code != tc.code || r.Mode != tc.mode {
				t.Fatalf("result %+v", r)
			}
			if tc.kind == "hostile" {
				if !strings.Contains(r.Protocol, "\x1b]52;") {
					t.Fatal("raw API value changed")
				}
				if strings.Contains(s.output.text(), "\x1b]52;") || strings.Contains(s.output.text(), "\u202e") {
					t.Fatal("unsafe terminal sequence")
				}
				plain := ansi.Strip(s.output.text())
				if regexp.MustCompile(`(?m)^Route:\s+direct`).MatchString(plain) {
					t.Fatalf("data injected an output row: %q", plain)
				}
				if !strings.HasSuffix(r.Protocol, "\nRoute: direct\r\t") {
					t.Fatal("API value lost its control characters")
				}
			}
		})
	}
}

func TestTerminalStaticFallbacks(t *testing.T) {
	for _, tc := range []struct{ in, out, term string }{
		{"pty", "null", "xterm-256color"}, {"null", "pty", "xterm-256color"},
		{"pipe", "pty", "xterm-256color"}, {"pty", "pipe", "xterm-256color"}, {"pty", "pty", "dumb"},
	} {
		t.Run(tc.in+"-"+tc.out+"-"+tc.term, func(t *testing.T) {
			s := startTerminal(t, "", tc.in, tc.out, tc.term, "", 120, 24)
			if r := s.finish(t); r.Code != 0 || r.Mode != "china" {
				t.Fatalf("result %+v", r)
			}
			text := s.output.text()
			withoutColors := regexp.MustCompile(`\x1b\[[0-9;:]*m`).ReplaceAllString(text, "")
			if strings.Contains(text, "> ") || strings.Contains(withoutColors, "\x1b") {
				t.Fatalf("static output used cursor controls: %q", text)
			}
			if (tc.out == "pipe" || tc.term == "dumb") && strings.Contains(text, "\x1b") {
				t.Fatal("plain output emitted ANSI")
			}
			if tc.out != "null" && !strings.Contains(text, "china-direct (current)") {
				t.Fatalf("missing static list: %q", text)
			}
		})
	}
}

func TestTerminalIncompleteChoiceListsStayReadOnly(t *testing.T) {
	for _, kind := range []string{"missing", "empty"} {
		for _, output := range []string{"pty", "pipe"} {
			t.Run(kind+"-"+output, func(t *testing.T) {
				s := startTerminal(t, kind, "pty", output, "xterm-256color", "", 80, 24)
				r := s.finish(t)
				text := s.output.text()
				if r.Code != 0 || r.Protocol != "retained-protocol" || !strings.Contains(text, "retained-protocol") || strings.Contains(text, "> ") {
					t.Fatalf("incomplete list changed or hid the selection: %+v: %q", r, text)
				}
			})
		}
	}
}

func TestTerminalPanelAndJSONOutput(t *testing.T) {
	for _, output := range []string{"pty", "pipe"} {
		t.Run("privacy-"+output, func(t *testing.T) {
			s := startTerminal(t, "privacy", "pty", output, "xterm-256color", "1", 120, 30)
			if r := s.finish(t); r.Code != 0 {
				t.Fatalf("result %+v", r)
			}
			text := ansi.Strip(s.output.text())
			labels := regexp.MustCompile(`^([\p{L}0-9 _-]+[:：])( +)`)
			column, count := -1, 0
			for _, line := range strings.Split(text, "\n") {
				if match := labels.FindString(line); match != "" {
					if column < 0 {
						column = tui.DisplayWidth(match)
					}
					if tui.DisplayWidth(match) != column {
						t.Fatalf("unaligned panel: %q", text)
					}
					count++
				}
			}
			if count < 5 {
				t.Fatalf("panel rows missing: %q", text)
			}
		})
	}
	s := startTerminal(t, "ip-json", "pty", "pty", "xterm-256color", "", 80, 24)
	if r := s.finish(t); r.Code != 0 {
		t.Fatalf("result %+v", r)
	}
	var report diagnosticReport
	if err := json.NewDecoder(strings.NewReader(s.output.text())).Decode(&report); err != nil {
		t.Fatal(err)
	}
	if report.SchemaVersion != 1 || len(report.Observations) != 2 || strings.Contains(s.output.text(), "\x1b") {
		t.Fatal("TTY JSON output changed")
	}
}

func TestTerminalDiagnosticsColorResizeAndCancellation(t *testing.T) {
	for _, tc := range []struct {
		name, term, color, keys string
		signal                  os.Signal
		code                    int
	}{
		{name: "color", term: "xterm-256color"}, {name: "no-color", term: "xterm-256color", color: "1"},
		{name: "dumb", term: "dumb"}, {name: "ctrl-c", term: "xterm-256color", keys: "\x03", code: 130},
		{name: "sigint", term: "xterm-256color", signal: os.Interrupt, code: 130},
		{name: "sigterm", term: "xterm-256color", signal: syscall.SIGTERM, code: 143},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := startTerminal(t, "ip", "pty", "pty", tc.term, tc.color, 80, 24)
			if tc.term != "dumb" {
				s.waitText(t, "testing...")
				if tc.name == "color" {
					unix.IoctlSetWinsize(int(s.slave.Fd()), unix.TIOCSWINSZ, &unix.Winsize{Row: 3, Col: 40})
					s.cmd.Process.Signal(syscall.SIGWINCH)
					time.Sleep(80 * time.Millisecond)
					unix.IoctlSetWinsize(int(s.slave.Fd()), unix.TIOCSWINSZ, &unix.Winsize{Row: 24, Col: 80})
					s.cmd.Process.Signal(syscall.SIGWINCH)
				}
				if tc.keys != "" {
					s.master.Write([]byte(tc.keys))
				}
				if tc.signal != nil {
					s.cmd.Process.Signal(tc.signal)
				}
			}
			r := s.finish(t)
			if r.Code != tc.code {
				t.Fatalf("result %+v: %q", r, s.output.text())
			}
			text := s.output.text()
			if tc.code == 0 && !strings.Contains(text, "203.0.113.7") {
				t.Fatal("final IP missing")
			}
			if tc.color != "" && regexp.MustCompile(`\x1b\[[0-9;:]*m`).MatchString(text) {
				t.Fatal("NO_COLOR emitted SGR")
			}
			if tc.term == "dumb" && strings.Contains(text, "\x1b[") {
				t.Fatal("dumb terminal used controls")
			}
		})
	}
}
