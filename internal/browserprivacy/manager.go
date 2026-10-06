// Package browserprivacy keeps the WebRTC choice independent of proxy state.
package browserprivacy

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Manager serializes mode changes and native policy/profile operations.
// The platform supplies Lock, Enable and Remove. An absent state file enables
// automatic setup. Only a successful explicit Set(true) clears an opt-out.
type Manager struct {
	StateFile string
	Lock      func() (func(), error)
	Enable    func() error
	Remove    func() error
}

func (w *Manager) optedOut() (bool, error) {
	if w.StateFile == "" {
		return false, nil
	}
	data, err := os.ReadFile(w.StateFile)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, i18n.Errorf("could not read the WebRTC mode state: %w", err)
	}
	if strings.TrimSpace(string(data)) != "off" {
		return false, i18n.New("the WebRTC mode state is invalid; use 'sbc webrtc on' or 'sbc webrtc off'")
	}
	return true, nil
}

// Ensure defaults to enabled, but never reverses an explicit opt-out.
func (w *Manager) Ensure() error {
	unlock, err := w.Lock()
	if err != nil {
		return err
	}
	defer unlock()
	off, err := w.optedOut()
	if err != nil || off {
		return err
	}
	return w.Enable()
}

// Set(false) records the choice before cleanup: cancellation or a partial
// cleanup must not let a later automatic Ensure recreate removed policies.
// Set(true) clears that choice only after verified setup and profile success.
func (w *Manager) Set(enabled bool) error {
	unlock, err := w.Lock()
	if err != nil {
		return err
	}
	defer unlock()
	if enabled {
		if err := w.Enable(); err != nil {
			return err
		}
		return w.removeState()
	}
	if err := w.writeOff(); err != nil {
		return err
	}
	if err := w.Remove(); err != nil {
		return i18n.Errorf("automatic WebRTC setup is now off, but cleanup failed; retry 'sbc webrtc off' or uninstall: %w", err)
	}
	return nil
}

// Cleanup leaves mode state intact even on success: a later uninstall step
// might fail, so the caller owns final config-directory removal. Failures
// propagate so uninstall can keep recovery files and the binary for retry.
// Pre-existing or externally changed policies survive; Remaining lists their
// locations without including values.
func (w *Manager) Cleanup() error {
	unlock, err := w.Lock()
	if err != nil {
		return err
	}
	defer unlock()
	return w.Remove()
}

func (w *Manager) removeState() error {
	if w.StateFile == "" {
		return nil
	}
	if err := os.Remove(w.StateFile); err != nil && !os.IsNotExist(err) {
		return i18n.Errorf("could not remove the WebRTC mode state: %w", err)
	}
	return nil
}

func (w *Manager) writeOff() error {
	if w.StateFile == "" {
		return i18n.New("a state file is required to persist the WebRTC opt-out")
	}
	dir := filepath.Dir(w.StateFile)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return i18n.Errorf("could not save the WebRTC opt-out: %w", err)
	}
	file, err := os.CreateTemp(dir, ".webrtc-*")
	if err != nil {
		return i18n.Errorf("could not save the WebRTC opt-out: %w", err)
	}
	defer os.Remove(file.Name())
	if _, err = file.WriteString("off\n"); err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(file.Name(), w.StateFile)
	}
	if err != nil {
		return i18n.Errorf("could not save the WebRTC opt-out: %w", err)
	}
	return nil
}
