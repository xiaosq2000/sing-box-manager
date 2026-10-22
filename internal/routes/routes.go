// Package routes defines the client-visible names of routing strategies.
package routes

import (
	"slices"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/tui"
)

// Canonical accepts the three legacy names without changing their meaning.
func Canonical(name string) string {
	switch name {
	case "ai":
		return "ai-only"
	case "gfw":
		return "gfw-proxy"
	case "china":
		return "china-direct"
	default:
		return name
	}
}

// Names hides aliases and removes duplicate choices. Unknown modes stay usable.
func Names(names []string) []string {
	result := make([]string, len(names))
	for index, name := range names {
		result[index] = Canonical(name)
	}
	slices.Sort(result)
	return slices.Compact(result)
}

// Description is shared by command help, route listings and completion.
func Description(name string) string {
	switch Canonical(name) {
	case "direct":
		return i18n.T("Direct all allowed traffic, including AI")
	case "ai-only":
		return i18n.T("Proxy listed AI destinations, direct the rest")
	case "gfw-proxy":
		return i18n.T("Proxy GFWList and AI destinations, direct the rest")
	case "china-direct":
		return i18n.T("Direct recognized China destinations, proxy the rest (AI and GFW first)")
	case "global":
		return i18n.T("Proxy traffic except LAN and configured direct exceptions")
	default:
		return ""
	}
}

// Help describes the built-in strategies without requiring an installation.
func Help() string {
	text := ""
	for _, name := range []string{"direct", "ai-only", "gfw-proxy", "china-direct", "global"} {
		text += "  " + tui.PadRight(name, 14) + Description(name) + "\n"
	}
	return text + i18n.T("Aliases: ai = ai-only, gfw = gfw-proxy, china = china-direct. Security restrictions remain active.")
}
