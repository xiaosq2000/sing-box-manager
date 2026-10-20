package install

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

// TunUpdate owns a complete, checked user candidate. Root publication and commit
// remain the caller's responsibility. It never starts either proxy service.
type TunUpdate struct {
	Layout    paths.Layout
	Envelope  *Envelope
	Upgraded  Available
	Pending   Available
	installer *Installer
	files     map[string]string
	recovery  *TunRecovery
	stage     string
}

// TunRecovery names a user transaction paired with one root transaction. The
// journal contains backup paths, not API credentials. Backups remain private.
type TunRecovery struct {
	Transaction         string
	PreviousTransaction string
	Mode                string
	Protocol            string
	layout              paths.Layout
	record              upgradeRecovery
	finished            bool
}

// PrepareTunUpdate downloads and checks candidates without changing live files.
// Refresh keeps installed binaries. Upgrade uses the signed release's binaries.
func (i *Installer) PrepareTunUpdate(link Link, sbcVersion, self string, upgrade bool) (_ *TunUpdate, result error) {
	if i.OS != "linux" {
		return nil, i18n.New("Linux TUN is available only on native Linux")
	}
	if _, err := os.Stat(RecoveryFile(i.Layout)); !os.IsNotExist(err) {
		return nil, i18n.New("An interrupted update needs recovery before another update.")
	}
	available, manifest, err := i.CheckUpgrade(link, sbcVersion)
	if err != nil {
		return nil, err
	}
	installed, err := os.ReadFile(i.Layout.SingBoxVersion())
	if err != nil || strings.TrimSpace(string(installed)) == "" {
		return nil, i18n.New("the installed sing-box version is unknown; run 'sbc install'")
	}
	coreVersion := strings.TrimSpace(string(installed))
	if err = os.MkdirAll(i.Layout.Data, 0o700); err != nil {
		return nil, err
	}
	stage, err := os.MkdirTemp(i.Layout.Data, ".upgrade-")
	if err != nil {
		return nil, err
	}
	defer func() {
		if result != nil {
			_ = os.RemoveAll(stage)
		}
	}()
	candidate := *i
	candidate.Layout = paths.Layout{Config: filepath.Join(stage, "config"), Data: filepath.Join(stage, "data")}
	plan := &TunUpdate{Layout: candidate.Layout, Pending: available, installer: i, files: map[string]string{}, stage: stage}
	if upgrade {
		plan.Upgraded = available
		plan.Pending = Available{}
		coreVersion = manifest.SingBox
	}
	if plan.Upgraded.SingBox != "" {
		if err = candidate.installSingBox(link, manifest, i.OS+"-"+i.Arch); err != nil {
			return nil, err
		}
	} else if err = copyTunDirectory(i.Layout.Bin(), candidate.Layout.Bin()); err != nil {
		return nil, err
	}
	if err = candidate.installRules(link, manifest); err != nil {
		return nil, err
	}
	if plan.Upgraded.SBC != "" {
		data, err := i.download(link, manifest, "sbc/"+i.OS+"-"+i.Arch+"/sbc")
		if err != nil {
			return nil, err
		}
		if err = paths.WriteFile(candidate.Layout.SBC(), data, 0o755); err != nil {
			return nil, err
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		output, checkErr := exec.CommandContext(ctx, candidate.Layout.SBC(), "version").Output()
		cancel()
		if checkErr != nil || strings.TrimSpace(string(output)) != "sbc "+manifest.SBC {
			return nil, i18n.New("the downloaded sbc failed its version check")
		}
		plan.files[i.Layout.SBC()] = candidate.Layout.SBC()
	} else if err = copyFile(self, candidate.Layout.SBC()); err != nil {
		return nil, err
	}
	// Always fetch the full envelope: mixed-only ETags cannot identify the
	// root generation, and root cannot use a configuration-free 304 response.
	envelope, header, err := i.fetchEnvelope(link, coreVersion)
	if err != nil {
		return nil, err
	}
	if len(envelope.ConfigTun) == 0 || string(envelope.ConfigTun) == "null" {
		return nil, i18n.New("the portal does not provide a Linux TUN config; update the portal")
	}
	current, err := os.ReadFile(i.Layout.ConfigFile())
	if err != nil {
		return nil, err
	}
	local, err := singbox.ReadLocal(current)
	if err != nil {
		return nil, err
	}
	patched, err := singbox.Patch(envelope.Config, local)
	if err != nil {
		return nil, err
	}
	if err = paths.WriteFile(candidate.Layout.ConfigFile(), patched, 0o600); err != nil {
		return nil, err
	}
	if err = singbox.Check(candidate.Layout.SingBox(), candidate.Layout.Data, candidate.Layout.ConfigFile()); err != nil {
		return nil, err
	}
	plan.Envelope = envelope
	plan.files[i.Layout.ConfigFile()] = candidate.Layout.ConfigFile()
	for _, pair := range [][2]string{{candidate.Layout.Bin(), i.Layout.Bin()}, {filepath.Join(candidate.Layout.Data, "rules"), filepath.Join(i.Layout.Data, "rules")}} {
		if pair[0] == candidate.Layout.Bin() && plan.Upgraded.SingBox == "" {
			continue
		}
		if err = filepath.WalkDir(pair[0], func(path string, entry os.DirEntry, walkErr error) error {
			if os.IsNotExist(walkErr) {
				return nil
			}
			if walkErr != nil || entry.IsDir() {
				return walkErr
			}
			rel, err := filepath.Rel(pair[0], path)
			if err == nil {
				plan.files[filepath.Join(pair[1], rel)] = path
			}
			return err
		}); err != nil {
			return nil, err
		}
	}
	clientVersion := sbcVersion
	if upgrade {
		clientVersion = manifest.SBC
	}
	for _, entry := range []struct{ live, staged, value string }{
		{i.Layout.SBCVersion(), candidate.Layout.SBCVersion(), clientVersion + "\n"},
		{i.Layout.SingBoxVersion(), candidate.Layout.SingBoxVersion(), coreVersion + "\n"},
		{i.Layout.Usage(), candidate.Layout.Usage(), header.Get(UsageHeader) + "\n"},
		{i.Layout.ETag(), candidate.Layout.ETag(), header.Get("ETag") + "\n"},
	} {
		if err = paths.WriteFile(entry.staged, []byte(entry.value), 0o600); err != nil {
			return nil, err
		}
		plan.files[entry.live] = entry.staged
	}
	return plan, nil
}

func copyTunDirectory(source, destination string) error {
	entries, err := os.ReadDir(source)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		if err = copyFile(filepath.Join(source, entry.Name()), filepath.Join(destination, entry.Name())); err != nil {
			return err
		}
	}
	return nil
}

// Begin makes all backups durable before root can stop the old generation.
func (p *TunUpdate) Begin(id, previous, mode, protocol string) error {
	if !tunTransactionID(id) || !tunTransactionID(previous) || mode == "" || protocol == "" {
		return i18n.New("invalid TUN maintenance transaction")
	}
	record := upgradeRecovery{Stage: p.stage, Kind: "tun", Transaction: id, PreviousTransaction: previous, Mode: mode, Protocol: protocol, Files: map[string]string{}}
	var destinations []string
	for path := range p.files {
		destinations = append(destinations, path)
	}
	destinations = append(destinations, p.installer.Layout.TunState())
	sort.Strings(destinations)
	for index, path := range destinations {
		record.Files[path] = ""
		info, err := os.Lstat(path)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return i18n.New("upgrade destination is not a regular file")
		}
		backup := filepath.Join(p.stage, "previous", fmt.Sprint(index))
		if err = copyFile(path, backup); err != nil {
			return err
		}
		if err = syncTunFile(backup); err != nil {
			return err
		}
		record.Files[path] = backup
	}
	if err := syncTunDirectory(filepath.Join(p.stage, "previous")); err != nil {
		return err
	}
	if err := syncTunDirectory(p.stage); err != nil {
		return err
	}
	if err := record.save(p.installer.Layout); err != nil {
		return err
	}
	p.recovery = tunRecovery(p.installer.Layout, record)
	return syncTunFile(RecoveryFile(p.installer.Layout))
}

// Publish updates only user-owned files. Root must still remain pending.
func (p *TunUpdate) Publish() error {
	if p.recovery == nil {
		return i18n.New("the TUN update has no recovery record")
	}
	var destinations []string
	for path := range p.files {
		destinations = append(destinations, path)
	}
	sort.Strings(destinations)
	for _, path := range destinations {
		if err := replaceFile(p.files[path], path); err != nil {
			return err
		}
		if err := syncTunFile(path); err != nil {
			return err
		}
	}
	return nil
}

// Close removes a disposable candidate, but retains unfinished recovery files.
func (p *TunUpdate) Close() {
	if p.recovery == nil || p.recovery.finished {
		_ = os.RemoveAll(p.stage)
	}
}

// Recovery returns the checked in-memory journal for this operation. A living
// controller must not depend on rereading damaged user metadata to stop capture.
func (p *TunUpdate) Recovery() *TunRecovery { return p.recovery }

// Finish retires the user journal only after root commit or confirmed rollback.
func (p *TunUpdate) Finish() error {
	if p.recovery == nil {
		return i18n.New("the TUN update has no recovery record")
	}
	return p.recovery.Finish()
}

// PendingTunUpdate reads a typed recovery record without changing files.
func (i *Installer) PendingTunUpdate() (*TunRecovery, error) {
	data, err := readRecovery(i.Layout)
	if err != nil || data == nil || data.Kind != "tun" {
		return nil, err
	}
	return tunRecovery(i.Layout, *data), nil
}

func tunRecovery(layout paths.Layout, record upgradeRecovery) *TunRecovery {
	return &TunRecovery{Transaction: record.Transaction, PreviousTransaction: record.PreviousTransaction, Mode: record.Mode, Protocol: record.Protocol, layout: layout, record: record}
}

// Restore changes user files only. The caller must first confirm root shutdown,
// or confirm that the previous generation never stopped. No service is started.
func (r *TunRecovery) Restore() error {
	var result error
	for destination, source := range r.record.Files {
		if source == "" {
			err := os.Remove(destination)
			if !os.IsNotExist(err) {
				result = errors.Join(result, err)
				if err == nil {
					result = errors.Join(result, syncTunDirectory(filepath.Dir(destination)))
				}
			}
		} else if err := replaceFile(source, destination); err != nil {
			result = errors.Join(result, err)
		} else {
			result = errors.Join(result, syncTunFile(destination))
		}
	}
	return result
}

// Finish is also safe after a committed root transaction outlives its caller.
func (r *TunRecovery) Finish() error {
	if err := r.record.finish(r.layout); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err := syncTunDirectory(filepath.Dir(RecoveryFile(r.layout))); err != nil {
		return err
	}
	r.finished = true
	_ = os.RemoveAll(r.record.Stage)
	return nil
}

func syncTunFile(path string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	err = errors.Join(file.Sync(), file.Close())
	dir, openErr := os.Open(filepath.Dir(path))
	if openErr != nil {
		return errors.Join(err, openErr)
	}
	return errors.Join(err, dir.Sync(), dir.Close())
}

func syncTunDirectory(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	return errors.Join(dir.Sync(), dir.Close())
}

func tunTransactionID(value string) bool {
	data, err := hex.DecodeString(value)
	return err == nil && len(data) == 16 && hex.EncodeToString(data) == value
}
