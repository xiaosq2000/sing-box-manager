package tun

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Runner runs the root-side commands. Run discards output, so a checker that
// prints config values cannot leak them into the terminal or the journal.
type Runner interface {
	Run(name string, args ...string) error
	Output(name string, args ...string) (string, error)
}

// ExecRunner runs commands for real.
type ExecRunner struct{}

// Run executes a command and discards both output streams.
func (ExecRunner) Run(name string, args ...string) error {
	command := exec.Command(name, args...)
	command.Stdout = io.Discard
	command.Stderr = io.Discard
	return command.Run()
}

// Output executes a command and returns its combined output.
func (ExecRunner) Output(name string, args ...string) (string, error) {
	output, err := exec.Command(name, args...).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("%s: %w", name, err)
	}
	return string(output), nil
}

// Tools are the administrative programs the root helper uses.
type Tools struct {
	IP         string
	Useradd    string
	Userdel    string
	Nft        string
	SystemdRun string
	Systemctl  string
}

func resolveTools(look func(string) (string, error)) (Tools, error) {
	if look == nil {
		look = sbinLookPath
	}
	var tools Tools
	for _, entry := range []struct {
		name   string
		target *string
	}{
		{"ip", &tools.IP},
		{"useradd", &tools.Useradd},
		{"userdel", &tools.Userdel},
		{"nft", &tools.Nft},
		{"systemd-run", &tools.SystemdRun},
		{"systemctl", &tools.Systemctl},
	} {
		path, err := look(entry.name)
		if err != nil {
			return Tools{}, i18n.Errorf("Linux TUN needs %s, which was not found", entry.name)
		}
		*entry.target = path
	}
	return tools, nil
}

// Options configures one root-side TUN application.
type Options struct {
	Paths     Paths
	Config    string
	BinDir    string
	RulesDir  string
	OwnerUID  int
	OwnerUser string

	Runner      Runner
	Euid        func() int
	LookPath    func(string) (string, error)
	LookupUser  func(string) (*user.User, error)
	LookupGroup func(string) (*user.Group, error)
	Chown       func(string, int, int) error
	Verify      func(Paths, Runner, Tools) error
}

func withDefaults(opts Options) Options {
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
	if opts.LookupUser == nil {
		opts.LookupUser = user.Lookup
	}
	if opts.LookupGroup == nil {
		opts.LookupGroup = user.LookupGroup
	}
	if opts.Chown == nil {
		opts.Chown = os.Chown
	}
	if opts.Verify == nil {
		opts.Verify = Verify
	}
	return opts
}

// Apply stages the TUN files, proves the capability, starts and verifies the
// service, and only then enables it at boot. A failed attempt stops the unit
// and removes the active marker.
func Apply(opts Options) error {
	opts = withDefaults(opts)
	if opts.Euid() != 0 {
		return i18n.New("Linux TUN setup must run as root")
	}
	if opts.OwnerUID <= 0 {
		return i18n.New("the Linux TUN owner must be a user, not root")
	}
	if opts.Config == "" || opts.BinDir == "" || opts.RulesDir == "" {
		return i18n.New("the Linux TUN staging paths are incomplete")
	}
	tools, err := resolveTools(opts.LookPath)
	if err != nil {
		return err
	}
	uid, gid, err := ensureServiceUser(opts, tools)
	if err != nil {
		return err
	}
	if err := ensureDirs(opts, uid, gid); err != nil {
		return err
	}
	if err := ensureOwner(opts, gid); err != nil {
		return err
	}

	// Stop the previous generation before replacing its files. A unit that is
	// not loaded is fine; one that is still active is not.
	if err := opts.Runner.Run(tools.Systemctl, "stop", ServiceName); err != nil {
		if opts.Runner.Run(tools.Systemctl, "is-active", "--quiet", ServiceName) == nil {
			return i18n.New("the previous TUN service did not stop")
		}
	}
	_ = os.Remove(opts.Paths.Active)
	if err := ensureActiveMarker(opts.Paths, opts.Chown); err != nil {
		abort(opts, tools)
		return err
	}

	if err := stage(opts, gid); err != nil {
		abort(opts, tools)
		return err
	}
	if err := writeUnit(opts); err != nil {
		abort(opts, tools)
		return err
	}
	if err := opts.Runner.Run(tools.Systemctl, "daemon-reload"); err != nil {
		abort(opts, tools)
		return i18n.New("systemd could not reload the TUN unit")
	}
	// The checker reads the final paths; it must never print config values.
	if err := opts.Runner.Run(
		"env", "LD_LIBRARY_PATH="+opts.Paths.BinaryDir,
		opts.Paths.Binary, "check", "-D", opts.Paths.StateDir, "-c", opts.Paths.Config,
	); err != nil {
		abort(opts, tools)
		return i18n.New("the TUN config did not pass sing-box check; no service was started")
	}
	if err := Probe(opts.Runner, tools, opts.Paths); err != nil {
		abort(opts, tools)
		return err
	}
	if err := opts.Runner.Run(tools.Systemctl, "start", ServiceName); err != nil {
		abort(opts, tools)
		return i18n.New("the TUN service did not start")
	}
	if err := opts.Verify(opts.Paths, opts.Runner, tools); err != nil {
		abort(opts, tools)
		return i18n.Errorf("the TUN service failed verification: %w", err)
	}
	if err := opts.Runner.Run(tools.Systemctl, "enable", ServiceName); err != nil {
		abort(opts, tools)
		return i18n.New("the TUN service works but could not be enabled at boot")
	}
	return nil
}

// StopOptions configures a root-side TUN stop.
type StopOptions struct {
	Paths    Paths
	Runner   Runner
	LookPath func(string) (string, error)
	Euid     func() int
}

// Stop disables and stops the TUN service and removes the active marker. It
// keeps the staged files for the next activation.
func Stop(opts StopOptions) error {
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
	if opts.Euid() != 0 {
		return i18n.New("Linux TUN removal must run as root")
	}
	tools, err := resolveTools(opts.LookPath)
	if err != nil {
		return err
	}
	// A unit that was never loaded is not an error for a stop.
	_ = opts.Runner.Run(tools.Systemctl, "disable", "--now", ServiceName)
	_ = os.Remove(opts.Paths.Active)
	return nil
}

func abort(opts Options, tools Tools) {
	_ = opts.Runner.Run(tools.Systemctl, "stop", ServiceName)
	_ = opts.Runner.Run(tools.Systemctl, "disable", ServiceName)
	_ = os.Remove(opts.Paths.Active)
}

func ensureActiveMarker(paths Paths, chown func(string, int, int) error) error {
	if err := os.MkdirAll(filepath.Dir(paths.Active), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(paths.Active, nil, 0o644); err != nil {
		return err
	}
	if err := chown(paths.Active, 0, 0); err != nil {
		return err
	}
	return os.Chmod(paths.Active, 0o644)
}

func ensureServiceUser(opts Options, tools Tools) (int, int, error) {
	account, err := opts.LookupUser(ServiceUser)
	if err != nil {
		if err := opts.Runner.Run(
			tools.Useradd, "--system", "--no-create-home",
			"--home-dir", opts.Paths.StateDir,
			"--shell", "/usr/sbin/nologin", "--user-group", ServiceUser,
		); err != nil {
			return 0, 0, i18n.New("could not create the sbc-tun service account")
		}
		account, err = opts.LookupUser(ServiceUser)
		if err != nil {
			return 0, 0, i18n.New("the sbc-tun service account is missing after creation")
		}
	}
	uid, err := ParseUID(account.Uid)
	if err != nil {
		return 0, 0, err
	}
	group, err := opts.LookupGroup(ServiceGroup)
	if err != nil {
		return 0, 0, i18n.New("the sbc-tun service group is missing")
	}
	gid, err := strconv.Atoi(group.Gid)
	if err != nil || gid <= 0 {
		return 0, 0, i18n.New("the sbc-tun service group has an invalid ID")
	}
	return uid, gid, nil
}

func ensureDirs(opts Options, uid, gid int) error {
	for _, entry := range []struct {
		path  string
		mode  os.FileMode
		owner int
		group int
	}{
		// The parent must be traversable by the service group so the
		// service user can reach the config directory.
		{filepath.Dir(opts.Paths.ConfigDir), 0o755, 0, 0},
		{opts.Paths.ConfigDir, 0o750, 0, gid},
		{opts.Paths.RulesDir, 0o750, 0, gid},
		{opts.Paths.StateDir, 0o700, uid, gid},
		{opts.Paths.BinaryDir, 0o755, 0, 0},
	} {
		if err := os.MkdirAll(entry.path, entry.mode); err != nil {
			return i18n.Errorf("create %s: %w", entry.path, err)
		}
		if err := os.Chmod(entry.path, entry.mode); err != nil {
			return err
		}
		if err := opts.Chown(entry.path, entry.owner, entry.group); err != nil {
			return i18n.Errorf("set ownership of %s: %w", entry.path, err)
		}
	}
	return nil
}

func ensureOwner(opts Options, gid int) error {
	owner, err := ReadOwner(opts.Paths.Owner)
	if err == nil {
		if owner.UID != opts.OwnerUID {
			return i18n.Errorf(
				"Linux TUN is owned by %s; run 'sbc tun off' as that user first",
				owner.User,
			)
		}
		return nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := WriteOwner(opts.Paths.Owner, Owner{UID: opts.OwnerUID, User: opts.OwnerUser}); err != nil {
		return err
	}
	if err := opts.Chown(opts.Paths.Owner, 0, gid); err != nil {
		return err
	}
	return os.Chmod(opts.Paths.Owner, 0o640)
}

func stage(opts Options, gid int) error {
	if err := os.RemoveAll(opts.Paths.BinaryDir); err != nil {
		return err
	}
	if err := os.MkdirAll(opts.Paths.BinaryDir, 0o755); err != nil {
		return err
	}
	if err := opts.Chown(opts.Paths.BinaryDir, 0, 0); err != nil {
		return err
	}
	if err := copyBinDir(opts, 0, 0); err != nil {
		return err
	}
	if err := os.RemoveAll(opts.Paths.RulesDir); err != nil {
		return err
	}
	if err := os.MkdirAll(opts.Paths.RulesDir, 0o750); err != nil {
		return err
	}
	if err := opts.Chown(opts.Paths.RulesDir, 0, gid); err != nil {
		return err
	}
	if err := copyRules(opts, gid); err != nil {
		return err
	}
	if err := copyFileAtomic(opts.Config, opts.Paths.Config, 0o640, 0, gid, opts.Chown); err != nil {
		return i18n.Errorf("stage the TUN config: %w", err)
	}
	if err := hashFile(opts.Paths.Binary, opts.Paths.BinaryHash, 0, gid, opts.Chown); err != nil {
		return err
	}
	return nil
}

func copyBinDir(opts Options, uid, gid int) error {
	entries, err := os.ReadDir(opts.BinDir)
	if err != nil {
		return i18n.Errorf("read the staged binary directory: %w", err)
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		mode := os.FileMode(0o644)
		if info.Mode()&0o111 != 0 {
			mode = 0o755
		}
		source := filepath.Join(opts.BinDir, entry.Name())
		target := filepath.Join(opts.Paths.BinaryDir, entry.Name())
		if err := copyFileAtomic(source, target, mode, uid, gid, opts.Chown); err != nil {
			return err
		}
	}
	if _, err := os.Stat(opts.Paths.Binary); err != nil {
		return i18n.New("the staged TUN binary is missing")
	}
	return nil
}

func copyRules(opts Options, gid int) error {
	entries, err := os.ReadDir(opts.RulesDir)
	if err != nil {
		return i18n.Errorf("read the staged rule snapshots: %w", err)
	}
	for _, entry := range entries {
		if entry.IsDir() {
			return i18n.New("a TUN rule snapshot must be a file")
		}
		source := filepath.Join(opts.RulesDir, entry.Name())
		target := filepath.Join(opts.Paths.RulesDir, entry.Name())
		if err := copyFileAtomic(source, target, 0o640, 0, gid, opts.Chown); err != nil {
			return err
		}
	}
	return nil
}

func writeUnit(opts Options) error {
	if err := os.MkdirAll(filepath.Dir(opts.Paths.Unit), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(opts.Paths.Unit, []byte(Unit(opts.Paths)), 0o644); err != nil {
		return err
	}
	if err := opts.Chown(opts.Paths.Unit, 0, 0); err != nil {
		return err
	}
	return os.Chmod(opts.Paths.Unit, 0o644)
}

func copyFileAtomic(source, target string, mode os.FileMode, uid, gid int, chown func(string, int, int) error) error {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	staged, err := os.CreateTemp(filepath.Dir(target), "."+filepath.Base(target)+".tmp-")
	if err != nil {
		return err
	}
	name := staged.Name()
	defer os.Remove(name)
	if _, err := io.Copy(staged, input); err != nil {
		staged.Close()
		return err
	}
	if err := staged.Chmod(mode); err != nil {
		staged.Close()
		return err
	}
	if err := chown(name, uid, gid); err != nil {
		staged.Close()
		return err
	}
	if err := staged.Sync(); err != nil {
		staged.Close()
		return err
	}
	if err := staged.Close(); err != nil {
		return err
	}
	return os.Rename(name, target)
}

func hashFile(path, target string, uid, gid int, chown func(string, int, int) error) error {
	input, err := os.Open(path)
	if err != nil {
		return err
	}
	defer input.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, input); err != nil {
		return err
	}
	data := []byte(hex.EncodeToString(hash.Sum(nil)) + "\n")
	staged, err := os.CreateTemp(filepath.Dir(target), ".sing-box.sha256.tmp-")
	if err != nil {
		return err
	}
	name := staged.Name()
	defer os.Remove(name)
	if _, err := staged.Write(data); err != nil {
		staged.Close()
		return err
	}
	if err := staged.Chmod(0o640); err != nil {
		staged.Close()
		return err
	}
	if err := chown(name, uid, gid); err != nil {
		staged.Close()
		return err
	}
	if err := staged.Close(); err != nil {
		return err
	}
	return os.Rename(name, target)
}
