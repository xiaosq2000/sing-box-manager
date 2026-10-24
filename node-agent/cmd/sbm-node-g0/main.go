// sbm-node-g0 is a disposable-host feasibility executable, not a production agent.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/accounting"
	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/contract"
	"github.com/xiaosq2000/sing-box-manager/node-agent/internal/counter"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "G0 failed:", err)
		os.Exit(1)
	}
}

func run() error {
	if len(os.Args) < 2 {
		return errors.New("expected init or run")
	}
	flags := flag.NewFlagSet("sbm-node-g0", flag.ContinueOnError)
	identityPath := flags.String("identity", "", "private identity file")
	state := flags.String("state", "", "private disposable state directory")
	origin := flags.String("origin", "", "verified HTTPS fixture origin")
	ca := flags.String("ca", "", "fixture CA file")
	engine := flags.String("engine", "", "installed sing-box executable")
	service := flags.String("service", "sbm-g0-proxy.service", "disposable proxy unit")
	interval := flags.Duration("interval", 2*time.Second, "fixture cycle interval")
	if err := flags.Parse(os.Args[2:]); err != nil {
		return err
	}
	if *identityPath == "" || flags.NArg() != 0 {
		return errors.New("identity is required")
	}
	if os.Args[1] == "init" {
		return initialize(*identityPath, *origin, *ca)
	}
	if os.Args[1] != "run" || *state == "" || *engine == "" || *service != "sbm-g0-proxy.service" || *interval < time.Second {
		return errors.New("invalid disposable agent options")
	}
	var identity contract.Identity
	if err := readJSON(*identityPath, &identity); err != nil {
		return errors.New("identity read failed")
	}
	client, err := apiClient(identity)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(*state, 0700); err != nil {
		return errors.New("state creation failed")
	}
	store, err := accounting.Open(filepath.Join(*state, "traffic.sqlite3"), identity.SourceID)
	if err != nil {
		return errors.New("accounting open failed")
	}
	defer store.Close()
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	var current *contract.Bundle
	var saved contract.Bundle
	if readJSON(filepath.Join(*state, "current", "bundle.json"), &saved) == nil {
		if saved.Validate(identity.NodeID) != nil {
			return errors.New("invalid saved bundle")
		}
		current = &saved
	}
	ticker := time.NewTicker(*interval)
	defer ticker.Stop()
	for {
		if err := cycle(ctx, client, identity, *state, *engine, *service, store, &current); err != nil {
			// Never print raw HTTP, checker, database or subprocess errors.
			fmt.Fprintln(os.Stderr, "G0 cycle:", err)
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}

func initialize(path, origin, ca string) error {
	if !validOrigin(origin) || ca == "" {
		return errors.New("verified HTTPS origin and CA are required")
	}
	var identity contract.Identity
	if _, err := os.Stat(path); err == nil {
		if readJSON(path, &identity) != nil || identity.Origin != origin || identity.CAFile != ca {
			return errors.New("identity conflict")
		}
	} else if errors.Is(err, os.ErrNotExist) {
		secret := make([]byte, 32)
		if _, err := rand.Read(secret); err != nil {
			return errors.New("random source failed")
		}
		identity = contract.Identity{NodeID: contract.UUID(), SourceID: contract.UUID(), Secret: hex.EncodeToString(secret), Origin: origin, CAFile: ca}
		data, _ := json.Marshal(identity)
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			return errors.New("identity directory failed")
		}
		if err := writeNew(path, data); err != nil {
			return errors.New("identity publication failed")
		}
	} else {
		return errors.New("identity inspection failed")
	}
	// Registration receives only a digest. The secret never leaves this file.
	return json.NewEncoder(os.Stdout).Encode(map[string]string{"node_id": identity.NodeID, "source_id": identity.SourceID, "token_digest": contract.Hash([]byte(identity.Secret))})
}

func validOrigin(origin string) bool {
	u, err := url.Parse(origin)
	return err == nil && u.Scheme == "https" && u.Hostname() != "" && u.User == nil && u.Path == "" && u.RawQuery == "" && u.Fragment == ""
}

func apiClient(identity contract.Identity) (*http.Client, error) {
	if !validOrigin(identity.Origin) || len(identity.Secret) != 64 {
		return nil, errors.New("invalid identity origin or secret")
	}
	pem, err := os.ReadFile(identity.CAFile)
	pool := x509.NewCertPool()
	if err != nil || !pool.AppendCertsFromPEM(pem) {
		return nil, errors.New("invalid HTTPS trust")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	return &http.Client{Transport: transport, Timeout: 30 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, nil
}

func request(ctx context.Context, client *http.Client, identity contract.Identity, method, path string, body any, etag string) (int, []byte, error) {
	var data []byte
	if body != nil {
		var err error
		data, err = json.Marshal(body)
		if err != nil {
			return 0, nil, errors.New("request encoding failed")
		}
	}
	req, err := http.NewRequestWithContext(ctx, method, identity.Origin+path, bytes.NewReader(data))
	if err != nil {
		return 0, nil, errors.New("request creation failed")
	}
	req.Header.Set("Authorization", "Bearer "+identity.NodeID+"."+identity.Secret)
	req.Header.Set("Content-Type", "application/json")
	if etag != "" {
		req.Header.Set("If-None-Match", etag)
	}
	response, err := client.Do(req)
	if err != nil {
		return 0, nil, errors.New("HTTPS request failed")
	}
	defer response.Body.Close()
	data, err = io.ReadAll(io.LimitReader(response.Body, (8<<20)+1))
	if err != nil || len(data) > 8<<20 {
		return 0, nil, errors.New("response bound failed")
	}
	return response.StatusCode, data, nil
}

func cycle(ctx context.Context, client *http.Client, identity contract.Identity, state, engine, service string, store *accounting.Store, current **contract.Bundle) error {
	etag := ""
	if *current != nil {
		etag = fmt.Sprintf("\"%d:%s\"", (*current).Generation, (*current).ConfigSHA256)
	}
	status, data, fetchErr := request(ctx, client, identity, "GET", "/node-g0/v1/config", nil, etag)
	if fetchErr == nil && status == http.StatusOK {
		var candidate contract.Bundle
		if json.Unmarshal(data, &candidate) != nil || candidate.Validate(identity.NodeID) != nil {
			return errors.New("bundle validation failed")
		}
		if *current != nil && (candidate.Generation < (*current).Generation || (candidate.Generation == (*current).Generation && candidate.ConfigSHA256 != (*current).ConfigSHA256)) {
			return errors.New("generation conflict")
		}
		if *current == nil || candidate.Generation != (*current).Generation {
			if err := apply(ctx, state, engine, service, candidate); err != nil {
				return err
			}
			*current = &candidate
		}
	}
	// A failed pull must not stop collection or the locally running proxy.
	if *current == nil {
		return errors.New("no accepted bundle")
	}
	b := *current
	invocation, err := command(ctx, "systemctl", "show", service, "--property=InvocationID", "--value")
	if err != nil || invocation == "" {
		return errors.New("engine identity unavailable")
	}
	if _, err := command(ctx, "systemctl", "is-active", "--quiet", service); err != nil {
		return errors.New("engine inactive")
	}
	pollCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	up, down, err := counter.Read(pollCtx, b.StatsAddress, b.Roster[0].WireName)
	cancel()
	if err != nil {
		return errors.New("counter sample failed")
	}
	if err := store.Record(ctx, invocation, b.Roster[0].CredentialID, up, down, time.Now()); err != nil {
		return errors.New("accounting commit failed")
	}
	snapshots, err := store.Snapshots(ctx)
	if err != nil {
		return errors.New("snapshot read failed")
	}
	status, _, err = request(ctx, client, identity, "POST", "/node-g0/v1/traffic", map[string]any{"snapshots": snapshots}, "")
	if err != nil || status != 200 {
		return errors.New("traffic upload pending")
	}
	status, _, err = request(ctx, client, identity, "POST", "/node-g0/v1/status", map[string]any{"generation": b.Generation, "config_sha256": b.ConfigSHA256, "invocation": invocation}, "")
	if err != nil || status != 200 {
		return errors.New("status upload pending")
	}
	return nil
}

func apply(ctx context.Context, state, engine, service string, b contract.Bundle) error {
	dir := filepath.Join(state, "generations", fmt.Sprint(b.Generation))
	if err := os.MkdirAll(dir, 0700); err != nil {
		return errors.New("generation creation failed")
	}
	config := filepath.Join(dir, "config.json")
	if previous, err := os.ReadFile(config); err == nil {
		if !bytes.Equal(previous, b.Config) {
			return errors.New("generation content conflict")
		}
	} else if err := writeNew(config, b.Config); err != nil {
		return errors.New("config staging failed")
	}
	data, _ := json.Marshal(b)
	if previous, err := os.ReadFile(filepath.Join(dir, "bundle.json")); err == nil {
		if !bytes.Equal(previous, data) {
			return errors.New("generation metadata conflict")
		}
	} else if err := writeNew(filepath.Join(dir, "bundle.json"), data); err != nil {
		return errors.New("bundle staging failed")
	}
	if _, err := command(ctx, engine, "check", "-D", state, "-c", config); err != nil {
		return errors.New("config check failed")
	}
	if _, err := command(ctx, "systemctl", "stop", service); err != nil {
		return errors.New("engine stop failed")
	}
	pid, err := command(ctx, "systemctl", "show", service, "--property=MainPID", "--value")
	if err != nil || pid != "0" {
		return errors.New("engine termination unconfirmed")
	}
	next := filepath.Join(state, ".current-"+contract.UUID())
	if err := os.Symlink(dir, next); err != nil {
		return errors.New("pointer staging failed")
	}
	defer os.Remove(next)
	if err := os.Rename(next, filepath.Join(state, "current")); err != nil {
		return errors.New("pointer publication failed")
	}
	if err := syncDir(state); err != nil {
		return errors.New("pointer durability failed")
	}
	if _, err := command(ctx, "systemctl", "start", service); err != nil {
		return errors.New("published engine startup failed")
	}
	return nil
}

func readJSON(path string, value any) error {
	data, err := os.ReadFile(path)
	if err != nil || len(data) > 8<<20 {
		return errors.New("private JSON read failed")
	}
	return json.Unmarshal(data, value)
}

func writeNew(path string, data []byte) error {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err := file.Write(data); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	return syncDir(filepath.Dir(path))
}

func syncDir(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func command(ctx context.Context, name string, args ...string) (string, error) {
	limited, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	output, err := exec.CommandContext(limited, name, args...).Output()
	return strings.TrimSpace(string(output)), err
}
