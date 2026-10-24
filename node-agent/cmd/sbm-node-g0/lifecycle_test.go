package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/accounting"
	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/contract"
)

type lifecycleFixture struct {
	state, engine, properties string
	identity                  contract.Identity
	bundle                    contract.Bundle
	receipt                   startupReceipt
}

func put(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
}

func putJSON(t *testing.T, path string, value any) {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	put(t, path, data)
}

func newLifecycleFixture(t *testing.T) lifecycleFixture {
	t.Helper()
	state := t.TempDir()
	engine, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	identity := contract.Identity{NodeID: contract.UUID(), SourceID: contract.UUID(), Origin: "https://control.fixture.test", Secret: strings.Repeat("a", 64)}
	config := json.RawMessage(`{"log":{"level":"error"}}`)
	bundle := contract.Bundle{Schema: 1, NodeID: identity.NodeID, Generation: 1, Config: config, ConfigSHA256: contract.Hash(config), StatsAddress: "127.0.0.1:19080", Roster: []contract.Entry{{CredentialID: contract.UUID(), WireName: "trojan:alice"}}}
	dir := filepath.Join(state, "generations", "1")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	put(t, filepath.Join(dir, "config.json"), config)
	putJSON(t, filepath.Join(dir, "bundle.json"), bundle)
	if err := os.Symlink(dir, filepath.Join(state, "current")); err != nil {
		t.Fatal(err)
	}
	receipt := startupReceipt{1, bundle.ConfigSHA256, strings.Repeat("a", 32), os.Getpid()}
	putJSON(t, filepath.Join(state, "running.json"), receipt)
	properties := filepath.Join(state, "properties")
	put(t, properties, []byte(fmt.Sprintf("ActiveState=active\nMainPID=%d\nInvocationID=%s\n", receipt.PID, receipt.Invocation)))
	bin := filepath.Join(state, "bin")
	if err := os.Mkdir(bin, 0700); err != nil {
		t.Fatal(err)
	}
	// The failure path can publish a generation but cannot start an engine.
	script := "#!/bin/sh\ncase \"$1\" in\nshow) case \"$*\" in *--value*) echo 0;; *) cat \"$SBM_TEST_PROPERTIES\";; esac;;\nstop) : > \"$SBM_TEST_PROPERTIES.stopped\";;\nstart) exit 1;;\n*) exit 1;;\nesac\n"
	path := filepath.Join(bin, "systemctl")
	put(t, path, []byte(script))
	if err := os.Chmod(path, 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("SBM_TEST_PROPERTIES", properties)
	return lifecycleFixture{state, engine, properties, identity, bundle, receipt}
}

func TestLifecycleLockExcludesOtherWritersAndHonorsCancellation(t *testing.T) {
	state := t.TempDir()
	lock, err := tryLock(state, "lifecycle.lock")
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if second, err := tryLock(state, "lifecycle.lock"); !errors.Is(err, errLocked) {
		if second != nil {
			second.Close()
		}
		t.Fatalf("second writer accepted: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, err := lifecycleLock(ctx, state); err == nil || err.Error() != "lifecycle lock wait expired" {
		t.Fatalf("wait did not expire: %v", err)
	}
	lock.Close()
	if _, err := lifecycleLock(ctx, state); err == nil {
		t.Fatal("expired context acquired a lock")
	}
	fresh, err := lifecycleLock(context.Background(), state)
	if err != nil {
		t.Fatal(err)
	}
	fresh.Close()
}

func TestRunningIdentityChecksReceiptDiskAndRealExecutable(t *testing.T) {
	for _, fault := range []string{"none", "digest", "generation", "invocation", "pid", "inactive", "zero-invocation", "executable", "config", "pointer"} {
		t.Run(fault, func(t *testing.T) {
			f := newLifecycleFixture(t)
			switch fault {
			case "digest":
				f.receipt.Digest = strings.Repeat("0", 64)
			case "generation":
				f.receipt.Generation++
			case "invocation":
				f.receipt.Invocation = strings.Repeat("b", 32)
			case "pid":
				f.receipt.PID++
			case "inactive":
				put(t, f.properties, []byte("ActiveState=activating\n"))
			case "zero-invocation":
				f.receipt.Invocation = strings.Repeat("0", 32)
				put(t, f.properties, []byte(fmt.Sprintf("ActiveState=active\nMainPID=%d\nInvocationID=%s\n", f.receipt.PID, f.receipt.Invocation)))
			case "executable":
				f.engine = "/bin/sh"
			case "config":
				put(t, filepath.Join(f.state, "generations/1/config.json"), []byte("{}"))
			case "pointer":
				os.Remove(filepath.Join(f.state, "current"))
				if err := os.Symlink(t.TempDir(), filepath.Join(f.state, "current")); err != nil {
					t.Fatal(err)
				}
			}
			putJSON(t, filepath.Join(f.state, "running.json"), f.receipt)
			invocation, err := observeRunning(context.Background(), f.state, f.engine, "sbm-g0-proxy.service", f.bundle)
			if fault == "none" {
				if err != nil || invocation != f.receipt.Invocation {
					t.Fatalf("valid engine rejected: %v", err)
				}
			} else if err == nil {
				t.Fatal("unconfirmed engine accepted")
			}
		})
	}
}

func TestBusyLifecycleDoesNotStopEngineOrPublish(t *testing.T) {
	f := newLifecycleFixture(t)
	lock, err := tryLock(f.state, "lifecycle.lock")
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	candidate := f.bundle
	candidate.Generation++
	if err := apply(ctx, f.state, f.engine, "sbm-g0-proxy.service", candidate); err == nil || err.Error() != "lifecycle lock wait expired" {
		t.Fatalf("busy apply: %v", err)
	}
	if _, err := os.Stat(f.properties + ".stopped"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("busy apply stopped engine")
	}
	b, _, err := currentBundle(f.state, f.identity.NodeID)
	if err != nil || b.Generation != 1 {
		t.Fatal("busy apply changed policy")
	}
}

func TestFailedStartupKeepsPublishedPolicyAndRejectsOlderGeneration(t *testing.T) {
	f := newLifecycleFixture(t)
	engine := filepath.Join(f.state, "engine")
	put(t, engine, []byte("#!/bin/sh\nexit 0\n"))
	if err := os.Chmod(engine, 0700); err != nil {
		t.Fatal(err)
	}
	candidate := f.bundle
	candidate.Generation++
	client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		data, _ := json.Marshal(candidate)
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(string(data)))}, nil
	})}
	store, err := accounting.Open(filepath.Join(f.state, "traffic.sqlite3"), f.identity.SourceID)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	current := &f.bundle
	if err := cycle(context.Background(), client, f.identity, f.state, engine, "sbm-g0-proxy.service", store, &current); err == nil || err.Error() != "published engine startup failed" {
		t.Fatalf("failed start: %v", err)
	}
	if current.Generation != 2 {
		t.Fatal("cache retained older policy")
	}
	published, _, err := currentBundle(f.state, f.identity.NodeID)
	if err != nil || published.Generation != 2 {
		t.Fatal("disk retained older policy")
	}
	candidate.Generation = 1
	if err := cycle(context.Background(), client, f.identity, f.state, engine, "sbm-g0-proxy.service", store, &current); err == nil || err.Error() != "generation conflict" {
		t.Fatalf("older policy accepted: %v", err)
	}
}

func TestExecuteEngineReleasesLockAtExecAndUsesImmutableConfig(t *testing.T) {
	f := newLifecycleFixture(t)
	f.bundle.Config = json.RawMessage("{\n  \"password\": \"fixture&<>secret\"\n}\n")
	f.bundle.ConfigSHA256 = contract.Hash(f.bundle.Config)
	put(t, filepath.Join(f.state, "generations/1/config.json"), f.bundle.Config)
	putJSON(t, filepath.Join(f.state, "generations/1/bundle.json"), f.bundle)
	engine := filepath.Join(f.state, "engine")
	put(t, engine, []byte("#!/bin/sh\ncase \"$1\" in\ncheck) exit 0;;\nrun) printf '%s\\n' \"$@\" > \"$SBM_TEST_STATE/exec-args\"; exec \"$SBM_TEST_BINARY\" -test.run=TestEngineLockHelper;;\nesac\nexit 1\n"))
	if err := os.Chmod(engine, 0700); err != nil {
		t.Fatal(err)
	}
	putJSON(t, filepath.Join(f.state, "identity.json"), f.identity)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	child := exec.CommandContext(ctx, f.engine, "-test.run=TestExecuteEngineHelper")
	child.Env = append(os.Environ(), "SBM_TEST_EXEC=1", "SBM_TEST_STATE="+f.state, "SBM_TEST_BINARY="+f.engine, "INVOCATION_ID="+f.receipt.Invocation)
	if output, err := child.CombinedOutput(); err != nil {
		t.Fatalf("exec failed: %v %s", err, output)
	}
	var receipt startupReceipt
	if err := readJSON(filepath.Join(f.state, "running.json"), &receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.PID != child.Process.Pid || receipt.Generation != 1 || receipt.Digest != f.bundle.ConfigSHA256 || receipt.Invocation != f.receipt.Invocation {
		t.Fatal("incorrect startup receipt")
	}
	info, _ := os.Stat(filepath.Join(f.state, "running.json"))
	if info.Mode().Perm() != 0600 {
		t.Fatal("receipt is not private")
	}
	args, err := os.ReadFile(filepath.Join(f.state, "exec-args"))
	if err != nil || !strings.HasSuffix(string(args), filepath.Join(f.state, "generations/1/config.json")+"\n") {
		t.Fatal("engine did not receive immutable config")
	}
}

func TestStartupRejectsInvalidInputsBeforePublishingReceipt(t *testing.T) {
	for _, fault := range []string{"invocation", "config", "checker"} {
		t.Run(fault, func(t *testing.T) {
			f := newLifecycleFixture(t)
			t.Setenv("INVOCATION_ID", f.receipt.Invocation)
			engine := filepath.Join(f.state, "engine")
			put(t, engine, []byte("#!/bin/sh\nexit 1\n"))
			if err := os.Chmod(engine, 0700); err != nil {
				t.Fatal(err)
			}
			switch fault {
			case "invocation":
				t.Setenv("INVOCATION_ID", "")
			case "config":
				put(t, filepath.Join(f.state, "generations/1/config.json"), []byte("{}"))
			}
			if err := executeEngine(f.identity, f.state, engine); err == nil {
				t.Fatal("invalid startup accepted")
			}
			var receipt startupReceipt
			if err := readJSON(filepath.Join(f.state, "running.json"), &receipt); err != nil || receipt != f.receipt {
				t.Fatal("invalid startup replaced receipt")
			}
		})
	}
}

func TestExecuteEngineHelper(t *testing.T) {
	if os.Getenv("SBM_TEST_EXEC") != "1" {
		return
	}
	state := os.Getenv("SBM_TEST_STATE")
	var identity contract.Identity
	if err := readJSON(filepath.Join(state, "identity.json"), &identity); err != nil {
		t.Fatal(err)
	}
	if err := executeEngine(identity, state, filepath.Join(state, "engine")); err != nil {
		t.Fatal(err)
	}
	t.Fatal("exec returned")
}

func TestEngineLockHelper(t *testing.T) {
	if os.Getenv("SBM_TEST_EXEC") != "1" {
		return
	}
	lock, err := tryLock(os.Getenv("SBM_TEST_STATE"), "lifecycle.lock")
	if err != nil {
		t.Fatal("engine inherited the lifecycle lock")
	}
	lock.Close()
}
