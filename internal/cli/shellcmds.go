package cli

import (
	"bufio"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strconv"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
	"github.com/xiaosq2000/sing-box-manager/internal/docker"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/shell"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

type installOptions struct {
	shell string
	noRC  bool
	auth  install.Auth
	// route and protocol replace the subscription's defaults, so a machine
	// moving from the bash client keeps its choices.
	route    string
	protocol string
}

func parseInstallFlags(args []string) (installOptions, error) {
	flags := flag.NewFlagSet("install", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	shellName := flags.String("shell", "", "")
	noRC := flags.Bool("no-rc", false, "")
	auth := flags.String("auth", "", "")
	route := flags.String("route", "", "")
	protocol := flags.String("protocol", "", "")
	if err := flags.Parse(args); err != nil {
		return installOptions{}, i18n.Errorf("install: %v", err)
	}
	if flags.NArg() > 0 {
		return installOptions{}, i18n.Errorf("install: unexpected argument %q", flags.Arg(0))
	}
	options := installOptions{shell: *shellName, noRC: *noRC, route: *route, protocol: *protocol}
	switch *auth {
	case "":
	case "on":
		options.auth = install.AuthOn
	case "off":
		options.auth = install.AuthOff
	default:
		return installOptions{}, i18n.Errorf("install: --auth takes on or off")
	}
	if options.shell == "" {
		options.shell = shell.DetectShell()
	}
	if _, err := shell.RCFile("/", options.shell); err != nil {
		return installOptions{}, err
	}
	return options, nil
}

// setUpShell turns the proxy on for shells and adds the rc block that runs
// `sbc init`.
func setUpShell(env Env, layout paths.Layout, options installOptions) error {
	if err := writeEnvFile(layout); err != nil {
		return err
	}
	line := shell.InitLine(layout.SBC(), options.shell)
	if options.noRC {
		fmt.Fprintf(env.Stdout, i18n.T("To use sbc from your shells, add this line to your rc file:\n  %s\n"), line)
		return nil
	}
	home, err := env.Home()
	if err != nil {
		return err
	}
	rc, err := shell.RCFile(home, options.shell)
	if err != nil {
		return err
	}
	if err := shell.AddBlock(rc, layout.SBC(), options.shell); err != nil {
		return err
	}
	fmt.Fprintf(env.Stdout, i18n.T("Open a new shell to use sbc there. Shells set up in dotfiles need this line:\n  %s\n"), line)
	return nil
}

func writeEnvFile(layout paths.Layout) error {
	_, local, err := localAPI(layout)
	if err != nil {
		return err
	}
	return paths.WriteFile(layout.EnvFile(), []byte(shell.EnvOn(local.ListenPort, local.Username, local.Password)), 0o600)
}

func runEnv(env Env, args []string) int {
	if len(args) > 1 || (len(args) == 1 && args[0] != "on" && args[0] != "off") {
		fmt.Fprintln(env.Stderr, i18n.T("sbc: env takes on or off"))
		return 2
	}
	if len(args) == 1 && args[0] == "off" {
		if env.OS == "windows" {
			fmt.Fprint(env.Stdout, winsettings.PowerShellEnv(0, "", "", false))
		} else {
			fmt.Fprint(env.Stdout, shell.EnvOff())
		}
		return 0
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	_, local, err := localAPI(layout)
	if err != nil {
		return fail(env, err)
	}
	if env.OS == "windows" {
		fmt.Fprint(env.Stdout, winsettings.PowerShellEnv(local.ListenPort, local.Username, local.Password, true))
	} else {
		fmt.Fprint(env.Stdout, shell.EnvOn(local.ListenPort, local.Username, local.Password))
	}
	return 0
}

func runInit(env Env, args []string) int {
	if len(args) != 1 {
		fmt.Fprintln(env.Stderr, i18n.T("sbc: init takes bash or zsh"))
		return 2
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	code, err := shell.Init(args[0], layout.CLIDir(), layout.EnvFile(), layout.UpgradeHint())
	if err != nil {
		return fail(env, err)
	}
	fmt.Fprint(env.Stdout, code)
	return 0
}

// runOn points shells, and the desktop after 'sbc desktop on', at the proxy,
// starting the service if it is stopped, so they never point at nothing.
func runOn(env Env) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	local, err := readLocal(layout)
	if err != nil {
		return fail(env, err)
	}
	if err := startService(env, layout); err != nil {
		return fail(env, err)
	}
	if err := enableEnvironment(env, layout); err != nil {
		return fail(env, err)
	}
	if env.OS == "windows" {
		windowsEnvironmentNotice(env)
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("Shells use the proxy from their next prompt."))
	}
	if err := switchDesktop(env, layout, local, true); err != nil {
		fmt.Fprintf(env.Stderr, i18n.T("sbc: the desktop proxy stays as it was: %v\n"), err)
		if env.OS == "windows" {
			return 1
		}
	}
	return 0
}

// runOff stops shells, and the desktop after 'sbc desktop on', from using the
// proxy and leaves sing-box running, so a program already using it, such as an
// agent, keeps its connection.
func runOff(env Env) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	if env.OS == "windows" {
		local, err := readLocal(layout)
		if err != nil {
			return fail(env, err)
		}
		if err := env.Windows.Move(local.ListenPort, 0, local.Username, local.Password); err != nil {
			return fail(env, err)
		}
	}
	if err := os.Remove(layout.EnvFile()); err != nil && !os.IsNotExist(err) {
		return fail(env, err)
	}
	if env.OS == "windows" {
		windowsEnvironmentNotice(env)
		fmt.Fprintln(env.Stdout, i18n.T("sing-box keeps running; 'sbc stop' stops it."))
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("Shells stop using the proxy from their next prompt. sing-box keeps running; 'sbc stop' stops it."))
	}
	if local, err := readLocal(layout); err == nil {
		// Over ssh there is no desktop session to change, and the shells are
		// off either way.
		if err := switchDesktop(env, layout, local, false); err != nil {
			fmt.Fprintf(env.Stderr, i18n.T("sbc: the desktop proxy stays as it was: %v\n"), err)
			if env.OS == "windows" {
				return 1
			}
		}
	}
	return 0
}

func runService(env Env, action string) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	manager, err := env.Service(layout.SBC())
	if err != nil {
		return fail(env, err)
	}
	switch action {
	case "start":
		err = manager.Start()
	case "stop":
		err = manager.Stop()
	default:
		err = manager.Restart()
	}
	if err != nil {
		return fail(env, err)
	}
	switch action {
	case "start":
		fmt.Fprintln(env.Stdout, i18n.T("sing-box is running."))
	case "stop":
		fmt.Fprintln(env.Stdout, i18n.T("sing-box is stopped."))
	default:
		fmt.Fprintln(env.Stdout, i18n.T("sing-box restarted."))
	}
	if action == "stop" {
		windowsOn := false
		if env.OS == "windows" {
			if local, err := readLocal(layout); err == nil {
				state, stateErr := env.Windows.State(local.ListenPort, local.Username, local.Password)
				windowsOn = stateErr == nil && state == i18n.T("on")
			}
		}
		if _, err := os.Stat(layout.EnvFile()); err == nil || windowsOn {
			fmt.Fprintln(env.Stdout, i18n.T("Shells still point at the proxy; 'sbc off' stops that."))
		} else if _, err := os.Stat(layout.DesktopFile()); err == nil {
			fmt.Fprintln(env.Stdout, i18n.T("The desktop may still point at the proxy; 'sbc off' stops that."))
		}
	}
	return 0
}

func runPort(env Env, args []string) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	_, local, err := localAPI(layout)
	if err != nil {
		return fail(env, err)
	}
	if len(args) == 0 {
		fmt.Fprintln(env.Stdout, local.ListenPort)
		return 0
	}
	port, err := strconv.Atoi(args[0])
	if err != nil || port < 1 || port > 65535 {
		return fail(env, i18n.Errorf("%q is not a port", args[0]))
	}
	if port == local.ListenPort {
		fmt.Fprintf(env.Stdout, i18n.T("The proxy already listens on 127.0.0.1:%d.\n"), port)
		return 0
	}
	if free, err := singbox.FreePort(port); err != nil || free != port {
		return fail(env, i18n.Errorf("port %d is in use", port))
	}
	manager, err := env.Service(layout.SBC())
	if err != nil {
		return fail(env, err)
	}
	previous := local.ListenPort
	desk, deskErr := env.Desktop()
	desktopState := desktop.Off
	if deskErr == nil {
		desktopState, deskErr = desk.State(previous)
	}
	if env.OS == "windows" && deskErr != nil {
		return fail(env, deskErr)
	}
	local.ListenPort = port
	if err := applyLocal(layout, manager, local); err != nil {
		return fail(env, err)
	}
	if env.OS == "windows" {
		if err := env.Windows.Move(previous, port, local.Username, local.Password); err != nil {
			local.ListenPort = previous
			if rollback := applyLocal(layout, manager, local); rollback != nil {
				return fail(env, fmt.Errorf("%w; %v", err, rollback))
			}
			return fail(env, err)
		}
	}
	if _, err := os.Stat(layout.EnvFile()); err == nil {
		if err := writeEnvFile(layout); err != nil {
			return fail(env, err)
		}
	}
	if deskErr == nil && desktopState == desktop.On {
		if err := desk.On(port); err != nil {
			if env.OS == "windows" {
				rollbackEnv := env.Windows.Move(port, previous, local.Username, local.Password)
				local.ListenPort = previous
				rollbackConfig := applyLocal(layout, manager, local)
				return fail(env, errors.Join(err, rollbackEnv, rollbackConfig))
			}
			return fail(env, err)
		}
		fmt.Fprintf(env.Stdout, i18n.T("The desktop proxy (%s) moved with it.\n"), desk.Name())
	}
	fmt.Fprintf(env.Stdout, i18n.T("The proxy listens on 127.0.0.1:%d.\n"), port)
	if daemon, err := env.Docker(); err == nil {
		if state, err := daemon.State(port); err == nil && state == docker.Stale {
			fmt.Fprintf(env.Stdout, i18n.T("The Docker daemon still uses port %d; 'sbc docker on' moves it, which restarts Docker.\n"), previous)
		}
	}
	return 0
}

// applyLocal rewrites the config with new local settings, checks it, and
// restarts a running sing-box, restoring the old config if that fails.
func applyLocal(layout paths.Layout, manager interface {
	Active() bool
	Restart() error
}, local singbox.Local) error {
	current, err := os.ReadFile(layout.ConfigFile())
	if err != nil {
		return err
	}
	patched, err := singbox.Patch(current, local)
	if err != nil {
		return err
	}
	staged := layout.ConfigFile() + ".new"
	if err := paths.WriteFile(staged, patched, 0o600); err != nil {
		return err
	}
	if err := singbox.Check(layout.SingBox(), layout.Data, staged); err != nil {
		os.Remove(staged)
		return err
	}
	if err := os.Rename(staged, layout.ConfigFile()); err != nil {
		return err
	}
	if !manager.Active() {
		return nil
	}
	if err := manager.Restart(); err == nil {
		client, _, err := localAPI(layout)
		if err == nil && client.WaitReady(10*time.Second) == nil {
			return nil
		}
	}
	paths.WriteFile(layout.ConfigFile(), current, 0o600)
	manager.Restart()
	return i18n.New("sing-box did not start with the change, so the previous config is back")
}

func runLink(env Env, args []string) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	if len(args) == 0 {
		link, err := install.ReadLink(layout)
		if err != nil {
			return fail(env, err)
		}
		fmt.Fprintln(env.Stdout, link.Masked())
		return 0
	}
	if args[0] != "set" {
		fmt.Fprintln(env.Stderr, i18n.T("sbc: link takes no argument, or set"))
		return 2
	}
	prompt(env, "New subscription link: ")
	line, err := bufio.NewReader(env.Stdin).ReadString('\n')
	if err != nil && line == "" {
		return fail(env, i18n.New("no subscription link was given"))
	}
	link, err := install.ParseLink(line)
	if err != nil {
		return fail(env, err)
	}
	if err := paths.WriteFile(layout.Link(), []byte(link.String()+"\n"), 0o600); err != nil {
		return fail(env, err)
	}
	os.Remove(layout.ETag())
	return runUpdate(env)
}

func runUpgrade(env Env) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	link, err := install.ReadLink(layout)
	if err != nil {
		return fail(env, err)
	}
	upgraded, err := env.Installer(layout, env.Stdout).Upgrade(link, env.Version)
	if err != nil {
		return fail(env, err)
	}
	if !upgraded.Any() {
		os.Remove(layout.UpgradeHint())
		fmt.Fprintln(env.Stdout, i18n.T("sbc and sing-box are up to date."))
		return 0
	}
	if upgraded.SingBox != "" {
		fmt.Fprintf(env.Stdout, i18n.T("Installed sing-box %s.\n"), upgraded.SingBox)
	}
	if upgraded.SBC != "" {
		fmt.Fprintf(env.Stdout, i18n.T("Installed sbc %s.\n"), upgraded.SBC)
	}
	// A new sing-box may need a config built for it, and the refresh restarts
	// the service either way.
	if manager, err := env.Service(layout.SBC()); err == nil && manager.Active() {
		manager.Restart()
	}
	os.Remove(layout.ETag())
	os.Remove(layout.UpgradeHint())
	return runUpdate(env)
}

// enableEnvironment writes persistent Windows settings or the Unix shell hook file.
func enableEnvironment(env Env, layout paths.Layout) error {
	if env.OS == "windows" {
		local, err := readLocal(layout)
		if err != nil {
			return err
		}
		return env.Windows.On(local.ListenPort, local.Username, local.Password)
	}
	return writeEnvFile(layout)
}

func windowsEnvironmentNotice(env Env) {
	fmt.Fprintln(env.Stdout, i18n.T("Windows user environment updated. Open a new terminal from Start; restart existing terminals and editors to use it."))
}
