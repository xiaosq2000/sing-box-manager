package cli

import (
	"bufio"
	"fmt"
	"os"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
	"github.com/xiaosq2000/sing-box-manager/internal/docker"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

// errPasswordOnDesktop explains why a proxy with a password stays off the
// desktop: its proxy settings have nowhere to keep one.
var errPasswordOnDesktop = i18n.New("this proxy asks for a password, which desktop proxy settings cannot hold; 'sbc install --auth off' turns the password off")

// errPasswordOnDocker explains why a proxy with a password stays off the
// Docker daemon, which every account on the machine uses.
var errPasswordOnDocker = i18n.New("this proxy asks for a password because other accounts use this machine, and so does its Docker daemon, so sbc keeps the daemon off the proxy")

func isTerminal(stream any) bool {
	file, ok := stream.(*os.File)
	if !ok {
		return false
	}
	info, err := file.Stat()
	return err == nil && info.Mode()&os.ModeCharDevice != 0
}

func readLocal(layout paths.Layout) (singbox.Local, error) {
	_, local, err := localAPI(layout)
	return local, err
}

func runDesktop(env Env, args []string) int {
	if len(args) > 1 || (len(args) == 1 && args[0] != "on" && args[0] != "off") {
		fmt.Fprintln(env.Stderr, i18n.T("sbc: desktop takes on or off"))
		return 2
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	local, err := readLocal(layout)
	if err != nil {
		return fail(env, err)
	}
	desk, err := env.Desktop()
	if err != nil {
		return fail(env, err)
	}
	state, err := desk.State(local.ListenPort)
	if err != nil {
		return fail(env, err)
	}
	if len(args) == 0 {
		fmt.Fprintf(env.Stdout, "%s (%s)\n", state, desk.Name())
		return 0
	}
	if args[0] == "off" {
		switch state {
		case desktop.On:
			if err := desk.Off(); err != nil {
				return fail(env, err)
			}
			fmt.Fprintf(env.Stdout, i18n.T("The desktop proxy (%s) is off.\n"), desk.Name())
		case desktop.Other:
			fmt.Fprintf(env.Stdout, i18n.T("The desktop proxy (%s) points at another proxy, so sbc left it alone.\n"), desk.Name())
		default:
			fmt.Fprintf(env.Stdout, i18n.T("The desktop proxy (%s) is already off.\n"), desk.Name())
		}
		if err := os.Remove(layout.DesktopFile()); err != nil && !os.IsNotExist(err) {
			return fail(env, err)
		}
		return 0
	}
	if env.OS == "windows" && state == desktop.Other {
		return fail(env, i18n.New("Windows desktop settings belong to another proxy or PAC; clear them in Proxy settings before running 'sbc desktop on'"))
	}
	if local.Username != "" {
		return fail(env, errPasswordOnDesktop)
	}
	// The desktop must never point at a proxy that is not running.
	if err := startService(env, layout); err != nil {
		return fail(env, err)
	}
	if err := desk.On(local.ListenPort); err != nil {
		return fail(env, err)
	}
	if err := paths.WriteFile(layout.DesktopFile(), nil, 0o600); err != nil {
		return fail(env, err)
	}
	fmt.Fprintf(env.Stdout, i18n.T("The desktop proxy (%s) is on. 'sbc on' and 'sbc off' now switch it along with shells.\n"), desk.Name())
	return 0
}

// switchDesktop points the desktop at the proxy or away from it, after
// 'sbc desktop on' asked that it follow 'sbc on' and 'sbc off'. A desktop that
// points at another proxy stays as it is.
func switchDesktop(env Env, layout paths.Layout, local singbox.Local, on bool) error {
	if _, err := os.Stat(layout.DesktopFile()); err != nil {
		return nil
	}
	desk, err := env.Desktop()
	if err != nil {
		return err
	}
	state, err := desk.State(local.ListenPort)
	if err != nil {
		return err
	}
	switch {
	case state == desktop.Other:
		fmt.Fprintf(env.Stdout, i18n.T("The desktop proxy (%s) points at another proxy, so sbc left it alone.\n"), desk.Name())
	// Repair browser policies even when the desktop already uses this proxy.
	case on:
		if local.Username != "" {
			return errPasswordOnDesktop
		}
		if err := desk.On(local.ListenPort); err != nil {
			return err
		}
		fmt.Fprintf(env.Stdout, i18n.T("The desktop proxy (%s) is on.\n"), desk.Name())
	case !on && state == desktop.On:
		if err := desk.Off(); err != nil {
			return err
		}
		fmt.Fprintf(env.Stdout, i18n.T("The desktop proxy (%s) is off.\n"), desk.Name())
	}
	return nil
}

// runWebRTC is separate from proxy switching so an explicit opt-out persists.
// It can also clean browser settings after an interrupted installation.
func runWebRTC(env Env, args []string) int {
	if len(args) != 1 || (args[0] != "on" && args[0] != "off") {
		fmt.Fprintln(env.Stderr, i18n.T("sbc: webrtc takes on or off"))
		return 2
	}
	privacy, err := env.Privacy()
	if err != nil {
		return fail(env, err)
	}
	if err := privacy.SetWebRTC(args[0] == "on"); err != nil {
		return fail(env, err)
	}
	if args[0] == "on" {
		if env.OS == "darwin" {
			fmt.Fprintln(env.Stdout, i18n.T("Safari WebRTC protection is not managed by sbc."))
		}
		fmt.Fprintln(env.Stdout, i18n.T("WebRTC protection is configured and stays enabled across proxy toggles. Restart open browsers to apply the changes."))
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("Managed WebRTC settings were removed. Automatic setup is off. Use 'sbc webrtc on' to enable it again. Restart open browsers to apply the changes."))
		if err := reportRemainingWebRTC(env, privacy); err != nil {
			return fail(env, err)
		}
	}
	return 0
}

func reportRemainingWebRTC(env Env, privacy desktop.BrowserPrivacy) error {
	remaining, err := privacy.RemainingWebRTC()
	if err != nil {
		return err
	}
	if len(remaining) != 0 {
		fmt.Fprintf(env.Stdout, i18n.T("Browser settings not removed automatically:\n  %s\nSee the WebRTC guide's manual WebRTC cleanup instructions. Keep policies required by your administrator.\n"), strings.Join(remaining, "\n  "))
	}
	return nil
}

func runDocker(env Env, args []string) int {
	action, yes := "", false
	for _, arg := range args {
		switch {
		case arg == "--yes":
			yes = true
		case (arg == "on" || arg == "off") && action == "":
			action = arg
		default:
			fmt.Fprintln(env.Stderr, i18n.T("sbc: docker takes on or off, and --yes"))
			return 2
		}
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	local, err := readLocal(layout)
	if err != nil {
		return fail(env, err)
	}
	daemon, err := env.Docker()
	if err != nil {
		return fail(env, err)
	}
	state, err := daemon.State(local.ListenPort)
	if err != nil {
		return fail(env, err)
	}
	if action == "" {
		fmt.Fprintln(env.Stdout, state)
		return 0
	}
	if state == docker.Other {
		return fail(env, i18n.Errorf("%s holds settings sbc did not write, so sbc leaves the Docker daemon alone", daemon.Path))
	}
	if action == "off" {
		if state == docker.Off {
			fmt.Fprintln(env.Stdout, i18n.T("The Docker daemon does not use the proxy."))
			return 0
		}
		if !confirmRestart(env, yes) {
			return 1
		}
		if err := daemon.Off(); err != nil {
			return fail(env, err)
		}
		fmt.Fprintln(env.Stdout, i18n.T("The Docker daemon no longer uses the proxy, and Docker restarted."))
		return 0
	}
	if local.Username != "" {
		return fail(env, errPasswordOnDocker)
	}
	if state == docker.On {
		fmt.Fprintln(env.Stdout, i18n.T("The Docker daemon already uses the proxy."))
		return 0
	}
	if !confirmRestart(env, yes) {
		return 1
	}
	if err := startService(env, layout); err != nil {
		return fail(env, err)
	}
	if err := daemon.On(local.ListenPort); err != nil {
		return fail(env, err)
	}
	fmt.Fprintf(env.Stdout, i18n.T("The Docker daemon uses the proxy on 127.0.0.1:%d, and Docker restarted.\n"), local.ListenPort)
	return 0
}

// confirmRestart asks before Docker restarts, because a restart stops the
// running containers that have no restart policy. Scripts and agents, which
// have no terminal to answer on, pass --yes.
func confirmRestart(env Env, yes bool) bool {
	warning := i18n.T("This restarts Docker, which stops running containers that have no restart policy.")
	if yes {
		return true
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

// startService starts sing-box when it is stopped, so nothing points at a
// proxy that is not there.
func startService(env Env, layout paths.Layout) error {
	manager, err := env.Service(layout.SBC())
	if err != nil {
		return err
	}
	if manager.Active() {
		return nil
	}
	if err := manager.Start(); err != nil {
		return i18n.Errorf("start the service: %w", err)
	}
	return nil
}
