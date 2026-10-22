package shell

import (
	"fmt"
	"net/url"
	"path/filepath"
	"strings"
)

// ProxyState supplies existing configuration, not an exported ownership marker.
// Capture paths are empty on platforms without Linux TUN.
type ProxyState struct {
	Known      map[string]string
	ControlDir string
	Active     string
	ConfigDir  string
	// BlockedRecord retains a native startup refusal until root metadata changes.
	// This covers inconsistent service state that a process-free hook cannot query.
	BlockedRecord string
}

// Values returns the exact name/value pairs sbc issues for Mixed mode.
func Values(port int, username, password string) map[string]string {
	address := fmt.Sprintf("127.0.0.1:%d", port)
	if username != "" {
		address = url.UserPassword(username, password).String() + "@" + address
	}
	values := make(map[string]string)
	for base, value := range map[string]string{
		"http_proxy": "http://" + address, "https_proxy": "http://" + address,
		"ftp_proxy": "http://" + address, "socks_proxy": "socks5://" + address,
		"no_proxy": NoProxy,
	} {
		values[base], values[strings.ToUpper(base)] = value, value
	}
	return values
}

// Residue returns variable names only. A generic bypass list alone proves no
// ownership. Diagnostics must never print the corresponding values.
func Residue(getenv func(string) string, known map[string]string) (owned, other []string) {
	matched := false
	for name, value := range known {
		if !strings.EqualFold(name, "no_proxy") && value != "" && getenv(name) == value {
			matched = true
		}
	}
	for _, base := range proxyVariables {
		for _, name := range []string{base, strings.ToUpper(base)} {
			value := getenv(name)
			if value == "" {
				continue
			}
			if expected := known[name]; expected != "" && value == expected && (!strings.EqualFold(name, "no_proxy") || matched) {
				owned = append(owned, name)
			} else {
				other = append(other, name)
			}
		}
	}
	return owned, other
}

func proxyNames() []string {
	var names []string
	for _, base := range proxyVariables {
		// sbc does not issue all_proxy. Never capture a foreign fallback proxy
		// merely because env.sh was sourced.
		if base == "all_proxy" {
			continue
		}
		names = append(names, base, strings.ToUpper(base))
	}
	return names
}

func proxyHelpers(shellName string, state ProxyState) string {
	var code strings.Builder
	names := []string{"_sbc_matched", "_sbc_record", "_sbc_extra", "_sbc_id", "_sbc_tail", "_sbc_date", "_sbc_committed", "_sbc_main", "_sbc_fraction", "_sbc_year", "_sbc_month", "_sbc_day", "_sbc_hour", "_sbc_minute", "_sbc_second", "_sbc_days", "_sbc_region_prev", "_sbc_region_tz", "_sbc_region_lang"}
	for _, name := range proxyNames() {
		names = append(names, "_sbc_seen_"+name, "_sbc_before_"+name)
	}
	code.WriteString("_sbc_private() {\n    ")
	if shellName == "bash" {
		code.WriteString("export -n ")
	} else {
		code.WriteString("typeset -g +x ")
	}
	code.WriteString(strings.Join(names, " ") + "\n}\n_sbc_capture_off() {\n")
	if state.ControlDir == "" {
		code.WriteString("    return 0\n")
	} else {
		// Accept only the canonical journal written by the root helper. Unknown
		// formats fail closed. read, case and parameter expansion are builtins.
		for _, dir := range []string{filepath.Dir(state.ControlDir), state.ControlDir, filepath.Dir(state.Active)} {
			q := Quote(dir)
			fmt.Fprintf(&code, "    if [ -e %s ] || [ -L %s ]; then\n        [ -d %s ] && [ -r %s ] && [ -x %s ] || return 1\n    fi\n", q, q, q, q, q)
		}
		for _, path := range []string{filepath.Join(state.ControlDir, "blocked"), state.Active} {
			q := Quote(path)
			fmt.Fprintf(&code, "    if [ -e %s ] || [ -L %s ]; then return 1; fi\n", q, q)
		}
		journal := Quote(filepath.Join(state.ControlDir, "transaction.json"))
		fmt.Fprintf(&code, "    if [ -e %s ] || [ -L %s ]; then\n        [ -f %s ] && [ -r %s ] || return 1\n", journal, journal, journal, journal)
		code.WriteString(`        { IFS= read -r _sbc_record && ! IFS= read -r _sbc_extra && [ -z "$_sbc_extra" ]; } < ` + journal + ` || return 1
`)
		if state.BlockedRecord != "" {
			fmt.Fprintf(&code, "        [ \"$_sbc_record\" != %s ] || return 1\n", Quote(state.BlockedRecord))
		}
		code.WriteString(`        _sbc_id=${_sbc_record#'{"id":"'}
        [ "$_sbc_id" != "$_sbc_record" ] || return 1
        _sbc_tail=${_sbc_id#*'","phase":"off","deadline":"'}
        [ "$_sbc_tail" != "$_sbc_id" ] || return 1
        _sbc_id=${_sbc_id%%'","phase":'*}
        case "$_sbc_id" in *[!0123456789abcdef]*) return 1 ;; esac
        [ -z "$_sbc_id" ] || [ "${#_sbc_id}" = 32 ] || return 1
        _sbc_committed=
        case "$_sbc_tail" in
        *'","committed":true}')
            _sbc_date=${_sbc_tail%'","committed":true}'}
            _sbc_committed=',"committed":true' ;;
        *'"}') _sbc_date=${_sbc_tail%'"}'} ;;
        *) return 1 ;;
        esac
        [ "$_sbc_record" = '{"id":"'"$_sbc_id"'","phase":"off","deadline":"'"$_sbc_date"'"'"$_sbc_committed"'}' ] || return 1
        _sbc_valid_deadline
        return $?
    fi
`)
		q := Quote(state.ConfigDir)
		fmt.Fprintf(&code, "    [ ! -e %s ] && [ ! -L %s ]\n", q, q)
	}
	code.WriteString(`}
_sbc_valid_deadline() {
    # Root writes UTC RFC3339Nano. Reject noncanonical or invalid timestamps.
    case "$_sbc_date" in *Z) _sbc_main=${_sbc_date%Z} ;; *) return 1 ;; esac
    case "$_sbc_main" in
    *.*)
        _sbc_fraction=${_sbc_main#*.}
        _sbc_main=${_sbc_main%%.*}
        case "$_sbc_fraction" in ''|*[!0123456789]*|*0) return 1 ;; esac
        [ "${#_sbc_fraction}" -le 9 ] || return 1 ;;
    esac
    case "$_sbc_main" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]) ;;
    *) return 1 ;;
    esac
    IFS='-T:' read -r _sbc_year _sbc_month _sbc_day _sbc_hour _sbc_minute _sbc_second <<_SBC_DATE
$_sbc_main
_SBC_DATE
    case "$_sbc_month" in
    01|03|05|07|08|10|12) _sbc_days=31 ;;
    04|06|09|11) _sbc_days=30 ;;
    02)
        _sbc_days=28
        if [ "$((10#$_sbc_year % 400))" -eq 0 ] ||
           { [ "$((10#$_sbc_year % 4))" -eq 0 ] && [ "$((10#$_sbc_year % 100))" -ne 0 ]; }; then
            _sbc_days=29
        fi ;;
    *) return 1 ;;
    esac
    [ "$((10#$_sbc_day))" -ge 1 ] && [ "$((10#$_sbc_day))" -le "$_sbc_days" ] &&
    [ "$((10#$_sbc_hour))" -le 23 ] && [ "$((10#$_sbc_minute))" -le 59 ] && [ "$((10#$_sbc_second))" -le 59 ]
}
_sbc_before_proxy() {
`)
	for _, name := range proxyNames() {
		fmt.Fprintf(&code, "    _sbc_before_%s=${%s-}\n", name, name)
	}
	code.WriteString("}\n_sbc_forget_proxy() {\n    unset")
	for _, name := range proxyNames() {
		fmt.Fprintf(&code, " _sbc_before_%s", name)
	}
	code.WriteString("\n}\n_sbc_remember_proxy() {\n")
	for _, name := range proxyNames() {
		// A successful but empty/partial file does not prove ownership of
		// unchanged foreign values. Retain earlier successful evidence.
		fmt.Fprintf(&code, "    if [ \"${%s-}\" != \"${_sbc_before_%s-}\" ] || { [ \"${_sbc_seen_%s+x}\" = x ] && [ \"${%s-}\" = \"$_sbc_seen_%s\" ]; }; then _sbc_seen_%s=${%s-}; fi\n", name, name, name, name, name, name, name)
	}
	code.WriteString("}\n_sbc_clear_proxy() {\n    _sbc_matched=\n")
	for _, name := range proxyNames() {
		if strings.EqualFold(name, "no_proxy") {
			continue
		}
		condition := fmt.Sprintf(`[ "${_sbc_seen_%s+x}" = x ] && [ "${%s-}" = "$_sbc_seen_%s" ]`, name, name, name)
		if value := state.Known[name]; value != "" {
			condition = "{ " + condition + "; } || [ \"${" + name + "-}\" = " + Quote(value) + " ]"
		}
		fmt.Fprintf(&code, "    if [ -n \"${%s-}\" ] && { %s; }; then unset %s; _sbc_matched=1; fi\n", name, condition, name)
	}
	for _, name := range []string{"no_proxy", "NO_PROXY"} {
		fmt.Fprintf(&code, "    if { [ \"${_sbc_seen_%s+x}\" = x ] && [ \"${%s-}\" = \"$_sbc_seen_%s\" ]; }", name, name, name)
		if value := state.Known[name]; value != "" {
			fmt.Fprintf(&code, " || { [ -n \"$_sbc_matched\" ] && [ \"${%s-}\" = %s ]; }", name, Quote(value))
		}
		fmt.Fprintf(&code, "; then unset %s; fi\n", name)
	}
	code.WriteString("    unset _sbc_matched _sbc_applied\n}\n")
	return code.String()
}
