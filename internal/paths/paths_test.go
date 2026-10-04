package paths

import (
	"errors"
	"path/filepath"
	"testing"
)

func TestWindowsKeepsEverythingUnderLocalAppData(t *testing.T) {
	getenv := func(name string) string {
		if name == "LOCALAPPDATA" {
			return `C:\Users\me\AppData\Local`
		}
		return ""
	}
	noHome := func() (string, error) { return "", errors.New("no profile") }

	layout, err := defaults("windows", getenv, noHome, noHome)
	if err != nil {
		t.Fatal(err)
	}

	base := filepath.Join(`C:\Users\me\AppData\Local`, "sbc")
	if layout.Config != base || layout.Data != base {
		t.Errorf("layout %+v", layout)
	}
	if filepath.Base(layout.SBC()) != "sbc.exe" || filepath.Base(layout.SingBox()) != "sing-box.exe" {
		t.Errorf("programs %s and %s", layout.SBC(), layout.SingBox())
	}
}

func TestWindowsFindsTheFolderFromSbcItselfWithoutTheProfile(t *testing.T) {
	none := func(string) string { return "" }
	noHome := func() (string, error) { return "", errors.New("no profile") }
	base := filepath.Join("D:", "Users", "me", "AppData", "Local", "sbc")
	installed := func() (string, error) { return filepath.Join(base, "cli", "sbc.exe"), nil }

	layout, err := defaults("windows", none, noHome, installed)
	if err != nil {
		t.Fatal(err)
	}
	if layout.Data != base {
		t.Errorf("data %s, want %s", layout.Data, base)
	}

	elsewhere := func() (string, error) { return filepath.Join("C:", "Downloads", "sbc.exe"), nil }
	if _, err := defaults("windows", none, noHome, elsewhere); err == nil {
		t.Error("an sbc outside its folder found one without the profile")
	}
}

func TestLinuxAndMacOSKeepTheirDirectories(t *testing.T) {
	home := func() (string, error) { return "/home/me", nil }
	xdg := func(name string) string {
		if name == "XDG_CONFIG_HOME" {
			return "/xdg"
		}
		return ""
	}

	linux, err := defaults("linux", xdg, home, nil)
	if err != nil {
		t.Fatal(err)
	}
	if linux.Config != "/xdg/sbc" || linux.Data != "/home/me/.local/share/sbc" || linux.Exe != "" {
		t.Errorf("linux %+v", linux)
	}
	mac, err := defaults("darwin", xdg, home, nil)
	if err != nil {
		t.Fatal(err)
	}
	if mac.Config != "/home/me/Library/Application Support/sbc" || mac.Data != mac.Config {
		t.Errorf("darwin %+v", mac)
	}
}
