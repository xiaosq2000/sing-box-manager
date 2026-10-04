// Package probe asks sites on the internet where this machine's traffic comes
// from. Every request goes through the local proxy, so the answers follow the
// route: a site the route sends direct sees this machine, and a site it
// proxies sees the server.
package probe

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Sites are the pages 'sbc ip' asks. Tests point them at a local server.
type Sites struct {
	// Gemini is Gemini's start page. The AI rule set covers it, so every
	// route sends it through the proxy.
	Gemini string
	// Abroad answers with the address a foreign site sees, and China with the
	// address a Chinese site sees.
	Abroad string
	China  string
	// Download sends as many bytes as its bytes parameter asks for, which
	// 'sbc speed --download' times.
	Download string
}

// DefaultSites are the sites sbc asks. Cloudflare's speed test sends at most
// 99,999,999 bytes at a time.
var DefaultSites = Sites{
	Gemini:   "https://gemini.google.com/",
	Abroad:   "https://ipinfo.io/json",
	China:    "http://cip.cc/",
	Download: "https://speed.cloudflare.com/__down",
}

// maxPage bounds what sbc reads of a page. Gemini's start page is under 1 MiB.
const maxPage = 8 << 20

// Client returns an HTTP client that sends every request through the local
// proxy on port, with the username and password when the proxy asks for them.
func Client(port int, username, password string, timeout time.Duration) *http.Client {
	proxy := &url.URL{Scheme: "http", Host: net.JoinHostPort("127.0.0.1", strconv.Itoa(port))}
	if username != "" {
		proxy.User = url.UserPassword(username, password)
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyURL(proxy)
	return &http.Client{Transport: transport, Timeout: timeout}
}

func get(client *http.Client, page, userAgent string) ([]byte, error) {
	request, err := http.NewRequest(http.MethodGet, page, nil)
	if err != nil {
		return nil, err
	}
	if userAgent != "" {
		request.Header.Set("User-Agent", userAgent)
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, explain(err, client.Timeout)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, maxPage))
	if err != nil {
		return nil, explain(err, client.Timeout)
	}
	if response.StatusCode != http.StatusOK {
		return nil, i18n.Errorf("the site answered %s", response.Status)
	}
	return body, nil
}

// explain shortens Go's request errors, which repeat the URL the line they
// appear on already names.
func explain(err error, timeout time.Duration) error {
	var slow interface{ Timeout() bool }
	if errors.As(err, &slow) && slow.Timeout() {
		return i18n.Errorf("no answer within %s", timeout)
	}
	var failed *url.Error
	if errors.As(err, &failed) {
		return failed.Err
	}
	return err
}

// geminiRegion finds the region in Gemini's start page, where Google writes
// its three-letter code after ',2,1,200,'. The page is not an API, so the
// pattern may stop matching, and sbc then says the page names no region.
var geminiRegion = regexp.MustCompile(`,2,1,200,"([A-Z]{3})"`)

// GeminiRegion returns the code of the region Gemini places the visitor in,
// such as USA.
func GeminiRegion(client *http.Client, page string) (string, error) {
	body, err := get(client, page, "")
	if err != nil {
		return "", err
	}
	match := geminiRegion.FindSubmatch(body)
	if match == nil {
		return "", i18n.New("the page names no region")
	}
	return string(match[1]), nil
}

// geminiAbsent holds the regions near sbc's users that Gemini does not serve.
var geminiAbsent = map[string]bool{"CHN": true, "HKG": true, "MAC": true}

// GeminiServes reports whether Gemini serves region, as far as sbc knows.
func GeminiServes(region string) bool { return !geminiAbsent[region] }

// Place is where a site places the visitor.
type Place struct {
	IP       string
	Location string
}

// String writes the location, then the address, as the bash client's
// 'proxy check ip' did.
func (p Place) String() string { return join(", ", p.Location, p.IP) }

// IPInfo asks ipinfo.io, which answers with JSON.
func IPInfo(client *http.Client, page string) (Place, error) {
	body, err := get(client, page, "")
	if err != nil {
		return Place{}, err
	}
	var info struct {
		IP      string `json:"ip"`
		City    string `json:"city"`
		Country string `json:"country"`
	}
	if err := json.Unmarshal(body, &info); err != nil || info.IP == "" {
		return Place{}, i18n.New("the site gave no address")
	}
	return Place{IP: info.IP, Location: join(", ", info.City, info.Country)}, nil
}

// CIP asks cip.cc, which answers in lines such as "IP	: 192.0.2.1" when the
// client says it is curl, and with a web page otherwise.
func CIP(client *http.Client, page string) (Place, error) {
	body, err := get(client, page, "curl/8")
	if err != nil {
		return Place{}, err
	}
	fields := map[string]string{}
	for _, line := range strings.Split(string(body), "\n") {
		if key, value, ok := strings.Cut(line, ":"); ok {
			fields[strings.TrimSpace(key)] = strings.Join(strings.Fields(value), " ")
		}
	}
	if fields["IP"] == "" {
		return Place{}, i18n.New("the site gave no address")
	}
	return Place{IP: fields["IP"], Location: join(" ", fields["地址"], fields["运营商"])}, nil
}

// Download fetches page and returns how many bytes arrived and how long they
// took from the first byte, which leaves out connecting and asking. It stops
// reading after limit, so a slow protocol cannot hold the test up, and fails
// when nothing arrives for a third of limit, since that stream has stalled.
func Download(client *http.Client, page string, limit time.Duration) (int64, time.Duration, error) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, page, nil)
	if err != nil {
		return 0, 0, err
	}
	// Compressed bytes would cross the network faster than they count.
	request.Header.Set("Accept-Encoding", "identity")
	response, err := client.Do(request)
	if err != nil {
		return 0, 0, explain(err, client.Timeout)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return 0, 0, i18n.Errorf("the site answered %s", response.Status)
	}
	idle := limit / 3
	var stalled atomic.Bool
	watchdog := time.AfterFunc(idle, func() { stalled.Store(true); cancel() })
	defer watchdog.Stop()
	buffer := make([]byte, 64<<10)
	var received int64
	var start time.Time
	for {
		count, err := response.Body.Read(buffer)
		if count > 0 {
			watchdog.Reset(idle)
			if received == 0 {
				start = time.Now()
			}
		}
		received += int64(count)
		if errors.Is(err, io.EOF) || (received > 0 && time.Since(start) >= limit) {
			break
		}
		if err != nil {
			if stalled.Load() {
				return 0, 0, i18n.Errorf("the download stalled after %.2f MB", float64(received)/1e6)
			}
			// The client's own timeout can end a download that started late.
			var slow interface{ Timeout() bool }
			if received > 0 && errors.As(err, &slow) && slow.Timeout() {
				break
			}
			return 0, 0, explain(err, client.Timeout)
		}
	}
	if received == 0 {
		return 0, 0, i18n.New("the site sent nothing")
	}
	return received, max(time.Since(start), time.Millisecond), nil
}

// join joins the parts that are not empty.
func join(separator string, parts ...string) string {
	var kept []string
	for _, part := range parts {
		if part != "" {
			kept = append(kept, part)
		}
	}
	return strings.Join(kept, separator)
}
