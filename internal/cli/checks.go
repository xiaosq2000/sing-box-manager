package cli

import (
	"fmt"
	"os"
	"slices"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/probe"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

type ipOptions struct {
	ai            bool
	allowDirectAI bool
}

// runIP checks general exit IPs through the active route. AI checks are opt-in
// and use a validated proxy-only inlet unless direct access is permitted.
func runIP(env Env, options ipOptions) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	client, local, err := localAPI(layout)
	if err != nil {
		return fail(env, err)
	}
	route, _, err := client.Modes()
	if err != nil {
		return fail(env, err)
	}
	web := probe.Client(local.ListenPort, local.Username, local.Password, 10*time.Second)
	checks := []struct {
		line string
		ask  func() (string, error)
	}{
		{i18n.T("abroad:   %s\n"), func() (string, error) {
			place, err := probe.IPInfo(web, env.Sites.Abroad)
			return place.String(), err
		}},
		{i18n.T("china:    %s\n"), func() (string, error) {
			place, err := probe.CIP(web, env.Sites.China)
			return place.String(), err
		}},
	}
	fmt.Fprintf(env.Stdout, i18n.T("route:    %s\n"), route)
	if options.ai {
		port := local.ListenPort
		skipped := ""
		if options.allowDirectAI {
			fmt.Fprintln(env.Stdout, i18n.T("AI checks follow the active route. Direct network IP exposure is permitted."))
		} else if route == "direct" {
			skipped = i18n.T("skipped: direct route (use --ai --allow-direct-ai to permit exposure)")
		} else {
			config, err := os.ReadFile(layout.ConfigFile())
			var safe bool
			if err == nil {
				port, safe = singbox.ProxyOnlyProbePort(config)
			}
			if !safe {
				skipped = i18n.T("skipped: proxy-only inlet unavailable (run sbc update)")
			} else {
				fmt.Fprintln(env.Stdout, i18n.T("AI checks use the remote proxy, not the active route's destination rules."))
			}
		}
		aiWeb := probe.AIClient(port, local.Username, local.Password, 10*time.Second)
		for _, entry := range []struct{ provider, page, line string }{
			{"gemini", env.Sites.Gemini, i18n.T("gemini:   %s\n")},
			{"chatgpt", env.Sites.ChatGPT, i18n.T("chatgpt:  %s\n")},
			{"claude", env.Sites.Claude, i18n.T("claude:   %s\n")},
		} {
			checks = append(checks, struct {
				line string
				ask  func() (string, error)
			}{entry.line, func() (string, error) {
				if skipped != "" {
					return skipped, nil
				}
				return probe.AI(aiWeb, entry.provider, entry.page)
			}})
		}
	}
	answers := make([]string, len(checks))
	errs := make([]error, len(checks))
	var wait sync.WaitGroup
	for index, check := range checks {
		wait.Go(func() { answers[index], errs[index] = check.ask() })
	}
	wait.Wait()
	code := 0
	for index, check := range checks {
		answer := answers[index]
		if errs[index] != nil {
			answer, code = fmt.Sprintf(i18n.T("failed: %v"), errs[index]), 1
		}
		fmt.Fprintf(env.Stdout, check.line, answer)
	}
	return code
}

// speedTries is how often 'sbc speed' times each protocol. It reports the
// median, so one slow answer does not decide.
const speedTries = 3

type timing struct {
	protocol string
	delay    time.Duration
	// failures counts the tries that failed, and err is set when all did.
	failures int
	err      error
}

// runSpeed times a small page through the protocol in use with sing-box's
// delay test, or with --download, measures how fast it downloads. --all tests
// every protocol instead. Neither switches the protocol in use.
func runSpeed(env Env, options speedOptions) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	client, local, err := localAPI(layout)
	if err != nil {
		return fail(env, err)
	}
	current, protocols, err := client.Selected(api.ProxySelector)
	if err != nil {
		return fail(env, err)
	}
	tested := []string{current}
	if options.all {
		tested = protocols
	}
	if options.megabytes > 0 {
		return runDownloadTest(env, client, local, current, tested, options)
	}
	timings := make([]timing, len(tested))
	var wait sync.WaitGroup
	for index, protocol := range tested {
		wait.Go(func() { timings[index] = timeProtocol(client, protocol) })
	}
	wait.Wait()
	// The fastest first, and the protocols that failed last.
	sort.SliceStable(timings, func(a, b int) bool {
		if (timings[a].err == nil) != (timings[b].err == nil) {
			return timings[a].err == nil
		}
		return timings[a].delay < timings[b].delay
	})
	width := 0
	for _, timed := range timings {
		width = max(width, len(timed.protocol))
	}
	if options.all {
		fmt.Fprintf(env.Stdout, i18n.T("Time to load a small page through each protocol, the median of %d tries:\n"), speedTries)
	} else {
		fmt.Fprintf(env.Stdout, i18n.T("Time to load a small page through the protocol in use, the median of %d tries:\n"), speedTries)
	}
	for _, timed := range timings {
		result := i18n.T("failed")
		if timed.err == nil {
			result = fmt.Sprintf(i18n.T("%d ms"), timed.delay.Milliseconds())
		}
		if options.all && timed.protocol == current {
			result += i18n.T(" (current)")
		}
		if timed.err == nil && timed.failures > 0 {
			result += fmt.Sprintf(i18n.T(", %d of %d tries failed"), timed.failures, speedTries)
		}
		fmt.Fprintf(env.Stdout, "  %-*s  %s\n", width, timed.protocol, result)
	}
	if len(timings) == 0 || timings[0].err != nil {
		return fail(env, noPage(options, current))
	}
	return 0
}

// noPage says that no tested protocol loaded the page.
func noPage(options speedOptions, current string) error {
	if options.all {
		return i18n.New("no protocol loaded the page; check this network")
	}
	return i18n.Errorf("%s did not load the page; check this network, or test the other protocols with 'sbc speed --all'", current)
}

// defaultMegabytes is how much 'sbc speed --download' fetches through each
// protocol unless told otherwise, and maxMegabytes is the most Cloudflare
// sends at a time.
const (
	defaultMegabytes = 20
	maxMegabytes     = 99
)

// speedOptions are what 'sbc speed' was asked for.
type speedOptions struct {
	// all tests every protocol instead of the one in use, which costs more
	// for a download.
	all bool
	// megabytes, when above zero, asks for a download of that size instead
	// of the latency test.
	megabytes int
}

const (
	// downloadLimit ends each download, which then counts what arrived, and
	// connectAllowance is how long a protocol may take to start one.
	downloadLimit    = 15 * time.Second
	connectAllowance = 15 * time.Second
	// downloadGain is how much faster another protocol must download before
	// 'sbc speed --download' suggests it, since one download is noisy.
	downloadGain = 1.5
)

// runDownloadTest downloads through each tested protocol in turn, so they do
// not share the line, and prints each speed as it is known.
func runDownloadTest(env Env, client *api.Client, local singbox.Local, current string, tested []string, options speedOptions) int {
	if local.SpeedPort == 0 {
		return fail(env, i18n.New("this config has no speed test yet; 'sbc update' adds it"))
	}
	if _, _, err := client.Selected(singbox.SpeedTestTag); err != nil {
		return fail(env, i18n.New("sing-box runs a config without the speed test; 'sbc restart' loads the new one"))
	}
	page := env.Sites.Download + "?bytes=" + strconv.Itoa(options.megabytes*1_000_000)
	// The server receives each byte and sends it on, and both count.
	transfer := 2 * options.megabytes * len(tested)
	if options.all {
		fmt.Fprintf(env.Stdout, i18n.T("Downloading %d MB through each protocol, which adds up to %d MB to the server's monthly transfer:\n"), options.megabytes, transfer)
	} else {
		fmt.Fprintf(env.Stdout, i18n.T("Downloading %d MB through %s, the protocol in use, which adds up to %d MB to the server's monthly transfer:\n"), options.megabytes, current, transfer)
	}
	width := 0
	for _, protocol := range tested {
		width = max(width, len(protocol))
	}
	rates := map[string]float64{}
	for _, protocol := range tested {
		var result string
		if rate, err := downloadThrough(env, client, local, protocol, page); err != nil {
			result = fmt.Sprintf(i18n.T("failed: %v"), err)
		} else {
			rates[protocol] = rate
			result = fmt.Sprintf(i18n.T("%.1f Mbit/s"), rate)
		}
		if options.all && protocol == current {
			result += i18n.T(" (current)")
		}
		fmt.Fprintf(env.Stdout, "  %-*s  %s\n", width, protocol, result)
	}
	if len(rates) == 0 {
		return fail(env, noPage(options, current))
	}
	if !options.all {
		return 0
	}
	best := ""
	for _, protocol := range tested {
		if rate, ok := rates[protocol]; ok && (best == "" || rate > rates[best]) {
			best = protocol
		}
	}
	// A current protocol that failed has no rate, which any rate beats.
	if best != current && rates[best] >= rates[current]*downloadGain {
		fmt.Fprintf(env.Stdout, i18n.T("%s downloads faster; to switch: sbc protocol %s\n"), best, best)
	}
	return 0
}

// downloadThrough points the speed test's selector at protocol and times a
// download through the speed test's inbound. Each protocol gets a new client,
// so a connection through the one before cannot be reused.
func downloadThrough(env Env, client *api.Client, local singbox.Local, protocol, page string) (float64, error) {
	if err := client.Select(singbox.SpeedTestTag, protocol); err != nil {
		return 0, err
	}
	web := probe.Client(local.SpeedPort, local.Username, local.Password, downloadLimit+connectAllowance)
	defer web.CloseIdleConnections()
	received, took, err := env.TimeDownload(web, page, downloadLimit)
	if err != nil {
		return 0, err
	}
	return float64(received) * 8 / took.Seconds() / 1e6, nil
}

// timeProtocol makes every try, so one lost packet does not mark a protocol
// that works as failed.
func timeProtocol(client *api.Client, protocol string) timing {
	result := timing{protocol: protocol}
	var delays []time.Duration
	for range speedTries {
		delay, err := client.Delay(protocol, 5*time.Second)
		if err != nil {
			result.failures, result.err = result.failures+1, err
			continue
		}
		delays = append(delays, delay)
	}
	if len(delays) > 0 {
		slices.Sort(delays)
		result.delay, result.err = delays[(len(delays)-1)/2], nil
	}
	return result
}

// formatBytes writes a byte count in binary units with two decimals, as the
// portal's pages do.
func formatBytes(count int64) string {
	size, unit := float64(count), "B"
	for _, larger := range []string{"KiB", "MiB", "GiB", "TiB"} {
		if size < 1024 {
			break
		}
		size, unit = size/1024, larger
	}
	if unit == "B" {
		return fmt.Sprintf("%d B", count)
	}
	return fmt.Sprintf("%.2f %s", size, unit)
}
