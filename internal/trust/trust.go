// Package trust checks the signed manifest of client downloads.
//
// sbc installs a file only when its SHA-256 matches a manifest signed by one of
// the keys below. The private key stays on the build machine, so a server that
// is broken into can serve other files but cannot sign for them.
package trust

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Keys are the base64 ed25519 public keys sbc trusts. A new key ships here
// before the release that first signs with it.
var Keys = []string{
	"eaDFZO3F8e1HG1pfCT03lKZts2R1fe9817b6qsyjCmw=",
}

// File is one download the manifest vouches for.
type File struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

// Manifest lists the downloads of one release.
type Manifest struct {
	Version int    `json:"version"`
	SBC     string `json:"sbc"`
	SingBox string `json:"sing_box"`
	Files   []File `json:"files"`
}

// ErrUntrusted means no trusted key signed the manifest.
var ErrUntrusted = i18n.New("the download manifest is not signed by a trusted key")

// Verify checks the signature on payload and returns the manifest it holds.
func Verify(payload, signature []byte, keys []string) (*Manifest, error) {
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(signature)))
	if err != nil || len(sig) != ed25519.SignatureSize {
		return nil, ErrUntrusted
	}
	trusted := false
	for _, encoded := range keys {
		key, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil || len(key) != ed25519.PublicKeySize {
			return nil, i18n.Errorf("malformed trusted key %q", encoded)
		}
		if ed25519.Verify(ed25519.PublicKey(key), payload, sig) {
			trusted = true
			break
		}
	}
	if !trusted {
		return nil, ErrUntrusted
	}
	var manifest Manifest
	if err := json.Unmarshal(payload, &manifest); err != nil {
		return nil, i18n.Errorf("read the download manifest: %w", err)
	}
	if manifest.Version != 1 {
		return nil, i18n.Errorf("download manifest version %d is not supported; upgrade sbc", manifest.Version)
	}
	return &manifest, nil
}

// Lookup returns the manifest entry for path.
func (m *Manifest) Lookup(path string) (File, bool) {
	for _, file := range m.Files {
		if file.Path == path {
			return file, true
		}
	}
	return File{}, false
}

// Check reports whether data is exactly the file the manifest lists.
func (f File) Check(data []byte) error {
	sum := sha256.Sum256(data)
	if int64(len(data)) != f.Size || hex.EncodeToString(sum[:]) != f.SHA256 {
		return i18n.Errorf("%s does not match the signed manifest", f.Path)
	}
	return nil
}
