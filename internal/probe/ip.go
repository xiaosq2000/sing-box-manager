package probe

import (
	"encoding/json"
	"io"
	"net/http"
	"net/netip"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync"
	"unicode"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// IPDetailsSource describes an observed IP, not the request's own exit.
const IPDetailsSource = "ipwho.is"

const maxIPPage = 64 << 10

// IPDetails holds database estimates. Empty fields are unknown.
type IPDetails struct {
	Country      string
	Region       string
	City         string
	ASN          uint32
	Organization string
}

// Location lists each available geographic name once, from country to city.
func (d IPDetails) Location() string {
	var parts []string
	for _, part := range []string{d.Country, d.Region, d.City} {
		if part != "" && !slices.Contains(parts, part) {
			parts = append(parts, part)
		}
	}
	return strings.Join(parts, " / ")
}

// IPResult keeps each source's observation, including partial failures.
type IPResult struct {
	Source     string
	Family     int
	IP         string
	Err        error
	Details    IPDetails
	DetailsErr error
}

// CheckIPs asks both sources in parallel. It then looks up each distinct IP
// once, without replacing observations or changing their source labels.
func CheckIPs(client *http.Client, sites Sites, dualStack bool) []IPResult {
	type check struct {
		source string
		family int
		page   string
	}
	checks := []check{
		{"IPify", 4, sites.IPify4},
		{"ident.me", 4, sites.IdentMe4},
	}
	if dualStack {
		checks = append(checks, check{"IPify", 6, sites.IPify6}, check{"ident.me", 6, sites.IdentMe6})
	}
	results := make([]IPResult, len(checks))
	var wait sync.WaitGroup
	for index, check := range checks {
		wait.Go(func() {
			result := &results[index]
			result.Source, result.Family = check.source, check.family
			result.IP, result.Err = ExitIP(client, check.page, check.family)
		})
	}
	wait.Wait()

	type lookup struct {
		details IPDetails
		err     error
	}
	lookups := map[string]*lookup{}
	for _, result := range results {
		if result.Err != nil || lookups[result.IP] != nil {
			continue
		}
		found := &lookup{}
		lookups[result.IP] = found
		wait.Go(func() { found.details, found.err = LookupIP(client, sites.IPDetails, result.IP) })
	}
	wait.Wait()
	for index := range results {
		result := &results[index]
		if found := lookups[result.IP]; found != nil {
			result.Details, result.DetailsErr = found.details, found.err
		}
	}
	return results
}

// ExitIP reads a plain-text address from a family-specific endpoint. Dialing
// loopback with tcp4 or tcp6 would not control an HTTP proxy's exit family.
func ExitIP(client *http.Client, page string, family int) (string, error) {
	body, err := ipPage(client, page)
	if err != nil {
		return "", err
	}
	ip, err := netip.ParseAddr(strings.TrimSpace(string(body)))
	if err != nil || ip.Zone() != "" {
		return "", i18n.New("the site gave no valid IP address")
	}
	ip = ip.Unmap()
	if !ip.IsGlobalUnicast() || ip.IsPrivate() {
		return "", i18n.New("the site gave no valid IP address")
	}
	got := 6
	if ip.Is4() {
		got = 4
	}
	if got != family {
		return "", i18n.Errorf("the site returned IPv%d, expected IPv%d", got, family)
	}
	return ip.String(), nil
}

// LookupIP requests details for the observed IP. It rejects answers for a
// different address, including a database that returns its caller's own IP.
func LookupIP(client *http.Client, base, address string) (IPDetails, error) {
	ip, err := netip.ParseAddr(address)
	if err != nil || ip.Zone() != "" {
		return IPDetails{}, i18n.New("cannot look up an invalid IP address")
	}
	page, err := url.JoinPath(base, ip.String())
	if err != nil {
		return IPDetails{}, err
	}
	body, err := ipPage(client, page)
	if err != nil {
		return IPDetails{}, err
	}
	var data struct {
		IP          string `json:"ip"`
		Success     *bool  `json:"success"`
		Country     string `json:"country"`
		CountryCode string `json:"country_code"`
		Region      string `json:"region"`
		City        string `json:"city"`
		Connection  struct {
			ASN uint32 `json:"asn"`
			Org string `json:"org"`
			ISP string `json:"isp"`
		} `json:"connection"`
	}
	if json.Unmarshal(body, &data) != nil || data.Success == nil {
		return IPDetails{}, i18n.New("the IP database returned an invalid response")
	}
	if !*data.Success {
		return IPDetails{}, i18n.New("the IP database rejected the lookup")
	}
	answer, err := netip.ParseAddr(data.IP)
	if err != nil || answer.Zone() != "" {
		return IPDetails{}, i18n.New("the IP database returned an invalid response")
	}
	if answer.Unmap() != ip.Unmap() {
		return IPDetails{}, i18n.New("the IP database returned a different address")
	}
	country := detailText(data.Country)
	if country == "" {
		country = detailText(data.CountryCode)
	}
	org := detailText(data.Connection.Org)
	if org == "" {
		org = detailText(data.Connection.ISP)
	}
	return IPDetails{
		Country: country, Region: detailText(data.Region), City: detailText(data.City),
		ASN: data.Connection.ASN, Organization: org,
	}, nil
}

// detailText bounds and removes terminal controls from untrusted names.
func detailText(value string) string {
	value = strings.Join(strings.FieldsFunc(value, func(r rune) bool {
		return unicode.IsSpace(r) || unicode.IsControl(r) || unicode.Is(unicode.Cf, r)
	}), " ")
	runes := []rune(value)
	if len(runes) > 80 {
		return string(runes[:80]) + "…"
	}
	return value
}

func ipPage(client *http.Client, page string) ([]byte, error) {
	request, err := http.NewRequest(http.MethodGet, page, nil)
	if err != nil {
		return nil, err
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, explain(err, client.Timeout)
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusTooManyRequests {
		return nil, i18n.New("rate limited (HTTP 429)")
	}
	if response.StatusCode >= 300 && response.StatusCode < 400 {
		return nil, i18n.Errorf("HTTP %d (redirect not followed)", response.StatusCode)
	}
	if response.StatusCode != http.StatusOK {
		// Do not print the server's untrusted HTTP reason phrase.
		status := strings.TrimSpace(strconv.Itoa(response.StatusCode) + " " + http.StatusText(response.StatusCode))
		return nil, i18n.Errorf("the site answered %s", status)
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, maxIPPage+1))
	if err != nil {
		return nil, explain(err, client.Timeout)
	}
	if len(body) > maxIPPage {
		return nil, i18n.New("the IP response exceeds 64 KiB")
	}
	return body, nil
}
