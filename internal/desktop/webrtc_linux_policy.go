package desktop

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

//go:embed webrtc_linux_policy.sh
var linuxPolicyScript string

func linuxPolicyFiles(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var result []string
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".json") {
			result = append(result, filepath.Join(dir, entry.Name()))
		}
	}
	return result, nil
}
func readLinuxPolicy(path string) (map[string]json.RawMessage, error) {
	data, err := privateFile(path)
	if err != nil {
		return nil, err
	}
	var values map[string]json.RawMessage
	if err := json.Unmarshal(data, &values); err != nil {
		return nil, i18n.Errorf("could not read browser policy file %s: %w", path, err)
	}
	return values, nil
}
func linuxBrowserInstalled(policy unixPolicy) bool {
	if _, err := os.Stat(policy.location); err == nil {
		return true
	}
	for _, name := range policy.programs {
		if _, err := exec.LookPath(name); err == nil {
			return true
		}
	}
	return false
}
func linuxPolicyData(policy unixPolicy) []byte {
	data, _ := json.Marshal(map[string]string{policy.name: winsettings.WebRtcDisableNonProxiedUDP})
	return append(data, '\n')
}
func (p *unixPrivacy) linuxFiles(policy unixPolicy) (string, string) {
	return filepath.Join(policy.location, "sbc-webrtc-"+p.uid+".json"), filepath.Join(policy.location, ".sbc-webrtc-"+p.uid+".owner")
}
func (p *unixPrivacy) linuxOwned(policy unixPolicy) (bool, error) {
	_, marker := p.linuxFiles(policy)
	data, err := privateFile(marker)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if string(data) != "sbc-webrtc-v1:"+p.uid+"\n" {
		return false, i18n.New("the WebRTC policy ownership record is invalid; cleanup cannot safely continue")
	}
	return true, nil
}
func (p *unixPrivacy) setLinuxPolicy(policy unixPolicy, enabled bool) error {
	file, _ := p.linuxFiles(policy)
	owned, err := p.linuxOwned(policy)
	if err != nil {
		return err
	}
	want := linuxPolicyData(policy)
	if enabled {
		if !linuxBrowserInstalled(policy) && !owned {
			return nil
		}
		files, err := linuxPolicyFiles(policy.location)
		if err != nil {
			return err
		}
		exists := false
		for _, path := range files {
			values, err := readLinuxPolicy(path)
			if err != nil {
				return err
			}
			if value, ok := values[policy.name]; ok {
				var text string
				if json.Unmarshal(value, &text) != nil || text != winsettings.WebRtcDisableNonProxiedUDP {
					return i18n.Errorf("an existing WebRTC policy at %s conflicts with proxy-only UDP; review your browser policies", path)
				}
				exists = true
			}
		}
		if exists {
			return nil
		}
		// A reserved filename without its ownership record is also foreign.
		if _, err := os.Lstat(file); err == nil {
			return i18n.Errorf("WebRTC settings already exist without ownership: %s", file)
		} else if !os.IsNotExist(err) {
			return err
		}
	} else if !owned {
		return nil
	}
	action := "off"
	if enabled {
		action = "on"
	}
	// Only fixed browser locations, this account's UID and constant policy data
	// reach the elevated helper. It rechecks ownership before each mutation.
	if _, err := p.run("sudo", "/bin/sh", "-c", linuxPolicyScript, "sbc-webrtc", action, policy.location, p.uid, string(want)); err != nil {
		return err
	}
	owned, err = p.linuxOwned(policy)
	if err != nil {
		return err
	}
	if enabled {
		got, err := privateFile(file)
		if err != nil {
			return err
		}
		if !owned || !bytes.Equal(got, want) {
			return i18n.Errorf("the browser did not retain the WebRTC policy at %s", file)
		}
		return nil
	}
	if owned {
		return i18n.Errorf("the browser did not remove the owned WebRTC policy at %s", file)
	}
	got, err := privateFile(file)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	if err == nil && bytes.Equal(got, want) {
		return i18n.Errorf("the browser did not remove the owned WebRTC policy at %s", file)
	}
	return nil
}

func (p *unixPrivacy) remainingLinuxPolicies() ([]string, error) {
	var locations []string
	for _, policy := range p.policies {
		files, err := linuxPolicyFiles(policy.location)
		if err != nil {
			return nil, err
		}
		for _, file := range files {
			values, err := readLinuxPolicy(file)
			if err != nil {
				return nil, err
			}
			_, supported := values[policy.name]
			_, legacy := values[winsettings.WebRtcPolicyName]
			if supported || legacy {
				locations = append(locations, file)
			}
		}
	}
	return locations, nil
}
