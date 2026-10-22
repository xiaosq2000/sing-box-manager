package cli

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
	"github.com/xiaosq2000/sing-box-manager/internal/diagnostic"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func diagnosticCommands(root *cobra.Command, env Env, exit *int) {
	command := func(use, help string, args cobra.PositionalArgs, run func(*cobra.Command, []string)) *cobra.Command {
		cmd := &cobra.Command{Use: use, Short: help, Args: args, Run: run, ValidArgsFunction: fixedChoices()}
		cmd.Flags().BoolP("help", "h", false, i18n.T("Show help for this command"))
		return cmd
	}
	var ipOptions diagnosticOptions
	ip := command("ip", i18n.T("Observe exit IPs through the active sbc route"), argumentRange(0, 0), func(_ *cobra.Command, _ []string) { *exit = runDiagnostic(env, "ip", nil, ipOptions) })
	ip.GroupID = "proxy"
	ip.Flags().BoolVar(&ipOptions.dualStack, "dual-stack", false, i18n.T("Request IPv6 observations as well, subject to the active policy"))
	ip.Flags().BoolVar(&ipOptions.json, "json", false, i18n.T("Print a versioned JSON result"))
	root.AddCommand(ip)

	var checkOptions diagnosticOptions
	checkOptions.samples = 1
	check := command("check [URL...]", i18n.T("Check website responses through the active sbc route"), nil, func(_ *cobra.Command, args []string) {
		var targets []diagnostic.Target
		if len(args) != 0 {
			for _, arg := range args {
				target, _ := diagnostic.ParseTarget(arg)
				targets = append(targets, target)
			}
		} else {
			var list diagnostic.TargetList
			var err error
			if checkOptions.preset != "" {
				list, err = diagnostic.Preset(checkOptions.preset)
			} else {
				layout, layoutErr := env.Layout()
				if layoutErr != nil {
					*exit = diagnosticConfigError(env)
					return
				}
				list, err = diagnostic.ReadList(targetPath(layout), true)
			}
			if err != nil {
				*exit = diagnosticConfigError(env)
				return
			}
			targets = list.Targets
		}
		if len(targets) == 0 {
			*exit = diagnosticConfigError(env)
			return
		}
		*exit = runDiagnostic(env, "check", targets, checkOptions)
	})
	check.GroupID = "proxy"
	check.Flags().IntVar(&checkOptions.samples, "samples", 1, i18n.T("Samples per website, from 1 to 5"))
	check.Flags().StringVar(&checkOptions.preset, "preset", "", i18n.T("Read-only website set: default, china, global, or ai"))
	check.Flags().BoolVar(&checkOptions.json, "json", false, i18n.T("Print a versioned JSON result"))
	check.RegisterFlagCompletionFunc("preset", fixedChoices("default", "china", "global", "ai"))
	check.Args = func(_ *cobra.Command, args []string) error {
		if checkOptions.samples < 1 || checkOptions.samples > 5 || len(args) > diagnostic.MaxTargets || (len(args) > 0 && checkOptions.preset != "") {
			return i18n.New("check takes HTTPS URLs or --preset, with --samples from 1 to 5")
		}
		if checkOptions.preset != "" {
			if _, err := diagnostic.Preset(checkOptions.preset); err != nil {
				return i18n.New("unknown diagnostic preset")
			}
		}
		for _, arg := range args {
			if _, err := diagnostic.ParseTarget(arg); err != nil {
				return i18n.New("check requires valid public HTTPS URLs without embedded credentials")
			}
		}
		return nil
	}
	root.AddCommand(check)
	manage := command("targets", i18n.T("Manage the private local website list without network requests"), argumentRange(0, 0), func(cmd *cobra.Command, _ []string) { cmd.Help() })
	check.AddCommand(manage)
	for _, name := range []string{"list", "add", "remove", "import", "export", "reset"} {
		minimum, maximum := 0, 0
		use, help := name, i18n.T("Show or change saved website targets")
		if name == "add" || name == "remove" || name == "import" {
			minimum, maximum = 1, 1
			use += " VALUE"
		}
		if name == "import" {
			help = i18n.T("Replace saved targets from a local versioned JSON file")
		}
		child := command(use, help, argumentRange(minimum, maximum), func(_ *cobra.Command, args []string) { *exit = runTargetCommand(env, name, args) })
		if name == "add" {
			child.Args = func(cmd *cobra.Command, args []string) error {
				if err := argumentRange(1, 1)(cmd, args); err != nil {
					return err
				}
				if _, err := diagnostic.ParseTarget(args[0]); err != nil {
					return i18n.New("check requires valid public HTTPS URLs without embedded credentials")
				}
				return nil
			}
		}
		manage.AddCommand(child)
	}

	speed := command("speed", i18n.T("Download throughput tests require the explicit download subcommand"), argumentRange(0, 0), func(cmd *cobra.Command, _ []string) { cmd.Help() })
	speed.GroupID = "proxy"
	var downloadOptions diagnosticOptions
	var size string
	download := command("download", i18n.T("Measure bounded single-stream download throughput through the active route"), nil, func(_ *cobra.Command, _ []string) { *exit = runDiagnostic(env, "speed.download", nil, downloadOptions) })
	download.Flags().StringVar(&size, "size", "20MB", i18n.T("Payload budget, from 1 to 99 decimal MB"))
	download.Flags().BoolVar(&downloadOptions.json, "json", false, i18n.T("Print a versioned JSON result"))
	download.Args = func(cmd *cobra.Command, args []string) error {
		if err := argumentRange(0, 0)(cmd, args); err != nil {
			return err
		}
		value := strings.TrimSuffix(strings.ToUpper(size), "MB")
		n, err := strconv.Atoi(value)
		if err != nil || n < 1 || n > 99 {
			return i18n.New("download size must be a whole number from 1 to 99 MB")
		}
		downloadOptions.megabytes = n
		return nil
	}
	speed.AddCommand(download)
	root.AddCommand(speed)
}

func targetPath(layout paths.Layout) string {
	return filepath.Join(layout.Config, "diagnostic-targets.json")
}

func diagnosticConfigError(env Env) int {
	fmt.Fprintln(env.Stderr, i18n.T("Invalid, unreadable, or empty diagnostic target configuration. No network request was sent."))
	return 2
}

func runTargetCommand(env Env, action string, args []string) int {
	var imported diagnostic.TargetList
	var err error
	if action == "import" {
		imported, err = diagnostic.ReadList(args[0], false)
		if err != nil {
			return diagnosticConfigError(env)
		}
	}
	layout, err := env.Layout()
	if err != nil {
		return diagnosticConfigError(env)
	}
	if action != "list" && action != "export" {
		unlock, err := maintenance.Lock(layout)
		if err != nil {
			return fail(env, err)
		}
		defer unlock()
	}
	var list diagnostic.TargetList
	if action == "reset" {
		list, err = diagnostic.Preset("default")
	} else if action == "import" {
		list = imported
	} else {
		list, err = diagnostic.ReadList(targetPath(layout), true)
	}
	if err != nil {
		return diagnosticConfigError(env)
	}
	switch action {
	case "list":
		for _, target := range list.Targets {
			fmt.Fprintf(env.Stdout, "%s\t%s\n", target.ID, diagnostic.Host(target.URL))
		}
		return 0
	case "export":
		if json.NewEncoder(env.Stdout).Encode(list) != nil {
			return 1
		}
		return 0
	case "add":
		target, err := diagnostic.ParseTarget(args[0])
		if err != nil || len(list.Targets) >= diagnostic.MaxTargets {
			return diagnosticConfigError(env)
		}
		for _, existing := range list.Targets {
			if existing.ID == target.ID || existing.URL == target.URL {
				return diagnosticConfigError(env)
			}
		}
		list.Targets = append(list.Targets, target)
	case "remove":
		found := -1
		for index, target := range list.Targets {
			if target.ID == args[0] {
				found = index
				break
			}
		}
		if found < 0 {
			return diagnosticConfigError(env)
		}
		list.Targets = append(list.Targets[:found], list.Targets[found+1:]...)
	}
	data, err := json.MarshalIndent(list, "", "  ")
	if err == nil && len(data)+1 > diagnostic.MaxListBytes {
		return diagnosticConfigError(env)
	}
	if err == nil {
		err = paths.WriteFile(targetPath(layout), append(data, '\n'), 0o600)
	}
	if err != nil {
		return fail(env, err)
	}
	fmt.Fprintln(env.Stdout, i18n.T("Saved the private diagnostic target list."))
	return 0
}
