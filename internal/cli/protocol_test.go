package cli

import (
	"strings"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

func TestProtocolChoicesDescribeManagedProtocols(t *testing.T) {
	for _, chinese := range []bool{false, true} {
		t.Run(map[bool]string{false: "en", true: "zh"}[chinese], func(t *testing.T) {
			i18n.SetChinese(chinese)
			defer i18n.SetChinese(false)
			h := newHarness(t)
			h.api.protocol = "snell"
			h.api.protocols = []string{"snell", "trojan", "hysteria2", "naive", "custom-tag"}
			if code := Run(h.env, []string{"protocol"}); code != 0 {
				t.Fatalf("code %d: %s", code, h.errOut)
			}
			text := h.out.String()
			for _, name := range h.api.protocols[:4] {
				description := protocolDescription(name)
				if description == "" || !strings.Contains(text, description) {
					t.Fatalf("missing %s description: %s", name, text)
				}
			}
			if chinese && !strings.Contains(text, "流量规律") {
				t.Fatalf("protocol descriptions did not use Chinese: %s", text)
			}
			if !strings.Contains(text, "snell "+i18n.T("(current)")) || !strings.Contains(text, "custom-tag") {
				t.Fatalf("missing current or custom option: %s", text)
			}
			if h.api.protocol != "snell" || len(h.svc.calls) != 0 {
				t.Fatal("listing changed the runtime")
			}
		})
	}
}

func TestProtocolDescriptionsMatchUserGuidance(t *testing.T) {
	defer i18n.SetChinese(false)
	for _, tc := range []struct {
		name, chinese string
		english       []string
	}{
		{"snell", "侧重减少容易识别的流量规律，但会额外消耗一些流量。", []string{"recognizable traffic patterns", "extra traffic"}},
		{"trojan", "存在被 GFW 识别和封锁的风险，本项目不推荐使用。", []string{"GFW may detect and block", "this project does not recommend"}},
		{"hysteria2", "侧重网络波动时的传输速度，但可能更耗流量，若所在网络屏蔽或限速 UDP，可能无法连接或速度受限。", []string{"unstable networks", "may use more data", "fail to connect", "blocks UDP", "throttles UDP"}},
		{"naive", "侧重模仿真实浏览器以降低识别风险，可能需要牺牲部分速度。", []string{"real browser connections", "reduce detection risk", "possibly at the cost of some speed"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			i18n.SetChinese(true)
			if got := protocolDescription(tc.name); got != tc.chinese {
				t.Fatalf("Chinese guidance = %q, want %q", got, tc.chinese)
			}
			i18n.SetChinese(false)
			got := protocolDescription(tc.name)
			for _, want := range tc.english {
				if !strings.Contains(got, want) {
					t.Errorf("English guidance %q omits %q", got, want)
				}
			}
		})
	}
}

func TestProtocolDescriptionDoesNotGuessCustomTags(t *testing.T) {
	for _, name := range []string{"", "custom-tag", "trojan-backup", "Snell", "naive\x1b[2J"} {
		if got := protocolDescription(name); got != "" {
			t.Errorf("description for unknown tag %q: %q", name, got)
		}
	}
}
