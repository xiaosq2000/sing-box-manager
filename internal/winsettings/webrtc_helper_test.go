package winsettings

import (
	"encoding/base64"
	"strings"
	"testing"
	"unicode/utf16"
)

func TestWebRTCPolicyScriptUsesOriginalSIDAndFixedAllowlist(t *testing.T) {
	// These are synthetic SIDs, not an account on the host.
	original := "S-1-5-21-111-222-333-1001"
	administrator := "S-1-5-21-444-555-666-1002"
	t.Setenv("USERNAME", administrator)
	t.Setenv("USER_SID", administrator)
	script, err := webRTCScript(original, "on")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(script, "$sid = '"+original+"'") || !strings.Contains(script, "[Microsoft.Win32.Registry]::Users.OpenSubKey($sid, $true)") {
		t.Fatal("helper did not target the original process-user hive")
	}
	for _, forbidden := range []string{administrator, "CurrentUser", "HKCU", "@@", "ReadToEnd", "Invoke-Expression", "Start-Process", "Set-Acl", "$args", "$env:USERNAME", "-File"} {
		// Comments explain why HKCU is forbidden; only inspect executable code.
		var code strings.Builder
		for _, line := range strings.Split(script, "\n") {
			if !strings.HasPrefix(strings.TrimSpace(line), "#") {
				code.WriteString(line)
			}
		}
		if strings.Contains(code.String(), forbidden) {
			t.Fatalf("helper includes forbidden input/operation %q", forbidden)
		}
	}
	for _, policy := range webRTCPolicies {
		if !strings.Contains(script, "Key = '"+policy.key+"'; Name = '"+policy.name+"'; Owner = '"+policy.owner+"'; Value = '"+policy.want.Text+"'") {
			t.Fatalf("allowlist missing %s", policy.owner)
		}
	}
	intent := strings.Index(script, "$metadata.SetValue($policy.Owner")
	flush := strings.Index(script[intent:], "$metadata.Flush()") + intent
	write := strings.Index(script, "$key.SetValue($policy.Name")
	if intent < 0 || flush < intent || write < flush {
		t.Fatal("intent was not persisted before writing browser policy")
	}
	deletion := strings.Index(script, "$key.DeleteValue($policy.Name")
	verification := strings.Index(script, "if ((Read-Policy $policy).Matches) { throw 'Policy deletion was not retained' }")
	forget := strings.LastIndex(script, "$metadata.DeleteValue($policy.Owner")
	if deletion < 0 || verification < deletion || forget < verification {
		t.Fatal("cleanup discarded ownership before verified policy deletion")
	}
	if !strings.Contains(script, "O:BAG:BAD:P(A;;KA;;;SY)(A;;KA;;;BA)(A;;KR;;;") || !strings.Contains(script, "throw 'Untrusted ownership key'") {
		t.Fatal("helper lacks protected ownership ACL and trust check")
	}
	lastIntent := strings.Index(script, "$lastIntent = $policy.Owner")
	deleteKey := strings.Index(script, "$hive.DeleteSubKey($metadataPath, $false)")
	if lastIntent < verification || deleteKey < lastIntent ||
		!strings.Contains(script, "$metadata.GetValueNames().Length -eq 1 -and $metadata.GetSubKeyNames().Length -eq 0") ||
		!strings.Contains(script, "$null -ne $lastIntent -or") {
		t.Fatal("cleanup does not retain a final intent until metadata key deletion")
	}
}

func TestWebRTCPolicyScriptSerializesHelpersAndDoesNotAlterExistingACLs(t *testing.T) {
	script, err := webRTCScript("S-1-5-21-1-2-3-1001", "off")
	if err != nil {
		t.Fatal(err)
	}
	wait := strings.Index(script, "$locked = $mutex.WaitOne(120000)")
	hive := strings.Index(script, "[Microsoft.Win32.Registry]::Users.OpenSubKey")
	forget := strings.LastIndex(script, "$metadata.DeleteValue($policy.Owner")
	release := strings.Index(script, "$mutex.ReleaseMutex()")
	if wait < 0 || hive < wait || forget < hive || release < forget {
		t.Fatal("the mutex does not cover the entire registry operation")
	}
	for _, required := range []string{
		"'Global\\sbc.WebRTC.' + $sid",
		"O:BAG:BAD:P(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)",
		"throw 'Untrusted policy mutex'",
		"while ($null -ne $cause.InnerException)",
		"$cause -is [System.Threading.AbandonedMutexException]",
		"$mutex.Dispose()",
		"$hive.CreateSubKey($metadataPath, [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree, $security)",
		"$key = $hive.CreateSubKey($policy.Key)",
	} {
		if !strings.Contains(script, required) {
			t.Fatalf("missing synchronization/ACL guard %q", required)
		}
	}
	// Security descriptors are supplied only for creation of private objects.
	// Existing object ACLs are checked, never repaired; browser keys receive
	// no descriptor and retain their inherited/current permissions.
	for _, forbidden := range []string{".SetAccessControl(", "Set-Acl", "CreateSubKey($policy.Key,", "$hive.SetSecurity", "$key.SetSecurity"} {
		if strings.Contains(script, forbidden) {
			t.Fatalf("helper changes an existing or browser ACL: %s", forbidden)
		}
	}
}

func TestWebRTCLauncherUnwrapsUACCancellation(t *testing.T) {
	launcher := webRTCLauncher(`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`, "Zg==")
	unwrap := strings.Index(launcher, "while($null -ne $e.InnerException){$e=$e.InnerException}")
	classify := strings.Index(launcher, "$e -is [System.ComponentModel.Win32Exception] -and $e.NativeErrorCode -eq 1223")
	if unwrap < 0 || classify < unwrap || !strings.Contains(launcher, "{exit 2};exit 1") {
		t.Fatal("launcher does not unwrap exceptions before classifying cancellation")
	}
}

func TestWebRTCPolicyHelperRejectsActionsAndSIDInjection(t *testing.T) {
	for _, sid := range []string{
		"", "alice", "S-1-5", "S-1-5-21-1'; throw 'injected", "S-1-5-21-+1", "S-1-5-21-01",
		"S-2-5-21-1", "s-1-5-21-1", "S-1-281474976710656-1", "S-1-5-4294967296",
		"S-1-5-" + strings.Repeat("1-", 16) + "1",
	} {
		if _, err := webRTCScript(sid, "on"); err == nil {
			t.Fatalf("accepted invalid SID %q", sid)
		}
	}
	for _, sid := range []string{"S-1-5-21-1-2-3-1001", "S-1-12-1-1-2-3-4", "S-1-5-18"} {
		for _, action := range []string{"on", "off"} {
			if _, err := webRTCScript(sid, action); err != nil {
				t.Fatalf("valid request %s %s: %v", sid, action, err)
			}
		}
	}
	for _, action := range []string{"", "ON", "reset", "on'; exit 0", "C:\\writable\\script.ps1"} {
		if _, err := webRTCScript("S-1-5-21-1-2-3-1001", action); err == nil {
			t.Fatalf("accepted action %q", action)
		}
	}
}

func TestWebRTCEncodedCommandAndLauncherFitWindowsLimits(t *testing.T) {
	script, err := webRTCScript("S-1-281474976710655-"+strings.TrimSuffix(strings.Repeat("4294967295-", 15), "-"), "off")
	if err != nil {
		t.Fatal(err)
	}
	encoded := encodedWebRTCCommand(script)
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatal(err)
	}
	units := make([]uint16, len(data)/2)
	for index := range units {
		units[index] = uint16(data[index*2]) | uint16(data[index*2+1])<<8
	}
	if string(utf16.Decode(units)) != script {
		t.Fatal("EncodedCommand did not preserve UTF-16LE script")
	}
	executable := `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`
	launcher := webRTCLauncher(executable, encoded)
	// Allow substantial extra space for path quoting/switches on Windows.
	if len(utf16.Encode([]rune(launcher)))+1000 >= 32767 || len(encoded)+1000 >= 32767 {
		t.Fatalf("command exceeds Windows limits: launcher=%d child=%d", len(launcher), len(encoded))
	}
	if !strings.Contains(launcher, "$s.FileName='"+executable+"'") || !strings.Contains(launcher, "$s.Verb='runas'") || !strings.Contains(launcher, "$s.WorkingDirectory=[System.IO.Path]::GetDirectoryName($s.FileName)") || !strings.Contains(launcher, "-NoProfile -NonInteractive -EncodedCommand "+encoded) || !strings.Contains(launcher, "$p.WaitForExit()") {
		t.Fatal("launcher did not elevate and wait for only the fixed system PowerShell command")
	}
	if strings.Contains(launcher, "sbc.exe") || strings.Contains(launcher, "-File") {
		t.Fatal("launcher elevates a writable executable or script")
	}
}

func TestWebRTCLauncherDoesNotInheritPowerShell7ModulePath(t *testing.T) {
	environment := webRTCEnvironment([]string{"Path=C:\\Windows", "PSModulePath=C:\\PowerShell7\\Modules", "pSmOdUlEpAtH=foreign", "HOME=fixture"})
	if strings.Join(environment, "|") != "Path=C:\\Windows|HOME=fixture" {
		t.Fatalf("environment: %v", environment)
	}
}
