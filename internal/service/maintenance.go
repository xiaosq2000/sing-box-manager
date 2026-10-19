package service

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
)

func taskName(action string) string {
	if action == "refresh" {
		return "sbc-config-refresh"
	}
	return "sbc-upgrade"
}
func taskLabel(action string) string { return "io." + strings.ReplaceAll(taskName(action), "-", ".") }

var actions = []string{"refresh", "upgrade"}

func (s *Systemd) Schedule(plan maintenance.Settings) error {
	if err := plan.Validate(); err != nil {
		return err
	}
	files := map[string]string{}
	for _, action := range actions {
		p := plan.Policy(action)
		name := taskName(action)
		files[name+".service"] = fmt.Sprintf("[Unit]\nDescription=sbc background %s\n\n[Service]\nType=oneshot\nExecStart=%s _maintenance %s\n", action, strconv.Quote(s.SBC), action)
		files[name+".timer"] = fmt.Sprintf("[Unit]\nDescription=sbc background %s\n\n[Timer]\nOnActiveSec=%ds\nOnUnitInactiveSec=%ds\nRandomizedDelaySec=%ds\n\n[Install]\nWantedBy=timers.target\n", action, min(p.Seconds, 600), p.Seconds, p.Delay())
	}
	if err := writeFiles(s.Dir, files); err != nil {
		return err
	}
	if err := s.systemctl("daemon-reload"); err != nil {
		return err
	}
	for _, action := range actions {
		name := taskName(action) + ".timer"
		if plan.Policy(action).Enabled {
			if err := s.systemctl("enable", name); err != nil {
				return err
			}
			if err := s.systemctl("restart", name); err != nil {
				return err
			}
		} else if err := s.systemctl("disable", "--now", name); err != nil {
			return err
		}
	}
	return nil
}
func (s *Systemd) RetireRefresh() error {
	if _, err := os.Stat(filepath.Join(s.Dir, refreshTimer)); os.IsNotExist(err) {
		return nil
	}
	if err := s.systemctl("disable", "--now", refreshTimer); err != nil {
		return err
	}
	for _, name := range []string{refreshTimer, refreshUnit} {
		if err := removeFile(filepath.Join(s.Dir, name)); err != nil {
			return err
		}
	}
	return s.systemctl("daemon-reload")
}
func (s *Systemd) removeSchedules() error {
	for _, action := range actions {
		name := taskName(action)
		_ = s.systemctl("disable", "--now", name+".timer", name+".service")
		for _, suffix := range []string{".timer", ".service"} {
			if err := removeFile(filepath.Join(s.Dir, name+suffix)); err != nil {
				return err
			}
		}
	}
	return nil
}
func (l *Launchd) Schedule(plan maintenance.Settings) error {
	if err := plan.Validate(); err != nil {
		return err
	}
	for _, action := range actions {
		label := taskLabel(action)
		path := filepath.Join(l.Dir, label+".plist")
		if err := l.unload(label); err != nil {
			return err
		}
		if !plan.Policy(action).Enabled {
			if err := removeFile(path); err != nil {
				return err
			}
			continue
		}
		body := plist(label, []string{l.SBC, "_maintenance", action}, fmt.Sprintf("  <key>StartInterval</key>\n  <integer>%d</integer>\n", plan.Policy(action).Seconds))
		if err := writeFiles(l.Dir, map[string]string{label + ".plist": body}); err != nil {
			return err
		}
		if err := l.Run("launchctl", "bootstrap", l.domain(), path); err != nil {
			return err
		}
	}
	return nil
}
func (l *Launchd) unload(label string) error {
	if err := l.Run("launchctl", "bootout", l.domain()+"/"+label); err != nil {
		if l.Run("launchctl", "print", l.domain()+"/"+label) == nil {
			return err
		}
	}
	return nil
}
func (l *Launchd) RetireRefresh() error { return l.retireRefresh(false) }
func (l *Launchd) retireRefresh(force bool) error {
	// bootout can terminate the old job that called this migration. Remove its
	// definition first so the next login cannot load it again.
	if err := removeFile(filepath.Join(l.Dir, refreshLabel+".plist")); err != nil {
		return err
	}
	if !force && l.Read != nil {
		output, err := l.Read("launchctl", "print", l.domain()+"/"+refreshLabel)
		if err != nil {
			return nil
		}
		for _, line := range strings.Split(output, "\n") {
			fields := strings.Fields(line)
			if len(fields) == 3 && fields[0] == "pid" && fields[1] == "=" {
				pid, err := strconv.Atoi(fields[2])
				// An old refresh may be waiting for this new proxy wrapper to
				// start. Let it finish. Its next invocation retires its own job.
				if err != nil || (pid > 0 && pid != os.Getpid()) {
					return nil
				}
			}
		}
	}
	return l.unload(refreshLabel)
}
func (l *Launchd) removeSchedules() error {
	for _, action := range actions {
		label := taskLabel(action)
		if err := l.unload(label); err != nil {
			return err
		}
		if err := removeFile(filepath.Join(l.Dir, label+".plist")); err != nil {
			return err
		}
	}
	return nil
}
func (t *Tasks) scheduleDefinitions(plan maintenance.Settings, logon string) map[string]string {
	now := time.Now
	if t.Now != nil {
		now = t.Now
	}
	definitions := map[string]string{}
	for _, action := range actions {
		p := plan.Policy(action)
		trigger := fmt.Sprintf("<TimeTrigger>\n<Repetition><Interval>PT%dS</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>\n<StartBoundary>%s</StartBoundary><Enabled>%t</Enabled><RandomDelay>PT%dS</RandomDelay>\n</TimeTrigger>", p.Seconds, now().Add(time.Duration(min(p.Seconds, 600))*time.Second).Format("2006-01-02T15:04:05"), p.Enabled, p.Delay())
		definitions[taskName(action)+".xml"] = t.definition(logon, "sbc background "+action, trigger, "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit><Priority>7</Priority>", "_maintenance "+action, p.Enabled)
	}
	return definitions
}
func (t *Tasks) Schedule(plan maintenance.Settings) error {
	if err := plan.Validate(); err != nil {
		return err
	}
	s4uErr := t.registerDefinitions(t.scheduleDefinitions(plan, s4u))
	if s4uErr == nil {
		return nil
	}
	if err := t.registerDefinitions(t.scheduleDefinitions(plan, interactive)); err != nil {
		return fmt.Errorf("S4U: %v; InteractiveToken: %w", s4uErr, err)
	}
	return nil
}
func (t *Tasks) RetireRefresh() error {
	if t.registered(refreshTask) {
		if err := t.schtasks("/Delete", "/TN", refreshTask, "/F"); err != nil {
			return err
		}
	}
	return removeFile(filepath.Join(t.Dir, refreshTask+".xml"))
}
func removeFile(path string) error {
	err := os.Remove(path)
	if os.IsNotExist(err) {
		return nil
	}
	return err
}
