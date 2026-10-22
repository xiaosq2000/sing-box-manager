//go:build !windows

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/probe"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

func TestIPDiagnosticSignalHelper(t *testing.T) {
	if os.Getenv("SBC_DIAG_SIGNAL_HELPER") != "1" {
		return
	}
	env := DefaultEnv("fixture", strings.NewReader(""), os.Stdout, os.Stderr)
	env.Layout = func() (paths.Layout, error) {
		return paths.Layout{Config: os.Getenv("SBC_DIAG_CONFIG"), Data: os.Getenv("SBC_DIAG_DATA")}, nil
	}
	env.TunCapture = func() tun.CaptureState { return tun.CaptureState{Status: "off", Phase: "off"} }
	if err := json.Unmarshal([]byte(os.Getenv("SBC_DIAG_SITES")), &env.Sites); err != nil {
		os.Exit(99)
	}
	os.Exit(Run(env, []string{"ip", "--json"}))
}

func TestRealSignalsRetainCompletedIPObservations(t *testing.T) {
	for _, item := range []struct {
		signal os.Signal
		code   int
	}{{os.Interrupt, 130}, {syscall.SIGTERM, 143}} {
		t.Run(fmt.Sprint(item.code), func(t *testing.T) {
			h := newHarness(t)
			blocked := make(chan struct{}, 1)
			proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Host == "first.test" {
					fmt.Fprint(w, "203.0.113.7")
					return
				}
				if r.URL.Host == "second.test" {
					select {
					case blocked <- struct{}{}:
					default:
					}
					<-r.Context().Done()
					return
				}
				t.Error("optional lookup started before core requests completed")
				w.WriteHeader(500)
			}))
			defer proxy.Close()
			u, _ := url.Parse(proxy.URL)
			port, _ := strconv.Atoi(u.Port())
			h.writeLocal(t, func(local *singbox.Local) { local.ListenPort = port })
			sites, _ := json.Marshal(probe.Sites{IPify4: "http://first.test/", IdentMe4: "http://second.test/", IPDetails: "http://details.test/"})
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestIPDiagnosticSignalHelper$")
			cmd.Env = append(os.Environ(), "SBC_DIAG_SIGNAL_HELPER=1", "SBC_DIAG_CONFIG="+h.layout.Config, "SBC_DIAG_DATA="+h.layout.Data, "SBC_DIAG_SITES="+string(sites))
			var out, errOut bytes.Buffer
			cmd.Stdout = &out
			cmd.Stderr = &errOut
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			select {
			case <-blocked:
			case <-ctx.Done():
				t.Fatal("child never reached the blocked source")
			}
			time.Sleep(100 * time.Millisecond)
			if err := cmd.Process.Signal(item.signal); err != nil {
				t.Fatal(err)
			}
			err := cmd.Wait()
			if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != item.code {
				t.Fatalf("exit %v: %s %s", err, &out, &errOut)
			}
			var report diagnosticReport
			if json.Unmarshal(out.Bytes(), &report) != nil || !report.Interrupted || len(report.Observations) != 2 || report.Observations[0].IP != "203.0.113.7" || report.Observations[0].IPValidation != "valid" {
				t.Fatalf("lost completed IP: %s %s", &out, &errOut)
			}
		})
	}
}
