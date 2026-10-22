// Package probe validates IP observations and supplies managed test endpoints.
package probe

import (
	"errors"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Sites contains no account credentials. Tests replace endpoints with fixtures.
type Sites struct {
	IPify4, IPify6     string
	IdentMe4, IdentMe6 string
	IPDetails          string
	Download           string
}

var DefaultSites = Sites{
	IPify4: "https://api.ipify.org/", IPify6: "https://api6.ipify.org/",
	IdentMe4: "https://4.ident.me/", IdentMe6: "https://6.ident.me/",
	IPDetails: "https://ipwho.is/", Download: "https://speed.cloudflare.com/__down",
}

func Client(port int, username, password string, timeout time.Duration) *http.Client {
	proxy := &url.URL{Scheme: "http", Host: net.JoinHostPort("127.0.0.1", strconv.Itoa(port))}
	if username != "" {
		proxy.User = url.UserPassword(username, password)
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyURL(proxy)
	return &http.Client{Transport: transport, Timeout: timeout}
}

func IPClient(port int, username, password string, timeout time.Duration) *http.Client {
	client := Client(port, username, password, timeout)
	client.Transport.(*http.Transport).DisableKeepAlives = true
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return client
}

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
