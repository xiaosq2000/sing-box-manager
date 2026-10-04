// Package install sets sbc up from a subscription link.
package install

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/trust"
)

// DefaultListenPort is where the local proxy listens unless it is taken.
const DefaultListenPort = 1080

var linkPath = regexp.MustCompile(`^/sub/([A-Za-z0-9_-]{22})/?$`)

// Link is a parsed subscription link. Its token is a credential, so errors
// name it only as [token].
type Link struct {
	base  string
	token string
}

// ParseLink accepts https://host/sub/<token>, with or without a trailing slash.
func ParseLink(text string) (Link, error) {
	text = strings.TrimPrefix(strings.TrimSpace(text), "\ufeff")
	parsed, err := url.Parse(strings.TrimSpace(text))
	if err != nil || parsed.Host == "" || (parsed.Scheme != "https" && parsed.Scheme != "http") {
		return Link{}, i18n.New("that is not a subscription link")
	}
	match := linkPath.FindStringSubmatch(parsed.Path)
	if match == nil {
		return Link{}, i18n.New("that is not a subscription link")
	}
	return Link{base: parsed.Scheme + "://" + parsed.Host + "/sub/" + match[1], token: match[1]}, nil
}

// String returns the link, which is a credential.
func (l Link) String() string { return l.base }

// Masked returns the link with most of its token hidden, for display.
func (l Link) Masked() string {
	return strings.TrimSuffix(l.base, l.token) + l.token[:4] + "…" + l.token[len(l.token)-4:]
}

func (l Link) redact(err error) error {
	if err == nil {
		return nil
	}
	return errors.New(strings.ReplaceAll(err.Error(), l.token, "[token]"))
}

// ProxyUsername is the username the local proxy asks for when it asks for one.
const ProxyUsername = "sbc"

// Auth says whether the local proxy asks for a username and password.
type Auth int

const (
	// AuthAuto keeps an earlier install's choice, and otherwise asks for a
	// password only where other people use the machine.
	AuthAuto Auth = iota
	AuthOn
	AuthOff
)

// Installer holds what an install needs, so tests can point it elsewhere.
type Installer struct {
	Client *http.Client
	Keys   []string
	Layout paths.Layout
	OS     string
	Arch   string
	Log    io.Writer
	Auth   Auth
	// Shared reports whether other people use this machine. Nil means
	// SharedHost.
	Shared func() bool
}

// Result describes a finished install.
type Result struct {
	SingBox    string
	SBC        string
	ListenPort int
	// Auth says whether the proxy asks for a password.
	Auth bool
}

// Install downloads what the link's release lists, checks each file against
// the signed manifest, and writes a checked config.
func (i *Installer) Install(link Link) (*Result, error) {
	manifest, err := i.manifest(link)
	if err != nil {
		return nil, err
	}
	platform := i.OS + "-" + i.Arch
	if err := i.installSingBox(link, manifest, platform); err != nil {
		return nil, err
	}
	if err := i.installRules(link, manifest); err != nil {
		return nil, err
	}
	config, usage, err := i.fetchConfig(link, manifest.SingBox)
	if err != nil {
		return nil, err
	}
	local, err := i.local()
	if err != nil {
		return nil, err
	}
	patched, err := singbox.Patch(config, local)
	if err != nil {
		return nil, err
	}
	if err := paths.WriteFile(i.Layout.ConfigFile(), patched, 0o600); err != nil {
		return nil, err
	}
	if err := singbox.Check(i.Layout.SingBox(), i.Layout.Data, i.Layout.ConfigFile()); err != nil {
		return nil, err
	}
	if err := paths.WriteFile(i.Layout.SingBoxVersion(), []byte(manifest.SingBox+"\n"), 0o644); err != nil {
		return nil, err
	}
	if err := paths.WriteFile(i.Layout.Link(), []byte(link.String()+"\n"), 0o600); err != nil {
		return nil, err
	}
	os.Remove(i.Layout.ETag())
	i.saveUsage(usage)
	return &Result{SingBox: manifest.SingBox, SBC: manifest.SBC, ListenPort: local.ListenPort, Auth: local.Username != ""}, nil
}

// local picks this machine's settings. A reinstall keeps the port and the
// password, which the running sing-box and open shells still use.
func (i *Installer) local() (singbox.Local, error) {
	var local singbox.Local
	reinstall := false
	if config, err := os.ReadFile(i.Layout.ConfigFile()); err == nil {
		if earlier, err := singbox.ReadLocal(config); err == nil && earlier.ListenPort > 0 {
			local.ListenPort, local.Username, local.Password = earlier.ListenPort, earlier.Username, earlier.Password
			reinstall = true
		}
	}
	var err error
	if local.ListenPort == 0 {
		if local.ListenPort, err = singbox.FreePort(DefaultListenPort); err != nil {
			return local, err
		}
	}
	if local.APIPort, err = singbox.FreePort(0); err != nil {
		return local, err
	}
	if local.SpeedPort, err = speedPort(local); err != nil {
		return local, err
	}
	if local.APISecret, err = randomHex(16); err != nil {
		return local, err
	}
	switch {
	case i.Auth == AuthOff:
		local.Username, local.Password = "", ""
	case local.Username != "":
	case i.Auth == AuthOn || (!reinstall && i.shared()):
		local.Username = ProxyUsername
		if local.Password, err = randomHex(16); err != nil {
			return local, err
		}
	}
	return local, nil
}

// speedPort picks a free port for the speed test that the config does not
// already use.
func speedPort(local singbox.Local) (int, error) {
	for range 10 {
		port, err := singbox.FreePort(0)
		if err != nil {
			return 0, err
		}
		if port != local.ListenPort && port != local.APIPort {
			return port, nil
		}
	}
	return 0, i18n.New("no free loopback port")
}

func (i *Installer) shared() bool {
	if i.Shared != nil {
		return i.Shared()
	}
	return SharedHost(i.OS)
}

func randomHex(size int) (string, error) {
	data := make([]byte, size)
	if _, err := rand.Read(data); err != nil {
		return "", err
	}
	return hex.EncodeToString(data), nil
}

func (i *Installer) get(link Link, rawURL string) ([]byte, error) {
	body, _, err := i.fetch(link, rawURL)
	return body, err
}

// fetch is get with the response's headers.
func (i *Installer) fetch(link Link, rawURL string) ([]byte, http.Header, error) {
	response, err := i.Client.Get(rawURL)
	if err != nil {
		return nil, nil, link.redact(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, nil, link.redact(err)
	}
	if response.StatusCode == http.StatusNotFound {
		return nil, nil, i18n.New("the portal does not know this link; it may have been replaced")
	}
	if response.StatusCode != http.StatusOK {
		return nil, nil, i18n.Errorf("the portal answered %s: %s", response.Status, strings.TrimSpace(string(body)))
	}
	return body, response.Header, nil
}

func (i *Installer) manifest(link Link) (*trust.Manifest, error) {
	payload, err := i.get(link, link.base+"/files/manifest.json")
	if err != nil {
		return nil, err
	}
	signature, err := i.get(link, link.base+"/files/manifest.json.sig")
	if err != nil {
		return nil, err
	}
	return trust.Verify(payload, signature, i.Keys)
}

func (i *Installer) download(link Link, manifest *trust.Manifest, file string) ([]byte, error) {
	entry, ok := manifest.Lookup(file)
	if !ok {
		return nil, i18n.Errorf("the release has no %s", file)
	}
	data, err := i.get(link, link.base+"/files/"+file)
	if err != nil {
		return nil, err
	}
	if err := entry.Check(data); err != nil {
		return nil, err
	}
	return data, nil
}

func (i *Installer) installSingBox(link Link, manifest *trust.Manifest, platform string) error {
	archive := fmt.Sprintf("sing-box/sing-box-%s-%s.%s", manifest.SingBox, platform, archiveSuffix(i.OS))
	fmt.Fprintf(i.Log, i18n.T("Downloading sing-box %s for %s...\n"), manifest.SingBox, platform)
	data, err := i.download(link, manifest, archive)
	if err != nil {
		return err
	}
	return extractSingBox(data, i.OS, i.Layout.Bin())
}

// archiveSuffix names the kind of archive sing-box releases for an OS.
func archiveSuffix(goos string) string {
	if goos == "windows" {
		return "zip"
	}
	return "tar.gz"
}

// extractSingBox writes the binary and the library the naive outbound loads
// from beside it, and nothing else the archive holds.
func extractSingBox(archive []byte, goos, bin string) error {
	binary, library := "sing-box", "libcronet.so"
	if goos == "windows" {
		binary, library = "sing-box.exe", "libcronet.dll"
	}
	wanted := map[string]os.FileMode{binary: 0o755, library: 0o644}
	found := false
	write := func(name string, reader io.Reader) error {
		mode, ok := wanted[path.Base(name)]
		if !ok {
			return nil
		}
		data, err := io.ReadAll(reader)
		if err != nil {
			return err
		}
		if err := paths.WriteFile(filepath.Join(bin, path.Base(name)), data, mode); err != nil {
			return err
		}
		found = found || path.Base(name) == binary
		return nil
	}
	var err error
	if goos == "windows" {
		err = eachZipFile(archive, write)
	} else {
		err = eachTarFile(archive, write)
	}
	if err != nil {
		return err
	}
	if !found {
		return i18n.New("the sing-box archive holds no sing-box binary")
	}
	return nil
}

// eachTarFile calls visit with each regular file in a tar.gz archive.
func eachTarFile(archive []byte, visit func(name string, reader io.Reader) error) error {
	compressed, err := gzip.NewReader(bytes.NewReader(archive))
	if err != nil {
		return i18n.Errorf("read the sing-box archive: %w", err)
	}
	reader := tar.NewReader(compressed)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return i18n.Errorf("read the sing-box archive: %w", err)
		}
		if header.Typeflag != tar.TypeReg {
			continue
		}
		if err := visit(header.Name, reader); err != nil {
			return err
		}
	}
}

// eachZipFile calls visit with each file in a zip archive.
func eachZipFile(archive []byte, visit func(name string, reader io.Reader) error) error {
	reader, err := zip.NewReader(bytes.NewReader(archive), int64(len(archive)))
	if err != nil {
		return i18n.Errorf("read the sing-box archive: %w", err)
	}
	for _, file := range reader.File {
		if file.FileInfo().IsDir() {
			continue
		}
		content, err := file.Open()
		if err != nil {
			return i18n.Errorf("read the sing-box archive: %w", err)
		}
		err = visit(file.Name, content)
		content.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

func (i *Installer) installRules(link Link, manifest *trust.Manifest) error {
	for _, file := range manifest.Files {
		if !strings.HasPrefix(file.Path, "rules/") {
			continue
		}
		data, err := i.download(link, manifest, file.Path)
		if err != nil {
			return err
		}
		if err := paths.WriteFile(filepath.Join(i.Layout.Data, filepath.FromSlash(file.Path)), data, 0o644); err != nil {
			return err
		}
	}
	return nil
}

// fetchConfig returns the subscription's config and the traffic count that
// came with it.
func (i *Installer) fetchConfig(link Link, singBoxVersion string) ([]byte, string, error) {
	query := url.Values{"format": {"sbc"}, "os": {i.OS}, "sing-box": {singBoxVersion}}
	body, header, err := i.fetch(link, link.base+"?"+query.Encode())
	if err != nil {
		return nil, "", err
	}
	var envelope struct {
		Version int             `json:"version"`
		Config  json.RawMessage `json:"config"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil, "", i18n.Errorf("read the subscription: %w", err)
	}
	if envelope.Version != 1 {
		return nil, "", i18n.Errorf("subscription version %d is not supported; upgrade sbc", envelope.Version)
	}
	return envelope.Config, header.Get(UsageHeader), nil
}

// NewClient returns the HTTP client sbc uses. It ignores proxy variables, since
// the proxy they name may be the one being installed.
func NewClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &http.Client{Transport: transport, Timeout: 2 * time.Minute}
}

// ReadLink returns the saved subscription link.
func ReadLink(layout paths.Layout) (Link, error) {
	data, err := os.ReadFile(layout.Link())
	if err != nil {
		return Link{}, i18n.New("sbc is not installed; run 'sbc install'")
	}
	return ParseLink(string(data))
}

// Refresh fetches the subscription config and applies it when it changed.
// A new config must pass `sing-box check`, and when sing-box does not come back
// up with it, the previous config is restored. It reports whether the config
// changed.
func (i *Installer) Refresh(link Link, restart func() error, ready func() error) (bool, error) {
	current, err := os.ReadFile(i.Layout.ConfigFile())
	if err != nil {
		return false, i18n.New("sbc is not installed; run 'sbc install'")
	}
	local, err := singbox.ReadLocal(current)
	if err != nil {
		return false, err
	}
	// A config from before the speed test gets a port for it, and a full
	// fetch to patch, since the portal answers an unchanged config with no
	// body.
	if local.SpeedPort == 0 {
		if local.SpeedPort, err = speedPort(local); err != nil {
			return false, err
		}
	}
	conditional := !singbox.MissingSpeedTest(current)
	version, err := os.ReadFile(i.Layout.SingBoxVersion())
	if err != nil {
		return false, i18n.New("the installed sing-box version is unknown; run 'sbc install'")
	}
	query := url.Values{"format": {"sbc"}, "os": {i.OS}, "sing-box": {strings.TrimSpace(string(version))}}
	request, err := http.NewRequest(http.MethodGet, link.base+"?"+query.Encode(), nil)
	if err != nil {
		return false, link.redact(err)
	}
	if etag, err := os.ReadFile(i.Layout.ETag()); err == nil && conditional {
		request.Header.Set("If-None-Match", strings.TrimSpace(string(etag)))
	}
	response, err := i.Client.Do(request)
	if err != nil {
		return false, link.redact(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return false, link.redact(err)
	}
	// The count comes with an unchanged config too.
	if response.StatusCode == http.StatusOK || response.StatusCode == http.StatusNotModified {
		i.saveUsage(response.Header.Get(UsageHeader))
	}
	switch response.StatusCode {
	case http.StatusNotModified:
		return false, nil
	case http.StatusOK:
	case http.StatusNotFound:
		return false, i18n.New("the portal does not know this link; it may have been replaced")
	default:
		return false, i18n.Errorf("the portal answered %s: %s", response.Status, strings.TrimSpace(string(body)))
	}
	var envelope struct {
		Version int             `json:"version"`
		Config  json.RawMessage `json:"config"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil || envelope.Version != 1 {
		return false, i18n.New("the subscription is not one this sbc reads; upgrade sbc")
	}
	patched, err := singbox.Patch(envelope.Config, local)
	if err != nil {
		return false, err
	}
	etag := response.Header.Get("ETag")
	if bytes.Equal(patched, current) {
		return false, paths.WriteFile(i.Layout.ETag(), []byte(etag+"\n"), 0o600)
	}
	staged := i.Layout.ConfigFile() + ".new"
	if err := paths.WriteFile(staged, patched, 0o600); err != nil {
		return false, err
	}
	if err := singbox.Check(i.Layout.SingBox(), i.Layout.Data, staged); err != nil {
		os.Remove(staged)
		return false, err
	}
	previous := i.Layout.ConfigFile() + ".prev"
	if err := paths.WriteFile(previous, current, 0o600); err != nil {
		return false, err
	}
	if err := os.Rename(staged, i.Layout.ConfigFile()); err != nil {
		return false, err
	}
	if err := restart(); err == nil {
		if err := ready(); err == nil {
			return true, paths.WriteFile(i.Layout.ETag(), []byte(etag+"\n"), 0o600)
		}
	}
	if err := os.Rename(previous, i.Layout.ConfigFile()); err != nil {
		return false, err
	}
	restart()
	return false, i18n.New("sing-box did not start with the new config, so the previous one is back")
}

// Available reports what the release offers that this machine lacks.
type Available struct {
	SBC     string
	SingBox string
}

// Any reports whether anything is newer.
func (a Available) Any() bool { return a.SBC != "" || a.SingBox != "" }

// CheckUpgrade compares the release's signed manifest with what is installed.
func (i *Installer) CheckUpgrade(link Link, sbcVersion string) (Available, *trust.Manifest, error) {
	manifest, err := i.manifest(link)
	if err != nil {
		return Available{}, nil, err
	}
	var available Available
	if manifest.SBC != sbcVersion {
		available.SBC = manifest.SBC
	}
	installed, _ := os.ReadFile(i.Layout.SingBoxVersion())
	if manifest.SingBox != strings.TrimSpace(string(installed)) {
		available.SingBox = manifest.SingBox
	}
	return available, manifest, nil
}

// Upgrade installs the release's sbc and sing-box where they differ from what
// runs here, each checked against the signed manifest, and reports what it
// installed.
func (i *Installer) Upgrade(link Link, sbcVersion string) (Available, error) {
	available, manifest, err := i.CheckUpgrade(link, sbcVersion)
	if err != nil || !available.Any() {
		return available, err
	}
	if available.SingBox != "" {
		if err := i.installSingBox(link, manifest, i.OS+"-"+i.Arch); err != nil {
			return Available{}, err
		}
		if err := i.installRules(link, manifest); err != nil {
			return Available{}, err
		}
		if err := paths.WriteFile(i.Layout.SingBoxVersion(), []byte(manifest.SingBox+"\n"), 0o644); err != nil {
			return Available{}, err
		}
	}
	if available.SBC != "" {
		fmt.Fprintf(i.Log, i18n.T("Downloading sbc %s...\n"), manifest.SBC)
		data, err := i.download(link, manifest, "sbc/"+i.OS+"-"+i.Arch+"/sbc")
		if err != nil {
			return Available{}, err
		}
		if err := paths.WriteFile(i.Layout.SBC(), data, 0o755); err != nil {
			return Available{}, err
		}
	}
	return available, nil
}
