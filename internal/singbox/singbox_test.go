package singbox

import (
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

const portalConfig = `{"log":{"level":"info"},"inbounds":[{"type":"mixed","tag":"mixed","listen":"127.0.0.1","listen_port":1080}],"outbounds":[{"type":"direct","tag":"direct"}],"experimental":{"clash_api":{"default_mode":"china"}}}`

func TestPatchSetsThePortAndOpensTheAPIOnLoopback(t *testing.T) {
	local := Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret"}
	patched, err := Patch([]byte(portalConfig), local)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := ReadLocal(patched); err != nil || got != local {
		t.Fatalf("%+v: %v", got, err)
	}
	var root map[string]any
	json.Unmarshal(patched, &root)
	api := root["experimental"].(map[string]any)["clash_api"].(map[string]any)
	if api["external_controller"] != "127.0.0.1:9091" || api["default_mode"] != "china" {
		t.Fatal(api)
	}
}

func TestPatchAddsAndRemovesTheProxyPassword(t *testing.T) {
	local := Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret", Username: "sbc", Password: "pw"}
	patched, err := Patch([]byte(portalConfig), local)
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := ReadLocal(patched); got != local {
		t.Fatal(got)
	}
	local.Username, local.Password = "", ""
	patched, err = Patch(patched, local)
	if err != nil || strings.Contains(string(patched), "users") {
		t.Fatal(err, string(patched))
	}
}

func TestPatchNeedsTheInboundAndTheAPISection(t *testing.T) {
	for _, config := range []string{`{"inbounds":[],"experimental":{"clash_api":{}}}`, `{"inbounds":[{"tag":"mixed"}]}`, `not json`} {
		if _, err := Patch([]byte(config), Local{}); err == nil {
			t.Fatal("patched invalid config")
		}
	}
}

func TestFreePortSkipsAPortInUse(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	busy := listener.Addr().(*net.TCPAddr).Port
	port, err := FreePort(busy)
	if err != nil || port == busy || port == 0 {
		t.Fatal(port, err)
	}
	udp, err := net.ListenPacket("udp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		t.Fatal(err)
	}
	defer udp.Close()
	other, err := FreePort(port)
	if err != nil || other == port {
		t.Fatal(other, err)
	}
}

func realSingBox(t *testing.T) string {
	t.Helper()
	binary := os.Getenv("SBM_TEST_SING_BOX")
	if binary == "" {
		binary = filepath.Join("..", "..", ".cache", "sing-box-manager", "bin", "sing-box")
	}
	binary, _ = filepath.Abs(binary)
	if _, err := os.Stat(binary); err != nil {
		t.Skip("sing-box not available")
	}
	return binary
}

func TestSingBoxAcceptsAPatchedConfigAndRejectsABrokenOne(t *testing.T) {
	binary := realSingBox(t)
	dir := t.TempDir()
	config := filepath.Join(dir, "config.json")
	data, err := Patch([]byte(portalConfig), Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret"})
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(config, data, 0o600)
	if err := Check(binary, dir, config); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(config, []byte(`{"inbounds":[{"type":"not-a-protocol"}]}`), 0o600)
	if err := Check(binary, dir, config); err == nil {
		t.Fatal("accepted broken config")
	}
}

func TestPatchRemovesRetiredDiagnosticOverridesWithoutChangingProtocols(t *testing.T) {
	config := `{"inbounds":[{"type":"mixed","tag":"mixed"},{"type":"mixed","tag":"speed-test","listen_port":3080}],"outbounds":[{"type":"selector","tag":"proxy","outbounds":["snell"]},{"type":"selector","tag":"speed-test","outbounds":["snell"]}],"route":{"rules":[{"inbound":["speed-test"],"outbound":"speed-test"},{"clash_mode":"global","outbound":"proxy"}]},"experimental":{"clash_api":{}}}`
	if !HasDiagnosticOverride([]byte(config)) {
		t.Fatal("retired config not recognized")
	}
	patched, err := Patch([]byte(config), Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret"})
	if err != nil {
		t.Fatal(err)
	}
	if HasDiagnosticOverride(patched) || strings.Contains(string(patched), "speed-test") || !strings.Contains(string(patched), "snell") {
		t.Fatal(string(patched))
	}
	if HasDiagnosticOverride([]byte("invalid")) {
		t.Fatal("invalid JSON claimed an override")
	}
	second, err := Patch(patched, Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret"})
	if err != nil || string(second) != string(patched) {
		t.Fatal("patch not idempotent")
	}
}
