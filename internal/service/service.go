// Package service runs sing-box under the user's service manager: a systemd
// user unit on Linux, a LaunchAgent on macOS and a scheduled task on Windows.
// A timer beside it refreshes the config from the subscription.
package service

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/powershell"
)

// RefreshSeconds is how often the timer refreshes the config.
const RefreshSeconds = 6 * 60 * 60

// Runner runs a command and reports whether it succeeded.
type Runner func(name string, args ...string) error

// Exec runs commands for real, keeping their output out of sbc's own.
func Exec(name string, args ...string) error {
	_, err := Output(name, args...)
	return err
}

// Reader runs a command and returns what it printed.
type Reader func(name string, args ...string) (string, error)

// Output runs commands for real and returns their output.
func Output(name string, args ...string) (string, error) {
	command := exec.Command(name, args...)
	if name == "powershell.exe" {
		command = powershell.Command(context.Background(), args...)
	}
	output, err := command.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("%s %s: %s", name, strings.Join(args, " "), strings.TrimSpace(string(output)))
	}
	return string(output), nil
}

// Manager installs and controls the proxy service and its refresh timer.
type Manager interface {
	// Install writes the service files and enables them, without starting.
	Install() error
	Start() error
	Stop() error
	Restart() error
	Active() bool
	// Remove stops and deletes everything Install wrote.
	Remove() error
}

// New returns the manager for goos, whose services run the sbc at sbc.
func New(goos, sbc string, run Runner) (Manager, error) {
	if goos == "windows" {
		// user.Current works without the profile variables, which a
		// scheduled task may lack.
		account, err := user.Current()
		if err != nil {
			return nil, i18n.New("cannot find the current user")
		}
		data := filepath.Dir(filepath.Dir(sbc))
		return &Tasks{
			Dir:  filepath.Join(data, "tasks"),
			SBC:  sbc,
			PID:  filepath.Join(data, "sing-box.pid"),
			User: account.Uid,
			Run:  run,
			Read: Output,
		}, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, i18n.New("cannot find the home directory")
	}
	switch goos {
	case "linux":
		dir := os.Getenv("XDG_CONFIG_HOME")
		if dir == "" {
			dir = filepath.Join(home, ".config")
		}
		return &Systemd{Dir: filepath.Join(dir, "systemd", "user"), SBC: sbc, Run: run}, nil
	case "darwin":
		return &Launchd{
			Dir: filepath.Join(home, "Library", "LaunchAgents"),
			SBC: sbc,
			Log: filepath.Join(filepath.Dir(filepath.Dir(sbc)), "sing-box.log"),
			UID: os.Getuid(),
			Run: run,
		}, nil
	default:
		return nil, i18n.Errorf("sbc cannot run a service on %s yet", goos)
	}
}

func writeFiles(dir string, files map[string]string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			return err
		}
	}
	return nil
}

// Systemd manages systemd user units. They run while the user is logged in,
// or always once `loginctl enable-linger` is on.
type Systemd struct {
	Dir string
	SBC string
	Run Runner
}

const (
	proxyUnit    = "sbc.service"
	refreshUnit  = "sbc-refresh.service"
	refreshTimer = "sbc-refresh.timer"
)

// Units returns the unit files by name.
func (s *Systemd) Units() map[string]string {
	sbc := strconv.Quote(s.SBC)
	return map[string]string{
		proxyUnit: "[Unit]\nDescription=sing-box proxy (sbc)\n" +
			"After=network-online.target\n\n" +
			"[Service]\nExecStart=" + sbc + " run\nRestart=on-failure\nRestartSec=5s\n\n" +
			"[Install]\nWantedBy=default.target\n",
		refreshUnit: "[Unit]\nDescription=Refresh the sing-box config (sbc)\n\n" +
			"[Service]\nType=oneshot\nExecStart=" + sbc + " update\n",
		refreshTimer: "[Unit]\nDescription=Refresh the sing-box config every 6 hours (sbc)\n\n" +
			"[Timer]\nOnBootSec=10min\nOnUnitActiveSec=" + strconv.Itoa(RefreshSeconds) + "s\n" +
			// Spreads every client's refresh across half an hour.
			"RandomizedDelaySec=30min\n\n" +
			"[Install]\nWantedBy=timers.target\n",
	}
}

func (s *Systemd) systemctl(args ...string) error {
	return s.Run("systemctl", append([]string{"--user"}, args...)...)
}

func (s *Systemd) Install() error {
	if err := writeFiles(s.Dir, s.Units()); err != nil {
		return err
	}
	if err := s.systemctl("daemon-reload"); err != nil {
		return err
	}
	return s.systemctl("enable", proxyUnit, refreshTimer)
}

func (s *Systemd) Start() error {
	return s.systemctl("start", proxyUnit, refreshTimer)
}

func (s *Systemd) Stop() error { return s.systemctl("stop", proxyUnit) }

func (s *Systemd) Restart() error { return s.systemctl("restart", proxyUnit) }

func (s *Systemd) Active() bool { return s.systemctl("is-active", "--quiet", proxyUnit) == nil }

func (s *Systemd) Remove() error {
	// Removal goes on when a unit was already stopped or never enabled.
	_ = s.systemctl("disable", "--now", proxyUnit, refreshTimer)
	for name := range s.Units() {
		if err := os.Remove(filepath.Join(s.Dir, name)); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return s.systemctl("daemon-reload")
}

// Launchd manages LaunchAgents. They run while the user is logged in.
type Launchd struct {
	Dir string
	SBC string
	Log string
	UID int
	Run Runner
}

const (
	proxyLabel   = "io.sbc.proxy"
	refreshLabel = "io.sbc.refresh"
)

func plist(label string, arguments []string, extra string) string {
	var args strings.Builder
	for _, argument := range arguments {
		args.WriteString("    <string>" + escapeXML(argument) + "</string>\n")
	}
	return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>` + label + `</string>
  <key>ProgramArguments</key>
  <array>
` + args.String() + `  </array>
` + extra + `</dict>
</plist>
`
}

func escapeXML(text string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;").Replace(text)
}

// Plists returns the LaunchAgent files by name.
func (l *Launchd) Plists() map[string]string {
	log := escapeXML(l.Log)
	return map[string]string{
		proxyLabel + ".plist": plist(proxyLabel, []string{l.SBC, "run"},
			"  <key>RunAtLoad</key>\n  <true/>\n  <key>KeepAlive</key>\n  <true/>\n"+
				"  <key>StandardOutPath</key>\n  <string>"+log+"</string>\n"+
				"  <key>StandardErrorPath</key>\n  <string>"+log+"</string>\n"),
		refreshLabel + ".plist": plist(refreshLabel, []string{l.SBC, "update"},
			"  <key>StartInterval</key>\n  <integer>"+strconv.Itoa(RefreshSeconds)+"</integer>\n"),
	}
}

func (l *Launchd) domain() string { return "gui/" + strconv.Itoa(l.UID) }

func (l *Launchd) Install() error { return writeFiles(l.Dir, l.Plists()) }

func (l *Launchd) Start() error {
	for _, label := range []string{proxyLabel, refreshLabel} {
		path := filepath.Join(l.Dir, label+".plist")
		// bootstrap fails for a label that is already loaded, so unload first.
		_ = l.Run("launchctl", "bootout", l.domain(), path)
		if err := l.Run("launchctl", "bootstrap", l.domain(), path); err != nil {
			return err
		}
	}
	return nil
}

func (l *Launchd) Stop() error {
	return l.Run("launchctl", "bootout", l.domain()+"/"+proxyLabel)
}

func (l *Launchd) Restart() error {
	return l.Run("launchctl", "kickstart", "-k", l.domain()+"/"+proxyLabel)
}

func (l *Launchd) Active() bool {
	return l.Run("launchctl", "print", l.domain()+"/"+proxyLabel) == nil
}

func (l *Launchd) Remove() error {
	for name := range l.Plists() {
		path := filepath.Join(l.Dir, name)
		_ = l.Run("launchctl", "bootout", l.domain(), path)
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}
