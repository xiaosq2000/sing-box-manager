package cli

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/diagnostic"
)

func TestDiagnosticOptionErrorsNeverEchoSecretURLs(t *testing.T) {
	env, out, errOut := inertEnv(t)
	secret := "https://example.com/private-fixture-path?token=fixture-secret-value"
	for _, args := range [][]string{
		{"check", "--samples", secret}, {"ip", "--json=" + secret},
		{"speed", "download", "--json=" + secret}, {"check", "--" + secret},
	} {
		out.Reset()
		errOut.Reset()
		if code := Run(env, args); code != 2 {
			t.Fatal(code)
		}
		text := out.String() + errOut.String()
		for _, value := range []string{"private-fixture-path", "fixture-secret-value", "example.com"} {
			if strings.Contains(text, value) {
				t.Fatalf("option error exposed %s: %s", value, text)
			}
		}
	}
}

func TestTargetImportCannotWriteAnUnreadableExpandedList(t *testing.T) {
	h := newHarness(t)
	if code := Run(h.env, []string{"check", "targets", "reset"}); code != 0 {
		t.Fatal(code)
	}
	before, _ := os.ReadFile(targetPath(h.layout))
	list := diagnostic.TargetList{SchemaVersion: 1, Targets: []diagnostic.Target{}}
	for index := range 6 {
		list.Targets = append(list.Targets, diagnostic.Target{ID: fmt.Sprint("target-", index), URL: fmt.Sprintf("https://fixture.test/%d?q=", index) + strings.Repeat("&", 2000)})
	}
	var raw bytes.Buffer
	encoder := json.NewEncoder(&raw)
	encoder.SetEscapeHTML(false)
	encoder.Encode(list)
	if raw.Len() >= diagnostic.MaxListBytes {
		t.Fatal("fixture not a valid bounded import")
	}
	file := targetPath(h.layout) + ".import"
	os.WriteFile(file, raw.Bytes(), 0o600)
	if code := Run(h.env, []string{"check", "targets", "import", file}); code != 2 {
		t.Fatal("expanded save was accepted", code)
	}
	after, _ := os.ReadFile(targetPath(h.layout))
	if !bytes.Equal(before, after) {
		t.Fatal("oversized import replaced the readable list")
	}
	if _, err := diagnostic.ReadList(targetPath(h.layout), false); err != nil {
		t.Fatal(err)
	}
}
