// Package probe asks sites on the internet where this machine's traffic comes
// from. Every request goes through the local proxy, so the answers follow the
// route: a site the route sends direct sees this machine, and a site it
// proxies sees the server.
package probe

import (
	"bytes"
	"context"
	"errors"
	"fmt"
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
	// AI checks use public endpoints without account credentials.
	Gemini  string
	ChatGPT string
	Claude  string
	// Each IP endpoint serves only the named address family. The local
	// HTTP proxy, not sbc's connection to loopback, selects the exit family.
	IPify4   string
	IPify6   string
	IdentMe4 string
	IdentMe6 string
	// IPDetails is the database's base URL. Lookups append an observed IP.
	IPDetails string
	// Download sends as many bytes as its bytes parameter asks for, which
	// 'sbc speed --download' times.
	Download string
}

// DefaultSites are the sites sbc asks. Cloudflare's speed test sends at most
// 99,999,999 bytes at a time.
var DefaultSites = Sites{
	Gemini:    "https://gemini.google.com/",
	ChatGPT:   "https://chatgpt.com/cdn-cgi/trace",
	Claude:    "https://claude.ai/",
	IPify4:    "https://api.ipify.org/",
	IPify6:    "https://api6.ipify.org/",
	IdentMe4:  "https://4.ident.me/",
	IdentMe6:  "https://6.ident.me/",
	IPDetails: "https://ipwho.is/",
	Download:  "https://speed.cloudflare.com/__down",
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

// IPClient uses a fresh connection for each request. It has no cookie jar
// and does not follow redirects, which can change the destination's route.
func IPClient(port int, username, password string, timeout time.Duration) *http.Client {
	client := Client(port, username, password, timeout)
	client.Transport.(*http.Transport).DisableKeepAlives = true
	client.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	return client
}

// AIClient applies the same connection and redirect limits to AI checks.
func AIClient(port int, username, password string, timeout time.Duration) *http.Client {
	return IPClient(port, username, password, timeout)
}

// AI reports public-page reachability, not service availability or account
// eligibility. Only Gemini's page and ChatGPT's edge trace can supply a region.
func AI(client *http.Client, provider, page string) (string, error) {
	request, err := http.NewRequest(http.MethodGet, page, nil)
	if err != nil {
		return "", err
	}
	response, err := client.Do(request)
	if err != nil {
		return "", explain(err, client.Timeout)
	}
	defer response.Body.Close()
	if response.StatusCode >= 300 && response.StatusCode < 400 {
		return fmt.Sprintf(i18n.T("HTTP %d (redirect not followed), region unknown"), response.StatusCode), nil
	}
	if response.StatusCode == http.StatusForbidden || response.StatusCode == http.StatusUnauthorized {
		return fmt.Sprintf(i18n.T("HTTP %d (denied or challenged), region unknown"), response.StatusCode), nil
	}
	if response.StatusCode != http.StatusOK {
		return "", i18n.Errorf("the site answered %s", response.Status)
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, maxPage))
	if err != nil {
		return "", explain(err, client.Timeout)
	}
	if bytes.Contains(body, []byte("cf-chl-")) || bytes.Contains(body, []byte("Just a moment...")) {
		return i18n.T("challenge received, region unknown"), nil
	}
	switch provider {
	case "gemini":
		if match := geminiRegion.FindSubmatch(body); match != nil {
			return fmt.Sprintf(i18n.T("page reachable, reported region %s"), match[1]), nil
		}
	case "chatgpt":
		fields := map[string]string{}
		for _, line := range strings.Split(string(body), "\n") {
			if key, value, ok := strings.Cut(line, "="); ok {
				fields[key] = strings.TrimSpace(value)
			}
		}
		if net.ParseIP(fields["ip"]) != nil && regexp.MustCompile(`^[A-Z]{2}$`).MatchString(fields["loc"]) {
			return fmt.Sprintf(i18n.T("edge country %s, IP %s (not service eligibility)"), fields["loc"], fields["ip"]), nil
		}
	}
	return i18n.T("page reachable, region unknown (not service eligibility)"), nil
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
