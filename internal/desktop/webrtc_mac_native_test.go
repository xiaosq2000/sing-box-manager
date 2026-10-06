//go:build darwin

package desktop

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Only disposable preference domains are touched. No browser defaults are read.
func TestMacNativePlistOwnership(t *testing.T) {
	p := fixturePrivacy(t, "darwin")
	domain := fmt.Sprintf("com.xiaosq2000.sbc.test.%d.%d", os.Getpid(), time.Now().UnixNano())
	policy := unixPolicy{location: domain, name: "WebRtcLocalhostIpHandling"}
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	policy.location = filepath.Join(dir, domain)
	p.policies = []unixPolicy{policy}
	// Execute native plist tools against a disposable plist, without sudo.
	p.run = func(name string, args ...string) (string, error) {
		if name == "sudo" {
			name, args = args[0], args[1:]
		}
		// Cache refresh is covered by the disposable lifecycle, never unit tests.
		if name == "/usr/bin/killall" {
			return "", nil
		}
		if name == "/bin/mkdir" {
			return "", os.MkdirAll(args[1], 0755)
		}
		for _, arg := range args {
			if strings.Contains(arg, "/Library/Managed Preferences") {
				t.Fatal("native fixture escaped its temporary directory")
			}
		}
		return Exec(name, args...)
	}

	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
	if info, err := os.Stat(policy.location + ".plist"); err != nil || info.Mode().Perm() != 0644 {
		t.Fatalf("new managed plist must be readable by browsers: %v, %v", info, err)
	}
	if err := p.Ensure(); err != nil {
		t.Fatal(err)
	}
	if err := p.Set(false); err != nil {
		t.Fatal(err)
	}
	_, present, err := readMacDefault(p.run, policy.location, policy.name)
	if err != nil || present {
		t.Fatalf("cleanup: present=%t, %v", present, err)
	}
}
