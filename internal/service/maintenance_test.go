package service

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
)

func TestSystemdIndependentMaintenanceSchedules(t *testing.T) {
	r := &recorder{}
	s := &Systemd{Dir: t.TempDir(), SBC: "/sbc", Run: r.run}
	p := maintenance.Defaults()
	p.Refresh.Enabled = false
	p.Upgrade.Seconds = 86400
	if err := s.Schedule(p); err != nil {
		t.Fatal(err)
	}
	timer, _ := os.ReadFile(filepath.Join(s.Dir, "sbc-upgrade.timer"))
	worker, _ := os.ReadFile(filepath.Join(s.Dir, "sbc-upgrade.service"))
	if !strings.Contains(string(timer), "OnUnitInactiveSec=86400s") || !strings.Contains(string(worker), `ExecStart="/sbc" _maintenance upgrade`) {
		t.Fatalf("timer: %s worker: %s", timer, worker)
	}
	calls := strings.Join(r.calls, "\n")
	for _, want := range []string{"disable --now sbc-config-refresh.timer", "enable sbc-upgrade.timer", "restart sbc-upgrade.timer"} {
		if !strings.Contains(calls, want) {
			t.Errorf("missing %s: %s", want, calls)
		}
	}
	if strings.Contains(calls, "restart sbc.service") {
		t.Fatal("policy change restarted the proxy")
	}
	os.WriteFile(filepath.Join(s.Dir, refreshTimer), []byte("old timer"), 0o644)
	os.WriteFile(filepath.Join(s.Dir, refreshUnit), []byte("old update task"), 0o644)
	if err := s.RetireRefresh(); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{refreshTimer, refreshUnit} {
		if _, err := os.Stat(filepath.Join(s.Dir, name)); !os.IsNotExist(err) {
			t.Fatalf("legacy file remains: %s", name)
		}
	}
	if err := s.Remove(); err != nil {
		t.Fatal(err)
	}
	if entries, _ := os.ReadDir(s.Dir); len(entries) != 0 {
		t.Fatalf("remaining definitions: %v", entries)
	}
}
func TestLaunchdIndependentMaintenanceSchedules(t *testing.T) {
	r := &recorder{}
	l := &Launchd{Dir: t.TempDir(), SBC: "/sbc", UID: 501, Run: r.run}
	p := maintenance.Defaults()
	p.Upgrade.Seconds = 1800
	if err := l.Schedule(p); err != nil {
		t.Fatal(err)
	}
	text, _ := os.ReadFile(filepath.Join(l.Dir, "io.sbc.upgrade.plist"))
	for _, want := range []string{"<integer>1800</integer>", "<string>_maintenance</string>", "<string>upgrade</string>"} {
		if !strings.Contains(string(text), want) {
			t.Errorf("missing %s: %s", want, text)
		}
	}
	p.Refresh.Enabled = false
	if err := l.Schedule(p); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(l.Dir, "io.sbc.config.refresh.plist")); !os.IsNotExist(err) {
		t.Fatal("disabled refresh plist remains")
	}
	if strings.Contains(strings.Join(r.calls, "\n"), "io.sbc.proxy") {
		t.Fatal("policy touched proxy")
	}
	os.WriteFile(filepath.Join(l.Dir, refreshLabel+".plist"), []byte("old task"), 0o644)
	if err := l.RetireRefresh(); err != nil {
		t.Fatal(err)
	}
	if err := l.Remove(); err != nil {
		t.Fatal(err)
	}
	if entries, _ := os.ReadDir(l.Dir); len(entries) != 0 {
		t.Fatalf("remaining definitions: %v", entries)
	}
}
func TestWindowsIndependentMaintenanceSchedules(t *testing.T) {
	r := &reader{}
	tasks := newTasks(t, r)
	p := maintenance.Defaults()
	p.Refresh.Enabled = false
	p.Upgrade.Seconds = 86400
	if err := tasks.Schedule(p); err != nil {
		t.Fatal(err)
	}
	refresh := readUTF16(t, filepath.Join(tasks.Dir, "sbc-config-refresh.xml"))
	upgrade := readUTF16(t, filepath.Join(tasks.Dir, "sbc-upgrade.xml"))
	for _, want := range []string{"<Enabled>false</Enabled>", "<Arguments>_maintenance refresh</Arguments>"} {
		if !strings.Contains(refresh, want) {
			t.Errorf("missing %s: %s", want, refresh)
		}
	}
	for _, want := range []string{"<Interval>PT86400S</Interval>", "<Arguments>_maintenance upgrade</Arguments>", "<RandomDelay>PT1800S</RandomDelay>", "<StartBoundary>2026-10-03T09:10:00</StartBoundary>"} {
		if !strings.Contains(upgrade, want) {
			t.Errorf("missing %s: %s", want, upgrade)
		}
	}
	if strings.Contains(strings.Join(r.calls, "\n"), "sbc-proxy") {
		t.Fatal("policy touched proxy")
	}
	if err := tasks.RetireRefresh(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(r.calls, "\n"), "/Delete /TN sbc-refresh /F") {
		t.Fatal("old task was not removed")
	}
}
func TestWindowsMaintenanceFallsBackToInteractiveLogon(t *testing.T) {
	r := &reader{}
	tasks := newTasks(t, r)
	r.fail = map[string]bool{"schtasks.exe /Create /TN sbc-upgrade /XML " + filepath.Join(tasks.Dir, "sbc-upgrade.xml") + " /F": true}
	r.failOnce = true
	if err := tasks.Schedule(maintenance.Defaults()); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"sbc-config-refresh.xml", "sbc-upgrade.xml"} {
		if text := readUTF16(t, filepath.Join(tasks.Dir, name)); !strings.Contains(text, "<LogonType>InteractiveToken</LogonType>") {
			t.Errorf("%s: %s", name, text)
		}
	}
}
