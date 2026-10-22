package diagnostic

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"sync"
	"sync/atomic"
	"time"
)

// Sample separates an HTTP observation from proof about its route.
type Sample struct {
	Transport       string   `json:"transport"`
	Issue           string   `json:"issue,omitempty"`
	HTTPStatus      *int     `json:"http_status"`
	HTTPOutcome     string   `json:"http_outcome"`
	FirstResponseMS *float64 `json:"first_response_ms"`
	PayloadBytes    int64    `json:"payload_bytes"`
	TransferMS      *float64 `json:"transfer_ms"`
	MbitPerSecond   *float64 `json:"mbit_per_second"`
	StopReason      string   `json:"stop_reason,omitempty"`
	Capture         Evidence `json:"capture"`
	Socket          int      `json:"-"`
	Body            []byte   `json:"-"`
}

// Requester uses a new transport for each request. Roots is for system-trust
// substitutes in isolated fixtures, not a public TLS-bypass setting.
type Requester struct {
	Proxy    *url.URL
	Observer *Observer
	Roots    *x509.CertPool
}

func number(value float64) *float64 { return &value }

func (r Requester) Fetch(parent context.Context, page string, bytes int64, download bool) Sample {
	result := Sample{Transport: "failed", HTTPOutcome: "not-tested", Capture: Evidence{Path: "unknown"}, Socket: -1}
	if download {
		result.StopReason = "failed"
	}
	target, err := url.Parse(page)
	if err != nil {
		result.Issue = "invalid-target"
		return result
	}
	port := target.Port()
	if port == "" {
		port = "443"
	}
	limit := 5 * time.Second
	if download {
		limit = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(parent, limit)
	defer cancel()
	var socket atomic.Int32
	socket.Store(-1)
	transport := &http.Transport{Proxy: http.ProxyURL(r.Proxy), TLSClientConfig: &tls.Config{RootCAs: r.Roots},
		DisableKeepAlives: true, DisableCompression: true, TLSHandshakeTimeout: 5 * time.Second, MaxResponseHeaderBytes: 64 << 10,
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			conn, err := (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, network, address)
			if err == nil && r.Observer != nil {
				var index int
				conn, index = r.Observer.Wrap(conn, target.Hostname(), port)
				socket.Store(int32(index))
			}
			return conn, err
		}}
	// nil is native access. Never inherit environment proxy variables.
	if r.Proxy == nil {
		transport.Proxy = nil
	}
	defer transport.CloseIdleConnections()
	web := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	var clock sync.Mutex
	start := time.Now()
	var first time.Time
	trace := &httptrace.ClientTrace{GotFirstResponseByte: func() {
		clock.Lock()
		if first.IsZero() {
			first = time.Now()
		}
		clock.Unlock()
	}}
	request, err := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), http.MethodGet, page, nil)
	if err != nil {
		result.Issue = "invalid-target"
		return result
	}
	request.Header.Set("Accept-Encoding", "identity")
	request.Header.Set("Cache-Control", "no-cache")
	response, err := web.Do(request)
	clock.Lock()
	if !first.IsZero() {
		result.FirstResponseMS = number(float64(first.Sub(start)) / float64(time.Millisecond))
	}
	clock.Unlock()
	result.Socket = int(socket.Load())
	if err != nil {
		result.Issue = classify(err)
		return result
	}
	defer response.Body.Close()
	result.HTTPStatus = &response.StatusCode
	result.Transport = "responded"
	switch {
	case response.StatusCode >= 200 && response.StatusCode < 300:
		result.HTTPOutcome = "accepted"
	case response.StatusCode >= 300 && response.StatusCode < 400:
		result.HTTPOutcome = "redirect-not-followed"
	case response.StatusCode == 401 || response.StatusCode == 403 || response.StatusCode == 429:
		result.HTTPOutcome = "restricted"
	case response.StatusCode >= 500:
		result.HTTPOutcome = "server-error"
	default:
		result.HTTPOutcome = "client-error"
	}
	if !download {
		result.Body, err = io.ReadAll(io.LimitReader(response.Body, bytes))
		if int64(len(result.Body)) > bytes {
			result.Body = result.Body[:bytes]
		}
		result.PayloadBytes = int64(len(result.Body))
		if err != nil {
			result.Transport, result.Issue = "failed", classify(err)
		}
		return result
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return result
	}
	if encoding := response.Header.Get("Content-Encoding"); encoding != "" && encoding != "identity" {
		result.Issue, result.StopReason = "invalid-download", "invalid-response"
		return result
	}
	if response.ContentLength >= 0 && response.ContentLength != bytes {
		result.Issue, result.StopReason = "invalid-download", "invalid-response"
		return result
	}
	var stalled atomic.Bool
	idle := time.AfterFunc(5*time.Second, func() { stalled.Store(true); cancel() })
	defer idle.Stop()
	var transfer *time.Timer
	var began time.Time
	var windowEnded atomic.Bool
	buffer := make([]byte, 32<<10)
	for result.PayloadBytes < bytes {
		readSize := min(int64(len(buffer)), bytes-result.PayloadBytes)
		if began.IsZero() {
			readSize = 1
		}
		count, readErr := response.Body.Read(buffer[:readSize])
		if count > 0 {
			if began.IsZero() {
				began = time.Now()
				transfer = time.AfterFunc(15*time.Second, func() { windowEnded.Store(true); cancel() })
				defer transfer.Stop()
			}
			idle.Reset(5 * time.Second)
			result.PayloadBytes += int64(count)
		}
		if readErr != nil {
			if stalled.Load() {
				result.Issue = "stalled"
			} else if !errors.Is(readErr, io.EOF) && !windowEnded.Load() {
				result.Transport, result.Issue = "failed", classify(readErr)
			} else if errors.Is(readErr, io.EOF) && result.PayloadBytes < bytes {
				result.Issue = "short-payload"
			}
			break
		}
	}
	switch {
	case result.PayloadBytes >= bytes:
		result.StopReason = "payload-complete"
	case windowEnded.Load():
		result.StopReason = "time-window-ended"
	case stalled.Load():
		result.StopReason = "stalled"
	}
	if result.PayloadBytes == 0 {
		if result.Issue == "" {
			result.Issue = "empty-payload"
		}
		return result
	}
	elapsed := max(time.Since(began), time.Microsecond)
	result.TransferMS = number(float64(elapsed) / float64(time.Millisecond))
	result.MbitPerSecond = number(float64(result.PayloadBytes) * 8 / elapsed.Seconds() / 1e6)
	return result
}

func classify(err error) string {
	var dns *net.DNSError
	var cert *tls.CertificateVerificationError
	var timed net.Error
	switch {
	case errors.Is(err, context.Canceled):
		return "canceled"
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	case errors.As(err, &dns):
		return "dns"
	case errors.As(err, &cert):
		return "tls"
	case errors.As(err, &timed) && timed.Timeout():
		return "timeout"
	default:
		return "connection"
	}
}

func (s Sample) Passed(tun bool, download bool) bool {
	return s.Transport == "responded" && s.Issue == "" && s.HTTPStatus != nil && *s.HTTPStatus >= 200 && *s.HTTPStatus < 300 && (!tun || s.Capture.Verified) && (!download || s.PayloadBytes > 0)
}
