package cli

import (
	"errors"
	"os"
	"strconv"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

type testRegistry struct {
	values  map[string]winsettings.Value
	failure error
}

func (r *testRegistry) Read(_ string, _ []string) (map[string]winsettings.Value, error) {
	values := map[string]winsettings.Value{}
	for name, value := range r.values {
		values[name] = value
	}
	return values, nil
}
func (r *testRegistry) Apply(_ string, changes map[string]winsettings.Value, _ string) error {
	if r.failure != nil {
		return r.failure
	}
	for name, value := range changes {
		if value.Kind == "" {
			delete(r.values, name)
		} else {
			r.values[name] = value
		}
	}
	return nil
}
func windowsHarness(t *testing.T) (*harness, *testRegistry) {
	h := newHarness(t)
	h.env.OS = "windows"
	h.useDesktop()
	r := &testRegistry{values: map[string]winsettings.Value{}}
	h.env.Windows = &winsettings.Environment{Registry: r}
	return h, r
}
func TestWindowsOnOffAndUninstallManageOnlyOurUserSettings(t *testing.T) {
	h, r := windowsHarness(t)
	r.values["Path"] = winsettings.Value{Text: `C:\tools`, Kind: "ExpandString"}
	if err := h.env.Windows.Path(h.layout.CLIDir(), true); err != nil {
		t.Fatal(err)
	}
	for _, command := range [][]string{{"on"}, {"off"}, {"on"}, {"uninstall", "--yes"}} {
		if code := Run(h.env, command); code != 0 {
			t.Fatalf("%v: %s", command, h.errOut)
		}
		if command[0] == "on" && r.values["HTTPS_PROXY"].Text != "http://127.0.0.1:2080" {
			t.Fatal("on did not set environment")
		}
		if command[0] == "off" && (!h.svc.active || r.values["HTTPS_PROXY"].Text != "") {
			t.Fatal("off did not preserve the service and clear environment")
		}
	}
	if len(r.values) != 1 || r.values["Path"].Text != `C:\tools` {
		t.Fatalf("uninstall left settings: %+v", r.values)
	}
	if !strings.Contains(h.out.String(), "Open a new terminal from Start") {
		t.Fatal("missing restart hint")
	}
}
func TestWindowsOnFailureDoesNotClaimShellsAreEnabled(t *testing.T) {
	h, r := windowsHarness(t)
	r.failure = errors.New("access denied")
	if code := Run(h.env, []string{"on"}); code == 0 {
		t.Fatal("write failure succeeded")
	}
	if _, err := os.Stat(h.layout.EnvFile()); !os.IsNotExist(err) {
		t.Fatal("failed on wrote marker")
	}
}
func TestWindowsPortMovesEnvironmentAndRestoresConfigOnFailure(t *testing.T) {
	for _, fails := range []bool{false, true} {
		t.Run(strconv.FormatBool(fails), func(t *testing.T) {
			h, r := windowsHarness(t)
			if code := Run(h.env, []string{"on"}); code != 0 {
				t.Fatal(h.errOut)
			}
			h.svc.active = false
			port, err := singbox.FreePort(0)
			if err != nil {
				t.Fatal(err)
			}
			if fails {
				r.failure = errors.New("denied")
			}
			code := Run(h.env, []string{"port", strconv.Itoa(port)})
			if (code != 0) != fails {
				t.Fatalf("code %d: %s", code, h.errOut)
			}
			if fails {
				port = 2080
			}
			local, err := readLocal(h.layout)
			if err != nil {
				t.Fatal(err)
			}
			if local.ListenPort != port || r.values["HTTP_PROXY"].Text != "http://127.0.0.1:"+strconv.Itoa(port) {
				t.Fatal("config and environment disagree")
			}
		})
	}
}
func TestWindowsOnReappliesDesktopSettingsWhenAlreadyOn(t *testing.T) {
	h, _ := windowsHarness(t)
	desk := h.useDesktop()
	if code := Run(h.env, []string{"desktop", "on"}); code != 0 {
		t.Fatal(h.errOut)
	}
	desk.calls = nil
	if code := Run(h.env, []string{"on"}); code != 0 {
		t.Fatal(h.errOut)
	}
	if got := strings.Join(desk.calls, ","); got != "on 2080" {
		t.Fatalf("already-on desktop settings were not reapplied: %q", got)
	}

	// Reapplying is still opt-in and must preserve a foreign proxy or PAC.
	desk.calls = nil
	desk.state = desktop.Other
	Run(h.env, []string{"on"})
	if len(desk.calls) != 0 {
		t.Fatal("on changed a foreign proxy")
	}
	desk.state = desktop.On
	if err := os.Remove(h.layout.DesktopFile()); err != nil {
		t.Fatal(err)
	}
	Run(h.env, []string{"on"})
	if len(desk.calls) != 0 {
		t.Fatal("on changed desktop settings without opt-in")
	}
}

func TestWindowsOnReportsDesktopFailureWhenAlreadyOn(t *testing.T) {
	h, _ := windowsHarness(t)
	desk := h.useDesktop()
	if code := Run(h.env, []string{"desktop", "on"}); code != 0 {
		t.Fatal(h.errOut)
	}
	h.env.Desktop = func() (desktop.Desktop, error) { return failingDesktop{fakeDesktop: desk}, nil }
	if code := Run(h.env, []string{"on"}); code == 0 || !strings.Contains(h.errOut.String(), "desktop write denied") {
		t.Fatalf("already-on desktop failure reported success: code %d, %s", code, h.errOut)
	}
}

func TestWindowsDesktopRefusesForeignProxy(t *testing.T) {
	h, _ := windowsHarness(t)
	desk := h.useDesktop()
	desk.state = desktop.Other
	if code := Run(h.env, []string{"desktop", "on"}); code == 0 {
		t.Fatal("foreign proxy overwritten")
	}
	if len(desk.calls) != 0 {
		t.Fatal("foreign proxy changed")
	}
}
func TestWindowsEnvPrintsPowerShell(t *testing.T) {
	h, _ := windowsHarness(t)
	if code := Run(h.env, []string{"env"}); code != 0 || !strings.Contains(h.out.String(), "$env:HTTP_PROXY = '") {
		t.Fatalf("%d: %s", code, h.out)
	}
	h.out.Reset()
	if code := Run(h.env, []string{"env", "off"}); code != 0 || !strings.Contains(h.out.String(), "Remove-Item Env:HTTP_PROXY") {
		t.Fatalf("%d: %s", code, h.out)
	}
}

type failingDesktop struct {
	*fakeDesktop
	readErr error
}

func (d failingDesktop) State(port int) (desktop.State, error) {
	if d.readErr != nil {
		return desktop.Off, d.readErr
	}
	return d.fakeDesktop.State(port)
}
func (d failingDesktop) On(int) error { return errors.New("desktop write denied") }

func TestWindowsPortRestoresEnvironmentWhenDesktopWriteFails(t *testing.T) {
	h, r := windowsHarness(t)
	Run(h.env, []string{"on"})
	h.svc.active = false
	desk := failingDesktop{fakeDesktop: &fakeDesktop{state: desktop.On, port: 2080}}
	h.env.Desktop = func() (desktop.Desktop, error) { return desk, nil }
	port, err := singbox.FreePort(0)
	if err != nil {
		t.Fatal(err)
	}
	if code := Run(h.env, []string{"port", strconv.Itoa(port)}); code == 0 {
		t.Fatal("failed desktop write succeeded")
	}
	local, err := readLocal(h.layout)
	if err != nil || local.ListenPort != 2080 || r.values["HTTP_PROXY"].Text != "http://127.0.0.1:2080" {
		t.Fatal("port failure left inconsistent settings")
	}
}

func TestWindowsStatusReadsManualEnvironmentChanges(t *testing.T) {
	h, r := windowsHarness(t)
	Run(h.env, []string{"on"})
	r.values["HTTPS_PROXY"] = winsettings.Value{Text: "http://other:3128", Kind: "String"}
	h.out.Reset()
	if code := Run(h.env, []string{"status"}); code != 0 || !strings.Contains(h.out.String(), "shells:   set to another proxy") {
		t.Fatalf("%d: %s", code, h.out)
	}
}

func TestWindowsWebRTCExplicitSwitchDoesNotToggleProxy(t *testing.T) {
	h, _ := windowsHarness(t)
	desk := h.useDesktop()
	desk.state, desk.port = desktop.On, 2080
	if err := os.Remove(h.layout.ConfigFile()); err != nil {
		t.Fatal(err)
	}
	for _, action := range []string{"off", "on"} {
		if code := Run(h.env, []string{"webrtc", action}); code != 0 {
			t.Fatal(h.errOut)
		}
	}
	if len(desk.privacyCalls) != 2 || desk.privacyCalls[0] || !desk.privacyCalls[1] || len(desk.calls) != 0 {
		t.Fatalf("privacy commands changed proxy state: %+v", desk)
	}
	desk.privacyErr = errors.New("UAC canceled")
	if code := Run(h.env, []string{"webrtc", "off"}); code == 0 {
		t.Fatal("canceled reset succeeded")
	}
	for _, args := range [][]string{{"webrtc"}, {"webrtc", "reset"}, {"webrtc", "off", "extra"}} {
		if code := Run(h.env, args); code != 2 {
			t.Fatalf("invalid command %v: %d", args, code)
		}
	}

}

func TestWindowsUninstallCleansPrivacyEvenWhenDesktopIsOff(t *testing.T) {
	h, _ := windowsHarness(t)
	desk := h.useDesktop()
	desk.remainingPolicies = []string{`HKCU\Software\Policies\Google\Chrome\WebRtcIPHandling`}
	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 0 {
		t.Fatal(h.errOut)
	}
	if desk.privacyCleanups != 1 || len(desk.calls) != 0 {
		t.Fatalf("cleanup depended on the proxy being on: %+v", desk)
	}
	if !strings.Contains(h.out.String(), "not removed automatically") || !strings.Contains(h.out.String(), "manual WebRTC cleanup") {
		t.Fatal("uninstall concealed a pre-existing policy")
	}
	if _, err := os.Stat(h.layout.ConfigFile()); !os.IsNotExist(err) {
		t.Fatal("successful cleanup did not permit uninstall")
	}
}

func TestWindowsUninstallCancellationKeepsClientAndProxyForRetry(t *testing.T) {
	h, registry := windowsHarness(t)
	desk := h.useDesktop()
	if code := Run(h.env, []string{"desktop", "on"}); code != 0 {
		t.Fatal(h.errOut)
	}
	if code := Run(h.env, []string{"on"}); code != 0 {
		t.Fatal(h.errOut)
	}
	desk.calls = nil
	desk.privacyErr = errors.New("UAC canceled")
	if code := Run(h.env, []string{"uninstall", "--yes"}); code == 0 {
		t.Fatal("uninstall ignored browser cleanup failure")
	}
	if !h.svc.active || registry.values["HTTP_PROXY"].Text == "" || len(desk.calls) != 0 {
		t.Fatal("canceled cleanup changed the service or proxy")
	}
	if _, err := os.Stat(h.layout.ConfigFile()); err != nil {
		t.Fatal("canceled cleanup removed recovery state")
	}
	if !strings.Contains(h.errOut.String(), "sbc is still installed") {
		t.Fatal("canceled cleanup did not explain how to retry")
	}
	desk.privacyErr = nil
	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 0 {
		t.Fatal(h.errOut)
	}
}

func TestWindowsUninstallKeepsServiceWhenDesktopCannotBeRead(t *testing.T) {
	h, r := windowsHarness(t)
	Run(h.env, []string{"on"})
	h.env.Desktop = func() (desktop.Desktop, error) {
		return failingDesktop{fakeDesktop: &fakeDesktop{}, readErr: errors.New("denied")}, nil
	}
	if code := Run(h.env, []string{"uninstall", "--yes"}); code == 0 {
		t.Fatal("uninstall ignored desktop read error")
	}
	if !h.svc.active || r.values["HTTP_PROXY"].Text == "" {
		t.Fatal("uninstall changed settings after read failure")
	}
	if _, err := os.Stat(h.layout.ConfigFile()); err != nil {
		t.Fatal("uninstall removed config after read failure")
	}
}
