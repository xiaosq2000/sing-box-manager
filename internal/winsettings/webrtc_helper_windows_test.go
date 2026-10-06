package winsettings

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Native tests deliberately do not call changeWebRTCPolicies: that would request
// UAC and change real browser policies. Instead they run the same embedded
// script with test-only substitutions for disposable paths/mutex names and
// user-owned protected ACLs. Production paths/ACLs have no runtime override.
type nativeWebRTCFixture struct {
	registry PowerShell
	root     string
	sid      string
	exe      string
}

func newNativeWebRTCFixture(t *testing.T) *nativeWebRTCFixture {
	t.Helper()
	account, err := user.Current()
	if err != nil || !validWebRTCSID(account.Uid) {
		t.Fatalf("original process SID: %v", err)
	}
	exe, err := webRTCSystemPowerShell()
	if err != nil {
		t.Fatal(err)
	}
	fixture := &nativeWebRTCFixture{
		root: `Software\sbc-tests\` + fmt.Sprintf("webrtc-%d-%d", os.Getpid(), time.Now().UnixNano()),
		sid:  account.Uid,
		exe:  exe,
	}
	fixture.registry.Run = func(input []byte) ([]byte, error) {
		return fixture.run(registryScript, input)
	}
	t.Cleanup(func() {
		// Only this test's disposable subtree. No actual policy path appears.
		script := "$ErrorActionPreference='Stop';[Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree('" + fixture.root + "',$false)"
		if _, err := fixture.run(script, nil); err != nil {
			t.Errorf("disposable registry cleanup: %v", err)
		}
	})
	return fixture
}

func (f *nativeWebRTCFixture) run(script string, input []byte) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, f.exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encodedWebRTCCommand(script))
	command.Env = webRTCEnvironment(os.Environ())
	command.Stdin = bytes.NewReader(input)
	return command.Output()
}

func (f *nativeWebRTCFixture) key(key string) string {
	if key == WebRTCMetadataKey {
		return f.root + `\Metadata`
	}
	for _, policy := range webRTCPolicies {
		if policy.key == key {
			return f.root + `\Browsers\` + strings.TrimPrefix(key, `Software\Policies\`)
		}
	}
	panic("not a fixture policy key")
}

func (f *nativeWebRTCFixture) script(t *testing.T, action string) string {
	t.Helper()
	script, err := webRTCScript(f.sid, action)
	if err != nil {
		t.Fatal(err)
	}
	pairs := []string{WebRTCMetadataKey, f.key(WebRTCMetadataKey)}
	seen := map[string]bool{}
	for _, policy := range webRTCPolicies {
		if !seen[policy.key] {
			pairs = append(pairs, policy.key, f.key(policy.key))
			seen[policy.key] = true
		}
	}
	script = strings.NewReplacer(pairs...).Replace(script)
	script = strings.Replace(script,
		"'O:BAG:BAD:P(A;;KA;;;SY)(A;;KA;;;BA)(A;;KR;;;' + $sid + ')'",
		"'O:' + $sid + 'D:P(A;;KA;;;' + $sid + ')'", 1)
	script = strings.Replace(script,
		"'O:BAG:BAD:P(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)'",
		"'O:' + $sid + 'D:P(A;;0x1f0001;;;' + $sid + ')'", 1)
	script = strings.Replace(script, `'Global\sbc.WebRTC.'`,
		`'Local\sbc-tests.WebRTC.`+strings.TrimPrefix(f.root, `Software\sbc-tests\`)+`.'`, 1)
	return script
}

func (f *nativeWebRTCFixture) read(t *testing.T, key, name string) Value {
	t.Helper()
	values, err := f.registry.Read(f.key(key), []string{name})
	if err != nil {
		t.Fatal(err)
	}
	return values[name]
}

func TestNativeWebRTCDisposablePoliciesPreservePreexistingAndChangedValues(t *testing.T) {
	f := newNativeWebRTCFixture(t)
	if err := f.registry.Apply(f.key(ChromePolicyKey), map[string]Value{
		WebRtcPolicyName: webRTCPolicies[0].want,
		"Unrelated":      {"private fixture text", "String"},
	}, ""); err != nil {
		t.Fatal(err)
	}
	if err := f.registry.Apply(f.key(FirefoxPolicyKey), map[string]Value{FirefoxProxyOnlyName: {"false", "String"}}, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := f.run(f.script(t, "on"), nil); err != nil {
		t.Fatalf("disposable policy setup: %v", err)
	}
	if f.read(t, WebRTCMetadataKey, WebRTCChromeOwnerName).Kind != "" || f.read(t, WebRTCMetadataKey, WebRTCFirefoxProxyName).Kind != "" {
		t.Fatal("pre-existing values were claimed")
	}
	if f.read(t, WebRTCMetadataKey, WebRTCEdgeOwnerName) != (Value{WebRTCMetadataIntent, WebRTCMetadataKind}) {
		t.Fatal("new policy lacks durable ownership")
	}
	if err := f.registry.Apply(f.key(EdgePolicyKey), map[string]Value{EdgeWebRtcPolicyName: {WebRtcDisableNonProxiedUDP, "ExpandString"}}, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := f.run(f.script(t, "off"), nil); err != nil {
		t.Fatalf("disposable policy cleanup: %v", err)
	}
	if f.read(t, ChromePolicyKey, WebRtcPolicyName) != webRTCPolicies[0].want || f.read(t, ChromePolicyKey, "Unrelated").Kind == "" {
		t.Fatal("pre-existing or unrelated value was removed")
	}
	if f.read(t, EdgePolicyKey, EdgeWebRtcPolicyName).Kind != "ExpandString" || f.read(t, FirefoxPolicyKey, FirefoxProxyOnlyName).Text != "false" {
		t.Fatal("foreign edit or Firefox value was removed")
	}
	if f.read(t, BravePolicyKey, WebRtcPolicyName).Kind != "" {
		t.Fatal("owned value was not removed")
	}
	for _, policy := range webRTCPolicies {
		if f.read(t, WebRTCMetadataKey, policy.owner).Kind != "" {
			t.Fatal("owned metadata remains")
		}
	}
}

func TestNativeWebRTCDisposableInterruptedSetupAndCleanupCanRetry(t *testing.T) {
	f := newNativeWebRTCFixture(t)
	script := strings.Replace(f.script(t, "on"), "$key.SetValue($policy.Name,", "if ($policy.Owner -eq 'EdgeLocalhostIP') { throw 'Synthetic write failure' }; $key.SetValue($policy.Name,", 1)
	if _, err := f.run(script, nil); err == nil {
		t.Fatal("injected setup failure reported success")
	}
	if f.read(t, ChromePolicyKey, WebRtcPolicyName) != webRTCPolicies[0].want || f.read(t, EdgePolicyKey, EdgeWebRtcPolicyName).Kind != "" {
		t.Fatal("fixture did not interrupt after partial setup")
	}
	if f.read(t, WebRTCMetadataKey, WebRTCEdgeOwnerName) != (Value{WebRTCMetadataIntent, WebRTCMetadataKind}) {
		t.Fatal("interrupted write lost its intent")
	}
	if _, err := f.run(f.script(t, "on"), nil); err != nil {
		t.Fatalf("setup retry: %v", err)
	}
	script = strings.Replace(f.script(t, "off"), "$key.DeleteValue($policy.Name,", "if ($policy.Owner -eq 'EdgeLocalhostIP') { throw 'Synthetic delete failure' }; $key.DeleteValue($policy.Name,", 1)
	if _, err := f.run(script, nil); err == nil {
		t.Fatal("injected deletion failure reported success")
	}
	if f.read(t, ChromePolicyKey, WebRtcPolicyName).Kind != "" || f.read(t, WebRTCMetadataKey, WebRTCEdgeOwnerName).Kind == "" {
		t.Fatal("partial cleanup lost recovery metadata")
	}
	if _, err := f.run(f.script(t, "off"), nil); err != nil {
		t.Fatalf("cleanup retry: %v", err)
	}
	for _, policy := range webRTCPolicies {
		if f.read(t, policy.key, policy.name).Kind != "" || f.read(t, WebRTCMetadataKey, policy.owner).Kind != "" {
			t.Fatal("retry left owned values")
		}
	}
	check := "$key=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('" + f.key(WebRTCMetadataKey) + "',$false);if($null -ne $key){$key.Dispose();exit 1}"
	if _, err := f.run(check, nil); err != nil {
		t.Fatal("empty ownership key was not removed")
	}
}

func TestNativeWebRTCDisposableConflictsAndUntrustedMetadataFailClosed(t *testing.T) {
	f := newNativeWebRTCFixture(t)
	foreign := Value{"default_public_interface_only", "String"}
	if err := f.registry.Apply(f.key(BravePolicyKey), map[string]Value{WebRtcPolicyName: foreign}, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := f.run(f.script(t, "on"), nil); err == nil {
		t.Fatal("conflicting Chromium policy was accepted")
	}
	if f.read(t, ChromePolicyKey, WebRtcPolicyName).Kind != "" || f.read(t, BravePolicyKey, WebRtcPolicyName) != foreign {
		t.Fatal("conflict caused partial setup")
	}
	if err := f.registry.Apply(f.key(BravePolicyKey), map[string]Value{WebRtcPolicyName: {}}, ""); err != nil {
		t.Fatal(err)
	}
	// Ordinary user-owned, inherited metadata must not be trusted even if its
	// value looks like an ownership marker. This is a disposable key only.
	if err := f.registry.Apply(f.key(WebRTCMetadataKey), map[string]Value{WebRTCChromeOwnerName: {WebRTCMetadataIntent, WebRTCMetadataKind}}, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := f.run(f.script(t, "on"), nil); err == nil {
		t.Fatal("untrusted metadata ACL was accepted")
	}
	if _, err := f.run(f.script(t, "off"), nil); err == nil {
		t.Fatal("untrusted metadata allowed cleanup")
	}
	if f.read(t, WebRTCMetadataKey, WebRTCChromeOwnerName).Kind == "" {
		t.Fatal("failed cleanup forgot recovery metadata")
	}
}

func TestNativeWebRTCDisposableEdgeMigration(t *testing.T) {
	for _, test := range []struct {
		name, action  string
		manual, owned bool
		legacy        Value
	}{
		{"upgrade", "on", false, true, Value{"disable_non_proxied_udp", "String"}},
		{"cleanup", "off", false, true, Value{"disable_non_proxied_udp", "String"}},
		{"manual supported policy", "on", true, true, Value{"disable_non_proxied_udp", "String"}},
		{"unowned obsolete value", "on", false, false, Value{"disable_non_proxied_udp", "String"}},
		{"changed obsolete value", "on", false, true, Value{"default", "String"}},
		{"changed obsolete type", "on", false, true, Value{"disable_non_proxied_udp", "ExpandString"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := newNativeWebRTCFixture(t)
			if _, err := f.run(f.script(t, "on"), nil); err != nil {
				t.Fatal(err)
			}
			// Keep the protected fixture ACL, but seed the old release's
			// value and ownership identity independently of the policy table.
			supported := Value{}
			if test.manual {
				supported = Value{"disable_non_proxied_udp", "String"}
			}
			if err := f.registry.Apply(f.key(EdgePolicyKey), map[string]Value{
				"WebRtcLocalhostIpHandling": supported, "WebRtcIPHandling": test.legacy,
			}, ""); err != nil {
				t.Fatal(err)
			}
			owner := Value{}
			if test.owned {
				owner = Value{"1", "DWord"}
			}
			if err := f.registry.Apply(f.key(WebRTCMetadataKey), map[string]Value{
				WebRTCEdgeOwnerName: {}, "Edge": owner,
			}, ""); err != nil {
				t.Fatal(err)
			}
			if _, err := f.run(f.script(t, test.action), nil); err != nil {
				t.Fatalf("migrate Edge: %v", err)
			}
			want := Value{}
			if test.action == "on" || test.manual {
				want = Value{"disable_non_proxied_udp", "String"}
			}
			legacy := test.legacy
			if test.owned && legacy == (Value{"disable_non_proxied_udp", "String"}) {
				legacy = Value{}
			}
			if f.read(t, EdgePolicyKey, "WebRtcLocalhostIpHandling") != want || f.read(t, EdgePolicyKey, "WebRtcIPHandling") != legacy {
				t.Fatal("migration applied the wrong Edge policy or changed a foreign value")
			}
			if f.read(t, WebRTCMetadataKey, "Edge").Kind != "" {
				t.Fatal("migration left the obsolete ownership intent")
			}
			if _, err := f.run(f.script(t, "off"), nil); err != nil {
				t.Fatal(err)
			}
			if f.read(t, EdgePolicyKey, "WebRtcLocalhostIpHandling") != supported || f.read(t, EdgePolicyKey, "WebRtcIPHandling") != legacy {
				t.Fatal("cleanup removed a manual policy or left the new owned policy")
			}
		})
	}
}

func TestNativeWebRTCDisposableEdgeMigrationRetriesFailedDeletion(t *testing.T) {
	f := newNativeWebRTCFixture(t)
	if _, err := f.run(f.script(t, "on"), nil); err != nil {
		t.Fatal(err)
	}
	if err := f.registry.Apply(f.key(EdgePolicyKey), map[string]Value{
		"WebRtcIPHandling": {"disable_non_proxied_udp", "String"},
	}, ""); err != nil {
		t.Fatal(err)
	}
	if err := f.registry.Apply(f.key(WebRTCMetadataKey), map[string]Value{"Edge": {"1", "DWord"}}, ""); err != nil {
		t.Fatal(err)
	}
	script := strings.Replace(f.script(t, "on"), "$key.DeleteValue($policy.Name,",
		"if ($policy.Owner -eq 'Edge') { throw 'Synthetic legacy deletion failure' }; $key.DeleteValue($policy.Name,", 1)
	if _, err := f.run(script, nil); err == nil {
		t.Fatal("failed migration reported success")
	}
	if f.read(t, WebRTCMetadataKey, "Edge") != (Value{"1", "DWord"}) || f.read(t, EdgePolicyKey, "WebRtcIPHandling").Kind == "" {
		t.Fatal("failed migration lost its retry state")
	}
	if _, err := f.run(f.script(t, "on"), nil); err != nil {
		t.Fatal(err)
	}
	if f.read(t, WebRTCMetadataKey, "Edge").Kind != "" || f.read(t, EdgePolicyKey, "WebRtcIPHandling").Kind != "" {
		t.Fatal("migration retry left the obsolete policy")
	}
}

func TestNativeWebRTCLauncherClassifiesWrappedCancellationWithoutUAC(t *testing.T) {
	f := newNativeWebRTCFixture(t)
	for _, test := range []struct {
		name, exception string
		status          int
	}{
		{"direct cancellation", `[System.ComponentModel.Win32Exception]::new(1223)`, 2},
		{"wrapped cancellation", `[System.Management.Automation.MethodInvocationException]::new('fixture', [System.ComponentModel.Win32Exception]::new(1223))`, 2},
		{"nested cancellation", `[System.Reflection.TargetInvocationException]::new('fixture', [System.Management.Automation.MethodInvocationException]::new('fixture', [System.ComponentModel.Win32Exception]::new(1223)))`, 2},
		{"access denied", `[System.Management.Automation.MethodInvocationException]::new('fixture', [System.ComponentModel.Win32Exception]::new(5))`, 1},
		{"other failure", `[System.InvalidOperationException]::new('fixture')`, 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			launcher := webRTCLauncher(f.exe, encodedWebRTCCommand("exit 0"))
			start := "$p=[System.Diagnostics.Process]::Start($s)"
			if strings.Count(launcher, start) != 1 {
				t.Fatal("launcher fixture cannot replace the UAC operation")
			}
			// Throw a synthetic exception instead of Process.Start: no UAC or
			// elevated process can be requested by this test.
			launcher = strings.Replace(launcher, start, "throw "+test.exception, 1)
			_, err := f.run(launcher, nil)
			exit, ok := err.(*exec.ExitError)
			if !ok || exit.ExitCode() != test.status {
				t.Fatalf("exception classification: %v; want status %d", err, test.status)
			}
		})
	}
}

func TestNativeWebRTCDisposableUnretainedDeletionKeepsIntent(t *testing.T) {
	f := newNativeWebRTCFixture(t)
	if _, err := f.run(f.script(t, "on"), nil); err != nil {
		t.Fatalf("setup: %v", err)
	}
	// A successful-looking deletion that does not remove the value must
	// still fail read-back verification before the intent is removed.
	script := strings.Replace(f.script(t, "off"), "$key.DeleteValue($policy.Name, $false)",
		"if ($policy.Owner -ne 'EdgeLocalhostIP') { $key.DeleteValue($policy.Name, $false) }", 1)
	if _, err := f.run(script, nil); err == nil {
		t.Fatal("unretained deletion reported success")
	}
	if f.read(t, EdgePolicyKey, EdgeWebRtcPolicyName) != webRTCPolicies[1].want ||
		f.read(t, WebRTCMetadataKey, WebRTCEdgeOwnerName) != (Value{WebRTCMetadataIntent, WebRTCMetadataKind}) {
		t.Fatal("failed deletion verification lost the retry intent")
	}
	if _, err := f.run(f.script(t, "off"), nil); err != nil {
		t.Fatalf("cleanup retry: %v", err)
	}
}

func TestNativeWebRTCDisposableMetadataKeyDeletionFailureKeepsFinalIntent(t *testing.T) {
	f := newNativeWebRTCFixture(t)
	if _, err := f.run(f.script(t, "on"), nil); err != nil {
		t.Fatalf("setup: %v", err)
	}
	script := strings.Replace(f.script(t, "off"), "$hive.DeleteSubKey($metadataPath, $false)",
		"throw 'Synthetic metadata key deletion failure'", 1)
	if _, err := f.run(script, nil); err == nil {
		t.Fatal("failed metadata key deletion reported success")
	}
	last := webRTCPolicies[len(webRTCPolicies)-1]
	if f.read(t, last.key, last.name).Kind != "" ||
		f.read(t, WebRTCMetadataKey, last.owner) != (Value{WebRTCMetadataIntent, WebRTCMetadataKind}) {
		t.Fatal("failed metadata key deletion lost its final retry intent")
	}
	if _, err := f.run(f.script(t, "off"), nil); err != nil {
		t.Fatalf("metadata key cleanup retry: %v", err)
	}
	if f.read(t, WebRTCMetadataKey, last.owner).Kind != "" {
		t.Fatal("metadata retry intent was not removed")
	}
}

func waitForNativeWebRTCFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return
		} else if !os.IsNotExist(err) {
			t.Fatal(err)
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatal("timed out waiting for the disposable helper fixture")
}

func TestNativeWebRTCDisposableConcurrentSetupAndResetSerialize(t *testing.T) {
	for _, interrupted := range []bool{false, true} {
		t.Run(fmt.Sprintf("interrupted=%t", interrupted), func(t *testing.T) {
			f := newNativeWebRTCFixture(t)
			dir := t.TempDir()
			setupReady := filepath.Join(dir, "setup-ready")
			release := filepath.Join(dir, "release")
			resetReady := filepath.Join(dir, "reset-ready")
			resetEntered := filepath.Join(dir, "reset-entered")
			quote := func(path string) string { return "'" + strings.ReplaceAll(path, "'", "''") + "'" }
			onDone, offDone := make(chan error, 1), make(chan error, 1)
			onFinished, offFinished, offStarted := false, false, false
			defer func() {
				// Always let the paused fixture exit before deleting its keys.
				_ = os.WriteFile(release, []byte("release"), 0600)
				if !onFinished {
					<-onDone
				}
				if offStarted && !offFinished {
					<-offDone
				}
			}()
			pause := "if ($policy.Owner -eq 'Chrome') { [System.IO.File]::WriteAllText(" + quote(setupReady) + ", 'ready');" +
				"while (-not [System.IO.File]::Exists(" + quote(release) + ")) { [System.Threading.Thread]::Sleep(25) };"
			if interrupted {
				// Abrupt exit leaves an abandoned mutex and a durable intent.
				pause += "[System.Diagnostics.Process]::GetCurrentProcess().Kill();"
			}
			pause += " }"
			on := strings.Replace(f.script(t, "on"), "$metadata.Flush()", "$metadata.Flush(); "+pause, 1)
			go func() { _, err := f.run(on, nil); onDone <- err }()
			waitForNativeWebRTCFile(t, setupReady)
			if f.read(t, WebRTCMetadataKey, WebRTCChromeOwnerName).Kind == "" || f.read(t, ChromePolicyKey, WebRtcPolicyName).Kind != "" {
				t.Fatal("setup fixture did not pause after intent and before policy write")
			}
			off := strings.Replace(f.script(t, "off"), "$mutex = [System.Threading.Mutex]::new",
				"[System.IO.File]::WriteAllText("+quote(resetReady)+", 'ready'); $mutex = [System.Threading.Mutex]::new", 1)
			off = strings.Replace(off, "$hive = [Microsoft.Win32.Registry]::Users.OpenSubKey",
				"[System.IO.File]::WriteAllText("+quote(resetEntered)+", 'entered'); $hive = [Microsoft.Win32.Registry]::Users.OpenSubKey", 1)
			offStarted = true
			go func() { _, err := f.run(off, nil); offDone <- err }()
			waitForNativeWebRTCFile(t, resetReady)
			time.Sleep(250 * time.Millisecond)
			if _, err := os.Stat(resetEntered); !os.IsNotExist(err) {
				t.Fatal("reset entered the registry while setup held the mutex")
			}
			select {
			case err := <-offDone:
				offFinished = true
				t.Fatalf("reset did not wait for setup: %v", err)
			default:
			}
			if err := os.WriteFile(release, []byte("release"), 0600); err != nil {
				t.Fatal(err)
			}
			err := <-onDone
			onFinished = true
			if (err != nil) != interrupted {
				t.Fatalf("setup exit: %v; interrupted=%t", err, interrupted)
			}
			err = <-offDone
			offFinished = true
			if err != nil {
				t.Fatalf("serialized reset / abandoned-mutex recovery: %v", err)
			}
			for _, policy := range webRTCPolicies {
				if f.read(t, policy.key, policy.name).Kind != "" || f.read(t, WebRTCMetadataKey, policy.owner).Kind != "" {
					t.Fatal("concurrent helper operations left owned values or erased retry ownership")
				}
			}
		})
	}
}
