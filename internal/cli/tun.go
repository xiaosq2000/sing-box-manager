package cli

import (
	"errors"
	"fmt"
	"os"
	"os/user"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/docker"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

// runTunOn checks the host, fetches the additive TUN config, stages the local
// files and asks root to publish and verify the service.
func runTunOn(env Env) int {
	host := tun.SystemHost()
	if env.TunHost != nil {
		host = env.TunHost()
	}
	if err := tun.Check(host); err != nil {
		return fail(env, err)
	}
	if env.Sudo == nil {
		return fail(env, i18n.New("sudo is not available in this environment"))
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	link, err := install.ReadLink(layout)
	if err != nil {
		return fail(env, i18n.New("run 'sbc install' before enabling Linux TUN"))
	}
	versionData, err := os.ReadFile(layout.SingBoxVersion())
	if err != nil {
		return fail(env, i18n.New("run 'sbc install' before enabling Linux TUN"))
	}
	version := strings.TrimSpace(string(versionData))
	installer := env.Installer(layout, env.Stdout)
	config, err := installer.FetchTun(link, version)
	if err != nil {
		return fail(env, err)
	}
	apiPort, err := singbox.FreePort(0)
	if err != nil {
		return fail(env, err)
	}
	secret, err := tun.RandomSecret()
	if err != nil {
		return fail(env, err)
	}
	rootPaths := tun.DefaultPaths()
	staged, cleanup, err := tun.Prepare(layout, config, tun.Local{
		APIPort:   apiPort,
		APISecret: secret,
		CachePath: rootPaths.Cache,
		RulesDir:  rootPaths.RulesDir,
	})
	if err != nil {
		return fail(env, err)
	}
	defer cleanup()

	if err := clearMixedProxy(env, layout); err != nil {
		return fail(env, i18n.Errorf("Linux TUN was not started: %w", err))
	}
	self, err := env.Self()
	if err != nil {
		return fail(env, err)
	}
	account, err := user.Current()
	if err != nil {
		return fail(env, i18n.New("cannot find the current user"))
	}
	transaction, err := tun.RandomSecret()
	if err != nil {
		return fail(env, err)
	}
	args := []string{
		"_tun-apply",
		"--transaction", transaction,
		"--config", staged.Config,
		"--bin-dir", staged.BinDir,
		"--rules", staged.RulesDir,
		"--owner-uid", strconv.Itoa(os.Getuid()),
		"--owner-user", account.Username,
	}
	if err := env.Sudo(self, args); err != nil {
		return fail(env, i18n.Errorf("Linux TUN setup failed: %w; the mixed proxy remains off", err))
	}
	state := tun.State{
		APIPort:   apiPort,
		APISecret: secret,
		Interface: tun.InterfaceName,
		Since:     time.Now().UTC().Format(time.RFC3339),
	}
	if err := finishTunActivation(env, layout, self, transaction, state); err != nil {
		return fail(env, err)
	}
	fmt.Fprintf(env.Stdout, i18n.T("Linux TUN is active on %s. Traffic is captured; 'sbc tun off' stops it and leaves the mixed proxy off.\n"), tun.InterfaceName)
	return 0
}

// finishTunActivation closes the user/root commit boundary. The independent
// recovery deadline remains armed until root accepts the saved user state.
func finishTunActivation(env Env, layout paths.Layout, self, transaction string, state tun.State) error {
	err := tun.WriteState(layout, state)
	if err == nil {
		err = env.Sudo(self, []string{"_tun-commit", "--transaction", transaction})
	}
	if err == nil {
		return nil
	}
	if stopErr := env.Sudo(self, []string{"_tun-off"}); stopErr != nil {
		return errors.Join(i18n.New("TUN commit failed. Recovery is pending. Run 'sbc tun off'"), err, stopErr)
	}
	return errors.Join(i18n.New("TUN commit failed. The TUN was stopped and the mixed proxy remains off"), err, tun.RemoveState(layout))
}

// runTunOff asks root to stop and disable the service. It never starts the
// mixed proxy.
func runTunOff(env Env) int {
	if env.OS != "linux" {
		return fail(env, i18n.New("Linux TUN is available only on native Linux"))
	}
	if env.Sudo == nil {
		return fail(env, i18n.New("sudo is not available in this environment"))
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	self, err := env.Self()
	if err != nil {
		return fail(env, err)
	}
	if err := env.Sudo(self, []string{"_tun-off"}); err != nil {
		return fail(env, i18n.Errorf("Linux TUN removal failed: %w", err))
	}
	if err := tun.RemoveState(layout); err != nil {
		return fail(env, err)
	}
	fmt.Fprintln(env.Stdout, i18n.T("Linux TUN is off. The mixed proxy is not started; run 'sbc on' to use it."))
	return 0
}

// runTunApply is the root-side hidden command.
func runTunApply(env Env, config, binDir, rules string, ownerUID int, ownerUser, transaction string) int {
	if err := tun.Apply(tun.Options{
		Paths:       tun.DefaultPaths(),
		Config:      config,
		BinDir:      binDir,
		RulesDir:    rules,
		OwnerUID:    ownerUID,
		OwnerUser:   ownerUser,
		Transaction: transaction,
	}); err != nil {
		return fail(env, err)
	}
	fmt.Fprintln(env.Stdout, i18n.T("Linux TUN service verified."))
	return 0
}

// runTunOffRoot is the root-side hidden stop command.
func runTunOffRoot(env Env) int {
	if err := tun.Stop(tun.StopOptions{Paths: tun.DefaultPaths()}); err != nil {
		return fail(env, err)
	}
	return 0
}

// clearMixedProxy stops the mixed service and removes the proxy settings sbc
// owns. The TUN captures everything, so a leftover local proxy would make
// programs bypass the tunnel.
func clearMixedProxy(env Env, layout paths.Layout) error {
	if manager, err := env.Service(layout.SBC()); err == nil && manager.Active() {
		if err := manager.Stop(); err != nil {
			return i18n.Errorf("stop the mixed service: %w", err)
		}
	}
	if err := os.Remove(layout.EnvFile()); err != nil && !os.IsNotExist(err) {
		return err
	}
	local, err := readLocal(layout)
	if err != nil {
		return nil
	}
	if err := switchDesktop(env, layout, local, false); err != nil {
		return err
	}
	daemon, err := env.Docker()
	if err != nil {
		return nil
	}
	state, err := daemon.State(local.ListenPort)
	if err != nil {
		return nil
	}
	if state == docker.On || state == docker.Stale {
		if err := daemon.Off(); err != nil {
			return i18n.Errorf("clear the Docker daemon proxy: %w", err)
		}
	}
	return nil
}
