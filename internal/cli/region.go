package cli

import (
	"bufio"
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

func systemRegionPaths(env Env) region.SystemPaths {
	if env.SystemRegion != nil {
		return env.SystemRegion()
	}
	return region.DefaultSystemPaths()
}

func runPrivacyRegion(env Env, args []string, timezone, language, system, yes bool) int {
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
		return enableRegion(env, layout, manager, timezone, language, system, yes)
	case "off":
		return disableRegion(env, manager, timezone, language, system)
	default:
		return fail(env, i18n.Errorf("unknown region action %q", action))
	}
}

func enableRegion(env Env, layout paths.Layout, manager *region.Manager, timezone, language, system, yes bool) int {
	state, mode, err := manager.Load()
	if err != nil {
		return fail(env, err)
	}
	if mode.Any() && mode.System() {
		// An explicit --system is optional once the saved scope is system.
		system = true
	}
	if mode.Any() && !mode.System() && system {
		return fail(env, i18n.New("region matching is on for this account; run 'sbc privacy region off' before using --system"))
	}
	if state == nil {
		fetched, err := fetchRegionState(env, layout)
		if err != nil {
			return fail(env, err)
		}
		state = fetched
	}
	if state.Pending == region.PendingRemove {
		return fail(env, i18n.New("the subscription no longer provides a client region; run 'sbc privacy region off' before enabling it again"))
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
	if system {
		requested.Scope = region.ScopeSystem
		return enableSystemRegion(env, manager, *state, mode, requested, yes)
	}
	requested.Scope = region.ScopeUser
	result, err := manager.Apply(*state, requested)
	if err != nil {
		return fail(env, err)
	}
	return reportRegionApply(env, requested, result)
}

func fetchRegionState(env Env, layout paths.Layout) (*region.State, error) {
	link, err := install.ReadLink(layout)
	if err != nil {
		return nil, err
	}
	version, err := os.ReadFile(layout.SingBoxVersion())
	if err != nil {
		return nil, i18n.New("the installed sing-box version is unknown; run 'sbc install'")
	}
	profile, node, err := env.Installer(layout, env.Stdout).FetchRegion(link, strings.TrimSpace(string(version)))
	if err != nil {
		return nil, err
	}
	if profile == nil {
		return nil, i18n.New("the portal did not send a client region for this node; ask the operator to set deployment.client_region")
	}
	normalized, err := profile.Normalize()
	if err != nil {
		return nil, err
	}
	return &region.State{Node: node, Profile: normalized}, nil
}

func enableSystemRegion(env Env, manager *region.Manager, state region.State, previous region.Mode, requested region.Mode, yes bool) int {
	paths := systemRegionPaths(env)
	var timezoneErr, languageErr error
	if requested.Timezone {
		if err := region.CheckSystemTimezone(paths, state.Profile.Timezone); err != nil {
			timezoneErr = err
			requested.Timezone = false
		}
	}
	if requested.Language {
		if manager.Locales == nil {
			languageErr = region.CheckSystemLocale(state.Profile.Language)
		} else if available, err := manager.Locales(); err != nil {
			languageErr = i18n.Errorf("could not list the installed locales: %w", err)
		} else {
			languageErr = region.CheckLocale(state.Profile.Language, available)
		}
		if languageErr != nil {
			requested.Language = false
		}
	}
	if !requested.Any() {
		return reportSystemRegionCheck(env, timezoneErr, languageErr)
	}
	if !confirmSystemRegion(env, state.Profile, requested, yes) {
		return 1
	}
	if env.Sudo == nil {
		return fail(env, i18n.New("sudo is not available in this environment"))
	}
	self, err := env.Self()
	if err != nil {
		return fail(env, err)
	}
	args := []string{
		"_region-system-on",
		"--timezone-value", state.Profile.Timezone,
		"--language-value", state.Profile.Language,
	}
	if requested.Timezone {
		args = append(args, "--timezone")
	}
	if requested.Language {
		args = append(args, "--language")
	}
	sudoErr := env.Sudo(self, args)

	status, statusErr := region.SystemStatusOf(paths)
	applied := region.Mode{Scope: region.ScopeSystem}
	if statusErr == nil {
		if status.State != nil {
			applied.Timezone = status.State.Timezone != nil
			applied.Language = status.State.Language != nil
		}
	} else if sudoErr == nil {
		// The helper exited cleanly but its record could not be read. Keep the
		// requested components so the saved choice is not lost.
		applied.Timezone = requested.Timezone
		applied.Language = requested.Language
	}
	// Do not adopt a component the saved mode did not already enable: an
	// orphaned record from an interrupted change stays visible in status.
	applied.Timezone = applied.Timezone && (requested.Timezone || previous.Timezone)
	applied.Language = applied.Language && (requested.Language || previous.Language)
	state.Pending = ""
	if err := manager.StoreState(state); err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		if sudoErr == nil {
			sudoErr = err
		}
	}
	// A stale account-level file would shadow the system setting for this
	// account. System scope owns no account files.
	if _, err := manager.ClearUser(); err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		if sudoErr == nil {
			sudoErr = err
		}
	}
	if applied.Any() {
		if err := manager.SaveMode(applied); err != nil {
			fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
			if sudoErr == nil {
				sudoErr = err
			}
		}
	}
	if requested.Timezone && applied.Timezone {
		fmt.Fprintln(env.Stdout, i18n.T("The system time zone is now set to the node's region for all users."))
	}
	if requested.Language && applied.Language {
		fmt.Fprintln(env.Stdout, i18n.T("The system language is now set to the node's region for all users."))
	}
	if requested.Timezone && !applied.Timezone {
		timezoneErr = i18n.New("the system time zone was not changed")
	}
	if requested.Language && !applied.Language {
		languageErr = i18n.New("the system language was not changed")
	}
	if timezoneErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", timezoneErr)
	}
	if languageErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", languageErr)
	}
	if sudoErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", i18n.Errorf("system region setup failed: %w", sudoErr))
	}
	if applied.Language {
		fmt.Fprintln(env.Stdout, i18n.T("Log out and back in for the system language to apply. Restart open applications and browsers."))
		warnLCAll(env, managerLCAll(manager))
	} else if applied.Timezone {
		fmt.Fprintln(env.Stdout, i18n.T("New processes use the new system time zone. Restart open applications and browsers to update their display."))
	}
	if timezoneErr != nil || languageErr != nil || sudoErr != nil {
		fmt.Fprintln(env.Stderr, i18n.T("Fix the problem above and retry the command."))
		return 1
	}
	return 0
}

func reportSystemRegionCheck(env Env, timezoneErr, languageErr error) int {
	if timezoneErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", timezoneErr)
	}
	if languageErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", languageErr)
	}
	fmt.Fprintln(env.Stderr, i18n.T("Fix the problem above and retry the command."))
	return 1
}

func confirmSystemRegion(env Env, profile region.Profile, mode region.Mode, yes bool) bool {
	if yes {
		return true
	}
	var warning string
	switch {
	case mode.Timezone && mode.Language:
		warning = fmt.Sprintf(i18n.T("This changes the system time zone to %s and the system language to %s for all users and services."), profile.Timezone, profile.Language)
	case mode.Timezone:
		warning = fmt.Sprintf(i18n.T("This changes the system time zone to %s for all users and services."), profile.Timezone)
	default:
		warning = fmt.Sprintf(i18n.T("This changes the system language to %s for all users and services."), profile.Language)
	}
	if !isTerminal(env.Stdin) {
		fmt.Fprintf(env.Stderr, i18n.T("sbc: %s Run it again with --yes to go ahead.\n"), warning)
		return false
	}
	fmt.Fprintf(env.Stderr, i18n.T("%s Continue? [y/N] "), warning)
	answer, _ := bufio.NewReader(env.Stdin).ReadString('\n')
	answer = strings.TrimPrefix(strings.TrimSpace(answer), "\ufeff")
	if strings.ToLower(strings.TrimSpace(answer)) != "y" {
		fmt.Fprintln(env.Stdout, i18n.T("Nothing was changed."))
		return false
	}
	return true
}

func disableRegion(env Env, manager *region.Manager, timezone, language, system bool) int {
	state, mode, err := manager.Load()
	if err != nil {
		savedMode, modeErr := manager.LoadMode()
		if modeErr == nil && savedMode.System() {
			return disableSystemRegion(env, manager, nil, savedMode, timezone, language)
		}
		if system {
			if modeErr != nil {
				savedMode = region.Mode{}
			}
			return disableSystemRegion(env, manager, nil, savedMode, timezone, language)
		}
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
	if mode.System() || system {
		return disableSystemRegion(env, manager, state, mode, timezone, language)
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

func disableSystemRegion(env Env, manager *region.Manager, state *region.State, mode region.Mode, timezone, language bool) int {
	offTimezone := timezone || (!timezone && !language)
	offLanguage := language || (!timezone && !language)
	if env.Sudo == nil {
		return fail(env, i18n.New("sudo is not available in this environment"))
	}
	self, err := env.Self()
	if err != nil {
		return fail(env, err)
	}
	args := []string{"_region-system-off"}
	if offTimezone {
		args = append(args, "--timezone")
	}
	if offLanguage {
		args = append(args, "--language")
	}
	sudoErr := env.Sudo(self, args)

	paths := systemRegionPaths(env)
	status, statusErr := region.SystemStatusOf(paths)
	if statusErr != nil {
		// Do not change the saved mode without knowing what remains.
		fmt.Fprintf(env.Stderr, "sbc: %v\n", statusErr)
		if sudoErr != nil {
			fmt.Fprintf(env.Stderr, "sbc: %v\n", i18n.Errorf("system region cleanup failed: %w", sudoErr))
		}
		return 1
	}
	remaining := region.Mode{Scope: region.ScopeSystem}
	if status.State != nil {
		remaining.Timezone = status.State.Timezone != nil
		remaining.Language = status.State.Language != nil
	}
	if mode.System() {
		if remaining.Any() {
			if err := manager.SaveMode(remaining); err != nil {
				return fail(env, err)
			}
		} else if state == nil {
			// The profile is missing or corrupt. Remove the local record too so
			// a later status does not report a broken profile.
			if _, err := manager.Remove(); err != nil {
				return fail(env, err)
			}
		} else if _, err := manager.ClearUser(); err != nil {
			return fail(env, err)
		}
	}
	if remaining.Any() {
		fmt.Fprintf(env.Stdout, i18n.T("System region matching is still %s.\n"), regionModeText(remaining))
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("The system settings sbc owned were restored or released."))
	}
	if offLanguage {
		fmt.Fprintln(env.Stdout, i18n.T("Log out and back in for the system language to apply. Restart open applications and browsers."))
	}
	if sudoErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", i18n.Errorf("system region cleanup failed: %w", sudoErr))
		return 1
	}
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
	if status.Scope == region.ScopeSystem {
		return printSystemRegionStatus(env, layout, status)
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
		printOrphanedSystemRegion(env)
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
	printOrphanedSystemRegion(env)
	return 0
}

func printSystemRegionStatus(env Env, layout paths.Layout, status region.Status) int {
	paths := systemRegionPaths(env)
	sys, sysErr := region.SystemStatusOf(paths)
	profile := region.Profile{}
	if status.State != nil {
		profile = status.State.Profile
	} else if sys.State != nil {
		profile = sys.State.Profile
	}
	if status.Mode.Any() {
		fmt.Fprintf(env.Stdout, i18n.T("Region: %s (system) (%s, %s)\n"), regionModeText(status.Mode), profile.Timezone, profile.Language)
		if status.State != nil {
			fmt.Fprintf(env.Stdout, i18n.T("Node: %s\n"), status.State.Node)
			if !status.Updated.IsZero() {
				fmt.Fprintf(env.Stdout, i18n.T("Profile updated: %s\n"), status.Updated.Format("2006-01-02 15:04"))
			}
		}
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("Region: off"))
	}
	if status.State == nil && status.Mode.Any() {
		fmt.Fprintln(env.Stdout, i18n.T("The saved region profile is missing; run 'sbc refresh'."))
	}
	if sysErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", sysErr)
	}
	if status.Mode.Timezone {
		target, owned, pending := systemComponent(sys, true)
		printSystemComponent(env, i18n.T("System time zone"), target, owned, pending, sys.TimezoneMatches(), sys.CurrentTimezone)
	}
	if status.Mode.Language {
		target, owned, pending := systemComponent(sys, false)
		printSystemComponent(env, i18n.T("System language"), target, owned, pending, sys.LanguageMatches(), sys.CurrentLanguage)
	}
	if status.State != nil && status.State.Pending == region.PendingRemove {
		fmt.Fprintln(env.Stdout, i18n.T("The subscription no longer provides a client region; run 'sbc privacy region off' to restore the system settings."))
	} else if status.Mode.Any() && systemRegionStale(status, sys, profile) {
		fmt.Fprintf(env.Stdout, i18n.T("The system region is out of date; run '%s' to apply the target.\n"), systemRegionHint(status.Mode))
	}
	if userRegionFilesExist(layout) {
		fmt.Fprintln(env.Stdout, i18n.T("Files: left over; run 'sbc privacy region off'."))
	}
	if !status.Mode.Any() && region.SystemStateExists(paths) {
		fmt.Fprintln(env.Stdout, i18n.T("A system region record exists; run 'sbc privacy region off --system' to restore it."))
	}
	if status.Mode.Any() && !proxyLikelyOn(layout) {
		fmt.Fprintln(env.Stdout, i18n.T("Warning: the proxy does not appear to be on; the exit IP will not match the region."))
	}
	if status.Mode.Language {
		warnLCAll(env, status.LCAll)
	}
	return 0
}

func systemComponent(sys region.SystemStatus, timezone bool) (target string, owned, pending bool) {
	if sys.State == nil {
		return "", false, false
	}
	component := sys.State.Language
	if timezone {
		component = sys.State.Timezone
	}
	if component == nil {
		return "", false, false
	}
	return component.Target, true, component.Pending
}

func printSystemComponent(env Env, name, target string, owned, pending, matches bool, current string) {
	switch {
	case !owned:
		fmt.Fprintf(env.Stdout, i18n.T("%s: not applied; run 'sbc privacy region on'.\n"), name)
	case pending:
		fmt.Fprintf(env.Stdout, i18n.T("%s: change incomplete; run 'sbc privacy region on' to finish.\n"), name)
	case matches:
		fmt.Fprintf(env.Stdout, i18n.T("%s: current (%s)\n"), name, current)
	default:
		fmt.Fprintf(env.Stdout, i18n.T("%s: changed outside sbc; sbc set %s. Run 'sbc privacy region on' to reapply.\n"), name, target)
	}
}

func systemRegionStale(status region.Status, sys region.SystemStatus, profile region.Profile) bool {
	if status.State != nil && status.State.Pending == region.PendingApply {
		return true
	}
	if sys.State == nil {
		return false
	}
	if status.Mode.Timezone && (sys.State.Timezone == nil || sys.State.Timezone.Target != profile.Timezone) {
		return true
	}
	if status.Mode.Language && (sys.State.Language == nil || sys.State.Language.Target != profile.Language) {
		return true
	}
	return false
}

func userRegionFilesExist(layout paths.Layout) bool {
	for _, path := range []string{layout.RegionEnv(), layout.RegionEnvironmentD()} {
		info, err := os.Stat(path)
		if err == nil && info.Mode().IsRegular() {
			return true
		}
	}
	return false
}

func printOrphanedSystemRegion(env Env) {
	if region.SystemStateExists(systemRegionPaths(env)) {
		fmt.Fprintln(env.Stdout, i18n.T("A system region record exists; run 'sbc privacy region off --system' to restore it."))
	}
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

func systemRegionHint(mode region.Mode) string {
	switch {
	case mode.Timezone && !mode.Language:
		return i18n.T("sbc privacy region on --timezone")
	case mode.Language && !mode.Timezone:
		return i18n.T("sbc privacy region on --language")
	default:
		return i18n.T("sbc privacy region on")
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
	paths := systemRegionPaths(env)
	if status.Scope == region.ScopeSystem {
		if status.State == nil || !status.Mode.Any() {
			if status.Mode.Any() {
				fmt.Fprintln(env.Stdout, i18n.T("region:   on (system), but the saved profile is missing; run 'sbc refresh'"))
			} else if region.SystemStateExists(paths) {
				fmt.Fprintln(env.Stdout, i18n.T("region:   a system region record exists; run 'sbc privacy region off --system'"))
			}
			return
		}
		fmt.Fprintf(env.Stdout, i18n.T("region:   %s (system) (%s, %s)\n"), regionModeText(status.Mode), status.State.Profile.Timezone, status.State.Profile.Language)
		sys, sysErr := region.SystemStatusOf(paths)
		if sysErr != nil {
			fmt.Fprintf(env.Stderr, "sbc: %v\n", sysErr)
		} else if systemRegionStale(status, sys, status.State.Profile) ||
			(status.Mode.Timezone && !sys.TimezoneMatches()) ||
			(status.Mode.Language && !sys.LanguageMatches()) {
			fmt.Fprintf(env.Stdout, i18n.T("region:   out of date; run '%s'\n"), systemRegionHint(status.Mode))
		}
		if !proxyLikelyOn(layout) {
			fmt.Fprintln(env.Stdout, i18n.T("region:   the proxy is off, so the exit IP will not match"))
		}
		return
	}
	if status.State == nil || !status.Mode.Any() {
		if region.SystemStateExists(paths) {
			fmt.Fprintln(env.Stdout, i18n.T("region:   a system region record exists; run 'sbc privacy region off --system'"))
		}
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

func managerLCAll(manager *region.Manager) string {
	if manager == nil || manager.Getenv == nil {
		return ""
	}
	return manager.Getenv("LC_ALL")
}

// storeInstallRegion saves the region a new subscription carries. A subscription
// without one turns the feature off, because the user asked to move to a
// different target.
func storeInstallRegion(env Env, layout paths.Layout, envelope *install.Envelope) {
	if env.OS != "linux" || envelope == nil {
		return
	}
	manager := regionFor(env, layout)
	state, mode, loadErr := manager.Load()
	if loadErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v; region matching was left unchanged.\n", loadErr)
		return
	}
	profile, node, err := envelope.ClientRegion()
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v; region matching was left unchanged.\n", err)
		return
	}
	if mode.System() {
		if profile == nil {
			if state != nil {
				state.Pending = region.PendingRemove
				if err := manager.StoreState(*state); err != nil {
					fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
					return
				}
			}
			fmt.Fprintln(env.Stdout, i18n.T("The new subscription does not provide a client region; run 'sbc privacy region off' to restore the system settings."))
			return
		}
		normalized, err := profile.Normalize()
		if err != nil {
			fmt.Fprintf(env.Stderr, "sbc: %v; region matching was left unchanged.\n", err)
			return
		}
		next := region.State{Node: node, Profile: normalized}
		sys, sysErr := region.SystemStatusOf(systemRegionPaths(env))
		if sysErr != nil || !systemTargetCurrent(sys, normalized, mode) {
			next.Pending = region.PendingApply
		}
		if err := manager.StoreState(next); err != nil {
			fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
			return
		}
		if next.Pending == region.PendingApply {
			fmt.Fprintf(env.Stdout, i18n.T("The system region target changed; run '%s' to apply it.\n"), systemRegionHint(mode))
		}
		return
	}
	if profile == nil {
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
	state, mode, loadErr := manager.Load()
	if loadErr != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", loadErr)
		return
	}
	profile, node, err := envelope.ClientRegion()
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v; keeping the saved region profile.\n", err)
		return
	}
	if mode.System() {
		refreshSystemRegion(env, manager, state, mode, profile, node, dropWhenMissing)
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

func refreshSystemRegion(env Env, manager *region.Manager, state *region.State, mode region.Mode, profile *region.Profile, node string, dropWhenMissing bool) {
	if profile == nil {
		if dropWhenMissing {
			if state != nil {
				state.Pending = region.PendingRemove
				if err := manager.StoreState(*state); err != nil {
					fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
					return
				}
			}
			fmt.Fprintln(env.Stdout, i18n.T("The new subscription does not provide a client region; run 'sbc privacy region off' to restore the system settings."))
		} else {
			fmt.Fprintln(env.Stderr, i18n.T("sbc: the portal did not send a client region; keeping the last saved profile and system settings."))
		}
		return
	}
	normalized, err := profile.Normalize()
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v; keeping the saved region profile.\n", err)
		return
	}
	next := region.State{Node: node, Profile: normalized}
	paths := systemRegionPaths(env)
	sys, sysErr := region.SystemStatusOf(paths)
	current := sysErr == nil && systemTargetCurrent(sys, normalized, mode)
	changed := state == nil || state.Node != next.Node || state.Profile != next.Profile
	if changed || !current {
		next.Pending = region.PendingApply
	}
	if err := manager.StoreState(next); err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		return
	}
	if next.Pending == region.PendingApply {
		fmt.Fprintf(env.Stdout, i18n.T("The system region target changed; run '%s' to apply it.\n"), systemRegionHint(mode))
	}
}

func systemTargetCurrent(sys region.SystemStatus, profile region.Profile, mode region.Mode) bool {
	if sys.State == nil {
		return false
	}
	if mode.Timezone {
		if sys.State.Timezone == nil || sys.State.Timezone.Target != profile.Timezone || !sys.TimezoneMatches() {
			return false
		}
	}
	if mode.Language {
		if sys.State.Language == nil || sys.State.Language.Target != profile.Language || !sys.LanguageMatches() {
			return false
		}
	}
	return true
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

// runRegionSystemOn is the root-side hidden command.
func runRegionSystemOn(env Env, timezoneValue, languageValue string, timezone, language bool) int {
	if env.OS != "linux" {
		return fail(env, i18n.New("region matching is available on Linux only"))
	}
	// The elevated process must not inherit a user-controlled zone search path.
	_ = os.Unsetenv("TZDIR")
	_ = os.Unsetenv("ZONEINFO")
	_ = os.Unsetenv("TZ")
	applyTimezone := timezone || (!timezone && !language)
	applyLanguage := language || (!timezone && !language)
	result, err := region.SystemApply(region.SystemApplyOptions{
		Paths:    systemRegionPaths(env),
		Profile:  region.Profile{Timezone: timezoneValue, Language: languageValue},
		Timezone: applyTimezone,
		Language: applyLanguage,
	})
	if err != nil {
		return fail(env, err)
	}
	failed := false
	if result.TimezoneWarning != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.TimezoneWarning)
	}
	if result.LanguageWarning != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.LanguageWarning)
	}
	if result.TimezoneError != nil {
		failed = true
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.TimezoneError)
	}
	if result.LanguageError != nil {
		failed = true
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.LanguageError)
	}
	if failed {
		return 1
	}
	return 0
}

// runRegionSystemOff is the root-side hidden command.
func runRegionSystemOff(env Env, timezone, language bool) int {
	if env.OS != "linux" {
		return fail(env, i18n.New("region matching is available on Linux only"))
	}
	result, err := region.SystemDisable(region.SystemDisableOptions{
		Paths:    systemRegionPaths(env),
		Timezone: timezone,
		Language: language,
	})
	if err != nil {
		return fail(env, err)
	}
	failed := false
	if result.TimezoneError != nil {
		failed = true
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.TimezoneError)
	}
	if result.LanguageError != nil {
		failed = true
		fmt.Fprintf(env.Stderr, "sbc: %v\n", result.LanguageError)
	}
	if result.TimezoneKept {
		fmt.Fprintln(env.Stderr, i18n.T("The system time zone was changed outside sbc and was left as is."))
	}
	if result.LanguageKept {
		fmt.Fprintln(env.Stderr, i18n.T("The system language was changed outside sbc and was left as is."))
	}
	if failed {
		return 1
	}
	return 0
}
