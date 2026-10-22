package shell

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func (s setup) useProxyState(t *testing.T, name string, state ProxyState) {
	t.Helper()
	code, err := Init(name, s.cli, s.env, s.region, s.hint, state)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(s.init, []byte(code), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestInheritedAndManualExportsNeedNoOwnershipMarker(t *testing.T) {
	for _, name := range shells(t) {
		s := newSetup(t, name)
		on := EnvOn(4321, "sbc", "p'@ss")
		s.useProxyState(t, name, ProxyState{Known: Values(4321, "sbc", "p'@ss")})
		for _, script := range []string{
			on + ". " + Quote(s.init) + "\n",
			on + "unset _sbc_applied\n_sbc_apply\n",
		} {
			output := s.run(t, name, script+`printf '%s %s %s\n' "${http_proxy-unset}" "${HTTPS_PROXY-unset}" "${no_proxy-unset}"
export -p`)
			if !strings.HasPrefix(output, "unset unset unset\n") || strings.Contains(output, "_sbc_") {
				t.Fatalf("%s left exports or exported state: %s", name, output)
			}
		}
	}
}

func TestCleanupPreservesChangedValuesAndForeignFallbacks(t *testing.T) {
	for _, name := range shells(t) {
		s := newSetup(t, name)
		if err := os.WriteFile(s.env, []byte(EnvOn(9876, "", "")), 0o600); err != nil {
			t.Fatal(err)
		}
		output := s.run(t, name, `
export HTTP_PROXY=http://corporate:3128
export ALL_PROXY=socks5://other:9999
export no_proxy=private.example
rm `+Quote(s.env)+`
PATH=/no-external-programs
_sbc_apply
printf '%s|%s|%s|%s|%s\n' "${http_proxy-unset}" "$HTTP_PROXY" "$ALL_PROXY" "$no_proxy" "${SOCKS_PROXY-unset}"`)
		if output != "unset|http://corporate:3128|socks5://other:9999|private.example|unset\n" {
			t.Fatalf("%s: %s", name, output)
		}
	}
}

func TestBypassListAloneDoesNotProveOwnership(t *testing.T) {
	for _, name := range shells(t) {
		s := newSetup(t, name)
		output := s.run(t, name, "export no_proxy="+Quote(NoProxy)+"\nexport all_proxy=http://127.0.0.1:4321\n_sbc_apply\nprintf '%s|%s\\n' \"$no_proxy\" \"$all_proxy\"")
		if output != NoProxy+"|http://127.0.0.1:4321\n" {
			t.Fatalf("%s: %s", name, output)
		}
	}
}

func TestPersistentParentNeedsItsOwnEnvironmentCleanup(t *testing.T) {
	for _, name := range shells(t) {
		s := newSetup(t, name)
		executable, err := exec.LookPath(name)
		if err != nil {
			t.Fatal(err)
		}
		child := Quote(executable)
		if name == "zsh" {
			child += " -f"
		} else {
			child += " --noprofile --norc"
		}
		clean := ". " + Quote(s.init) + `; printf 'child:%s\n' "${http_proxy-unset}"`
		stale := `printf 'later:%s\n' "${http_proxy-unset}"`
		output := s.run(t, name, EnvOn(4321, "", "")+child+" -c "+Quote(clean)+"\n"+
			`printf 'parent:%s\n' "$http_proxy"`+"\n"+child+" -c "+Quote(stale)+"\n_sbc_apply\n"+child+" -c "+Quote(clean))
		want := "child:unset\nparent:http://127.0.0.1:4321\nlater:http://127.0.0.1:4321\nchild:unset\n"
		if output != want {
			t.Fatalf("%s: %s", name, output)
		}
	}
}

func TestCaptureMetadataBlocksStaleEnvironmentFiles(t *testing.T) {
	off := `{"id":"0123456789abcdef0123456789abcdef","phase":"off","deadline":"2026-10-22T00:00:00Z","committed":true}` + "\n"
	for _, name := range shells(t) {
		for _, test := range []struct {
			name, journal, marker string
			allow                 bool
		}{
			{"absent", "", "", true},
			{"off", off, "", true},
			{"native refusal", off, "native", false},
			{"off uncommitted", strings.ReplaceAll(off, `,"committed":true`, ""), "", true},
			{"pending", strings.ReplaceAll(off, `"off"`, `"pending"`), "", false},
			{"active", strings.ReplaceAll(off, `"off"`, `"active"`), "", false},
			{"dirty", strings.ReplaceAll(off, `"off"`, `"dirty"`), "", false},
			{"invalid", `{"phase":"off"}` + "\n", "", false},
			{"invalid month", strings.ReplaceAll(off, "2026-10-22", "2026-99-99"), "", false},
			{"invalid leap day", strings.ReplaceAll(off, "2026-10-22", "2026-02-29"), "", false},
			{"invalid hour", strings.ReplaceAll(off, "T00:", "T24:"), "", false},
			{"invalid minute", strings.ReplaceAll(off, "T00:00:", "T00:60:"), "", false},
			{"invalid second", strings.ReplaceAll(off, "00Z", "60Z"), "", false},
			{"leap century", strings.ReplaceAll(off, "2026-10-22", "2000-02-29"), "", true},
			{"non-leap century", strings.ReplaceAll(off, "2026-10-22", "1900-02-29"), "", false},
			{"nano precision", strings.ReplaceAll(off, "00Z", "00.123456789Z"), "", true},
			{"positive offset", strings.ReplaceAll(off, "00Z", "00+08:00"), "", true},
			{"negative offset", strings.ReplaceAll(off, "00Z", "00-07:00"), "", true},
			{"fractional offset", strings.ReplaceAll(off, "00Z", "00.123456789+05:45"), "", true},
			{"invalid offset hour", strings.ReplaceAll(off, "00Z", "00+24:00"), "", false},
			{"invalid offset minute", strings.ReplaceAll(off, "00Z", "00-07:60"), "", false},
			{"extra JSON field", strings.ReplaceAll(off, `"phase":"off"`, `"extra":true,"phase":"off"`), "", false},
			{"extra record", off + off, "", false},
			{"trailing content", off + "broken", "", false},
			{"blocked", off, "blocked", false},
			{"active marker", off, "active", false},
			{"legacy config", "", "config", false},
			{"unreadable journal", off, "unreadable", false},
			{"journal directory", "", "journal-directory", false},
			{"broken marker", off, "broken", false},
		} {
			t.Run(name+"/"+test.name, func(t *testing.T) {
				s := newSetup(t, name)
				state := ProxyState{Known: Values(4321, "", ""), ControlDir: filepath.Join(s.dir, "etc", "sbc", "tun-control"), ConfigDir: filepath.Join(s.dir, "etc", "sbc", "tun"), Active: filepath.Join(s.dir, "run", "sbc-tun", "active")}
				os.MkdirAll(state.ControlDir, 0o755)
				os.MkdirAll(filepath.Dir(state.Active), 0o755)
				if test.journal != "" {
					os.WriteFile(filepath.Join(state.ControlDir, "transaction.json"), []byte(test.journal), 0o644)
				}
				switch test.marker {
				case "blocked":
					os.WriteFile(filepath.Join(state.ControlDir, "blocked"), nil, 0o644)
				case "active":
					os.WriteFile(state.Active, nil, 0o644)
				case "config":
					os.MkdirAll(state.ConfigDir, 0o750)
				case "native":
					state.BlockedRecord = strings.TrimSuffix(test.journal, "\n")
				case "unreadable":
					if os.Geteuid() == 0 {
						t.Skip("root bypasses file read permissions")
					}
					os.Chmod(filepath.Join(state.ControlDir, "transaction.json"), 0)
				case "journal-directory":
					os.Mkdir(filepath.Join(state.ControlDir, "transaction.json"), 0o755)
				case "broken":
					os.Symlink(filepath.Join(s.dir, "missing"), filepath.Join(state.ControlDir, "blocked"))
				}
				os.WriteFile(s.env, []byte(EnvOn(4321, "", "")), 0o600)
				s.useProxyState(t, name, state)
				// Introduce the record after valid initialization, so the native
				// startup snapshot cannot conceal weaknesses in the live parser.
				var mutate string
				if test.journal != "" && test.marker == "" {
					mutate = "printf '%s' " + Quote(test.journal) + " > " + Quote(filepath.Join(state.ControlDir, "transaction.json")) + "\n"
					os.WriteFile(filepath.Join(state.ControlDir, "transaction.json"), []byte(off), 0o644)
				}
				output := s.run(t, name, mutate+`PATH=/no-processes; _sbc_apply; printf '%s\n' "${http_proxy-unset}"`)
				want := "unset\n"
				if test.allow {
					want = "http://127.0.0.1:4321\n"
				}
				if output != want {
					t.Fatalf("%s: got %s", test.name, output)
				}
			})
		}
	}
}

func TestResidueUsesNamesAndConservativeOwnership(t *testing.T) {
	values := Values(4321, "sbc", "synthetic-secret")
	getenv := func(name string) string { return values[name] }
	owned, other := Residue(getenv, values)
	if len(owned) != 10 || len(other) != 0 {
		t.Fatalf("unexpected ownership counts: %d %d", len(owned), len(other))
	}
	owned, other = Residue(func(name string) string {
		if name == "no_proxy" {
			return NoProxy
		}
		return ""
	}, values)
	if len(owned) != 0 || fmt.Sprint(other) != "[no_proxy]" {
		t.Fatalf("generic bypass list proves no ownership: %v %v", owned, other)
	}
}
