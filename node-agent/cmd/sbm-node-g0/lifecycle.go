package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/accounting"
	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/contract"
	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/counter"
)

var errLocked = errors.New("state lock is busy")

type startupReceipt struct {
	Generation int64  `json:"generation"`
	Digest     string `json:"config_sha256"`
	Invocation string `json:"invocation"`
	PID        int    `json:"pid"`
}

func tryLock(state, name string) (*os.File, error) {
	file, err := os.OpenFile(filepath.Join(state, name), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, errors.New("state lock file unavailable")
	}
	// Go file descriptors are close-on-exec. The engine must not retain this lock.
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, errLocked
		}
		return nil, errors.New("state lock acquisition failed")
	}
	return file, nil
}

func lifecycleLock(ctx context.Context, state string) (*os.File, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	ticker := time.NewTicker(25 * time.Millisecond)
	defer ticker.Stop()
	for {
		if ctx.Err() != nil {
			return nil, errors.New("lifecycle lock wait expired")
		}
		file, err := tryLock(state, "lifecycle.lock")
		if !errors.Is(err, errLocked) {
			return file, err
		}
		select {
		case <-ctx.Done():
			return nil, errors.New("lifecycle lock wait expired")
		case <-ticker.C:
		}
	}
}

func currentBundle(state, nodeID string) (contract.Bundle, string, error) {
	var b contract.Bundle
	root, err := filepath.Abs(state)
	if err != nil {
		return b, "", errors.New("current generation unavailable")
	}
	dir, err := filepath.EvalSymlinks(filepath.Join(root, "current"))
	if err != nil || filepath.Dir(dir) != filepath.Join(root, "generations") {
		return b, "", errors.New("current generation unavailable")
	}
	b, err = generationBundle(dir, nodeID)
	if err != nil || dir != filepath.Join(root, "generations", fmt.Sprint(b.Generation)) {
		return b, "", errors.New("current generation invalid")
	}
	return b, dir, nil
}

func generationBundle(dir, nodeID string) (contract.Bundle, error) {
	var b contract.Bundle
	if readJSON(filepath.Join(dir, "bundle.json"), &b) != nil {
		return b, errors.New("generation metadata invalid")
	}
	// config.json owns the exact bytes. JSON encoding can change an embedded RawMessage.
	config, err := os.ReadFile(filepath.Join(dir, "config.json"))
	if err != nil || len(config) > 8<<20 {
		return b, errors.New("generation config unavailable")
	}
	b.Config = config
	if b.Validate(nodeID) != nil {
		return b, errors.New("generation config invalid")
	}
	return b, nil
}

func validInvocation(value string) bool {
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == 16 && value == strings.ToLower(value) && value != strings.Repeat("0", 32)
}

func executeEngine(identity contract.Identity, state, engine string) error {
	lock, err := lifecycleLock(context.Background(), state)
	if err != nil {
		return err
	}
	defer lock.Close()
	b, dir, err := currentBundle(state, identity.NodeID)
	if err != nil {
		return err
	}
	invocation := os.Getenv("INVOCATION_ID")
	if !validInvocation(invocation) {
		return errors.New("systemd invocation is required")
	}
	if _, err := command(context.Background(), engine, "check", "-D", state, "-c", filepath.Join(dir, "config.json")); err != nil {
		return errors.New("startup config check failed")
	}
	receipt := startupReceipt{b.Generation, b.ConfigSHA256, invocation, os.Getpid()}
	data, _ := json.Marshal(receipt)
	next := filepath.Join(state, ".running-"+contract.UUID())
	defer os.Remove(next)
	if writeNew(next, data) != nil || os.Rename(next, filepath.Join(state, "running.json")) != nil || syncDir(state) != nil {
		return errors.New("startup receipt publication failed")
	}
	// Use the immutable generation, not the mutable current symlink.
	if err := syscall.Exec(engine, []string{engine, "run", "-D", state, "-c", filepath.Join(dir, "config.json")}, os.Environ()); err != nil {
		return errors.New("engine execution failed")
	}
	return nil
}

// The caller holds lifecycle.lock. A receipt alone does not prove a running engine.
func runningIdentity(ctx context.Context, state, engine, service string, expected contract.Bundle) (string, error) {
	current, _, err := currentBundle(state, expected.NodeID)
	if err != nil || current.Generation != expected.Generation || current.ConfigSHA256 != expected.ConfigSHA256 {
		return "", errors.New("running generation unconfirmed")
	}
	var receipt startupReceipt
	if readJSON(filepath.Join(state, "running.json"), &receipt) != nil {
		return "", errors.New("startup receipt unavailable")
	}
	output, err := command(ctx, "systemctl", "show", service, "--property=ActiveState", "--property=MainPID", "--property=InvocationID")
	properties := make(map[string]string)
	for _, line := range strings.Split(output, "\n") {
		key, value, ok := strings.Cut(line, "=")
		if ok {
			properties[key] = value
		}
	}
	pid, pidErr := strconv.Atoi(properties["MainPID"])
	invocation := properties["InvocationID"]
	if err != nil || pidErr != nil || pid <= 0 || properties["ActiveState"] != "active" || !validInvocation(invocation) || receipt.PID != pid || receipt.Invocation != invocation || receipt.Generation != current.Generation || receipt.Digest != current.ConfigSHA256 {
		return "", errors.New("engine startup identity unconfirmed")
	}
	actual, err := os.Stat(fmt.Sprintf("/proc/%d/exe", pid))
	installed, installedErr := os.Stat(engine)
	if err != nil || installedErr != nil || !os.SameFile(actual, installed) {
		return "", errors.New("engine executable unconfirmed")
	}
	return invocation, nil
}

func observeRunning(ctx context.Context, state, engine, service string, b contract.Bundle) (string, error) {
	lock, err := lifecycleLock(ctx, state)
	if err != nil {
		return "", err
	}
	defer lock.Close()
	return runningIdentity(ctx, state, engine, service, b)
}

func recordSample(ctx context.Context, state, engine, service string, b contract.Bundle, store *accounting.Store) (string, error) {
	lock, err := lifecycleLock(ctx, state)
	if err != nil {
		return "", err
	}
	defer lock.Close()
	invocation, err := runningIdentity(ctx, state, engine, service, b)
	if err != nil {
		return "", err
	}
	up, down, err := counter.Read(ctx, b.StatsAddress, b.Roster[0].WireName)
	if err != nil {
		return "", fmt.Errorf("counter sample failed: %w", err)
	}
	observed, err := runningIdentity(ctx, state, engine, service, b)
	if err != nil || observed != invocation {
		return "", errors.New("engine identity unconfirmed after sample")
	}
	if err := store.Record(ctx, invocation, b.Roster[0].CredentialID, up, down, time.Now()); err != nil {
		return "", errors.New("accounting commit failed")
	}
	return invocation, nil
}

func apply(ctx context.Context, state, engine, service string, b contract.Bundle) error {
	lock, err := lifecycleLock(ctx, state)
	if err != nil {
		return err
	}
	defer lock.Close()
	dir := filepath.Join(state, "generations", fmt.Sprint(b.Generation))
	if err := os.MkdirAll(dir, 0700); err != nil {
		return errors.New("generation creation failed")
	}
	config := filepath.Join(dir, "config.json")
	if previous, err := os.ReadFile(config); err == nil {
		if !bytes.Equal(previous, b.Config) {
			return errors.New("generation content conflict")
		}
	} else if err := writeNew(config, b.Config); err != nil {
		return errors.New("config staging failed")
	}
	metadata := b
	metadata.Config = nil
	data, _ := json.Marshal(metadata)
	if previous, err := os.ReadFile(filepath.Join(dir, "bundle.json")); err == nil {
		if !bytes.Equal(previous, data) {
			return errors.New("generation metadata conflict")
		}
	} else if err := writeNew(filepath.Join(dir, "bundle.json"), data); err != nil {
		return errors.New("bundle staging failed")
	}
	staged, err := generationBundle(dir, b.NodeID)
	if err != nil || staged.Generation != b.Generation || staged.ConfigSHA256 != b.ConfigSHA256 {
		return errors.New("staged generation invalid")
	}
	if _, err := command(ctx, engine, "check", "-D", state, "-c", config); err != nil {
		return errors.New("config check failed")
	}
	if _, err := command(ctx, "systemctl", "stop", service); err != nil {
		return errors.New("engine stop failed")
	}
	pid, err := command(ctx, "systemctl", "show", service, "--property=MainPID", "--value")
	if err != nil || pid != "0" {
		return errors.New("engine termination unconfirmed")
	}
	next := filepath.Join(state, ".current-"+contract.UUID())
	if err := os.Symlink(dir, next); err != nil {
		return errors.New("pointer staging failed")
	}
	defer os.Remove(next)
	if os.Rename(next, filepath.Join(state, "current")) != nil || syncDir(state) != nil {
		return errors.New("pointer publication failed")
	}
	// The startup wrapper takes the same lock. Never wait for it while holding it.
	if err := lock.Close(); err != nil {
		return errors.New("lifecycle lock release failed")
	}
	if _, err := command(ctx, "systemctl", "start", service); err != nil {
		return errors.New("published engine startup failed")
	}
	ready, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	for {
		if _, err := observeRunning(ready, state, engine, service, b); err == nil {
			return nil
		}
		select {
		case <-ready.Done():
			return errors.New("published startup identity unconfirmed")
		case <-time.After(25 * time.Millisecond):
		}
	}
}
