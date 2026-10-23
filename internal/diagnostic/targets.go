package diagnostic

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/netip"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"unicode"
)

const MaxTargets = 24
const MaxListBytes = 64 << 10

var idPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,47}$`)

type Target struct {
	ID       string `json:"id"`
	URL      string `json:"url"`
	Category string `json:"category,omitempty"`
}

type TargetList struct {
	SchemaVersion int      `json:"schema_version"`
	Targets       []Target `json:"targets"`
}

var presets = map[string][]Target{
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

func Preset(name string) (TargetList, error) {
	if name == "default" {
		targets := append([]Target{}, presets["china"]...)
		return TargetList{1, append(targets, presets["global"]...)}, nil
	}
	targets, ok := presets[name]
	if !ok {
		return TargetList{}, errors.New("unknown-preset")
	}
	return TargetList{1, append([]Target{}, targets...)}, nil
}

// TargetCategory recognizes exact built-in URLs in lists without category metadata.
// A category describes a website group, not its observed traffic path.
func TargetCategory(target Target) string {
	if target.Category != "" {
		return target.Category
	}
	for _, targets := range presets {
		for _, builtin := range targets {
			if target.URL == builtin.URL {
				return builtin.Category
			}
		}
	}
	return "custom"
}

func validCategory(category string) bool {
	switch category {
	case "", "china", "global", "ai", "custom":
		return true
	default:
		return false
	}
}

// ParseTarget validates syntax without local DNS resolution. Literal-local
// refusal does not imply protection against all DNS-based private destinations.
func ParseTarget(value string) (Target, error) {
	bad := func() (Target, error) { return Target{}, errors.New("invalid-target") }
	if len(value) > 4096 || strings.IndexFunc(value, func(r rune) bool { return unicode.IsControl(r) || unicode.Is(unicode.Cf, r) }) >= 0 {
		return bad()
	}
	u, err := url.Parse(value)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || u.Opaque != "" {
		return bad()
	}
	host := strings.ToLower(strings.TrimSuffix(u.Hostname(), "."))
	if host == "localhost" || strings.HasSuffix(host, ".localhost") || strings.Contains(host, "%") {
		return bad()
	}
	if port := u.Port(); port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 1 || n > 65535 {
			return bad()
		}
	}
	if address, err := netip.ParseAddr(host); err == nil {
		address = address.Unmap()
		if !address.IsGlobalUnicast() || address.IsPrivate() || netip.MustParsePrefix("100.64.0.0/10").Contains(address) {
			return bad()
		}
	}
	sum := sha256.Sum256([]byte(value))
	return Target{ID: "url-" + hex.EncodeToString(sum[:6]), URL: value}, nil
}

func ReadList(path string, missingDefault bool) (TargetList, error) {
	data, err := ReadRegular(path, MaxListBytes)
	if os.IsNotExist(err) && missingDefault {
		return Preset("default")
	}
	if err != nil {
		return TargetList{}, errors.New("list-read")
	}
	return DecodeList(strings.NewReader(string(data)))
}

func DecodeList(reader io.Reader) (TargetList, error) {
	data, err := io.ReadAll(io.LimitReader(reader, MaxListBytes+1))
	if err != nil || len(data) > MaxListBytes {
		return TargetList{}, errors.New("invalid-list")
	}
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.DisallowUnknownFields()
	var list TargetList
	if decoder.Decode(&list) != nil || decoder.Decode(new(any)) != io.EOF || list.SchemaVersion != 1 || list.Targets == nil || len(list.Targets) > MaxTargets {
		return TargetList{}, errors.New("invalid-list")
	}
	ids, urls := map[string]bool{}, map[string]bool{}
	for _, target := range list.Targets {
		if _, err := ParseTarget(target.URL); err != nil || !idPattern.MatchString(target.ID) || !validCategory(target.Category) || ids[target.ID] || urls[target.URL] {
			return TargetList{}, errors.New("invalid-list")
		}
		ids[target.ID], urls[target.URL] = true, true
	}
	return list, nil
}

// Host is intentionally lossy. Neither URL paths nor query values reach reports.
func Host(page string) string {
	u, err := url.Parse(page)
	if err != nil {
		return "unknown"
	}
	return SafeText(u.Hostname())
}

func SafeText(value string) string {
	value = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			return -1
		}
		return r
	}, value)
	runes := []rune(value)
	if len(runes) > 80 {
		runes = runes[:80]
	}
	return string(runes)
}
