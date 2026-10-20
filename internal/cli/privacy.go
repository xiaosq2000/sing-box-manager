package cli

import (
	"fmt"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/region"
)

// runPrivacy summarizes the browser and region choices. It changes nothing.
func runPrivacy(env Env) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	if env.OS == "linux" {
		printBrowserSummary(env)
		_ = printBrowserLanguageStatus(env, layout)
		printRegionChoice(env, layout)
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("Browser protection: unavailable on this platform; Linux Chrome only"))
		fmt.Fprintln(env.Stdout, i18n.T("Region matching: unavailable on this platform"))
	}
	fmt.Fprintln(env.Stdout, i18n.T("Run 'sbc privacy browser' or 'sbc privacy region' for details."))
	return 0
}

func printBrowserSummary(env Env) {
	privacy, err := env.Privacy()
	if err != nil {
		fmt.Fprintf(env.Stdout, i18n.T("Browser protection: unavailable (%v)\n"), err)
		return
	}
	optedOut, err := privacy.WebRTCOptedOut()
	if err != nil {
		fmt.Fprintf(env.Stdout, i18n.T("Browser protection: unknown (%v)\n"), err)
		return
	}
	if optedOut {
		fmt.Fprintln(env.Stdout, i18n.T("Browser protection: off (opted out)"))
		return
	}
	fmt.Fprintln(env.Stdout, i18n.T("Browser protection: on (automatic repair)"))
}

func printRegionChoice(env Env, layout paths.Layout) {
	status, err := regionFor(env, layout).Status()
	if err != nil {
		fmt.Fprintf(env.Stdout, i18n.T("Region matching: unknown (%v)\n"), err)
		return
	}
	if status.Scope == region.ScopeSystem {
		if status.State != nil && status.Mode.Any() {
			profile := status.State.Profile
			fmt.Fprintf(env.Stdout, i18n.T("Region matching: %s (system) (%s, %s)\n"), regionModeText(status.Mode), profile.Timezone, profile.Language)
		} else if status.Mode.Any() {
			fmt.Fprintln(env.Stdout, i18n.T("Region matching: on (system), but the saved profile is missing; run 'sbc refresh'."))
		} else if region.SystemStateExists(systemRegionPaths(env)) {
			fmt.Fprintln(env.Stdout, i18n.T("Region matching: a system region record exists; run 'sbc privacy region off --system'"))
		} else {
			fmt.Fprintln(env.Stdout, i18n.T("Region matching: off"))
		}
		return
	}
	if status.State == nil {
		if status.MissingState {
			fmt.Fprintln(env.Stdout, i18n.T("Region matching: on, but the saved profile is missing; run 'sbc refresh'."))
		} else {
			fmt.Fprintln(env.Stdout, i18n.T("Region matching: unavailable: no client region is saved"))
		}
		printOrphanedSystemRegion(env)
		return
	}
	if status.Mode.Any() {
		profile := status.State.Profile
		fmt.Fprintf(env.Stdout, i18n.T("Region matching: %s (%s, %s)\n"), regionModeText(status.Mode), profile.Timezone, profile.Language)
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("Region matching: off"))
	}
	printOrphanedSystemChoice(env)
}

func printOrphanedSystemChoice(env Env) {
	if region.SystemStateExists(systemRegionPaths(env)) {
		fmt.Fprintln(env.Stdout, i18n.T("Region matching: a system region record exists; run 'sbc privacy region off --system'"))
	}
}

// runPrivacyBrowser attempts both components, even if one fails.
func runPrivacyBrowser(env Env, args []string) int {
	if env.OS != "linux" {
		return fail(env, i18n.New("Chrome WebRTC protection is available on Linux only"))
	}
	webrtc := runPrivacyWebRTC(env, args)
	language := runPrivacyBrowserLanguage(env, args)
	if webrtc != 0 || language != 0 {
		return 1
	}
	return 0
}

// runPrivacyWebRTC changes only the Chrome WebRTC policy choice.
func runPrivacyWebRTC(env Env, args []string) int {
	if env.OS != "linux" {
		return fail(env, i18n.New("Chrome WebRTC protection is available on Linux only"))
	}
	privacy, err := env.Privacy()
	if err != nil {
		return fail(env, err)
	}
	action := "status"
	if len(args) != 0 {
		action = args[0]
	}
	if action == "status" {
		return printBrowserStatus(env, privacy)
	}
	if err := privacy.SetWebRTC(action == "on"); err != nil {
		return fail(env, err)
	}
	if action == "on" {
		fmt.Fprintln(env.Stdout, i18n.T("Chrome WebRTC protection is configured and stays enabled across proxy toggles. Restart Chrome to apply the policy."))
		return 0
	}
	if err := reportRemainingWebRTC(env, privacy); err != nil {
		return fail(env, err)
	}
	fmt.Fprintln(env.Stdout, i18n.T("No owned Chrome policy remains. Automatic setup is off. Use 'sbc privacy browser webrtc on' to enable it again. Restart Chrome to apply the change."))
	return 0
}

func printBrowserStatus(env Env, privacy desktop.BrowserPrivacy) int {
	optedOut, err := privacy.WebRTCOptedOut()
	if err != nil {
		return fail(env, err)
	}
	if optedOut {
		fmt.Fprintln(env.Stdout, i18n.T("Browser protection: off (opted out)"))
		fmt.Fprintln(env.Stdout, i18n.T("Run 'sbc privacy browser webrtc on' to enable it."))
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("Browser protection: on (automatic repair)"))
		fmt.Fprintln(env.Stdout, i18n.T("Run 'sbc privacy browser webrtc off' to remove the managed Chrome policy."))
	}
	fmt.Fprintln(env.Stdout, i18n.T("Only Google Chrome on Linux is managed."))
	return 0
}

func reportRemainingWebRTC(env Env, privacy desktop.BrowserPrivacy) error {
	remaining, err := privacy.RemainingWebRTC()
	if err != nil {
		return err
	}
	if len(remaining) != 0 {
		fmt.Fprintf(env.Stdout, i18n.T("Browser settings from another source were preserved:\n  %s\nThese settings can keep WebRTC protection enabled. sbc does not remove administrator or externally changed settings.\n"), strings.Join(remaining, "\n  "))
	}
	return nil
}
