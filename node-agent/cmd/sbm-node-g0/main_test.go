package main

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/contract"
)

func TestInitIsPrivateIdempotentAndDoesNotPrintSecret(t *testing.T) {
	path := filepath.Join(t.TempDir(), "identity.json")
	read, write, _ := os.Pipe()
	old := os.Stdout
	os.Stdout = write
	defer func() { os.Stdout = old }()
	if err := initialize(path, "https://control.fixture.test", "/fixture/ca.crt"); err != nil {
		t.Fatal(err)
	}
	if err := initialize(path, "https://control.fixture.test", "/fixture/ca.crt"); err != nil {
		t.Fatal(err)
	}
	write.Close()
	output, _ := io.ReadAll(read)
	read.Close()
	var id contract.Identity
	if err := readJSON(path, &id); err != nil {
		t.Fatal(err)
	}
	if len(id.Secret) != 64 || strings.Contains(string(output), id.Secret) {
		t.Fatal("invalid or exposed secret")
	}
	lines := strings.Split(strings.TrimSpace(string(output)), "\n")
	if len(lines) != 2 || lines[0] != lines[1] {
		t.Fatal("registration changed on retry")
	}
	var registration map[string]string
	if err := json.Unmarshal([]byte(lines[0]), &registration); err != nil {
		t.Fatal(err)
	}
	if registration["token_digest"] != contract.Hash([]byte(id.Secret)) {
		t.Fatal("digest mismatch")
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0600 {
		t.Fatal("identity permissions")
	}
	if initialize(path, "https://another.fixture.test", "/fixture/ca.crt") == nil {
		t.Fatal("origin overwritten")
	}
}

func TestHTTPSIgnoresProxyAndRefusesRedirect(t *testing.T) {
	var received string
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received = r.Header.Get("Authorization")
		w.Header().Set("Location", "https://never-contact.fixture.test/secret")
		w.WriteHeader(http.StatusFound)
	}))
	defer server.Close()
	ca := filepath.Join(t.TempDir(), "ca.crt")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := x509.ParseCertificate(server.Certificate().Raw); err != nil {
		t.Fatal(err)
	}
	id := contract.Identity{NodeID: "fixture-node", Secret: strings.Repeat("a", 64), Origin: server.URL, CAFile: ca}
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	client, err := apiClient(id)
	if err != nil {
		t.Fatal(err)
	}
	status, _, err := request(context.Background(), client, id, "GET", "/node-g0/v1/config", nil, "")
	if err != nil || status != http.StatusFound || received != "Bearer "+id.NodeID+"."+id.Secret {
		t.Fatalf("request result %d, %v", status, err)
	}
}

func TestOriginValidation(t *testing.T) {
	for _, origin := range []string{"http://host", "https://user:password@host", "https://host/path", "https://host?secret=value", "https://host#fragment", "https:///missing-host"} {
		if validOrigin(origin) {
			t.Errorf("accepted unsafe origin %q", origin)
		}
	}
}
