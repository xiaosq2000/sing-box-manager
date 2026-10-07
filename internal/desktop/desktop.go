// Package desktop points the desktop's proxy settings at sbc: GNOME's on
// Linux, those of the active network service on macOS, and WinINet on Windows.
// They are the settings the script clients wrote. Settings that point
// anywhere else belong to someone else, and sbc leaves them alone.
package desktop

import (
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

// Host is where the local proxy listens.
const Host = "127.0.0.1"

// NoProxy lists the destinations the desktop reaches directly. It matches what
// shells get.
var NoProxy = []string{"localhost", "127.0.0.0/8", "::1", "host.docker.internal"}

// State says where the desktop's proxy points.
type State int

const (
	Off State = iota
	// On means the proxy points at sbc's port.
	On
	// Other means the proxy points somewhere else.
	Other
)

func (s State) String() string {
	switch s {
	case On:
		return i18n.T("on")
	case Other:
		return i18n.T("set to another proxy")
	default:
		return i18n.T("off")
	}
}

// Runner runs a command and returns its standard output.
type Runner func(name string, args ...string) (string, error)

// Exec runs commands for real. sudo reads a password from the terminal itself.
func Exec(name string, args ...string) (string, error) {
	command := exec.Command(name, args...)
	command.Stdin = os.Stdin
	var stderr strings.Builder
	command.Stderr = &stderr
	output, err := command.Output()
	if err != nil {
		detail := strings.TrimSpace(stderr.String())
		if detail == "" {
			detail = err.Error()
		}
		if name == "sudo" && len(args) > 2 && args[2] == linuxPolicyScript {
			return "", i18n.Errorf("could not change WebRTC browser policies: %s", detail)
		}
		return "", fmt.Errorf("%s %s: %s", name, strings.Join(args, " "), detail)
	}
	return string(output), nil
}

// Desktop reads and changes one desktop's proxy settings.
type Desktop interface {
	// Name says whose settings these are: GNOME, a macOS network service,
	// or Windows.
	Name() string
	State(port int) (State, error)
	On(port int) error
	Off() error
}

// ErrUnsupported means sbc cannot set the proxy of this desktop.
var ErrUnsupported = i18n.New("sbc sets the desktop proxy in GNOME on Linux, on macOS and on Windows")

// New returns the desktop this session runs in, judged from the platform and
// the session's variables.
func New(goos string, getenv func(string) string, run Runner) (Desktop, error) {
	switch goos {
	case "windows":
		privacy, err := newPrivacy(goos, run)
		if err != nil {
			return nil, err
		}
		return &Windows{Registry: winsettings.PowerShell{}, Privacy: privacy}, nil
	case "linux":
		if !isGNOME(getenv) {
			return nil, ErrUnsupported
		}
		privacy, err := newPrivacy("linux", run)
		return &GNOME{Run: run, Privacy: privacy}, err
	case "darwin":
		service, err := activeService(run)
		if err != nil {
			return nil, err
		}
		privacy, err := newPrivacy("darwin", run)
		return &MacOS{Service: service, Run: run, Privacy: privacy}, err
	default:
		return nil, ErrUnsupported
	}
}

func isGNOME(getenv func(string) string) bool {
	if getenv("GNOME_DESKTOP_SESSION_ID") != "" {
		return true
	}
	return strings.Contains(strings.ToLower(getenv("XDG_CURRENT_DESKTOP")+":"+getenv("DESKTOP_SESSION")), "gnome")
}

// GNOME changes org.gnome.system.proxy through gsettings.
type GNOME struct {
	Privacy WebRTCSettings
	Run     Runner
}

const gnomeSchema = "org.gnome.system.proxy"

// The schemes GNOME proxies. FTP is set but not read back, as the bash client
// did, so its value never decides whose settings these are.
var (
	gnomeSchemes      = []string{"http", "https", "ftp", "socks"}
	gnomeCheckSchemes = []string{"http", "https", "socks"}
)

func (g *GNOME) Name() string { return "GNOME" }

func (g *GNOME) get(schema, key string) (string, error) {
	output, err := g.Run("gsettings", "get", schema, key)
	return strings.Trim(strings.TrimSpace(output), "'"), err
}

func (g *GNOME) set(schema, key, value string) error {
	_, err := g.Run("gsettings", "set", schema, key, value)
	return err
}

func (g *GNOME) State(port int) (State, error) {
	mode, err := g.get(gnomeSchema, "mode")
	if err != nil || mode != "manual" {
		return Off, err
	}
	ours := false
	for _, scheme := range gnomeCheckSchemes {
		host, err := g.get(gnomeSchema+"."+scheme, "host")
		if err != nil {
			return Off, err
		}
		if host == "" {
			continue
		}
		value, err := g.get(gnomeSchema+"."+scheme, "port")
		if err != nil {
			return Off, err
		}
		if host != Host || value != strconv.Itoa(port) {
			return Other, nil
		}
		ours = true
	}
	if ours {
		return On, nil
	}
	return Off, nil
}

// On writes the hosts first and switches the mode last, so the desktop never
// uses a half-written proxy.
func (g *GNOME) On(port int) error {
	if g.Privacy != nil {
		if err := g.Privacy.Ensure(); err != nil {
			return err
		}
	}
	for _, scheme := range gnomeSchemes {
		if err := g.set(gnomeSchema+"."+scheme, "host", "'"+Host+"'"); err != nil {
			return err
		}
		if err := g.set(gnomeSchema+"."+scheme, "port", strconv.Itoa(port)); err != nil {
			return err
		}
	}
	if err := g.set(gnomeSchema, "ignore-hosts", "['"+strings.Join(NoProxy, "', '")+"']"); err != nil {
		return err
	}
	return g.set(gnomeSchema, "mode", "'manual'")
}

// Off switches the mode back and keeps the hosts, as the bash client did.
func (g *GNOME) Off() error {
	return g.set(gnomeSchema, "mode", "'none'")
}

// MacOS changes the proxies of one network service through networksetup,
// which needs administrator rights to change them.
type MacOS struct {
	Privacy WebRTCSettings
	Service string
	Run     Runner
}

// macGetters read the web, secure web and SOCKS proxies, in that order.
var macGetters = []string{"-getwebproxy", "-getsecurewebproxy", "-getsocksfirewallproxy"}

// activeService finds the network service that holds the default route. With
// a VPN or a virtual adapter up, that may not be the one people expect, which
// is why messages name it.
func activeService(run Runner) (string, error) {
	route, err := run("route", "-n", "get", "default")
	if err != nil {
		return "", i18n.New("this Mac has no default route, so there is no network service to set")
	}
	device := ""
	for _, line := range strings.Split(route, "\n") {
		if value, ok := strings.CutPrefix(strings.TrimSpace(line), "interface:"); ok {
			device = strings.TrimSpace(value)
		}
	}
	order, err := run("networksetup", "-listnetworkserviceorder")
	if err != nil {
		return "", err
	}
	previous := ""
	for _, line := range strings.Split(order, "\n") {
		if device != "" && strings.Contains(line, "Device: "+device+")") {
			// "(1) Wi-Fi", or "(*) Wi-Fi" for a disabled service.
			if _, name, ok := strings.Cut(strings.TrimSpace(previous), ") "); ok {
				return name, nil
			}
		}
		previous = line
	}
	return "", i18n.Errorf("no network service uses %q, the default route's interface", device)
}

func (m *MacOS) Name() string { return m.Service }

func (m *MacOS) State(port int) (State, error) {
	ours := false
	for _, getter := range macGetters {
		output, err := m.Run("networksetup", getter, m.Service)
		if err != nil {
			return Off, err
		}
		fields := map[string]string{}
		for _, line := range strings.Split(output, "\n") {
			if key, value, ok := strings.Cut(line, ": "); ok {
				fields[key] = strings.TrimSpace(value)
			}
		}
		if fields["Enabled"] != "Yes" || fields["Server"] == "" {
			continue
		}
		if fields["Server"] != Host || fields["Port"] != strconv.Itoa(port) {
			return Other, nil
		}
		ours = true
	}
	if ours {
		return On, nil
	}
	return Off, nil
}

func (m *MacOS) On(port int) error {
	if m.Privacy != nil {
		if err := m.Privacy.Ensure(); err != nil {
			return err
		}
	}
	value := strconv.Itoa(port)
	for _, setter := range []string{"-setwebproxy", "-setsecurewebproxy", "-setsocksfirewallproxy"} {
		if _, err := m.Run("sudo", "networksetup", setter, m.Service, Host, value); err != nil {
			return err
		}
	}
	if _, err := m.Run("sudo", append([]string{"networksetup", "-setproxybypassdomains", m.Service}, NoProxy...)...); err != nil {
		return err
	}
	return nil
}

func (m *MacOS) Off() error {
	for _, setter := range []string{"-setwebproxystate", "-setsecurewebproxystate", "-setsocksfirewallproxystate"} {
		if _, err := m.Run("sudo", "networksetup", setter, m.Service, "off"); err != nil {
			return err
		}
	}
	return nil
}
