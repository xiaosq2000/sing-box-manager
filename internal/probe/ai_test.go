package probe

import (
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestAIReportsOnlyObservedFacts(t *testing.T) {
	for _, entry := range []struct {
		provider, body, want string
		status               int
	}{
		{"gemini", `,2,1,200,"CHN"`, "page reachable, reported region CHN", 200},
		{"chatgpt", "ip=203.0.113.7\nloc=US\n", "edge country US, IP 203.0.113.7 (not service eligibility)", 200},
		{"chatgpt", "ip=not-an-ip\nloc=US\n", "page reachable, region unknown (not service eligibility)", 200},
		{"claude", "<html>Welcome</html>", "page reachable, region unknown (not service eligibility)", 200},
		{"claude", "Just a moment...", "challenge received, region unknown", 200},
		{"claude", "", "HTTP 403 (denied or challenged), region unknown", 403},
		{"claude", "", "HTTP 307 (redirect not followed), region unknown", 307},
	} {
		t.Run(entry.provider+entry.want, func(t *testing.T) {
			requests := 0
			port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
				requests++
				if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
					t.Error("provider received account credentials")
				}
				w.Header().Set("Location", "http://direct.test/login")
				w.Header().Set("Set-Cookie", "session=fixture")
				w.WriteHeader(entry.status)
				io.WriteString(w, entry.body)
			})
			got, err := AI(AIClient(port, "sbc", "fixture", time.Second), entry.provider, "http://provider.test/")
			if err != nil || got != entry.want || requests != 1 {
				t.Fatalf("got %q, %v, %d requests", got, err, requests)
			}
		})
	}
}

func TestAIStopsOnErrorsWithoutRetry(t *testing.T) {
	requests := 0
	port := proxyPort(t, func(w http.ResponseWriter, r *http.Request) {
		requests++
		w.WriteHeader(http.StatusTooManyRequests)
	})
	_, err := AI(AIClient(port, "", "", time.Second), "claude", "http://provider.test/")
	if err == nil || !strings.Contains(err.Error(), "429") || requests != 1 {
		t.Fatalf("got %v, %d requests", err, requests)
	}
}
