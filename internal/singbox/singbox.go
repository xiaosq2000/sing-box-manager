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

// SpeedTestTag names the inbound and the selector that 'sbc speed --download'
// uses. The inbound sends everything to the selector, which holds the same
// protocols as the proxy selector, so sbc can switch it without touching the
// protocol in use, and test every protocol whatever the route.
const SpeedTestTag = "speed-test"

// proxySelectorTag names the portal's selector of protocols.
const proxySelectorTag = "proxy"

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
	// SpeedPort is the loopback port of the speed test's inbound, which asks
	// for the same password. Zero leaves the speed test out.
	SpeedPort int
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
	addSpeedTest(root, local)
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

// addSpeedTest replaces what an earlier patch added for the speed test: an
// inbound, a selector with the proxy selector's protocols, and a first route
// rule from one to the other. A config without the proxy selector gets none.
func addSpeedTest(root map[string]any, local Local) {
	inbounds, _ := root["inbounds"].([]any)
	outbounds, _ := root["outbounds"].([]any)
	inbounds, outbounds = withoutTag(inbounds, SpeedTestTag), withoutTag(outbounds, SpeedTestTag)
	route, _ := root["route"].(map[string]any)
	rules, _ := route["rules"].([]any)
	rules = slices.DeleteFunc(rules, func(item any) bool {
		rule, ok := item.(map[string]any)
		return ok && rule["outbound"] == SpeedTestTag
	})
	if _, ok := route["rules"]; ok {
		route["rules"] = rules
	}
	var protocols any
	for _, item := range outbounds {
		if outbound, ok := item.(map[string]any); ok && outbound["tag"] == proxySelectorTag {
			protocols = outbound["outbounds"]
		}
	}
	if local.SpeedPort > 0 && protocols != nil {
		inbound := map[string]any{"type": "mixed", "tag": SpeedTestTag, "listen": "127.0.0.1", "listen_port": local.SpeedPort}
		setUsers(inbound, local)
		inbounds = append(inbounds, inbound)
		outbounds = append(outbounds, map[string]any{"type": "selector", "tag": SpeedTestTag, "outbounds": protocols})
		if route == nil {
			route = map[string]any{}
			root["route"] = route
		}
		route["rules"] = append([]any{map[string]any{"inbound": []any{SpeedTestTag}, "outbound": SpeedTestTag}}, rules...)
	}
	root["inbounds"] = inbounds
	if outbounds != nil {
		root["outbounds"] = outbounds
	}
}

// MissingSpeedTest reports whether config has the proxy selector the speed
// test copies, but not the speed test, as configs written before it do.
func MissingSpeedTest(config []byte) bool {
	type tagged struct {
		Tag string `json:"tag"`
	}
	var root struct {
		Inbounds  []tagged `json:"inbounds"`
		Outbounds []tagged `json:"outbounds"`
	}
	if json.Unmarshal(config, &root) != nil {
		return false
	}
	named := func(tag string) func(tagged) bool {
		return func(item tagged) bool { return item.Tag == tag }
	}
	return slices.ContainsFunc(root.Outbounds, named(proxySelectorTag)) && !slices.ContainsFunc(root.Inbounds, named(SpeedTestTag))
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
		case SpeedTestTag:
			local.SpeedPort = inbound.ListenPort
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
