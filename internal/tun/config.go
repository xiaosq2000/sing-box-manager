package tun

import (
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strconv"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Local is what the owner adds to the portal's TUN config.
type Local struct {
	APIPort   int
	APISecret string
	CachePath string
	RulesDir  string
	// Selection and CacheID are set together for a maintenance generation.
	Selection Selection
	CacheID   string
}

// Patch adds the local API, cache path and absolute rule snapshot paths. The
// portal config is immutable input; a fresh byte slice is returned.
func Patch(config []byte, local Local) ([]byte, []string, error) {
	var root map[string]any
	if err := json.Unmarshal(config, &root); err != nil {
		return nil, nil, i18n.Errorf("read the TUN config: %w", err)
	}
	experimental, ok := root["experimental"].(map[string]any)
	if !ok {
		return nil, nil, i18n.New("the TUN config has no experimental section")
	}
	api, ok := experimental["clash_api"].(map[string]any)
	if !ok {
		api = map[string]any{}
		experimental["clash_api"] = api
	}
	api["external_controller"] = net.JoinHostPort(
		"127.0.0.1", strconv.Itoa(local.APIPort),
	)
	api["secret"] = local.APISecret

	cache, _ := experimental["cache_file"].(map[string]any)
	if cache == nil {
		cache = map[string]any{"enabled": true, "store_dns": true}
		experimental["cache_file"] = cache
	}
	cache["path"] = local.CachePath
	if err := patchSelection(root, local.Selection, local.CacheID); err != nil {
		return nil, nil, err
	}

	route, _ := root["route"].(map[string]any)
	sets, _ := route["rule_set"].([]any)
	seen := make(map[string]struct{})
	var files []string
	for _, item := range sets {
		set, ok := item.(map[string]any)
		if !ok {
			continue
		}
		initial, _ := set["initial_path"].(string)
		if initial == "" && set["type"] == "local" {
			initial, _ = set["path"].(string)
		}
		if initial == "" {
			if set["type"] == "remote" {
				return nil, nil, i18n.Errorf("the TUN rule set %q has no installed snapshot", set["tag"])
			}
			continue
		}
		name := filepath.Base(filepath.FromSlash(initial))
		if name == "" || name == "." || name == ".." {
			return nil, nil, i18n.Errorf("invalid rule snapshot path %q", initial)
		}
		// Remote rule sets still fetch at startup with update_interval=0.
		// The root service must only read the complete installed snapshot.
		localSet := map[string]any{
			"type": "local", "tag": set["tag"],
			"path": filepath.Join(local.RulesDir, name),
		}
		if format, ok := set["format"]; ok {
			localSet["format"] = format
		}
		for key := range set {
			delete(set, key)
		}
		for key, value := range localSet {
			set[key] = value
		}
		if _, ok := seen[name]; !ok {
			seen[name] = struct{}{}
			files = append(files, name)
		}
	}

	patched, err := json.MarshalIndent(root, "", "  ")
	if err != nil {
		return nil, nil, err
	}
	return patched, files, nil
}

// ReadAPI reads the loopback API address from a staged TUN config.
func ReadAPI(configPath string) (int, string, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return 0, "", err
	}
	var root struct {
		Experimental struct {
			ClashAPI struct {
				Controller string `json:"external_controller"`
				Secret     string `json:"secret"`
			} `json:"clash_api"`
		} `json:"experimental"`
	}
	if err := json.Unmarshal(data, &root); err != nil {
		return 0, "", i18n.Errorf("read the TUN config: %w", err)
	}
	_, portText, err := net.SplitHostPort(root.Experimental.ClashAPI.Controller)
	if err != nil {
		return 0, "", i18n.New("the TUN config has no local API address")
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port <= 0 {
		return 0, "", i18n.New("the TUN config has an invalid local API port")
	}
	if root.Experimental.ClashAPI.Secret == "" {
		return 0, "", i18n.New("the TUN config has no local API secret")
	}
	return port, root.Experimental.ClashAPI.Secret, nil
}

// ReadTunTag reads the tag of the TUN inbound from a staged TUN config.
func ReadTunTag(configPath string) (string, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return "", err
	}
	var root struct {
		Inbounds []struct {
			Type string `json:"type"`
			Tag  string `json:"tag"`
		} `json:"inbounds"`
	}
	if err := json.Unmarshal(data, &root); err != nil {
		return "", i18n.Errorf("read the TUN config: %w", err)
	}
	for _, inbound := range root.Inbounds {
		if inbound.Type == "tun" && inbound.Tag != "" {
			return inbound.Tag, nil
		}
	}
	return "", i18n.New("the TUN config has no TUN inbound tag")
}
