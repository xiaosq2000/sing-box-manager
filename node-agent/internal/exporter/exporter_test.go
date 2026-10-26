package exporter

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/traffic"
)

const (
	testSourceID = "11111111-1111-1111-1111-111111111111"
	credentialA  = "22222222-2222-2222-2222-222222222222"
	credentialB  = "33333333-3333-3333-3333-333333333333"
	credentialC  = "44444444-4444-4444-4444-444444444444"
	testToken    = "opaque:token/with=punctuation.Case"
)

type fakeStore struct {
	snapshots    []traffic.CounterSnapshot
	pendingFunc  func(context.Context, int) ([]traffic.CounterSnapshot, error)
	pendingErr   error
	ackFunc      func(context.Context, traffic.Request, traffic.ReceiptResponse) (int, error)
	ackResult    int
	pendingCalls int
	lastLimit    int
	ackCalls     int
	ackRequest   traffic.Request
	ackResponse  traffic.ReceiptResponse
}

func (s *fakeStore) PendingCounters(ctx context.Context, limit int) ([]traffic.CounterSnapshot, error) {
	s.pendingCalls++
	s.lastLimit = limit
	if s.pendingFunc != nil {
		return s.pendingFunc(ctx, limit)
	}
	if s.pendingErr != nil {
		return nil, s.pendingErr
	}
	return append([]traffic.CounterSnapshot(nil), s.snapshots...), nil
}

func (s *fakeStore) AcknowledgeCounters(ctx context.Context, request traffic.Request, response traffic.ReceiptResponse) (int, error) {
	s.ackCalls++
	s.ackRequest = request
	s.ackResponse = response
	if s.ackFunc != nil {
		return s.ackFunc(ctx, request, response)
	}
	return s.ackResult, nil
}

type recordedRequest struct {
	method         string
	path           string
	rawQuery       string
	authorization  string
	contentType    string
	acceptEncoding string
	body           []byte
}

type recorder struct {
	mu       sync.Mutex
	requests []recordedRequest
}

func (r *recorder) add(request *http.Request, body []byte) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.requests = append(r.requests, recordedRequest{
		method:         request.Method,
		path:           request.URL.Path,
		rawQuery:       request.URL.RawQuery,
		authorization:  request.Header.Get("Authorization"),
		contentType:    request.Header.Get("Content-Type"),
		acceptEncoding: request.Header.Get("Accept-Encoding"),
		body:           append([]byte(nil), body...),
	})
}

func (r *recorder) snapshot() []recordedRequest {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]recordedRequest(nil), r.requests...)
}

func counterSnapshot(t *testing.T, credential, day string, version, upload, download int64) traffic.CounterSnapshot {
	t.Helper()
	date, err := traffic.ParseDate(day)
	if err != nil {
		t.Fatalf("parse date %q: %v", day, err)
	}
	snapshot, err := traffic.MakeCounterSnapshot(testSourceID, credential, date, version, upload, download)
	if err != nil {
		t.Fatalf("make snapshot: %v", err)
	}
	return snapshot
}

func pageRequest(t *testing.T, snapshots []traffic.CounterSnapshot) traffic.Request {
	t.Helper()
	page := make([]traffic.Snapshot, len(snapshots))
	for index := range snapshots {
		page[index] = snapshots[index]
	}
	request, err := traffic.MakeRequest(page...)
	if err != nil {
		t.Fatalf("make request: %v", err)
	}
	return request
}

func receiptsFor(request traffic.Request) traffic.ReceiptResponse {
	receipts := make([]traffic.Receipt, len(request.Snapshots))
	for index, snapshot := range request.Snapshots {
		counter := snapshot.(traffic.CounterSnapshot)
		receipts[index] = traffic.Receipt{
			SourceID:       counter.SourceID,
			CredentialID:   counter.CredentialID,
			Kind:           traffic.KindCounter,
			AccountingDate: counter.AccountingDate,
			Version:        counter.Version,
			Digest:         counter.Digest,
		}
	}
	return traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: receipts}
}

func newTLSServer(t *testing.T, handler http.HandlerFunc) *httptest.Server {
	t.Helper()
	server := httptest.NewUnstartedServer(handler)
	server.Config.ErrorLog = log.New(io.Discard, "", 0)
	server.StartTLS()
	t.Cleanup(server.Close)
	return server
}

func trustRoots(t *testing.T, server *httptest.Server) *x509.CertPool {
	t.Helper()
	certificate := server.Certificate()
	if certificate == nil {
		t.Fatal("test server has no certificate")
	}
	roots := x509.NewCertPool()
	roots.AddCert(certificate)
	return roots
}

func newReceiptServer(t *testing.T, recorder *recorder) *httptest.Server {
	t.Helper()
	return newTLSServer(t, func(writer http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("read request body: %v", err)
			http.Error(writer, "read failed", http.StatusInternalServerError)
			return
		}
		if recorder != nil {
			recorder.add(request, body)
		}
		parsed, err := traffic.ParseRequest(body)
		if err != nil {
			t.Errorf("parse request: %v", err)
			http.Error(writer, "parse failed", http.StatusBadRequest)
			return
		}
		raw, err := traffic.EncodeReceiptResponse(receiptsFor(parsed), parsed)
		if err != nil {
			t.Errorf("encode receipts: %v", err)
			http.Error(writer, "encode failed", http.StatusInternalServerError)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		if _, err := writer.Write(raw); err != nil {
			t.Errorf("write response: %v", err)
		}
	})
}

type wireReceipt struct {
	SourceID       string `json:"source_id"`
	CredentialID   string `json:"credential_id"`
	Kind           string `json:"kind"`
	AccountingDate string `json:"accounting_date"`
	Version        int64  `json:"version"`
	Digest         string `json:"digest"`
}

type wireReceipts struct {
	Schema   int64         `json:"schema"`
	Receipts []wireReceipt `json:"receipts"`
}

func marshalReceiptResponse(t *testing.T, response traffic.ReceiptResponse) []byte {
	t.Helper()
	document := wireReceipts{Schema: response.Schema, Receipts: make([]wireReceipt, len(response.Receipts))}
	for index, receipt := range response.Receipts {
		document.Receipts[index] = wireReceipt{
			SourceID:       receipt.SourceID,
			CredentialID:   receipt.CredentialID,
			Kind:           string(receipt.Kind),
			AccountingDate: receipt.AccountingDate.String(),
			Version:        receipt.Version,
			Digest:         receipt.Digest,
		}
	}
	raw, err := json.Marshal(document)
	if err != nil {
		t.Fatalf("marshal wire receipts: %v", err)
	}
	return raw
}

func assertNoLeak(t *testing.T, err error, needles ...string) {
	t.Helper()
	message := err.Error()
	for _, needle := range needles {
		if needle == "" {
			continue
		}
		if strings.Contains(message, needle) {
			t.Fatalf("error exposes %q: %v", needle, err)
		}
	}
}

func TestExportOncePostsOneCanonicalPageAndAcknowledges(t *testing.T) {
	snapshots := []traffic.CounterSnapshot{
		counterSnapshot(t, credentialA, "2026-10-24", 2, 10, 20),
		counterSnapshot(t, credentialB, "2026-10-25", 1, 0, 5),
	}
	expectedRequest := pageRequest(t, snapshots)
	expectedBody, err := traffic.EncodeRequest(expectedRequest)
	if err != nil {
		t.Fatalf("encode expected request: %v", err)
	}
	recorder := &recorder{}
	server := newTLSServer(t, func(writer http.ResponseWriter, request *http.Request) {
		if request.TLS == nil {
			t.Error("request did not use TLS")
		} else if request.TLS.Version < tls.VersionTLS12 {
			t.Errorf("TLS version = %d, want at least TLS 1.2", request.TLS.Version)
		}
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("read request body: %v", err)
			http.Error(writer, "read failed", http.StatusInternalServerError)
			return
		}
		recorder.add(request, body)
		parsed, err := traffic.ParseRequest(body)
		if err != nil {
			t.Errorf("parse request: %v", err)
			http.Error(writer, "parse failed", http.StatusBadRequest)
			return
		}
		raw, err := traffic.EncodeReceiptResponse(receiptsFor(parsed), parsed)
		if err != nil {
			t.Errorf("encode receipts: %v", err)
			http.Error(writer, "encode failed", http.StatusInternalServerError)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		if _, err := writer.Write(raw); err != nil {
			t.Errorf("write response: %v", err)
		}
	})
	store := &fakeStore{snapshots: snapshots}
	store.ackFunc = func(_ context.Context, request traffic.Request, response traffic.ReceiptResponse) (int, error) {
		if _, err := traffic.EncodeRequest(request); err != nil {
			t.Errorf("ack received invalid request: %v", err)
		}
		if _, err := traffic.EncodeReceiptResponse(response, request); err != nil {
			t.Errorf("ack received invalid response: %v", err)
		}
		return len(request.Snapshots), nil
	}
	client, err := New(store, Options{
		Origin:   server.URL + "/",
		Token:    testToken,
		RootCAs:  trustRoots(t, server),
		PageSize: 2,
	})
	if err != nil {
		t.Fatalf("new exporter: %v", err)
	}
	result, err := client.ExportOnce(context.Background())
	if err != nil {
		t.Fatalf("export once: %v", err)
	}
	if result.Selected != 2 || result.Acknowledged != 2 {
		t.Fatalf("result = %+v, want selected 2 and acknowledged 2", result)
	}
	if store.pendingCalls != 1 || store.lastLimit != 2 {
		t.Fatalf("pending calls = %d, limit = %d, want one call with limit 2", store.pendingCalls, store.lastLimit)
	}
	if store.ackCalls != 1 {
		t.Fatalf("ack calls = %d, want 1", store.ackCalls)
	}
	if len(store.ackRequest.Snapshots) != 2 || len(store.ackResponse.Receipts) != 2 {
		t.Fatalf("ack page = %d request rows, %d receipts, want 2 and 2", len(store.ackRequest.Snapshots), len(store.ackResponse.Receipts))
	}
	requests := recorder.snapshot()
	if len(requests) != 1 {
		t.Fatalf("HTTP requests = %d, want exactly 1", len(requests))
	}
	request := requests[0]
	if request.method != http.MethodPost {
		t.Fatalf("method = %q, want POST", request.method)
	}
	if request.path != "/node/v1/traffic" {
		t.Fatalf("path = %q, want /node/v1/traffic", request.path)
	}
	if request.rawQuery != "" {
		t.Fatalf("query = %q, want empty", request.rawQuery)
	}
	if request.authorization != "Bearer "+testToken {
		t.Fatalf("authorization = %q, want exact bearer token", request.authorization)
	}
	if request.contentType != "application/json" {
		t.Fatalf("content type = %q, want application/json", request.contentType)
	}
	if request.acceptEncoding != "" {
		t.Fatalf("accept encoding = %q, want transparent compression disabled", request.acceptEncoding)
	}
	if !bytes.Equal(request.body, expectedBody) {
		t.Fatalf("request body = %s, want canonical %s", request.body, expectedBody)
	}
}

func TestExportOnceEmptyPagePerformsNoHTTPOrAck(t *testing.T) {
	recorder := &recorder{}
	server := newTLSServer(t, func(writer http.ResponseWriter, request *http.Request) {
		recorder.add(request, nil)
		writer.WriteHeader(http.StatusInternalServerError)
	})
	store := &fakeStore{}
	client, err := New(store, Options{
		Origin:  server.URL,
		Token:   testToken,
		RootCAs: trustRoots(t, server),
	})
	if err != nil {
		t.Fatalf("new exporter: %v", err)
	}
	result, err := client.ExportOnce(context.Background())
	if err != nil {
		t.Fatalf("export once: %v", err)
	}
	if result != (Result{}) {
		t.Fatalf("result = %+v, want zero", result)
	}
	if store.pendingCalls != 1 || store.lastLimit != traffic.MaxPageItems {
		t.Fatalf("pending calls = %d, limit = %d, want default page size", store.pendingCalls, store.lastLimit)
	}
	if store.ackCalls != 0 {
		t.Fatalf("ack calls = %d, want 0", store.ackCalls)
	}
	if requests := recorder.snapshot(); len(requests) != 0 {
		t.Fatalf("HTTP requests = %d, want 0", len(requests))
	}
}

func TestNewRejectsInvalidOptions(t *testing.T) {
	store := &fakeStore{}
	cases := []struct {
		name    string
		store   CounterStore
		options Options
	}{
		{name: "nil store", options: Options{Origin: "https://example.com", Token: "token"}},
		{name: "empty origin", store: store, options: Options{Token: "token"}},
		{name: "http origin", store: store, options: Options{Origin: "http://example.com", Token: "token"}},
		{name: "missing host", store: store, options: Options{Origin: "https://", Token: "token"}},
		{name: "userinfo", store: store, options: Options{Origin: "https://user:pass@example.com", Token: "token"}},
		{name: "query", store: store, options: Options{Origin: "https://example.com?page=1", Token: "token"}},
		{name: "empty query", store: store, options: Options{Origin: "https://example.com?", Token: "token"}},
		{name: "fragment", store: store, options: Options{Origin: "https://example.com#fragment", Token: "token"}},
		{name: "empty fragment", store: store, options: Options{Origin: "https://example.com#", Token: "token"}},
		{name: "root path empty fragment", store: store, options: Options{Origin: "https://example.com/#", Token: "token"}},
		{name: "non-root path", store: store, options: Options{Origin: "https://example.com/other", Token: "token"}},
		{name: "escaped slash path", store: store, options: Options{Origin: "https://example.com/%2F", Token: "token"}},
		{name: "empty token", store: store, options: Options{Origin: "https://example.com", Token: ""}},
		{name: "space token", store: store, options: Options{Origin: "https://example.com", Token: "token with space"}},
		{name: "tab token", store: store, options: Options{Origin: "https://example.com", Token: "token\t"}},
		{name: "newline token", store: store, options: Options{Origin: "https://example.com", Token: "token\n"}},
		{name: "carriage return token", store: store, options: Options{Origin: "https://example.com", Token: "token\r"}},
		{name: "delete control token", store: store, options: Options{Origin: "https://example.com", Token: "token\x7f"}},
		{name: "negative page size", store: store, options: Options{Origin: "https://example.com", Token: "token", PageSize: -1}},
		{name: "oversized page size", store: store, options: Options{Origin: "https://example.com", Token: "token", PageSize: traffic.MaxPageItems + 1}},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			client, err := New(testCase.store, testCase.options)
			if !errors.Is(err, ErrInvalidOptions) {
				t.Fatalf("error = %v, want ErrInvalidOptions", err)
			}
			if client != nil {
				t.Fatal("client created for invalid options")
			}
			assertNoLeak(t, err, testCase.options.Token, testCase.options.Origin)
		})
	}
}

func TestNewAcceptsRootOriginAndOpaqueToken(t *testing.T) {
	token := "opaque:" + strings.Repeat("x", 200)
	cases := []struct {
		name       string
		origin     string
		pageSize   int
		wantPage   int
		wantTarget string
	}{
		{
			name:       "empty path defaults page size",
			origin:     "https://example.com",
			wantPage:   traffic.MaxPageItems,
			wantTarget: "https://example.com/node/v1/traffic",
		},
		{
			name:       "root path",
			origin:     "https://example.com/",
			pageSize:   1,
			wantPage:   1,
			wantTarget: "https://example.com/node/v1/traffic",
		},
		{
			name:       "port and maximum page size",
			origin:     "https://example.com:8443",
			pageSize:   traffic.MaxPageItems,
			wantPage:   traffic.MaxPageItems,
			wantTarget: "https://example.com:8443/node/v1/traffic",
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			client, err := New(&fakeStore{}, Options{
				Origin:   testCase.origin,
				Token:    token,
				RootCAs:  x509.NewCertPool(),
				PageSize: testCase.pageSize,
			})
			if err != nil {
				t.Fatalf("new exporter: %v", err)
			}
			if client.token != token {
				t.Fatalf("token was normalized")
			}
			if client.pageSize != testCase.wantPage {
				t.Fatalf("page size = %d, want %d", client.pageSize, testCase.wantPage)
			}
			if client.endpoint != testCase.wantTarget {
				t.Fatalf("endpoint = %q, want %q", client.endpoint, testCase.wantTarget)
			}
			client.CloseIdleConnections()
		})
	}
}

func TestNewUsesDedicatedRestrictedTransport(t *testing.T) {
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:9")
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:9")
	t.Setenv("ALL_PROXY", "http://127.0.0.1:9")
	client, err := New(&fakeStore{}, Options{Origin: "https://example.com", Token: "token"})
	if err != nil {
		t.Fatalf("new exporter: %v", err)
	}
	if client.transport.Proxy != nil {
		t.Fatal("transport uses an environment or caller proxy")
	}
	if client.transport.DialContext == nil {
		t.Fatal("transport has no bounded dialer")
	}
	if !client.transport.DisableCompression {
		t.Fatal("transparent compression is enabled")
	}
	if client.transport.TLSHandshakeTimeout <= 0 || client.transport.TLSHandshakeTimeout > 30*time.Second {
		t.Fatalf("TLS handshake timeout = %v, want a positive bounded wait", client.transport.TLSHandshakeTimeout)
	}
	if client.transport.ResponseHeaderTimeout <= 0 || client.transport.ResponseHeaderTimeout > 30*time.Second {
		t.Fatalf("response header timeout = %v, want a positive bounded wait", client.transport.ResponseHeaderTimeout)
	}
	if client.transport.TLSClientConfig == nil {
		t.Fatal("transport has no TLS config")
	}
	if client.transport.TLSClientConfig.RootCAs != nil {
		t.Fatal("nil RootCAs did not select the system trust store")
	}
	if client.transport.TLSClientConfig.InsecureSkipVerify {
		t.Fatal("transport disables certificate verification")
	}
	if client.transport.TLSClientConfig.MinVersion != tls.VersionTLS12 {
		t.Fatalf("minimum TLS version = %d, want TLS 1.2", client.transport.TLSClientConfig.MinVersion)
	}
	if client.httpClient.Timeout != 30*time.Second {
		t.Fatalf("client timeout = %v, want 30s", client.httpClient.Timeout)
	}
	if client.httpClient.Jar != nil {
		t.Fatal("client has a cookie jar")
	}
	if client.httpClient.CheckRedirect == nil {
		t.Fatal("client has no redirect policy")
	}
	if err := client.httpClient.CheckRedirect(nil, nil); !errors.Is(err, http.ErrUseLastResponse) {
		t.Fatalf("redirect policy = %v, want ErrUseLastResponse", err)
	}
	client.CloseIdleConnections()
}

func TestExportOnceRequiresTrustedCA(t *testing.T) {
	snapshots := []traffic.CounterSnapshot{
		counterSnapshot(t, credentialA, "2026-10-24", 1, 7, 9),
	}
	server := newReceiptServer(t, nil)
	t.Run("untrusted", func(t *testing.T) {
		store := &fakeStore{snapshots: snapshots, ackResult: len(snapshots)}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: x509.NewCertPool(),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		result, err := client.ExportOnce(context.Background())
		if !errors.Is(err, ErrRequest) {
			t.Fatalf("error = %v, want ErrRequest", err)
		}
		if result.Selected != len(snapshots) || result.Acknowledged != 0 {
			t.Fatalf("result = %+v, want selected rows and zero acknowledged", result)
		}
		if store.ackCalls != 0 {
			t.Fatalf("ack calls = %d, want 0", store.ackCalls)
		}
		assertNoLeak(t, err, testToken, credentialA)
	})
	t.Run("trusted", func(t *testing.T) {
		store := &fakeStore{snapshots: snapshots, ackResult: len(snapshots)}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: trustRoots(t, server),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		result, err := client.ExportOnce(context.Background())
		if err != nil {
			t.Fatalf("export once: %v", err)
		}
		if result.Selected != 1 || result.Acknowledged != 1 {
			t.Fatalf("result = %+v, want 1 and 1", result)
		}
	})
	t.Run("cloned roots", func(t *testing.T) {
		roots := x509.NewCertPool()
		store := &fakeStore{snapshots: snapshots, ackResult: len(snapshots)}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: roots,
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		roots.AddCert(server.Certificate())
		if _, err := client.ExportOnce(context.Background()); !errors.Is(err, ErrRequest) {
			t.Fatalf("error = %v, want ErrRequest after mutating the caller pool", err)
		}
		if store.ackCalls != 0 {
			t.Fatalf("ack calls = %d, want 0", store.ackCalls)
		}
	})
}

func TestExportOnceDoesNotFollowRedirects(t *testing.T) {
	snapshots := []traffic.CounterSnapshot{
		counterSnapshot(t, credentialA, "2026-10-24", 1, 1, 2),
	}
	recorder := &recorder{}
	server := newTLSServer(t, func(writer http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("read request body: %v", err)
		}
		recorder.add(request, body)
		if request.URL.Path == "/node/v1/traffic" {
			http.Redirect(writer, request, "/node/v1/traffic-other", http.StatusTemporaryRedirect)
			return
		}
		writer.WriteHeader(http.StatusOK)
	})
	store := &fakeStore{snapshots: snapshots, ackResult: len(snapshots)}
	client, err := New(store, Options{
		Origin:  server.URL,
		Token:   testToken,
		RootCAs: trustRoots(t, server),
	})
	if err != nil {
		t.Fatalf("new exporter: %v", err)
	}
	result, err := client.ExportOnce(context.Background())
	if !errors.Is(err, ErrRequestStatus) {
		t.Fatalf("error = %v, want ErrRequestStatus", err)
	}
	if !strings.Contains(err.Error(), "307") {
		t.Fatalf("error = %v, want bounded redirect status", err)
	}
	if result.Selected != 1 || result.Acknowledged != 0 {
		t.Fatalf("result = %+v, want selected and zero acknowledged", result)
	}
	if store.ackCalls != 0 {
		t.Fatalf("ack calls = %d, want 0", store.ackCalls)
	}
	requests := recorder.snapshot()
	if len(requests) != 1 || requests[0].path != "/node/v1/traffic" {
		t.Fatalf("requests = %+v, want exactly one to /node/v1/traffic", requests)
	}
}

func TestExportOncePreservesContextCancellation(t *testing.T) {
	t.Run("store selection", func(t *testing.T) {
		recorder := &recorder{}
		server := newTLSServer(t, func(writer http.ResponseWriter, request *http.Request) {
			recorder.add(request, nil)
			writer.WriteHeader(http.StatusInternalServerError)
		})
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		store := &fakeStore{pendingFunc: func(ctx context.Context, _ int) ([]traffic.CounterSnapshot, error) {
			cancel()
			return nil, ctx.Err()
		}}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: trustRoots(t, server),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		result, err := client.ExportOnce(ctx)
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("error = %v, want context.Canceled", err)
		}
		if result != (Result{}) {
			t.Fatalf("result = %+v, want zero", result)
		}
		if store.pendingCalls != 1 {
			t.Fatalf("pending calls = %d, want 1", store.pendingCalls)
		}
		if store.ackCalls != 0 {
			t.Fatalf("ack calls = %d, want 0", store.ackCalls)
		}
		if requests := recorder.snapshot(); len(requests) != 0 {
			t.Fatalf("HTTP requests = %d, want 0", len(requests))
		}
	})
	t.Run("canceled before selection", func(t *testing.T) {
		recorder := &recorder{}
		server := newTLSServer(t, func(writer http.ResponseWriter, request *http.Request) {
			recorder.add(request, nil)
			writer.WriteHeader(http.StatusInternalServerError)
		})
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		store := &fakeStore{snapshots: []traffic.CounterSnapshot{
			counterSnapshot(t, credentialA, "2026-10-24", 1, 1, 2),
		}}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: trustRoots(t, server),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		result, err := client.ExportOnce(ctx)
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("error = %v, want context.Canceled", err)
		}
		if result != (Result{}) {
			t.Fatalf("result = %+v, want zero", result)
		}
		if store.pendingCalls != 0 {
			t.Fatalf("pending calls = %d, want 0", store.pendingCalls)
		}
		if store.ackCalls != 0 {
			t.Fatalf("ack calls = %d, want 0", store.ackCalls)
		}
		if requests := recorder.snapshot(); len(requests) != 0 {
			t.Fatalf("HTTP requests = %d, want 0", len(requests))
		}
	})
	t.Run("store cancellation sentinel", func(t *testing.T) {
		recorder := &recorder{}
		server := newTLSServer(t, func(writer http.ResponseWriter, request *http.Request) {
			recorder.add(request, nil)
			writer.WriteHeader(http.StatusInternalServerError)
		})
		store := &fakeStore{pendingErr: context.Canceled}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: trustRoots(t, server),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		result, err := client.ExportOnce(context.Background())
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("error = %v, want context.Canceled", err)
		}
		if result != (Result{}) {
			t.Fatalf("result = %+v, want zero", result)
		}
		if store.ackCalls != 0 {
			t.Fatalf("ack calls = %d, want 0", store.ackCalls)
		}
		if requests := recorder.snapshot(); len(requests) != 0 {
			t.Fatalf("HTTP requests = %d, want 0", len(requests))
		}
	})
	t.Run("HTTPS request", func(t *testing.T) {
		snapshots := []traffic.CounterSnapshot{
			counterSnapshot(t, credentialA, "2026-10-24", 1, 3, 4),
		}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		started := make(chan struct{})
		release := make(chan struct{})
		defer close(release)
		server := newTLSServer(t, func(_ http.ResponseWriter, _ *http.Request) {
			close(started)
			<-release
		})
		store := &fakeStore{snapshots: snapshots, ackResult: len(snapshots)}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: trustRoots(t, server),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		type outcome struct {
			result Result
			err    error
		}
		done := make(chan outcome, 1)
		go func() {
			result, err := client.ExportOnce(ctx)
			done <- outcome{result: result, err: err}
		}()
		select {
		case <-started:
		case <-time.After(5 * time.Second):
			t.Fatal("HTTPS request did not start")
		}
		cancel()
		var got outcome
		select {
		case got = <-done:
		case <-time.After(5 * time.Second):
			t.Fatal("export did not observe cancellation")
		}
		if !errors.Is(got.err, context.Canceled) {
			t.Fatalf("error = %v, want context.Canceled", got.err)
		}
		if got.result.Selected != 1 || got.result.Acknowledged != 0 {
			t.Fatalf("result = %+v, want selected and zero acknowledged", got.result)
		}
		if store.ackCalls != 0 {
			t.Fatalf("ack calls = %d, want 0", store.ackCalls)
		}
	})
}

func TestExportOnceRejectsInvalidResponsesWithoutAck(t *testing.T) {
	snapshots := []traffic.CounterSnapshot{
		counterSnapshot(t, credentialA, "2026-10-24", 2, 10, 20),
		counterSnapshot(t, credentialB, "2026-10-25", 1, 0, 5),
	}
	request := pageRequest(t, snapshots)
	valid := receiptsFor(request)
	missing := traffic.ReceiptResponse{Schema: traffic.SchemaVersion, Receipts: append([]traffic.Receipt(nil), valid.Receipts[:1]...)}
	extra := traffic.ReceiptResponse{
		Schema: traffic.SchemaVersion,
		Receipts: append(append([]traffic.Receipt(nil), valid.Receipts...), traffic.Receipt{
			SourceID:       testSourceID,
			CredentialID:   credentialC,
			Kind:           traffic.KindCounter,
			AccountingDate: traffic.Date("2026-10-23"),
			Version:        1,
			Digest:         strings.Repeat("a", 64),
		}),
	}
	duplicate := traffic.ReceiptResponse{
		Schema:   traffic.SchemaVersion,
		Receipts: []traffic.Receipt{valid.Receipts[0], valid.Receipts[0]},
	}
	cases := []struct {
		name   string
		status int
		body   []byte
		want   error
		leaks  []string
	}{
		{
			name:   "error status",
			status: http.StatusInternalServerError,
			body:   []byte("server refused " + testToken + " for " + credentialA),
			want:   ErrRequestStatus,
			leaks:  []string{testToken, credentialA, "server refused"},
		},
		{
			name:   "malformed body",
			status: http.StatusOK,
			body:   []byte("not a receipt response " + credentialA),
			want:   ErrResponseInvalid,
			leaks:  []string{credentialA, "not a receipt response"},
		},
		{
			name:   "missing receipt",
			status: http.StatusOK,
			body:   marshalReceiptResponse(t, missing),
			want:   ErrResponseInvalid,
			leaks:  []string{credentialA, credentialB},
		},
		{
			name:   "extra receipt",
			status: http.StatusOK,
			body:   marshalReceiptResponse(t, extra),
			want:   ErrResponseInvalid,
			leaks:  []string{credentialA, credentialB, credentialC},
		},
		{
			name:   "duplicate receipt",
			status: http.StatusOK,
			body:   marshalReceiptResponse(t, duplicate),
			want:   ErrResponseInvalid,
			leaks:  []string{credentialA, credentialB},
		},
		{
			name:   "oversized body",
			status: http.StatusOK,
			body:   bytes.Repeat([]byte(" "), traffic.MaxRawBodyBytes+1),
			want:   ErrResponseTooLarge,
			leaks:  []string{credentialA},
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			recorder := &recorder{}
			server := newTLSServer(t, func(writer http.ResponseWriter, request *http.Request) {
				body, err := io.ReadAll(request.Body)
				if err != nil {
					t.Errorf("read request body: %v", err)
				}
				recorder.add(request, body)
				writer.WriteHeader(testCase.status)
				if _, err := writer.Write(testCase.body); err != nil {
					t.Errorf("write response: %v", err)
				}
			})
			store := &fakeStore{snapshots: snapshots}
			client, err := New(store, Options{
				Origin:  server.URL,
				Token:   testToken,
				RootCAs: trustRoots(t, server),
			})
			if err != nil {
				t.Fatalf("new exporter: %v", err)
			}
			result, err := client.ExportOnce(context.Background())
			if !errors.Is(err, testCase.want) {
				t.Fatalf("error = %v, want %v", err, testCase.want)
			}
			if result.Selected != 2 || result.Acknowledged != 0 {
				t.Fatalf("result = %+v, want selected 2 and zero acknowledged", result)
			}
			if store.ackCalls != 0 {
				t.Fatalf("ack calls = %d, want 0", store.ackCalls)
			}
			if requests := recorder.snapshot(); len(requests) != 1 {
				t.Fatalf("HTTP requests = %d, want exactly 1", len(requests))
			}
			assertNoLeak(t, err, testCase.leaks...)
		})
	}
}

func TestExportOnceRejectsTruncatedResponseWithoutAck(t *testing.T) {
	snapshots := []traffic.CounterSnapshot{
		counterSnapshot(t, credentialA, "2026-10-24", 1, 10, 20),
	}
	server := newTLSServer(t, func(writer http.ResponseWriter, _ *http.Request) {
		hijacker, ok := writer.(http.Hijacker)
		if !ok {
			t.Error("test server does not support connection hijacking")
			return
		}
		connection, buffer, err := hijacker.Hijack()
		if err != nil {
			t.Errorf("hijack connection: %v", err)
			return
		}
		defer connection.Close()
		_, _ = buffer.WriteString("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n")
		_, _ = buffer.WriteString(`{"schema":1,`)
		_ = buffer.Flush()
	})
	store := &fakeStore{snapshots: snapshots, ackResult: len(snapshots)}
	client, err := New(store, Options{
		Origin:  server.URL,
		Token:   testToken,
		RootCAs: trustRoots(t, server),
	})
	if err != nil {
		t.Fatalf("new exporter: %v", err)
	}
	result, err := client.ExportOnce(context.Background())
	if !errors.Is(err, ErrResponseRead) {
		t.Fatalf("error = %v, want ErrResponseRead", err)
	}
	if result.Selected != 1 || result.Acknowledged != 0 {
		t.Fatalf("result = %+v, want selected 1 and zero acknowledged", result)
	}
	if store.ackCalls != 0 {
		t.Fatalf("ack calls = %d, want 0", store.ackCalls)
	}
	assertNoLeak(t, err, credentialA)
}

func TestExportOnceAcknowledgesOnlyStoreCommittedReceipts(t *testing.T) {
	snapshots := []traffic.CounterSnapshot{
		counterSnapshot(t, credentialA, "2026-10-24", 1, 10, 20),
		counterSnapshot(t, credentialB, "2026-10-25", 2, 0, 5),
	}
	t.Run("matching receipts", func(t *testing.T) {
		server := newReceiptServer(t, nil)
		store := &fakeStore{snapshots: snapshots, ackResult: len(snapshots)}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: trustRoots(t, server),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		result, err := client.ExportOnce(context.Background())
		if err != nil {
			t.Fatalf("export once: %v", err)
		}
		if result.Selected != 2 || result.Acknowledged != 2 {
			t.Fatalf("result = %+v, want 2 and 2", result)
		}
		if store.ackCalls != 1 {
			t.Fatalf("ack calls = %d, want 1", store.ackCalls)
		}
	})
	t.Run("stored receipts diverge", func(t *testing.T) {
		request := pageRequest(t, snapshots)
		response := receiptsFor(request)
		for index := range response.Receipts {
			response.Receipts[index].Version++
			response.Receipts[index].Digest = strings.Repeat("b", 64)
		}
		raw, err := traffic.EncodeReceiptResponse(response, request)
		if err != nil {
			t.Fatalf("encode divergent receipts: %v", err)
		}
		server := newTLSServer(t, func(writer http.ResponseWriter, _ *http.Request) {
			writer.Header().Set("Content-Type", "application/json")
			if _, err := writer.Write(raw); err != nil {
				t.Errorf("write response: %v", err)
			}
		})
		store := &fakeStore{snapshots: snapshots}
		store.ackResult = 0
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: trustRoots(t, server),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		result, err := client.ExportOnce(context.Background())
		if err != nil {
			t.Fatalf("export once: %v", err)
		}
		if result.Selected != 2 || result.Acknowledged != 0 {
			t.Fatalf("result = %+v, want selected 2 and zero acknowledged", result)
		}
		if store.ackCalls != 1 {
			t.Fatalf("ack calls = %d, want 1", store.ackCalls)
		}
		if len(store.ackResponse.Receipts) != 2 {
			t.Fatalf("ack receipts = %d, want 2", len(store.ackResponse.Receipts))
		}
		if store.ackResponse.Receipts[0].Version != response.Receipts[0].Version ||
			store.ackResponse.Receipts[0].Digest != response.Receipts[0].Digest {
			t.Fatal("ack received request receipts instead of stored receipts")
		}
	})
	t.Run("ack error", func(t *testing.T) {
		server := newReceiptServer(t, nil)
		store := &fakeStore{snapshots: snapshots}
		store.ackFunc = func(context.Context, traffic.Request, traffic.ReceiptResponse) (int, error) {
			return len(snapshots), errors.New("sqlite failure at /secret/path")
		}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: trustRoots(t, server),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		result, err := client.ExportOnce(context.Background())
		if !errors.Is(err, ErrAcknowledge) {
			t.Fatalf("error = %v, want ErrAcknowledge", err)
		}
		if result.Selected != 2 || result.Acknowledged != 0 {
			t.Fatalf("result = %+v, want selected 2 and zero acknowledged", result)
		}
		if store.ackCalls != 1 {
			t.Fatalf("ack calls = %d, want 1", store.ackCalls)
		}
		assertNoLeak(t, err, "sqlite", "/secret/path", credentialA, credentialB)
	})
	t.Run("invalid ack count", func(t *testing.T) {
		server := newReceiptServer(t, nil)
		store := &fakeStore{snapshots: snapshots, ackResult: len(snapshots) + 1}
		client, err := New(store, Options{
			Origin:  server.URL,
			Token:   testToken,
			RootCAs: trustRoots(t, server),
		})
		if err != nil {
			t.Fatalf("new exporter: %v", err)
		}
		result, err := client.ExportOnce(context.Background())
		if !errors.Is(err, ErrAcknowledge) {
			t.Fatalf("error = %v, want ErrAcknowledge", err)
		}
		if result.Selected != 2 || result.Acknowledged != 0 {
			t.Fatalf("result = %+v, want selected 2 and zero acknowledged", result)
		}
	})
}

func TestExportOnceSelectionAndEncodingFailuresPerformNoHTTP(t *testing.T) {
	valid := counterSnapshot(t, credentialA, "2026-10-24", 1, 1, 2)
	invalidDigest := valid
	invalidDigest.Digest = strings.Repeat("0", 64)
	cases := []struct {
		name         string
		store        *fakeStore
		pageSize     int
		want         error
		wantSelected int
		leaks        []string
	}{
		{
			name:         "selection error",
			store:        &fakeStore{pendingErr: errors.New("database failure at /secret/path")},
			pageSize:     10,
			want:         ErrSelectPending,
			wantSelected: 0,
			leaks:        []string{"database", "/secret/path"},
		},
		{
			name:         "page overflow",
			store:        &fakeStore{snapshots: []traffic.CounterSnapshot{valid, counterSnapshot(t, credentialB, "2026-10-25", 1, 1, 2)}},
			pageSize:     1,
			want:         ErrSelectPending,
			wantSelected: 0,
			leaks:        []string{credentialA, credentialB},
		},
		{
			name:         "invalid snapshot",
			store:        &fakeStore{snapshots: []traffic.CounterSnapshot{invalidDigest}},
			pageSize:     10,
			want:         ErrEncodeRequest,
			wantSelected: 1,
			leaks:        []string{invalidDigest.Digest, credentialA},
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			recorder := &recorder{}
			server := newTLSServer(t, func(writer http.ResponseWriter, request *http.Request) {
				recorder.add(request, nil)
				writer.WriteHeader(http.StatusInternalServerError)
			})
			client, err := New(testCase.store, Options{
				Origin:   server.URL,
				Token:    testToken,
				RootCAs:  trustRoots(t, server),
				PageSize: testCase.pageSize,
			})
			if err != nil {
				t.Fatalf("new exporter: %v", err)
			}
			result, err := client.ExportOnce(context.Background())
			if !errors.Is(err, testCase.want) {
				t.Fatalf("error = %v, want %v", err, testCase.want)
			}
			if result.Selected != testCase.wantSelected || result.Acknowledged != 0 {
				t.Fatalf("result = %+v, want selected %d and zero acknowledged", result, testCase.wantSelected)
			}
			if testCase.store.ackCalls != 0 {
				t.Fatalf("ack calls = %d, want 0", testCase.store.ackCalls)
			}
			if requests := recorder.snapshot(); len(requests) != 0 {
				t.Fatalf("HTTP requests = %d, want 0", len(requests))
			}
			assertNoLeak(t, err, testCase.leaks...)
		})
	}
}
