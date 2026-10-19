package install

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
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

// Upgrade downloads and validates all candidates before changing live files.
// Callers serialize it with refresh, installation and local configuration edits.
// A failed write, restart or readiness check restores binaries and configuration.
func (i *Installer) Upgrade(link Link, sbcVersion string, restart, ready func() error) (Available, error) {
	if _, err := os.Stat(RecoveryFile(i.Layout)); err == nil {
		return Available{}, i18n.New("An interrupted upgrade needs recovery before another upgrade.")
	} else if !os.IsNotExist(err) {
		return Available{}, err
	}
	available, manifest, err := i.CheckUpgrade(link, sbcVersion)
	if err != nil || !available.Any() {
		return available, err
	}
	if err = os.MkdirAll(i.Layout.Data, 0o700); err != nil {
		return Available{}, err
	}
	stage, err := os.MkdirTemp(i.Layout.Data, ".upgrade-")
	if err != nil {
		return Available{}, err
	}
	retain := false
	defer func() {
		if !retain {
			os.RemoveAll(stage)
		}
	}()
	candidate := *i
	candidate.Layout = paths.Layout{Config: filepath.Join(stage, "config"), Data: filepath.Join(stage, "data"), Exe: i.Layout.Exe}
	current, err := os.ReadFile(i.Layout.ConfigFile())
	if err != nil {
		return Available{}, err
	}
	local, err := singbox.ReadLocal(current)
	if err != nil {
		return Available{}, err
	}
	checkBinary := i.Layout.SingBox()
	if available.SingBox != "" {
		if err = candidate.installSingBox(link, manifest, i.OS+"-"+i.Arch); err != nil {
			return Available{}, err
		}
		checkBinary = candidate.Layout.SingBox()
	}
	if err = candidate.installRules(link, manifest); err != nil {
		return Available{}, err
	}
	if available.SBC != "" {
		fmt.Fprintf(i.Log, i18n.T("Downloading sbc %s...\n"), manifest.SBC)
		data, err := i.download(link, manifest, "sbc/"+i.OS+"-"+i.Arch+"/sbc")
		if err != nil {
			return Available{}, err
		}
		if err = paths.WriteFile(candidate.Layout.SBC(), data, 0o755); err != nil {
			return Available{}, err
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		output, checkErr := exec.CommandContext(ctx, candidate.Layout.SBC(), "version").Output()
		cancel()
		if checkErr != nil || strings.TrimSpace(string(output)) != "sbc "+manifest.SBC {
			return Available{}, i18n.New("the downloaded sbc failed its version check")
		}
	}
	config, usage, err := i.fetchConfig(link, manifest.SingBox)
	if err != nil {
		return Available{}, err
	}
	patched, err := singbox.Patch(config, local)
	if err != nil {
		return Available{}, err
	}
	if err = paths.WriteFile(candidate.Layout.ConfigFile(), patched, 0o600); err != nil {
		return Available{}, err
	}
	if err = singbox.Check(checkBinary, candidate.Layout.Data, candidate.Layout.ConfigFile()); err != nil {
		return Available{}, err
	}

	files := map[string]string{i.Layout.ConfigFile(): candidate.Layout.ConfigFile()}
	for _, pair := range [][2]string{{candidate.Layout.Bin(), i.Layout.Bin()}, {filepath.Join(candidate.Layout.Data, "rules"), filepath.Join(i.Layout.Data, "rules")}} {
		if pair[0] == candidate.Layout.Bin() && available.SingBox == "" {
			continue
		}
		err = filepath.WalkDir(pair[0], func(path string, entry os.DirEntry, walkErr error) error {
			if os.IsNotExist(walkErr) {
				return nil
			}
			if walkErr != nil {
				return walkErr
			}
			if entry.IsDir() {
				return nil
			}
			rel, err := filepath.Rel(pair[0], path)
			if err != nil {
				return err
			}
			files[filepath.Join(pair[1], rel)] = path
			return nil
		})
		if err != nil {
			return Available{}, err
		}
	}
	if available.SBC != "" {
		files[i.Layout.SBC()] = candidate.Layout.SBC()
	}
	for _, entry := range []struct{ live, staged, value string }{
		{i.Layout.SBCVersion(), candidate.Layout.SBCVersion(), manifest.SBC + "\n"},
		{i.Layout.SingBoxVersion(), candidate.Layout.SingBoxVersion(), manifest.SingBox + "\n"},
		{i.Layout.Usage(), candidate.Layout.Usage(), usage + "\n"},
	} {
		if err = paths.WriteFile(entry.staged, []byte(entry.value), 0o600); err != nil {
			return Available{}, err
		}
		files[entry.live] = entry.staged
	}
	// Back up every destination before the first live write.
	var destinations []string
	for path := range files {
		destinations = append(destinations, path)
	}
	sort.Strings(destinations)
	backups := map[string]string{}
	for index, path := range destinations {
		backups[path] = ""
		info, err := os.Lstat(path)
		if os.IsNotExist(err) {
			if path == i.Layout.SBCVersion() {
				// Migrated clients may not have a receipt. A newer executable
				// must still know the restored build after interrupted rollback.
				backup := filepath.Join(stage, "previous", fmt.Sprint(index))
				if err := paths.WriteFile(backup, []byte(sbcVersion+"\n"), 0o600); err != nil {
					return Available{}, err
				}
				backups[path] = backup
			}
			continue
		}
		if err != nil {
			return Available{}, err
		}
		if !info.Mode().IsRegular() {
			return Available{}, i18n.New("upgrade destination is not a regular file")
		}
		backup := filepath.Join(stage, "previous", fmt.Sprint(index))
		if err = copyFile(path, backup); err != nil {
			return Available{}, err
		}
		backups[path] = backup
	}
	snapshot := upgradeRecovery{Stage: stage, Files: backups}
	if err = snapshot.save(i.Layout); err != nil {
		return Available{}, err
	}
	restartNeeded := available.SingBox != "" || !bytes.Equal(current, patched) || (i.OS == "windows" && available.SBC != "")
	recoverFiles := func(cause error) (Available, error) {
		var recovery error
		if i.BeforeRestore != nil {
			recovery = i.BeforeRestore()
		}
		if recovery == nil {
			recovery = snapshot.restore()
		}
		if recovery == nil && restartNeeded && restart != nil {
			recovery = errors.Join(recovery, restart())
			if ready != nil {
				recovery = errors.Join(recovery, ready())
			}
		}
		if recovery == nil {
			recovery = snapshot.finish(i.Layout)
		}
		if recovery != nil {
			retain = true
			return Available{}, i18n.Errorf("Upgrade failed and recovery failed. Recovery files remain at %s: %w", stage, errors.Join(cause, recovery))
		}
		return Available{}, i18n.Errorf("Upgrade failed. The previous binaries and configuration were restored: %w", cause)
	}
	for _, path := range destinations {
		if err = replaceFile(files[path], path); err != nil {
			return recoverFiles(err)
		}
	}
	if restartNeeded && restart != nil {
		if err = restart(); err != nil {
			return recoverFiles(err)
		}
		if ready != nil {
			if err = ready(); err != nil {
				return recoverFiles(err)
			}
		}
	}
	if err = os.Remove(i.Layout.ETag()); err != nil && !os.IsNotExist(err) {
		return recoverFiles(err)
	}
	if err = snapshot.finish(i.Layout); err != nil {
		return recoverFiles(err)
	}
	return available, nil
}

func copyFile(source, destination string) error {
	info, err := os.Stat(source)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return i18n.New("upgrade source is not a regular file")
	}
	if err = os.MkdirAll(filepath.Dir(destination), 0o700); err != nil {
		return err
	}
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(destination, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, info.Mode().Perm())
	if err != nil {
		return err
	}
	_, err = io.Copy(out, in)
	return errors.Join(err, out.Close())
}
func replaceFile(source, destination string) error {
	data, err := os.ReadFile(source)
	if err != nil {
		return err
	}
	info, err := os.Stat(source)
	if err != nil {
		return err
	}
	return paths.WriteFile(destination, data, info.Mode().Perm())
}
