package desktop

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

func (p *unixPrivacy) macOwners(policy unixPolicy) string {
	return filepath.Join(filepath.Dir(policy.location), ".sbc-webrtc-"+p.uid)
}

// defaults returns exit 1 for an absent key. Other failures must propagate.
// Use the C locale so the absence diagnostic is independent of the UI language.
func readMacDefault(run Runner, domain, name string) (string, bool, error) {
	// An unreadable plist is not an absent preference. defaults can report the
	// same diagnostic for both, so check file access independently.
	if filepath.IsAbs(domain) {
		if _, err := privateFile(domain + ".plist"); os.IsNotExist(err) {
			return "", false, nil
		} else if err != nil {
			return "", false, err
		}
	}
	value, err := run("/usr/bin/env", "LC_ALL=C", "/usr/bin/defaults", "read", domain, name)
	if err != nil {
		if strings.Contains(err.Error(), "The domain/default pair of ("+domain+", "+name+") does not exist") {
			return "", false, nil
		}
		return "", false, err
	}
	kind, err := run("/usr/bin/env", "LC_ALL=C", "/usr/bin/defaults", "read-type", domain, name)
	if err != nil {
		return "", false, err
	}
	if strings.TrimSpace(kind) != "Type is string" {
		return "<non-string>", true, nil
	}
	return strings.TrimSpace(value), true, nil
}
func writeMacDefault(run Runner, domain, name string, remove bool) error {
	// Managed preference files need root ownership. Refuse symbolic links before
	// calling defaults. The normal destination and its parents are root-owned.
	for path := domain + ".plist"; path != "/"; path = filepath.Dir(path) {
		info, err := os.Lstat(path)
		if err != nil && !os.IsNotExist(err) {
			return err
		}
		if err == nil && info.Mode()&os.ModeSymlink != 0 {
			return i18n.Errorf("WebRTC settings path is not a regular file: %s", path)
		}
	}
	if !remove {
		if _, err := run("sudo", "/bin/mkdir", "-p", filepath.Dir(domain)); err != nil {
			return err
		}
	}
	_, beforeErr := os.Stat(domain + ".plist")
	args := []string{"write", domain, name, "-string", winsettings.WebRtcDisableNonProxiedUDP}
	if remove {
		args = []string{"delete", domain, name}
	}
	_, err := run("sudo", append([]string{"/usr/bin/defaults"}, args...)...)
	if err == nil && !remove && os.IsNotExist(beforeErr) {
		// Browsers and unelevated sbc must be able to read a newly created plist.
		_, err = run("sudo", "/bin/chmod", "0644", domain+".plist")
	}
	return err
}
func (p *unixPrivacy) setMacPolicy(policy unixPolicy, enabled bool) error {
	owner := filepath.Base(policy.location) + "." + policy.name
	macPrivacyOwners := p.macOwners(policy)
	marker, owned, err := readMacDefault(p.run, macPrivacyOwners, owner)
	if err != nil {
		return err
	}
	if owned && marker != winsettings.WebRtcDisableNonProxiedUDP {
		return i18n.New("the WebRTC policy ownership record is invalid; cleanup cannot safely continue")
	}
	value, present, err := readMacDefault(p.run, policy.location, policy.name)
	if err != nil {
		return err
	}
	if enabled {
		// Preserve user-level values too. A conflict must be reviewed before a
		// mandatory policy could override it.
		local, exists, err := readMacDefault(p.run, filepath.Base(policy.location), policy.name)
		if err != nil {
			return err
		}
		if exists && local != winsettings.WebRtcDisableNonProxiedUDP {
			return i18n.Errorf("an existing WebRTC policy at %s conflicts with proxy-only UDP; review your browser policies", filepath.Base(policy.location))
		}
		if present {
			if value != winsettings.WebRtcDisableNonProxiedUDP {
				return i18n.Errorf("an existing WebRTC policy at %s conflicts with proxy-only UDP; review your browser policies", policy.location)
			}
			return nil // A matching manual policy remains unowned.
		}
		// Persist intent before the native write so interruption remains recoverable.
		if err := writeMacDefault(p.run, macPrivacyOwners, owner, false); err != nil {
			return err
		}
		if marker, ok, err := readMacDefault(p.run, macPrivacyOwners, owner); err != nil || !ok || marker != winsettings.WebRtcDisableNonProxiedUDP {
			if err != nil {
				return err
			}
			return i18n.New("the WebRTC policy ownership record is invalid; cleanup cannot safely continue")
		}
		if err := writeMacDefault(p.run, policy.location, policy.name, false); err != nil {
			return err
		}
		value, present, err = readMacDefault(p.run, policy.location, policy.name)
		if err != nil {
			return err
		}
		if !present || value != winsettings.WebRtcDisableNonProxiedUDP {
			return i18n.Errorf("the browser did not retain the WebRTC policy at %s", policy.location)
		}
		return nil
	}
	if !owned {
		return nil
	}
	if present && value == winsettings.WebRtcDisableNonProxiedUDP {
		if err := writeMacDefault(p.run, policy.location, policy.name, true); err != nil {
			return err
		}
		_, present, err = readMacDefault(p.run, policy.location, policy.name)
		if err != nil {
			return err
		}
		if present {
			return i18n.Errorf("the browser did not remove the owned WebRTC policy at %s", policy.location)
		}
	}
	if err := writeMacDefault(p.run, macPrivacyOwners, owner, true); err != nil {
		return err
	}
	_, present, err = readMacDefault(p.run, macPrivacyOwners, owner)
	if err != nil {
		return err
	}
	if present {
		return i18n.New("the WebRTC policy ownership record is invalid; cleanup cannot safely continue")
	}
	return nil
}
