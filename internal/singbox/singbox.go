// Package singbox adapts the portal's config to this machine and runs sing-box.
package singbox

import (
	"bytes"
	"encoding/json"
	"net"
	"os/exec"
	"slices"
	"strconv"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// MixedInboundTag names the local proxy inbound in the portal's config.
const MixedInboundTag = "mixed"

// Candidate publication removes the retired diagnostic route override.
const retiredDiagnosticTag = "speed-test"

// Local is what this machine adds to the portal's config.
type Local struct {
	ListenPort int
	// APIPort and APISecret open sing-box's Clash API on loopback, which
	// switches route and protocol. The portal's config leaves it closed.
	APIPort   int
	APISecret string
	// Username and Password, when set, are what the local proxy asks for, so
	// other accounts on a shared machine cannot use it.
	Username string
	Password string
}

// Patch returns the portal's config with the local settings applied.
func Patch(config []byte, local Local) ([]byte, error) {
	var root map[string]any
	if err := json.Unmarshal(config, &root); err != nil {
		return nil, i18n.Errorf("read the config: %w", err)
	}
	inbounds, _ := root["inbounds"].([]any)
	patched := false
	for _, item := range inbounds {
		inbound, ok := item.(map[string]any)
		if ok && inbound["tag"] == MixedInboundTag {
			inbound["listen"] = "127.0.0.1"
			inbound["listen_port"] = local.ListenPort
			setUsers(inbound, local)
			patched = true
		}
	}
	if !patched {
		return nil, i18n.New("the config has no mixed inbound")
	}
	removeDiagnosticOverride(root)
	experimental, _ := root["experimental"].(map[string]any)
	if experimental == nil {
		return nil, i18n.New("the config has no experimental section")
	}
	api, _ := experimental["clash_api"].(map[string]any)
	if api == nil {
		return nil, i18n.New("the config has no Clash API section")
	}
	api["external_controller"] = net.JoinHostPort("127.0.0.1", strconv.Itoa(local.APIPort))
	api["secret"] = local.APISecret
	return json.MarshalIndent(root, "", "  ")
}

func setUsers(inbound map[string]any, local Local) {
	if local.Username != "" {
		inbound["users"] = []any{map[string]any{"username": local.Username, "password": local.Password}}
	} else {
		delete(inbound, "users")
	}
}

func removeDiagnosticOverride(root map[string]any) {
	inbounds, _ := root["inbounds"].([]any)
	outbounds, _ := root["outbounds"].([]any)
	root["inbounds"] = withoutTag(inbounds, retiredDiagnosticTag)
	if outbounds != nil {
		root["outbounds"] = withoutTag(outbounds, retiredDiagnosticTag)
	}
	route, _ := root["route"].(map[string]any)
	rules, _ := route["rules"].([]any)
	if _, ok := route["rules"]; ok {
		route["rules"] = slices.DeleteFunc(rules, func(item any) bool {
			rule, ok := item.(map[string]any)
			return ok && rule["outbound"] == retiredDiagnosticTag
		})
	}
}

// HasDiagnosticOverride forces a complete refresh for a retired diagnostic inlet.
func HasDiagnosticOverride(config []byte) bool {
	var root struct {
		Inbounds []struct {
			Tag string `json:"tag"`
		}
		Outbounds []struct {
			Tag string `json:"tag"`
		}
		Route struct {
			Rules []struct {
				Outbound string `json:"outbound"`
			}
		}
	}
	if json.Unmarshal(config, &root) != nil {
		return false
	}
	for _, inlet := range root.Inbounds {
		if inlet.Tag == retiredDiagnosticTag {
			return true
		}
	}
	for _, outbound := range root.Outbounds {
		if outbound.Tag == retiredDiagnosticTag {
			return true
		}
	}
	for _, rule := range root.Route.Rules {
		if rule.Outbound == retiredDiagnosticTag {
			return true
		}
	}
	return false
}

func withoutTag(items []any, tag string) []any {
	return slices.DeleteFunc(items, func(item any) bool {
		entry, ok := item.(map[string]any)
		return ok && entry["tag"] == tag
	})
}

// ReadLocal returns the local settings a patched config holds.
func ReadLocal(config []byte) (Local, error) {
	var root struct {
		Inbounds []struct {
			Tag        string `json:"tag"`
			ListenPort int    `json:"listen_port"`
			Users      []struct {
				Username string `json:"username"`
				Password string `json:"password"`
			} `json:"users"`
		} `json:"inbounds"`
		Experimental struct {
			ClashAPI struct {
				Controller string `json:"external_controller"`
				Secret     string `json:"secret"`
			} `json:"clash_api"`
		} `json:"experimental"`
	}
	if err := json.Unmarshal(config, &root); err != nil {
		return Local{}, i18n.Errorf("read the config: %w", err)
	}
	var local Local
	for _, inbound := range root.Inbounds {
		switch inbound.Tag {
		case MixedInboundTag:
			local.ListenPort = inbound.ListenPort
			if len(inbound.Users) > 0 {
				local.Username, local.Password = inbound.Users[0].Username, inbound.Users[0].Password
			}
		}
	}
	_, port, err := net.SplitHostPort(root.Experimental.ClashAPI.Controller)
	if err != nil {
		return Local{}, i18n.New("the config has no local API address")
	}
	local.APIPort, _ = strconv.Atoi(port)
	local.APISecret = root.Experimental.ClashAPI.Secret
	return local, nil
}

// FreePort returns a loopback port free for both TCP and UDP, preferring
// preferred when it is free.
func FreePort(preferred int) (int, error) {
	if preferred > 0 && portFree(preferred) {
		return preferred, nil
	}
	for range 100 {
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			return 0, err
		}
		port := listener.Addr().(*net.TCPAddr).Port
		listener.Close()
		if portFree(port) {
			return port, nil
		}
	}
	return 0, i18n.New("no free loopback port")
}

func portFree(port int) bool {
	address := net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	tcp, err := net.Listen("tcp", address)
	if err != nil {
		return false
	}
	defer tcp.Close()
	udp, err := net.ListenPacket("udp", address)
	if err != nil {
		return false
	}
	udp.Close()
	return true
}

// Check runs `sing-box check` on a config with the working directory sing-box
// will run in.
func Check(binary, workDir, configPath string) error {
	var stderr bytes.Buffer
	command := exec.Command(binary, "check", "-D", workDir, "-c", configPath)
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		if detail == "" {
			detail = err.Error()
		}
		return i18n.Errorf("sing-box rejected the config: %s", detail)
	}
	return nil
}
