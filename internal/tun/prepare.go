package tun

import (
	"io"
	"os"
	"path/filepath"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

// Staged is the user-owned tree that root applies. It is removed when the
// caller's cleanup runs, whether or not the root helper succeeded.
type Staged struct {
	Dir      string
	Config   string
	BinDir   string
	RulesDir string
}

// Prepare patches the portal config and copies the installed binary and the
// rule snapshots into a private staging directory.
func Prepare(layout paths.Layout, config []byte, local Local) (Staged, func(), error) {
	patched, files, err := Patch(config, local)
	if err != nil {
		return Staged{}, nil, err
	}
	dir, err := os.MkdirTemp("", "sbc-tun-")
	if err != nil {
		return Staged{}, nil, err
	}
	cleanup := func() { _ = os.RemoveAll(dir) }

	binDir := filepath.Join(dir, "bin")
	if err := copyUserDir(layout.Bin(), binDir, true); err != nil {
		cleanup()
		return Staged{}, nil, err
	}
	if _, err := os.Stat(filepath.Join(binDir, "sing-box")); err != nil {
		cleanup()
		return Staged{}, nil, i18n.New("the installed sing-box binary is missing; run sbc install")
	}
	rulesDir := filepath.Join(dir, "rules")
	if err := os.MkdirAll(rulesDir, 0o700); err != nil {
		cleanup()
		return Staged{}, nil, err
	}
	for _, name := range files {
		source := filepath.Join(layout.Data, "rules", name)
		if err := copyUserFile(source, filepath.Join(rulesDir, name), 0o600); err != nil {
			cleanup()
			return Staged{}, nil, i18n.Errorf("the rule snapshot %s is missing; run sbc upgrade", name)
		}
	}
	configPath := filepath.Join(dir, "config.json")
	if err := os.WriteFile(configPath, patched, 0o600); err != nil {
		cleanup()
		return Staged{}, nil, err
	}
	return Staged{
		Dir:      dir,
		Config:   configPath,
		BinDir:   binDir,
		RulesDir: rulesDir,
	}, cleanup, nil
}

func copyUserDir(source, target string, requireFiles bool) error {
	entries, err := os.ReadDir(source)
	if err != nil {
		return i18n.New("the installed client files are missing; run sbc install")
	}
	if err := os.MkdirAll(target, 0o700); err != nil {
		return err
	}
	copied := 0
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		mode := os.FileMode(0o600)
		if info.Mode()&0o111 != 0 {
			mode = 0o700
		}
		if err := copyUserFile(filepath.Join(source, entry.Name()), filepath.Join(target, entry.Name()), mode); err != nil {
			return err
		}
		copied++
	}
	if requireFiles && copied == 0 {
		return i18n.New("the installed client files are missing; run sbc install")
	}
	return nil
}

func copyUserFile(source, target string, mode os.FileMode) error {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return err
	}
	defer output.Close()
	if _, err := io.Copy(output, input); err != nil {
		return err
	}
	return output.Chmod(mode)
}
