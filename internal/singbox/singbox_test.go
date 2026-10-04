package singbox

import (
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/probe"
)

const portalConfig = `{
  "log": {"level": "info"},
  "inbounds": [{"type": "mixed", "tag": "mixed", "listen": "127.0.0.1", "listen_port": 1080}],
  "outbounds": [{"type": "direct", "tag": "direct"}],
  "experimental": {"clash_api": {"default_mode": "china"}}
}`

func TestPatchSetsThePortAndOpensTheAPIOnLoopback(t *testing.T) {
	patched, err := Patch([]byte(portalConfig), Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret"})
	if err != nil {
		t.Fatal(err)
	}
	local, err := ReadLocal(patched)
	if err != nil {
		t.Fatal(err)
	}
	if local != (Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret"}) {
		t.Errorf("got %+v", local)
	}
	var root map[string]any
	json.Unmarshal(patched, &root)
	api := root["experimental"].(map[string]any)["clash_api"].(map[string]any)
	if api["external_controller"] != "127.0.0.1:9091" || api["default_mode"] != "china" {
		t.Errorf("clash_api %v", api)
	}
}

func TestPatchAddsAndRemovesTheProxyPassword(t *testing.T) {
	withAuth := Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret", Username: "sbc", Password: "pw"}
	patched, err := Patch([]byte(portalConfig), withAuth)
	if err != nil {
		t.Fatal(err)
	}
	if local, _ := ReadLocal(patched); local != withAuth {
		t.Errorf("read back %+v", local)
	}

	withAuth.Username, withAuth.Password = "", ""
	unpatched, err := Patch(patched, withAuth)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(unpatched), "users") {
		t.Errorf("the password stayed:\n%s", unpatched)
	}
}

func TestPatchNeedsTheInboundAndTheAPISection(t *testing.T) {
	for _, config := range []string{
		`{"inbounds": [], "experimental": {"clash_api": {}}}`,
		`{"inbounds": [{"tag": "mixed"}]}`,
		`not json`,
	} {
		if _, err := Patch([]byte(config), Local{}); err == nil {
			t.Errorf("%s was patched", config)
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
	if err != nil {
		t.Fatal(err)
	}
	if port == busy || port == 0 {
		t.Errorf("got %d, which is busy", port)
	}
	udp, err := net.ListenPacket("udp", "127.0.0.1:"+strconv.Itoa(port))
	if err != nil {
		t.Fatalf("port %d is not free for UDP: %v", port, err)
	}
	udp.Close()
}

// The binary `pixi run fetch-sing-box` provides, when it is there.
func realSingBox(t *testing.T) string {
	t.Helper()
	if path := os.Getenv("SBM_TEST_SING_BOX"); path != "" {
		return path
	}
	path, _ := filepath.Abs("../../.cache/sing-box-manager/bin/sing-box")
	if _, err := os.Stat(path); err != nil {
		t.Skip("run `pixi run fetch-sing-box` or set SBM_TEST_SING_BOX")
	}
	return path
}

func TestSingBoxAcceptsAPatchedConfigAndRejectsABrokenOne(t *testing.T) {
	binary := realSingBox(t)
	dir := t.TempDir()
	patched, err := Patch([]byte(portalConfig), Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret", Username: "sbc", Password: "pw"})
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, patched, 0o600)
	if err := Check(binary, dir, path); err != nil {
		t.Fatal(err)
	}

	os.WriteFile(path, []byte(`{"inbounds": [{"type": "nonsense"}]}`), 0o600)
	err = Check(binary, dir, path)
	if err == nil || !strings.Contains(err.Error(), "sing-box rejected the config") {
		t.Fatalf("got %v", err)
	}
}

// A config with the portal's proxy selector, whose protocols the speed test
// copies.
const selectorConfig = `{
  "inbounds": [{"type": "mixed", "tag": "mixed", "listen": "127.0.0.1", "listen_port": 1080}],
  "outbounds": [
    {"type": "selector", "tag": "proxy", "outbounds": ["good", "dead"], "default": "dead"},
    {"type": "direct", "tag": "good"},
    {"type": "socks", "tag": "dead", "server": "127.0.0.1", "server_port": 9}
  ],
  "route": {"rules": [{"action": "sniff"}, {"clash_mode": "china", "outbound": "proxy"}], "final": "proxy"},
  "experimental": {"clash_api": {"default_mode": "china"}}
}`

func TestPatchAddsTheSpeedTestBesideTheProxySelector(t *testing.T) {
	local := Local{ListenPort: 2080, APIPort: 9091, APISecret: "s3cret", Username: "sbc", Password: "pw", SpeedPort: 3080}
	patched, err := Patch([]byte(selectorConfig), local)
	if err != nil {
		t.Fatal(err)
	}
	if read, _ := ReadLocal(patched); read != local {
		t.Errorf("read back %+v", read)
	}
	var root struct {
		Inbounds  []map[string]any `json:"inbounds"`
		Outbounds []map[string]any `json:"outbounds"`
		Route     struct {
			Rules []map[string]any `json:"rules"`
		} `json:"route"`
	}
	json.Unmarshal(patched, &root)
	inbound, _ := json.Marshal(root.Inbounds[len(root.Inbounds)-1])
	if string(inbound) != `{"listen":"127.0.0.1","listen_port":3080,"tag":"speed-test","type":"mixed","users":[{"password":"pw","username":"sbc"}]}` {
		t.Errorf("inbound %s", inbound)
	}
	selector, _ := json.Marshal(root.Outbounds[len(root.Outbounds)-1])
	if string(selector) != `{"outbounds":["good","dead"],"tag":"speed-test","type":"selector"}` {
		t.Errorf("selector %s", selector)
	}
	rule, _ := json.Marshal(root.Route.Rules[0])
	if string(rule) != `{"inbound":["speed-test"],"outbound":"speed-test"}` || len(root.Route.Rules) != 3 {
		t.Errorf("rules start with %s, and there are %d", rule, len(root.Route.Rules))
	}

	again, _ := Patch(patched, local)
	if string(again) != string(patched) {
		t.Errorf("patching twice changed the config:\n%s", again)
	}
	local.SpeedPort = 0
	without, _ := Patch(patched, local)
	if strings.Contains(string(without), "speed-test") {
		t.Errorf("the speed test stayed:\n%s", without)
	}
	if !MissingSpeedTest(without) || MissingSpeedTest(patched) || MissingSpeedTest([]byte(portalConfig)) {
		t.Error("MissingSpeedTest is wrong about a config")
	}
	local.SpeedPort = 3080
	plain, _ := Patch([]byte(portalConfig), local)
	if strings.Contains(string(plain), "speed-test") || strings.Contains(string(plain), "route") {
		t.Errorf("a config without the proxy selector got a speed test:\n%s", plain)
	}
}

func TestTheSpeedTestInboundUsesItsOwnSelector(t *testing.T) {
	binary := realSingBox(t)
	dir := t.TempDir()
	ports := make([]int, 3)
	for index := range ports {
		port, err := FreePort(0)
		if err != nil {
			t.Fatal(err)
		}
		ports[index] = port
	}
	local := Local{ListenPort: ports[0], APIPort: ports[1], APISecret: "s3cret", Username: "sbc", Password: "pw", SpeedPort: ports[2]}
	patched, err := Patch([]byte(selectorConfig), local)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, patched, 0o600)
	process := exec.Command(binary, "run", "-D", dir, "-c", path)
	if err := process.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		process.Process.Kill()
		process.Wait()
	})
	client := api.New(local.APIPort, local.APISecret)
	if err := client.WaitReady(5 * time.Second); err != nil {
		t.Fatal(err)
	}
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "ok") }))
	defer site.Close()
	loads := func(username, password string) bool {
		response, err := probe.Client(local.SpeedPort, username, password, 5*time.Second).Get(site.URL)
		if err != nil {
			return false
		}
		response.Body.Close()
		return response.StatusCode == http.StatusOK
	}

	// The proxy selector chose the dead protocol, and the speed test still
	// goes through the one its own selector chose.
	if err := client.Select(SpeedTestTag, "good"); err != nil {
		t.Fatal(err)
	}
	if !loads("sbc", "pw") {
		t.Error("the speed test did not load the page through its choice")
	}
	if loads("", "") {
		t.Error("the speed test let a request in without the password")
	}
	if err := client.Select(SpeedTestTag, "dead"); err != nil {
		t.Fatal(err)
	}
	if loads("sbc", "pw") {
		t.Error("the speed test loaded the page through a dead protocol")
	}
	if now, _, _ := client.Selected("proxy"); now != "dead" {
		t.Errorf("the proxy selector moved to %s", now)
	}
}
