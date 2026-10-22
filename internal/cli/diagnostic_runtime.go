package cli

import (
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/json"
	"errors"
	"net"
	"net/url"
	"strconv"
	"sync"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/diagnostic"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

type diagnosticRuntime struct {
	mu                                                 sync.Mutex
	env                                                Env
	client                                             *api.Client
	mode, route, protocol, generation, policy, inbound string
	file                                               string
	digest                                             [32]byte
	valid                                              bool
	requester                                          diagnostic.Requester
	observer                                           *diagnostic.Observer
}

func readDiagnosticFile(path string) ([]byte, error) {
	return diagnostic.ReadRegular(path, 8<<20)
}

func openDiagnostic(ctx context.Context, env Env) (*diagnosticRuntime, error) {
	capture := tunCapture(env)
	if !captureOff(capture) && !activeTunGeneration(capture) {
		return nil, errors.New("unsafe-runtime")
	}
	layout, err := env.Layout()
	if err != nil {
		return nil, errors.New("unavailable-runtime")
	}
	r := &diagnosticRuntime{env: env, valid: true}
	r.requester.Roots = env.DiagnosticRoots
	if r.requester.Roots == nil {
		r.requester.Roots, err = x509.SystemCertPool()
		if err != nil {
			return nil, errors.New("unavailable-runtime")
		}
	}
	r.file = layout.ConfigFile()
	if !captureOff(capture) {
		r.file = layout.TunState()
	}
	data, err := readDiagnosticFile(r.file)
	if err != nil {
		return nil, errors.New("unavailable-runtime")
	}
	r.digest = sha256.Sum256(data)
	if captureOff(capture) {
		local, err := singbox.ReadLocal(data)
		if err != nil || local.ListenPort < 1 || local.ListenPort > 65535 || local.APIPort < 1 || local.APIPort > 65535 || local.APISecret == "" {
			return nil, errors.New("unavailable-runtime")
		}
		r.client, r.mode = api.New(local.APIPort, local.APISecret), "mixed"
		var config struct {
			Inbounds []struct {
				Type, Tag, Listen string
				ListenPort        int `json:"listen_port"`
			}
		}
		if json.Unmarshal(data, &config) != nil {
			return nil, errors.New("unavailable-runtime")
		}
		for _, inlet := range config.Inbounds {
			if inlet.Type == "mixed" && inlet.ListenPort == local.ListenPort && inlet.Listen == "127.0.0.1" {
				r.inbound = "mixed/" + inlet.Tag
			}
		}
		if r.inbound == "" {
			return nil, errors.New("unavailable-runtime")
		}
		r.requester.Proxy = &url.URL{Scheme: "http", Host: net.JoinHostPort("127.0.0.1", strconv.Itoa(local.ListenPort))}
		if local.Username != "" {
			r.requester.Proxy.User = url.UserPassword(local.Username, local.Password)
		}
	} else {
		var state tun.State
		if json.Unmarshal(data, &state) != nil || state.APIPort < 1 || state.APIPort > 65535 || state.APISecret == "" || state.Interface != tun.InterfaceName {
			return nil, errors.New("unsafe-runtime")
		}
		if state.Transaction != capture.Transaction || state.InboundTag == "" || len(state.InboundTag) > 128 {
			return nil, errors.New("missing-diagnostic-state")
		}
		r.client, r.mode = api.New(state.APIPort, state.APISecret), "tun"
		r.generation, r.policy, r.inbound = capture.Transaction, state.IPv6Policy, "tun/"+state.InboundTag
	}
	probe, stop := context.WithTimeout(ctx, time.Second)
	snapshot, err := r.client.RuntimeContext(probe)
	stop()
	if err != nil {
		return nil, errors.New("unavailable-runtime")
	}
	r.route, r.protocol = snapshot.Route, snapshot.Protocol
	r.observer, err = diagnostic.NewObserver(ctx, r.inbound, snapshot.Protocols, r.client.ConnectionsContext)
	if err != nil {
		return nil, errors.New("unavailable-runtime")
	}
	r.requester.Observer = r.observer
	if !r.check(ctx) {
		r.observer.Stop()
		return nil, errors.New("runtime-changed")
	}
	return r, nil
}

func (r *diagnosticRuntime) check(ctx context.Context) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if ctx.Err() != nil || !r.valid {
		return false
	}
	capture := tunCapture(r.env)
	if (r.mode == "mixed" && !captureOff(capture)) || (r.mode == "tun" && (!activeTunGeneration(capture) || capture.Transaction != r.generation)) {
		r.valid = false
		return false
	}
	data, err := readDiagnosticFile(r.file)
	if err != nil || sha256.Sum256(data) != r.digest {
		r.valid = false
		return false
	}
	probe, stop := context.WithTimeout(ctx, time.Second)
	defer stop()
	snapshot, err := r.client.RuntimeContext(probe)
	if ctx.Err() != nil {
		return false
	}
	if err != nil || snapshot.Route != r.route || snapshot.Protocol != r.protocol {
		r.valid = false
	}
	return r.valid
}

func (r *diagnosticRuntime) isValid() bool { r.mu.Lock(); defer r.mu.Unlock(); return r.valid }
