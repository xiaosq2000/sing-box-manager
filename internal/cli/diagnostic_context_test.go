package cli

import (
	"net/http"
	"sync/atomic"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/probe"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

func TestDiagnosticProtocolModeAndGenerationChangesInvalidateResults(t *testing.T) {
	for _, kind := range []string{"protocol", "mode", "generation"} {
		t.Run(kind, func(t *testing.T) {
			f := newRuntimeFixture(t)
			var capture atomic.Value
			capture.Store(f.capture)
			f.h.env.TunCapture = func() tun.CaptureState { return capture.Load().(tun.CaptureState) }
			f.state.Transaction, f.state.InboundTag = f.capture.Transaction, "fixture-tun"
			if err := tun.WriteState(f.h.layout, f.state); err != nil {
				t.Fatal(err)
			}
			site, roots := diagnosticSite(t, http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
				switch kind {
				case "protocol":
					f.api.lock.Lock()
					f.api.protocol = "trojan"
					f.api.lock.Unlock()
				case "mode":
					capture.Store(tun.CaptureState{Status: "off", Phase: "off"})
				case "generation":
					next := f.capture
					next.Transaction = "different-generation"
					capture.Store(next)
				}
				<-r.Context().Done()
			}))
			f.h.env.DiagnosticRoots = roots
			f.h.env.Sites = probe.Sites{IPify4: site.URL, IdentMe4: site.URL, IPDetails: site.URL}
			if code := Run(f.h.env, []string{"ip", "--json"}); code != 1 {
				t.Fatal(code)
			}
			report := reportFrom(t, f.h)
			if report.ContextValid || report.Interrupted || report.Issue != "runtime-changed" {
				t.Fatalf("%+v", report)
			}
			f.untouched(t)
		})
	}
}
