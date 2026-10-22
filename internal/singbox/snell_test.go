package singbox

import (
	"encoding/json"
	"testing"
)

func TestSnellIsAProxyOnlyDiagnosticTransport(t *testing.T) {
	base := `{"inbounds":[{"tag":"mixed"}],"outbounds":[{"type":"selector","tag":"proxy","outbounds":["snell"]},{"type":"snell","tag":"snell","version":6,"psk":"synthetic-shared-snell-psk","userkey":"synthetic-snell-alice"}],"experimental":{"clash_api":{}}}`
	patched, err := Patch([]byte(base), Local{ListenPort: 1080, SpeedPort: 1081})
	if err != nil {
		t.Fatal(err)
	}
	if port, safe := ProxyOnlyProbePort(patched); !safe || port != 1081 {
		t.Fatalf("Snell probe port=%d, safe=%v", port, safe)
	}
	var config map[string]any
	if err := json.Unmarshal(patched, &config); err != nil {
		t.Fatal(err)
	}
	outbound := config["outbounds"].([]any)[1].(map[string]any)
	if outbound["psk"] != "synthetic-shared-snell-psk" || outbound["userkey"] != "synthetic-snell-alice" || outbound["version"] != float64(6) {
		t.Fatal("patch changed the Snell credential or version")
	}
}
