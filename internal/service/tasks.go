package service

import (
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Tasks manages scheduled tasks on Windows. They run whether the user is
// logged on or not, so sing-box has no console window and survives sign-out.
// Where Task Scheduler refuses that logon type, the tasks run in the user's
// session instead, with a console window, and Interactive says so.
type Tasks struct {
	// Dir keeps a copy of each task definition. Task Scheduler holds its own.
	Dir string
	SBC string
	// PID is where 'sbc run' records the sing-box process it started.
	PID string
	// User is the SID of the account the tasks run as.
	User string
	Run  Runner
	Read Reader
	// Now is the clock the refresh schedule starts from. Nil means time.Now.
	Now func() time.Time
	// Interactive is set once Install fell back to tasks in the user's
	// session.
	Interactive bool
}

const (
	proxyTask   = "sbc-proxy"
	refreshTask = "sbc-refresh"
	// s4u is Task Scheduler's "run whether user is logged on or not" without
	// a stored password. interactive is its fallback, "run only when user is
	// logged on".
	s4u         = "S4U"
	interactive = "InteractiveToken"
)

// Definitions returns the task definitions by file name, for the logon type.
func (t *Tasks) Definitions(logon string) map[string]string {
	now := time.Now
	if t.Now != nil {
		now = t.Now
	}
	return map[string]string{
		proxyTask + ".xml": t.definition(logon, "sing-box proxy (sbc)",
			"<LogonTrigger>\n      <Enabled>true</Enabled>\n      <UserId>"+escapeXML(t.User)+"</UserId>\n    </LogonTrigger>",
			// PT0S lifts the default limit of three days.
			"<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n    <Priority>4</Priority>\n"+
				"    <RestartOnFailure>\n      <Interval>PT1M</Interval>\n      <Count>3</Count>\n    </RestartOnFailure>",
			"run"),
		refreshTask + ".xml": t.definition(logon, "Refresh the sing-box config every 6 hours (sbc)",
			// Starts soon after the install, whether or not anyone logs on
			// again, and spreads every client's refresh across half an hour.
			// The elements keep the order Task Scheduler's own exports use.
			"<TimeTrigger>\n      <Repetition>\n        <Interval>PT6H</Interval>\n        <StopAtDurationEnd>false</StopAtDurationEnd>\n      </Repetition>\n"+
				"      <StartBoundary>"+now().Add(10*time.Minute).Format("2006-01-02T15:04:05")+"</StartBoundary>\n"+
				"      <Enabled>true</Enabled>\n      <RandomDelay>PT30M</RandomDelay>\n    </TimeTrigger>",
			"<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>\n    <Priority>7</Priority>",
			"update"),
	}
}

func (t *Tasks) definition(logon, description, trigger, settings, argument string) string {
	return `<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>` + description + `</Description>
  </RegistrationInfo>
  <Triggers>
    ` + trigger + `
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>` + escapeXML(t.User) + `</UserId>
      <LogonType>` + logon + `</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    ` + settings + `
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>` + escapeXML(t.SBC) + `</Command>
      <Arguments>` + argument + `</Arguments>
      <WorkingDirectory>` + escapeXML(filepath.Dir(filepath.Dir(t.SBC))) + `</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
`
}

func (t *Tasks) schtasks(args ...string) error {
	return t.Run("schtasks.exe", args...)
}

// encodeUTF16 returns text as schtasks reads a definition: UTF-16 with a
// byte order mark, the encoding Task Scheduler itself exports.
func encodeUTF16(text string) []byte {
	units := utf16.Encode([]rune(text))
	data := make([]byte, 2, 2+2*len(units))
	data[0], data[1] = 0xFF, 0xFE
	for _, unit := range units {
		data = binary.LittleEndian.AppendUint16(data, unit)
	}
	return data
}

// register writes the definitions for a logon type and registers them.
func (t *Tasks) register(logon string) error {
	definitions := t.Definitions(logon)
	if err := os.MkdirAll(t.Dir, 0o755); err != nil {
		return err
	}
	for name, content := range definitions {
		if err := os.WriteFile(filepath.Join(t.Dir, name), encodeUTF16(content), 0o644); err != nil {
			return err
		}
	}
	for name := range definitions {
		task := strings.TrimSuffix(name, ".xml")
		if err := t.schtasks("/Create", "/TN", task, "/XML", filepath.Join(t.Dir, name), "/F"); err != nil {
			return err
		}
	}
	return nil
}

// Install registers the tasks without starting them. A machine whose policy
// refuses the S4U logon gets tasks in the user's session instead.
func (t *Tasks) Install() error {
	s4uErr := t.register(s4u)
	if s4uErr == nil {
		t.Interactive = false
		return nil
	}
	if err := t.register(interactive); err != nil {
		return fmt.Errorf("S4U: %v; InteractiveToken: %w", s4uErr, err)
	}
	t.Interactive = true
	return nil
}

func (t *Tasks) Start() error {
	return t.schtasks("/Run", "/TN", proxyTask)
}

func (t *Tasks) Stop() error {
	// Ending a task that does not run is an error only in name.
	if err := t.schtasks("/End", "/TN", proxyTask); err != nil && t.Active() {
		return err
	}
	return t.waitStopped()
}

// waitStopped waits for the sing-box the task ran to exit. Task Scheduler
// gives an ended task's process a few seconds before it terminates it, and
// sing-box, which goes with it, holds the ports until then.
func (t *Tasks) waitStopped() error {
	data, err := os.ReadFile(t.PID)
	if err != nil {
		return nil
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		return nil
	}
	process, err := os.FindProcess(pid)
	if err != nil {
		return nil
	}
	exited := make(chan struct{})
	go func() {
		process.Wait()
		close(exited)
	}()
	select {
	case <-exited:
		return nil
	case <-time.After(15 * time.Second):
		return i18n.New("sing-box did not stop")
	}
}

func (t *Tasks) Restart() error {
	if err := t.Stop(); err != nil {
		return err
	}
	// A task whose process is gone is about to be ready for the next run.
	for range 10 {
		if !t.Active() {
			break
		}
		time.Sleep(500 * time.Millisecond)
	}
	return t.Start()
}

// Active asks PowerShell, whose task states are the same in every language,
// where schtasks prints them in the display language.
func (t *Tasks) Active() bool {
	state, err := t.Read("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
		"-Command", "(Get-ScheduledTask -TaskName '"+proxyTask+"' -ErrorAction Stop).State")
	return err == nil && strings.TrimSpace(state) == "Running"
}

func (t *Tasks) Remove() error {
	for _, task := range []string{proxyTask, refreshTask} {
		_ = t.schtasks("/End", "/TN", task)
		if task == proxyTask {
			// sing-box must be gone before uninstall deletes its program.
			_ = t.waitStopped()
		}
		if err := t.schtasks("/Delete", "/TN", task, "/F"); err != nil && t.registered(task) {
			return err
		}
		if err := os.Remove(filepath.Join(t.Dir, task+".xml")); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}

func (t *Tasks) registered(task string) bool {
	return t.schtasks("/Query", "/TN", task) == nil
}
