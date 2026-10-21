package probe

import (
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestExitIPValidatesTheAddressAndFamily(t *testing.T) {
	for _, entry := range []struct {
		body, want, failure string
		family              int
	}{
		{" 203.0.113.7\n", "203.0.113.7", "", 4},
		{"2001:0db8:0000::1\n", "2001:db8::1", "", 6},
		{"::ffff:203.0.113.7", "203.0.113.7", "", 4},
		{"", "", "the site gave no valid IP address", 4},
		{"<html>challenge</html>", "", "the site gave no valid IP address", 4},
		{`{"ip":"203.0.113.7"}`, "", "the site gave no valid IP address", 4},
		{"203.0.113.7\n198.51.100.1", "", "the site gave no valid IP address", 4},
		{"fe80::1%eth0", "", "the site gave no valid IP address", 6},
		{"fe80::1", "", "the site gave no valid IP address", 6},
		{"127.0.0.1", "", "the site gave no valid IP address", 4},
		{"192.168.1.1", "", "the site gave no valid IP address", 4},
		{"::", "", "the site gave no valid IP address", 6},
		{"ff02::1", "", "the site gave no valid IP address", 6},
		{"2001:db8::1", "", "the site returned IPv6, expected IPv4", 4},
		{"203.0.113.7", "", "the site returned IPv4, expected IPv6", 6},
		{"::ffff:203.0.113.7", "", "the site returned IPv4, expected IPv6", 6},
		{strings.Repeat("x", maxIPPage+1), "", "the IP response exceeds 64 KiB", 4},
	} {
		t.Run(fmt.Sprintf("family%d/%s", entry.family, entry.want+entry.failure), func(t *testing.T) {
			got, err := ExitIP(answering(t, entry.body), "http://echo.test/", entry.family)
			if got != entry.want || (err == nil) != (entry.failure == "") || (err != nil && err.Error() != entry.failure) {
				t.Fatalf("got %q, %v; want %q, %q", got, err, entry.want, entry.failure)
			}
		})
	}
}

func TestIPRequestsDoNotFollowRedirectsRetryOrKeepCookies(t *testing.T) {
	for _, entry := range []struct {
		status int
		want   string
	}{
		{302, "HTTP 302 (redirect not followed)"},
		{403, "the site answered 403 Forbidden"},
		{429, "rate limited (HTTP 429)"},
		{502, "the site answered 502 Bad Gateway"},
	} {
		t.Run(fmt.Sprint(entry.status), func(t *testing.T) {
			var requests int
			port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
				requests++
				if r.Header.Get("Cookie") != "" || r.Header.Get("Authorization") != "" {
					t.Error("IP request carried account credentials")
				}
				w.Header().Set("Location", "http://other.test/")
				w.Header().Set("Set-Cookie", "session=synthetic")
				w.Header().Set("Retry-After", "1")
				w.WriteHeader(entry.status)
			})
			client := IPClient(port, "", "", time.Second)
			_, err := ExitIP(client, "http://echo.test/", 4)
			if err == nil || err.Error() != entry.want || requests != 1 || client.Jar != nil {
				t.Fatalf("%d requests, error %v", requests, err)
			}
		})
	}
}

func TestIPRequestTimeout(t *testing.T) {
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() })
	_, err := ExitIP(IPClient(port, "", "", 30*time.Millisecond), "http://echo.test/", 4)
	if err == nil || err.Error() != "no answer within 30ms" {
		t.Fatal(err)
	}
}

func TestLookupIPDetails(t *testing.T) {
	body := `{"success":true,"ip":"203.0.113.7","country":"United States","region":"California","city":"Los Angeles","connection":{"asn":64500,"org":"Example Network","isp":"Example ISP"}}`
	details, err := LookupIP(answering(t, body), "http://details.test/", "203.0.113.7")
	if err != nil || details.Location() != "United States / California / Los Angeles" || details.ASN != 64500 || details.Organization != "Example Network" {
		t.Fatalf("got %+v, %v", details, err)
	}
	body = `{"success":true,"ip":"203.0.113.7","country_code":"US","connection":{"isp":"Example ISP"}}`
	details, err = LookupIP(answering(t, body), "http://details.test/", "203.0.113.7")
	if err != nil || details.Location() != "US" || details.ASN != 0 || details.Organization != "Example ISP" {
		t.Fatalf("fallback fields: %+v, %v", details, err)
	}
	body = `{"success":true,"ip":"203.0.113.7"}`
	details, err = LookupIP(answering(t, body), "http://details.test/", "203.0.113.7")
	if err != nil || details != (IPDetails{}) {
		t.Fatalf("unknown fields: %+v, %v", details, err)
	}
	if got := (IPDetails{Country: "China", Region: "Shanghai", City: "Shanghai"}).Location(); got != "China / Shanghai" {
		t.Fatal(got)
	}
}

func TestLookupIPRejectsInvalidAndMismatchedResponses(t *testing.T) {
	for _, entry := range []struct{ body, want string }{
		{"not JSON", "the IP database returned an invalid response"},
		{`{"ip":"203.0.113.7"}`, "the IP database returned an invalid response"},
		{`{"success":false,"message":"untrusted rejection text"}`, "the IP database rejected the lookup"},
		{`{"success":true}`, "the IP database returned an invalid response"},
		{`{"success":true,"ip":"203.0.113.7/24"}`, "the IP database returned an invalid response"},
		{`{"success":true,"ip":"198.51.100.9"}`, "the IP database returned a different address"},
		{`{"success":true,"ip":"203.0.113.7","connection":{"asn":-1}}`, "the IP database returned an invalid response"},
		{strings.Repeat("x", maxIPPage+1), "the IP response exceeds 64 KiB"},
	} {
		details, err := LookupIP(answering(t, entry.body), "http://details.test/", "203.0.113.7")
		if err == nil || err.Error() != entry.want || details != (IPDetails{}) {
			t.Fatalf("got %+v, %v; want %q", details, err, entry.want)
		}
	}
	if _, err := LookupIP(nil, "http://details.test/", "not-an-IP"); err == nil || err.Error() != "cannot look up an invalid IP address" {
		t.Fatal(err)
	}
}

func TestLookupIPUsesTheObservedIPv6AndSanitizesNames(t *testing.T) {
	var path string
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		io.WriteString(w, `{"success":true,"ip":"2001:0db8:0::1","country":" Test\nCountry\t","region":"\u001b[31mRegion","city":"\u202eCity","connection":{"org":"`+strings.Repeat("界", 90)+`"}}`)
	})
	details, err := LookupIP(IPClient(port, "", "", time.Second), "http://details.test/", "2001:db8::1")
	if err != nil || path != "/2001:db8::1" || details.Country != "Test Country" || strings.ContainsAny(details.Location(), "\n\t\x1b\u202e") || len([]rune(details.Organization)) != 81 {
		t.Fatalf("path %q, details %+v, error %v", path, details, err)
	}
}

func TestCheckIPsKeepsBothSourcesAndDeduplicatesDetails(t *testing.T) {
	for _, dual := range []bool{false, true} {
		t.Run(fmt.Sprint(dual), func(t *testing.T) {
			var lock sync.Mutex
			counts := map[string]int{}
			password := "Basic " + base64.StdEncoding.EncodeToString([]byte("sbc:synthetic"))
			port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Proxy-Authorization") != password || r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
					t.Error("wrong authentication headers")
				}
				lock.Lock()
				counts[r.URL.Host+r.URL.Path]++
				lock.Unlock()
				switch r.URL.Host {
				case "ipify4.test", "ident4.test":
					io.WriteString(w, "203.0.113.7")
				case "ipify6.test":
					io.WriteString(w, "2001:0db8:0::1")
				case "ident6.test":
					io.WriteString(w, "2001:db8::1")
				case "details.test":
					fmt.Fprintf(w, `{"success":true,"ip":%q,"country":"Test Country"}`, strings.TrimPrefix(r.URL.Path, "/"))
				default:
					t.Errorf("unexpected host %s", r.URL.Host)
				}
			})
			sites := Sites{
				IPify4: "http://ipify4.test/", IPify6: "http://ipify6.test/",
				IdentMe4: "http://ident4.test/", IdentMe6: "http://ident6.test/", IPDetails: "http://details.test/",
			}
			results := CheckIPs(IPClient(port, "sbc", "synthetic", time.Second), sites, dual)
			want := 2
			if dual {
				want = 4
			}
			if len(results) != want || len(counts) != want+want/2 || counts["details.test/203.0.113.7"] != 1 {
				t.Fatalf("results %+v, requests %v", results, counts)
			}
			for index, result := range results {
				if result.Err != nil || result.DetailsErr != nil || result.Details.Country != "Test Country" || result.Family != 4+(index/2)*2 {
					t.Fatalf("result %d: %+v", index, result)
				}
			}
			if results[0].Source != "IPify" || results[1].Source != "ident.me" {
				t.Fatal(results)
			}
		})
	}
}

func TestCheckIPsRunsEachPhaseInParallelAndKeepsDifferentIPs(t *testing.T) {
	var probes, lookups atomic.Int32
	probeReady, lookupReady := make(chan struct{}), make(chan struct{})
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Host == "details.test" {
			if lookups.Add(1) == 2 {
				close(lookupReady)
			}
			select {
			case <-lookupReady:
			case <-r.Context().Done():
				return
			}
			fmt.Fprintf(w, `{"success":true,"ip":%q}`, strings.TrimPrefix(r.URL.Path, "/"))
			return
		}
		if probes.Add(1) == 2 {
			close(probeReady)
		}
		select {
		case <-probeReady:
		case <-r.Context().Done():
			return
		}
		if r.URL.Host == "ipify.test" {
			io.WriteString(w, "203.0.113.7")
		} else {
			io.WriteString(w, "198.51.100.9")
		}
	})
	sites := Sites{IPify4: "http://ipify.test/", IdentMe4: "http://ident.test/", IPDetails: "http://details.test/"}
	results := CheckIPs(IPClient(port, "", "", time.Second), sites, false)
	for _, result := range results {
		if result.Err != nil || result.DetailsErr != nil {
			t.Fatalf("a phase did not run in parallel: %+v", result)
		}
	}
	if results[0].IP != "203.0.113.7" || results[1].IP != "198.51.100.9" || probes.Load() != 2 || lookups.Load() != 2 {
		t.Fatalf("results %+v, %d probes and %d lookups", results, probes.Load(), lookups.Load())
	}
}

func TestCheckIPsPreservesObservationsWhenDetailsFail(t *testing.T) {
	var lookups atomic.Int32
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Host == "details.test" {
			lookups.Add(1)
			w.WriteHeader(429)
			return
		}
		io.WriteString(w, "203.0.113.7")
	})
	sites := Sites{IPify4: "http://ipify.test/", IdentMe4: "http://ident.test/", IPDetails: "http://details.test/"}
	results := CheckIPs(IPClient(port, "", "", time.Second), sites, false)
	for _, result := range results {
		if result.IP != "203.0.113.7" || result.Err != nil || result.DetailsErr == nil || result.DetailsErr.Error() != "rate limited (HTTP 429)" {
			t.Fatalf("got %+v", result)
		}
	}
	if lookups.Load() != 1 {
		t.Fatal(lookups.Load())
	}
}

func TestCheckIPsNeverRetriesDirectlyOrLooksUpFailedProbes(t *testing.T) {
	var direct atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		direct.Add(1)
		io.WriteString(w, "203.0.113.7")
	}))
	defer target.Close()
	var requests atomic.Int32
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		w.WriteHeader(502)
	})
	sites := Sites{IPify4: target.URL, IdentMe4: target.URL, IPDetails: target.URL}
	results := CheckIPs(IPClient(port, "", "", time.Second), sites, false)
	if requests.Load() != 2 || direct.Load() != 0 {
		t.Fatalf("proxy requests %d, direct requests %d", requests.Load(), direct.Load())
	}
	for _, result := range results {
		if result.Err == nil || result.IP != "" || result.DetailsErr != nil {
			t.Fatalf("got %+v", result)
		}
	}
}
