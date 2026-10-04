package docker

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// What the bash client's `proxy docker on` wrote for port 1085.
const bashClientDropIn = `[Service]
Environment="http_proxy=http://127.0.0.1:1085"
Environment="https_proxy=http://127.0.0.1:1085"
Environment="no_proxy=localhost,127.0.0.0/8,::1,host.docker.internal"
Environment="HTTP_PROXY=http://127.0.0.1:1085"
Environment="HTTPS_PROXY=http://127.0.0.1:1085"
Environment="NO_PROXY=localhost,127.0.0.0/8,::1,host.docker.internal"
`

// fakeSudo carries out file commands in a test directory and logs the rest.
type fakeSudo struct {
	calls []string
}

func (f *fakeSudo) run(input, name string, args ...string) error {
	f.calls = append(f.calls, name+" "+strings.Join(args, " "))
	if name != "sudo" {
		return nil
	}
	switch args[0] {
	case "mkdir":
		return os.MkdirAll(args[2], 0o755)
	case "tee":
		return os.WriteFile(args[1], []byte(input), 0o644)
	case "rm":
		return os.Remove(args[2])
	}
	return nil
}

func TestNewNeedsLinuxAndADockerService(t *testing.T) {
	if _, err := New("darwin", nil); !errors.Is(err, ErrUnsupported) {
		t.Errorf("macOS: %v", err)
	}
	missing := func(string, string, ...string) error { return errors.New("no such unit") }
	if _, err := New("linux", missing); !errors.Is(err, ErrNotInstalled) {
		t.Errorf("no Docker: %v", err)
	}
}

func TestTheBashClientsDropInCountsAsOurs(t *testing.T) {
	if Content(1085) != bashClientDropIn {
		t.Errorf("content:\n%s", Content(1085))
	}
	path := filepath.Join(t.TempDir(), "http-proxy.conf")
	daemon := &Daemon{Path: path}
	if state, _ := daemon.State(1085); state != Off {
		t.Errorf("no file: %v", state)
	}
	// The bash client's uninstaller compared the file without its last newline.
	os.WriteFile(path, []byte(strings.TrimSuffix(bashClientDropIn, "\n")), 0o644)
	if state, _ := daemon.State(1085); state != On {
		t.Errorf("the bash client's file: %v", state)
	}
	if state, _ := daemon.State(1080); state != Stale {
		t.Errorf("our file for another port: %v", state)
	}
	os.WriteFile(path, []byte("[Service]\nEnvironment=\"HTTPS_PROXY=http://proxy.corp:3128\"\n"), 0o644)
	if state, _ := daemon.State(1085); state != Other {
		t.Errorf("someone else's file: %v", state)
	}
}

func TestOnAndOffRestartTheDaemon(t *testing.T) {
	sudo := &fakeSudo{}
	path := filepath.Join(t.TempDir(), "docker.service.d", "http-proxy.conf")
	daemon := &Daemon{Path: path, Run: sudo.run}

	if err := daemon.On(2080); err != nil {
		t.Fatal(err)
	}
	if state, _ := daemon.State(2080); state != On {
		t.Errorf("after on: %v", state)
	}
	if err := daemon.Off(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("after off the drop-in is still there: %v", err)
	}
	want := []string{
		"sudo mkdir -p " + filepath.Dir(path),
		"sudo tee " + path,
		"sudo systemctl daemon-reload",
		"sudo systemctl restart docker",
		"sudo rm -f " + path,
		"sudo systemctl daemon-reload",
		"sudo systemctl restart docker",
	}
	if strings.Join(sudo.calls, "\n") != strings.Join(want, "\n") {
		t.Errorf("calls:\n%s", strings.Join(sudo.calls, "\n"))
	}
}
