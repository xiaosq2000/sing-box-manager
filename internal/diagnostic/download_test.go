package diagnostic

import (
	"context"
	"crypto/x509"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
)

func TestDownloadTimingDoesNotWaitForSnapshots(t *testing.T) {
	site := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(60 * time.Millisecond)
		w.Header().Set("Content-Length", "65536")
		fmt.Fprint(w, strings.Repeat("x", 32768))
		w.(http.Flusher).Flush()
		time.Sleep(150 * time.Millisecond)
		fmt.Fprint(w, strings.Repeat("x", 32768))
	}))
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
	began := time.Now()
	sample := (Requester{Roots: roots, Observer: o}).Fetch(context.Background(), site.URL, 65536, true)
	if !sample.Passed(false, true) || sample.PayloadBytes != 65536 || sample.StopReason != "payload-complete" || sample.TransferMS == nil || *sample.TransferMS < 120 || *sample.TransferMS > 600 || time.Since(began) > 800*time.Millisecond {
		t.Fatalf("download reader or first-payload timing changed: %+v", sample)
	}
}

func TestDownloadRetainsLastBytesOnPrematureEOF(t *testing.T) {
	site := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "1000")
		fmt.Fprint(w, "last payload")
	}))
	defer site.Close()
	roots := x509.NewCertPool()
	roots.AddCert(site.Certificate())
	sample := (Requester{Roots: roots}).Fetch(context.Background(), site.URL, 1000, true)
	if sample.Passed(false, true) || sample.PayloadBytes != int64(len("last payload")) || sample.TransferMS == nil || sample.StopReason != "failed" {
		t.Fatalf("%+v", sample)
	}
}

func TestDownloadStopsAtTransferWindow(t *testing.T) {
	if testing.Short() {
		t.Skip("15-second transfer window")
	}
	site := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "99000000")
		ticker := time.NewTicker(20 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-r.Context().Done():
				return
			case <-ticker.C:
				if _, err := fmt.Fprint(w, strings.Repeat("x", 1024)); err != nil {
					return
				}
				w.(http.Flusher).Flush()
			}
		}
	}))
	defer site.Close()
	roots := x509.NewCertPool()
	roots.AddCert(site.Certificate())
	sample := (Requester{Roots: roots}).Fetch(context.Background(), site.URL, 99000000, true)
	if !sample.Passed(false, true) || sample.StopReason != "time-window-ended" || sample.PayloadBytes <= 0 || sample.PayloadBytes >= 99000000 || sample.TransferMS == nil || *sample.TransferMS < 14500 || *sample.TransferMS > 17000 {
		t.Fatalf("%+v", sample)
	}
}
