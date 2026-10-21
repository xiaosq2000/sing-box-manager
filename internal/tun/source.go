package tun

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"sort"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

// SourceFingerprint identifies the unpatched config and its required rule
// contents. It contains no plaintext credentials and does not read the engine.
func SourceFingerprint(config []byte, digest func(string) (string, error)) (string, error) {
	_, names, err := Patch(config, Local{})
	if err != nil {
		return "", err
	}
	sort.Strings(names)
	hash := sha256.New()
	hash.Write(config)
	for _, name := range names {
		value, err := digest(name)
		if err != nil {
			return "", err
		}
		hash.Write([]byte("\x00" + name + "\x00" + value))
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

// InstalledFingerprint reads the snapshots that a candidate will actually use.
func InstalledFingerprint(layout paths.Layout, config []byte) (string, error) {
	return SourceFingerprint(config, func(name string) (string, error) {
		data, err := os.ReadFile(filepath.Join(layout.Data, "rules", name))
		if err != nil {
			return "", err
		}
		sum := sha256.Sum256(data)
		return hex.EncodeToString(sum[:]), nil
	})
}
