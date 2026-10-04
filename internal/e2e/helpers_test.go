//go:build e2e

package e2e

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/powershell"
)

// token is the stand-in portal's subscription token, 22 characters as the
// portal makes them.
const token = "e2eToken-e2eToken-e2eT"

// pinnedSingBox returns the sing-box version releases ship.
func pinnedSingBox(t *testing.T, root string) string {
	t.Helper()
	inventory, err := os.ReadFile(filepath.Join(root, "config", "inventory", "example.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	match := regexp.MustCompile(`(?m)^sing_box_version:\s*(\S+)`).FindSubmatch(inventory)
	if match == nil {
		t.Fatal("example.yaml names no sing_box_version")
	}
	return string(match[1])
}

// exe is the suffix of program names on this platform.
var exe = map[string]string{"windows": ".exe"}[runtime.GOOS]

// archiveName is the upstream sing-box archive for this platform, a zip on
// Windows and a tar.gz elsewhere.
func archiveName(version, platform string) string {
	if runtime.GOOS == "windows" {
		return fmt.Sprintf("sing-box-%s-%s.zip", version, platform)
	}
	return fmt.Sprintf("sing-box-%s-%s.tar.gz", version, platform)
}

// fetchSingBox downloads the upstream archive for platform and checks it
// against the digest GitHub publishes, as the release builder does.
func fetchSingBox(t *testing.T, version, platform string) []byte {
	t.Helper()
	name := archiveName(version, platform)
	request, _ := http.NewRequest(http.MethodGet, "https://api.github.com/repos/SagerNet/sing-box/releases/tags/v"+version, nil)
	request.Header.Set("Accept", "application/vnd.github+json")
	if githubToken := os.Getenv("GITHUB_TOKEN"); githubToken != "" {
		request.Header.Set("Authorization", "Bearer "+githubToken)
	}
	var release struct {
		Assets []struct {
			Name   string `json:"name"`
			URL    string `json:"browser_download_url"`
			Digest string `json:"digest"`
		} `json:"assets"`
	}
	if err := json.Unmarshal(get(t, request), &release); err != nil {
		t.Fatal(err)
	}
	for _, asset := range release.Assets {
		if asset.Name != name {
			continue
		}
		download, _ := http.NewRequest(http.MethodGet, asset.URL, nil)
		data := get(t, download)
		sum := sha256.Sum256(data)
		if want := strings.TrimPrefix(asset.Digest, "sha256:"); want == "" || hex.EncodeToString(sum[:]) != want {
			t.Fatalf("%s does not match the digest GitHub published, %q", name, asset.Digest)
		}
		return data
	}
	t.Fatalf("sing-box v%s has no asset %s", version, name)
	return nil
}

func get(t *testing.T, request *http.Request) []byte {
	t.Helper()
	response, err := (&http.Client{Timeout: 5 * time.Minute}).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK {
		t.Fatalf("%s answered %s", request.URL, response.Status)
	}
	return data
}

// extractSingBox writes the sing-box binary from archive to a directory of
// its own and returns its path.
func extractSingBox(t *testing.T, archive []byte) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		return extractSingBoxZip(t, archive)
	}
	compressed, err := gzip.NewReader(bytes.NewReader(archive))
	if err != nil {
		t.Fatal(err)
	}
	reader := tar.NewReader(compressed)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			t.Fatal("the archive holds no sing-box binary")
		}
		if err != nil {
			t.Fatal(err)
		}
		if path.Base(header.Name) != "sing-box" || header.Typeflag != tar.TypeReg {
			continue
		}
		binary := filepath.Join(t.TempDir(), "sing-box")
		data, err := io.ReadAll(reader)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(binary, data, 0o755); err != nil {
			t.Fatal(err)
		}
		return binary
	}
}

func extractSingBoxZip(t *testing.T, archive []byte) string {
	t.Helper()
	reader, err := zip.NewReader(bytes.NewReader(archive), int64(len(archive)))
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range reader.File {
		if path.Base(file.Name) != "sing-box.exe" {
			continue
		}
		content, err := file.Open()
		if err != nil {
			t.Fatal(err)
		}
		data, err := io.ReadAll(content)
		content.Close()
		if err != nil {
			t.Fatal(err)
		}
		binary := filepath.Join(t.TempDir(), "sing-box.exe")
		if err := os.WriteFile(binary, data, 0o755); err != nil {
			t.Fatal(err)
		}
		return binary
	}
	t.Fatal("the archive holds no sing-box binary")
	return ""
}

// buildSBC builds sbc trusting publicKey beside the real keys. An overlay
// adds the key at build time, so the source tree stays as it is.
func buildSBC(t *testing.T, root, publicKey string) string {
	t.Helper()
	dir := t.TempDir()
	key := filepath.Join(dir, "key.go")
	source := "package trust\n\nfunc init() { Keys = append(Keys, " + strconv.Quote(publicKey) + ") }\n"
	if err := os.WriteFile(key, []byte(source), 0o644); err != nil {
		t.Fatal(err)
	}
	overlay, _ := json.Marshal(map[string]any{"Replace": map[string]string{
		filepath.Join(root, "internal", "trust", "zz_e2e_key.go"): key,
	}})
	overlayFile := filepath.Join(dir, "overlay.json")
	if err := os.WriteFile(overlayFile, overlay, 0o644); err != nil {
		t.Fatal(err)
	}
	binary := filepath.Join(dir, "sbc"+exe)
	build := exec.Command("go", "build", "-buildvcs=false", "-overlay", overlayFile, "-ldflags", "-X main.version=e2e", "-o", binary, "./cmd/sbc")
	build.Dir = root
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build sbc: %v\n%s", err, output)
	}
	return binary
}

func freePort(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port
}

// holdPort keeps a loopback port taken until the test ends.
func holdPort(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	return listener.Addr().(*net.TCPAddr).Port
}

// startUpstream runs a sing-box that stands in for the server: a local SOCKS
// and HTTP proxy that sends everything out directly.
func startUpstream(t *testing.T, singBox string) int {
	t.Helper()
	port := freePort(t)
	dir := t.TempDir()
	config := fmt.Sprintf(`{"log": {"level": "warn"},
"inbounds": [{"type": "mixed", "tag": "in", "listen": "127.0.0.1", "listen_port": %d}],
"outbounds": [{"type": "direct", "tag": "direct"}]}`, port)
	configFile := filepath.Join(dir, "config.json")
	if err := os.WriteFile(configFile, []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	process := exec.Command(singBox, "run", "-D", dir, "-c", configFile)
	process.Stdout, process.Stderr = os.Stderr, os.Stderr
	if err := process.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		process.Process.Kill()
		process.Wait()
	})
	deadline := time.Now().Add(15 * time.Second)
	for {
		connection, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
		if err == nil {
			connection.Close()
			return port
		}
		if time.Now().After(deadline) {
			t.Fatalf("the upstream sing-box did not listen: %v", err)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// blockedTag names the inbound that clientConfig adds on a port in use.
const blockedTag = "blocked-by-e2e"

// clientConfig is what the stand-in portal serves: the portal's shape, with
// protocols that reach the upstream instead of a real server. A config with
// blocked above zero also listens there, and sing-box cannot start with it.
func clientConfig(upstream, blocked int) string {
	inbounds := `[{"type": "mixed", "tag": "mixed", "listen": "127.0.0.1", "listen_port": 1080}]`
	if blocked > 0 {
		inbounds = fmt.Sprintf(`[{"type": "mixed", "tag": "mixed", "listen": "127.0.0.1", "listen_port": 1080},
{"type": "mixed", "tag": %q, "listen": "127.0.0.1", "listen_port": %d}]`, blockedTag, blocked)
	}
	return fmt.Sprintf(`{"log": {"level": "warn"},
"inbounds": %s,
"outbounds": [
  {"type": "selector", "tag": "proxy", "outbounds": ["socks", "http"], "default": "socks"},
  {"type": "socks", "tag": "socks", "server": "127.0.0.1", "server_port": %d},
  {"type": "http", "tag": "http", "server": "127.0.0.1", "server_port": %d},
  {"type": "direct", "tag": "direct"}
],
"route": {"rules": [
  {"clash_mode": "china", "outbound": "proxy"},
  {"clash_mode": "gfw", "outbound": "proxy"},
  {"clash_mode": "ai", "outbound": "direct"},
  {"clash_mode": "global", "outbound": "proxy"}
], "final": "proxy"},
"experimental": {"cache_file": {"enabled": true}, "clash_api": {"default_mode": "china"}}}`, inbounds, upstream, upstream)
}

// portal stands in for the subscription side of the real one: the config
// envelope with an ETag and the traffic header, the signed manifest and the
// files it lists.
type portal struct {
	server   *httptest.Server
	lock     sync.Mutex
	config   string
	requests map[string]int
}

func startPortal(t *testing.T, key ed25519.PrivateKey, singBox string, files map[string][]byte, config string, installer []byte) *portal {
	t.Helper()
	type file struct {
		Path   string `json:"path"`
		Size   int    `json:"size"`
		SHA256 string `json:"sha256"`
	}
	var listed []file
	for name, data := range files {
		sum := sha256.Sum256(data)
		listed = append(listed, file{name, len(data), hex.EncodeToString(sum[:])})
	}
	manifest, _ := json.Marshal(map[string]any{"version": 1, "sbc": "e2e", "sing_box": singBox, "files": listed})
	files["manifest.json"] = manifest
	files["manifest.json.sig"] = []byte(base64.StdEncoding.EncodeToString(ed25519.Sign(key, manifest)) + "\n")

	p := &portal{config: config, requests: make(map[string]int)}
	prefix := "/sub/" + token
	p.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.lock.Lock()
		p.requests[r.URL.Path]++
		p.lock.Unlock()
		switch {
		case r.URL.Path == "/install.ps1":
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			w.Write(bytes.TrimPrefix(installer, []byte("\xef\xbb\xbf")))
		case r.URL.Path == prefix && r.URL.Query().Get("format") == "sbc":
			p.lock.Lock()
			body := `{"version": 1, "config": ` + p.config + `}`
			p.lock.Unlock()
			sum := sha256.Sum256([]byte(body))
			etag := `"` + hex.EncodeToString(sum[:8]) + `"`
			w.Header().Set("ETag", etag)
			w.Header().Set("subscription-userinfo", "upload=1000; download=2000")
			if r.Header.Get("If-None-Match") == etag {
				w.WriteHeader(http.StatusNotModified)
				return
			}
			io.WriteString(w, body)
		case strings.HasPrefix(r.URL.Path, prefix+"/files/"):
			data, ok := files[strings.TrimPrefix(r.URL.Path, prefix+"/files/")]
			if !ok {
				http.NotFound(w, r)
				return
			}
			w.Write(data)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(p.server.Close)
	return p
}

func (p *portal) requestCount(path string) int {
	p.lock.Lock()
	defer p.lock.Unlock()
	return p.requests[path]
}

func (p *portal) link() string { return p.server.URL + "/sub/" + token }

func (p *portal) setConfig(config string) {
	p.lock.Lock()
	defer p.lock.Unlock()
	p.config = config
}

// runCode runs a command in English and returns its output and exit code.
func runCode(t *testing.T, env []string, name string, args ...string) (string, int) {
	t.Helper()
	return runInput(t, "", env, name, args...)
}

// runInput is runCode with text on the command's standard input.
func runInput(t *testing.T, input string, env []string, name string, args ...string) (string, int) {
	t.Helper()
	command := exec.Command(name, args...)
	command.Env = append(append(os.Environ(), "SBC_LANG=en"), env...)
	if name == "powershell.exe" {
		command = powershell.Command(context.Background(), args...)
		command.Env = append(command.Env, append([]string{"SBC_LANG=en"}, env...)...)
	}
	command.Stdin = strings.NewReader(input)
	output, err := command.CombinedOutput()
	var exit *exec.ExitError
	switch {
	case err == nil:
		return string(output), 0
	case errors.As(err, &exit):
		return string(output), exit.ExitCode()
	default:
		t.Fatalf("%s: %v", name, err)
		return "", 0
	}
}

func run(t *testing.T, env []string, name string, args ...string) string {
	t.Helper()
	output, code := runCode(t, env, name, args...)
	if code != 0 {
		t.Fatalf("%s %s exited %d:\n%s", filepath.Base(name), strings.Join(args, " "), code, output)
	}
	return output
}

func expect(t *testing.T, output string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(output, want) {
			t.Errorf("no %q in:\n%s", want, output)
		}
	}
}

// proxyURL reads the proxy, with its password when it has one, from what
// 'sbc env' prints.
func proxyURL(t *testing.T, env string) *url.URL {
	t.Helper()
	for _, line := range strings.Split(env, "\n") {
		prefix := "export http_proxy="
		if runtime.GOOS == "windows" {
			prefix = "$env:HTTP_PROXY = "
		}
		if value, ok := strings.CutPrefix(line, prefix); ok {
			proxy, err := url.Parse(strings.Trim(value, `'"`))
			if err != nil {
				t.Fatal(err)
			}
			return proxy
		}
	}
	t.Fatalf("sbc env printed no http_proxy:\n%s", env)
	return nil
}

// expectPage loads target through proxy, waiting out a sing-box restart.
func expectPage(t *testing.T, proxy *url.URL, target string) {
	t.Helper()
	client := &http.Client{
		Transport: &http.Transport{Proxy: http.ProxyURL(proxy), DisableKeepAlives: true},
		Timeout:   5 * time.Second,
	}
	deadline := time.Now().Add(30 * time.Second)
	for {
		response, err := client.Get(target)
		if err == nil {
			body, _ := io.ReadAll(response.Body)
			response.Body.Close()
			if string(body) == "reached" {
				return
			}
			err = fmt.Errorf("%s: %q", response.Status, body)
		}
		if time.Now().After(deadline) {
			t.Fatalf("no page loads through the proxy: %v", err)
		}
		time.Sleep(500 * time.Millisecond)
	}
}

// pgrep returns the ids of the processes running binary, one per line.
func pgrep(binary string) string {
	if runtime.GOOS == "windows" {
		// The proxy task's process runs in another session, which WMI sees.
		output, _ := powershell.Command(context.Background(), "-NoProfile", "-NonInteractive", "-Command",
			"(Get-CimInstance Win32_Process -Filter \"Name = 'sing-box.exe'\" | Where-Object ExecutablePath -eq '"+binary+"').ProcessId").Output()
		return strings.TrimSpace(string(output))
	}
	output, _ := exec.Command("pgrep", "-f", regexp.QuoteMeta(binary)+" run").Output()
	return strings.TrimSpace(string(output))
}

func singBoxPID(t *testing.T, binary string) string {
	t.Helper()
	pids := pgrep(binary)
	if pids == "" || strings.Contains(pids, "\n") {
		t.Fatalf("expected one sing-box process, found %q", pids)
	}
	return pids
}

// expectServiceGone checks that uninstall took the services away: the files
// sbc's service manager wrote, or the scheduled tasks on Windows.
func expectServiceGone(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		for _, task := range []string{"sbc-proxy", "sbc-refresh"} {
			if exec.Command("schtasks.exe", "/Query", "/TN", task).Run() == nil {
				t.Errorf("uninstall left the task %s", task)
			}
		}
		return
	}
	for _, file := range serviceFiles(t) {
		if _, err := os.Stat(file); !os.IsNotExist(err) {
			t.Errorf("uninstall left %s", file)
		}
	}
}

// serviceFiles are what sbc's service manager writes on Linux and macOS.
func serviceFiles(t *testing.T) []string {
	t.Helper()
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "darwin" {
		dir := filepath.Join(home, "Library", "LaunchAgents")
		return []string{filepath.Join(dir, "io.sbc.proxy.plist"), filepath.Join(dir, "io.sbc.refresh.plist")}
	}
	dir := os.Getenv("XDG_CONFIG_HOME")
	if dir == "" {
		dir = filepath.Join(home, ".config")
	}
	dir = filepath.Join(dir, "systemd", "user")
	return []string{filepath.Join(dir, "sbc.service"), filepath.Join(dir, "sbc-refresh.service"), filepath.Join(dir, "sbc-refresh.timer")}
}
