package cli

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
	"github.com/xiaosq2000/sing-box-manager/internal/docker"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/service"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

// fakeAPI answers the Clash API calls sbc makes, as sing-box does.
type fakeAPI struct {
	mode     string
	protocol string
	// protocols are the selector's options, trojan and naive unless set.
	protocols []string
	// delays holds what the delay test measures through each outbound, one
	// answer per try, and the last answer repeats. A negative answer fails
	// that try, and an outbound without delays fails every try, as when its
	// server does not answer.
	delays map[string][]int
	// unreachable makes every delay test fail.
	unreachable bool
	// speedTests lists the protocols sbc pointed the speed test's selector
	// at, the last one being its choice now, and noSpeedTest leaves that
	// selector out, as sing-box running an older config does.
	speedTests  []string
	noSpeedTest bool
	lock        sync.Mutex
	tries       map[string]int
}

// speedTest returns the speed test selector's choice.
func (f *fakeAPI) speedTest() string {
	f.lock.Lock()
	defer f.lock.Unlock()
	if len(f.speedTests) == 0 {
		return ""
	}
	return f.speedTests[len(f.speedTests)-1]
}

func (f *fakeAPI) options() []string {
	if f.protocols == nil {
		return []string{"trojan", "naive"}
	}
	return f.protocols
}

func (f *fakeAPI) nextDelay(name string) (int, bool) {
	f.lock.Lock()
	defer f.lock.Unlock()
	answers := f.delays[name]
	if len(answers) == 0 || f.unreachable {
		return 0, false
	}
	if f.tries == nil {
		f.tries = map[string]int{}
	}
	try := min(f.tries[name], len(answers)-1)
	f.tries[name]++
	return answers[try], answers[try] >= 0
}

func (f *fakeAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Authorization") != "Bearer s3cret" {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	switch {
	case r.URL.Path == "/version":
		json.NewEncoder(w).Encode(map[string]any{"version": "sing-box 1.14.2"})
	case r.URL.Path == "/configs" && r.Method == http.MethodGet:
		json.NewEncoder(w).Encode(map[string]any{"mode": f.mode, "mode-list": []string{"ai", "china", "gfw", "global"}})
	case r.URL.Path == "/configs" && r.Method == http.MethodPatch:
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		f.mode = body["mode"]
		w.WriteHeader(http.StatusNoContent)
	case r.URL.Path == "/proxies/proxy" && r.Method == http.MethodGet:
		json.NewEncoder(w).Encode(map[string]any{"now": f.protocol, "all": f.options()})
	case r.URL.Path == "/proxies/proxy" && r.Method == http.MethodPut:
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		f.protocol = body["name"]
		w.WriteHeader(http.StatusNoContent)
	case r.URL.Path == "/proxies/speed-test" && !f.noSpeedTest && r.Method == http.MethodGet:
		json.NewEncoder(w).Encode(map[string]any{"now": f.speedTest(), "all": f.options()})
	case r.URL.Path == "/proxies/speed-test" && !f.noSpeedTest && r.Method == http.MethodPut:
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		f.lock.Lock()
		f.speedTests = append(f.speedTests, body["name"])
		f.lock.Unlock()
		w.WriteHeader(http.StatusNoContent)
	case strings.HasPrefix(r.URL.Path, "/proxies/") && strings.HasSuffix(r.URL.Path, "/delay"):
		delay, ok := f.nextDelay(strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/proxies/"), "/delay"))
		if !ok {
			w.WriteHeader(http.StatusServiceUnavailable)
			json.NewEncoder(w).Encode(map[string]any{"message": "An error occurred in the delay test"})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"delay": delay})
	default:
		http.NotFound(w, r)
	}
}

type fakeService struct {
	active bool
	calls  []string
}

func (f *fakeService) Install() error { f.calls = append(f.calls, "install"); return nil }
func (f *fakeService) Start() error   { f.calls = append(f.calls, "start"); f.active = true; return nil }
func (f *fakeService) Stop() error    { f.active = false; return nil }
func (f *fakeService) Restart() error { f.calls = append(f.calls, "restart"); return nil }
func (f *fakeService) Active() bool   { return f.active }
func (f *fakeService) Remove() error  { f.calls = append(f.calls, "remove"); return nil }

type harness struct {
	env    Env
	out    *bytes.Buffer
	errOut *bytes.Buffer
	api    *fakeAPI
	svc    *fakeService
	layout paths.Layout
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	root := t.TempDir()
	h := &harness{
		out:    &bytes.Buffer{},
		errOut: &bytes.Buffer{},
		api:    &fakeAPI{mode: "china", protocol: "trojan", delays: map[string][]int{"proxy": {87}}},
		svc:    &fakeService{active: true},
		layout: paths.Layout{Config: filepath.Join(root, "config"), Data: filepath.Join(root, "data")},
	}
	server := httptest.NewServer(h.api)
	t.Cleanup(server.Close)
	address, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(address.Port())
	config, err := singbox.Patch([]byte(`{"inbounds":[{"tag":"mixed"}],"outbounds":[{"type":"selector","tag":"proxy","outbounds":["trojan","naive"]}],"experimental":{"clash_api":{}}}`),
		singbox.Local{ListenPort: 2080, APIPort: port, APISecret: "s3cret"})
	if err != nil {
		t.Fatal(err)
	}
	paths.WriteFile(h.layout.ConfigFile(), config, 0o600)
	paths.WriteFile(h.layout.SingBox(), []byte("#!/bin/sh\nexit 0\n"), 0o755)
	h.env = DefaultEnv("1.2.3", strings.NewReader(""), h.out, h.errOut)
	h.env.Layout = func() (paths.Layout, error) { return h.layout, nil }
	h.env.Service = func(string) (service.Manager, error) { return h.svc, nil }
	home := filepath.Join(root, "home")
	os.MkdirAll(home, 0o755)
	h.env.Home = func() (string, error) { return home, nil }
	// Tests never read or change this machine's desktop or Docker settings.
	h.env.Desktop = func() (desktop.Desktop, error) { return nil, desktop.ErrUnsupported }
	h.env.Docker = func() (*docker.Daemon, error) { return nil, docker.ErrUnsupported }
	return h
}

// writeLocal replaces the harness config's local settings.
func (h *harness) writeLocal(t *testing.T, change func(*singbox.Local)) {
	t.Helper()
	config, _ := os.ReadFile(h.layout.ConfigFile())
	local, err := singbox.ReadLocal(config)
	if err != nil {
		t.Fatal(err)
	}
	change(&local)
	patched, err := singbox.Patch(config, local)
	if err != nil {
		t.Fatal(err)
	}
	paths.WriteFile(h.layout.ConfigFile(), patched, 0o600)
}

// fakeDesktop keeps one proxy setting, as GNOME or macOS would.
type fakeDesktop struct {
	state desktop.State
	port  int
	calls []string
}

func (f *fakeDesktop) Name() string { return "GNOME" }

func (f *fakeDesktop) State(port int) (desktop.State, error) {
	if f.state == desktop.On && f.port != port {
		return desktop.Other, nil
	}
	return f.state, nil
}

func (f *fakeDesktop) On(port int) error {
	f.state, f.port = desktop.On, port
	f.calls = append(f.calls, "on "+strconv.Itoa(port))
	return nil
}

func (f *fakeDesktop) Off() error {
	f.state = desktop.Off
	f.calls = append(f.calls, "off")
	return nil
}

func (h *harness) useDesktop() *fakeDesktop {
	desk := &fakeDesktop{}
	h.env.Desktop = func() (desktop.Desktop, error) { return desk, nil }
	return desk
}

// useDocker gives the harness a daemon whose drop-in lives in a test directory
// and whose sudo commands are logged.
func (h *harness) useDocker(t *testing.T) (*docker.Daemon, *[]string) {
	t.Helper()
	var calls []string
	daemon := &docker.Daemon{
		Path: filepath.Join(t.TempDir(), "http-proxy.conf"),
		Run: func(input, name string, args ...string) error {
			calls = append(calls, strings.Join(args, " "))
			if len(args) > 1 && args[0] == "tee" {
				return os.WriteFile(args[1], []byte(input), 0o644)
			}
			if len(args) > 2 && args[0] == "rm" {
				return os.Remove(args[2])
			}
			return nil
		},
	}
	h.env.Docker = func() (*docker.Daemon, error) { return daemon, nil }
	return daemon, &calls
}

func TestRouteAndProtocolShowAndSwitch(t *testing.T) {
	h := newHarness(t)

	if code := Run(h.env, []string{"route"}); code != 0 || h.out.String() != "china (choices: ai, china, gfw, global)\n" {
		t.Fatalf("route: code %d, %q", code, h.out.String())
	}
	if code := Run(h.env, []string{"route", "gfw"}); code != 0 || h.api.mode != "gfw" {
		t.Fatalf("switch route: code %d, mode %q, %s", code, h.api.mode, h.errOut)
	}
	if code := Run(h.env, []string{"protocol", "naive"}); code != 0 || h.api.protocol != "naive" {
		t.Fatalf("switch protocol: code %d, protocol %q, %s", code, h.api.protocol, h.errOut)
	}
	h.errOut.Reset()
	if code := Run(h.env, []string{"route", "cn"}); code != 1 || !strings.Contains(h.errOut.String(), `unknown route "cn"`) {
		t.Fatalf("unknown route: code %d, %s", code, h.errOut)
	}
}

func TestStatusShowsServiceRouteProtocolAndPort(t *testing.T) {
	h := newHarness(t)

	Run(h.env, []string{"status"})

	want := "service:  running\nproxy:    127.0.0.1:2080\nshells:   off\nroute:    china\nprotocol: trojan\n"
	if h.out.String() != want {
		t.Errorf("got\n%s", h.out.String())
	}
}

func TestUninstallAsksFirstAndThenRemovesEverything(t *testing.T) {
	h := newHarness(t)
	h.env.Stdin = strings.NewReader("n\n")

	if code := Run(h.env, []string{"uninstall"}); code != 1 {
		t.Fatalf("declined: code %d", code)
	}
	if _, err := os.Stat(h.layout.ConfigFile()); err != nil {
		t.Fatal("a declined uninstall removed the config")
	}

	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 0 {
		t.Fatalf("code %d, %s", code, h.errOut)
	}
	if _, err := os.Stat(h.layout.Config); !os.IsNotExist(err) {
		t.Error("the config directory is still there")
	}
	if strings.Join(h.svc.calls, ",") != "remove" {
		t.Errorf("service calls %v", h.svc.calls)
	}
}

func TestInstallServiceCopiesSbcAndStartsIt(t *testing.T) {
	h := newHarness(t)
	self := filepath.Join(t.TempDir(), "sbc")
	os.WriteFile(self, []byte("sbc binary"), 0o755)
	h.env.Self = func() (string, error) { return self, nil }

	if err := installService(h.env, h.layout); err != nil {
		t.Fatal(err)
	}

	copied, err := os.ReadFile(h.layout.SBC())
	if err != nil || string(copied) != "sbc binary" {
		t.Fatalf("copy: %q, %v", copied, err)
	}
	if strings.Join(h.svc.calls, ",") != "install,start,restart" {
		t.Errorf("a reinstall over a running service: calls %v", h.svc.calls)
	}

	h.svc.active, h.svc.calls = false, nil
	if err := installService(h.env, h.layout); err != nil {
		t.Fatal(err)
	}
	if strings.Join(h.svc.calls, ",") != "install,start" {
		t.Errorf("a first install: calls %v", h.svc.calls)
	}
}

func TestCommandsBeforeInstallSayToInstall(t *testing.T) {
	h := newHarness(t)
	os.Remove(h.layout.ConfigFile())

	for _, command := range []string{"status", "route", "protocol", "update"} {
		h.errOut.Reset()
		if code := Run(h.env, []string{command}); code != 1 || !strings.Contains(h.errOut.String(), "run 'sbc install'") {
			t.Errorf("%s: code %d, %q", command, code, h.errOut.String())
		}
	}
}

func TestEnvPrintsLinesForTheConfiguredPort(t *testing.T) {
	h := newHarness(t)

	Run(h.env, []string{"env", "on"})
	if !strings.Contains(h.out.String(), "export http_proxy='http://127.0.0.1:2080'\n") {
		t.Errorf("env on:\n%s", h.out)
	}
	h.out.Reset()
	Run(h.env, []string{"env", "off"})
	if !strings.HasPrefix(h.out.String(), "unset http_proxy HTTP_PROXY") {
		t.Errorf("env off: %q", h.out)
	}
	h.out.Reset()
	if code := Run(h.env, []string{"env"}); code != 0 || !strings.Contains(h.out.String(), "export https_proxy=") {
		t.Errorf("env alone, for scripts: code %d, %q", code, h.out)
	}
	if code := Run(h.env, []string{"env", "maybe"}); code != 2 {
		t.Errorf("env maybe: code %d", code)
	}
}

func TestOnStartsWhatShellsNeedAndOffLeavesSingBoxRunning(t *testing.T) {
	h := newHarness(t)
	h.svc.active = false

	if code := Run(h.env, []string{"on"}); code != 0 {
		t.Fatalf("on: %s", h.errOut)
	}
	env, err := os.ReadFile(h.layout.EnvFile())
	if err != nil || !strings.Contains(string(env), "127.0.0.1:2080") || !h.svc.active {
		t.Fatalf("after on: env %q, active %v", env, h.svc.active)
	}
	if code := Run(h.env, []string{"off"}); code != 0 {
		t.Fatalf("off: %s", h.errOut)
	}
	if _, err := os.Stat(h.layout.EnvFile()); !os.IsNotExist(err) {
		t.Errorf("after off: env file %v", err)
	}
	if !h.svc.active {
		t.Error("off stopped sing-box, which cuts an agent's own connection")
	}
}

func TestStartStopAndRestartControlTheService(t *testing.T) {
	h := newHarness(t)

	for _, action := range []string{"stop", "start", "restart"} {
		if code := Run(h.env, []string{action}); code != 0 {
			t.Fatalf("%s: %s", action, h.errOut)
		}
	}
	if strings.Join(h.svc.calls, ",") != "start,restart" || !h.svc.active {
		t.Errorf("calls %v, active %v", h.svc.calls, h.svc.active)
	}
}

func TestInitPrintsTheShellCodeForTheLayout(t *testing.T) {
	h := newHarness(t)

	if code := Run(h.env, []string{"init", "zsh"}); code != 0 {
		t.Fatalf("init: %s", h.errOut)
	}
	if !strings.Contains(h.out.String(), h.layout.CLIDir()) || !strings.Contains(h.out.String(), "add-zsh-hook precmd _sbc_apply") {
		t.Errorf("init printed:\n%s", h.out)
	}
	if code := Run(h.env, []string{"init"}); code != 2 {
		t.Errorf("init without a shell: code %d", code)
	}
}

func TestLinkShowsAMaskedLinkAndSetReplacesIt(t *testing.T) {
	h := newHarness(t)
	paths.WriteFile(h.layout.Link(), []byte("https://vpn.example.com/sub/abcdefghijklmnopqrstuv\n"), 0o600)

	if code := Run(h.env, []string{"link"}); code != 0 || h.out.String() != "https://vpn.example.com/sub/abcd…stuv\n" {
		t.Fatalf("link: code %d, %q", code, h.out.String())
	}

	h.env.Stdin = strings.NewReader("https://other.example.com/sub/zyxwvutsrqponmlkjihgfe\n")
	Run(h.env, []string{"link", "set"})
	saved, _ := os.ReadFile(h.layout.Link())
	if string(saved) != "https://other.example.com/sub/zyxwvutsrqponmlkjihgfe\n" {
		t.Errorf("saved %q", saved)
	}

	h.env.Stdin = strings.NewReader("\ufeffhttps://third.example.com/sub/zyxwvutsrqponmlkjihgfe\n")
	Run(h.env, []string{"link", "set"})
	saved, _ = os.ReadFile(h.layout.Link())
	if string(saved) != "https://third.example.com/sub/zyxwvutsrqponmlkjihgfe\n" {
		t.Errorf("saved with BOM %q", saved)
	}
}

func TestPortMovesTheProxyAndNewShellsFollow(t *testing.T) {
	h := newHarness(t)
	h.svc.active = false
	Run(h.env, []string{"on"})
	port, err := singbox.FreePort(0)
	if err != nil {
		t.Fatal(err)
	}

	if code := Run(h.env, []string{"port", strconv.Itoa(port)}); code != 0 {
		t.Fatalf("port: %s", h.errOut)
	}
	config, _ := os.ReadFile(h.layout.ConfigFile())
	local, _ := singbox.ReadLocal(config)
	env, _ := os.ReadFile(h.layout.EnvFile())
	if local.ListenPort != port || !strings.Contains(string(env), ":"+strconv.Itoa(port)+"'") {
		t.Errorf("port %d, env %q", local.ListenPort, env)
	}
	if local.APISecret != "s3cret" {
		t.Error("changing the port lost the API secret")
	}
	h.errOut.Reset()
	if code := Run(h.env, []string{"port", "0"}); code != 1 || !strings.Contains(h.errOut.String(), "is not a port") {
		t.Errorf("port 0: code %d, %s", code, h.errOut)
	}
}

func TestInstallAddsOneRCBlockThatRunsInit(t *testing.T) {
	h := newHarness(t)
	home, _ := h.env.Home()

	for range 2 {
		if err := setUpShell(h.env, h.layout, installOptions{shell: "zsh"}); err != nil {
			t.Fatal(err)
		}
	}
	rc, _ := os.ReadFile(filepath.Join(home, ".zshrc"))
	if strings.Count(string(rc), "init zsh") != 1 || !strings.Contains(string(rc), h.layout.SBC()) {
		t.Errorf(".zshrc:\n%s", rc)
	}
	if _, err := os.Stat(h.layout.EnvFile()); err != nil {
		t.Error("shells do not get the proxy after install")
	}

	Run(h.env, []string{"uninstall", "--yes"})
	rc, _ = os.ReadFile(filepath.Join(home, ".zshrc"))
	if strings.Contains(string(rc), "sbc") {
		t.Errorf(".zshrc after uninstall:\n%s", rc)
	}
}

func TestInstallFlags(t *testing.T) {
	options, err := parseInstallFlags([]string{"--shell", "zsh", "--no-rc", "--auth", "on", "--route", "gfw", "--protocol", "naive"})
	if err != nil || options != (installOptions{shell: "zsh", noRC: true, auth: install.AuthOn, route: "gfw", protocol: "naive"}) {
		t.Errorf("got %+v, %v", options, err)
	}
	if _, err := parseInstallFlags([]string{"--auth", "maybe"}); err == nil {
		t.Error("--auth maybe was accepted")
	}
	if _, err := parseInstallFlags([]string{"--shell", "fish"}); err == nil {
		t.Error("fish was accepted")
	}
	if _, err := parseInstallFlags([]string{"https://vpn.example.com/sub/abcdefghijklmnopqrstuv"}); err == nil {
		t.Error("a link as an argument was accepted; it belongs on standard input")
	}
}

func TestInstallCheckAppliesTheChoicesAndLoadsAPage(t *testing.T) {
	h := newHarness(t)

	if err := checkProxy(h.env, h.layout, installOptions{route: "gfw", protocol: "naive"}); err != nil {
		t.Fatal(err)
	}
	if h.api.mode != "gfw" || h.api.protocol != "naive" {
		t.Errorf("mode %q, protocol %q", h.api.mode, h.api.protocol)
	}
	if !strings.Contains(h.out.String(), "Route gfw, protocol naive: a page loads through the proxy in 87 ms.") {
		t.Errorf("stdout %q", h.out)
	}
}

func TestInstallCheckKeepsTheDefaultForAChoiceTheSubscriptionLacks(t *testing.T) {
	h := newHarness(t)

	if err := checkProxy(h.env, h.layout, installOptions{protocol: "vless"}); err != nil {
		t.Fatal(err)
	}
	if h.api.protocol != "trojan" || !strings.Contains(h.errOut.String(), `unknown protocol "vless"`) || !strings.Contains(h.errOut.String(), "keeping the default protocol") {
		t.Errorf("protocol %q, stderr %q", h.api.protocol, h.errOut)
	}
}

func TestInstallCheckFailsWhenNoPageLoads(t *testing.T) {
	h := newHarness(t)
	h.api.unreachable = true

	err := checkProxy(h.env, h.layout, installOptions{})
	if err == nil || !strings.Contains(err.Error(), "no page loads through trojan") || !strings.Contains(err.Error(), "sbc protocol <name>") {
		t.Errorf("got %v", err)
	}
}

func TestDesktopOnMakesOnAndOffSwitchTheDesktopToo(t *testing.T) {
	h := newHarness(t)
	desk := h.useDesktop()

	if code := Run(h.env, []string{"desktop", "on"}); code != 0 {
		t.Fatalf("desktop on: %s", h.errOut)
	}
	Run(h.env, []string{"off"})
	Run(h.env, []string{"on"})
	if code := Run(h.env, []string{"desktop", "off"}); code != 0 {
		t.Fatalf("desktop off: %s", h.errOut)
	}
	Run(h.env, []string{"on"})

	if got := strings.Join(desk.calls, ","); got != "on 2080,off,on 2080,off" {
		t.Errorf("desktop calls %s", got)
	}
	if _, err := os.Stat(h.layout.DesktopFile()); !os.IsNotExist(err) {
		t.Error("'sbc desktop off' left the desktop following 'sbc on'")
	}
}

func TestDesktopLeavesAnotherProxyAlone(t *testing.T) {
	h := newHarness(t)
	desk := h.useDesktop()
	desk.state = desktop.Other
	paths.WriteFile(h.layout.DesktopFile(), nil, 0o600)

	Run(h.env, []string{"off"})
	Run(h.env, []string{"desktop", "off"})

	if len(desk.calls) != 0 || !strings.Contains(h.out.String(), "points at another proxy, so sbc left it alone") {
		t.Errorf("calls %v, output %q", desk.calls, h.out)
	}
}

func TestADesktopCannotUseAProxyWithAPassword(t *testing.T) {
	h := newHarness(t)
	desk := h.useDesktop()
	h.writeLocal(t, func(local *singbox.Local) { local.Username, local.Password = "sbc", "pw" })

	if code := Run(h.env, []string{"desktop", "on"}); code != 1 || !strings.Contains(h.errOut.String(), "cannot hold") {
		t.Errorf("code %d, %s", code, h.errOut)
	}
	if len(desk.calls) != 0 {
		t.Errorf("calls %v", desk.calls)
	}
}

func TestDockerOnAndOffAskBeforeRestartingDocker(t *testing.T) {
	h := newHarness(t)
	daemon, calls := h.useDocker(t)

	if code := Run(h.env, []string{"docker", "on"}); code != 1 || !strings.Contains(h.errOut.String(), "--yes") || len(*calls) != 0 {
		t.Fatalf("without --yes: code %d, calls %v, %s", code, *calls, h.errOut)
	}
	if code := Run(h.env, []string{"docker", "on", "--yes"}); code != 0 {
		t.Fatalf("docker on: %s", h.errOut)
	}
	if content, _ := os.ReadFile(daemon.Path); string(content) != docker.Content(2080) {
		t.Errorf("drop-in %q", content)
	}
	h.out.Reset()
	Run(h.env, []string{"docker"})
	if h.out.String() != "on\n" {
		t.Errorf("docker: %q", h.out)
	}
	if code := Run(h.env, []string{"docker", "off", "--yes"}); code != 0 {
		t.Fatalf("docker off: %s", h.errOut)
	}
	want := "mkdir -p " + filepath.Dir(daemon.Path) + ",tee " + daemon.Path + ",systemctl daemon-reload,systemctl restart docker," +
		"rm -f " + daemon.Path + ",systemctl daemon-reload,systemctl restart docker"
	if got := strings.Join(*calls, ","); got != want {
		t.Errorf("calls %s", got)
	}
}

func TestDockerLeavesSomeoneElsesDropInAndAPasswordAlone(t *testing.T) {
	h := newHarness(t)
	daemon, calls := h.useDocker(t)
	os.WriteFile(daemon.Path, []byte("[Service]\nEnvironment=\"HTTPS_PROXY=http://proxy.corp:3128\"\n"), 0o644)
	if code := Run(h.env, []string{"docker", "on", "--yes"}); code != 1 || !strings.Contains(h.errOut.String(), "sbc did not write") {
		t.Errorf("someone else's drop-in: code %d, %s", code, h.errOut)
	}

	os.Remove(daemon.Path)
	h.writeLocal(t, func(local *singbox.Local) { local.Username, local.Password = "sbc", "pw" })
	h.errOut.Reset()
	if code := Run(h.env, []string{"docker", "on", "--yes"}); code != 1 || !strings.Contains(h.errOut.String(), "keeps the daemon off the proxy") {
		t.Errorf("a password: code %d, %s", code, h.errOut)
	}
	if len(*calls) != 0 {
		t.Errorf("calls %v", *calls)
	}
}

func TestPortMovesTheDesktopAndPointsOutDocker(t *testing.T) {
	h := newHarness(t)
	desk := h.useDesktop()
	desk.state, desk.port = desktop.On, 2080
	daemon, _ := h.useDocker(t)
	os.WriteFile(daemon.Path, []byte(docker.Content(2080)), 0o644)
	port, err := singbox.FreePort(0)
	if err != nil {
		t.Fatal(err)
	}

	if code := Run(h.env, []string{"port", strconv.Itoa(port)}); code != 0 {
		t.Fatalf("port: %s", h.errOut)
	}
	if desk.port != port {
		t.Errorf("the desktop stayed on %d", desk.port)
	}
	if !strings.Contains(h.out.String(), "The Docker daemon still uses port 2080") {
		t.Errorf("output %q", h.out)
	}
}

func TestStatusShowsTheDesktopDockerAndAPassword(t *testing.T) {
	h := newHarness(t)
	h.useDesktop()
	h.useDocker(t)
	h.writeLocal(t, func(local *singbox.Local) { local.Username, local.Password = "sbc", "pw" })

	Run(h.env, []string{"status"})

	for _, want := range []string{"proxy:    127.0.0.1:2080, with a password\n", "desktop:  off (GNOME)\n", "docker:   off\n"} {
		if !strings.Contains(h.out.String(), want) {
			t.Errorf("missing %q in\n%s", want, h.out)
		}
	}
}

func TestUninstallWaitsForDockerAndTurnsTheDesktopOff(t *testing.T) {
	h := newHarness(t)
	desk := h.useDesktop()
	desk.state, desk.port = desktop.On, 2080
	daemon, _ := h.useDocker(t)
	os.WriteFile(daemon.Path, []byte(docker.Content(2080)), 0o644)

	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 1 || !strings.Contains(h.errOut.String(), "sbc docker off") {
		t.Fatalf("with Docker on: code %d, %s", code, h.errOut)
	}
	if _, err := os.Stat(h.layout.ConfigFile()); err != nil {
		t.Fatal("the refused uninstall removed the config")
	}

	os.Remove(daemon.Path)
	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 0 {
		t.Fatalf("uninstall: %s", h.errOut)
	}
	if strings.Join(desk.calls, ",") != "off" {
		t.Errorf("desktop calls %v", desk.calls)
	}
}

func TestInstallNotesPointOutTheDesktopAndAStrandedDockerDaemon(t *testing.T) {
	h := newHarness(t)
	h.useDesktop()
	daemon, _ := h.useDocker(t)
	// The bash client's drop-in, for the port it used.
	os.WriteFile(daemon.Path, []byte(docker.Content(1085)), 0o644)

	installNotes(h.env, &install.Result{ListenPort: 2080})
	if !strings.Contains(h.out.String(), "sbc desktop on") || h.errOut.Len() != 0 {
		t.Errorf("without a password: stdout %q, stderr %q", h.out, h.errOut)
	}

	h.out.Reset()
	installNotes(h.env, &install.Result{ListenPort: 2080, Auth: true})
	if strings.Contains(h.out.String(), "sbc desktop on") || !strings.Contains(h.errOut.String(), "image pulls through it fail") {
		t.Errorf("with a password: stdout %q, stderr %q", h.out, h.errOut)
	}
}

func TestOffWithoutADesktopSessionStillTurnsShellsOff(t *testing.T) {
	h := newHarness(t)
	Run(h.env, []string{"on"})
	paths.WriteFile(h.layout.DesktopFile(), nil, 0o600)

	if code := Run(h.env, []string{"off"}); code != 0 {
		t.Fatalf("code %d, %s", code, h.errOut)
	}
	if _, err := os.Stat(h.layout.EnvFile()); !os.IsNotExist(err) {
		t.Error("shells still use the proxy")
	}
	if !strings.Contains(h.errOut.String(), "the desktop proxy stays as it was") {
		t.Errorf("stderr %q", h.errOut)
	}
}

func TestInstallServiceSaysWhenWindowsTasksRunInTheSession(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "windows"
	h.env.Self = func() (string, error) { return h.layout.SBC(), nil }
	s4u := true
	tasks := &service.Tasks{
		Dir:  t.TempDir(),
		SBC:  h.layout.SBC(),
		User: "S-1-5-21-1-2-3-1001",
		// The S4U registration is refused, so the definitions are rewritten
		// for the session and registered again.
		Run: func(name string, args ...string) error {
			if args[0] == "/Create" && s4u {
				s4u = false
				return errors.New("access denied")
			}
			return nil
		},
		Read: func(string, ...string) (string, error) { return "Ready", nil },
	}
	h.env.Service = func(string) (service.Manager, error) { return tasks, nil }

	if err := installService(h.env, h.layout); err != nil {
		t.Fatal(err)
	}

	if !tasks.Interactive || !strings.Contains(h.errOut.String(), "a console window stays open") {
		t.Errorf("interactive %v, stderr %q", tasks.Interactive, h.errOut.String())
	}
}
