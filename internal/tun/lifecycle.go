package tun

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

const recoveryLimit = 3 * time.Minute

// ControlDir contains root-owned transaction metadata, never API credentials.
// It is separate from the state directory that the engine can write.
func (p Paths) ControlDir() string { return filepath.Join(filepath.Dir(p.ConfigDir), "tun-control") }
func (p Paths) blocked() string    { return filepath.Join(p.ControlDir(), "blocked") }
func (p Paths) journal() string    { return filepath.Join(p.ControlDir(), "transaction.json") }

type transaction struct {
	ID        string    `json:"id"`
	Phase     string    `json:"phase"`
	Deadline  time.Time `json:"deadline,omitempty"`
	Committed bool      `json:"committed,omitempty"`
}

func loadTransaction(p Paths) (transaction, error) {
	var record transaction
	data, err := os.ReadFile(p.journal())
	if err != nil {
		return record, err
	}
	err = json.Unmarshal(data, &record)
	if err == nil && record.Phase != "pending" && record.Phase != "active" && record.Phase != "off" && record.Phase != "dirty" {
		err = fmt.Errorf("invalid TUN transaction phase")
	}
	return record, err
}

func saveTransaction(p Paths, record transaction) error {
	data, err := json.Marshal(record)
	if err != nil {
		return err
	}
	return writeDurable(p.journal(), append(data, '\n'), 0o644)
}

func controlLock(p Paths) (func(), error) {
	if err := os.MkdirAll(p.ControlDir(), 0o755); err != nil {
		return nil, err
	}
	return maintenance.Lock(paths.Layout{Config: filepath.Join(p.ControlDir(), "control")})
}

func block(p Paths) error {
	return writeDurable(p.blocked(), []byte("TUN state requires verification\n"), 0o644)
}

func writeDurable(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".tun-state-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	if err := file.Chmod(mode); err != nil {
		return err
	}
	if _, err := file.Write(data); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(file.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

// AllowStart refuses an uncommitted generation after reboot: transient
// recovery timers do not survive reboot, so pending state alone is not enough.
func AllowStart(opts StopOptions) error {
	opts = stopDefaults(opts)
	if opts.Euid() != 0 {
		return i18n.New("invalid root TUN transaction")
	}
	record, err := loadTransaction(opts.Paths)
	if err != nil {
		return err
	}
	if record.Phase == "active" {
		return nil
	}
	if record.Phase == "pending" && time.Now().Before(record.Deadline) {
		tools, err := resolveTools(opts.LookPath)
		if err != nil {
			return err
		}
		if err := opts.Runner.Run(tools.Systemctl, "is-active", "--quiet", recoveryUnit(record.ID)+".timer"); err == nil {
			return nil
		}
	}
	return i18n.New("TUN startup refused: the generation is not committed and recovery is not armed")
}

// CaptureState contains public root metadata, never an API credential.
type CaptureState struct {
	Status      string
	Transaction string
	Phase       string
	Committed   bool
}

// Current pairs conservative status with the root transaction identity. A
// caller must recheck identity after a privileged recovery operation.
func Current() CaptureState {
	state := CaptureState{Status: Status()}
	if runtime.GOOS == "linux" {
		if record, err := loadTransaction(DefaultPaths()); err == nil {
			state.Transaction, state.Phase = record.ID, record.Phase
			state.Committed = record.Committed || record.Phase == "active"
		}
	}
	return state
}

// Status reports conservative capture state. A pending or unreadable journal
// blocks mixed mode, including after a reboot or a failed service stop.
func Status() string {
	if runtime.GOOS != "linux" {
		return "off"
	}
	p := DefaultPaths()
	record, err := loadTransaction(p)
	if err == nil && record.Phase != "off" {
		if record.Phase != "active" {
			return "dirty"
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if exec.CommandContext(ctx, "systemctl", "is-active", "--quiet", ServiceName).Run() != nil {
			return "dirty"
		}
		return "active"
	}
	if err != nil && !os.IsNotExist(err) {
		return "dirty"
	}
	if ActiveAt(p.blocked()) || ActiveAt(p.Active) {
		return "dirty"
	}
	// Check the service as well as markers for installations without a journal.
	if _, err := os.Stat(p.ConfigDir); !os.IsNotExist(err) {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		out, err := exec.CommandContext(ctx, "systemctl", "show", "-p", "ActiveState", "--value", ServiceName).Output()
		if err != nil || strings.TrimSpace(string(out)) != "inactive" && strings.TrimSpace(string(out)) != "failed" {
			return "dirty"
		}
	}
	return "off"
}

func validTransactionID(id string) bool {
	if len(id) != 32 {
		return false
	}
	for _, c := range id {
		if !strings.ContainsRune("0123456789abcdef", c) {
			return false
		}
	}
	return true
}

func recoveryUnit(id string) string { return "sbc-tun-recover-" + id }

func armRecovery(opts Options, tools Tools, record transaction) error {
	self := opts.Self
	if self == "" {
		var err error
		self, err = os.Executable()
		if err != nil {
			return err
		}
	}
	helper := filepath.Join(opts.Paths.ControlDir(), "recover-sbc")
	if err := copyFileAtomic(self, helper, 0o755, 0, 0, opts.Chown); err != nil {
		return err
	}
	return opts.Runner.Run(tools.SystemdRun,
		"--quiet", "--collect", "--unit="+recoveryUnit(record.ID),
		"--on-active="+strconv.Itoa(int(recoveryLimit.Seconds()))+"s",
		"--timer-property=AccuracySec=1s", "-p", "Restart=on-failure", "-p", "RestartSec=1s",
		"-p", "StartLimitIntervalSec=0",
		helper, "_tun-recover", "--transaction", record.ID,
	)
}

// Commit accepts a verified root generation only after the user saved its API
// state. A stale timer cannot stop a different or committed transaction.
func Commit(opts StopOptions, id string) error {
	opts = stopDefaults(opts)
	if opts.Euid() != 0 || !validTransactionID(id) {
		return i18n.New("invalid root TUN transaction")
	}
	unlock, err := controlLock(opts.Paths)
	if err != nil {
		return err
	}
	defer unlock()
	record, err := loadTransaction(opts.Paths)
	if err != nil || record.ID != id || record.Phase != "pending" || !time.Now().Before(record.Deadline) {
		return i18n.New("the TUN transaction is not pending or its recovery deadline expired")
	}
	tools, err := resolveTools(opts.LookPath)
	if err != nil {
		return err
	}
	if err := opts.Runner.Run(tools.Systemctl, "is-active", "--quiet", ServiceName); err != nil {
		return i18n.New("the TUN service stopped before commit")
	}
	if err := opts.Runner.Run(tools.Systemctl, "enable", ServiceName); err != nil {
		return i18n.New("the TUN service works but could not be enabled at boot")
	}
	record.Phase = "active"
	record.Committed = true
	if err := saveTransaction(opts.Paths, record); err != nil {
		return err
	}
	// The committed journal makes an already-started recovery service a no-op.
	_ = opts.Runner.Run(tools.Systemctl, "stop", recoveryUnit(id)+".timer")
	return nil
}

// Recover runs in a transient root service, independently of the caller.
func Recover(opts StopOptions, id string) error {
	opts = stopDefaults(opts)
	if opts.Euid() != 0 || !validTransactionID(id) {
		return i18n.New("invalid root TUN transaction")
	}
	unlock, err := controlLock(opts.Paths)
	if err != nil {
		return err // systemd retries while the apply helper owns the lock.
	}
	defer unlock()
	record, err := loadTransaction(opts.Paths)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if record.ID != id || record.Committed || record.Phase == "active" || record.Phase == "off" {
		return nil
	}
	tools, err := resolveTools(opts.LookPath)
	if err != nil {
		return err
	}
	return stopLocked(opts.Paths, opts.Runner, tools, record)
}

func stopDefaults(opts StopOptions) StopOptions {
	if opts.Paths.ConfigDir == "" {
		opts.Paths = DefaultPaths()
	}
	if opts.Runner == nil {
		opts.Runner = ExecRunner{}
	}
	if opts.Euid == nil {
		opts.Euid = os.Geteuid
	}
	if opts.LookPath == nil {
		opts.LookPath = sbinLookPath
	}
	return opts
}

func stopLocked(p Paths, runner Runner, tools Tools, record transaction) error {
	blockErr := block(p)
	// A stopped committed generation must not become eligible for user-file
	// rollback. Also retain the commit fact from journals without this field.
	record.Committed = record.Committed || record.Phase == "active"
	record.Phase = "dirty"
	stateErr := errors.Join(blockErr, saveTransaction(p, record))
	// Storage failure must not prevent an attempt to stop capture. Retain
	// existing markers and report failure if durable state cannot be saved.
	stopErr := runner.Run(tools.Systemctl, "disable", "--now", ServiceName)
	output, err := runner.Output(tools.Systemctl, "show", ServiceName,
		"--property=LoadState,ActiveState,MainPID,UnitFileState")
	state := properties(output)
	if err != nil || state["MainPID"] != "0" || state["ActiveState"] != "inactive" && state["ActiveState"] != "failed" ||
		state["LoadState"] != "loaded" && state["LoadState"] != "not-found" ||
		state["UnitFileState"] != "disabled" && state["LoadState"] != "not-found" ||
		stopErr != nil && state["LoadState"] != "not-found" {
		return errors.Join(stateErr, i18n.New("TUN stop is unconfirmed. Capture may remain active. Run 'sbc tun off' again"))
	}
	if err := noCaptureResources(p, runner, tools); err != nil {
		return errors.Join(stateErr, err)
	}
	if stateErr != nil {
		return errors.Join(stateErr, i18n.New("TUN capture stopped, but recovery state could not be saved. Run 'sbc tun off' again"))
	}
	if err := removeIfPresent(p.Active); err != nil {
		return err
	}
	record.Phase = "off"
	if err := saveTransaction(p, record); err != nil {
		return err
	}
	return removeIfPresent(p.blocked())
}

func properties(output string) map[string]string {
	result := map[string]string{}
	for _, line := range strings.Split(output, "\n") {
		key, value, ok := strings.Cut(line, "=")
		if ok {
			result[key] = value
		}
	}
	return result
}

func removeIfPresent(path string) error {
	err := os.Remove(path)
	if os.IsNotExist(err) {
		return nil
	}
	return err
}

func noCaptureResources(p Paths, runner Runner, tools Tools) error {
	for _, args := range [][]string{{"-j", "link", "show"}, {"-j", "-4", "route", "show", "table", "all"}, {"-j", "-6", "route", "show", "table", "all"}} {
		output, err := runner.Output(tools.IP, args...)
		var entries []map[string]any
		if err != nil || json.Unmarshal([]byte(output), &entries) != nil {
			return i18n.New("TUN resource cleanup could not be checked. Run 'sbc tun off' again")
		}
		for _, entry := range entries {
			if entry["ifname"] == InterfaceName || entry["dev"] == InterfaceName {
				return i18n.New("TUN capture resources remain. Run 'sbc tun off' again")
			}
		}
	}
	output, err := runner.Output(tools.Nft, "-j", "list", "ruleset")
	var rules struct {
		NFTables []struct {
			Table struct {
				Name string `json:"name"`
			} `json:"table"`
		} `json:"nftables"`
	}
	if err != nil || json.Unmarshal([]byte(output), &rules) != nil {
		return i18n.New("TUN resource cleanup could not be checked. Run 'sbc tun off' again")
	}
	for _, entry := range rules.NFTables {
		if entry.Table.Name == "sing-box" {
			return i18n.New("TUN capture resources remain. Run 'sbc tun off' again")
		}
	}
	return nil
}

func abort(opts Options, tools Tools, cause error) error {
	record, err := loadTransaction(opts.Paths)
	if err != nil {
		return errors.Join(cause, err)
	}
	return errors.Join(cause, stopLocked(opts.Paths, opts.Runner, tools, record))
}
