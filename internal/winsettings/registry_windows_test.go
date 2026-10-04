package winsettings

import (
	"fmt"
	"os"
	"os/exec"
	"reflect"
	"testing"
	"time"
)

// Native tests use a disposable HKCU key, never the user's environment or proxy.
func TestNativeRegistryPreservesUnicodeTypesAndRollsBack(t *testing.T) {
	key := `Software\sbc-tests\` + fmt.Sprintf("%d-%d", os.Getpid(), time.Now().UnixNano())
	t.Cleanup(func() {
		if output, err := exec.Command("reg.exe", "delete", `HKCU\`+key, "/f").CombinedOutput(); err != nil {
			t.Errorf("cleanup: %v %s", err, output)
		}
	})
	registry := PowerShell{}
	original := map[string]Value{
		"A":       {Text: "用户 ' ; $(throw 'bad')", Kind: "String"},
		"Path":    {Text: `%USERPROFILE%\工具`, Kind: "ExpandString"},
		"Enabled": {Text: "1", Kind: "DWord"},
	}
	if err := registry.Apply(key, original, ""); err != nil {
		t.Fatal(err)
	}
	names := []string{"A", "Path", "Enabled", "Missing"}
	values, err := registry.Read(key, names)
	if err != nil || !reflect.DeepEqual(values, original) {
		t.Fatalf("read: %+v %v", values, err)
	}
	if err := registry.Apply(key, map[string]Value{"A": {Text: "changed", Kind: "String"}, "Z": {Text: "invalid", Kind: "InvalidKind"}}, ""); err == nil {
		t.Fatal("invalid write succeeded")
	}
	values, err = registry.Read(key, names)
	if err != nil || !reflect.DeepEqual(values, original) {
		t.Fatalf("rollback: %+v %v", values, err)
	}
	if err := registry.Apply(key, map[string]Value{"A": {}}, ""); err != nil {
		t.Fatal(err)
	}
	values, err = registry.Read(key, []string{"A"})
	if err != nil || len(values) != 0 {
		t.Fatalf("delete: %+v %v", values, err)
	}
}
