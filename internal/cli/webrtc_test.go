package cli

import (
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
)

func TestWebRTCCommandsWorkWithoutDesktopOrConfig(t *testing.T) {
	for _, goos := range []string{"linux", "darwin", "windows"} {
		t.Run(goos, func(t *testing.T) {
			h := newHarness(t)
			h.env.OS = goos
			privacy := h.useDesktop()
			h.env.Desktop = func() (desktop.Desktop, error) { t.Fatal("WebRTC inspected desktop settings"); return nil, nil }
			os.Remove(h.layout.ConfigFile())
			for _, action := range []string{"on", "off"} {
				if code := Run(h.env, []string{"webrtc", action}); code != 0 {
					t.Fatal(h.errOut)
				}
			}
			if len(privacy.privacyCalls) != 2 || !privacy.privacyCalls[0] || privacy.privacyCalls[1] {
				t.Fatalf("commands: %v", privacy.privacyCalls)
			}
			if !strings.Contains(h.out.String(), "Restart open browsers") {
				t.Fatal("missing restart instruction")
			}
		})
	}
}
func TestUnixUninstallCleansPrivacyWithoutDesktop(t *testing.T) {
	for _, goos := range []string{"linux", "darwin"} {
		t.Run(goos, func(t *testing.T) {
			h := newHarness(t)
			h.env.OS = goos
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
		})
	}
}
