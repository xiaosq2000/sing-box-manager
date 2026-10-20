// Package tun manages the root-owned Linux TUN service and the files it
// needs. The user command checks the host, stages a config and asks root to
// apply it; this package does the root-side work and the local state.
package tun

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

const (
	// InterfaceName is the TUN interface sing-box creates.
	InterfaceName = "sbc-tun0"
	// ServiceName is the root systemd unit.
	ServiceName = "sbc-tun.service"
	// ServiceUser is the unprivileged account that runs sing-box.
	ServiceUser = "sbc-tun"
	// ServiceGroup is the group that reads the root config and rule files.
	ServiceGroup = "sbc-tun"
	// ActivePath exists while the TUN service owns packet capture.
	ActivePath = "/run/sbc-tun/active"
	// ProbePrefix names the disposable systemd-run units the capability
	// probe creates.
	ProbePrefix = "sbc-tun-probe"
)

// Paths names the root-owned files the service uses. Tests replace them with a
// temporary tree.
type Paths struct {
	BinaryDir  string
	Binary     string
	ConfigDir  string
	Config     string
	Cache      string
	RulesDir   string
	StateDir   string
	Owner      string
	BinaryHash string
	Unit       string
	Active     string
}

// DefaultPaths returns the paths from the Linux TUN design.
func DefaultPaths() Paths {
	state := "/var/lib/sbc-tun"
	config := "/etc/sbc/tun"
	binary := "/usr/local/lib/sbc-tun"
	return Paths{
		BinaryDir:  binary,
		Binary:     filepath.Join(binary, "sing-box"),
		ConfigDir:  config,
		Config:     filepath.Join(config, "config.json"),
		Cache:      filepath.Join(state, "cache.db"),
		RulesDir:   filepath.Join(config, "rules"),
		StateDir:   state,
		Owner:      filepath.Join(state, "owner"),
		BinaryHash: filepath.Join(state, "sing-box.sha256"),
		Unit:       filepath.Join("/etc/systemd/system", ServiceName),
		Active:     ActivePath,
	}
}

// Host is the part of the machine the environment check reads. Tests replace
// it so refusal cases do not depend on the test host.
type Host struct {
	GOOS     string
	Euid     int
	Getenv   func(string) string
	Stat     func(string) (os.FileInfo, error)
	ReadFile func(string) ([]byte, error)
	LookPath func(string) (string, error)
	Shared   func() bool
}

// SystemHost reads the real machine.
func SystemHost() Host {
	return Host{
		GOOS:     runtime.GOOS,
		Euid:     os.Geteuid(),
		Getenv:   os.Getenv,
		Stat:     os.Stat,
		ReadFile: os.ReadFile,
		LookPath: sbinLookPath,
		Shared:   func() bool { return install.SharedHost("linux") },
	}
}

// Check refuses a host where root-owned TUN capture is unsafe or impossible.
// It is deliberately non-destructive; the capability probe runs as root
// before the service is published.
func Check(host Host) error {
	if host.GOOS != "linux" {
		return i18n.New("Linux TUN is available only on native Linux")
	}
	if host.Euid != 0 {
		if _, err := host.LookPath("sudo"); err != nil {
			return i18n.New("sudo is required to install the Linux TUN service")
		}
	} else {
		return i18n.New("run 'sbc tun on' as your user; sbc asks for sudo itself")
	}
	for _, name := range []string{"SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"} {
		if host.Getenv != nil && host.Getenv(name) != "" {
			return i18n.New("Linux TUN cannot be configured from a remote SSH session; run it at the machine's own console")
		}
	}
	kernel := strings.ToLower(read(host, "/proc/sys/kernel/osrelease") + " " + read(host, "/proc/version"))
	if strings.Contains(kernel, "microsoft") || strings.Contains(kernel, "wsl") {
		return i18n.New("Linux TUN is refused under WSL2; use the Windows client or a native Linux machine")
	}
	if container := strings.TrimSpace(read(host, "/run/systemd/container")); container != "" {
		return i18n.New("Linux TUN is refused inside a container")
	}
	if exists(host, "/.dockerenv") {
		return i18n.New("Linux TUN is refused inside a container")
	}
	cgroup := strings.ToLower(read(host, "/proc/1/cgroup"))
	for _, marker := range []string{"docker", "containerd", "kubepods", "libpod", "podman", "lxc"} {
		if strings.Contains(cgroup, marker) {
			return i18n.New("Linux TUN is refused inside a container")
		}
	}
	if !exists(host, "/run/systemd/system") {
		return i18n.New("Linux TUN needs a running systemd system manager")
	}
	if !exists(host, "/dev/net/tun") {
		return i18n.New("Linux TUN needs /dev/net/tun")
	}
	for _, name := range []string{"ip", "useradd", "userdel", "nft", "systemd-run", "systemctl"} {
		if _, err := host.LookPath(name); err != nil {
			return i18n.Errorf("Linux TUN needs %s, which was not found", name)
		}
	}
	if host.Shared != nil && host.Shared() {
		return i18n.New("Linux TUN needs a single-user machine; this host has other login accounts")
	}
	return nil
}

func read(host Host, name string) string {
	if host.ReadFile == nil {
		return ""
	}
	data, _ := host.ReadFile(name)
	return string(data)
}

func exists(host Host, name string) bool {
	if host.Stat == nil {
		return false
	}
	_, err := host.Stat(name)
	return err == nil
}

// sbinLookPath finds an administrative tool. A desktop user's PATH often
// omits /usr/sbin, so a missing PATH entry must not look like a missing tool.
func sbinLookPath(name string) (string, error) {
	if path, err := exec.LookPath(name); err == nil {
		return path, nil
	}
	for _, dir := range []string{"/usr/sbin", "/sbin", "/usr/bin", "/bin"} {
		candidate := filepath.Join(dir, name)
		info, err := os.Stat(candidate)
		if err == nil && !info.IsDir() && info.Mode()&0o111 != 0 {
			return candidate, nil
		}
	}
	return "", exec.ErrNotFound
}

// Active blocks mixed mode for active, pending and unconfirmed TUN state.
func Active() bool { return Status() != "off" }

// ActiveAt reports whether a marker exists. Tests use a temporary path.
func ActiveAt(path string) bool {
	_, err := os.Stat(path)
	return !os.IsNotExist(err)
}

// State is the owner's copy of the TUN API address. It is a credential and
// must be mode 0600.
type State struct {
	APIPort   int    `json:"api_port"`
	APISecret string `json:"api_secret"`
	Interface string `json:"interface"`
	Since     string `json:"since,omitempty"`
}

// StatePath is the owner's local TUN state.
func StatePath(layout paths.Layout) string {
	return filepath.Join(layout.Config, "tun.json")
}

// WriteState writes the owner's TUN state with the credential-protected mode.
func WriteState(layout paths.Layout, state State) error {
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	return writeDurable(StatePath(layout), append(data, '\n'), 0o600)
}

// ReadState reads the owner's TUN state.
func ReadState(layout paths.Layout) (State, error) {
	data, err := os.ReadFile(StatePath(layout))
	if err != nil {
		return State{}, err
	}
	var state State
	if err := json.Unmarshal(data, &state); err != nil {
		return State{}, i18n.Errorf("read the TUN state: %w", err)
	}
	return state, nil
}

// RemoveState removes the owner's TUN state. A missing file is not an error.
func RemoveState(layout paths.Layout) error {
	err := os.Remove(StatePath(layout))
	if os.IsNotExist(err) {
		return nil
	}
	return err
}

// Owner records which user owns the single TUN installation on a machine.
type Owner struct {
	UID  int    `json:"uid"`
	User string `json:"user"`
}

// ReadOwner reads the root owner manifest.
func ReadOwner(path string) (Owner, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Owner{}, err
	}
	var owner Owner
	if err := json.Unmarshal(data, &owner); err != nil {
		return Owner{}, i18n.Errorf("read the TUN owner manifest: %w", err)
	}
	if owner.UID <= 0 {
		return Owner{}, i18n.New("the TUN owner manifest has no user")
	}
	return owner, nil
}

// WriteOwner writes the root owner manifest. The caller sets its ownership.
func WriteOwner(path string, owner Owner) error {
	data, err := json.Marshal(owner)
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0o640)
}

// RandomSecret returns a new API secret.
func RandomSecret() (string, error) {
	data := make([]byte, 16)
	if _, err := rand.Read(data); err != nil {
		return "", err
	}
	return hex.EncodeToString(data), nil
}

// ParseUID converts a numeric user ID from os/user.
func ParseUID(text string) (int, error) {
	uid, err := strconv.Atoi(text)
	if err != nil || uid <= 0 {
		return 0, i18n.Errorf("invalid user ID %q", text)
	}
	return uid, nil
}
