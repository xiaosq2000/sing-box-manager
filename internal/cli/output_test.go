package cli

import (
	"strings"
	"testing"
)

func TestHumanSelectionOutputSeparatesDataFromRows(t *testing.T) {
	h := newHarness(t)
	value := "naive\nRoute: direct\r\t\x1b]52;c;ZmFrZQ==\a\u202e"
	h.api.protocols = []string{"trojan", value}
	if code := Run(h.env, []string{"protocol", value}); code != 0 {
		t.Fatal(code, h.errOut)
	}
	if code := Run(h.env, []string{"status"}); code != 0 {
		t.Fatal(code, h.errOut)
	}
	text := h.out.String()
	if h.api.protocol != value || h.api.mode != "china" {
		t.Fatal("display changed the selected API value or route")
	}
	if strings.Contains(text, "\nRoute: direct") || strings.ContainsAny(text, "\x1b\r\t\u202e") {
		t.Fatalf("unsafe human output: %q", text)
	}

	f := newRuntimeFixture(t)
	f.api.protocol = value
	f.api.mode = "gfw\nRoute: direct"
	if code := Run(f.h.env, []string{"status"}); code != 0 {
		t.Fatal(code, f.h.errOut)
	}
	if strings.Contains(f.h.out.String(), "\nRoute: direct") {
		t.Fatalf("unsafe TUN status: %q", f.h.out)
	}
	f.untouched(t)
}

func TestStaticChoicesAlwaysShowCurrentValue(t *testing.T) {
	for _, what := range []string{"route", "protocol"} {
		h := newHarness(t)
		current := "retained-" + what
		if what == "route" {
			h.api.mode = current
		} else {
			h.api.protocol = current
		}
		h.env.Stdin = forbiddenInput{t}
		if code := Run(h.env, []string{what}); code != 0 {
			t.Fatal(code, h.errOut)
		}
		if !strings.Contains(h.out.String(), current) || !strings.Contains(h.out.String(), "╭") {
			t.Fatalf("missing current setting: %s", h.out)
		}
		if len(h.svc.calls) != 0 {
			t.Fatal("read-only query touched services")
		}
	}
}
