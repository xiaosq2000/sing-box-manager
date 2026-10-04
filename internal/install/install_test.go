package install

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

const token = "abcdefghijklmnopqrstuv"

// A stand-in sing-box: `check` passes unless the config says "reject".
const fakeSingBox = "#!/bin/sh\nif grep -q reject \"$5\"; then echo 'FATAL bad config' >&2; exit 1; fi\n"

type portal struct {
	server   *httptest.Server
	files    map[string][]byte
	key      ed25519.PrivateKey
	tamper   string
	config   string
	requests []string
	// usage is the traffic count the subscription sends, when set.
	usage       string
	notModified int
}

func newPortal(t *testing.T) *portal {
	t.Helper()
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	p := &portal{
		key: key,
		files: map[string][]byte{
			"sing-box/sing-box-1.14.2-linux-amd64.tar.gz": archive(t, map[string]string{
				"sing-box-1.14.2-linux-amd64/sing-box":     fakeSingBox,
				"sing-box-1.14.2-linux-amd64/libcronet.so": "library",
				"sing-box-1.14.2-linux-amd64/LICENSE":      "license",
			}),
			"rules/abc.srs":       []byte("rules"),
			"sbc/linux-amd64/sbc": []byte("new sbc"),
		},
		config: `{"inbounds":[{"type":"mixed","tag":"mixed","listen":"127.0.0.1","listen_port":1080}],"experimental":{"clash_api":{"default_mode":"china"}}}`,
	}
	p.server = httptest.NewServer(http.HandlerFunc(p.serve))
	t.Cleanup(p.server.Close)
	return p
}

func (p *portal) publicKey() string {
	return base64.StdEncoding.EncodeToString(p.key.Public().(ed25519.PublicKey))
}

func (p *portal) manifest() []byte {
	type file struct {
		Path   string `json:"path"`
		Size   int    `json:"size"`
		SHA256 string `json:"sha256"`
	}
	// Sorted: map order changes between calls, and the signature is over
	// exact bytes.
	names := make([]string, 0, len(p.files))
	for name := range p.files {
		names = append(names, name)
	}
	sort.Strings(names)
	var files []file
	for _, path := range names {
		data := p.files[path]
		sum := sha256.Sum256(data)
		files = append(files, file{path, len(data), hex.EncodeToString(sum[:])})
	}
	payload, _ := json.Marshal(map[string]any{"version": 1, "sbc": "abc123", "sing_box": "1.14.2", "files": files})
	return payload
}

func (p *portal) serve(w http.ResponseWriter, r *http.Request) {
	p.requests = append(p.requests, r.URL.RequestURI())
	prefix := "/sub/" + token
	switch {
	case r.URL.Path == prefix:
		if r.URL.Query().Get("format") != "sbc" || r.URL.Query().Get("sing-box") != "1.14.2" {
			http.Error(w, "bad query", http.StatusBadRequest)
			return
		}
		sum := sha256.Sum256([]byte(p.config))
		etag := `"` + hex.EncodeToString(sum[:8]) + `"`
		w.Header().Set("ETag", etag)
		if p.usage != "" {
			w.Header().Set(UsageHeader, p.usage)
		}
		if r.Header.Get("If-None-Match") == etag {
			p.notModified++
			w.WriteHeader(http.StatusNotModified)
			return
		}
		io.WriteString(w, `{"version":1,"config":`+p.config+`}`)
	case r.URL.Path == prefix+"/files/manifest.json":
		w.Write(p.manifest())
	case r.URL.Path == prefix+"/files/manifest.json.sig":
		io.WriteString(w, base64.StdEncoding.EncodeToString(ed25519.Sign(p.key, p.manifest()))+"\n")
	case strings.HasPrefix(r.URL.Path, prefix+"/files/"):
		name := strings.TrimPrefix(r.URL.Path, prefix+"/files/")
		data, ok := p.files[name]
		if !ok {
			http.NotFound(w, r)
			return
		}
		if name == p.tamper {
			data = append([]byte("x"), data...)
		}
		w.Write(data)
	default:
		http.NotFound(w, r)
	}
}

func archive(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buffer bytes.Buffer
	compressed := gzip.NewWriter(&buffer)
	writer := tar.NewWriter(compressed)
	for name, body := range files {
		writer.WriteHeader(&tar.Header{Name: name, Mode: 0o755, Size: int64(len(body)), Typeflag: tar.TypeReg})
		io.WriteString(writer, body)
	}
	writer.Close()
	compressed.Close()
	return buffer.Bytes()
}

func installer(t *testing.T, p *portal, keys ...string) (*Installer, paths.Layout) {
	t.Helper()
	root := t.TempDir()
	layout := paths.Layout{Config: filepath.Join(root, "config"), Data: filepath.Join(root, "data")}
	if len(keys) == 0 {
		keys = []string{p.publicKey()}
	}
	return &Installer{
		Client: p.server.Client(),
		Keys:   keys,
		Layout: layout,
		OS:     "linux",
		Arch:   "amd64",
		Log:    io.Discard,
		Shared: func() bool { return false },
	}, layout
}

func link(t *testing.T, p *portal) Link {
	t.Helper()
	parsed, err := ParseLink(p.server.URL + "/sub/" + token + "/\n")
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func TestInstallWritesCheckedFilesAndAPatchedConfig(t *testing.T) {
	p := newPortal(t)
	i, layout := installer(t, p)

	result, err := i.Install(link(t, p))
	if err != nil {
		t.Fatal(err)
	}

	if result.SingBox != "1.14.2" || result.SBC != "abc123" {
		t.Errorf("result %+v", result)
	}
	for path, mode := range map[string]os.FileMode{
		layout.SingBox(): 0o755,
		filepath.Join(layout.Bin(), "libcronet.so"): 0o644,
		filepath.Join(layout.Data, "rules/abc.srs"): 0o644,
		layout.ConfigFile():                         0o600,
		layout.Link():                               0o600,
	} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != mode {
			t.Errorf("%s has mode %v, want %v", path, info.Mode().Perm(), mode)
		}
	}
	if _, err := os.Stat(filepath.Join(layout.Bin(), "LICENSE")); !os.IsNotExist(err) {
		t.Error("files the archive holds besides sing-box and its library were written")
	}
	config, _ := os.ReadFile(layout.ConfigFile())
	local, err := singbox.ReadLocal(config)
	if err != nil {
		t.Fatal(err)
	}
	if local.ListenPort != result.ListenPort || local.APIPort == 0 || len(local.APISecret) != 32 {
		t.Errorf("local settings %+v", local)
	}
	saved, _ := os.ReadFile(layout.Link())
	if strings.TrimSpace(string(saved)) != p.server.URL+"/sub/"+token {
		t.Errorf("saved link %q", saved)
	}
}

func TestInstallRefusesAManifestNoTrustedKeySigned(t *testing.T) {
	p := newPortal(t)
	other := newPortal(t)
	i, layout := installer(t, p, other.publicKey())

	_, err := i.Install(link(t, p))

	if err == nil || !strings.Contains(err.Error(), "not signed by a trusted key") {
		t.Fatalf("got %v", err)
	}
	if _, err := os.Stat(layout.SingBox()); !os.IsNotExist(err) {
		t.Error("sing-box was written from an untrusted manifest")
	}
}

func TestInstallRefusesAFileThatDoesNotMatchTheManifest(t *testing.T) {
	p := newPortal(t)
	p.tamper = "sing-box/sing-box-1.14.2-linux-amd64.tar.gz"
	i, layout := installer(t, p)

	_, err := i.Install(link(t, p))

	if err == nil || !strings.Contains(err.Error(), "does not match the signed manifest") {
		t.Fatalf("got %v", err)
	}
	if _, err := os.Stat(layout.SingBox()); !os.IsNotExist(err) {
		t.Error("a tampered sing-box was written")
	}
}

func TestInstallStopsWhenSingBoxRejectsTheConfig(t *testing.T) {
	p := newPortal(t)
	p.config = strings.Replace(p.config, `"china"`, `"reject"`, 1)
	i, layout := installer(t, p)

	_, err := i.Install(link(t, p))

	if err == nil || !strings.Contains(err.Error(), "sing-box rejected the config: FATAL bad config") {
		t.Fatalf("got %v", err)
	}
	if _, err := os.Stat(layout.Link()); !os.IsNotExist(err) {
		t.Error("the link was saved for a failed install")
	}
}

func TestErrorsNeverShowTheToken(t *testing.T) {
	p := newPortal(t)
	i, _ := installer(t, p)
	parsed := link(t, p)
	p.server.Close()

	_, err := i.Install(parsed)

	if err == nil || strings.Contains(err.Error(), token) || !strings.Contains(err.Error(), "[token]") {
		t.Fatalf("got %v", err)
	}
}

func TestParseLinkAcceptsOnlySubscriptionLinks(t *testing.T) {
	for _, text := range []string{
		"https://vpn.example.com/sub/" + token,
		"https://vpn.example.com/sub/" + token + "/",
		"\ufeffhttps://vpn.example.com/sub/" + token,
		"\ufeff  https://vpn.example.com/sub/" + token + "/ \r\n",
	} {
		if _, err := ParseLink(text); err != nil {
			t.Errorf("%s: %v", text, err)
		}
	}
	for _, text := range []string{
		"vpn.example.com/sub/" + token,
		"https://vpn.example.com/sub/short",
		"ftp://vpn.example.com/sub/" + token,
		"https://vpn.example.com/files/" + token,
	} {
		if _, err := ParseLink(text); err == nil {
			t.Errorf("%s was accepted", text)
		}
	}
}

func installed(t *testing.T) (*portal, *Installer, paths.Layout) {
	t.Helper()
	p := newPortal(t)
	i, layout := installer(t, p)
	if _, err := i.Install(link(t, p)); err != nil {
		t.Fatal(err)
	}
	return p, i, layout
}

func TestReinstallKeepsTheListenPort(t *testing.T) {
	p, i, layout := installed(t)
	config, _ := os.ReadFile(layout.ConfigFile())
	first, _ := singbox.ReadLocal(config)
	// The first install's sing-box holds its port, so it is not free.
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(first.ListenPort)))
	if err == nil {
		defer listener.Close()
	}

	result, err := i.Install(link(t, p))
	if err != nil {
		t.Fatal(err)
	}
	if result.ListenPort != first.ListenPort {
		t.Errorf("the reinstall moved the proxy from %d to %d", first.ListenPort, result.ListenPort)
	}
}

func readLocal(t *testing.T, layout paths.Layout) singbox.Local {
	t.Helper()
	config, err := os.ReadFile(layout.ConfigFile())
	if err != nil {
		t.Fatal(err)
	}
	local, err := singbox.ReadLocal(config)
	if err != nil {
		t.Fatal(err)
	}
	return local
}

func TestThePasswordFollowsTheMachineAndTheAuthChoice(t *testing.T) {
	p := newPortal(t)
	i, layout := installer(t, p)
	i.Shared = func() bool { return true }

	result, err := i.Install(link(t, p))
	if err != nil {
		t.Fatal(err)
	}
	first := readLocal(t, layout)
	if !result.Auth || first.Username != ProxyUsername || len(first.Password) != 32 {
		t.Fatalf("a shared machine got %+v, auth %v", first, result.Auth)
	}

	i.Shared = func() bool { return false }
	if _, err := i.Install(link(t, p)); err != nil {
		t.Fatal(err)
	}
	if again := readLocal(t, layout); again.Password != first.Password {
		t.Error("a reinstall changed the password that open shells still use")
	}

	i.Auth = AuthOff
	if result, err := i.Install(link(t, p)); err != nil || result.Auth || readLocal(t, layout).Username != "" {
		t.Fatalf("--auth off: %+v, %v", result, err)
	}

	i.Auth, i.Shared = AuthAuto, func() bool { return true }
	if _, err := i.Install(link(t, p)); err != nil || readLocal(t, layout).Username != "" {
		t.Errorf("a reinstall did not keep the earlier choice: %v", err)
	}

	i.Auth = AuthOn
	if _, err := i.Install(link(t, p)); err != nil || readLocal(t, layout).Password == "" {
		t.Errorf("--auth on: %v", err)
	}
}

func TestOtherAccountsCountOnlyWhenSomeoneCanLogIn(t *testing.T) {
	passwd := "root:x:0:0:root:/root:/bin/bash\n" +
		"me:x:1000:1000::/home/me:/bin/zsh\n" +
		"nobody:x:65534:65534::/nonexistent:/usr/sbin/nologin\n" +
		"backup:x:1001:1001::/srv:/bin/false\n"
	if otherAccount(strings.NewReader(passwd), 1000, 1000) {
		t.Error("a machine with one person counted as shared")
	}
	if !otherAccount(strings.NewReader(passwd+"alice:x:1002:1002::/home/alice:/bin/bash\n"), 1000, 1000) {
		t.Error("another person's account was missed")
	}
}

func TestAHomeDirectorySomeoneElseOwnsMeansShared(t *testing.T) {
	homes := t.TempDir()
	owners := map[string]int{"me": 1000, "lost+found": 0, "alice": 1002}
	for name := range owners {
		os.Mkdir(filepath.Join(homes, name), 0o755)
	}
	owner := func(info fs.FileInfo) (int, bool) {
		id, ok := owners[info.Name()]
		return id, ok
	}
	if !otherHome(homes, 1000, 1000, owner) {
		t.Error("alice's home, as LDAP accounts have, was missed")
	}
	delete(owners, "alice")
	if otherHome(homes, 1000, 1000, owner) {
		t.Error("a machine with one person counted as shared")
	}
	if otherHome(homes, os.Getuid(), 1000, fileOwner) {
		t.Error("directories this test made count as someone else's")
	}
}

func TestRefreshAppliesANewConfigAndKeepsTheLocalSettings(t *testing.T) {
	p, i, layout := installed(t)
	before, _ := os.ReadFile(layout.ConfigFile())
	restarts := 0
	restart := func() error { restarts++; return nil }
	ready := func() error { return nil }

	changed, err := i.Refresh(link(t, p), restart, ready)
	if err != nil || changed || restarts != 0 {
		t.Fatalf("unchanged config: changed %v, restarts %d, err %v", changed, restarts, err)
	}

	p.config = strings.Replace(p.config, `"china"`, `"gfw"`, 1)
	changed, err = i.Refresh(link(t, p), restart, ready)
	if err != nil || !changed || restarts != 1 {
		t.Fatalf("new config: changed %v, restarts %d, err %v", changed, restarts, err)
	}
	after, _ := os.ReadFile(layout.ConfigFile())
	if !strings.Contains(string(after), `"gfw"`) {
		t.Errorf("config was not replaced:\n%s", after)
	}
	old, _ := singbox.ReadLocal(before)
	now, _ := singbox.ReadLocal(after)
	if old != now {
		t.Errorf("local settings changed from %+v to %+v", old, now)
	}

	requests := len(p.requests)
	changed, err = i.Refresh(link(t, p), restart, ready)
	if err != nil || changed {
		t.Fatalf("repeat: changed %v, err %v", changed, err)
	}
	if last := p.requests[requests]; !strings.HasPrefix(last, "/sub/"+token+"?") {
		t.Errorf("repeat asked for %s", last)
	}
}

func TestRefreshRestoresThePreviousConfigWhenSingBoxDoesNotComeUp(t *testing.T) {
	p, i, layout := installed(t)
	before, _ := os.ReadFile(layout.ConfigFile())
	p.config = strings.Replace(p.config, `"china"`, `"gfw"`, 1)
	restarts := 0

	changed, err := i.Refresh(link(t, p),
		func() error { restarts++; return nil },
		func() error { return errors.New("no answer") },
	)

	if err == nil || changed || !strings.Contains(err.Error(), "the previous one is back") {
		t.Fatalf("changed %v, err %v", changed, err)
	}
	after, _ := os.ReadFile(layout.ConfigFile())
	if !bytes.Equal(before, after) || restarts != 2 {
		t.Errorf("restarts %d, config restored %v", restarts, bytes.Equal(before, after))
	}
	if _, err := os.Stat(layout.ETag()); !os.IsNotExist(err) {
		t.Error("the rejected config's ETag was saved")
	}
}

func TestRefreshLeavesTheConfigWhenSingBoxRejectsTheNewOne(t *testing.T) {
	p, i, layout := installed(t)
	before, _ := os.ReadFile(layout.ConfigFile())
	p.config = strings.Replace(p.config, `"china"`, `"reject"`, 1)

	_, err := i.Refresh(link(t, p),
		func() error { t.Error("restarted for a rejected config"); return nil },
		func() error { return nil },
	)

	if err == nil || !strings.Contains(err.Error(), "sing-box rejected the config") {
		t.Fatalf("got %v", err)
	}
	after, _ := os.ReadFile(layout.ConfigFile())
	if !bytes.Equal(before, after) {
		t.Error("the config changed")
	}
}

func TestCheckUpgradeComparesWithWhatIsInstalled(t *testing.T) {
	p, i, _ := installed(t)

	available, _, err := i.CheckUpgrade(link(t, p), "abc123")
	if err != nil || available.Any() {
		t.Fatalf("current versions: %+v, %v", available, err)
	}
	available, _, err = i.CheckUpgrade(link(t, p), "older")
	if err != nil || available != (Available{SBC: "abc123"}) {
		t.Fatalf("older sbc: %+v, %v", available, err)
	}
}

func TestUpgradeInstallsTheCheckedSbc(t *testing.T) {
	p, i, layout := installed(t)

	upgraded, err := i.Upgrade(link(t, p), "older")
	if err != nil || upgraded != (Available{SBC: "abc123"}) {
		t.Fatalf("got %+v, %v", upgraded, err)
	}
	data, _ := os.ReadFile(layout.SBC())
	info, _ := os.Stat(layout.SBC())
	if string(data) != "new sbc" || info.Mode().Perm() != 0o755 {
		t.Errorf("sbc %q with mode %v", data, info.Mode().Perm())
	}

	p.tamper = "sbc/linux-amd64/sbc"
	os.Remove(layout.SBC())
	if _, err := i.Upgrade(link(t, p), "older"); err == nil || !strings.Contains(err.Error(), "does not match") {
		t.Fatalf("tampered: %v", err)
	}
	if _, err := os.Stat(layout.SBC()); !os.IsNotExist(err) {
		t.Error("a tampered sbc was written")
	}
}

func TestTheTrafficCountComesWithEachConfigAndGoesWithoutOne(t *testing.T) {
	p := newPortal(t)
	p.usage = "upload=1; download=2"
	i, layout := installer(t, p)
	if _, err := i.Install(link(t, p)); err != nil {
		t.Fatal(err)
	}
	if usage, _, ok := ReadUsage(layout); !ok || usage != (Usage{Upload: 1, Download: 2}) {
		t.Fatalf("after the install: %+v, %v", usage, ok)
	}
	refresh := func() {
		t.Helper()
		if _, err := i.Refresh(link(t, p), func() error { return nil }, func() error { return nil }); err != nil {
			t.Fatal(err)
		}
	}

	p.usage = "upload=3; download=4; total=0; expire=0"
	refresh()
	if usage, _, _ := ReadUsage(layout); usage != (Usage{Upload: 3, Download: 4}) {
		t.Errorf("after a refresh: %+v", usage)
	}
	p.usage = "upload=5; download=6"
	refresh()
	if usage, _, _ := ReadUsage(layout); usage != (Usage{Upload: 5, Download: 6}) || p.notModified != 1 {
		t.Errorf("after an unchanged config: %+v, %d unchanged answers", usage, p.notModified)
	}
	p.usage = ""
	refresh()
	if _, _, ok := ReadUsage(layout); ok {
		t.Error("a count stayed after the portal stopped sending one")
	}
}

func TestParseUsageNeedsBothCounts(t *testing.T) {
	cases := map[string]bool{
		"upload=1; download=2":                 true,
		"upload=1;download=2;total=0;expire=0": true,
		" download = 2 ; upload = 1 ":          true,
		"upload=1":                             false,
		"upload=-1; download=2":                false,
		"upload=x; download=2":                 false,
		"":                                     false,
	}
	for value, want := range cases {
		if _, ok := ParseUsage(value); ok != want {
			t.Errorf("%q: got %v", value, ok)
		}
	}
}

func TestRefreshAddsTheSpeedTestToAConfigFromBefore(t *testing.T) {
	p := newPortal(t)
	p.config = `{"inbounds":[{"type":"mixed","tag":"mixed","listen":"127.0.0.1","listen_port":1080}],"outbounds":[{"type":"selector","tag":"proxy","outbounds":["direct"]},{"type":"direct","tag":"direct"}],"experimental":{"clash_api":{"default_mode":"china"}}}`
	i, layout := installer(t, p)
	if _, err := i.Install(link(t, p)); err != nil {
		t.Fatal(err)
	}
	installed := readLocal(t, layout)
	if installed.SpeedPort == 0 || installed.SpeedPort == installed.ListenPort || installed.SpeedPort == installed.APIPort {
		t.Fatalf("the install gave the speed test %+v", installed)
	}
	// What an sbc from before the speed test wrote, and the ETag that would
	// otherwise get an answer without a body.
	before := installed
	before.SpeedPort = 0
	current, _ := os.ReadFile(layout.ConfigFile())
	older, _ := singbox.Patch(current, before)
	paths.WriteFile(layout.ConfigFile(), older, 0o600)
	if _, err := i.Refresh(link(t, p), func() error { return nil }, func() error { return nil }); err != nil {
		t.Fatal(err)
	}
	paths.WriteFile(layout.ConfigFile(), older, 0o600)

	changed, err := i.Refresh(link(t, p), func() error { return nil }, func() error { return nil })

	if err != nil || !changed || p.notModified != 0 {
		t.Fatalf("changed %v, %d unchanged answers, err %v", changed, p.notModified, err)
	}
	if after := readLocal(t, layout); after.SpeedPort == 0 || after.ListenPort != installed.ListenPort {
		t.Errorf("after the refresh: %+v", after)
	}
}

func zipArchive(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	for name, body := range files {
		file, err := writer.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		io.WriteString(file, body)
	}
	writer.Close()
	return buffer.Bytes()
}

func TestWindowsGetsSingBoxAndItsLibraryFromTheZip(t *testing.T) {
	p := newPortal(t)
	p.files["sing-box/sing-box-1.14.2-windows-amd64.zip"] = zipArchive(t, map[string]string{
		"sing-box-1.14.2-windows-amd64/sing-box.exe":  "program",
		"sing-box-1.14.2-windows-amd64/libcronet.dll": "library",
		"sing-box-1.14.2-windows-amd64/LICENSE":       "license",
	})
	i, layout := installer(t, p)
	i.OS = "windows"
	layout.Exe = ".exe"
	i.Layout = layout

	// A stand-in sing-box.exe cannot check the config here, so the install
	// stops right after the download.
	_, err := i.Install(link(t, p))

	if err == nil || !strings.Contains(err.Error(), "sing-box rejected the config") {
		t.Fatalf("got %v", err)
	}
	for name, want := range map[string]string{"sing-box.exe": "program", "libcronet.dll": "library"} {
		data, err := os.ReadFile(filepath.Join(layout.Bin(), name))
		if err != nil || string(data) != want {
			t.Errorf("%s: %q, %v", name, data, err)
		}
	}
	if _, err := os.Stat(filepath.Join(layout.Bin(), "LICENSE")); !os.IsNotExist(err) {
		t.Error("the license was written")
	}
	if _, err := os.Stat(filepath.Join(layout.Bin(), "sing-box")); !os.IsNotExist(err) {
		t.Error("a program without the .exe suffix was written")
	}
}
