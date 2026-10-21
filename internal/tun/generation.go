package tun

import (
	"os"
	"path/filepath"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// prepareGeneration copies a complete candidate without changing the live files.
// Candidate checks use candidate rule paths. Published configs use final paths.
func prepareGeneration(opts Options, tools Tools, gid int, id string) (Paths, error) {
	candidate := opts.Paths
	candidate.BinaryDir += ".next-" + id
	candidate.Binary = filepath.Join(candidate.BinaryDir, "sing-box")
	candidate.ConfigDir += ".next-" + id
	candidate.Config = filepath.Join(candidate.ConfigDir, "config.json")
	candidate.RulesDir = filepath.Join(candidate.ConfigDir, "rules")
	candidate.BinaryHash = filepath.Join(candidate.ConfigDir, "sing-box.sha256")
	staged := opts
	staged.Paths = candidate
	if err := os.MkdirAll(candidate.ConfigDir, 0o750); err != nil {
		return candidate, err
	}
	if err := os.Chmod(candidate.ConfigDir, 0o750); err != nil {
		return candidate, err
	}
	if err := opts.Chown(candidate.ConfigDir, 0, gid); err != nil {
		return candidate, err
	}
	if err := stage(staged, gid); err != nil {
		return candidate, err
	}
	port, secret, err := ReadAPI(candidate.Config)
	if err != nil {
		return candidate, err
	}
	content, _, err := PatchFile(candidate.Config, Local{APIPort: port, APISecret: secret, CachePath: candidate.Cache, RulesDir: candidate.RulesDir})
	if err != nil {
		return candidate, err
	}
	checkPath := filepath.Join(candidate.ConfigDir, "check.json")
	if err := os.WriteFile(checkPath, content, 0o640); err != nil {
		return candidate, err
	}
	if err := opts.Runner.Run("env", "LD_LIBRARY_PATH="+candidate.BinaryDir,
		candidate.Binary, "check", "-D", candidate.StateDir, "-c", checkPath); err != nil {
		return candidate, i18n.New("the TUN candidate did not pass sing-box check. Live files were not changed")
	}
	return candidate, nil
}

// PatchFile is Patch with a file input. It never modifies the input file.
func PatchFile(path string, local Local) ([]byte, []string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, err
	}
	return Patch(data, local)
}

func publishGeneration(opts Options, candidate Paths, id string) error {
	// Keep superseded generations for recovery. No failed publication starts an
	// older service automatically, and no publication deletes the only old copy.
	for _, pair := range [][2]string{{opts.Paths.BinaryDir, candidate.BinaryDir}, {opts.Paths.ConfigDir, candidate.ConfigDir}} {
		if _, err := os.Lstat(pair[0]); err == nil {
			if err := os.Rename(pair[0], pair[0]+".prev-"+id); err != nil {
				return err
			}
		} else if !os.IsNotExist(err) {
			return err
		}
		if err := os.Rename(pair[1], pair[0]); err != nil {
			return err
		}
	}
	if _, err := os.Stat(opts.Paths.Unit); err == nil {
		if err := copyFileAtomic(opts.Paths.Unit, opts.Paths.Unit+".prev-"+id, 0o644, 0, 0, opts.Chown); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	return hashFile(opts.Paths.Binary, opts.Paths.BinaryHash, 0, 0, opts.Chown)
}
