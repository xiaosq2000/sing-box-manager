package probe

import (
	"encoding/base64"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"
)

func proxyPort(t *testing.T, handler http.HandlerFunc) int {
	t.Helper()
	proxy := httptest.NewServer(handler)
	t.Cleanup(proxy.Close)
	address, _ := url.Parse(proxy.URL)
	port, _ := strconv.Atoi(address.Port())
	return port
}

func answering(t *testing.T, page string) *http.Client {
	t.Helper()
	port := proxyPort(t, func(w http.ResponseWriter, _ *http.Request) { io.WriteString(w, page) })
	return Client(port, "", "", 5*time.Second)
}

func TestRequestsGoThroughTheProxyWithItsPassword(t *testing.T) {
	var seen []string
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, strings.Join([]string{r.Header.Get("Proxy-Authorization"), r.URL.String(), r.Header.Get("User-Agent")}, " "))
		io.WriteString(w, "198.51.100.20\n")
	})
	address, err := ExitIP(IPClient(port, "sbc", "s3cret", 5*time.Second), "http://echo.test/", 4)
	if err != nil || address != "198.51.100.20" {
		t.Fatal(address, err)
	}
	password := "Basic " + base64.StdEncoding.EncodeToString([]byte("sbc:s3cret"))
	if len(seen) != 1 || seen[0] != password+" http://echo.test/ Go-http-client/1.1" {
		t.Fatal(seen)
	}
}
