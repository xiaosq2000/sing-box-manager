package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"os/signal"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/diagnostic"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/probe"
	"github.com/xiaosq2000/sing-box-manager/internal/tui"
)

type diagnosticOptions struct {
	json, dualStack    bool
	samples, megabytes int
	preset             string
}

type diagnosticObservation struct {
	ID           string              `json:"id"`
	Host         string              `json:"host"`
	Category     string              `json:"category,omitempty"`
	Source       string              `json:"source,omitempty"`
	Family       int                 `json:"family,omitempty"`
	Policy       string              `json:"policy,omitempty"`
	IP           string              `json:"ip,omitempty"`
	IPValidation string              `json:"ip_validation,omitempty"`
	Details      *probe.IPDetails    `json:"details,omitempty"`
	Samples      []diagnostic.Sample `json:"samples"`
}

type diagnosticReport struct {
	SchemaVersion int                     `json:"schema_version"`
	Command       string                  `json:"command"`
	Scope         string                  `json:"scope"`
	Mode          *string                 `json:"mode"`
	Route         *string                 `json:"route"`
	Protocol      *string                 `json:"protocol"`
	ContextValid  bool                    `json:"context_valid"`
	Interrupted   bool                    `json:"interrupted"`
	Observations  []diagnosticObservation `json:"observations"`
	Warnings      []string                `json:"warnings"`
	Issue         string                  `json:"issue,omitempty"`
}

func runDiagnostic(env Env, command string, targets []diagnostic.Target, options diagnosticOptions) int {
	report := diagnosticReport{SchemaVersion: 1, Command: command, Scope: "sbc", Observations: []diagnosticObservation{}, Warnings: []string{}}
	parent := env.Context
	if parent == nil {
		parent = context.Background()
	}
	ctx, cancel := context.WithTimeout(parent, 60*time.Second)
	defer cancel()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	var interrupted atomic.Int32
	watchSignals := make(chan struct{})
	go func() {
		defer close(watchSignals)
		select {
		case sig := <-signals:
			code := 130
			if sig == syscall.SIGTERM {
				code = 143
			}
			interrupted.Store(int32(code))
			cancel()
		case <-ctx.Done():
		}
	}()
	defer func() { cancel(); <-watchSignals }()
	runtime, err := openDiagnostic(ctx, env)
	if err != nil {
		report.Issue = err.Error()
		if interrupted.Load() != 0 || parent.Err() == context.Canceled {
			report.Interrupted = true
			code := int(interrupted.Load())
			if code == 0 {
				code = 130
			}
			return renderDiagnostic(env, report, options.json, code)
		}
		return renderDiagnostic(env, report, options.json, 1)
	}
	report.Mode, report.Route, report.Protocol = &runtime.mode, &runtime.route, &runtime.protocol
	var entries []diagnostic.Target
	if command == "ip" {
		entries = []diagnostic.Target{{ID: "ipify4", URL: env.Sites.IPify4}, {ID: "ident4", URL: env.Sites.IdentMe4}}
		if options.dualStack {
			entries = append(entries, diagnostic.Target{ID: "ipify6", URL: env.Sites.IPify6}, diagnostic.Target{ID: "ident6", URL: env.Sites.IdentMe6})
		}
	} else if command == "speed.download" {
		page, parseErr := url.Parse(env.Sites.Download)
		if parseErr != nil {
			runtime.observer.Stop()
			report.Issue = "invalid-target"
			return renderDiagnostic(env, report, options.json, 2)
		}
		query := page.Query()
		query.Set("bytes", strconv.Itoa(options.megabytes*1_000_000))
		page.RawQuery = query.Encode()
		entries = []diagnostic.Target{{ID: "download", URL: page.String()}}
		fmt.Fprintf(env.Stderr, i18n.T("Download budget: at most %d MB of payload. Network and proxy overhead add traffic.\n"), options.megabytes)
	} else {
		entries = targets
	}
	if command == "check" && options.preset == "ai" {
		report.Warnings = append(report.Warnings, "direct-exposure")
	}
	// The same notice applies to arbitrary explicit URLs, not only known AI names.
	if command == "check" {
		report.Warnings = append(report.Warnings, "active-rules")
	}
	samples := options.samples
	if samples == 0 {
		samples = 1
	}
	for _, entry := range entries {
		row := diagnosticObservation{ID: entry.ID, Host: diagnostic.Host(entry.URL), Samples: []diagnostic.Sample{}}
		if command == "check" {
			row.Category = diagnostic.TargetCategory(entry)
		}
		if command == "ip" {
			row.IPValidation = "not-tested"
			row.Family = 4
			if strings.HasSuffix(entry.ID, "6") {
				row.Family = 6
			}
			row.Source = "ident.me"
			if strings.HasPrefix(entry.ID, "ipify") {
				row.Source = "IPify"
			}
			if row.Family == 6 && runtime.mode == "tun" && runtime.policy == "public-ipv6-disabled-v1" {
				row.Policy = "policy-skipped-not-verified"
				row.IPValidation = "policy-skipped"
			}
		}
		for range samples {
			row.Samples = append(row.Samples, diagnostic.Sample{Transport: "not-tested", HTTPOutcome: "not-tested", Issue: "not-started", Capture: diagnostic.Evidence{Path: "unknown"}, Socket: -1})
		}
		report.Observations = append(report.Observations, row)
	}
	watchDone := make(chan struct{})
	go func() {
		defer close(watchDone)
		ticker := time.NewTicker(200 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if !runtime.check(ctx) {
					cancel()
					return
				}
			}
		}
	}()
	var progressMu sync.Mutex
	var liveProg *tui.Program
	var liveDone chan struct{}
	var liveErr error
	sendProgress := func() {
		if liveProg == nil {
			return
		}
		progressMu.Lock()
		rows, pending := buildDiagnosticTableRows(report.Observations)
		progressMu.Unlock()
		liveProg.Send(tui.LiveTableUpdateMsg{Rows: rows, Pending: pending})
	}
	if !options.json && env.output().Interactive() {
		out := env.output()
		th := out.Theme
		headerLine := ""
		if report.Mode != nil {
			headerLine = strings.TrimSuffix(fmt.Sprintf(i18n.T("scope: sbc; mode: %s; route: %s; selected protocol: %s\n"), *report.Mode, diagnostic.SafeText(*report.Route), diagnostic.SafeText(*report.Protocol)), "\n")
		}
		initialRows, initialPending := buildDiagnosticTableRows(report.Observations)
		liveModel := tui.NewLiveTableModel(th, headerLine, diagnosticHeaders(command), initialRows, initialPending, i18n.T("testing..."), func() {
			interrupted.CompareAndSwap(0, 130)
			cancel()
		})
		liveModel.Width, liveModel.Height = out.Size()
		liveModel.HelpText = i18n.T("↑/↓ scroll • ctrl+c cancel")
		liveProg, liveErr = out.Program(liveModel)
		if liveErr != nil {
			cancel()
		} else {
			liveDone = make(chan struct{})
			go func() {
				defer close(liveDone)
				_, liveErr = liveProg.Run()
				if liveErr != nil {
					cancel()
				}
			}()
		}
	}
	var workers sync.WaitGroup
	jobs := make(chan int)
	for range min(4, len(entries)) {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for index := range jobs {
				row := &report.Observations[index]
				if row.Policy != "" {
					continue
				}
				for attempt := range samples {
					if !runtime.check(ctx) {
						cancel()
						break
					}
					limit := int64(8 << 10)
					if command == "ip" {
						limit = 64 << 10
					}
					if command == "speed.download" {
						limit = int64(options.megabytes) * 1_000_000
					}
					sample := runtime.requester.Fetch(ctx, entries[index].URL, limit, command == "speed.download")
					progressMu.Lock()
					row.Samples[attempt] = sample
					progressMu.Unlock()
					sendProgress()
					if !runtime.check(ctx) {
						cancel()
						break
					}
				}
			}
		}()
	}
	for index := range entries {
		jobs <- index
	}
	close(jobs)
	workers.Wait()
	// Parse every completed source independently of cancellation or optional lookups.
	// Raw response bodies do not enter JSON, so parsing cannot wait for detail I/O.
	if command == "ip" {
		for index := range report.Observations {
			row := &report.Observations[index]
			if row.Policy != "" || len(row.Samples) == 0 {
				continue
			}
			sample := &row.Samples[0]
			if sample.HTTPStatus == nil || *sample.HTTPStatus != 200 || sample.Issue != "" {
				continue
			}
			row.IP, err = probe.ParseExitIP(sample.Body, row.Family)
			if err != nil {
				row.IPValidation = "failed"
				sample.Issue = "invalid-ip"
			} else {
				row.IPValidation = "valid"
			}
		}
		sendProgress()
	}
	// Optional lookups never prevent retaining an already observed address.
	if command == "ip" && ctx.Err() == nil {
		details := map[string]*probe.IPDetails{}
		for index := range report.Observations {
			row := &report.Observations[index]
			if row.Policy != "" {
				continue
			}
			if row.IPValidation != "valid" {
				continue
			}
			if found, ok := details[row.IP]; ok {
				row.Details = found
				continue
			}
			if !runtime.check(ctx) {
				cancel()
				break
			}
			page, joinErr := url.JoinPath(env.Sites.IPDetails, row.IP)
			if joinErr != nil {
				report.Warnings = append(report.Warnings, "details-unavailable")
				details[row.IP] = nil
				continue
			}
			lookup := runtime.requester.Fetch(ctx, page, 64<<10, false)
			var value probe.IPDetails
			if lookup.HTTPStatus != nil && *lookup.HTTPStatus == 200 && lookup.Issue == "" {
				value, err = probe.ParseIPDetails(lookup.Body, row.IP)
			} else {
				err = fmt.Errorf("details-unavailable")
			}
			if err != nil {
				report.Warnings = append(report.Warnings, "details-unavailable")
				details[row.IP] = nil
			} else {
				details[row.IP] = &value
				row.Details = &value
			}
		}
	}
	if ctx.Err() == nil && !runtime.check(ctx) {
		cancel()
	}
	commandExpired := ctx.Err() == context.DeadlineExceeded
	if parent.Err() != nil && interrupted.Load() == 0 {
		interrupted.Store(130)
	}
	runtime.observer.Stop()
	cancel()
	<-watchDone
	if liveProg != nil {
		liveProg.Send(tui.LiveTableDoneMsg{})
		<-liveDone
	}
	report.ContextValid = runtime.isValid()
	if !report.ContextValid {
		report.Issue = "runtime-changed"
	}
	if commandExpired {
		report.Issue = "command-timeout"
	}
	code := 0
	for index := range report.Observations {
		row := &report.Observations[index]
		if row.Policy != "" {
			continue
		}
		for attempt := range row.Samples {
			sample := &row.Samples[attempt]
			sample.Capture = runtime.observer.Evidence(sample.Socket)
			if !sample.Passed(runtime.mode == "tun", command == "speed.download") {
				code = 1
			}
		}
		if command == "ip" && row.IP == "" {
			code = 1
		}
	}
	if liveErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %s\n", tui.SafeText(liveErr.Error()))
		code = 1
	}
	if report.Issue != "" {
		code = 1
	}
	if interrupted.Load() != 0 {
		report.Interrupted = true
		code = int(interrupted.Load())
	}
	return renderDiagnostic(env, report, options.json, code)
}

func diagnosticHeaders(command string) []string {
	raw := strings.TrimSuffix(i18n.T("Target / source\tFirst response\tResponses\tHTTP\tCapture\tPath\tExit IP / rate\n"), "\n")
	headers := strings.Split(raw, "\t")
	if command == "check" {
		headers[6] = i18n.T("Category")
	}
	return headers
}

func diagnosticCategoryLabel(category string) string {
	switch category {
	case "china":
		return i18n.T("China")
	case "global":
		return i18n.T("International")
	case "ai":
		return i18n.T("AI")
	default:
		return i18n.T("Custom")
	}
}

func formatObservationRow(row diagnosticObservation) ([]string, bool) {
	name := row.ID + " (" + row.Host + ")"
	if row.Source != "" {
		name = fmt.Sprintf("%s IPv%d", row.Source, row.Family)
	}
	if row.Policy != "" {
		return []string{name, "-", "-", "-", i18n.T("policy-skipped, not verified"), "-", "-"}, false
	}
	var durations []float64
	var statuses []string
	verified, responses, path, pending := true, 0, "unknown", false
	for _, sample := range row.Samples {
		if sample.Issue == "not-started" {
			pending = true
		}
		if sample.FirstResponseMS != nil {
			durations = append(durations, *sample.FirstResponseMS)
		}
		if sample.HTTPStatus != nil {
			responses++
			statuses = append(statuses, strconv.Itoa(*sample.HTTPStatus))
		}
		verified = verified && sample.Capture.Verified
		if sample.Capture.Path != "unknown" {
			path = sample.Capture.Path
		}
	}
	latency := "-"
	if len(durations) > 0 {
		slices.Sort(durations)
		middle := len(durations) / 2
		median := durations[middle]
		if len(durations)%2 == 0 {
			median = (median + durations[middle-1]) / 2
		}
		latency = fmt.Sprintf("%.1f ms", median)
	}
	capture := i18n.T("unverified")
	if verified {
		capture = i18n.T("verified")
	}
	value := row.IP
	if row.Category != "" {
		value = diagnosticCategoryLabel(row.Category)
	}
	if value == "" {
		value = i18n.T("unknown")
	}
	if len(row.Samples) == 1 && row.Samples[0].MbitPerSecond != nil {
		value = fmt.Sprintf("%.1f Mbit/s", *row.Samples[0].MbitPerSecond)
	}
	httpText := strings.Join(slices.Compact(statuses), "/")
	if httpText == "" {
		httpText = "-"
	}
	return []string{
		name,
		latency,
		fmt.Sprintf("%d/%d", responses, len(row.Samples)),
		httpText,
		capture,
		diagnostic.SafeText(path),
		value,
	}, pending
}

func buildDiagnosticTableRows(observations []diagnosticObservation) ([][]string, map[int]bool) {
	rows := make([][]string, len(observations))
	pending := make(map[int]bool)
	for i, obs := range observations {
		row, isPending := formatObservationRow(obs)
		rows[i] = row
		if isPending {
			pending[i] = true
		}
	}
	return rows, pending
}

func renderDiagnostic(env Env, report diagnosticReport, asJSON bool, code int) int {
	if asJSON {
		if json.NewEncoder(env.Stdout).Encode(report) != nil {
			return 1
		}
		return code
	}
	out := env.output()
	th := out.Theme
	if report.Mode != nil {
		headerLine := fmt.Sprintf(i18n.T("scope: sbc; mode: %s; route: %s; selected protocol: %s\n"), *report.Mode, diagnostic.SafeText(*report.Route), diagnostic.SafeText(*report.Protocol))
		if th.Styled() {
			headerLine = th.Header.Render(strings.TrimSuffix(headerLine, "\n")) + "\n"
		}
		width, _ := out.Size()
		fmt.Fprint(out, tui.FitLines(headerLine, width))
	}
	for _, warning := range slices.Compact(report.Warnings) {
		fmt.Fprintln(env.Stderr, diagnosticMessage(warning))
	}
	rows, _ := buildDiagnosticTableRows(report.Observations)
	printTable(env, diagnosticHeaders(report.Command), rows, tui.DiagnosticCellStyler)
	for _, row := range report.Observations {
		if row.Policy != "" {
			continue
		}
		if row.Details != nil {
			location, org, asn := diagnostic.SafeText(row.Details.Location()), diagnostic.SafeText(row.Details.Organization), i18n.T("unknown")
			if location == "" {
				location = i18n.T("unknown")
			}
			if org == "" {
				org = i18n.T("unknown")
			}
			if row.Details.ASN != 0 {
				asn = fmt.Sprintf("AS%d", row.Details.ASN)
			}
			fmt.Fprintf(env.Stdout, i18n.T("%s: %s; %s / %s (database estimates)\n"), row.ID, location, asn, org)
		}
		for _, sample := range row.Samples {
			if report.Command == "speed.download" {
				duration := i18n.T("unknown")
				if sample.TransferMS != nil {
					duration = fmt.Sprintf("%.3f s", *sample.TransferMS/1000)
				}
				fmt.Fprintf(env.Stdout, i18n.T("Downloaded %d bytes; transfer %s; stop: %s.\n"), sample.PayloadBytes, duration, diagnosticStopLabel(sample.StopReason))
			}
			if sample.Issue != "" {
				fmt.Fprintf(env.Stdout, "%s: %s\n", row.ID, diagnosticMessage(sample.Issue))
			}
			if report.Mode != nil && *report.Mode == "tun" && sample.HTTPStatus != nil && !sample.Capture.Verified {
				fmt.Fprintf(env.Stdout, "%s: %s\n", row.ID, i18n.T("TUN scope is unverified. The HTTP observation does not prove that TUN captured this request."))
			}
		}
	}
	if report.Issue != "" {
		fmt.Fprintln(env.Stderr, diagnosticMessage(report.Issue))
	}
	if report.Interrupted {
		fmt.Fprintln(env.Stderr, i18n.T("Interrupted. Results are partial."))
	}
	return code
}

func diagnosticStopLabel(reason string) string {
	switch reason {
	case "payload-complete":
		return i18n.T("payload budget complete")
	case "time-window-ended":
		return i18n.T("transfer time window ended")
	case "stalled":
		return i18n.T("download stalled")
	default:
		return i18n.T("download did not complete")
	}
}

func diagnosticMessage(code string) string {
	switch code {
	case "unsafe-runtime":
		return i18n.T("TUN state is unsafe or unavailable for this owner. Run 'sbc tun off' locally to recover.")
	case "missing-diagnostic-state":
		return i18n.T("The active TUN has no generation-bound diagnostic metadata. Run 'sbc refresh' locally first.")
	case "unavailable-runtime":
		return i18n.T("The installed sbc runtime or its authenticated API is unavailable.")
	case "runtime-changed":
		return i18n.T("Runtime context changed or became unavailable. Results are not comparable. Run the check again.")
	case "command-timeout", "timeout":
		return i18n.T("The diagnostic time limit expired.")
	case "dns":
		return i18n.T("DNS resolution failed.")
	case "tls":
		return i18n.T("TLS verification failed.")
	case "connection":
		return i18n.T("The connection or HTTP response failed.")
	case "canceled":
		return i18n.T("The request was canceled.")
	case "not-started":
		return i18n.T("The request did not start.")
	case "invalid-ip":
		return i18n.T("The source returned an invalid address or address family.")
	case "empty-payload":
		return i18n.T("The download returned no payload.")
	case "invalid-download", "short-payload":
		return i18n.T("The download response did not match the requested payload.")
	case "stalled":
		return i18n.T("The download stopped receiving data.")
	case "details-unavailable":
		return i18n.T("IP details are unavailable. Observed IP addresses remain valid.")
	case "active-rules", "direct-exposure":
		return i18n.T("Checks follow active rules. Direct destinations can see this network's public IP. No account eligibility is tested.")
	default:
		return i18n.T("Invalid diagnostic target or configuration.")
	}
}

// formatBytes is shared with status and traffic output.
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
