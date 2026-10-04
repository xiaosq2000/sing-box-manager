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

// proxyPort serves handler as the local proxy, which also plays the sites
// behind it, and returns its port.
func proxyPort(t *testing.T, handler http.HandlerFunc) int {
	t.Helper()
	proxy := httptest.NewServer(handler)
	t.Cleanup(proxy.Close)
	address, _ := url.Parse(proxy.URL)
	port, _ := strconv.Atoi(address.Port())
	return port
}

// answering returns a client whose proxy answers every request with page.
func answering(t *testing.T, page string) *http.Client {
	t.Helper()
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, page) })
	return Client(port, "", "", 5*time.Second)
}

func TestRequestsGoThroughTheProxyWithItsPassword(t *testing.T) {
	var seen []string
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, strings.Join([]string{r.Header.Get("Proxy-Authorization"), r.URL.String(), r.Header.Get("User-Agent")}, " "))
		io.WriteString(w, "IP\t: 198.51.100.20\n地址\t: 中国  上海  上海\n运营商\t: 电信\n\nURL\t: http://www.cip.cc/198.51.100.20\n")
	})

	place, err := CIP(Client(port, "sbc", "s3cret", 5*time.Second), "http://cip.test/")

	if err != nil || place.String() != "中国 上海 上海 电信, 198.51.100.20" {
		t.Fatalf("got %q, %v", place, err)
	}
	password := "Basic " + base64.StdEncoding.EncodeToString([]byte("sbc:s3cret"))
	if len(seen) != 1 || seen[0] != password+" http://cip.test/ curl/8" {
		t.Errorf("the proxy saw %q", seen)
	}
}

func TestGeminiRegionReadsTheCodeGoogleWritesInThePage(t *testing.T) {
	region, err := GeminiRegion(answering(t, `[["AA2Yr",2,1,200,"USA",null,null,"269"]]`), "http://gemini.test/")
	if err != nil || region != "USA" {
		t.Errorf("got %q, %v", region, err)
	}
	_, err = GeminiRegion(answering(t, "<html>a page without it</html>"), "http://gemini.test/")
	if err == nil || err.Error() != "the page names no region" {
		t.Errorf("got %v", err)
	}
	if GeminiServes("HKG") || GeminiServes("MAC") || GeminiServes("CHN") || !GeminiServes("USA") {
		t.Error("GeminiServes is wrong about a region")
	}
}

func TestIPInfoReadsTheAddressCityAndCountry(t *testing.T) {
	place, err := IPInfo(answering(t, `{"ip": "203.0.113.7", "city": "Los Angeles", "region": "California", "country": "US"}`), "http://ipinfo.test/json")
	if err != nil || place.String() != "Los Angeles, US, 203.0.113.7" {
		t.Errorf("got %q, %v", place, err)
	}
	place, err = IPInfo(answering(t, `{"ip": "203.0.113.7"}`), "http://ipinfo.test/json")
	if err != nil || place.String() != "203.0.113.7" {
		t.Errorf("without a city: got %q, %v", place, err)
	}
}

func TestASiteThatGivesNoAddressOrNoAnswerSaysSo(t *testing.T) {
	if _, err := CIP(answering(t, "<!DOCTYPE html><html><head><meta charset=utf-8></head></html>"), "http://cip.test/"); err == nil || err.Error() != "the site gave no address" {
		t.Errorf("a web page: got %v", err)
	}
	if _, err := IPInfo(answering(t, `{"error": "rate limited"}`), "http://ipinfo.test/json"); err == nil || err.Error() != "the site gave no address" {
		t.Errorf("JSON without an address: got %v", err)
	}

	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "slow down", http.StatusTooManyRequests)
	})
	if _, err := IPInfo(Client(port, "", "", 5*time.Second), "http://ipinfo.test/json"); err == nil || err.Error() != "the site answered 429 Too Many Requests" {
		t.Errorf("an error status: got %v", err)
	}

	port = proxyPort(t, func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() })
	if _, err := GeminiRegion(Client(port, "", "", 50*time.Millisecond), "http://gemini.test/"); err == nil || err.Error() != "no answer within 50ms" {
		t.Errorf("a site that never answers: got %v", err)
	}
}

func TestDownloadCountsEveryByteAndAsksForThemUncompressed(t *testing.T) {
	var encoding string
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
		encoding = r.Header.Get("Accept-Encoding")
		chunk := make([]byte, 1<<20)
		for range 3 {
			w.Write(chunk)
		}
	})

	received, took, err := Download(Client(port, "", "", 5*time.Second), "http://speed.test/__down?bytes=3145728", 5*time.Second)

	if err != nil || received != 3<<20 || took <= 0 {
		t.Errorf("got %d bytes in %s, %v", received, took, err)
	}
	if encoding != "identity" {
		t.Errorf("asked for %q", encoding)
	}
}

// trickle sends a small chunk every 10 ms until the client goes, and after
// stall chunks stops sending without closing, when stall is above zero.
func trickle(stall int) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		for sent := 0; stall == 0 || sent < stall; sent++ {
			if _, err := w.Write(make([]byte, 1000)); err != nil {
				return
			}
			w.(http.Flusher).Flush()
			select {
			case <-r.Context().Done():
				return
			case <-time.After(10 * time.Millisecond):
			}
		}
		<-r.Context().Done()
	}
}

func TestDownloadStopsAtTheLimitAndFailsAStreamThatStalls(t *testing.T) {
	port := proxyPort(t, trickle(0))
	received, took, err := Download(Client(port, "", "", 5*time.Second), "http://speed.test/", 200*time.Millisecond)
	if err != nil || received == 0 || took < 200*time.Millisecond || took > 2*time.Second {
		t.Errorf("an endless stream: %d bytes in %s, %v", received, took, err)
	}

	port = proxyPort(t, trickle(5))
	began := time.Now()
	_, _, err = Download(Client(port, "", "", 30*time.Second), "http://speed.test/", 600*time.Millisecond)
	if err == nil || !strings.HasPrefix(err.Error(), "the download stalled after ") || time.Since(began) > 5*time.Second {
		t.Errorf("a stream that stalls: %v after %s", err, time.Since(began))
	}
}

func TestDownloadFailsWithoutBytes(t *testing.T) {
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {})
	if _, _, err := Download(Client(port, "", "", 5*time.Second), "http://speed.test/", time.Second); err == nil || err.Error() != "the site sent nothing" {
		t.Errorf("an empty answer: %v", err)
	}
	port = proxyPort(t, func(w http.ResponseWriter, r *http.Request) { http.Error(w, "no", http.StatusForbidden) })
	if _, _, err := Download(Client(port, "", "", 5*time.Second), "http://speed.test/", time.Second); err == nil || err.Error() != "the site answered 403 Forbidden" {
		t.Errorf("a refusal: %v", err)
	}
}
