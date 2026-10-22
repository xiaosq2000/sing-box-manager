package cli

import (
	"fmt"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func browserGeolocationFor(env Env, layout paths.Layout) (*desktop.ChromeGeolocation, error) {
	if env.BrowserGeolocation != nil {
		return env.BrowserGeolocation(layout)
	}
	return desktop.NewChromeGeolocation(env.OS, layout.BrowserGeolocationFile(), desktop.Exec)
}

func runPrivacyBrowserGeolocation(env Env, args []string) int {
	if env.OS != "linux" {
		return fail(env, i18n.New("Chrome geolocation protection is available on Linux only"))
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	manager, err := browserGeolocationFor(env, layout)
	if err != nil {
		return fail(env, err)
	}
	if len(args) == 0 || args[0] == "status" {
		return printBrowserGeolocationStatus(env, manager)
	}
	if err := manager.Set(args[0] == "on"); err != nil {
		return fail(env, err)
	}
	if args[0] == "on" {
		fmt.Fprintln(env.Stdout, i18n.T("Chrome website geolocation blocking is configured for all accounts. Restart Chrome and check chrome://policy."))
		fmt.Fprintln(env.Stdout, i18n.T("This blocks website location requests, not IP-based location estimates."))
		return 0
	}
	fmt.Fprintln(env.Stdout, i18n.T("Chrome geolocation protection: off. No owned policy remains. Existing website permissions were not changed. Restart Chrome."))
	return reportRemainingGeolocation(env, manager)
}

func printBrowserGeolocationStatus(env Env, manager *desktop.ChromeGeolocation) int {
	status, err := manager.Status()
	if err != nil {
		return fail(env, err)
	}
	choice := i18n.T("off")
	if status.Enabled {
		choice = i18n.T("on")
	}
	printKVLine(env, fmt.Sprintf(i18n.T("Chrome geolocation saved choice: %s\n"), choice))
	if status.Configured {
		printKVLine(env, i18n.T("Chrome geolocation policy: blocking value found in local files")+"\n")
	} else {
		printKVLine(env, i18n.T("Chrome geolocation policy: no local blocking value found")+"\n")
	}
	if status.Owned {
		printKVLine(env, i18n.T("Chrome geolocation ownership record: present")+"\n")
	}
	if !status.Enabled && status.Owned {
		fmt.Fprintln(env.Stdout, i18n.T("Chrome geolocation cleanup is pending. Retry 'sbc privacy browser geolocation off'."))
	}
	printGeolocationPolicies(env, status)
	fmt.Fprintln(env.Stdout, i18n.T("Local files do not prove Chrome's effective policy. Restart Chrome and check chrome://policy for DefaultGeolocationSetting=2 and URL exceptions."))
	fmt.Fprintln(env.Stdout, i18n.T("Only Google Chrome on Linux is managed. The policy affects all accounts. IP-based location estimates remain possible."))
	if len(status.Conflicts) != 0 || (status.Enabled && !status.Configured) || (!status.Enabled && status.Owned) {
		return 1
	}
	return 0
}

func printGeolocationPolicies(env Env, status desktop.GeolocationStatus) {
	if len(status.Policies) != 0 {
		fmt.Fprintf(env.Stdout, i18n.T("Chrome geolocation policy files (including external settings):\n  %s\n"), strings.Join(status.Policies, "\n  "))
	}
	if len(status.Conflicts) != 0 {
		fmt.Fprintf(env.Stdout, i18n.T("Chrome geolocation policy conflicts:\n  %s\nReview DefaultGeolocationSetting and geolocation URL lists. sbc does not overwrite external policies.\n"), strings.Join(status.Conflicts, "\n  "))
	}
}

func reportRemainingGeolocation(env Env, manager *desktop.ChromeGeolocation) int {
	status, err := manager.Status()
	if err != nil {
		return fail(env, err)
	}
	printGeolocationPolicies(env, status)
	return 0
}
