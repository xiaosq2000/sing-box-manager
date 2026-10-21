package desktop

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func geolocationFixture(t *testing.T) *ChromeGeolocation {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the Linux policy payload needs /bin/sh")
	}
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	p, err := NewChromeGeolocation("linux", filepath.Join(root, "config", "geolocation-mode"), nil)
	if err != nil {
		t.Fatal(err)
	}
	p.PolicyDir, p.UID = filepath.Join(root, "policies"), "1001"
	p.Run = func(name string, args ...string) (string, error) {
		if name != "sudo" || len(args) != 7 || args[0] != "/bin/sh" || args[2] != geolocationPolicyScript || args[5] != p.PolicyDir || args[6] != p.UID {
			t.Fatalf("unexpected command: %s %v", name, args)
		}
		// Exercise policy and marker modes under a private elevated umask.
		command := append([]string{"-c", `umask 077; exec "$@"`, "fixture"}, args...)
		output, err := exec.Command("/bin/sh", command...).CombinedOutput()
		if err != nil {
			return "", fmt.Errorf("%w: %s", err, output)
		}
		return string(output), nil
	}
	return p
}

func writeGeolocationFixture(t *testing.T, path, data string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(data), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestChromeGeolocationLifecycle(t *testing.T) {
	p := geolocationFixture(t)
	status, err := p.Status()
	if err != nil || status.Enabled || status.Configured || status.Owned {
		t.Fatalf("default status: %+v, %v", status, err)
	}
	if _, err := os.Stat(filepath.Dir(p.StateFile)); !os.IsNotExist(err) {
		t.Fatal("status created state")
	}
	for range 2 {
		if err := p.Set(true); err != nil {
			t.Fatal(err)
		}
	}
	status, err = p.Status()
	if err != nil || !status.Enabled || !status.Configured || !status.Owned || len(status.Conflicts) != 0 {
		t.Fatalf("enabled status: %+v, %v", status, err)
	}
	file, marker := p.files()
	data, _ := os.ReadFile(file)
	if string(data) != geolocationPolicyData {
		t.Fatalf("wrong policy: %s", data)
	}
	for _, path := range []string{file, marker} {
		info, err := os.Stat(path)
		if err != nil || info.Mode().Perm() != 0o644 {
			t.Fatalf("policy mode: %s, %v", path, err)
		}
	}
	info, _ := os.Stat(p.StateFile)
	if info.Mode().Perm() != 0o600 {
		t.Fatal("choice is not private")
	}
	os.Remove(file)
	status, err = p.Status()
	if err != nil || !status.Enabled || status.Configured || !status.Owned {
		t.Fatalf("missing policy: %+v, %v", status, err)
	}
	if err := p.Set(true); err != nil {
		t.Fatal("explicit repair:", err)
	}
	if err := p.Cleanup(); err != nil {
		t.Fatal(err)
	}
	status, err = p.Status()
	if err != nil || !status.Enabled || status.Configured || status.Owned {
		t.Fatalf("cleanup must retain choice: %+v, %v", status, err)
	}
	for range 2 {
		if err := p.Set(false); err != nil {
			t.Fatal(err)
		}
	}
	status, err = p.Status()
	if err != nil || status.Enabled || len(status.Policies) != 0 {
		t.Fatalf("disabled status: %+v, %v", status, err)
	}
}

func TestChromeGeolocationExternalPolicies(t *testing.T) {
	for _, test := range []struct {
		name, data string
		conflict   bool
		configured bool
	}{
		{"matching", geolocationPolicyData, false, true},
		{"allow", `{"DefaultGeolocationSetting":1}`, true, false},
		{"ask", `{"DefaultGeolocationSetting":3}`, true, false},
		{"wrong type", `{"DefaultGeolocationSetting":"2"}`, true, false},
		{"null default", `{"DefaultGeolocationSetting":null}`, true, false},
		{"allowed exception", `{"GeolocationAllowedForUrls":["https://example.com"]}`, true, false},
		{"blocking with exception", `{"DefaultGeolocationSetting":2,"GeolocationAllowedForUrls":["*"]}`, true, true},
		{"invalid exception", `{"GeolocationAllowedForUrls":"*"}`, true, false},
		{"null exception", `{"GeolocationAllowedForUrls":null}`, true, false},
		{"empty exception", `{"GeolocationAllowedForUrls":[]}`, false, false},
		{"blocked sites", `{"GeolocationBlockedForUrls":["https://example.com"]}`, false, false},
		{"invalid blocked list", `{"GeolocationBlockedForUrls":1}`, true, false},
		{"null blocked entry", `{"GeolocationBlockedForUrls":[null]}`, true, false},
		{"unrelated", `{"OtherPolicy":true}`, false, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			p := geolocationFixture(t)
			path := filepath.Join(p.PolicyDir, "external.json")
			writeGeolocationFixture(t, path, test.data)
			status, err := p.Status()
			if err != nil || (len(status.Conflicts) != 0) != test.conflict || status.Configured != test.configured {
				t.Fatalf("status: %+v, %v", status, err)
			}
			if err := p.Set(true); (err != nil) != test.conflict {
				t.Fatalf("enable: %v", err)
			}
			if test.conflict {
				if _, err := os.Stat(p.StateFile); !os.IsNotExist(err) {
					t.Fatal("failed enable saved a choice")
				}
			} else if test.configured {
				owned, err := p.owned()
				if err != nil || owned {
					t.Fatal("adopted external policy")
				}
			}
			if err := p.Set(false); err != nil {
				t.Fatal(err)
			}
			data, _ := os.ReadFile(path)
			if string(data) != test.data {
				t.Fatal("external policy changed")
			}
		})
	}
}

func TestChromeGeolocationOffFailureAndRetry(t *testing.T) {
	p := geolocationFixture(t)
	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
	run := p.Run
	p.Run = func(string, ...string) (string, error) { return "", errors.New("fixture sudo denied") }
	if err := p.Set(false); err == nil {
		t.Fatal("cleanup failure reported success")
	}
	status, err := p.Status()
	if err != nil || status.Enabled || !status.Owned || !status.Configured {
		t.Fatalf("failed cleanup lost choice or ownership: %+v, %v", status, err)
	}
	p.Run = run
	if err := p.Set(false); err != nil {
		t.Fatal(err)
	}
}

func TestChromeGeolocationHelperRechecksCleanup(t *testing.T) {
	for _, replacement := range []string{"changed", "symlink", "marker"} {
		t.Run(replacement, func(t *testing.T) {
			p := geolocationFixture(t)
			if err := p.Set(true); err != nil {
				t.Fatal(err)
			}
			file, marker := p.files()
			run := p.Run
			p.Run = func(name string, args ...string) (string, error) {
				switch replacement {
				case "changed":
					writeGeolocationFixture(t, file, `{"DefaultGeolocationSetting":2,"OtherPolicy":true}`)
				case "symlink":
					target := filepath.Join(t.TempDir(), "target")
					writeGeolocationFixture(t, target, geolocationPolicyData)
					os.Remove(file)
					if err := os.Symlink(target, file); err != nil {
						t.Fatal(err)
					}
				case "marker":
					writeGeolocationFixture(t, marker, "foreign\n")
				}
				return run(name, args...)
			}
			err := p.Set(false)
			if (err != nil) != (replacement != "changed") {
				t.Fatalf("recheck error: %v", err)
			}
			if _, err := os.Lstat(file); err != nil {
				t.Fatal("changed policy was removed")
			}
			if replacement != "changed" {
				if _, err := os.Stat(marker); err != nil {
					t.Fatal("failed cleanup discarded ownership")
				}
			}
		})
	}
}

func TestChromeGeolocationRefusesUnsafeOrInvalidFiles(t *testing.T) {
	for _, kind := range []string{"unowned", "invalid marker", "invalid JSON", "null JSON", "symlink", "directory symlink", "empty directory symlink", "state directory symlink"} {
		t.Run(kind, func(t *testing.T) {
			p := geolocationFixture(t)
			file, marker := p.files()
			switch kind {
			case "unowned":
				writeGeolocationFixture(t, file, geolocationPolicyData)
			case "invalid marker":
				writeGeolocationFixture(t, marker, "invalid\n")
			case "invalid JSON":
				writeGeolocationFixture(t, filepath.Join(p.PolicyDir, "external.json"), "{")
			case "null JSON":
				writeGeolocationFixture(t, filepath.Join(p.PolicyDir, "external.json"), "null")
			case "symlink":
				writeGeolocationFixture(t, file, geolocationPolicyData)
				if err := os.Symlink(file, filepath.Join(p.PolicyDir, "external.json")); err != nil {
					t.Fatal(err)
				}
			case "directory symlink", "empty directory symlink", "state directory symlink":
				root, err := filepath.EvalSymlinks(t.TempDir())
				if err != nil {
					t.Fatal(err)
				}
				if kind == "directory symlink" {
					writeGeolocationFixture(t, filepath.Join(root, "external.json"), geolocationPolicyData)
				}
				link := p.PolicyDir
				if kind == "state directory symlink" {
					link = filepath.Dir(p.StateFile)
				}
				if err := os.Symlink(root, link); err != nil {
					t.Fatal(err)
				}
			}
			p.Run = func(string, ...string) (string, error) {
				t.Fatal("unsafe files reached sudo")
				return "", nil
			}
			if err := p.Set(true); err == nil {
				t.Fatal("unsafe enable succeeded")
			}
		})
	}
}

func TestChromeGeolocationDoesNotSaveOnAfterSetupFailure(t *testing.T) {
	for _, failure := range []string{"sudo denied", "no publication", "new conflict", "choice write"} {
		t.Run(failure, func(t *testing.T) {
			p := geolocationFixture(t)
			run := p.Run
			p.Run = func(name string, args ...string) (string, error) {
				switch failure {
				case "sudo denied":
					return "", errors.New("fixture sudo denied")
				case "no publication":
					return "", nil
				case "new conflict":
					writeGeolocationFixture(t, filepath.Join(p.PolicyDir, "external.json"), `{"GeolocationAllowedForUrls":["*"]}`)
				case "choice write":
					if err := os.Mkdir(p.StateFile, 0o700); err != nil {
						t.Fatal(err)
					}
				}
				return run(name, args...)
			}
			if err := p.Set(true); err == nil {
				t.Fatal("setup failure reported success")
			}
			if failure == "choice write" {
				os.Remove(p.StateFile) // Remove only this fixture's injected obstruction.
			}
			if _, err := os.Stat(p.StateFile); !os.IsNotExist(err) {
				t.Fatal("failed setup saved on")
			}
			if failure == "new conflict" || failure == "choice write" {
				owned, err := p.owned()
				if err != nil || !owned {
					t.Fatal("failed setup discarded ownership")
				}
			}
			p.Run = run
			if err := p.Set(false); err != nil {
				t.Fatal("cannot recover failed setup:", err)
			}
		})
	}
}

func TestChromeGeolocationExplicitChoiceRepairsDamagedMode(t *testing.T) {
	p := geolocationFixture(t)
	writeGeolocationFixture(t, p.StateFile, "invalid\n")
	if _, err := p.Status(); err == nil {
		t.Fatal("invalid choice accepted")
	}
	if err := p.Set(true); err != nil {
		t.Fatal(err)
	}
	if status, err := p.Status(); err != nil || !status.Enabled {
		t.Fatalf("choice not repaired: %+v, %v", status, err)
	}
}

func TestChromeGeolocationIsLinuxOnly(t *testing.T) {
	for _, goos := range []string{"darwin", "windows"} {
		if _, err := NewChromeGeolocation(goos, "", nil); err == nil || !strings.Contains(err.Error(), "Linux only") {
			t.Fatalf("%s: %v", goos, err)
		}
	}
}
