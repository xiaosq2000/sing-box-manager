// Package exporter uploads one bounded page of local counter snapshots over a
// dedicated authenticated HTTPS client and asks the store to acknowledge the
// exact receipts returned by the server.
package exporter

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/traffic"
)

const (
	trafficPath           = "/node/v1/traffic"
	clientTimeout         = 30 * time.Second
	dialTimeout           = 10 * time.Second
	tlsHandshakeTimeout   = 10 * time.Second
	responseHeaderTimeout = 30 * time.Second
	expectContinueTimeout = time.Second
)

var (
	// ErrInvalidOptions reports a store, origin, token or page-size problem.
	ErrInvalidOptions = errors.New("exporter: invalid options")
	// ErrSelectPending reports that the store could not return a bounded page.
	ErrSelectPending = errors.New("exporter: pending counter selection failed")
	// ErrEncodeRequest reports an invalid selected page or request encoding.
	ErrEncodeRequest = errors.New("exporter: counter page encoding failed")
	// ErrRequest reports a failed HTTPS request, including TLS, dial and
	// request construction failures.
	ErrRequest = errors.New("exporter: HTTPS request failed")
	// ErrRequestStatus reports a non-2xx HTTPS response.
	ErrRequestStatus = errors.New("exporter: unexpected HTTPS status")
	// ErrResponseRead reports a response body read failure.
	ErrResponseRead = errors.New("exporter: response body read failed")
	// ErrResponseTooLarge reports a response body beyond the wire body cap.
	ErrResponseTooLarge = errors.New("exporter: response body exceeds wire bound")
	// ErrResponseInvalid reports a response the receipt codec rejects.
	ErrResponseInvalid = errors.New("exporter: invalid receipt response")
	// ErrAcknowledge reports a failed or invalid store acknowledgement.
	ErrAcknowledge = errors.New("exporter: counter acknowledgement failed")
)

// CounterStore is the frozen local state interface consumed by the exporter.
// The concrete accounting store is composed outside this package.
type CounterStore interface {
	PendingCounters(context.Context, int) ([]traffic.CounterSnapshot, error)
	AcknowledgeCounters(context.Context, traffic.Request, traffic.ReceiptResponse) (int, error)
}

// Options configures one exporter client. Zero PageSize means MaxPageItems.
type Options struct {
	Origin   string
	Token    string
	RootCAs  *x509.CertPool
	PageSize int
}

// Result reports one ExportOnce selection and the store commit count.
// Selected counts the selected request rows and does not prove a server
// commit. Acknowledged is nonzero only after the store reports a successful
// acknowledgement transaction.
type Result struct {
	Selected     int
	Acknowledged int
}

// Client is one dedicated HTTPS counter exporter.
type Client struct {
	store      CounterStore
	endpoint   string
	token      string
	pageSize   int
	httpClient *http.Client
	transport  *http.Transport
}

// New validates the options and builds a dedicated standard-library HTTPS
// client. It never accepts a caller-supplied transport or client.
func New(store CounterStore, options Options) (*Client, error) {
	if store == nil {
		return nil, fmt.Errorf("%w: store", ErrInvalidOptions)
	}
	endpoint, err := trafficEndpoint(options.Origin)
	if err != nil {
		return nil, err
	}
	if !validToken(options.Token) {
		return nil, fmt.Errorf("%w: token", ErrInvalidOptions)
	}
	pageSize := options.PageSize
	if pageSize == 0 {
		pageSize = traffic.MaxPageItems
	}
	if pageSize < 1 || pageSize > traffic.MaxPageItems {
		return nil, fmt.Errorf("%w: page size", ErrInvalidOptions)
	}
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	if options.RootCAs != nil {
		tlsConfig.RootCAs = options.RootCAs.Clone()
	}
	dialer := &net.Dialer{Timeout: dialTimeout, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		Proxy:                 nil,
		DialContext:           dialer.DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          4,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   tlsHandshakeTimeout,
		ResponseHeaderTimeout: responseHeaderTimeout,
		ExpectContinueTimeout: expectContinueTimeout,
		DisableCompression:    true,
		TLSClientConfig:       tlsConfig,
	}
	httpClient := &http.Client{
		Transport: transport,
		Timeout:   clientTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	return &Client{
		store:      store,
		endpoint:   endpoint,
		token:      options.Token,
		pageSize:   pageSize,
		httpClient: httpClient,
		transport:  transport,
	}, nil
}

// ExportOnce selects one bounded page, makes at most one POST and then asks the
// store to acknowledge the exact parsed receipts. An empty page performs no
// HTTP or acknowledgement. After a valid bounded selection, Selected still
// reports the selected row count on error while Acknowledged remains zero.
func (c *Client) ExportOnce(ctx context.Context) (Result, error) {
	if err := ctx.Err(); err != nil {
		return Result{}, err
	}
	snapshots, err := c.store.PendingCounters(ctx, c.pageSize)
	if err != nil {
		return Result{}, contextFailure(ctx, err, ErrSelectPending)
	}
	if len(snapshots) == 0 {
		return Result{}, nil
	}
	if len(snapshots) > c.pageSize {
		return Result{}, fmt.Errorf("%w: page size", ErrSelectPending)
	}
	result := Result{Selected: len(snapshots)}
	page := make([]traffic.Snapshot, len(snapshots))
	for index := range snapshots {
		page[index] = snapshots[index]
	}
	request, err := traffic.MakeRequest(page...)
	if err != nil {
		return result, boundedCodecFailure(err, ErrEncodeRequest)
	}
	raw, err := traffic.EncodeRequest(request)
	if err != nil {
		return result, boundedCodecFailure(err, ErrEncodeRequest)
	}
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(raw))
	if err != nil {
		return result, contextFailure(ctx, err, ErrRequest)
	}
	httpRequest.Header.Set("Content-Type", "application/json")
	httpRequest.Header.Set("Authorization", "Bearer "+c.token)
	response, err := c.httpClient.Do(httpRequest)
	if err != nil {
		if response != nil && response.Body != nil {
			_ = response.Body.Close()
		}
		return result, contextFailure(ctx, err, ErrRequest)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode > 299 {
		return result, fmt.Errorf("%w: status %d", ErrRequestStatus, response.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, traffic.MaxRawBodyBytes+1))
	if err != nil {
		return result, contextFailure(ctx, err, ErrResponseRead)
	}
	if len(body) > traffic.MaxRawBodyBytes {
		return result, ErrResponseTooLarge
	}
	receipts, err := traffic.ParseReceiptResponse(body, request)
	if err != nil {
		return result, boundedCodecFailure(err, ErrResponseInvalid)
	}
	acknowledged, err := c.store.AcknowledgeCounters(ctx, request, receipts)
	if err != nil {
		return result, contextFailure(ctx, err, ErrAcknowledge)
	}
	if acknowledged < 0 || acknowledged > len(snapshots) {
		return result, ErrAcknowledge
	}
	result.Acknowledged = acknowledged
	return result, nil
}

// CloseIdleConnections closes idle connections held by this client's dedicated
// transport.
func (c *Client) CloseIdleConnections() {
	if c == nil || c.transport == nil {
		return
	}
	c.transport.CloseIdleConnections()
}

func trafficEndpoint(origin string) (string, error) {
	// net/url drops an empty fragment, so reject the raw delimiter before
	// parsing to keep the origin contract strict.
	if strings.ContainsRune(origin, '#') {
		return "", fmt.Errorf("%w: origin", ErrInvalidOptions)
	}
	parsed, err := url.Parse(origin)
	if err != nil || !validOrigin(parsed) {
		return "", fmt.Errorf("%w: origin", ErrInvalidOptions)
	}
	return "https://" + parsed.Host + trafficPath, nil
}

func validOrigin(parsed *url.URL) bool {
	return parsed.Scheme == "https" &&
		parsed.Host != "" &&
		parsed.Hostname() != "" &&
		parsed.User == nil &&
		parsed.Opaque == "" &&
		!parsed.ForceQuery &&
		parsed.RawQuery == "" &&
		parsed.Fragment == "" &&
		(parsed.Path == "" || parsed.Path == "/") &&
		(parsed.RawPath == "" || parsed.RawPath == "/")
}

// validToken enforces only opaque nonempty bearer safety: no header whitespace
// or control bytes. It deliberately imposes no length or encoding format.
func validToken(token string) bool {
	if token == "" {
		return false
	}
	for index := 0; index < len(token); index++ {
		if token[index] <= ' ' || token[index] == 0x7f {
			return false
		}
	}
	return true
}

// contextFailure preserves a caller context result without exposing arbitrary
// store or transport diagnostics.
func contextFailure(ctx context.Context, err, fallback error) error {
	if ctxErr := ctx.Err(); ctxErr != nil {
		return ctxErr
	}
	if errors.Is(err, context.Canceled) {
		return context.Canceled
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return context.DeadlineExceeded
	}
	return fallback
}

// boundedCodecFailure keeps only the fixed wire error code.
func boundedCodecFailure(err, fallback error) error {
	if code, ok := traffic.ErrorCodeOf(err); ok {
		return fmt.Errorf("%w: %s", fallback, code)
	}
	return fallback
}
