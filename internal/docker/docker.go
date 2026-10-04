// Package docker points the Docker daemon on Linux at the proxy, so image pulls
// use it. A systemd drop-in sets the daemon's proxy variables. It is the file
// the bash client wrote, byte for byte, so sbc takes that one over.
package docker

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// DropIn is the drop-in sbc writes.
const DropIn = "/etc/systemd/system/docker.service.d/http-proxy.conf"

// noProxy matches what shells get.
const noProxy = "localhost,127.0.0.0/8,::1,host.docker.internal"

// State says where the daemon's proxy points.
type State int

const (
	Off State = iota
	// On means the drop-in points at sbc's port.
	On
	// Stale means the drop-in is one sbc or the bash client wrote for another
	// port, as after 'sbc port'.
	Stale
	// Other means the drop-in holds something sbc did not write.
	Other
)

func (s State) String() string {
	switch s {
	case On:
		return i18n.T("on")
	case Stale:
		return i18n.T("on, for an earlier port")
	case Other:
		return i18n.T("set by someone else")
	default:
		return i18n.T("off")
	}
}

// Content is the drop-in for a proxy on port.
func Content(port int) string {
	proxy := fmt.Sprintf("http://127.0.0.1:%d", port)
	return "[Service]\n" +
		`Environment="http_proxy=` + proxy + "\"\n" +
		`Environment="https_proxy=` + proxy + "\"\n" +
		`Environment="no_proxy=` + noProxy + "\"\n" +
		`Environment="HTTP_PROXY=` + proxy + "\"\n" +
		`Environment="HTTPS_PROXY=` + proxy + "\"\n" +
		`Environment="NO_PROXY=` + noProxy + "\"\n"
}

// Runner runs a command with input on its standard input.
type Runner func(input, name string, args ...string) error

// Exec runs commands for real. sudo reads a password from the terminal itself.
func Exec(input, name string, args ...string) error {
	command := exec.Command(name, args...)
	command.Stdin = strings.NewReader(input)
	output, err := command.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s %s: %s", name, strings.Join(args, " "), strings.TrimSpace(string(output)))
	}
	return nil
}

// ErrUnsupported means there is no Docker daemon here that sbc can configure.
var ErrUnsupported = i18n.New("sbc sets the proxy of the Docker daemon on Linux; Docker Desktop on macOS keeps it in its own settings")

// ErrNotInstalled means systemd knows no Docker service.
var ErrNotInstalled = i18n.New("Docker is not installed here")

// Daemon reads and changes the daemon's drop-in. Changes go through sudo and
// restart the daemon, which stops containers that have no restart policy.
type Daemon struct {
	Path string
	Run  Runner
}

// New returns the daemon of this machine.
func New(goos string, run Runner) (*Daemon, error) {
	if goos != "linux" {
		return nil, ErrUnsupported
	}
	if run("", "systemctl", "cat", "docker.service") != nil {
		return nil, ErrNotInstalled
	}
	return &Daemon{Path: DropIn, Run: run}, nil
}

func (d *Daemon) State(port int) (State, error) {
	data, err := os.ReadFile(d.Path)
	if os.IsNotExist(err) {
		return Off, nil
	}
	if err != nil {
		return Off, err
	}
	written, ok := portOf(string(data))
	switch {
	case !ok:
		return Other, nil
	case written == port:
		return On, nil
	default:
		return Stale, nil
	}
}

// portOf returns the port of a drop-in that sbc or the bash client wrote.
func portOf(content string) (int, bool) {
	const prefix = `Environment="http_proxy=http://127.0.0.1:`
	for _, line := range strings.Split(content, "\n") {
		value, ok := strings.CutPrefix(line, prefix)
		if !ok {
			continue
		}
		port, err := strconv.Atoi(strings.TrimSuffix(value, `"`))
		if err != nil || strings.TrimRight(content, "\n") != strings.TrimRight(Content(port), "\n") {
			return 0, false
		}
		return port, true
	}
	return 0, false
}

// On writes the drop-in and restarts the daemon to load it.
func (d *Daemon) On(port int) error {
	if err := d.Run("", "sudo", "mkdir", "-p", filepath.Dir(d.Path)); err != nil {
		return err
	}
	if err := d.Run(Content(port), "sudo", "tee", d.Path); err != nil {
		return err
	}
	return d.restart()
}

// Off removes the drop-in and restarts the daemon without it.
func (d *Daemon) Off() error {
	if err := d.Run("", "sudo", "rm", "-f", d.Path); err != nil {
		return err
	}
	return d.restart()
}

func (d *Daemon) restart() error {
	if err := d.Run("", "sudo", "systemctl", "daemon-reload"); err != nil {
		return err
	}
	return d.Run("", "sudo", "systemctl", "restart", "docker")
}
