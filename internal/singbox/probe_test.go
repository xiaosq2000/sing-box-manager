package singbox

import (
	"encoding/json"
	"testing"
)

func TestAIProxyInletValidationFailsClosed(t *testing.T) {
	base := `{"inbounds":[{"tag":"mixed"}],"outbounds":[{"type":"selector","tag":"proxy","outbounds":["trojan"]},{"type":"trojan","tag":"trojan"}],"experimental":{"clash_api":{}}}`
	data, err := Patch([]byte(base), Local{ListenPort: 1080, SpeedPort: 1081})
	if err != nil {
		t.Fatal(err)
	}
	if port, safe := ProxyOnlyProbePort(data); !safe || port != 1081 {
		t.Fatalf("port %d, safe %v", port, safe)
	}
	for _, mutate := range []func(map[string]any){
		func(root map[string]any) { root["route"] = nil },
		func(root map[string]any) {
			root["route"].(map[string]any)["rules"].([]any)[0].(map[string]any)["clash_mode"] = "global"
		},
		func(root map[string]any) { root["outbounds"].([]any)[1].(map[string]any)["type"] = "direct" },
		func(root map[string]any) { root["inbounds"].([]any)[1].(map[string]any)["listen"] = "0.0.0.0" },
		func(root map[string]any) {
			root["route"].(map[string]any)["rules"].([]any)[0].(map[string]any)["outbound"] = "direct"
		},
	} {
		var root map[string]any
		json.Unmarshal(data, &root)
		mutate(root)
		invalid, _ := json.Marshal(root)
		if _, safe := ProxyOnlyProbePort(invalid); safe {
			t.Fatalf("unsafe config accepted: %s", invalid)
		}
	}
	if _, safe := ProxyOnlyProbePort([]byte("{}")); safe {
		t.Fatal("empty config accepted")
	}
}
