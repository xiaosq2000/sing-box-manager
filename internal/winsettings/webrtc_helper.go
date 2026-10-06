package winsettings

import (
	_ "embed"
	"encoding/base64"
	"fmt"
	"strconv"
	"strings"
	"unicode/utf16"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

//go:embed webrtc_policy.ps1
var webRTCPolicyScript string

// SIDs come from the original process token, never a username/environment.
// Numeric validation also makes interpolation into the fixed script safe.
func validWebRTCSID(sid string) bool {
	parts := strings.Split(sid, "-")
	if len(parts) < 4 || len(parts) > 18 || parts[0] != "S" || parts[1] != "1" {
		return false
	}
	for index, part := range parts[2:] {
		if part == "" || len(part) > 1 && part[0] == '0' {
			return false
		}
		for _, digit := range part {
			if digit < '0' || digit > '9' {
				return false
			}
		}
		bits := 32
		if index == 0 {
			bits = 48
		}
		if _, err := strconv.ParseUint(part, 10, bits); err != nil {
			return false
		}
	}
	return true
}

func webRTCScript(sid, action string) (string, error) {
	if !validWebRTCSID(sid) {
		return "", i18n.New("Windows could not determine a valid SID for the original user")
	}
	if action != "on" && action != "off" {
		return "", i18n.New("the WebRTC policy helper accepts only 'on' or 'off'")
	}
	var table strings.Builder
	for _, policy := range webRTCPolicies {
		chromium := "$false"
		if policy.chromium {
			chromium = "$true"
		}
		obsolete := "$false"
		if policy.obsolete {
			obsolete = "$true"
		}
		// These strings are compile-time allowlisted policy constants. Neither
		// callers nor registry data supply script paths, names or values.
		fmt.Fprintf(&table, "    @{ Key = '%s'; Name = '%s'; Owner = '%s'; Value = '%s'; Chromium = %s; Obsolete = %s }\n",
			policy.key, policy.name, policy.owner, policy.want.Text, chromium, obsolete)
	}
	return strings.NewReplacer("@@SID@@", sid, "@@ACTION@@", action,
		"@@METADATA@@", WebRTCMetadataKey, "@@POLICIES@@", table.String()).Replace(webRTCPolicyScript), nil
}

func encodedWebRTCCommand(script string) string {
	units := utf16.Encode([]rune(script))
	data := make([]byte, len(units)*2)
	for index, unit := range units {
		data[index*2] = byte(unit)
		data[index*2+1] = byte(unit >> 8)
	}
	return base64.StdEncoding.EncodeToString(data)
}

// The unelevated, trusted Windows PowerShell launcher inherits a scrubbed
// environment. Only a second instance of that same system executable gets
// RunAs, with the fixed embedded policy script, SID and action. No temp scripts,
// writable sbc executables, or caller-supplied commands cross the UAC boundary.
func webRTCLauncher(executable, encoded string) string {
	executable = strings.ReplaceAll(executable, "'", "''")
	return "$ErrorActionPreference='Stop';$env:PSModulePath=$null;try{" +
		"$s=[System.Diagnostics.ProcessStartInfo]::new();$s.FileName='" + executable + "';" +
		"$s.WorkingDirectory=[System.IO.Path]::GetDirectoryName($s.FileName);" +
		"$s.UseShellExecute=$true;$s.Verb='runas';" +
		"$s.Arguments='-NoProfile -NonInteractive -EncodedCommand " + encoded + "';" +
		"$p=[System.Diagnostics.Process]::Start($s);$p.WaitForExit();exit $p.ExitCode" +
		"}catch{$e=$_.Exception;while($null -ne $e.InnerException){$e=$e.InnerException};" +
		"if($e -is [System.ComponentModel.Win32Exception] -and $e.NativeErrorCode -eq 1223){exit 2};exit 1}"
}

func webRTCEnvironment(environment []string) []string {
	result := make([]string, 0, len(environment))
	for _, entry := range environment {
		name, _, _ := strings.Cut(entry, "=")
		if !strings.EqualFold(name, "PSModulePath") {
			result = append(result, entry)
		}
	}
	return result
}
