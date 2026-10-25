package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/accounting"
	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/contract"
)

// Only a failed start of a confirmed stopped service permits a newer remote repair.
type recoveryStartError struct{ error }

type serviceState struct {
	active, invocation, group, sub string
	pid                            int
}

func inspectService(ctx context.Context, service string) (serviceState, error) {
	output, err := command(ctx, "systemctl", "show", service, "--property=ActiveState", "--property=MainPID", "--property=InvocationID", "--property=ControlGroup", "--property=SubState")
	properties := make(map[string]string)
	for _, line := range strings.Split(output, "\n") {
		key, value, ok := strings.Cut(line, "=")
		if ok {
			properties[key] = value
		}
	}
	pid, pidErr := strconv.Atoi(properties["MainPID"])
	_, hasGroup := properties["ControlGroup"]
	if err != nil || pidErr != nil || pid < 0 || properties["ActiveState"] == "" || !hasGroup {
		return serviceState{}, errors.New("engine service state unavailable")
	}
	return serviceState{active: properties["ActiveState"], invocation: properties["InvocationID"], group: properties["ControlGroup"], sub: properties["SubState"], pid: pid}, nil
}

func (s serviceState) stopped() bool {
	return s.pid == 0 && (s.active == "inactive" || s.active == "failed" || s.sub == "auto-restart")
}

func emptyGroup(group string) error {
	if group == "" {
		return nil
	} // systemd removes the group only after it is empty.
	if !filepath.IsAbs(group) || filepath.Clean(group) != group || group == "/" {
		return errors.New("engine cgroup invalid")
	}
	data, err := os.ReadFile(filepath.Join("/sys/fs/cgroup", group, "cgroup.procs"))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil || strings.TrimSpace(string(data)) != "" {
		return errors.New("engine cgroup termination unconfirmed")
	}
	return nil
}

func missingEngine(ctx context.Context, state string, store *accounting.Store) error {
	var receipt startupReceipt
	// No receipt is expected after publication but before the first wrapper start.
	if readJSON(filepath.Join(state, "running.json"), &receipt) == nil && validInvocation(receipt.Invocation) {
		if err := store.Gap(ctx, receipt.Invocation); err != nil {
			return errors.New("accounting gap commit failed")
		}
	}
	return nil
}

func startSelected(ctx context.Context, state, engine, service string, b contract.Bundle) error {
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

// Recovery uses only the durable local pointer. It never needs an HTTP response.
func recoverCurrent(ctx context.Context, state, engine, service string, b contract.Bundle, store *accounting.Store) error {
	lock, err := lifecycleLock(ctx, state)
	if err != nil {
		return err
	}
	defer lock.Close()
	current, _, err := currentBundle(state, b.NodeID)
	if err != nil || current.Generation != b.Generation || current.ConfigSHA256 != b.ConfigSHA256 {
		return errors.New("recovery generation unconfirmed")
	}
	status, err := inspectService(ctx, service)
	if err != nil {
		return err
	}
	if !status.stopped() {
		_, err := runningIdentity(ctx, state, engine, service, b)
		return err // Unknown live identity is not permission to stop or replace it.
	}
	if err := emptyGroup(status.group); err != nil {
		return err
	}
	if err := missingEngine(ctx, state, store); err != nil {
		return err
	}
	if err := lock.Close(); err != nil {
		return errors.New("lifecycle lock release failed")
	}
	if err := startSelected(ctx, state, engine, service, b); err != nil {
		return &recoveryStartError{err}
	}
	return nil
}
