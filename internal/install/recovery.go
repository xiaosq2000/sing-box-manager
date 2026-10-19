package install

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

// RecoveryFile exists only while an upgrade can still need rollback.
func RecoveryFile(l paths.Layout) string { return filepath.Join(l.Data, "upgrade-recovery.json") }

type upgradeRecovery struct {
	Stage string            `json:"stage"`
	Files map[string]string `json:"files"`
}

func (r upgradeRecovery) save(l paths.Layout) error {
	data, err := json.Marshal(r)
	if err != nil {
		return err
	}
	return paths.WriteFile(RecoveryFile(l), append(data, '\n'), 0o600)
}
func (r upgradeRecovery) restore() error {
	var result error
	for destination, source := range r.Files {
		if source == "" {
			err := os.Remove(destination)
			if !os.IsNotExist(err) {
				result = errors.Join(result, err)
			}
		} else {
			result = errors.Join(result, replaceFile(source, destination))
		}
	}
	return result
}
func (r upgradeRecovery) finish(l paths.Layout) error { return os.Remove(RecoveryFile(l)) }

// Recover restores an interrupted transaction under the caller's operation lock.
// A proxy wrapper passes nil callbacks because it has not started sing-box yet.
func (i *Installer) Recover(restart, ready func() error) (bool, error) {
	data, err := os.ReadFile(RecoveryFile(i.Layout))
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var recovery upgradeRecovery
	if err = json.Unmarshal(data, &recovery); err != nil {
		return false, i18n.New("invalid upgrade recovery record")
	}
	if err = recovery.validate(i.Layout); err != nil {
		return false, err
	}
	if err = recovery.restore(); err != nil {
		return false, err
	}
	if restart != nil {
		if err = restart(); err != nil {
			return false, err
		}
	}
	if ready != nil {
		if err = ready(); err != nil {
			return false, err
		}
	}
	if err = recovery.finish(i.Layout); err != nil {
		return false, err
	}
	_ = os.RemoveAll(recovery.Stage)
	return true, nil
}

func (r upgradeRecovery) validate(l paths.Layout) error {
	invalid := i18n.New("invalid upgrade recovery record")
	if filepath.Dir(r.Stage) != filepath.Clean(l.Data) || !strings.HasPrefix(filepath.Base(r.Stage), ".upgrade-") || filepath.Clean(r.Stage) != r.Stage || len(r.Files) == 0 {
		return invalid
	}
	for destination, source := range r.Files {
		allowed := destination == l.ConfigFile() || destination == l.SBC() || destination == l.SBCVersion() || destination == l.SingBoxVersion() || destination == l.Usage()
		if filepath.Dir(destination) == l.Bin() {
			name := filepath.Base(destination)
			allowed = name == "sing-box"+l.Exe || name == "libcronet.so" || name == "libcronet.dll"
		}
		rules := filepath.Join(l.Data, "rules") + string(filepath.Separator)
		allowed = allowed || strings.HasPrefix(destination, rules)
		if !allowed || filepath.Clean(destination) != destination {
			return invalid
		}
		if source != "" && (filepath.Dir(source) != filepath.Join(r.Stage, "previous") || filepath.Clean(source) != source) {
			return invalid
		}
	}
	return nil
}
