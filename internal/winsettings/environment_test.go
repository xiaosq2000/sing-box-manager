package winsettings

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

type memoryRegistry struct {
	values map[string]Value
	err    error
}

func (m *memoryRegistry) Read(_ string, _ []string) (map[string]Value, error) {
	copy := map[string]Value{}
	for name, value := range m.values {
		copy[name] = value
	}
	return copy, nil
}
func (m *memoryRegistry) Apply(_ string, changes map[string]Value, _ string) error {
	if m.err != nil {
		return m.err
	}
	for name, value := range changes {
		if value.Kind == "" {
			delete(m.values, name)
		} else {
			m.values[name] = value
		}
	}
	return nil
}

func TestEnvironmentOnMoveOffPreservesForeignEdits(t *testing.T) {
	registry := &memoryRegistry{values: map[string]Value{"Path": {"%USERPROFILE%\\bin", "ExpandString"}}}
	env := &Environment{Registry: registry}
	if err := env.On(1080, "name@x", "p'ass;$(bad)"); err != nil {
		t.Fatal(err)
	}
	want := "http://name%40x:p%27ass;$%28bad%29@127.0.0.1:1080"
	if got := registry.values["HTTP_PROXY"].Text; got != want {
		t.Fatalf("escaped URL %q", got)
	}
	// Repeat installs do not lose unrelated variables.
	if err := env.On(1080, "name@x", "p'ass;$(bad)"); err != nil {
		t.Fatal(err)
	}
	registry.values["HTTPS_PROXY"] = Value{"http://other:3128", "String"}
	if err := env.Move(1080, 2080, "name@x", "p'ass;$(bad)"); err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(registry.values["HTTP_PROXY"].Text, ":2080") {
		t.Fatal("port did not move")
	}
	if err := env.Move(2080, 0, "name@x", "p'ass;$(bad)"); err != nil {
		t.Fatal(err)
	}
	if _, ok := registry.values["HTTP_PROXY"]; ok {
		t.Fatal("off left our variable")
	}
	if registry.values["HTTPS_PROXY"].Text != "http://other:3128" || registry.values["Path"].Text != `%USERPROFILE%\bin` {
		t.Fatal("off changed unrelated settings")
	}
}

func TestEnvironmentRefusesForeignSettingsWithoutPartialWrites(t *testing.T) {
	for _, name := range []string{"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"} {
		t.Run(name, func(t *testing.T) {
			registry := &memoryRegistry{values: map[string]Value{name: {"foreign", "String"}}}
			before, _ := registry.Read("", nil)
			if err := (&Environment{Registry: registry}).On(1080, "", ""); err == nil {
				t.Fatal("foreign setting overwritten")
			}
			if !reflect.DeepEqual(before, registry.values) {
				t.Fatal("partial write")
			}
		})
	}
}

func TestEnvironmentWriteFailureAndOffWithoutOwnership(t *testing.T) {
	registry := &memoryRegistry{values: map[string]Value{"NO_PROXY": {"localhost,127.0.0.0/8,::1,host.docker.internal", "String"}}}
	env := &Environment{Registry: registry}
	if err := env.Move(1080, 0, "", ""); err != nil {
		t.Fatal(err)
	}
	if registry.values["NO_PROXY"].Text == "" {
		t.Fatal("removed an unowned bypass list")
	}
	registry.err = errors.New("denied")
	if err := env.On(1080, "", ""); !errors.Is(err, registry.err) {
		t.Fatalf("lost write error: %v", err)
	}
}

func TestPathPreservesRawValuesAndRemovesOnlySBC(t *testing.T) {
	original := Value{`%USERPROFILE%\bin;C:\Tools;C:\sbc\cli-other`, "ExpandString"}
	registry := &memoryRegistry{values: map[string]Value{"Path": original}}
	env := &Environment{Registry: registry}
	for range 2 {
		if err := env.Path(`C:\sbc\cli`, true); err != nil {
			t.Fatal(err)
		}
	}
	if value := registry.values["Path"]; value.Kind != "ExpandString" || value.Text != original.Text+`;C:\sbc\cli` {
		t.Fatalf("path: %+v", value)
	}
	if err := env.Path(`c:/SBC/cli/`, false); err != nil {
		t.Fatal(err)
	}
	if registry.values["Path"] != original {
		t.Fatal("uninstall lost the original PATH")
	}
}

func TestPowerShellEnvEscapesValuesAndCanClearTheCurrentSession(t *testing.T) {
	code := PowerShellEnv(1080, "u", "'\n; bad", true)
	if strings.Contains(code, "export ") || !strings.Contains(code, "$env:HTTPS_PROXY = 'http://u:%27%0A;%20bad@127.0.0.1:1080'") {
		t.Fatal(code)
	}
	if code := PowerShellEnv(0, "", "", false); !strings.Contains(code, "Remove-Item Env:HTTP_PROXY") {
		t.Fatal(code)
	}
}

func TestReinstallCanChangeOurCredentialsAndKeepsForeignEdits(t *testing.T) {
	old := ProxyValues(1080, "user", "old")
	registry := &memoryRegistry{values: map[string]Value{}}
	env := &Environment{Registry: registry}
	if err := env.Enable(old, nil); err != nil {
		t.Fatal(err)
	}
	if err := env.Enable(ProxyValues(1080, "", ""), old); err != nil {
		t.Fatal(err)
	}
	if registry.values["HTTP_PROXY"].Text != "http://127.0.0.1:1080" {
		t.Fatal("old credentials remain")
	}
	registry.values["HTTP_PROXY"] = Value{"http://foreign:3128", "String"}
	if err := env.Enable(ProxyValues(1080, "new", "secret"), old); err == nil {
		t.Fatal("reinstall overwrote foreign edits")
	}
}
