//go:build e2e

package e2e

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

// Called only by the explicitly enabled, disposable-host lifecycle.
func checkMaintenance(t *testing.T, l paths.Layout) {
	t.Helper()
	sbc := l.SBC()
	pid := singBoxPID(t, l.SingBox())
	expect(t, run(t, nil, sbc, "status"), "refresh: auto on, interval 6h", "upgrade: auto on, interval 6h")
	seedLegacyRefresh(t, l)
	if err := os.Remove(maintenance.File(l)); err != nil {
		t.Fatal(err)
	}
	expect(t, run(t, nil, sbc, "update"), "Background tasks migrated")
	if output, code := runCode(t, nil, sbc, "update"); code == 0 || !strings.Contains(output, "is retired") {
		t.Fatalf("legacy action survived migration: %d %s", code, output)
	}
	if runtime.GOOS == "windows" {
		if _, code := runCode(t, nil, "schtasks.exe", "/Query", "/TN", "sbc-refresh"); code == 0 {
			t.Fatal("legacy task remains")
		}
	} else {
		for _, path := range serviceFiles(t) {
			if strings.Contains(path, "sbc-refresh") || strings.Contains(path, "io.sbc.refresh") {
				if _, err := os.Stat(path); !os.IsNotExist(err) {
					t.Fatalf("legacy definition remains: %s", path)
				}
			}
		}
	}
	run(t, nil, sbc, "refresh", "interval", "30m")
	run(t, nil, sbc, "upgrade", "interval", "24h")
	for _, action := range []string{"refresh", "upgrade"} {
		name := "sbc-upgrade"
		seconds := "86400"
		label := "io.sbc.upgrade"
		if action == "refresh" {
			name = "sbc-config-refresh"
			seconds = "1800"
			label = "io.sbc.config.refresh"
		}
		switch runtime.GOOS {
		case "linux":
			expect(t, run(t, nil, "systemctl", "--user", "is-active", name+".timer"), "active")
			for _, file := range serviceFiles(t) {
				if filepath.Base(file) == name+".timer" {
					data, err := os.ReadFile(file)
					if err != nil || !strings.Contains(string(data), "OnUnitInactiveSec="+seconds+"s") {
						t.Fatalf("timer %s: %v %s", file, err, data)
					}
				}
			}
		case "darwin":
			run(t, nil, "launchctl", "print", fmt.Sprintf("gui/%d/%s", os.Getuid(), label))
			for _, file := range serviceFiles(t) {
				if filepath.Base(file) == label+".plist" {
					data, err := os.ReadFile(file)
					if err != nil || !strings.Contains(string(data), "<integer>"+seconds+"</integer>") {
						t.Fatalf("agent %s: %v %s", file, err, data)
					}
				}
			}
		case "windows":
			command := fmt.Sprintf("[System.Xml.XmlConvert]::ToTimeSpan((Get-ScheduledTask -TaskName '%s').Triggers.Repetition.Interval).TotalSeconds", name)
			expect(t, run(t, nil, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command), seconds)
		}
	}
	// Keep later lifecycle checks deterministic. Manual actions still work.
	for _, action := range []string{"refresh", "upgrade"} {
		run(t, nil, sbc, action, "auto", "off")
		if output := run(t, nil, sbc, "_maintenance", action); strings.TrimSpace(output) != "" {
			t.Fatalf("disabled worker ran: %s", output)
		}
	}
	if now := singBoxPID(t, l.SingBox()); now != pid {
		t.Fatalf("policy change restarted the proxy: %s -> %s", pid, now)
	}
	expect(t, run(t, nil, sbc, "status"), "refresh: auto off, interval 30m", "upgrade: auto off, interval 24h")
}

func seedLegacyRefresh(t *testing.T, l paths.Layout) {
	t.Helper()
	if runtime.GOOS == "windows" {
		run(t, nil, "schtasks.exe", "/Create", "/TN", "sbc-refresh", "/SC", "HOURLY", "/MO", "6", "/TR", `"`+l.SBC()+`" update`, "/F")
		return
	}
	for _, file := range serviceFiles(t) {
		name := filepath.Base(file)
		if name != "sbc-config-refresh.timer" && name != "sbc-config-refresh.service" && name != "io.sbc.config.refresh.plist" {
			continue
		}
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		text := strings.ReplaceAll(string(data), "sbc-config-refresh", "sbc-refresh")
		text = strings.ReplaceAll(text, "io.sbc.config.refresh", "io.sbc.refresh")
		text = strings.ReplaceAll(text, "_maintenance refresh", "update")
		text = strings.ReplaceAll(text, "<string>_maintenance</string>", "")
		text = strings.ReplaceAll(text, "<string>refresh</string>", "<string>update</string>")
		oldName := strings.ReplaceAll(strings.ReplaceAll(name, "sbc-config-refresh", "sbc-refresh"), "io.sbc.config.refresh", "io.sbc.refresh")
		oldPath := filepath.Join(filepath.Dir(file), oldName)
		if err := os.WriteFile(oldPath, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
		if runtime.GOOS == "darwin" {
			run(t, nil, "launchctl", "bootstrap", fmt.Sprintf("gui/%d", os.Getuid()), oldPath)
		}
	}
	if runtime.GOOS == "linux" {
		run(t, nil, "systemctl", "--user", "daemon-reload")
		run(t, nil, "systemctl", "--user", "enable", "--now", "sbc-refresh.timer")
	}
}
