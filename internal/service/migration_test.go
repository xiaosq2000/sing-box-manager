package service

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLaunchdRetirementDoesNotKillAnOlderActiveRefresh(t *testing.T) {
	for _, own := range []bool{false, true} {
		t.Run(fmt.Sprint(own), func(t *testing.T) {
			r := &recorder{}
			pid := os.Getpid()
			if !own {
				pid++
			}
			l := &Launchd{Dir: t.TempDir(), UID: 501, Run: r.run, Read: func(string, ...string) (string, error) { return fmt.Sprintf("state = running\n pid = %d\n", pid), nil }}
			path := filepath.Join(l.Dir, refreshLabel+".plist")
			os.WriteFile(path, []byte("legacy task"), 0o644)
			l.Run = func(name string, args ...string) error {
				if _, err := os.Stat(path); !os.IsNotExist(err) {
					t.Fatal("bootout can kill this process before it removes its definition")
				}
				return r.run(name, args...)
			}
			if err := l.RetireRefresh(); err != nil {
				t.Fatal(err)
			}
			if got := strings.Contains(strings.Join(r.calls, "\n"), "bootout"); got != own {
				t.Fatalf("retired the wrong active process: %v", r.calls)
			}
			if _, err := os.Stat(path); !os.IsNotExist(err) {
				t.Fatal("old task can load again at login")
			}
			if !own {
				if err := l.retireRefresh(true); err != nil {
					t.Fatal(err)
				}
				if len(r.calls) == 0 {
					t.Fatal("explicit uninstall did not stop the old task")
				}
			}
		})
	}
}
