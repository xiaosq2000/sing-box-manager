package cli

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/browserlanguage"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/region"
)

func browserOperation(env Env, args []string, action func(Env, []string) int) int {
	if env.OS != "linux" || len(args) == 0 || args[0] == "status" {
		return withPanel(env, func(env Env) int { return action(env, args) })
	}
	return withOperation(env, func(env Env) int { return action(env, args) })
}

func browserLanguageFor(env Env, layout paths.Layout) (*browserlanguage.Manager, error) {
	if env.BrowserLanguage != nil {
		return env.BrowserLanguage(layout)
	}
	base := os.Getenv("CHROME_CONFIG_HOME")
	if base == "" {
		base = os.Getenv("XDG_CONFIG_HOME")
	}
	if base == "" {
		home, err := env.Home()
		if err != nil {
			return nil, err
		}
		base = filepath.Join(home, ".config")
	}
	return browserlanguage.New(layout.BrowserLanguageState(), base), nil
}

func runPrivacyBrowserLanguage(env Env, args []string) int {
	if env.OS != "linux" {
		return fail(env, i18n.New("Chrome language matching is available on Linux only"))
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	if len(args) == 0 || args[0] == "status" {
		return printBrowserLanguageStatus(env, layout)
	}
	manager, err := browserLanguageFor(env, layout)
	if err != nil {
		return fail(env, err)
	}
	if args[0] == "off" {
		result, err := manager.Disable()
		reportPreservedLanguages(env, result)
		if err != nil {
			return fail(env, err)
		}
		fmt.Fprintln(env.Stdout, i18n.T("Chrome language matching is off. The language fields sbc still owned were restored or released."))
		return 0
	}
	target, err := cachedBrowserLanguageTarget(env, layout)
	if err != nil {
		return fail(env, err)
	}
	fmt.Fprintln(env.Stdout, i18n.T("Warning: Chrome settings sync can copy these languages to other devices or overwrite them here. sbc does not change sync settings."))
	result, err := manager.Enable(target)
	if len(result.Changed) != 0 {
		languages, _ := browserlanguage.Languages(target.Language)
		fmt.Fprintf(env.Stdout, i18n.T("Chrome languages: applied %s to %d personal profiles. Start Chrome to use them.\n"), languages, len(result.Changed))
	}
	if err != nil {
		return fail(env, err)
	}
	return 0
}

func cachedBrowserLanguageTarget(env Env, layout paths.Layout) (browserlanguage.Target, error) {
	manager := regionFor(env, layout)
	state, err := manager.LoadProfile()
	if err != nil {
		return browserlanguage.Target{}, err
	}
	if state == nil {
		state, err = fetchRegionState(env, layout)
		if err != nil {
			return browserlanguage.Target{}, err
		}
		if err := manager.StoreState(*state); err != nil {
			return browserlanguage.Target{}, err
		}
	}
	if state.Pending == region.PendingRemove {
		return browserlanguage.Target{}, i18n.New("the node language is unavailable. Run 'sbc privacy browser language off' to restore the previous languages")
	}
	return browserlanguage.Target{Node: state.Node, Language: state.Profile.Language}, nil
}

func printBrowserLanguageStatus(env Env, layout paths.Layout) int {
	manager, err := browserLanguageFor(env, layout)
	if err != nil {
		return fail(env, err)
	}
	status, err := manager.Status()
	if err != nil {
		return fail(env, err)
	}
	if !status.State.Enabled {
		printKVLine(env, i18n.T("Chrome languages: off")+"\n")
		if status.Pending != 0 {
			fmt.Fprintln(env.Stdout, i18n.T("Chrome language cleanup is pending. Exit Chrome and retry 'sbc privacy browser language off'."))
		}
		return 0
	}
	if status.State.Target == nil {
		printKVLine(env, i18n.T("Chrome languages: on, but the node language is unavailable. Run 'sbc privacy browser language off'.")+"\n")
	} else {
		printKVLine(env, fmt.Sprintf(i18n.T("Chrome languages: on (%s, node %s)\n"), status.State.Target.Language, status.State.Target.Node))
	}
	printKVLine(env, fmt.Sprintf(i18n.T("Chrome language profiles: %d current, %d pending or changed\n"), status.Current, status.Pending))
	if status.NoProfiles {
		fmt.Fprintln(env.Stdout, i18n.T("sbc found no standard Chrome personal profile. Start Chrome once. Exit Chrome and retry"))
	}
	if (status.Pending != 0 || status.NoProfiles) && status.State.Target != nil {
		fmt.Fprintln(env.Stdout, i18n.T("Exit Chrome and run 'sbc privacy browser language on' to apply the target. If languages changed outside sbc, run 'sbc privacy browser language off' first."))
	}
	fmt.Fprintln(env.Stdout, i18n.T("Warning: Chrome settings sync can copy these languages to other devices or overwrite them here. sbc does not change sync settings."))
	return 0
}

func reportPreservedLanguages(env Env, result browserlanguage.Result) {
	if len(result.Preserved) != 0 {
		fmt.Fprintf(env.Stdout, i18n.T("Chrome language fields changed outside sbc were preserved in:\n  %s\n"), strings.Join(result.Preserved, "\n  "))
	}
}

func noteBrowserLanguageTarget(env Env, layout paths.Layout, envelope *install.Envelope, dropMissing bool) {
	manager, err := browserLanguageFor(env, layout)
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		return
	}
	profile, node, err := envelope.ClientRegion()
	if err != nil {
		return // The region refresh reports invalid subscription metadata.
	}
	var target *browserlanguage.Target
	if profile != nil {
		target = &browserlanguage.Target{Node: node, Language: profile.Language}
	}
	changed, err := manager.NoteTarget(target, dropMissing)
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		return
	}
	if changed {
		if target == nil {
			fmt.Fprintln(env.Stdout, i18n.T("The node language is unavailable. Run 'sbc privacy browser language off' to restore the previous languages."))
		} else {
			fmt.Fprintln(env.Stdout, i18n.T("The Chrome language target changed. Exit Chrome and run 'sbc privacy browser language on' to apply it."))
		}
	}
}
