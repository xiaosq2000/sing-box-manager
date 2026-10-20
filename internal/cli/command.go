package cli

import (
	"errors"
	"fmt"
	"io"
	"slices"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
	"github.com/spf13/pflag"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/routes"
	"github.com/xiaosq2000/sing-box-manager/internal/shell"
)

// Run creates a fresh command tree for each invocation. Handlers retain their
// exit codes: 0 for success, 1 for an operation failure, and 2 for CLI syntax errors.
func Run(env Env, args []string) int {
	exit := 0
	root := newCommand(env, &exit)
	args = normalizeArgs(root, args)
	if len(args) > 0 && (args[0] == cobra.ShellCompRequestCmd || args[0] == cobra.ShellCompNoDescRequestCmd) {
		// Cobra's informational stderr can become a terminating native-command
		// error in PowerShell 5.1 when ErrorActionPreference is Stop.
		root.SetErr(io.Discard)
	}
	// A non-nil slice prevents Cobra from reading the test process's arguments.
	root.SetArgs(append([]string{}, args...))
	command, err := root.ExecuteC()
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		if command == nil || command == root {
			fmt.Fprintln(env.Stderr, i18n.T("Run 'sbc help' for usage."))
		} else {
			fmt.Fprintf(env.Stderr, i18n.T("Run '%s --help' for usage.\n"), command.CommandPath())
		}
		return 2
	}
	return exit
}

func newCommand(env Env, exit *int) *cobra.Command {
	root := &cobra.Command{
		Use: "sbc", Short: i18n.T("Manage the sing-box client"), Version: env.Version,
		SilenceErrors: true, SilenceUsage: true,
		CompletionOptions: cobra.CompletionOptions{DisableDefaultCmd: true},
		Args: func(cmd *cobra.Command, args []string) error {
			if len(args) != 0 {
				return i18n.Errorf("unknown command %q", args[0])
			}
			return nil
		},
		Run: func(cmd *cobra.Command, _ []string) { cmd.Help() },
	}
	root.SetIn(env.Stdin)
	root.SetOut(env.Stdout)
	root.SetErr(env.Stderr)
	root.SetVersionTemplate("sbc {{.Version}}\n")
	root.SetHelpFunc(func(cmd *cobra.Command, _ []string) { printHelp(cmd) })
	root.SetUsageFunc(func(cmd *cobra.Command) error { printHelp(cmd); return nil })
	root.SetFlagErrorFunc(localizeFlagError)
	root.CompletionOptions.SetDefaultShellCompDirective(cobra.ShellCompDirectiveNoFileComp)
	root.AddGroup(
		&cobra.Group{ID: "setup", Title: i18n.T("Setup:")},
		&cobra.Group{ID: "shell", Title: i18n.T("Shells and programs:")},
		&cobra.Group{ID: "privacy", Title: i18n.T("Privacy:")},
		&cobra.Group{ID: "proxy", Title: i18n.T("Proxy:")},
		&cobra.Group{ID: "help", Title: i18n.T("Help:")},
	)

	add := func(use, description, group string, args cobra.PositionalArgs, run func([]string) int) *cobra.Command {
		cmd := &cobra.Command{
			Use: use, Short: description, GroupID: group, Args: args,
			ValidArgsFunction: fixedChoices(),
			Run:               func(_ *cobra.Command, args []string) { *exit = run(args) },
		}
		cmd.Flags().BoolP("help", "h", false, i18n.T("Show help for this command"))
		root.AddCommand(cmd)
		return cmd
	}

	var options installOptions
	var auth string
	installer := add("install", i18n.T("Install from a subscription link read from standard input"), "setup", argumentRange(0, 0), func(_ []string) int {
		return withRecovery(env, func(env Env) int { return runInstall(env, options) })
	})
	installer.Flags().StringVar(&options.shell, "shell", "", i18n.T("Shell integration: bash or zsh"))
	installer.Flags().BoolVar(&options.noRC, "no-rc", false, i18n.T("Do not edit shell rc files"))
	installer.Flags().StringVar(&auth, "auth", "", i18n.T("Local proxy authentication: on or off"))
	installer.Flags().StringVar(&options.route, "route", "", i18n.T("Initial route name"))
	installer.Flags().StringVar(&options.protocol, "protocol", "", i18n.T("Initial protocol name"))
	installer.Args = func(cmd *cobra.Command, args []string) error {
		if err := argumentRange(0, 0)(cmd, args); err != nil {
			return err
		}
		switch auth {
		case "":
		case "on":
			options.auth = install.AuthOn
		case "off":
			options.auth = install.AuthOff
		default:
			return i18n.New("install: --auth takes on or off")
		}
		if options.shell == "" {
			options.shell = shell.DetectShell()
		}
		if !slices.Contains([]string{"bash", "zsh"}, options.shell) {
			return i18n.Errorf("unsupported shell %q; use bash or zsh", options.shell)
		}
		return nil
	}
	installer.RegisterFlagCompletionFunc("shell", fixedChoices("bash", "zsh"))
	installer.RegisterFlagCompletionFunc("auth", fixedChoices("on", "off"))
	installer.RegisterFlagCompletionFunc("route", configChoices(env, "route"))
	installer.RegisterFlagCompletionFunc("protocol", configChoices(env, "protocol"))

	init := add("init bash|zsh", i18n.T("Print shell integration code, including completion"), "setup", choiceArgs(1, "bash", "zsh"), func(args []string) int {
		return runInit(env, args, root)
	})
	init.ValidArgsFunction = fixedChoices("bash", "zsh")
	link := add("link [set]", i18n.T("Show the masked subscription link, or replace it from standard input"), "setup", choiceArgs(0, "set"), func(args []string) int {
		if len(args) == 0 {
			return runLink(env, args)
		}
		return withRecovery(env, func(env Env) int { return runLink(env, args) })
	})
	link.ValidArgsFunction = fixedChoices("set")
	upgrade := add("upgrade", i18n.T("Install newer binaries from the signed release"), "setup", argumentRange(0, 0), func([]string) int { return runMaintenance(env, "upgrade", false) })
	maintenanceCommands(upgrade, env, exit, "upgrade")
	var yes bool
	uninstall := add("uninstall", i18n.T("Stop the service and remove sbc's files"), "setup", argumentRange(0, 0), func([]string) int { return withOperation(env, func(env Env) int { return runUninstall(env, yes) }) })
	uninstall.Flags().BoolVar(&yes, "yes", false, i18n.T("Proceed without confirmation"))

	add("on", i18n.T("Enable the shell proxy and any opted-in desktop proxy"), "shell", argumentRange(0, 0), func([]string) int { return withRecovery(env, func(env Env) int { return runOn(env) }) })
	add("off", i18n.T("Disable the shell and opted-in desktop proxies. Keep sing-box running"), "shell", argumentRange(0, 0), func([]string) int { return withOperation(env, func(env Env) int { return runOff(env) }) })
	for _, entry := range []struct {
		use, description, group string
		minimum                 int
		run                     func(Env, []string) int
	}{
		{"env [on|off]", i18n.T("Print shell proxy variables. Print PowerShell commands on Windows"), "shell", 0, runEnv},
		{"desktop [on|off]", i18n.T("Show or switch the desktop proxy (GNOME, macOS, Windows)"), "shell", 0, runDesktop},
	} {
		cmd := add(entry.use, entry.description, entry.group, choiceArgs(entry.minimum, "on", "off"), func(args []string) int {
			if entry.use == "desktop [on|off]" && len(args) != 0 {
				if args[0] == "on" {
					return withRecovery(env, func(env Env) int { return entry.run(env, args) })
				}
				return withOperation(env, func(env Env) int { return entry.run(env, args) })
			}
			return entry.run(env, args)
		})
		cmd.ValidArgsFunction = fixedChoices("on", "off")
	}
	privacy := add("privacy", i18n.T("Show or change browser and region privacy settings"), "privacy", argumentRange(0, 0), func([]string) int {
		return runPrivacy(env)
	})
	browser := &cobra.Command{
		Use:   "browser [on|off|status]",
		Short: i18n.T("Show or change Chrome WebRTC protection (Linux)"),
		Args:  choiceArgs(0, "on", "off", "status"),
		Run: func(_ *cobra.Command, args []string) {
			*exit = runPrivacyBrowser(env, args)
		},
	}
	browser.Flags().BoolP("help", "h", false, i18n.T("Show help for this command"))
	browser.ValidArgsFunction = fixedChoices("on", "off", "status")
	privacy.AddCommand(browser)
	var regionTimezone, regionLanguage, regionSystem, regionYes bool
	region := &cobra.Command{
		Use:   "region [on|off|status]",
		Short: i18n.T("Match the time zone and language to the node (Linux)"),
		Args:  choiceArgs(0, "on", "off", "status"),
		Run: func(_ *cobra.Command, args []string) {
			action := ""
			if len(args) != 0 {
				action = args[0]
			}
			if action == "on" || action == "off" {
				*exit = withOperation(env, func(env Env) int {
					return runPrivacyRegion(env, args, regionTimezone, regionLanguage, regionSystem, regionYes)
				})
				return
			}
			*exit = runPrivacyRegion(env, args, regionTimezone, regionLanguage, regionSystem, regionYes)
		},
	}
	region.Flags().BoolVar(&regionTimezone, "timezone", false, i18n.T("Change only the time zone"))
	region.Flags().BoolVar(&regionLanguage, "language", false, i18n.T("Change only the language"))
	region.Flags().BoolVar(&regionSystem, "system", false, i18n.T("Change the system time zone and language for all users"))
	region.Flags().BoolVar(&regionYes, "yes", false, i18n.T("Proceed without confirmation"))
	region.Flags().BoolP("help", "h", false, i18n.T("Show help for this command"))
	region.ValidArgsFunction = fixedChoices("on", "off", "status")
	privacy.AddCommand(region)
	var systemTimezoneValue, systemLanguageValue string
	var systemTimezone, systemLanguage bool
	systemOn := add("_region-system-on", "", "privacy", argumentRange(0, 0), func([]string) int {
		return runRegionSystemOn(env, systemTimezoneValue, systemLanguageValue, systemTimezone, systemLanguage)
	})
	systemOn.Hidden = true
	systemOn.Flags().StringVar(&systemTimezoneValue, "timezone-value", "", "")
	systemOn.Flags().StringVar(&systemLanguageValue, "language-value", "", "")
	systemOn.Flags().BoolVar(&systemTimezone, "timezone", false, "")
	systemOn.Flags().BoolVar(&systemLanguage, "language", false, "")
	var systemOffTimezone, systemOffLanguage bool
	systemOff := add("_region-system-off", "", "privacy", argumentRange(0, 0), func([]string) int {
		return runRegionSystemOff(env, systemOffTimezone, systemOffLanguage)
	})
	systemOff.Hidden = true
	systemOff.Flags().BoolVar(&systemOffTimezone, "timezone", false, "")
	systemOff.Flags().BoolVar(&systemOffLanguage, "language", false, "")
	var dockerYes bool
	docker := add("docker [on|off]", i18n.T("Show or switch the Docker daemon's proxy (Linux). Restarts Docker"), "shell", choiceArgs(0, "on", "off"), func(args []string) int {
		action := ""
		if len(args) != 0 {
			action = args[0]
		}
		if action == "on" {
			return withRecovery(env, func(env Env) int { return runDocker(env, action, dockerYes) })
		}
		if action == "off" {
			return withOperation(env, func(env Env) int { return runDocker(env, action, dockerYes) })
		}
		return runDocker(env, action, dockerYes)
	})
	docker.Flags().BoolVar(&dockerYes, "yes", false, i18n.T("Proceed without confirmation"))
	docker.ValidArgsFunction = fixedChoices("on", "off")

	tunCmd := add("tun on|off", i18n.T("Enable or disable the root-owned Linux TUN service"), "proxy", choiceArgs(1, "on", "off"), func(args []string) int {
		if args[0] == "on" {
			return withOperation(env, func(env Env) int { return runTunOn(env) })
		}
		return withOperation(env, func(env Env) int { return runTunOff(env) })
	})
	tunCmd.ValidArgsFunction = fixedChoices("on", "off")

	var tunConfig, tunBinDir, tunRules, tunOwnerUser string
	var tunOwnerUID int
	tunApply := add("_tun-apply", "", "proxy", argumentRange(0, 0), func([]string) int {
		return runTunApply(env, tunConfig, tunBinDir, tunRules, tunOwnerUID, tunOwnerUser)
	})
	tunApply.Hidden = true
	tunApply.Flags().StringVar(&tunConfig, "config", "", "")
	tunApply.Flags().StringVar(&tunBinDir, "bin-dir", "", "")
	tunApply.Flags().StringVar(&tunRules, "rules", "", "")
	tunApply.Flags().IntVar(&tunOwnerUID, "owner-uid", 0, "")
	tunApply.Flags().StringVar(&tunOwnerUser, "owner-user", "", "")
	tunApply.Args = func(cmd *cobra.Command, args []string) error {
		if err := argumentRange(0, 0)(cmd, args); err != nil {
			return err
		}
		if tunConfig == "" || tunBinDir == "" || tunRules == "" || tunOwnerUID <= 0 || tunOwnerUser == "" {
			return i18n.New("_tun-apply: missing staging arguments")
		}
		return nil
	}
	tunOffRoot := add("_tun-off", "", "proxy", argumentRange(0, 0), func([]string) int {
		return runTunOffRoot(env)
	})
	tunOffRoot.Hidden = true

	add("status", i18n.T("Show service, proxy, route, protocol, shells, and traffic"), "proxy", argumentRange(0, 0), func([]string) int { return runStatus(env) })
	var ip ipOptions
	ipCmd := add("ip", i18n.T("Show exit IPs, with optional unauthenticated AI provider checks"), "proxy", argumentRange(0, 0), func([]string) int { return runIP(env, ip) })
	ipCmd.Flags().BoolVar(&ip.ai, "ai", false, i18n.T("Also probe Gemini, ChatGPT, and Claude"))
	ipCmd.Flags().BoolVar(&ip.allowDirectAI, "allow-direct-ai", false, i18n.T("Permit AI probes to expose your direct network IP (requires --ai)"))
	ipCmd.Args = func(cmd *cobra.Command, args []string) error {
		if err := argumentRange(0, 0)(cmd, args); err != nil {
			return err
		}
		if ip.allowDirectAI && !ip.ai {
			return i18n.New("ip: --allow-direct-ai requires --ai")
		}
		return nil
	}
	ipCmd.Long = i18n.T("General IP checks follow the active route. --ai uses the proxy-only diagnostic inlet and skips the direct route. --allow-direct-ai uses the active route instead. Provider checks use no account credentials and do not prove account eligibility. Redirects are not followed.")
	var speed speedOptions
	speedCmd := add("speed", i18n.T("Test page latency, or download throughput with --download [MB]"), "proxy", argumentRange(0, 0), func([]string) int { return runSpeed(env, speed) })
	speedCmd.Flags().BoolVar(&speed.all, "all", false, i18n.T("Test every protocol without switching the one in use"))
	speedCmd.Flags().IntVar(&speed.megabytes, "download", 0, i18n.T("Download 20 MB, or a size from 1 to 99 MB"))
	speedCmd.Flags().Lookup("download").NoOptDefVal = strconv.Itoa(defaultMegabytes)
	for _, name := range []string{"all", "download"} {
		flag := speedCmd.Flags().Lookup(name)
		flag.Value = &singleUseValue{Value: flag.Value}
	}
	speedCmd.Args = func(cmd *cobra.Command, args []string) error {
		if len(args) != 0 || (cmd.Flags().Changed("download") && (speed.megabytes < 1 || speed.megabytes > maxMegabytes)) {
			return i18n.Errorf("speed takes --all, and --download with an optional size from 1 to %d MB", maxMegabytes)
		}
		return nil
	}
	speedCmd.RegisterFlagCompletionFunc("download", fixedChoices())
	var listRoutes bool
	for _, what := range []string{"route", "protocol"} {
		description := i18n.T("Show or switch the route without restarting sing-box")
		if what == "protocol" {
			description = i18n.T("Show or switch the protocol without restarting sing-box")
		}
		cmd := add(what+" [name]", description, "proxy", argumentRange(0, 1), func(args []string) int {
			if what == "route" && listRoutes {
				return runRouteList(env)
			}
			return runSwitch(env, args, what)
		})
		cmd.ValidArgsFunction = configChoices(env, what)
		if what == "route" {
			cmd.Flags().BoolVar(&listRoutes, "list", false, i18n.T("List available route strategies and their meanings"))
			cmd.Long = routes.Help()
			cmd.Args = func(cmd *cobra.Command, args []string) error {
				if listRoutes && len(args) != 0 {
					return i18n.New("route: --list does not take a route name")
				}
				return argumentRange(0, 1)(cmd, args)
			}
		}
	}
	add("port [number]", i18n.T("Show or change the local proxy port"), "proxy", argumentRange(0, 1), func(args []string) int {
		if len(args) == 0 {
			return runPort(env, args)
		}
		return withRecovery(env, func(env Env) int { return runPort(env, args) })
	})
	refresh := add("refresh", i18n.T("Refresh the subscription config now"), "proxy", argumentRange(0, 0), func([]string) int { return runMaintenance(env, "refresh", false) })
	maintenanceCommands(refresh, env, exit, "refresh")
	legacy := add("update", "", "proxy", argumentRange(0, 0), func([]string) int { return runLegacyUpdate(env) })
	legacy.Hidden = true
	worker := add("_maintenance refresh|upgrade", "", "proxy", choiceArgs(1, "refresh", "upgrade"), func(args []string) int { return runMaintenance(env, args[0], true) })
	worker.Hidden = true
	for _, action := range []string{"start", "stop", "restart"} {
		description := map[string]string{
			"start": i18n.T("Start the sing-box service"), "stop": i18n.T("Stop the sing-box service"), "restart": i18n.T("Restart the sing-box service"),
		}[action]
		add(action, description, "proxy", argumentRange(0, 0), func([]string) int {
			if action == "stop" {
				return withOperation(env, func(env Env) int { return runService(env, action) })
			}
			return withRecovery(env, func(env Env) int { return runService(env, action) })
		})
	}
	add("run", i18n.T("Run sing-box in the foreground without a service manager"), "proxy", argumentRange(0, 0), func([]string) int { return runSingBox(env) })
	lang := add("lang [en|zh|auto]", i18n.T("Show or set the CLI language"), "help", choiceArgs(0, "en", "zh", "auto"), func(args []string) int {
		return runLang(env, args)
	})
	lang.ValidArgsFunction = fixedChoices("en", "zh", "auto")
	add("version", i18n.T("Print the sbc build version"), "help", argumentRange(0, 0), func([]string) int {
		fmt.Fprintf(env.Stdout, "sbc %s\n", env.Version)
		return 0
	})
	completion := add("completion bash|zsh|powershell", i18n.T("Print a shell completion script"), "help", choiceArgs(1, "bash", "zsh", "powershell"), func(args []string) int {
		if err := writeCompletion(root, args[0], env.Stdout); err != nil {
			return fail(env, err)
		}
		return 0
	})
	completion.ValidArgsFunction = fixedChoices("bash", "zsh", "powershell")
	completion.Long = i18n.T("Bash and Zsh load completion through sbc init. For PowerShell, add this command to your profile:\n  sbc completion powershell | Out-String | Invoke-Expression\nThe installer does not edit PowerShell profiles.")
	help := &cobra.Command{
		Use: "help [command]", Short: i18n.T("Show help for a command"), GroupID: "help",
		Args: argumentRange(0, 1), ValidArgsFunction: func(_ *cobra.Command, args []string, prefix string) ([]string, cobra.ShellCompDirective) {
			var names []string
			for _, cmd := range root.Commands() {
				if !cmd.Hidden {
					names = append(names, cmd.Name())
				}
			}
			return fixedChoices(names...)(root, args, prefix)
		},
		RunE: func(_ *cobra.Command, args []string) error {
			cmd := root
			if len(args) != 0 {
				var remaining []string
				var err error
				cmd, remaining, err = root.Find(args)
				if err != nil || len(remaining) != 0 || cmd.Hidden {
					return i18n.Errorf("unknown command %q", args[0])
				}
			}
			return cmd.Help()
		},
	}
	help.Flags().BoolP("help", "h", false, i18n.T("Show help for this command"))
	root.SetHelpCommand(help)
	root.Flags().BoolP("help", "h", false, i18n.T("Show help for this command"))
	root.Flags().Bool("version", false, i18n.T("Print the sbc build version"))
	return root
}

func argumentRange(minimum, maximum int) cobra.PositionalArgs {
	return func(cmd *cobra.Command, args []string) error {
		if len(args) < minimum || len(args) > maximum {
			return i18n.Errorf("%s expects %d to %d arguments, got %d", cmd.Name(), minimum, maximum, len(args))
		}
		return nil
	}
}

func choiceArgs(minimum int, choices ...string) cobra.PositionalArgs {
	return func(cmd *cobra.Command, args []string) error {
		if len(args) < minimum || len(args) > 1 || (len(args) == 1 && !slices.Contains(choices, args[0])) {
			return i18n.Errorf("%s takes %s", cmd.Name(), strings.Join(choices, " | "))
		}
		return nil
	}
}

// normalizeArgs preserves the optional, space-separated download size and
// install's standard-library flag spelling. Parsing itself belongs to Cobra.
func normalizeArgs(root *cobra.Command, args []string) []string {
	result := append([]string{}, args...)
	if len(result) == 0 {
		return result
	}
	cmd, _, _ := root.Find(result[:1])
	for index := 1; index < len(result) && result[index] != "--"; index++ {
		arg := result[index]
		if cmd.Name() == "install" && strings.HasPrefix(arg, "-") {
			name, _, hasValue := strings.Cut(strings.TrimLeft(arg, "-"), "=")
			flag := cmd.Flags().Lookup(name)
			if flag != nil {
				if !strings.HasPrefix(arg, "--") && len(name) > 1 {
					result[index] = "-" + arg
				}
				// A string value can itself start with a dash. Do not rewrite it.
				if !hasValue && flag.NoOptDefVal == "" {
					index++
				}
			}
		}
		if cmd.Name() == "speed" && arg == "--download" && index+1 < len(result) && !strings.HasPrefix(result[index+1], "-") {
			result[index] += "=" + result[index+1]
			result = slices.Delete(result, index+1, index+2)
		}
	}
	return result
}

// Speed flags retain their single-use contract while pflag does the parsing.
type singleUseValue struct {
	pflag.Value
	set bool
}

var errRepeatedFlag = errors.New("repeated flag")

func (value *singleUseValue) Set(text string) error {
	if value.set {
		return errRepeatedFlag
	}
	value.set = true
	return value.Value.Set(text)
}

func localizeFlagError(_ *cobra.Command, err error) error {
	var unknown *pflag.NotExistError
	var missing *pflag.ValueRequiredError
	var invalid *pflag.InvalidValueError
	var syntax *pflag.InvalidSyntaxError
	switch {
	case errors.As(err, &unknown):
		return i18n.Errorf("unknown flag %q", unknown.GetSpecifiedName())
	case errors.As(err, &missing):
		return i18n.Errorf("flag --%s needs a value", missing.GetFlag().Name)
	case errors.As(err, &invalid):
		if errors.Is(err, errRepeatedFlag) {
			return i18n.Errorf("flag --%s must not be repeated", invalid.GetFlag().Name)
		}
		return i18n.Errorf("invalid value %q for flag --%s", invalid.GetValue(), invalid.GetFlag().Name)
	case errors.As(err, &syntax):
		return i18n.Errorf("invalid flag syntax %q", syntax.GetSpecifiedFlag())
	default:
		return err
	}
}

// Help uses command and flag metadata, not a second list of command syntax.
func printHelp(cmd *cobra.Command) {
	out := cmd.OutOrStdout()
	description := cmd.Short
	if cmd.Long != "" {
		description = cmd.Long
	}
	if description != "" {
		fmt.Fprintln(out, description)
		fmt.Fprintln(out)
	}
	usage := cmd.UseLine()
	if cmd == cmd.Root() {
		usage = "sbc <command> [flags]"
	}
	fmt.Fprintf(out, "%s %s\n", i18n.T("Usage:"), usage)
	width := 0
	for _, child := range cmd.Commands() {
		if !child.Hidden {
			width = max(width, len(child.Use))
		}
	}
	for _, group := range cmd.Groups() {
		fmt.Fprintf(out, "\n%s\n", group.Title)
		for _, child := range cmd.Commands() {
			if child.GroupID == group.ID && !child.Hidden {
				fmt.Fprintf(out, "  %-*s  %s\n", width, child.Use, child.Short)
			}
		}
	}
	ungrouped := false
	for _, child := range cmd.Commands() {
		if child.GroupID == "" && !child.Hidden {
			ungrouped = true
		}
	}
	if ungrouped {
		fmt.Fprintf(out, "\n%s\n", i18n.T("Commands:"))
		for _, child := range cmd.Commands() {
			if child.GroupID == "" && !child.Hidden {
				fmt.Fprintf(out, "  %-*s  %s\n", width, child.Use, child.Short)
			}
		}
	}
	if cmd.HasAvailableFlags() {
		fmt.Fprintf(out, "\n%s\n%s", i18n.T("Flags:"), cmd.LocalFlags().FlagUsages())
	}
	if cmd == cmd.Root() {
		fmt.Fprintln(out)
		fmt.Fprintln(out, i18n.T("Use 'sbc <command> --help' for command details."))
	}
}
