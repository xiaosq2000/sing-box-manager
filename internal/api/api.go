// Package api talks to sing-box's Clash API on loopback, which switches the
// route mode and the protocol selector without a restart.
package api

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// ProxySelector is the selector that holds the protocols.
const ProxySelector = "proxy"

// TestURL is the page Delay fetches through the proxy. sing-box swaps any
// http:// URL for its own default, so this one is https.
const TestURL = "https://www.gstatic.com/generate_204"

// Client calls one sing-box's API.
type Client struct {
	base   string
	secret string
	http   *http.Client
}

// New returns a client for the API on 127.0.0.1:port.
func New(port int, secret string) *Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &Client{
		base:   "http://" + net.JoinHostPort("127.0.0.1", strconv.Itoa(port)),
		secret: secret,
		http:   &http.Client{Transport: transport, Timeout: 5 * time.Second},
	}
}

// ErrNotRunning means nothing answers on the API port.
var ErrNotRunning = i18n.New("sing-box is not running")

func (c *Client) call(method, path string, body, out any) error {
	return c.callWith(c.http, method, path, body, out)
}

func (c *Client) callWith(client *http.Client, method, path string, body, out any) error {
	var payload io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		payload = bytes.NewReader(data)
	}
	request, err := http.NewRequest(method, c.base+path, payload)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+c.secret)
	response, err := client.Do(request)
	if err != nil {
		return ErrNotRunning
	}
	defer response.Body.Close()
	data, _ := io.ReadAll(response.Body)
	if response.StatusCode >= 300 {
		return i18n.Errorf("sing-box answered %s: %s", response.Status, strings.TrimSpace(string(data)))
	}
	if out != nil {
		return json.Unmarshal(data, out)
	}
	return nil
}

// Modes returns the current route mode and every mode the config offers.
func (c *Client) Modes() (string, []string, error) {
	var config struct {
		Mode     string   `json:"mode"`
		ModeList []string `json:"mode-list"`
	}
	err := c.call(http.MethodGet, "/configs", nil, &config)
	return config.Mode, config.ModeList, err
}

// SetMode switches the route. sing-box ignores a mode it does not have, so
// this checks the list first.
func (c *Client) SetMode(mode string) error {
	_, modes, err := c.Modes()
	if err != nil {
		return err
	}
	if !slices.Contains(modes, mode) {
		return i18n.Errorf("unknown route %q; choose one of %s", mode, strings.Join(modes, ", "))
	}
	return c.call(http.MethodPatch, "/configs", map[string]string{"mode": mode}, nil)
}

// Selected returns the selector's current choice and its options.
func (c *Client) Selected(selector string) (string, []string, error) {
	var proxy struct {
		Now string   `json:"now"`
		All []string `json:"all"`
	}
	err := c.call(http.MethodGet, "/proxies/"+selector, nil, &proxy)
	return proxy.Now, proxy.All, err
}

// Select switches the selector to option.
func (c *Client) Select(selector, option string) error {
	_, options, err := c.Selected(selector)
	if err != nil {
		return err
	}
	if !slices.Contains(options, option) {
		return i18n.Errorf("unknown protocol %q; choose one of %s", option, strings.Join(options, ", "))
	}
	return c.call(http.MethodPut, "/proxies/"+selector, map[string]string{"name": option}, nil)
}

// Delay fetches TestURL through the outbound or selector called name and
// returns how long that took. An error means the request did not get through
// within timeout.
func (c *Client) Delay(name string, timeout time.Duration) (time.Duration, error) {
	// sing-box reads the timeout as a 16-bit count of milliseconds.
	milliseconds := min(timeout.Milliseconds(), 30000)
	query := url.Values{"url": {TestURL}, "timeout": {strconv.FormatInt(milliseconds, 10)}}
	client := *c.http
	client.Timeout = time.Duration(milliseconds)*time.Millisecond + 5*time.Second
	var result struct {
		Delay int64 `json:"delay"`
	}
	err := c.callWith(&client, http.MethodGet, "/proxies/"+url.PathEscape(name)+"/delay?"+query.Encode(), nil, &result)
	if errors.Is(err, ErrNotRunning) {
		return 0, err
	}
	if err != nil {
		return 0, i18n.Errorf("%s did not reach %s within %s", name, TestURL, time.Duration(milliseconds)*time.Millisecond)
	}
	return time.Duration(result.Delay) * time.Millisecond, nil
}

// WaitReady waits for the API to answer, which sing-box does once it started.
func (c *Client) WaitReady(timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		err := c.call(http.MethodGet, "/version", nil, nil)
		if err == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return err
		}
		time.Sleep(100 * time.Millisecond)
	}
}
