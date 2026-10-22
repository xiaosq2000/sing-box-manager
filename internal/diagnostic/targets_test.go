package diagnostic

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestTargetValidationPreservesSemanticsWithoutDNS(t *testing.T) {
	for _, value := range []string{"http://fixture.test/", "https://u:p@fixture.test/", "https://127.0.0.1/", "https://[::1]/", "https://[::ffff:127.0.0.1]/", "https://10.0.0.1/", "https://169.254.169.254/", "https://100.64.0.1/", "https://localhost./", "https://fixture.test:0/", "https://fixture.test:99999/", "https://fixture.test/\x1b[31m", "file:///secret", "https://fixture.test/#fragment"} {
		if _, err := ParseTarget(value); err == nil {
			t.Fatalf("accepted unsafe target %q", value)
		}
	}
	first := "https://does-not-resolve.fixture.test/path?signature=fixture-value&x=%2F"
	a, err := ParseTarget(first)
	if err != nil || a.URL != first {
		t.Fatal(a, err)
	}
	b, err := ParseTarget("https://does-not-resolve.fixture.test/other")
	if err != nil || a.ID == b.ID {
		t.Fatal("host deduplication lost distinct endpoints")
	}
	if Host(first) != "does-not-resolve.fixture.test" {
		t.Fatal("report included a path or query")
	}
}

func TestTargetListVersionBoundsAndEmptySemantics(t *testing.T) {
	for _, value := range []string{`{"schema_version":2,"targets":[]}`, `{"schema_version":1}`, `{"schema_version":1,"targets":[],"script":"ignored"}`, `{"schema_version":1,"targets":[]} {}`, strings.Repeat(" ", 65537)} {
		if _, err := DecodeList(strings.NewReader(value)); err == nil {
			t.Fatal("accepted invalid list")
		}
	}
	list, err := DecodeList(strings.NewReader(`{"schema_version":1,"targets":[]}`))
	if err != nil || list.Targets == nil || len(list.Targets) != 0 {
		t.Fatal(list, err)
	}
	list.Targets = []Target{}
	for range MaxTargets + 1 {
		list.Targets = append(list.Targets, Target{ID: "same", URL: "https://fixture.test/"})
	}
	data, _ := json.Marshal(list)
	if _, err := DecodeList(strings.NewReader(string(data))); err == nil {
		t.Fatal("accepted oversized duplicate list")
	}
	defaults, _ := Preset("default")
	for _, target := range defaults.Targets {
		if strings.Contains(target.URL, "chatgpt") || strings.Contains(target.URL, "claude") || strings.Contains(target.URL, "gemini") {
			t.Fatal("default contacts AI")
		}
	}
	copy, _ := Preset("default")
	copy.Targets[0].URL = "changed"
	again, _ := Preset("default")
	if again.Targets[0].URL == "changed" {
		t.Fatal("preset mutated globally")
	}
}
