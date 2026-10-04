package install

import (
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

// UsageHeader is the response header in which the portal reports the user's
// traffic in the current billing cycle, in the form subscription clients read.
const UsageHeader = "subscription-userinfo"

// Usage is the traffic the portal counted for the user in this billing cycle.
type Usage struct {
	Upload   int64
	Download int64
}

// ParseUsage reads a header value such as "upload=1; download=2". It skips
// fields it does not know, and needs both counts.
func ParseUsage(value string) (Usage, bool) {
	var usage Usage
	var upload, download bool
	for _, field := range strings.Split(value, ";") {
		key, number, ok := strings.Cut(field, "=")
		if !ok {
			continue
		}
		count, err := strconv.ParseInt(strings.TrimSpace(number), 10, 64)
		if err != nil || count < 0 {
			continue
		}
		switch strings.TrimSpace(key) {
		case "upload":
			usage.Upload, upload = count, true
		case "download":
			usage.Download, download = count, true
		}
	}
	return usage, upload && download
}

// ReadUsage returns the count the last refresh brought, and when it came.
func ReadUsage(layout paths.Layout) (Usage, time.Time, bool) {
	info, err := os.Stat(layout.Usage())
	if err != nil {
		return Usage{}, time.Time{}, false
	}
	data, err := os.ReadFile(layout.Usage())
	if err != nil {
		return Usage{}, time.Time{}, false
	}
	usage, ok := ParseUsage(string(data))
	return usage, info.ModTime(), ok
}

// saveUsage keeps the portal's latest count for 'sbc status'. A portal that
// counts no traffic sends none, and then the old count goes. The count is a
// courtesy, so a failed write fails nothing.
func (i *Installer) saveUsage(value string) {
	if _, ok := ParseUsage(value); !ok {
		os.Remove(i.Layout.Usage())
		return
	}
	paths.WriteFile(i.Layout.Usage(), []byte(strings.TrimSpace(value)+"\n"), 0o600)
}
