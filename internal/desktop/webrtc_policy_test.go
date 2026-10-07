package desktop

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

func TestUnixWebRTCContinuesAfterPolicyFailures(t *testing.T) {
	for _, goos := range []string{"linux", "darwin"} {
		for _, enabled := range []bool{true, false} {
			action := "remove"
			if enabled {
				action = "enable"
			}
			t.Run(goos+"/"+action, func(t *testing.T) {
				var p *unixPrivacy
				if goos == "linux" {
					p = linuxFixture(t)
					second := p.policies[0]
					second.location = filepath.Join(second.location, "second")
					if err := os.Mkdir(second.location, 0755); err != nil {
						t.Fatal(err)
					}
					p.policies = append(p.policies, second)
				} else {
					p = fixturePrivacy(t, goos)
					base, err := filepath.EvalSymlinks(t.TempDir())
					if err != nil {
						t.Fatal(err)
					}
					p.policies = []unixPolicy{
						{location: filepath.Join(base, "first"), name: winsettings.WebRtcPolicyName},
						{location: filepath.Join(base, "second"), name: winsettings.EdgeWebRtcPolicyName},
					}
					p.run = (&macDefaultFixture{values: map[string]string{}}).run
				}
				if err := p.enable(); err != nil {
					t.Fatal(err)
				}
				if goos == "linux" && enabled {
					for _, policy := range p.policies {
						file, _ := p.linuxFiles(policy)
						if err := os.Remove(file); err != nil {
							t.Fatal(err)
						}
					}
				}
				profile := filepath.Join(firefoxDataDirs(goos)[0], "test.default")
				if err := os.MkdirAll(profile, 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(profile, "prefs.js"), nil, 0600); err != nil {
					t.Fatal(err)
				}
				if !enabled {
					if err := applyFirefoxWebRTC(goos); err != nil {
						t.Fatal(err)
					}
				}

				failures := []error{errors.New("first policy failed"), errors.New("second policy failed")}
				runner := p.run
				p.run = func(name string, args ...string) (string, error) {
					for i, policy := range p.policies {
						for _, arg := range args {
							if arg == policy.location || arg == policy.location+".plist" {
								return "", failures[i]
							}
						}
					}
					return runner(name, args...)
				}
				if goos == "darwin" {
					if err := p.markMacReload(); err != nil {
						t.Fatal(err)
					}
				}
				var err error
				if enabled {
					err = p.enable()
				} else {
					err = p.remove()
				}
				for _, failure := range failures {
					if !errors.Is(err, failure) {
						t.Fatalf("lost policy failure %v: %v", failure, err)
					}
				}
				_, profileErr := os.Stat(filepath.Join(profile, "user.js"))
				if enabled && profileErr != nil || !enabled && !os.IsNotExist(profileErr) {
					t.Fatalf("policy failures skipped Firefox %s: %v", action, profileErr)
				}
				if goos == "darwin" {
					if _, err := os.Stat(p.StateFile + ".reload"); !os.IsNotExist(err) {
						t.Fatalf("policy failures skipped pending cache refresh: %v", err)
					}
				}
			})
		}
	}
}

func TestLinuxRemainingWebRTCPolicies(t *testing.T) {
	p := linuxFixture(t)
	dir := p.policies[0].location
	files := []struct{ name, data string }{
		{"a-supported.json", `{"WebRtcLocalhostIpHandling":"manual"}`},
		{"b-legacy.json", `{"WebRtcIPHandling":"manual"}`},
		{"c-both.json", `{"WebRtcLocalhostIpHandling":"manual","WebRtcIPHandling":"manual"}`},
		{"d-unrelated.json", `{"OtherPolicy":true}`},
	}
	for _, file := range files {
		if err := os.WriteFile(filepath.Join(dir, file.name), []byte(file.data), 0644); err != nil {
			t.Fatal(err)
		}
	}
	want := []string{filepath.Join(dir, files[0].name), filepath.Join(dir, files[1].name), filepath.Join(dir, files[2].name)}
	got, err := p.Remaining()
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("remaining locations: %v, %v; want %v", got, err, want)
	}
	if err := os.WriteFile(filepath.Join(dir, "e-invalid.json"), []byte("invalid"), 0644); err != nil {
		t.Fatal(err)
	}
	if got, err := p.Remaining(); err == nil || got != nil {
		t.Fatalf("invalid policy returned partial locations: %v, %v", got, err)
	}
}

func TestMacRemainingWebRTCPolicies(t *testing.T) {
	p := fixturePrivacy(t, "darwin")
	domain := filepath.Join(t.TempDir(), "com.microsoft.Edge")
	p.policies = []unixPolicy{{location: domain, name: winsettings.EdgeWebRtcPolicyName}}
	if err := os.WriteFile(domain+".plist", []byte("fixture"), 0644); err != nil {
		t.Fatal(err)
	}
	f := &macDefaultFixture{values: map[string]string{}}
	p.run = f.run
	var want []string
	for _, location := range []string{domain, filepath.Base(domain)} {
		for _, name := range []string{winsettings.EdgeWebRtcPolicyName, winsettings.WebRtcPolicyName} {
			f.values[location+"/"+name] = "manual value"
			want = append(want, location+" / "+name)
		}
	}
	got, err := p.Remaining()
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("remaining locations: %v, %v; want %v", got, err, want)
	}
	f.fail = true
	if got, err := p.Remaining(); err == nil || got != nil || !strings.Contains(err.Error(), "permission denied") {
		t.Fatalf("read failure returned partial locations: %v, %v", got, err)
	}
}
