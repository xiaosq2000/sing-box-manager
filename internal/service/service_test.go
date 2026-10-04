package service

import (
	"encoding/binary"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
)

type recorder struct {
	calls []string
	fail  map[string]bool
	// failOnce makes each failing call fail only the first time.
	failOnce bool
}

func (r *recorder) run(name string, args ...string) error {
	call := name + " " + strings.Join(args, " ")
	r.calls = append(r.calls, call)
	if r.fail[call] {
		if r.failOnce {
			delete(r.fail, call)
		}
		return os.ErrNotExist
	}
	return nil
}

func TestSystemdInstallsEnablesAndStartsTheUnits(t *testing.T) {
	dir := t.TempDir()
	r := &recorder{}
	s := &Systemd{Dir: dir, SBC: "/home/me/.local/share/sbc/bin/sbc", Run: r.run}

	if err := s.Install(); err != nil {
		t.Fatal(err)
	}
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}

	proxy, _ := os.ReadFile(filepath.Join(dir, "sbc.service"))
	if !strings.Contains(string(proxy), `ExecStart="/home/me/.local/share/sbc/bin/sbc" run`) {
		t.Errorf("sbc.service:\n%s", proxy)
	}
	timer, _ := os.ReadFile(filepath.Join(dir, "sbc-refresh.timer"))
	if !strings.Contains(string(timer), "OnUnitActiveSec=21600s") || !strings.Contains(string(timer), "RandomizedDelaySec=30min") {
		t.Errorf("sbc-refresh.timer:\n%s", timer)
	}
	refresh, _ := os.ReadFile(filepath.Join(dir, "sbc-refresh.service"))
	if !strings.Contains(string(refresh), `ExecStart="/home/me/.local/share/sbc/bin/sbc" update`) {
		t.Errorf("sbc-refresh.service:\n%s", refresh)
	}
	want := []string{
		"systemctl --user daemon-reload",
		"systemctl --user enable sbc.service sbc-refresh.timer",
		"systemctl --user start sbc.service sbc-refresh.timer",
	}
	if strings.Join(r.calls, "\n") != strings.Join(want, "\n") {
		t.Errorf("calls:\n%s", strings.Join(r.calls, "\n"))
	}
}

func TestSystemdRemoveDeletesTheUnitsEvenWhenTheyAreStopped(t *testing.T) {
	dir := t.TempDir()
	r := &recorder{fail: map[string]bool{"systemctl --user disable --now sbc.service sbc-refresh.timer": true}}
	s := &Systemd{Dir: dir, SBC: "/sbc", Run: r.run}
	s.Install()

	if err := s.Remove(); err != nil {
		t.Fatal(err)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 0 {
		t.Errorf("left %v", entries)
	}
}

func TestLaunchdUnloadsBeforeLoadingEachAgent(t *testing.T) {
	dir := t.TempDir()
	r := &recorder{}
	l := &Launchd{Dir: dir, SBC: "/Users/me/Library/Application Support/sbc/bin/sbc", Log: "/tmp/a&b.log", UID: 501, Run: r.run}

	if err := l.Install(); err != nil {
		t.Fatal(err)
	}
	if err := l.Start(); err != nil {
		t.Fatal(err)
	}

	proxy, _ := os.ReadFile(filepath.Join(dir, "io.sbc.proxy.plist"))
	for _, want := range []string{
		"<string>/Users/me/Library/Application Support/sbc/bin/sbc</string>",
		"<string>run</string>",
		"<key>KeepAlive</key>",
		"<string>/tmp/a&amp;b.log</string>",
	} {
		if !strings.Contains(string(proxy), want) {
			t.Errorf("io.sbc.proxy.plist lacks %s:\n%s", want, proxy)
		}
	}
	refresh, _ := os.ReadFile(filepath.Join(dir, "io.sbc.refresh.plist"))
	if !strings.Contains(string(refresh), "<integer>21600</integer>") {
		t.Errorf("io.sbc.refresh.plist:\n%s", refresh)
	}
	proxyPath := filepath.Join(dir, "io.sbc.proxy.plist")
	if r.calls[0] != "launchctl bootout gui/501 "+proxyPath || r.calls[1] != "launchctl bootstrap gui/501 "+proxyPath {
		t.Errorf("calls:\n%s", strings.Join(r.calls, "\n"))
	}
}

func TestNewPicksTheManagerForThePlatform(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", "/xdg")
	manager, err := New("linux", "/sbc", Exec)
	if err != nil {
		t.Fatal(err)
	}
	if manager.(*Systemd).Dir != "/xdg/systemd/user" {
		t.Errorf("dir %s", manager.(*Systemd).Dir)
	}
	if _, err := New("plan9", "/sbc", Exec); err == nil {
		t.Error("plan9 got a manager")
	}
}

// reader answers PowerShell's task state query with the state it is given,
// and records everything else like recorder.
type reader struct {
	recorder
	state string
}

func (r *reader) read(name string, args ...string) (string, error) {
	r.calls = append(r.calls, name+" "+strings.Join(args, " "))
	return r.state + "\r\n", nil
}

// base stands in for %LOCALAPPDATA%\sbc, with this platform's separators.
var base = filepath.Join("C:", "Users", "me", "AppData", "Local", "sbc")

// readUTF16 returns a task definition file as text.
func readUTF16(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) < 2 || data[0] != 0xFF || data[1] != 0xFE || len(data)%2 != 0 {
		t.Fatalf("%s is not UTF-16 with a byte order mark", path)
	}
	units := make([]uint16, 0, len(data)/2)
	for i := 2; i < len(data); i += 2 {
		units = append(units, binary.LittleEndian.Uint16(data[i:]))
	}
	return string(utf16.Decode(units))
}

func newTasks(t *testing.T, r *reader) *Tasks {
	t.Helper()
	return &Tasks{
		Dir:  t.TempDir(),
		SBC:  filepath.Join(base, "cli", "sbc.exe"),
		User: "S-1-5-21-1-2-3-1001",
		Run:  r.run,
		Read: r.read,
		Now:  func() time.Time { return time.Date(2026, 10, 3, 9, 0, 0, 0, time.UTC) },
	}
}

func TestTasksRunInTheBackgroundAndStartAtLogon(t *testing.T) {
	r := &reader{state: "Ready"}
	tasks := newTasks(t, r)

	if err := tasks.Install(); err != nil {
		t.Fatal(err)
	}
	if err := tasks.Start(); err != nil {
		t.Fatal(err)
	}

	if tasks.Interactive {
		t.Error("the tasks fell back to the user's session")
	}
	proxy := readUTF16(t, filepath.Join(tasks.Dir, "sbc-proxy.xml"))
	for _, want := range []string{
		`<?xml version="1.0" encoding="UTF-16"?>`,
		"<LogonTrigger>",
		"<UserId>S-1-5-21-1-2-3-1001</UserId>",
		"<LogonType>S4U</LogonType>",
		"<RunLevel>LeastPrivilege</RunLevel>",
		"<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
		"<Command>" + filepath.Join(base, "cli", "sbc.exe") + "</Command>",
		"<Arguments>run</Arguments>",
		"<WorkingDirectory>" + base + "</WorkingDirectory>",
	} {
		if !strings.Contains(string(proxy), want) {
			t.Errorf("sbc-proxy.xml lacks %s:\n%s", want, proxy)
		}
	}
	refresh := readUTF16(t, filepath.Join(tasks.Dir, "sbc-refresh.xml"))
	for _, want := range []string{
		"<StartBoundary>2026-10-03T09:10:00</StartBoundary>",
		"<Interval>PT6H</Interval>",
		"<RandomDelay>PT30M</RandomDelay>",
		"<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>",
		"<Arguments>update</Arguments>",
	} {
		if !strings.Contains(string(refresh), want) {
			t.Errorf("sbc-refresh.xml lacks %s:\n%s", want, refresh)
		}
	}
	sort.Strings(r.calls[:2])
	want := []string{
		"schtasks.exe /Create /TN sbc-proxy /XML " + filepath.Join(tasks.Dir, "sbc-proxy.xml") + " /F",
		"schtasks.exe /Create /TN sbc-refresh /XML " + filepath.Join(tasks.Dir, "sbc-refresh.xml") + " /F",
		"schtasks.exe /Run /TN sbc-proxy",
	}
	if strings.Join(r.calls, "\n") != strings.Join(want, "\n") {
		t.Errorf("calls:\n%s", strings.Join(r.calls, "\n"))
	}
}

func TestTasksFallBackToTheSessionWhereS4UIsRefused(t *testing.T) {
	r := &reader{state: "Ready"}
	tasks := newTasks(t, r)
	r.fail = map[string]bool{
		"schtasks.exe /Create /TN sbc-proxy /XML " + filepath.Join(tasks.Dir, "sbc-proxy.xml") + " /F": true,
	}
	// The first registration of each file fails, and the fallback's succeeds.
	r.failOnce = true

	if err := tasks.Install(); err != nil {
		t.Fatal(err)
	}

	if !tasks.Interactive {
		t.Error("the fallback was not noted")
	}
	proxy := readUTF16(t, filepath.Join(tasks.Dir, "sbc-proxy.xml"))
	if !strings.Contains(proxy, "<LogonType>InteractiveToken</LogonType>") {
		t.Errorf("sbc-proxy.xml:\n%s", proxy)
	}
}

func TestTasksAskPowerShellWhetherTheProxyRuns(t *testing.T) {
	r := &reader{state: "Running"}
	tasks := newTasks(t, r)

	if !tasks.Active() {
		t.Error("a running task counts as inactive")
	}
	r.state = "Ready"
	if tasks.Active() {
		t.Error("a ready task counts as active")
	}
	if !strings.Contains(r.calls[0], "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command (Get-ScheduledTask -TaskName 'sbc-proxy' -ErrorAction Stop).State") {
		t.Errorf("calls:\n%s", strings.Join(r.calls, "\n"))
	}
}

func TestTasksReportBothRegistrationFailures(t *testing.T) {
	tasks := newTasks(t, &reader{})
	failures := []error{errors.New("batch logon denied"), errors.New("task access denied")}
	calls := 0
	tasks.Run = func(string, ...string) error {
		err := failures[calls]
		calls++
		return err
	}
	err := tasks.Install()
	if err == nil || !strings.Contains(err.Error(), "S4U: batch logon denied") ||
		!strings.Contains(err.Error(), "InteractiveToken: task access denied") || !errors.Is(err, failures[1]) {
		t.Fatalf("lost task registration diagnostics: %v", err)
	}
}

func TestTasksRestartWaitsForTheEndedProcessAndRemoveDeletesBoth(t *testing.T) {
	r := &reader{state: "Ready"}
	tasks := newTasks(t, r)
	if err := tasks.Install(); err != nil {
		t.Fatal(err)
	}
	r.calls = nil

	if err := tasks.Restart(); err != nil {
		t.Fatal(err)
	}
	if last := r.calls[len(r.calls)-1]; last != "schtasks.exe /Run /TN sbc-proxy" || r.calls[0] != "schtasks.exe /End /TN sbc-proxy" {
		t.Errorf("calls:\n%s", strings.Join(r.calls, "\n"))
	}

	r.calls = nil
	r.fail = map[string]bool{"schtasks.exe /Delete /TN sbc-refresh /F": true, "schtasks.exe /Query /TN sbc-refresh": true}
	if err := tasks.Remove(); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"schtasks.exe /Delete /TN sbc-proxy /F", "schtasks.exe /Delete /TN sbc-refresh /F"} {
		if !strings.Contains(strings.Join(r.calls, "\n"), want) {
			t.Errorf("calls lack %s:\n%s", want, strings.Join(r.calls, "\n"))
		}
	}
	if entries, _ := os.ReadDir(tasks.Dir); len(entries) != 0 {
		t.Errorf("definitions left: %v", entries)
	}
}

func TestNewOnWindowsRunsTheInstalledSbc(t *testing.T) {
	manager, err := New("windows", filepath.Join("C:", "sbc", "cli", "sbc.exe"), Exec)
	if err != nil {
		t.Fatal(err)
	}
	tasks := manager.(*Tasks)
	if tasks.Dir != filepath.Join("C:", "sbc", "tasks") || tasks.User == "" {
		t.Errorf("tasks %+v", tasks)
	}
}

func TestTasksStopWaitsOnlyForARecordedProcessThatRuns(t *testing.T) {
	r := &reader{state: "Ready"}
	tasks := newTasks(t, r)
	tasks.PID = filepath.Join(t.TempDir(), "sing-box.pid")

	// No record, and a record of a process that is gone, both return at once.
	if err := tasks.Stop(); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(tasks.PID, []byte("2147483646\n"), 0o600)
	start := time.Now()
	if err := tasks.Stop(); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) > 5*time.Second {
		t.Error("stop waited for a process that does not exist")
	}
	if r.calls[0] != "schtasks.exe /End /TN sbc-proxy" {
		t.Errorf("calls:\n%s", strings.Join(r.calls, "\n"))
	}
}
