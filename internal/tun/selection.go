package tun

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/routes"
)

// Selection is the owner's current routing policy, not a probe override.
type Selection struct {
	Mode     string
	Protocol string
}

func (s Selection) empty() bool { return s.Mode == "" && s.Protocol == "" }

// patchSelection sets startup defaults and a fresh cache namespace. A new
// generation must not briefly capture traffic with stale cached selections.
func patchSelection(root map[string]any, s Selection, cacheID string) error {
	if s.empty() {
		return nil
	}
	if s.Mode == "" || s.Protocol == "" || cacheID == "" {
		return i18n.New("the TUN maintenance selection is incomplete")
	}
	experimental, _ := root["experimental"].(map[string]any)
	clash, _ := experimental["clash_api"].(map[string]any)
	if clash == nil {
		return i18n.New("the TUN config has no Clash API section")
	}
	mode := ""
	if current, ok := clash["default_mode"].(string); ok && routes.Canonical(current) == routes.Canonical(s.Mode) {
		mode = current
	}
	var inspect func(any)
	inspect = func(value any) {
		switch item := value.(type) {
		case map[string]any:
			if candidate, ok := item["clash_mode"].(string); ok && routes.Canonical(candidate) == routes.Canonical(s.Mode) && mode == "" {
				mode = candidate
			}
			for _, child := range item {
				inspect(child)
			}
		case []any:
			for _, child := range item {
				inspect(child)
			}
		}
	}
	inspect(root["route"])
	if mode == "" {
		return i18n.Errorf("the TUN candidate does not support the current route %q", s.Mode)
	}
	outbounds, _ := root["outbounds"].([]any)
	found := false
	for _, item := range outbounds {
		outbound, _ := item.(map[string]any)
		if outbound["tag"] != api.ProxySelector || outbound["type"] != "selector" {
			continue
		}
		options, _ := outbound["outbounds"].([]any)
		for _, option := range options {
			if option == s.Protocol {
				outbound["default"] = s.Protocol
				found = true
			}
		}
	}
	if !found {
		return i18n.Errorf("the TUN candidate does not support the current protocol %q", s.Protocol)
	}
	clash["default_mode"] = mode
	cache, _ := experimental["cache_file"].(map[string]any)
	if cache == nil {
		cache = map[string]any{}
		experimental["cache_file"] = cache
	}
	cache["enabled"] = true
	cache["cache_id"] = cacheID
	return nil
}

func checkSelection(config string, s Selection, transaction string) error {
	if s.empty() {
		return nil
	}
	data, err := os.ReadFile(config)
	if err != nil {
		return err
	}
	var root map[string]any
	if err = json.Unmarshal(data, &root); err != nil {
		return err
	}
	experimental, _ := root["experimental"].(map[string]any)
	clash, _ := experimental["clash_api"].(map[string]any)
	cache, _ := experimental["cache_file"].(map[string]any)
	if cache["enabled"] != true || cache["cache_id"] != "sbc-tun-"+transaction || routes.Canonical(fmt.Sprint(clash["default_mode"])) != routes.Canonical(s.Mode) {
		return i18n.New("the TUN candidate does not preserve the requested startup selection")
	}
	outbounds, _ := root["outbounds"].([]any)
	for _, item := range outbounds {
		outbound, _ := item.(map[string]any)
		if outbound["tag"] == api.ProxySelector && outbound["default"] != s.Protocol {
			return i18n.New("the TUN candidate does not preserve the requested startup protocol")
		}
	}
	return patchSelection(root, s, "sbc-tun-"+transaction)
}

func verifySelection(paths Paths, s Selection) error {
	if s.empty() {
		return nil
	}
	port, secret, err := ReadAPI(paths.Config)
	if err != nil {
		return err
	}
	client := api.New(port, secret)
	if err = client.WaitReady(15 * time.Second); err != nil {
		return err
	}
	mode, _, err := client.Modes()
	if err != nil || mode != routes.Canonical(s.Mode) {
		return i18n.New("the TUN service did not preserve the current route")
	}
	protocol, _, err := client.Selected(api.ProxySelector)
	if err != nil || protocol != s.Protocol {
		return i18n.New("the TUN service did not preserve the current protocol")
	}
	return nil
}
