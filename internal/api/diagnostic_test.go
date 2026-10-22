package api

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestDiagnosticReadsAreAuthenticatedBoundedAndCancelable(t *testing.T) {
	for _, variant := range []string{"ok", "oversize", "error", "cancel"} {
		t.Run(variant, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "GET" || r.Header.Get("Authorization") != "Bearer fixture-diag-key" {
					t.Error("wrong authenticated read")
					w.WriteHeader(401)
					return
				}
				switch variant {
				case "oversize":
					fmt.Fprint(w, strings.Repeat(" ", (2<<20)+1))
				case "error":
					http.Error(w, "private-engine-error", 500)
				case "cancel":
					<-r.Context().Done()
				default:
					fmt.Fprint(w, `{"connections":[{"id":"fixture","metadata":{"type":"tun/fixture","host":"fixture.test"}}]}`)
				}
			}))
			defer server.Close()
			u, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(u.Port())
			deadline := time.Second
			if variant == "cancel" {
				deadline = 50 * time.Millisecond
			}
			ctx, cancel := context.WithTimeout(context.Background(), deadline)
			defer cancel()
			connections, err := New(port, "fixture-diag-key").ConnectionsContext(ctx)
			if variant == "ok" {
				if err != nil || len(connections) != 1 || connections[0].Metadata.Host != "fixture.test" {
					t.Fatal(connections, err)
				}
			} else if err == nil || strings.Contains(err.Error(), "private-engine-error") {
				t.Fatal("snapshot failed to bound or sanitize the response")
			}
		})
	}
}
