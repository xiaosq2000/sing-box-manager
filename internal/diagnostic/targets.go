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
	ID  string `json:"id"`
	URL string `json:"url"`
}

type TargetList struct {
	SchemaVersion int      `json:"schema_version"`
	Targets       []Target `json:"targets"`
}

var presets = map[string][]Target{
	"default": {{"baidu", "https://www.baidu.com/favicon.ico"}, {"bilibili", "https://www.bilibili.com/favicon.ico"}, {"github", "https://github.com/favicon.ico"}, {"cloudflare", "https://speed.cloudflare.com/favicon.ico"}},
	"china":   {{"baidu", "https://www.baidu.com/favicon.ico"}, {"bilibili", "https://www.bilibili.com/favicon.ico"}},
	"global":  {{"github", "https://github.com/favicon.ico"}, {"cloudflare", "https://speed.cloudflare.com/favicon.ico"}, {"google", "https://www.google.com/favicon.ico"}},
	"ai":      {{"chatgpt", "https://chatgpt.com/"}, {"claude", "https://claude.ai/"}, {"gemini", "https://gemini.google.com/"}},
}

func Preset(name string) (TargetList, error) {
	targets, ok := presets[name]
	if !ok {
		return TargetList{}, errors.New("unknown-preset")
	}
	return TargetList{1, append([]Target{}, targets...)}, nil
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
		if _, err := ParseTarget(target.URL); err != nil || !idPattern.MatchString(target.ID) || ids[target.ID] || urls[target.URL] {
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
