package diagnostic

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestCategorizedPresets(t *testing.T) {
	groups := map[string][]Target{
		"china": {
			{ID: "baidu", URL: "https://www.baidu.com/favicon.ico", Category: "china"},
			{ID: "bilibili", URL: "https://www.bilibili.com/favicon.ico", Category: "china"},
			{ID: "wechat", URL: "https://www.wechat.com/favicon.ico", Category: "china"},
			{ID: "taobao", URL: "https://www.taobao.com/favicon.ico", Category: "china"},
			{ID: "zhihu", URL: "https://www.zhihu.com/favicon.ico", Category: "china"},
			{ID: "douyin", URL: "https://www.douyin.com/favicon.ico", Category: "china"},
			{ID: "um", URL: "https://www.um.edu.mo/favicon.ico", Category: "china"},
			{ID: "hku", URL: "https://www.hku.hk/favicon.ico", Category: "china"},
		},
		"global": {
			{ID: "github", URL: "https://github.com/favicon.ico", Category: "global"},
			{ID: "cloudflare", URL: "https://speed.cloudflare.com/favicon.ico", Category: "global"},
			{ID: "google", URL: "https://www.google.com/favicon.ico", Category: "global"},
			{ID: "youtube", URL: "https://www.youtube.com/favicon.ico", Category: "global"},
			{ID: "x", URL: "https://x.com/favicon.ico", Category: "global"},
			{ID: "tiktok", URL: "https://www.tiktok.com/favicon.ico", Category: "global"},
			{ID: "reddit", URL: "https://www.reddit.com/favicon.ico", Category: "global"},
			{ID: "telegram", URL: "https://telegram.org/favicon.ico", Category: "global"},
			{ID: "ieee", URL: "https://www.ieee.org/favicon.ico", Category: "global"},
		},
		"ai": {
			{ID: "chatgpt", URL: "https://chatgpt.com/", Category: "ai"},
			{ID: "claude", URL: "https://claude.ai/", Category: "ai"},
			{ID: "gemini", URL: "https://gemini.google.com/", Category: "ai"},
		},
	}
	groups["default"] = append(append([]Target{}, groups["china"]...), groups["global"]...)
	if len(groups["default"]) != 17 {
		t.Fatal("default fixture must contain 17 non-AI targets")
	}
	for name, want := range groups {
		t.Run(name, func(t *testing.T) {
			list, err := Preset(name)
			if err != nil || list.SchemaVersion != 1 || !reflect.DeepEqual(list.Targets, want) {
				t.Fatalf("preset = %+v, %v; want %+v", list, err, want)
			}
			data, err := json.Marshal(list)
			if err != nil {
				t.Fatal(err)
			}
			decoded, err := DecodeList(strings.NewReader(string(data)))
			if err != nil || !reflect.DeepEqual(list, decoded) {
				t.Fatalf("invalid preset round trip: %+v, %v", decoded, err)
			}
			list.Targets[0].Category = "custom"
			list.Targets[0].URL = "https://changed.test/"
			again, _ := Preset(name)
			if !reflect.DeepEqual(again.Targets, want) {
				t.Fatal("caller modified a shared preset")
			}
		})
	}
	if _, err := Preset("unknown"); err == nil {
		t.Fatal("accepted an unknown preset")
	}
}

func TestTargetCategoryPreservesLegacyListsAndExactURLs(t *testing.T) {
	for _, tc := range []struct {
		target Target
		want   string
	}{
		{Target{ID: "baidu", URL: "https://www.baidu.com/favicon.ico"}, "china"},
		{Target{ID: "old-id", URL: "https://www.google.com/favicon.ico"}, "global"},
		{Target{ID: "chatgpt", URL: "https://chatgpt.com/"}, "ai"},
		{Target{ID: "baidu", URL: "https://fixture.test/"}, "custom"},
		{Target{ID: "custom", URL: "https://www.baidu.com/other"}, "custom"},
		{Target{ID: "custom", URL: "https://www.baidu.com/favicon.ico?token=fixture"}, "custom"},
		{Target{ID: "custom", URL: "https://fixture.test/", Category: "global"}, "global"},
		{Target{ID: "baidu", URL: "https://www.baidu.com/favicon.ico", Category: "custom"}, "custom"},
	} {
		if got := TargetCategory(tc.target); got != tc.want {
			t.Errorf("%+v: category = %q, want %q", tc.target, got, tc.want)
		}
	}
	legacy := `{"schema_version":1,"targets":[{"id":"baidu","url":"https://www.baidu.com/favicon.ico"},{"id":"private","url":"https://fixture.test/private?token=fixture"}]}`
	list, err := DecodeList(strings.NewReader(legacy))
	if err != nil || len(list.Targets) != 2 {
		t.Fatal(list, err)
	}
	if list.Targets[0].Category != "" || TargetCategory(list.Targets[0]) != "china" || TargetCategory(list.Targets[1]) != "custom" {
		t.Fatal("legacy category inference changed saved metadata", list)
	}
}

func TestTargetCategoryValidationAndRoundTrip(t *testing.T) {
	for _, category := range []string{"", "china", "global", "ai", "custom", "unknown", "GLOBAL", "global\n", "\x1b[31m", strings.Repeat("x", 81)} {
		list := TargetList{SchemaVersion: 1, Targets: []Target{{ID: "fixture", URL: "https://fixture.test/private?token=fixture", Category: category}}}
		data, err := json.Marshal(list)
		if err != nil {
			t.Fatal(err)
		}
		decoded, err := DecodeList(strings.NewReader(string(data)))
		wantValid := category == "" || category == "china" || category == "global" || category == "ai" || category == "custom"
		if (err == nil) != wantValid {
			t.Fatalf("category %q: %v", category, err)
		}
		if wantValid && !reflect.DeepEqual(list, decoded) {
			t.Fatal("round trip lost category or URL", decoded)
		}
	}
}
