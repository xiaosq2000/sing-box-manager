// Package paths names the files sbc keeps for one user.
package paths

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Layout holds the two directories sbc writes. Config keeps the subscription
// link and the sing-box config, both mode 0600. Data keeps the binaries and is
// sing-box's working directory, where the rule files and its cache live.
type Layout struct {
	Config string
	Data   string
	// Exe is the suffix of program names: ".exe" on Windows.
	Exe string
}

// Default returns the platform's usual directories: XDG on Linux, Application
// Support on macOS and the local application data folder on Windows.
func Default() (Layout, error) {
	return defaults(runtime.GOOS, os.Getenv, os.UserHomeDir, os.Executable)
}

func defaults(goos string, getenv func(string) string, home, executable func() (string, error)) (Layout, error) {
	if goos == "windows" {
		base, err := windowsBase(getenv, executable)
		if err != nil {
			return Layout{}, err
		}
		return Layout{Config: base, Data: base, Exe: ".exe"}, nil
	}
	dir, err := home()
	if err != nil {
		return Layout{}, i18n.New("cannot find the home directory")
	}
	if goos == "darwin" {
		base := filepath.Join(dir, "Library", "Application Support", "sbc")
		return Layout{Config: base, Data: base}, nil
	}
	config := getenv("XDG_CONFIG_HOME")
	if config == "" {
		config = filepath.Join(dir, ".config")
	}
	data := getenv("XDG_DATA_HOME")
	if data == "" {
		data = filepath.Join(dir, ".local", "share")
	}
	return Layout{Config: filepath.Join(config, "sbc"), Data: filepath.Join(data, "sbc")}, nil
}

// windowsBase is %LOCALAPPDATA%\sbc. A scheduled task can run without the
// profile variables, so the sbc that the tasks run finds its folder from
// where it is.
func windowsBase(getenv func(string) string, executable func() (string, error)) (string, error) {
	if local := getenv("LOCALAPPDATA"); local != "" {
		return filepath.Join(local, "sbc"), nil
	}
	exe, err := executable()
	if err == nil && strings.EqualFold(filepath.Base(filepath.Dir(exe)), "cli") {
		return filepath.Dir(filepath.Dir(exe)), nil
	}
	return "", i18n.New("cannot find the home directory")
}

// Link is the file holding the subscription link, which is a credential.
func (l Layout) Link() string { return filepath.Join(l.Config, "subscription") }

// ConfigFile is the patched sing-box config, which holds credentials.
func (l Layout) ConfigFile() string { return filepath.Join(l.Config, "config.json") }

// ETag names the subscription config the current one came from.
func (l Layout) ETag() string { return filepath.Join(l.Config, "etag") }

// SingBoxVersion records the sing-box version installed in Bin.
func (l Layout) SingBoxVersion() string { return filepath.Join(l.Data, "sing-box.version") }

// EnvFile holds the proxy variables new shells load while the proxy is on.
func (l Layout) EnvFile() string { return filepath.Join(l.Config, "env.sh") }

// DesktopFile exists while 'sbc on' and 'sbc off' switch the desktop proxy
// along with the shells.
func (l Layout) DesktopFile() string { return filepath.Join(l.Config, "desktop") }

// UpgradeHint exists while a newer sbc or sing-box is available.
func (l Layout) UpgradeHint() string { return filepath.Join(l.Config, "upgrade-available") }

// Usage holds the traffic count the portal sent with the last refresh.
func (l Layout) Usage() string { return filepath.Join(l.Config, "usage") }

// Log is where sing-box's output goes when no service manager keeps it: on
// macOS and Windows.
func (l Layout) Log() string { return filepath.Join(l.Data, "sing-box.log") }

// PIDFile records the sing-box process 'sbc run' started on Windows, where
// the service manager stops sbc and must know when sing-box has followed.
func (l Layout) PIDFile() string { return filepath.Join(l.Data, "sing-box.pid") }

// CLIDir holds only sbc, so putting it on PATH exposes nothing else.
func (l Layout) CLIDir() string { return filepath.Join(l.Data, "cli") }

// SBC is sbc's own copy, which shells and the services run.
func (l Layout) SBC() string { return filepath.Join(l.CLIDir(), "sbc"+l.Exe) }

// Bin is where sing-box and the library its naive outbound loads live.
func (l Layout) Bin() string { return filepath.Join(l.Data, "bin") }

// SingBox is the sing-box binary.
func (l Layout) SingBox() string { return filepath.Join(l.Bin(), "sing-box"+l.Exe) }

// WriteFile writes data to path atomically with the given mode, creating the
// directory as private to the user.
func WriteFile(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	staged := path + ".tmp"
	if err := os.WriteFile(staged, data, mode); err != nil {
		return err
	}
	if err := os.Chmod(staged, mode); err != nil {
		os.Remove(staged)
		return err
	}
	err := os.Rename(staged, path)
	if err != nil && runtime.GOOS == "windows" {
		// Windows refuses to replace a running program, which can still be
		// renamed, so the old one moves aside until it has exited.
		os.Remove(path + ".old")
		if err := os.Rename(path, path+".old"); err == nil {
			return os.Rename(staged, path)
		}
	}
	if err != nil {
		os.Remove(staged)
	}
	return err
}
