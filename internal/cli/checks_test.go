package cli

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/diagnostic"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/probe"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

func (h *harness) useSites(t *testing.T, pages map[string]string) *[]string {
	t.Helper()
	var lock sync.Mutex
	var seen []string
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lock.Lock()
		seen = append(seen, r.Header.Get("Proxy-Authorization"))
		lock.Unlock()
		page, ok := pages[r.URL.Host+r.URL.Path]
		if !ok {
			page, ok = pages[r.URL.Host]
		}
		if !ok && r.URL.Host == "details.test" {
			fmt.Fprintf(w, `{"success":true,"ip":%q}`, strings.TrimPrefix(r.URL.Path, "/"))
			return
		}
		if !ok {
			http.Error(w, "down", 502)
			return
		}
		io.WriteString(w, page)
	}))
	t.Cleanup(proxy.Close)
	address, _ := url.Parse(proxy.URL)
	port, _ := strconv.Atoi(address.Port())
	h.writeLocal(t, func(local *singbox.Local) { local.ListenPort = port })
	h.env.Sites = probe.Sites{IPify4: "http://ipify4.test/", IPify6: "http://ipify6.test/", IdentMe4: "http://ident4.test/", IdentMe6: "http://ident6.test/", IPDetails: "http://details.test/"}
	return &seen
}

func reportFrom(t *testing.T, h *harness) diagnosticReport {
	t.Helper()
	var report diagnosticReport
	if err := json.Unmarshal(h.out.Bytes(), &report); err != nil {
		t.Fatalf("%v: %s%s", err, h.out, h.errOut)
	}
	return report
}

func TestIPUsesOrdinaryAuthenticatedProxyAndRetainsSources(t *testing.T) {
	h := newHarness(t)
	h.writeLocal(t, func(local *singbox.Local) { local.Username, local.Password = "sbc", "s3cret" })
	seen := h.useSites(t, map[string]string{"ipify4.test": "203.0.113.7", "ident4.test": "198.51.100.20", "details.test/203.0.113.7": `{"success":true,"ip":"203.0.113.7","country":"US","connection":{"asn":64500,"org":"Example Network"}}`})
	before, _ := os.ReadFile(h.layout.ConfigFile())
	if code := Run(h.env, []string{"ip", "--json"}); code != 0 {
		t.Fatalf("%d: %s%s", code, h.out, h.errOut)
	}
	report := reportFrom(t, h)
	if report.SchemaVersion != 1 || report.Scope != "sbc" || *report.Mode != "mixed" || *report.Route != "china-direct" || !report.ContextValid || len(report.Observations) != 2 {
		t.Fatalf("%+v", report)
	}
	if report.Observations[0].IP != "203.0.113.7" || report.Observations[1].IP != "198.51.100.20" || report.Observations[0].Details.ASN != 64500 {
		t.Fatal(h.out)
	}
	if len(*seen) != 4 {
		t.Fatal(*seen)
	}
	for _, header := range *seen {
		if header != "Basic "+base64.StdEncoding.EncodeToString([]byte("sbc:s3cret")) {
			t.Fatal("proxy authentication missing")
		}
	}
	after, _ := os.ReadFile(h.layout.ConfigFile())
	if string(before) != string(after) || len(h.svc.calls) != 0 || h.api.mode != "china" || h.api.protocol != "trojan" {
		t.Fatal("diagnostic mutated runtime")
	}
	if strings.Contains(h.out.String(), "s3cret") || strings.Contains(h.out.String(), "api_port") {
		t.Fatal("private runtime data escaped")
	}
}

func TestIPPartialFamiliesAndOptionalDetails(t *testing.T) {
	for _, brokenDetails := range []bool{false, true} {
		h := newHarness(t)
		pages := map[string]string{"ipify4.test": "203.0.113.7", "ident4.test": "203.0.113.7", "ipify6.test": "2001:db8::7"}
		if brokenDetails {
			pages["details.test"] = "not JSON"
		}
		seen := h.useSites(t, pages)
		args := []string{"ip", "--json"}
		want := 0
		if !brokenDetails {
			args = append(args, "--dual-stack")
			want = 1
		}
		if code := Run(h.env, args); code != want {
			t.Fatalf("%d: %s%s", code, h.out, h.errOut)
		}
		report := reportFrom(t, h)
		if report.Observations[0].IP != "203.0.113.7" {
			t.Fatal(h.out)
		}
		if brokenDetails && (len(*seen) != 3 || len(report.Warnings) != 1) {
			t.Fatalf("%d: %+v", len(*seen), report)
		}
		if !brokenDetails && (len(report.Observations) != 4 || report.Observations[2].IP != "2001:db8::7" || report.Observations[3].IP != "") {
			t.Fatal(h.out)
		}
	}
}

// TLS fixture certificates trust only synthetic destinations. No skip-verify
// option or public endpoint is used by these tests.
func diagnosticSite(t *testing.T, handler http.Handler) (*httptest.Server, *x509.CertPool) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), DNSNames: []string{"fixture.test"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IsCA: true, BasicConstraintsValid: true}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	roots := x509.NewCertPool()
	roots.AddCert(cert)
	site := httptest.NewUnstartedServer(handler)
	site.TLS = &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}}
	site.StartTLS()
	t.Cleanup(site.Close)
	return site, roots
}

func tunnelProxy(t *testing.T, h *harness, site *httptest.Server) {
	t.Helper()
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "CONNECT" {
			t.Error("expected CONNECT")
			w.WriteHeader(502)
			return
		}
		upstream, err := net.Dial("tcp", site.Listener.Addr().String())
		if err != nil {
			w.WriteHeader(502)
			return
		}
		client, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			upstream.Close()
			return
		}
		defer client.Close()
		defer upstream.Close()
		io.WriteString(client, "HTTP/1.1 200 Connection Established\r\n\r\n")
		done := make(chan struct{})
		go func() { io.Copy(upstream, client); upstream.Close(); close(done) }()
		io.Copy(client, upstream)
		client.Close()
		<-done
	}))
	t.Cleanup(proxy.Close)
	u, _ := url.Parse(proxy.URL)
	port, _ := strconv.Atoi(u.Port())
	h.writeLocal(t, func(local *singbox.Local) { local.ListenPort = port })
}

func TestWebsiteChecksDistinguishHTTPAndTransportGoals(t *testing.T) {
	for _, status := range []int{200, 204, 302, 403, 429, 503} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			h := newHarness(t)
			var calls atomic.Int32
			site, roots := diagnosticSite(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.Header().Set("Location", "https://must-not-follow.test/private?secret=fixture")
				w.WriteHeader(status)
				fmt.Fprint(w, "body")
			}))
			h.env.DiagnosticRoots = roots
			tunnelProxy(t, h, site)
			u, _ := url.Parse(site.URL)
			target := "https://fixture.test:" + u.Port() + "/private-fixture-path?secret=fixture-query-value"
			code := Run(h.env, []string{"check", target, "--samples", "3", "--json"})
			want := 1
			if status < 300 {
				want = 0
			}
			if code != want || calls.Load() != 3 {
				t.Fatalf("%d, %d: %s%s", code, calls.Load(), h.out, h.errOut)
			}
			report := reportFrom(t, h)
			if len(report.Observations) != 1 || len(report.Observations[0].Samples) != 3 {
				t.Fatal(h.out)
			}
			for _, sample := range report.Observations[0].Samples {
				if sample.HTTPStatus == nil || *sample.HTTPStatus != status || sample.FirstResponseMS == nil || sample.Transport != "responded" {
					t.Fatalf("%+v", sample)
				}
			}
			for _, secret := range []string{"private-fixture-path", "fixture-query-value", "must-not-follow", "s3cret"} {
				if strings.Contains(h.out.String()+h.errOut.String(), secret) {
					t.Fatalf("report contains %s", secret)
				}
			}
		})
	}
}

func TestWebsiteTLSFailureAndEmptyListAreNotSilentSuccess(t *testing.T) {
	h := newHarness(t)
	site, _ := diagnosticSite(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { fmt.Fprint(w, "body") }))
	tunnelProxy(t, h, site)
	u, _ := url.Parse(site.URL)
	if code := Run(h.env, []string{"check", "https://fixture.test:" + u.Port(), "--json"}); code != 1 {
		t.Fatal(code)
	}
	if reportFrom(t, h).Observations[0].Samples[0].Issue != "tls" {
		t.Fatal(h.out)
	}
	h.out.Reset()
	h.errOut.Reset()
	paths.WriteFile(targetPath(h.layout), []byte(`{"schema_version":1,"targets":[]}`), 0o600)
	if code := Run(h.env, []string{"check"}); code != 2 || h.out.Len() != 0 {
		t.Fatal(code, h.out)
	}
}

func TestDownloadIsExplicitBoundedAndReportsActualBytes(t *testing.T) {
	h := newHarness(t)
	var calls atomic.Int32
	site, roots := diagnosticSite(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Query().Get("bytes") != "1000000" || r.Header.Get("Accept-Encoding") != "identity" {
			t.Error("wrong download budget or encoding")
		}
		fmt.Fprint(w, strings.Repeat("x", 1_000_000))
	}))
	h.env.DiagnosticRoots = roots
	h.env.Sites.Download = site.URL + "/download"
	tunnelProxy(t, h, site)
	if code := Run(h.env, []string{"speed"}); code != 0 || calls.Load() != 0 {
		t.Fatal(code, calls.Load())
	}
	h.out.Reset()
	if code := Run(h.env, []string{"speed", "download", "--size", "1MB", "--json"}); code != 0 {
		t.Fatalf("%d: %s%s", code, h.out, h.errOut)
	}
	sample := reportFrom(t, h).Observations[0].Samples[0]
	if calls.Load() != 1 || sample.PayloadBytes != 1_000_000 || sample.MbitPerSecond == nil || sample.TransferMS == nil || sample.FirstResponseMS == nil || sample.StopReason != "payload-complete" {
		t.Fatalf("%+v", sample)
	}
	h.out.Reset()
	if code := Run(h.env, []string{"speed", "download", "--size", "1MB"}); code != 0 || !strings.Contains(h.out.String(), "Downloaded 1000000 bytes; transfer ") || !strings.Contains(h.out.String(), "stop: payload budget complete") {
		t.Fatalf("%d: %s%s", code, h.out, h.errOut)
	}
}

func TestNativeTUNReportsMissingEvidenceWithoutFallingBack(t *testing.T) {
	f := newRuntimeFixture(t)
	var calls atomic.Int32
	site, roots := diagnosticSite(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if strings.HasPrefix(r.URL.Path, "/details/") {
			fmt.Fprintf(w, `{"success":true,"ip":%q}`, strings.TrimPrefix(r.URL.Path, "/details/"))
			return
		}
		fmt.Fprint(w, "203.0.113.7")
	}))
	f.state.Transaction, f.state.InboundTag, f.state.IPv6Policy = f.capture.Transaction, "fixture-tun", "public-ipv6-disabled-v1"
	tun.WriteState(f.h.layout, f.state)
	f.h.env.DiagnosticRoots = roots
	f.h.env.Sites = probe.Sites{IPify4: site.URL, IdentMe4: site.URL, IPify6: "https://must-not-probe.test/", IdentMe6: "https://must-not-probe.test/", IPDetails: site.URL + "/details"}
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	if code := Run(f.h.env, []string{"ip", "--dual-stack", "--json"}); code != 1 {
		t.Fatal(code)
	}
	report := reportFrom(t, f.h)
	if report.Observations[0].IP != "203.0.113.7" || report.Observations[0].Samples[0].Capture.Verified || report.Observations[2].Policy != "policy-skipped-not-verified" || calls.Load() != 3 {
		t.Fatal(f.h.out, calls.Load())
	}
	f.untouched(t)
}

func TestNativeTUNMatchesOnlyItsOwnSourceSockets(t *testing.T) {
	f := newRuntimeFixture(t)
	var mu sync.Mutex
	active := map[net.Conn]int{}
	nextID := 0
	site, roots := diagnosticSite(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(40 * time.Millisecond)
		if strings.HasPrefix(r.URL.Path, "/details/") {
			fmt.Fprintf(w, `{"success":true,"ip":%q}`, strings.TrimPrefix(r.URL.Path, "/details/"))
		} else {
			fmt.Fprint(w, "203.0.113.7")
		}
	}))
	site.Config.ConnState = func(conn net.Conn, state http.ConnState) {
		mu.Lock()
		defer mu.Unlock()
		if state == http.StateNew || state == http.StateActive {
			if _, ok := active[conn]; !ok {
				nextID++
				active[conn] = nextID
			}
		}
		if state == http.StateClosed {
			delete(active, conn)
		}
	}
	port := site.Listener.Addr().(*net.TCPAddr).Port
	original := f.server.Config.Handler
	f.server.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/connections" {
			original.ServeHTTP(w, r)
			return
		}
		if r.Header.Get("Authorization") != "Bearer "+f.state.APISecret {
			w.WriteHeader(401)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		var connections []api.Connection
		for conn, id := range active {
			host, sourcePort, _ := net.SplitHostPort(conn.RemoteAddr().String())
			connections = append(connections, api.Connection{ID: fmt.Sprint(id), Metadata: api.ConnectionMetadata{Type: "tun/fixture-tun", Network: "tcp", SourceIP: host, SourcePort: sourcePort, DestinationIP: "127.0.0.1", DestinationPort: strconv.Itoa(port)}, Chains: []string{"direct"}})
		}
		json.NewEncoder(w).Encode(map[string]any{"connections": connections})
	})
	f.state.Transaction, f.state.InboundTag = f.capture.Transaction, "fixture-tun"
	tun.WriteState(f.h.layout, f.state)
	f.h.env.DiagnosticRoots = roots
	f.h.env.Sites = probe.Sites{IPify4: site.URL, IdentMe4: site.URL, IPDetails: site.URL + "/details"}
	if code := Run(f.h.env, []string{"ip", "--json"}); code != 0 {
		t.Fatalf("%d: %s%s", code, f.h.out, f.h.errOut)
	}
	for _, row := range reportFrom(t, f.h).Observations {
		if !row.Samples[0].Capture.Verified || row.Samples[0].Capture.Path != "direct" {
			t.Fatal(f.h.out)
		}
	}
	f.untouched(t)
}

func TestDiagnosticCancellationAndObservedRouteChanges(t *testing.T) {
	for _, changeRoute := range []bool{false, true} {
		h := newHarness(t)
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		h.env.Context = ctx
		site, roots := diagnosticSite(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if changeRoute {
				h.api.lock.Lock()
				h.api.mode = "direct"
				h.api.lock.Unlock()
			} else {
				cancel()
			}
			<-r.Context().Done()
		}))
		h.env.DiagnosticRoots = roots
		tunnelProxy(t, h, site)
		u, _ := url.Parse(site.URL)
		code := Run(h.env, []string{"check", "https://fixture.test:" + u.Port(), "--json"})
		report := reportFrom(t, h)
		if changeRoute && (code != 1 || report.ContextValid || report.Issue != "runtime-changed") {
			t.Fatalf("%d: %+v", code, report)
		}
		if !changeRoute && (code != 130 || !report.Interrupted) {
			t.Fatalf("%d: %+v", code, report)
		}
	}
}

func TestTargetOperationsAreOfflinePrivateAndRoundTrip(t *testing.T) {
	h := newHarness(t)
	h.env.TunCapture = func() tun.CaptureState { t.Fatal("list operation inspected capture"); return tun.CaptureState{} }
	if code := Run(h.env, []string{"check", "targets", "list"}); code != 0 {
		t.Fatal(code)
	}
	if _, err := os.Stat(targetPath(h.layout)); !os.IsNotExist(err) {
		t.Fatal("read-only default wrote a file")
	}
	h.out.Reset()
	if code := Run(h.env, []string{"check", "targets", "add", "https://fixture.test/private?secret=fixture"}); code != 0 {
		t.Fatal(code)
	}
	info, _ := os.Stat(targetPath(h.layout))
	if info.Mode().Perm() != 0o600 {
		t.Fatal(info.Mode())
	}
	h.out.Reset()
	Run(h.env, []string{"check", "targets", "export"})
	list, err := diagnostic.DecodeList(strings.NewReader(h.out.String()))
	if err != nil || len(list.Targets) != 5 {
		t.Fatal(err, h.out)
	}
	if list.Targets[4].URL != "https://fixture.test/private?secret=fixture" {
		t.Fatal("export lost URL semantics")
	}
	before, _ := os.ReadFile(targetPath(h.layout))
	bad := targetPath(h.layout) + ".bad"
	os.WriteFile(bad, []byte(`{"schema_version":1,"targets":[{"id":"bad","url":"file:///secret"}]}`), 0o600)
	if code := Run(h.env, []string{"check", "targets", "import", bad}); code != 2 {
		t.Fatal(code)
	}
	after, _ := os.ReadFile(targetPath(h.layout))
	if string(before) != string(after) {
		t.Fatal("failed import changed the list")
	}
}

func TestDiagnosticChineseOutput(t *testing.T) {
	i18n.SetChinese(true)
	defer i18n.SetChinese(false)
	h := newHarness(t)
	h.useSites(t, map[string]string{"ipify4.test": "203.0.113.7", "ident4.test": "203.0.113.7"})
	if code := Run(h.env, []string{"ip"}); code != 0 {
		t.Fatal(code, h.errOut)
	}
	if !strings.Contains(h.out.String(), "首响应") || !strings.Contains(h.out.String(), "未验证") {
		t.Fatal(h.out)
	}
}

func TestStatusShowsTheTrafficCountOfTheLastRefresh(t *testing.T) {
	h := newHarness(t)
	paths.WriteFile(h.layout.Usage(), []byte("upload=734003200; download=16428249907\n"), 0o600)
	at := time.Date(2026, 10, 3, 14, 5, 0, 0, time.Local)
	os.Chtimes(h.layout.Usage(), at, at)
	Run(h.env, []string{"status"})
	want := "service:  running\nproxy:    127.0.0.1:2080\nshells:   off\ntraffic:  700.00 MiB up, 15.30 GiB down this billing cycle, as of 2026-10-03 14:05\nrefresh: auto on, interval 6h, last attempt not run yet\nupgrade: auto on, interval 6h, last attempt not run yet\nBackground schedules need setup. Set either auto policy to retry.\nroute:    china-direct\nprotocol: trojan\n"
	if h.out.String() != want {
		t.Errorf("got\n%s", h.out)
	}
}

func TestFormatBytesUsesBinaryUnitsAsThePortalDoes(t *testing.T) {
	for count, want := range map[int64]string{0: "0 B", 1023: "1023 B", 1536: "1.50 KiB", 734003200: "700.00 MiB", 16428249907: "15.30 GiB", 5 << 40: "5.00 TiB", 5000 << 40: "5000.00 TiB"} {
		if got := formatBytes(count); got != want {
			t.Errorf("%d: %s", count, got)
		}
	}
}
