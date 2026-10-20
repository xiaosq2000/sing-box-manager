package install

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func tunInstaller(t *testing.T) (*Installer, *portal) {
	t.Helper()
	p := newPortal(t)
	p.configTun = `{"inbounds":[{"type":"tun","tag":"tun"}],"experimental":{"clash_api":{"default_mode":"direct"}}}`
	i, layout := installer(t, p)
	if _, err := i.Install(link(t, p)); err != nil {
		t.Fatal(err)
	}
	for path, value := range map[string]string{
		layout.SBC():        "#!/bin/sh\nprintf 'sbc old-build\\n'\n",
		layout.SBCVersion(): "old-build\n",
		layout.TunState():   `{"api_port":32123,"api_secret":"fixture-old-secret"}`,
		layout.ETag():       "fixture-old-etag\n",
		layout.Usage():      "fixture-old-usage\n",
	} {
		if err := paths.WriteFile(path, []byte(value), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	return i, p
}

func tunUserSnapshot(t *testing.T, layout paths.Layout) map[string][]byte {
	t.Helper()
	result := map[string][]byte{}
	for _, path := range []string{layout.SBC(), layout.SBCVersion(), layout.ConfigFile(), layout.TunState(), layout.SingBoxVersion(), layout.ETag(), layout.Usage(), layout.SingBox(), filepath.Join(layout.Bin(), "libcronet.so"), filepath.Join(layout.Data, "rules", "abc.srs")} {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		result[path] = data
	}
	return result
}

func assertTunSnapshot(t *testing.T, want map[string][]byte) {
	t.Helper()
	for path, before := range want {
		after, err := os.ReadFile(path)
		if err != nil || !bytes.Equal(before, after) {
			t.Fatalf("live file changed: %s: %v", filepath.Base(path), err)
		}
	}
}

func TestPrepareTunUpdateDoesNotPublishUserFiles(t *testing.T) {
	for _, upgrade := range []bool{false, true} {
		t.Run(map[bool]string{false: "refresh", true: "upgrade"}[upgrade], func(t *testing.T) {
			i, portal := tunInstaller(t)
			before := tunUserSnapshot(t, i.Layout)
			plan, err := i.PrepareTunUpdate(link(t, portal), "old-build", i.Layout.SBC(), upgrade)
			if err != nil {
				t.Fatal(err)
			}
			defer plan.Close()
			assertTunSnapshot(t, before)
			if plan.Envelope == nil || len(plan.Envelope.ConfigTun) == 0 {
				t.Fatal("candidate lost the TUN envelope")
			}
			if _, err := os.Stat(plan.Layout.SingBox()); err != nil {
				t.Fatal("candidate is missing its complete binary directory")
			}
			if _, err := os.Stat(RecoveryFile(i.Layout)); !os.IsNotExist(err) {
				t.Fatal("preparation created a live recovery record")
			}
			if upgrade && plan.Upgraded.SBC != "abc123" || !upgrade && plan.Pending.SBC != "abc123" {
				t.Fatal("candidate lost the signed build selection")
			}
		})
	}
}

func TestTunEngineUpgradePublishesAndRestoresItsCompleteCandidate(t *testing.T) {
	i, p := tunInstaller(t)
	for path, value := range map[string]string{
		i.Layout.SingBoxVersion():                     "1.14.1\n",
		i.Layout.SingBox():                            fakeSingBox + "# installed-old-engine\n",
		filepath.Join(i.Layout.Bin(), "libcronet.so"): "old-library",
	} {
		if err := paths.WriteFile(path, []byte(value), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	before := tunUserSnapshot(t, i.Layout)
	plan, err := i.PrepareTunUpdate(link(t, p), "old-build", i.Layout.SBC(), true)
	if err != nil {
		t.Fatal(err)
	}
	defer plan.Close()
	assertTunSnapshot(t, before)
	if plan.Upgraded.SingBox != "1.14.2" {
		t.Fatal("engine candidate was not downloaded")
	}
	if err = plan.Begin(strings.Repeat("2", 32), strings.Repeat("1", 32), "direct", "trojan"); err != nil {
		t.Fatal(err)
	}
	if err = plan.Publish(); err != nil {
		t.Fatal(err)
	}
	for path, want := range map[string]string{
		i.Layout.SingBoxVersion():                     "1.14.2\n",
		i.Layout.SingBox():                            fakeSingBox,
		filepath.Join(i.Layout.Bin(), "libcronet.so"): "library",
	} {
		data, err := os.ReadFile(path)
		if err != nil || string(data) != want {
			t.Fatalf("engine publication failed: %s", filepath.Base(path))
		}
	}
	if err = plan.Recovery().Restore(); err != nil {
		t.Fatal(err)
	}
	assertTunSnapshot(t, before)
	if err = plan.Finish(); err != nil {
		t.Fatal(err)
	}
}

func TestPrepareTunUpdateRejectsBadCandidatesWithoutLiveWrites(t *testing.T) {
	for _, failure := range []string{"signature", "core", "rule", "client", "config", "no-tun"} {
		t.Run(failure, func(t *testing.T) {
			i, p := tunInstaller(t)
			if failure == "core" {
				if err := paths.WriteFile(i.Layout.SingBoxVersion(), []byte("1.14.1\n"), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			before := tunUserSnapshot(t, i.Layout)
			switch failure {
			case "signature":
				i.Keys = []string{newPortal(t).publicKey()}
			case "core":
				p.tamper = "sing-box/sing-box-1.14.2-linux-amd64.tar.gz"
			case "rule":
				p.tamper = "rules/abc.srs"
			case "client":
				p.files["sbc/linux-amd64/sbc"] = []byte("#!/bin/sh\nprintf 'sbc wrong-build\\n'\n")
			case "config":
				p.config = strings.ReplaceAll(p.config, "china", "reject")
			case "no-tun":
				p.configTun = ""
			}
			plan, err := i.PrepareTunUpdate(link(t, p), "old-build", i.Layout.SBC(), true)
			if err == nil {
				plan.Close()
				t.Fatal("bad candidate passed preparation")
			}
			assertTunSnapshot(t, before)
			if _, err := os.Stat(RecoveryFile(i.Layout)); !os.IsNotExist(err) {
				t.Fatal("bad preparation left a live recovery record")
			}
		})
	}
}

func TestTunRecoveryCannotUseMixedRollback(t *testing.T) {
	i, portal := tunInstaller(t)
	before := tunUserSnapshot(t, i.Layout)
	plan, err := i.PrepareTunUpdate(link(t, portal), "old-build", i.Layout.SBC(), true)
	if err != nil {
		t.Fatal(err)
	}
	defer plan.Close()
	if err = plan.Begin(strings.Repeat("2", 32), strings.Repeat("1", 32), "direct", "trojan"); err != nil {
		t.Fatal(err)
	}
	if err = plan.Publish(); err != nil {
		t.Fatal(err)
	}
	published, err := os.ReadFile(i.Layout.SBC())
	if err != nil || bytes.Equal(before[i.Layout.SBC()], published) {
		t.Fatal("test did not publish the candidate")
	}
	i.BeforeRestore = func() error { t.Fatal("mixed shutdown hook ran"); return nil }
	called := func() error { t.Fatal("mixed restart hook ran"); return nil }
	if _, err = i.Recover(called, called); err == nil {
		t.Fatal("mixed recovery accepted a TUN transaction")
	}
	recovery, err := i.PendingTunUpdate()
	if err != nil || recovery == nil {
		t.Fatalf("typed recovery missing: %v", err)
	}
	if err = recovery.Restore(); err != nil {
		t.Fatal(err)
	}
	assertTunSnapshot(t, before)
	if err = recovery.Finish(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(RecoveryFile(i.Layout)); !os.IsNotExist(err) {
		t.Fatal("finished recovery kept its live journal")
	}
}

func TestTunRecoveryValidatesItsKindAndDestinations(t *testing.T) {
	layout := paths.Layout{Config: "/fixture/config", Data: "/fixture/data"}
	valid := upgradeRecovery{Stage: "/fixture/data/.upgrade-example", Kind: "tun", Transaction: strings.Repeat("2", 32), PreviousTransaction: strings.Repeat("1", 32), Mode: "direct", Protocol: "trojan", Files: map[string]string{layout.TunState(): "/fixture/data/.upgrade-example/previous/0"}}
	if err := valid.validate(layout); err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*upgradeRecovery){
		func(r *upgradeRecovery) { r.Kind = "" },
		func(r *upgradeRecovery) { r.Kind = "unknown" },
		func(r *upgradeRecovery) { r.Transaction = "invalid" },
		func(r *upgradeRecovery) { r.PreviousTransaction = "invalid" },
		func(r *upgradeRecovery) { r.Mode = "" },
		func(r *upgradeRecovery) { r.Files = map[string]string{"/outside": ""} },
	} {
		bad := valid
		change(&bad)
		if err := bad.validate(layout); err == nil {
			t.Fatal("invalid recovery record passed validation")
		}
	}
}
