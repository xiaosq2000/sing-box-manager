package diagnostic

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
)

func TestRequesterBoundsReadsAndRetainsHTTPOutcomes(t *testing.T) {
	var requests atomic.Int32
	site := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Header.Get("Cookie") != "" || r.Header.Get("Authorization") != "" {
			t.Error("unexpected credentials")
		}
		switch r.URL.Path {
		case "/redirect":
			w.Header().Set("Location", "/must-not-follow")
			w.WriteHeader(302)
		case "/denied":
			w.WriteHeader(403)
		case "/download":
			fmt.Fprint(w, strings.Repeat("x", 32768))
		case "/compressed":
			w.Header().Set("Content-Encoding", "gzip")
			fmt.Fprint(w, "invalid")
		default:
			fmt.Fprint(w, strings.Repeat("x", 32768))
		}
	}))
	defer site.Close()
	roots := x509.NewCertPool()
	roots.AddCert(site.Certificate())
	r := Requester{Roots: roots}
	for _, item := range []struct{ path, outcome string }{{"/redirect", "redirect-not-followed"}, {"/denied", "restricted"}, {"/large", "accepted"}} {
		before := requests.Load()
		result := r.Fetch(context.Background(), site.URL+item.path+"?secret=fixture", 8192, false)
		if result.HTTPOutcome != item.outcome || result.PayloadBytes > 8192 || result.FirstResponseMS == nil || requests.Load() != before+1 {
			t.Fatalf("%+v", result)
		}
		data, _ := json.Marshal(result)
		if strings.Contains(string(data), "secret") || strings.Contains(string(data), "fixture") {
			t.Fatal("raw request data escaped")
		}
	}
	good := r.Fetch(context.Background(), site.URL+"/download", 32768, true)
	if !good.Passed(false, true) || good.PayloadBytes != 32768 || good.TransferMS == nil {
		t.Fatalf("%+v", good)
	}
	for _, path := range []string{"/compressed", "/download"} {
		bad := r.Fetch(context.Background(), site.URL+path, 65536, true)
		if bad.Passed(false, true) || (bad.Issue != "invalid-download" && bad.Issue != "short-payload") {
			t.Fatalf("%+v", bad)
		}
	}
}

func TestSlowSnapshotDoesNotBlockHTTPMeasurement(t *testing.T) {
	site := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { fmt.Fprint(w, "small") }))
	defer site.Close()
	roots := x509.NewCertPool()
	roots.AddCert(site.Certificate())
	var calls atomic.Int32
	o, err := NewObserver(context.Background(), "tun/fixture", nil, func(ctx context.Context) ([]api.Connection, error) {
		if calls.Add(1) == 1 {
			return nil, nil
		}
		<-ctx.Done()
		return nil, ctx.Err()
	})
	if err != nil {
		t.Fatal(err)
	}
	defer o.Stop()
	start := time.Now()
	result := (Requester{Roots: roots, Observer: o}).Fetch(context.Background(), site.URL, 8192, false)
	if !result.Passed(false, false) || result.FirstResponseMS == nil || time.Since(start) > 200*time.Millisecond {
		t.Fatalf("API wait contaminated request: %+v", result)
	}
	if o.Evidence(result.Socket).Verified {
		t.Fatal("stalled snapshot granted proof")
	}
}

func TestRequesterCancellationAndDownloadStallAreFailures(t *testing.T) {
	site := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "1000")
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer site.Close()
	roots := x509.NewCertPool()
	roots.AddCert(site.Certificate())
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if sample := (Requester{Roots: roots}).Fetch(ctx, site.URL, 8192, false); sample.Passed(false, false) || sample.Issue != "canceled" {
		t.Fatalf("%+v", sample)
	}
	sample := (Requester{Roots: roots}).Fetch(context.Background(), site.URL, 1000, true)
	if sample.Passed(false, true) || sample.Issue != "stalled" {
		t.Fatalf("%+v", sample)
	}
}
