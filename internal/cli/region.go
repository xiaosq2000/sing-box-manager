package cli

import (
	"fmt"
	"os"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/region"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

func regionFor(env Env, layout paths.Layout) *region.Manager {
	manager := region.New(layout, os.Getenv)
	if env.Region != nil {
		manager = env.Region(layout)
	}
	// Follow Env.OS so fixtures can exercise the Linux path on other hosts.
	manager.Supported = env.OS == "linux"
	return manager
}

func runPrivacyRegion(env Env, args []string, timezone, language bool) int {
	if env.OS != "linux" {
		return fail(env, i18n.New("region matching is available on Linux only"))
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	manager := regionFor(env, layout)
	action := "status"
	if len(args) != 0 {
		action = args[0]
	}
	switch action {
	case "status":
		return printRegionStatus(env)
	case "on":
		return enableRegion(env, layout, manager, timezone, language)
	case "off":
		return disableRegion(env, manager, timezone, language)
	default:
		return fail(env, i18n.Errorf("unknown region action %q", action))
	}
}

func enableRegion(env Env, layout paths.Layout, manager *region.Manager, timezone, language bool) int {
	state, mode, err := manager.Load()
	if err != nil {
		return fail(env, err)
	}
	if state == nil {
		link, err := install.ReadLink(layout)
		if err != nil {
			return fail(env, err)
		}
		version, err := os.ReadFile(layout.SingBoxVersion())
		if err != nil {
			return fail(env, i18n.New("the installed sing-box version is unknown; run 'sbc install'"))
		}
		profile, node, err := env.Installer(layout, env.Stdout).FetchRegion(link, strings.TrimSpace(string(version)))
		if err != nil {
			return fail(env, err)
		}
		if profile == nil {
			return fail(env, i18n.New("the portal did not send a client region for this node; ask the operator to set deployment.client_region"))
		}
		state = &region.State{Node: node, Profile: *profile}
	}
	requested := mode
	if !timezone && !language {
		requested = region.Mode{Timezone: true, Language: true}
	} else {
		if timezone {
			requested.Timezone = true
		}
		if language {
			requested.Language = true
		}
	}
	result, err := manager.Apply(*state, requested)
	if err != nil {
		return fail(env, err)
	}
	return reportRegionApply(env, requested, result)
}

func disableRegion(env Env, manager *region.Manager, timezone, language bool) int {
	state, mode, err := manager.Load()
	if err != nil {
		if timezone || language {
			return fail(env, err)
		}
		// A corrupt state or mode file must not trap the user. Remove the
		// cached profile too, so 'on' fetches a fresh one.
		result, disableErr := manager.Remove()
		if disableErr != nil {
			return fail(env, disableErr)
		}
		reportRegionDisabled(env, result)
		return 0
	}
	requested := mode
	if !timezone && !language {
		requested = region.Mode{}
	} else {
		if timezone {
			requested.Timezone = false
		}
		if language {
			requested.Language = false
		}
	}
	if requested.Any() {
		if state == nil {
			return fail(env, i18n.New("the saved region profile is missing; run 'sbc refresh'"))
		}
		result, err := manager.Apply(*state, requested)
		if err != nil {
			return fail(env, err)
		}
		reportRegionDisabled(env, result)
		return 0
	}
	result, err := manager.Disable()
	if err != nil {
		return fail(env, err)
	}
	reportRegionDisabled(env, result)
	return 0
}

func reportRegionApply(env Env, requested region.Mode, result region.ApplyResult) int {
	failed := false
	if result.Mode.Timezone {
		fmt.Fprintln(env.Stdout, i18n.T("The time zone is now set to the node's region."))
	}
	if result.Mode.Language {
		fmt.Fprintln(env.Stdout, i18n.T("The language is now set to the node's region."))
	}
	if result.TimezoneError != nil {
		failed = true
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.TimezoneError)
	}
	if result.LanguageError != nil {
		failed = true
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.LanguageError)
		fmt.Fprintln(env.Stderr, i18n.T("After generating the locale, run 'sbc privacy region on --language'."))
	}
	if failed {
		fmt.Fprintln(env.Stderr, i18n.T("Fix the problem above and retry the command."))
	}
	if requested.Any() && result.Mode.Any() {
		fmt.Fprintln(env.Stdout, i18n.T("Restart open applications and browsers to apply the change."))
	}
	if result.ManagerWarning != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.ManagerWarning)
		fmt.Fprintln(env.Stderr, i18n.T("Log out and back in for the desktop to use the new settings."))
	}
	if result.Mode.Language {
		warnLCAll(env, result.LCAll)
	}
	if failed {
		return 1
	}
	return 0
}

func reportRegionDisabled(env Env, result region.ApplyResult) {
	if result.TimezoneError != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.TimezoneError)
	}
	if result.LanguageError != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.LanguageError)
	}
	if result.Mode.Any() {
		fmt.Fprintf(env.Stdout, i18n.T("Region matching is now %s. Open applications keep their current time zone and language until you restart them.\n"), regionModeText(result.Mode))
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("Region matching is off. Open applications keep their current time zone and language until you restart them."))
	}
	if result.ManagerWarning != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.ManagerWarning)
		fmt.Fprintln(env.Stderr, i18n.T("Log out and back in for the desktop to use the new settings."))
	}
}

func printRegionStatus(env Env) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	if env.OS != "linux" {
		return fail(env, i18n.New("region matching is available on Linux only"))
	}
	manager := regionFor(env, layout)
	status, err := manager.Status()
	if err != nil {
		return fail(env, err)
	}
	if status.State == nil {
		if status.MissingState {
			fmt.Fprintln(env.Stdout, i18n.T("Region: on, but the saved profile is missing; run 'sbc refresh'."))
		} else {
			fmt.Fprintln(env.Stdout, i18n.T("Region: unavailable: no client region is saved; run 'sbc refresh' after the portal provides one."))
		}
		if !status.FilesCurrent {
			fmt.Fprintln(env.Stdout, i18n.T("Files: left over; run 'sbc privacy region off'."))
		}
		return 0
	}
	profile := status.State.Profile
	if status.Mode.Any() {
		fmt.Fprintf(env.Stdout, i18n.T("Region: %s (%s, %s)\n"), regionModeText(status.Mode), profile.Timezone, profile.Language)
		fmt.Fprintf(env.Stdout, i18n.T("Node: %s\n"), status.State.Node)
		if !status.Updated.IsZero() {
			fmt.Fprintf(env.Stdout, i18n.T("Profile updated: %s\n"), status.Updated.Format("2006-01-02 15:04"))
		}
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("Region: off"))
		fmt.Fprintf(env.Stdout, i18n.T("Last profile: %s, %s (%s)\n"), profile.Timezone, profile.Language, status.State.Node)
		if !status.Updated.IsZero() {
			fmt.Fprintf(env.Stdout, i18n.T("Profile updated: %s\n"), status.Updated.Format("2006-01-02 15:04"))
		}
	}
	if status.Mode.Timezone {
		if status.TimezoneAvailable {
			fmt.Fprintln(env.Stdout, i18n.T("Time zone: available"))
		} else {
			fmt.Fprintf(env.Stderr, "sbc: %v\n", status.TimezoneError)
		}
	} else if status.TimezoneError != nil {
		fmt.Fprintf(env.Stdout, i18n.T("Time zone: off; %v\n"), status.TimezoneError)
	}
	if status.Mode.Language {
		if status.LanguageAvailable {
			fmt.Fprintf(env.Stdout, i18n.T("Language: available (%s)\n"), status.Locale)
		} else {
			fmt.Fprintf(env.Stderr, "sbc: %v\n", status.LanguageError)
		}
	} else if status.LanguageError != nil {
		fmt.Fprintf(env.Stdout, i18n.T("Language: off; %v\n"), status.LanguageError)
	}
	if status.Mode.Any() {
		if status.FilesCurrent {
			fmt.Fprintln(env.Stdout, i18n.T("Files: current"))
		} else {
			fmt.Fprintln(env.Stdout, i18n.T("Files: out of date; run 'sbc privacy region on' to repair."))
		}
	} else if !status.FilesCurrent {
		fmt.Fprintln(env.Stdout, i18n.T("Files: left over; run 'sbc privacy region off'."))
	}
	if !status.Mode.Any() {
		fmt.Fprintln(env.Stdout, i18n.T("Run 'sbc privacy region on' to apply it."))
	}
	if status.Mode.Any() && !proxyLikelyOn(layout) {
		fmt.Fprintln(env.Stdout, i18n.T("Warning: the proxy does not appear to be on; the exit IP will not match the region."))
	}
	if status.Mode.Language {
		warnLCAll(env, status.LCAll)
	}
	return 0
}

func regionModeText(mode region.Mode) string {
	switch {
	case mode.Timezone && mode.Language:
		return i18n.T("on")
	case mode.Timezone:
		return i18n.T("time zone on, language off")
	case mode.Language:
		return i18n.T("time zone off, language on")
	default:
		return i18n.T("off")
	}
}

func proxyLikelyOn(layout paths.Layout) bool {
	if tun.Active() {
		return true
	}
	if _, err := os.Stat(layout.EnvFile()); err == nil {
		return true
	}
	_, err := os.Stat(layout.DesktopFile())
	return err == nil
}

func printRegionSummary(env Env, layout paths.Layout) {
	if env.OS != "linux" {
		return
	}
	manager := regionFor(env, layout)
	status, err := manager.Status()
	if err != nil {
		fmt.Fprintf(env.Stdout, i18n.T("region:   unavailable (%v)\n"), err)
		return
	}
	if status.State == nil || !status.Mode.Any() {
		return
	}
	profile := status.State.Profile
	fmt.Fprintf(env.Stdout, i18n.T("region:   %s (%s, %s)\n"), regionModeText(status.Mode), profile.Timezone, profile.Language)
	if status.Mode.Language && !status.LanguageAvailable {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", status.LanguageError)
	}
	if status.Mode.Timezone && !status.TimezoneAvailable {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", status.TimezoneError)
	}
	if !status.FilesCurrent {
		fmt.Fprintln(env.Stdout, i18n.T("region:   out of date; run 'sbc privacy region on'"))
	}
	if !proxyLikelyOn(layout) {
		fmt.Fprintln(env.Stdout, i18n.T("region:   the proxy is off, so the exit IP will not match"))
	}
}

func warnLCAll(env Env, value string) {
	if value == "" {
		return
	}
	fmt.Fprintf(env.Stderr, i18n.T("LC_ALL is set to %s and overrides the target language; unset it to let sbc's language take effect.\n"), value)
}

// storeInstallRegion saves the region a new subscription carries. A subscription
// without one turns the feature off, because the user asked to move to a
// different target.
func storeInstallRegion(env Env, layout paths.Layout, envelope *install.Envelope) {
	if env.OS != "linux" || envelope == nil {
		return
	}
	manager := regionFor(env, layout)
	profile, node, err := envelope.ClientRegion()
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v; region matching was left unchanged.\n", err)
		return
	}
	if profile == nil {
		state, mode, loadErr := manager.Load()
		hadMode := loadErr == nil && mode.Any()
		result, removeErr := manager.Remove()
		if removeErr != nil {
			fmt.Fprintf(env.Stderr, "sbc: %v\n", removeErr)
			return
		}
		if hadMode {
			reportRegionDisabled(env, result)
		}
		if hadMode || state != nil {
			fmt.Fprintln(env.Stdout, i18n.T("The portal did not send a client region; region matching is off."))
		}
		return
	}
	storeRegion(env, manager, profile, node)
}

// refreshRegion updates the cached profile after a subscription refresh. A
// portal that temporarily omits the field keeps the last profile. A new link
// that omits it turns the feature off instead.
func refreshRegion(env Env, layout paths.Layout, envelope *install.Envelope, dropWhenMissing bool) {
	if env.OS != "linux" || envelope == nil {
		return
	}
	manager := regionFor(env, layout)
	profile, node, err := envelope.ClientRegion()
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v; keeping the saved region profile.\n", err)
		return
	}
	state, mode, loadErr := manager.Load()
	if loadErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", loadErr)
		return
	}
	if profile == nil {
		if dropWhenMissing {
			hadMode := mode.Any()
			result, removeErr := manager.Remove()
			if removeErr != nil {
				fmt.Fprintf(env.Stderr, "sbc: %v\n", removeErr)
				return
			}
			if hadMode {
				reportRegionDisabled(env, result)
			}
			if hadMode || state != nil {
				fmt.Fprintln(env.Stdout, i18n.T("The new subscription does not provide a client region; region matching is off."))
			}
		} else if state != nil {
			fmt.Fprintln(env.Stderr, i18n.T("sbc: the portal did not send a client region; keeping the last saved profile."))
		}
		return
	}
	normalized, err := profile.Normalize()
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v; keeping the saved region profile.\n", err)
		return
	}
	next := region.State{Node: node, Profile: normalized}
	changed := state == nil || state.Node != next.Node || state.Profile != next.Profile
	if err := manager.StoreState(next); err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		return
	}
	if !mode.Any() {
		return
	}
	if !changed {
		status, err := manager.Status()
		if err == nil && status.FilesCurrent {
			return
		}
	}
	result, err := manager.Apply(next, mode)
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		return
	}
	reportRegionApply(env, mode, result)
}

func storeRegion(env Env, manager *region.Manager, profile *region.Profile, node string) {
	normalized, err := profile.Normalize()
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v; region matching was left unchanged.\n", err)
		return
	}
	state := region.State{Node: node, Profile: normalized}
	if err := manager.StoreState(state); err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		return
	}
	_, mode, err := manager.Load()
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		return
	}
	if !mode.Any() {
		return
	}
	result, err := manager.Apply(state, mode)
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		return
	}
	reportRegionApply(env, mode, result)
}
