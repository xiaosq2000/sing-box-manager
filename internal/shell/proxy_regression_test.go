package shell

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFailedSourceDoesNotClaimForeignValues(t *testing.T) {
	for _, name := range shells(t) {
		for _, content := range []string{"return 1\n", "", "# no exports\n", "unreadable"} {
			unreadable := content == "unreadable"
			s := newSetup(t, name)
			if unreadable && os.Geteuid() == 0 {
				continue
			}
			mode := os.FileMode(0o600)
			if unreadable {
				mode = 0
			}
			if err := os.WriteFile(s.env, []byte(content), mode); err != nil {
				t.Fatal(err)
			}
			output := s.run(t, name, `export http_proxy=http://corporate:3128
_sbc_apply
rm `+Quote(s.env)+`
_sbc_apply
printf '%s\n' "$http_proxy"`)
			if output != "http://corporate:3128\n" {
				t.Fatalf("%s: failed sourcing captured a foreign value", name)
			}
		}
	}
}

func TestFailedSourceRetainsThePreviousSuccessfulSnapshot(t *testing.T) {
	for _, name := range shells(t) {
		s := newSetup(t, name)
		os.WriteFile(s.env, []byte(EnvOn(9876, "", "")), 0o600)
		output := s.run(t, name, `export http_proxy=http://corporate:3128
printf 'return 1\n' > `+Quote(s.env)+`
_sbc_apply
rm `+Quote(s.env)+`
_sbc_apply
printf '%s|%s\n' "$http_proxy" "${https_proxy-unset}"`)
		if output != "http://corporate:3128|unset\n" {
			t.Fatalf("%s: failed source changed the successful snapshot", name)
		}
	}
}

func TestAllexportNeverExportsPrivateStateOrFunctions(t *testing.T) {
	for _, name := range shells(t) {
		s := newSetup(t, name)
		state := ProxyState{Known: Values(4321, "sbc", "synthetic-private-value"), ControlDir: filepath.Join(s.dir, "tun-control"), ConfigDir: filepath.Join(s.dir, "tun"), Active: filepath.Join(s.dir, "run", "active")}
		os.MkdirAll(state.ControlDir, 0o755)
		journal := filepath.Join(state.ControlDir, "transaction.json")
		off := `{"id":"","phase":"off","deadline":"0001-01-01T00:00:00Z"}` + "\n"
		os.WriteFile(journal, []byte(off), 0o644)
		os.WriteFile(s.env, []byte(EnvOn(4321, "sbc", "synthetic-private-value")), 0o600)
		s.useProxyState(t, name, state)
		output := s.run(t, name, `
export _sbc_seen_http_proxy=previously-exported _sbc_record=previously-exported
set -a
. `+Quote(s.init)+`
case $- in *a*) ;; *) exit 21 ;; esac
printf '%s' `+Quote(strings.ReplaceAll(off, `"off"`, `"active"`))+` > `+Quote(journal)+`
_sbc_apply
case $- in *a*) ;; *) exit 22 ;; esac
CHECK_ALLEXPORT=preserved
printf 'proxy:%s\n' "${http_proxy-unset}"
command env
set +a
_sbc_apply
case $- in *a*) exit 23 ;; esac`)
		if !strings.HasPrefix(output, "proxy:unset\n") || !strings.Contains(output, "CHECK_ALLEXPORT=preserved") {
			t.Fatalf("%s: proxy cleanup or caller options changed", name)
		}
		if strings.Contains(output, "_sbc_") || strings.Contains(output, "synthetic-private-value") {
			t.Fatalf("%s exported private bookkeeping or functions", name)
		}
	}
}

func TestHookResumesMixedAfterAConfirmedTunStop(t *testing.T) {
	for _, name := range shells(t) {
		s := newSetup(t, name)
		state := ProxyState{Known: Values(4321, "", ""), ControlDir: filepath.Join(s.dir, "tun-control"), ConfigDir: filepath.Join(s.dir, "tun"), Active: filepath.Join(s.dir, "run", "active")}
		os.MkdirAll(state.ControlDir, 0o755)
		os.MkdirAll(state.ConfigDir, 0o750)
		journal := filepath.Join(state.ControlDir, "transaction.json")
		off := `{"id":"0123456789abcdef0123456789abcdef","phase":"off","deadline":"2026-10-22T00:00:00.123Z","committed":true}` + "\n"
		active := strings.ReplaceAll(off, `"off"`, `"active"`)
		os.WriteFile(journal, []byte(active), 0o644)
		state.BlockedRecord = strings.TrimSuffix(active, "\n")
		os.WriteFile(s.env, []byte(EnvOn(4321, "", "")), 0o600)
		s.useProxyState(t, name, state)
		output := s.run(t, name, `printf 'tun:%s\n' "${http_proxy-unset}"
printf '%s' `+Quote(off)+` > `+Quote(journal)+`
_sbc_apply
printf 'mixed:%s\n' "$http_proxy"`)
		if output != "tun:unset\nmixed:http://127.0.0.1:4321\n" {
			t.Fatalf("%s: hook did not follow confirmed shutdown", name)
		}
	}
}
