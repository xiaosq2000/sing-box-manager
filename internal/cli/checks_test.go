package cli

import (
	"encoding/base64"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/probe"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

// useSites points the proxy at a stand-in that answers for the sites 'sbc ip'
// asks, with pages by host, and returns the Proxy-Authorization headers it
// saw. A host without a page answers 502, as a site that is down.
func (h *harness) useSites(t *testing.T, pages map[string]string) *[]string {
	t.Helper()
	var lock sync.Mutex
	var seen []string
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lock.Lock()
		seen = append(seen, r.Header.Get("Proxy-Authorization"))
		lock.Unlock()
		page, ok := pages[r.URL.Host]
		if !ok {
			http.Error(w, "down", http.StatusBadGateway)
			return
		}
		io.WriteString(w, page)
	}))
	t.Cleanup(proxy.Close)
	address, _ := url.Parse(proxy.URL)
	port, _ := strconv.Atoi(address.Port())
	h.writeLocal(t, func(local *singbox.Local) { local.ListenPort = port })
	h.env.Sites = probe.Sites{Gemini: "http://gemini.test/", Abroad: "http://ipinfo.test/json", China: "http://cip.test/"}
	return &seen
}

func TestIPShowsTheRouteAndWhereEachKindOfSitePlacesTheMachine(t *testing.T) {
	h := newHarness(t)
	h.writeLocal(t, func(local *singbox.Local) { local.Username, local.Password = "sbc", "s3cret" })
	seen := h.useSites(t, map[string]string{
		"gemini.test": `[["AA2Yr",2,1,200,"USA",null]]`,
		"ipinfo.test": `{"ip": "203.0.113.7", "city": "Los Angeles", "country": "US"}`,
		"cip.test":    "IP\t: 198.51.100.20\n地址\t: 中国  上海  上海\n运营商\t: 电信\n",
	})

	code := Run(h.env, []string{"ip"})

	want := "route:    china\ngemini:   USA\nabroad:   Los Angeles, US, 203.0.113.7\nchina:    中国 上海 上海 电信, 198.51.100.20\n"
	if code != 0 || h.out.String() != want {
		t.Fatalf("code %d, got\n%s%s", code, h.out, h.errOut)
	}
	password := "Basic " + base64.StdEncoding.EncodeToString([]byte("sbc:s3cret"))
	if len(*seen) != 3 || (*seen)[0] != password || (*seen)[1] != password || (*seen)[2] != password {
		t.Errorf("the proxy saw %q", *seen)
	}
}

func TestIPReportsASiteThatFailsAndARegionGeminiDoesNotServe(t *testing.T) {
	h := newHarness(t)
	h.useSites(t, map[string]string{
		"gemini.test": `,2,1,200,"HKG"`,
		"cip.test":    "IP\t: 198.51.100.20\n",
	})

	code := Run(h.env, []string{"ip"})

	want := "route:    china\n" +
		"gemini:   HKG (Gemini does not serve this region)\n" +
		"abroad:   failed: the site answered 502 Bad Gateway\n" +
		"china:    198.51.100.20\n"
	if code != 1 || h.out.String() != want {
		t.Errorf("code %d, got\n%s", code, h.out)
	}
}

func TestSpeedAllTimesEveryProtocolAndListsTheFastestFirst(t *testing.T) {
	h := newHarness(t)
	h.api.protocols = []string{"trojan", "naive", "hysteria2", "tuic"}
	h.api.delays = map[string][]int{"trojan": {300, 140, 150}, "hysteria2": {95, 90, 100}, "tuic": {-1, 80, 85}}

	code := Run(h.env, []string{"speed", "--all"})

	// Latency matters little for downloads, so it suggests no switch.
	want := "Time to load a small page through each protocol, the median of 3 tries:\n" +
		"  tuic       80 ms, 1 of 3 tries failed\n" +
		"  hysteria2  95 ms\n" +
		"  trojan     150 ms (current)\n" +
		"  naive      failed\n"
	if code != 0 || h.out.String() != want {
		t.Errorf("code %d, got\n%s%s", code, h.out, h.errOut)
	}
	if h.api.protocol != "trojan" {
		t.Errorf("the test switched the protocol to %s", h.api.protocol)
	}
}

// useDownloads gives the harness a speed test port with a password, and
// downloads that take, for each protocol the speed test's selector points
// at, the time took gives, or fail with failures' error. It returns the
// requests the downloads saw.
func (h *harness) useDownloads(t *testing.T, took map[string]time.Duration, failures map[string]error) *[]string {
	t.Helper()
	h.writeLocal(t, func(local *singbox.Local) {
		local.SpeedPort, local.Username, local.Password = 3080, "sbc", "s3cret"
	})
	h.env.Sites.Download = "http://speed.test/__down"
	var seen []string
	h.env.TimeDownload = func(client *http.Client, page string, limit time.Duration) (int64, time.Duration, error) {
		request, _ := http.NewRequest(http.MethodGet, page, nil)
		proxy, _ := client.Transport.(*http.Transport).Proxy(request)
		protocol := h.api.speedTest()
		seen = append(seen, protocol+" "+proxy.String()+" "+page+" "+limit.String())
		if err := failures[protocol]; err != nil {
			return 0, 0, err
		}
		size, _ := strconv.Atoi(strings.TrimPrefix(request.URL.RawQuery, "bytes="))
		return int64(size), took[protocol], nil
	}
	return &seen
}

func TestSpeedDownloadAllTimesEachProtocolThroughTheSpeedTest(t *testing.T) {
	h := newHarness(t)
	h.api.protocols = []string{"trojan", "naive", "hysteria2"}
	seen := h.useDownloads(t,
		map[string]time.Duration{"trojan": 4 * time.Second, "naive": 2 * time.Second},
		map[string]error{"hysteria2": errors.New("no answer within 30s")},
	)

	code := Run(h.env, []string{"speed", "--download", "--all"})

	want := "Downloading 20 MB through each protocol, which adds up to 120 MB to the server's monthly transfer:\n" +
		"  trojan     40.0 Mbit/s (current)\n" +
		"  naive      80.0 Mbit/s\n" +
		"  hysteria2  failed: no answer within 30s\n" +
		"naive downloads faster; to switch: sbc protocol naive\n"
	if code != 0 || h.out.String() != want {
		t.Fatalf("code %d, got\n%s%s", code, h.out, h.errOut)
	}
	through := "http://sbc:s3cret@127.0.0.1:3080 http://speed.test/__down?bytes=20000000 15s"
	if len(*seen) != 3 || (*seen)[0] != "trojan "+through || (*seen)[1] != "naive "+through || (*seen)[2] != "hysteria2 "+through {
		t.Errorf("the downloads saw %q", *seen)
	}
	if h.api.protocol != "trojan" {
		t.Errorf("the test switched the protocol in use to %s", h.api.protocol)
	}
}

func TestSpeedDownloadTakesASizeAndSuggestsNoSwitchForASmallGain(t *testing.T) {
	h := newHarness(t)
	h.useDownloads(t, map[string]time.Duration{"trojan": time.Second, "naive": 800 * time.Millisecond}, nil)

	Run(h.env, []string{"speed", "--download", "5", "--all"})

	want := "Downloading 5 MB through each protocol, which adds up to 20 MB to the server's monthly transfer:\n" +
		"  trojan  40.0 Mbit/s (current)\n" +
		"  naive   50.0 Mbit/s\n"
	if h.out.String() != want {
		t.Errorf("got\n%s%s", h.out, h.errOut)
	}
}

func TestSpeedDownloadSaysWhatItNeeds(t *testing.T) {
	h := newHarness(t)
	for _, args := range [][]string{{"--download", "0"}, {"--download", "100"}, {"--download", "5", "6"}, {"--download", "-5"}, {"--all", "--all"}, {"--download", "--download"}, {"--upload"}} {
		h.errOut.Reset()
		if code := Run(h.env, append([]string{"speed"}, args...)); code != 2 || !strings.Contains(h.errOut.String(), "an optional size from 1 to 99 MB") {
			t.Errorf("%q: code %d, %s", args, code, h.errOut)
		}
	}

	h.errOut.Reset()
	if code := Run(h.env, []string{"speed", "--download"}); code != 1 || !strings.Contains(h.errOut.String(), "'sbc update' adds it") {
		t.Errorf("without a speed test port: code %d, %s", code, h.errOut)
	}

	h.useDownloads(t, nil, nil)
	h.api.noSpeedTest = true
	h.errOut.Reset()
	if code := Run(h.env, []string{"speed", "--download"}); code != 1 || !strings.Contains(h.errOut.String(), "'sbc restart' loads the new one") {
		t.Errorf("sing-box without the speed test: code %d, %s", code, h.errOut)
	}
}

func TestSpeedFailsWhenNoProtocolLoadsThePage(t *testing.T) {
	h := newHarness(t)
	h.api.unreachable = true

	code := Run(h.env, []string{"speed"})

	if code != 1 || h.out.String() != "Time to load a small page through the protocol in use, the median of 3 tries:\n  trojan  failed\n" || !strings.Contains(h.errOut.String(), "trojan did not load the page; check this network, or test the other protocols with 'sbc speed --all'") {
		t.Errorf("the protocol in use: code %d, got\n%s%s", code, h.out, h.errOut)
	}
	h.out.Reset()
	h.errOut.Reset()
	code = Run(h.env, []string{"speed", "--all"})
	if code != 1 || !strings.Contains(h.out.String(), "  trojan  failed (current)\n") || !strings.Contains(h.errOut.String(), "no protocol loaded the page") {
		t.Errorf("every protocol: code %d, got\n%s%s", code, h.out, h.errOut)
	}
}

func TestSpeedTestsOnlyTheProtocolInUseUnlessAskedForAll(t *testing.T) {
	h := newHarness(t)
	h.api.delays = map[string][]int{"trojan": {150}, "naive": {90}}
	seen := h.useDownloads(t, map[string]time.Duration{"trojan": 4 * time.Second, "naive": time.Second}, nil)

	Run(h.env, []string{"speed"})
	Run(h.env, []string{"speed", "--download"})

	want := "Time to load a small page through the protocol in use, the median of 3 tries:\n" +
		"  trojan  150 ms\n" +
		"Downloading 20 MB through trojan, the protocol in use, which adds up to 40 MB to the server's monthly transfer:\n" +
		"  trojan  40.0 Mbit/s\n"
	if h.out.String() != want {
		t.Errorf("got\n%s%s", h.out, h.errOut)
	}
	if h.api.tries["naive"] != 0 || len(*seen) != 1 || !strings.HasPrefix((*seen)[0], "trojan ") {
		t.Errorf("tested naive %d times, and downloaded %q", h.api.tries["naive"], *seen)
	}
}

func TestIPAndSpeedSayWhenSingBoxIsNotRunning(t *testing.T) {
	h := newHarness(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	closed := listener.Addr().(*net.TCPAddr).Port
	listener.Close()
	h.writeLocal(t, func(local *singbox.Local) { local.APIPort = closed })

	for _, command := range []string{"ip", "speed"} {
		h.errOut.Reset()
		if code := Run(h.env, []string{command}); code != 1 || !strings.Contains(h.errOut.String(), "sing-box is not running") {
			t.Errorf("%s: code %d, %s", command, code, h.errOut)
		}
	}
}

func TestStatusShowsTheTrafficCountOfTheLastRefresh(t *testing.T) {
	h := newHarness(t)
	paths.WriteFile(h.layout.Usage(), []byte("upload=734003200; download=16428249907\n"), 0o600)
	at := time.Date(2026, 10, 3, 14, 5, 0, 0, time.Local)
	os.Chtimes(h.layout.Usage(), at, at)

	Run(h.env, []string{"status"})

	want := "service:  running\nproxy:    127.0.0.1:2080\nshells:   off\n" +
		"traffic:  700.00 MiB up, 15.30 GiB down this billing cycle, as of 2026-10-03 14:05\n" +
		"route:    china\nprotocol: trojan\n"
	if h.out.String() != want {
		t.Errorf("got\n%s", h.out)
	}
}

func TestFormatBytesUsesBinaryUnitsAsThePortalDoes(t *testing.T) {
	cases := map[int64]string{
		0:           "0 B",
		1023:        "1023 B",
		1536:        "1.50 KiB",
		734003200:   "700.00 MiB",
		16428249907: "15.30 GiB",
		5 << 40:     "5.00 TiB",
		5000 << 40:  "5000.00 TiB",
	}
	for count, want := range cases {
		if got := formatBytes(count); got != want {
			t.Errorf("%d: got %q, want %q", count, got, want)
		}
	}
}
