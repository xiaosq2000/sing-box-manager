package cli

import (
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
)

func TestPrivacyWebRTCCommandsNeedNoDesktopOrConfig(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	privacy := h.useDesktop()
	h.env.Desktop = func() (desktop.Desktop, error) {
		t.Fatal("browser protection inspected desktop settings")
		return nil, nil
	}
	os.Remove(h.layout.ConfigFile())
	for _, action := range []string{"on", "off"} {
		if code := Run(h.env, []string{"privacy", "browser", "webrtc", action}); code != 0 {
			t.Fatal(h.errOut)
		}
	}
	if len(privacy.privacyCalls) != 2 || !privacy.privacyCalls[0] || privacy.privacyCalls[1] {
		t.Fatalf("commands: %v", privacy.privacyCalls)
	}
	if !strings.Contains(h.out.String(), "Restart Chrome") {
		t.Fatal("missing restart instruction")
	}
}

func TestPrivacyBrowserOffReportsOnlyPreservedForeignSettings(t *testing.T) {
	for _, foreign := range []bool{false, true} {
		h := newHarness(t)
		h.env.OS = "linux"
		privacy := h.useDesktop()
		if foreign {
			privacy.remainingPolicies = []string{"/etc/opt/chrome/policies/managed/manual.json"}
		}
		if code := Run(h.env, []string{"privacy", "browser", "webrtc", "off"}); code != 0 {
			t.Fatal(h.errOut)
		}
		output := h.out.String()
		if !strings.Contains(output, "No owned Chrome policy remains") || strings.Contains(output, "Firefox") {
			t.Fatalf("off output: %s", output)
		}
		if strings.Contains(output, "from another source were preserved") != foreign {
			t.Fatalf("foreign setting report: %s", output)
		}
	}
}

func TestPrivacyBrowserOffFailureDoesNotClaimCleanupSuccess(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	privacy := h.useDesktop()
	privacy.privacyErr = errors.New("close Chrome and retry")
	if code := Run(h.env, []string{"privacy", "browser", "webrtc", "off"}); code == 0 {
		t.Fatal("cleanup failure reported success")
	}
	if strings.Contains(h.out.String(), "policy was removed") || !strings.Contains(h.errOut.String(), "close Chrome and retry") {
		t.Fatalf("cleanup failure output: %s, %s", h.out, h.errOut)
	}
}

func TestPrivacyBrowserIsLinuxOnly(t *testing.T) {
	for _, goos := range []string{"darwin", "windows"} {
		t.Run(goos, func(t *testing.T) {
			h := newHarness(t)
			h.env.OS = goos
			h.env.Privacy = func() (desktop.BrowserPrivacy, error) {
				t.Fatal("unsupported platform inspected browser settings")
				return nil, nil
			}
			if code := Run(h.env, []string{"privacy", "browser", "on"}); code != 1 {
				t.Fatalf("code %d", code)
			}
			if !strings.Contains(h.errOut.String(), "Linux only") {
				t.Fatalf("stderr %q", h.errOut.String())
			}
		})
	}
}

func TestPrivacyBrowserStatusReadsTheSavedChoice(t *testing.T) {
	for _, optedOut := range []bool{false, true} {
		h := newHarness(t)
		h.env.OS = "linux"
		privacy := h.useDesktop()
		privacy.privacyOptedOut = optedOut
		if code := Run(h.env, []string{"privacy", "browser"}); code != 0 {
			t.Fatal(h.errOut)
		}
		output := strings.Join(strings.Fields(h.out.String()), " ")
		want := "Browser protection: on (automatic repair)"
		if optedOut {
			want = "Browser protection: off (opted out)"
		}
		if !strings.Contains(output, want) || !strings.Contains(output, "Only Google Chrome on Linux is managed") {
			t.Fatalf("stdout %q", output)
		}
	}
}

func TestPrivacySummaryShowsBothFeatures(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	h.useDesktop()
	if code := Run(h.env, []string{"privacy"}); code != 0 {
		t.Fatal(h.errOut)
	}
	output := strings.Join(strings.Fields(h.out.String()), " ")
	for _, want := range []string{"Browser protection: on", "Region matching:", "Run 'sbc privacy browser' or 'sbc privacy region'"} {
		if !strings.Contains(output, want) {
			t.Fatalf("stdout %q does not contain %q", output, want)
		}
	}
}

func TestWebRTCCommandIsRemoved(t *testing.T) {
	h := newHarness(t)
	if code := Run(h.env, []string{"webrtc", "on"}); code != 2 {
		t.Fatalf("code %d", code)
	}
	if !strings.Contains(h.errOut.String(), `unknown command "webrtc"`) {
		t.Fatalf("stderr %q", h.errOut.String())
	}
}

func TestUnixUninstallCleansPrivacyWithoutDesktop(t *testing.T) {
	h := newHarness(t)
	h.env.OS = "linux"
	privacy := h.useDesktop()
	h.env.Desktop = func() (desktop.Desktop, error) { return nil, desktop.ErrUnsupported }
	privacy.privacyErr = errors.New("cleanup denied")
	if code := Run(h.env, []string{"uninstall", "--yes"}); code == 0 {
		t.Fatal("failed cleanup allowed uninstall")
	}
	if _, err := os.Stat(h.layout.ConfigFile()); err != nil {
		t.Fatal("cleanup failure removed recovery state")
	}
	if !h.svc.active {
		t.Fatal("cleanup failure stopped proxy")
	}
	privacy.privacyErr = nil
	if code := Run(h.env, []string{"uninstall", "--yes"}); code != 0 {
		t.Fatal(h.errOut)
	}
	if privacy.privacyCleanups != 2 {
		t.Fatal("uninstall depended on desktop")
	}
}
