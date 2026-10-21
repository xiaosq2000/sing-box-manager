package desktop

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

//go:embed geolocation_policy.sh
var geolocationPolicyScript string

const geolocationPolicyData = "{\"DefaultGeolocationSetting\":2}\n"

// ChromeGeolocation manages an explicit, Linux Chrome-only choice. No proxy or
// maintenance operation enables it. PolicyDir is fixed in production.
type ChromeGeolocation struct {
	StateFile string
	PolicyDir string
	UID       string
	Run       Runner
}

// GeolocationStatus describes local files, not a running browser's effective policy.
type GeolocationStatus struct {
	Enabled    bool
	Owned      bool
	Configured bool
	Policies   []string
	Conflicts  []string
}

// NewChromeGeolocation uses the fixed Linux Chrome policy directory.
func NewChromeGeolocation(goos, stateFile string, run Runner) (*ChromeGeolocation, error) {
	if goos != "linux" {
		return nil, i18n.New("Chrome geolocation protection is available on Linux only")
	}
	return &ChromeGeolocation{
		StateFile: stateFile,
		PolicyDir: "/etc/opt/chrome/policies/managed",
		UID:       strconv.Itoa(os.Getuid()),
		Run:       run,
	}, nil
}

func (p *ChromeGeolocation) files() (string, string) {
	return filepath.Join(p.PolicyDir, "sbc-geolocation-"+p.UID+".json"), filepath.Join(p.PolicyDir, ".sbc-geolocation-"+p.UID+".owner")
}

// Missing components are permitted, but their existing ancestors must be safe.
func geolocationPath(path string, regular bool) error {
	if !filepath.IsAbs(path) {
		return i18n.Errorf("Chrome geolocation path is unsafe: %s", path)
	}
	path = filepath.Clean(path)
	for parent := path; ; parent = filepath.Dir(parent) {
		info, err := os.Lstat(parent)
		if err != nil && !os.IsNotExist(err) {
			return err
		}
		if err == nil {
			unsafe := info.Mode()&os.ModeSymlink != 0
			if parent == path {
				if regular {
					unsafe = unsafe || !info.Mode().IsRegular() || info.Size() > 8<<20
				} else {
					unsafe = unsafe || !info.IsDir()
				}
			}
			if unsafe {
				return i18n.Errorf("Chrome geolocation path is unsafe: %s", parent)
			}
		}
		if filepath.Dir(parent) == parent {
			return nil
		}
	}
}

func geolocationRead(path string) ([]byte, error) {
	if err := geolocationPath(path, true); err != nil {
		return nil, err
	}
	return os.ReadFile(path)
}

func (p *ChromeGeolocation) owned() (bool, error) {
	_, marker := p.files()
	data, err := geolocationRead(marker)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if string(data) != "sbc-geolocation-v1:"+p.UID+"\n" {
		return false, i18n.New("the Chrome geolocation ownership record is invalid. Cleanup cannot safely continue")
	}
	return true, nil
}

// Status performs no writes, sudo calls, browser launches or network requests.
func (p *ChromeGeolocation) Status() (GeolocationStatus, error) {
	var status GeolocationStatus
	data, err := geolocationRead(p.StateFile)
	if err == nil {
		switch string(data) {
		case "on\n":
			status.Enabled = true
		case "off\n":
		default:
			return status, i18n.New("the Chrome geolocation choice is invalid. Run 'sbc privacy browser geolocation on' or 'sbc privacy browser geolocation off'")
		}
	} else if !os.IsNotExist(err) {
		return status, err
	}
	return p.policyStatus(status)
}

func (p *ChromeGeolocation) policyStatus(status GeolocationStatus) (GeolocationStatus, error) {
	if err := geolocationPath(p.PolicyDir, false); err != nil {
		return status, err
	}
	var err error
	status.Owned, err = p.owned()
	if err != nil {
		return status, err
	}
	files, err := linuxPolicyFiles(p.PolicyDir)
	if err != nil {
		return status, err
	}
	for _, file := range files {
		data, err := geolocationRead(file)
		if err != nil {
			return status, err
		}
		var values map[string]json.RawMessage
		if err := json.Unmarshal(data, &values); err != nil || values == nil {
			return status, i18n.Errorf("could not inspect Chrome geolocation policies at %s", file)
		}
		relevant, conflict := false, false
		if value, ok := values["DefaultGeolocationSetting"]; ok {
			relevant = true
			var setting int
			if json.Unmarshal(value, &setting) != nil || setting != 2 {
				conflict = true
			} else {
				status.Configured = true
			}
		}
		for _, key := range []string{"GeolocationAllowedForUrls", "GeolocationBlockedForUrls"} {
			if value, ok := values[key]; ok {
				var urls []json.RawMessage
				invalid := json.Unmarshal(value, &urls) != nil || bytes.Equal(bytes.TrimSpace(value), []byte("null"))
				for _, url := range urls {
					var text string
					if json.Unmarshal(url, &text) != nil || text == "" {
						invalid = true
					}
				}
				if invalid || len(urls) != 0 {
					relevant = true
					if invalid || key == "GeolocationAllowedForUrls" {
						conflict = true
					}
				}
			}
		}
		if relevant {
			status.Policies = append(status.Policies, file)
		}
		if conflict {
			status.Conflicts = append(status.Conflicts, file)
		}
	}
	return status, nil
}

func (p *ChromeGeolocation) saveChoice(enabled bool) error {
	if err := os.MkdirAll(filepath.Dir(p.StateFile), 0o700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(p.StateFile), ".geolocation-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	choice := "off\n"
	if enabled {
		choice = "on\n"
	}
	if _, err = file.WriteString(choice); err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(file.Name(), p.StateFile)
	}
	return err
}

// Set records off before removal. On is saved only after verified configuration.
func (p *ChromeGeolocation) Set(enabled bool) error {
	if err := geolocationPath(p.StateFile, true); err != nil {
		return err
	}
	unlock, err := lockUnixPrivacy(p.StateFile + ".lock")
	if err != nil {
		return err
	}
	defer unlock()
	if !enabled {
		if err := p.saveChoice(false); err != nil {
			return err
		}
		return p.remove()
	}
	// Inspect policy files independently of a damaged saved choice, so an
	// explicit command can repair that choice without discarding ownership.
	status, err := p.policyStatus(GeolocationStatus{})
	if err != nil {
		return err
	}
	if len(status.Conflicts) != 0 {
		return i18n.Errorf("Chrome geolocation policies conflict at: %s. Review DefaultGeolocationSetting and geolocation URL lists", strings.Join(status.Conflicts, ", "))
	}
	file, _ := p.files()
	if _, err := os.Lstat(file); err == nil && !status.Owned {
		return i18n.Errorf("Chrome geolocation settings already exist without ownership: %s", file)
	} else if err != nil && !os.IsNotExist(err) {
		return err
	}
	if !status.Configured {
		if err := p.mutate("on"); err != nil {
			return err
		}
		data, err := geolocationRead(file)
		if err != nil {
			return err
		}
		owned, err := p.owned()
		if err != nil {
			return err
		}
		if !owned || string(data) != geolocationPolicyData {
			return i18n.Errorf("Chrome did not retain the owned geolocation policy at %s", file)
		}
	}
	status, err = p.policyStatus(GeolocationStatus{})
	if err != nil {
		return err
	}
	if !status.Configured || len(status.Conflicts) != 0 {
		return i18n.New("Chrome geolocation policies changed during setup. Inspect the status and retry")
	}
	return p.saveChoice(true)
}

func (p *ChromeGeolocation) mutate(action string) error {
	if _, err := p.Run("sudo", "/bin/sh", "-c", geolocationPolicyScript, "sbc-geolocation", action, p.PolicyDir, p.UID); err != nil {
		return i18n.Errorf("could not change Chrome geolocation policies: %w", err)
	}
	return nil
}

func (p *ChromeGeolocation) remove() error {
	owned, err := p.owned()
	if err != nil || !owned {
		return err
	}
	if err := p.mutate("off"); err != nil {
		return err
	}
	owned, err = p.owned()
	if err != nil {
		return err
	}
	if owned {
		return i18n.New("Chrome geolocation cleanup is incomplete. Retry 'sbc privacy browser geolocation off'")
	}
	file, _ := p.files()
	data, err := geolocationRead(file)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if string(data) == geolocationPolicyData {
		return i18n.New("Chrome geolocation cleanup is incomplete. Retry 'sbc privacy browser geolocation off'")
	}
	return nil // Externally changed policy files remain unowned and intact.
}

// Cleanup preserves the choice if a later uninstall step fails.
func (p *ChromeGeolocation) Cleanup() error {
	if err := geolocationPath(p.StateFile, true); err != nil {
		return err
	}
	unlock, err := lockUnixPrivacy(p.StateFile + ".lock")
	if err != nil {
		return err
	}
	defer unlock()
	return p.remove()
}
