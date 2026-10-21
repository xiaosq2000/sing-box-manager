package tun

import (
	"errors"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

const testTransactionID = "0123456789abcdef0123456789abcdef"

type faultRunner struct {
	fakeRunner
	fail   func(string) bool
	output func(string) (string, error)
}

func (r *faultRunner) Run(name string, args ...string) error {
	call := strings.Join(append([]string{filepath.Base(name)}, args...), " ")
	if r.fail != nil && r.fail(call) {
		r.runCalls = append(r.runCalls, name+" "+strings.Join(args, " "))
		return os.ErrPermission
	}
	return r.fakeRunner.Run(name, args...)
}

func (r *faultRunner) Output(name string, args ...string) (string, error) {
	if r.output != nil {
		return r.output(strings.Join(append([]string{filepath.Base(name)}, args...), " "))
	}
	return r.fakeRunner.Output(name, args...)
}

func lifecycleOptions(t *testing.T) (Options, *faultRunner, StopOptions) {
	t.Helper()
	root := t.TempDir()
	p := testPaths(root)
	staging := filepath.Join(root, "staging")
	for _, dir := range []string{staging, filepath.Join(staging, "bin"), filepath.Join(staging, "rules")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	config := filepath.Join(staging, "config.json")
	data := `{"experimental":{"clash_api":{"external_controller":"127.0.0.1:63001","secret":"fixture"}},"route":{"rule_set":[]}}`
	for name, data := range map[string]string{config: data, filepath.Join(staging, "bin", "sing-box"): "fixture binary"} {
		if err := os.WriteFile(name, []byte(data), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	runner := &faultRunner{}
	opts := Options{Paths: p, Config: config, BinDir: filepath.Join(staging, "bin"), RulesDir: filepath.Join(staging, "rules"),
		OwnerUID: 1000, OwnerUser: "alice", Transaction: testTransactionID, Self: filepath.Join(staging, "bin", "sing-box"),
		Runner: runner, Euid: func() int { return 0 }, LookPath: func(name string) (string, error) { return "/usr/sbin/" + name, nil },
		LookupUser:  func(string) (*user.User, error) { return &user.User{Uid: "61000", Gid: "61000"}, nil },
		LookupGroup: func(string) (*user.Group, error) { return &user.Group{Gid: "61000"}, nil },
		Chown:       func(string, int, int) error { return nil }, Verify: func(Paths, Runner, Tools) error { return nil },
	}
	return opts, runner, StopOptions{Paths: p, Runner: runner, Euid: opts.Euid, LookPath: opts.LookPath}
}

func assertPhase(t *testing.T, p Paths, phase string) {
	t.Helper()
	record, err := loadTransaction(p)
	if err != nil || record.Phase != phase {
		t.Fatalf("phase=%#v err=%v, want %s", record, err, phase)
	}
	if phase != "off" && !ActiveAt(p.blocked()) {
		t.Fatal("mixed-mode block is missing")
	}
}

func TestStopAttemptsCaptureCleanupWhenMetadataWritesFail(t *testing.T) {
	for _, kind := range []string{"blocked", "journal"} {
		t.Run(kind, func(t *testing.T) {
			opts, runner, stop := lifecycleOptions(t)
			path := opts.Paths.blocked()
			if kind == "journal" {
				path = opts.Paths.journal()
			}
			if err := os.MkdirAll(path, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := Stop(stop); err == nil {
				t.Fatal("metadata failure reported a clean off state")
			}
			if !strings.Contains(runner.joined(), "disable --now "+ServiceName) {
				t.Fatal("metadata failure prevented the stop attempt")
			}
			if !ActiveAt(opts.Paths.blocked()) {
				t.Fatal("metadata failure removed the mixed-mode block")
			}
		})
	}
}

func TestRecoveryArmFailureRetainsTheUnderlyingError(t *testing.T) {
	opts, runner, _ := lifecycleOptions(t)
	runner.fail = func(call string) bool {
		return strings.HasPrefix(call, "systemd-run ") && strings.Contains(call, "--on-active=")
	}
	if err := Apply(opts); !errors.Is(err, os.ErrPermission) {
		t.Fatalf("timer setup error lost its cause: %v", err)
	}
	assertPhase(t, opts.Paths, "off")
}

func TestStopFailureRetainsDirtyState(t *testing.T) {
	cases := []struct {
		name   string
		fail   func(string) bool
		output func(string) (string, error)
	}{
		{"disable failed", func(s string) bool { return strings.HasPrefix(s, "systemctl disable") }, nil},
		{"state unavailable", nil, func(string) (string, error) { return "", os.ErrPermission }},
		{"process still running", nil, func(string) (string, error) {
			return "LoadState=loaded\nActiveState=active\nMainPID=123\nUnitFileState=disabled\n", nil
		}},
		{"interface remains", nil, func(s string) (string, error) {
			if strings.HasPrefix(s, "ip ") {
				return `[{"ifname":"sbc-tun0"}]`, nil
			}
			return (&fakeRunner{}).Output("systemctl")
		}},
		{"nft table remains", nil, func(s string) (string, error) {
			if strings.HasPrefix(s, "nft ") {
				return `{"nftables":[{"table":{"name":"sing-box"}}]}`, nil
			}
			if strings.HasPrefix(s, "ip ") {
				return "[]", nil
			}
			return (&fakeRunner{}).Output("systemctl")
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			opts, runner, stop := lifecycleOptions(t)
			runner.fail, runner.output = tc.fail, tc.output
			if err := Stop(stop); err == nil {
				t.Fatal("stop failure reported success")
			}
			assertPhase(t, opts.Paths, "dirty")
		})
	}
}

func TestRecoveryStopsPendingButNotCommittedOrDifferentTransactions(t *testing.T) {
	for _, phase := range []string{"pending", "active", "other"} {
		t.Run(phase, func(t *testing.T) {
			opts, runner, stop := lifecycleOptions(t)
			if err := Apply(opts); err != nil {
				t.Fatal(err)
			}
			assertPhase(t, opts.Paths, "pending")
			if phase == "active" {
				if err := Commit(stop, opts.Transaction); err != nil {
					t.Fatal(err)
				}
			}
			id := opts.Transaction
			if phase == "other" {
				id = "1123456789abcdef0123456789abcdef"
			}
			runner.runCalls = nil
			if err := Recover(stop, id); err != nil {
				t.Fatal(err)
			}
			if phase == "pending" {
				assertPhase(t, opts.Paths, "off")
				if ActiveAt(opts.Paths.blocked()) {
					t.Fatal("successful recovery left mixed mode blocked")
				}
			} else if len(runner.runCalls) != 0 {
				t.Fatalf("stale recovery ran commands: %s", runner.joined())
			}
		})
	}
}

func TestExpiredCommitAndRebootCannotActivatePendingGeneration(t *testing.T) {
	opts, runner, stop := lifecycleOptions(t)
	if err := Apply(opts); err != nil {
		t.Fatal(err)
	}
	runner.fail = func(s string) bool { return strings.Contains(s, ".timer") }
	if err := AllowStart(stop); err == nil {
		t.Fatal("pending generation started without recovery timer")
	}
	record, _ := loadTransaction(opts.Paths)
	record.Deadline = time.Now().Add(-time.Second)
	if err := saveTransaction(opts.Paths, record); err != nil {
		t.Fatal(err)
	}
	if err := Commit(stop, record.ID); err == nil {
		t.Fatal("expired commit succeeded")
	}
	if err := AllowStart(stop); err == nil {
		t.Fatal("expired generation started")
	}
	assertPhase(t, opts.Paths, "pending")
}

func TestCandidateDirectoryGrantsServiceGroupTraversal(t *testing.T) {
	opts, _, _ := lifecycleOptions(t)
	candidate := opts.Paths.ConfigDir + ".next-" + opts.Transaction
	granted := false
	opts.Chown = func(path string, uid, gid int) error {
		if path == candidate && uid == 0 && gid == 61000 {
			granted = true
		}
		return nil
	}
	if err := Apply(opts); err != nil {
		t.Fatal(err)
	}
	if !granted {
		t.Fatal("candidate directory did not grant service group traversal")
	}
	info, err := os.Stat(opts.Paths.ConfigDir)
	if err != nil || info.Mode().Perm() != 0o750 {
		t.Fatalf("published directory=%v %v", info, err)
	}
}

func TestCandidateFailurePreservesLiveGeneration(t *testing.T) {
	opts, runner, _ := lifecycleOptions(t)
	if err := os.MkdirAll(opts.Paths.BinaryDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(opts.Paths.Binary, []byte("old binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	runner.fail = func(s string) bool { return strings.Contains(s, " check ") }
	if err := Apply(opts); err == nil {
		t.Fatal("candidate check succeeded")
	}
	data, err := os.ReadFile(opts.Paths.Binary)
	if err != nil || string(data) != "old binary" {
		t.Fatalf("live binary changed: %q %v", data, err)
	}
	if strings.Contains(runner.joined(), "disable --now") {
		t.Fatal("candidate failure stopped live service")
	}
}

func TestVerificationAndRecoveryFailureAreBothReported(t *testing.T) {
	opts, runner, _ := lifecycleOptions(t)
	opts.Verify = func(Paths, Runner, Tools) error {
		runner.fail = func(s string) bool { return strings.Contains(s, "disable --now") }
		return errors.New("fixture verification failed")
	}
	err := Apply(opts)
	if err == nil || !strings.Contains(err.Error(), "fixture verification failed") || !strings.Contains(err.Error(), "stop is unconfirmed") {
		t.Fatalf("error=%v", err)
	}
	assertPhase(t, opts.Paths, "dirty")
}

func TestCompleteGenerationIsRetainedBeforeReplacement(t *testing.T) {
	opts, _, stop := lifecycleOptions(t)
	if err := Apply(opts); err != nil {
		t.Fatal(err)
	}
	if err := Commit(stop, opts.Transaction); err != nil {
		t.Fatal(err)
	}
	old, err := os.ReadFile(opts.Paths.Config)
	if err != nil {
		t.Fatal(err)
	}
	opts.Transaction = "1123456789abcdef0123456789abcdef"
	if err := Apply(opts); err != nil {
		t.Fatal(err)
	}
	backup := opts.Paths.ConfigDir + ".prev-" + opts.Transaction
	data, err := os.ReadFile(filepath.Join(backup, "config.json"))
	if err != nil || string(data) != string(old) {
		t.Fatalf("previous config=%q %v", data, err)
	}
	if _, err := os.Stat(opts.Paths.BinaryDir + ".prev-" + opts.Transaction + "/sing-box"); err != nil {
		t.Fatal(err)
	}
}

func TestCandidateChecksRealLocalSnapshotsWithoutLiveFiles(t *testing.T) {
	binary, err := filepath.Abs("../../.cache/sing-box-manager/bin/sing-box")
	if runtime.GOOS != "linux" {
		t.Skip("native Linux binary check")
	}
	if _, err := os.Stat(binary); err != nil {
		t.Skip("run pixi run fetch-sing-box for the native candidate check")
	}
	if err != nil {
		t.Fatal(err)
	}
	opts, _, _ := lifecycleOptions(t)
	opts.BinDir = filepath.Dir(binary)
	opts.Runner = ExecRunner{}
	source := filepath.Join(opts.RulesDir, "fixture.json")
	snapshot := filepath.Join(opts.RulesDir, "fixture.srs")
	if err := os.WriteFile(source, []byte(`{"version":3,"rules":[{"domain":["fixture.test"]}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	command := exec.Command(binary, "rule-set", "compile", "--output", snapshot, source)
	command.Env = append(os.Environ(), "LD_LIBRARY_PATH="+opts.BinDir)
	if out, err := command.CombinedOutput(); err != nil {
		t.Fatalf("compile snapshot: %v %s", err, out)
	}
	data := []byte(`{"experimental":{"clash_api":{"external_controller":"127.0.0.1:63001","secret":"fixture"}},"route":{"rule_set":[{"type":"local","tag":"fixture","format":"binary","path":"/missing/live/fixture.srs"}]}}`)
	if err := os.WriteFile(opts.Config, data, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(opts.Paths.StateDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := prepareGeneration(opts, Tools{}, 61000, opts.Transaction); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(opts.Paths.Config); !os.IsNotExist(err) {
		t.Fatalf("candidate check published live config: %v", err)
	}
}

func TestUnitPassesNativeSystemdValidation(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("systemd is Linux-only")
	}
	analyzer, err := exec.LookPath("systemd-analyze")
	if err != nil {
		t.Skip("systemd-analyze is not installed")
	}
	p := testPaths(t.TempDir())
	p.Binary = "/bin/true"
	for _, dir := range []string{p.StateDir, filepath.Dir(p.Unit), p.ControlDir()} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(p.ControlDir(), "recover-sbc"), []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p.Unit, []byte(Unit(p)), 0o644); err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(analyzer, "verify", "--man=no", p.Unit).CombinedOutput()
	if err != nil {
		t.Fatalf("systemd rejected the unit: %v\n%s", err, out)
	}
}

func TestProbePlanHonorsCurrentMode(t *testing.T) {
	for _, mode := range []string{"direct", "ai-only", "china-direct", "gfw-proxy", "global"} {
		page, proxy, err := probePlan(mode)
		if err != nil || page == "" || proxy != (mode != "direct") {
			t.Fatalf("plan %s=%s %v %v", mode, page, proxy, err)
		}
	}
	if _, _, err := probePlan("unknown"); err == nil {
		t.Fatal("unknown mode accepted")
	}
}
