package cli

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

func withOperation(env Env, run func(Env) int) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	unlock, err := maintenance.Lock(layout)
	if err != nil {
		return fail(env, err)
	}
	defer unlock()
	guard, err := newStartupGuard(env, layout)
	if err != nil {
		return fail(env, err)
	}
	defer guard.close()
	return run(guard.scope(env))
}

func (g *startupGuard) scope(env Env) Env {
	env.startup = g
	factory := env.Installer
	env.Installer = func(layout paths.Layout, log io.Writer) *install.Installer {
		installer := *factory(layout, log)
		installer.BeforeRestore = g.beforeRestore
		return &installer
	}
	return env
}

func withRecovery(env Env, run func(Env) int) int {
	return withOperation(env, func(env Env) int {
		layout, err := env.Layout()
		if err != nil {
			return fail(env, err)
		}
		if err = recoverMaintenance(env, layout); err != nil {
			return fail(env, err)
		}
		if err = env.startup.settle(); err != nil {
			return fail(env, err)
		}
		return run(env)
	})
}

// schedulingError separates native scheduler failures from invalid settings and
// failed state writes. Manual maintenance can proceed only for the former.
type schedulingError struct{ error }

func tryMaintenanceSchedule(env Env, layout paths.Layout) error {
	err := ensureMaintenance(env, layout, false)
	var unavailable schedulingError
	if errors.As(err, &unavailable) {
		fmt.Fprintln(env.Stderr, i18n.Errorf("Background scheduling failed: %w", err))
		return nil
	}
	return err
}

func ensureMaintenance(env Env, layout paths.Layout, force bool) error {
	settings, err := maintenance.Load(layout)
	if err != nil {
		return err
	}
	if settings.Scheduled && !force {
		return nil
	}
	manager, err := env.Service(layout.SBC())
	if err != nil {
		return err
	}
	if err = manager.Schedule(settings); err != nil {
		return schedulingError{err}
	}
	settings.Scheduled = true
	if err = settings.Save(layout); err != nil {
		return err
	}
	// Retirement is last: an old launchd refresh may terminate itself here.
	if err = manager.RetireRefresh(); err != nil {
		return schedulingError{err}
	}
	return nil
}

func maintenanceCommands(parent *cobra.Command, env Env, exit *int, action string) {
	parent.AddGroup(&cobra.Group{ID: "maintenance", Title: i18n.T("Background policy:")})
	auto := &cobra.Command{GroupID: "maintenance", Use: "auto [on|off]", Short: i18n.T("Show or change the background policy"), Args: choiceArgs(0, "on", "off"), ValidArgsFunction: fixedChoices("on", "off"), Run: func(_ *cobra.Command, args []string) { *exit = runMaintenanceSetting(env, action, "auto", args) }}
	interval := &cobra.Command{GroupID: "maintenance", Use: "interval [duration]", Short: i18n.T("Show or change the interval (1m to 720h)"), Args: argumentRange(0, 1), ValidArgsFunction: fixedChoices(), Run: func(_ *cobra.Command, args []string) { *exit = runMaintenanceSetting(env, action, "interval", args) }}
	interval.Args = func(cmd *cobra.Command, args []string) error {
		if err := argumentRange(0, 1)(cmd, args); err != nil {
			return err
		}
		if len(args) > 0 {
			_, err := maintenance.Interval(args[0])
			return err
		}
		return nil
	}
	for _, cmd := range []*cobra.Command{auto, interval} {
		cmd.Flags().BoolP("help", "h", false, i18n.T("Show help for this command"))
		parent.AddCommand(cmd)
	}
}

func runMaintenanceSetting(env Env, action, field string, args []string) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	if len(args) == 0 {
		settings, err := maintenance.Load(layout)
		if err != nil {
			return fail(env, err)
		}
		p := settings.Policy(action)
		if field == "auto" {
			fmt.Fprintln(env.Stdout, map[bool]string{true: "on", false: "off"}[p.Enabled])
		} else {
			fmt.Fprintln(env.Stdout, p.Interval())
		}
		return 0
	}
	var seconds int64
	if field == "interval" {
		seconds, err = maintenance.Interval(args[0])
		if err != nil {
			return fail(env, err)
		}
	}
	return withOperation(env, func(env Env) int {
		if _, err := os.Stat(layout.ConfigFile()); err != nil {
			return fail(env, i18n.New("sbc is not installed; run 'sbc install'"))
		}
		settings, err := maintenance.Load(layout)
		if err != nil {
			return fail(env, err)
		}
		p := settings.Policy(action)
		if field == "auto" {
			p.Enabled = args[0] == "on"
		} else {
			p.Seconds = seconds
		}
		settings.Scheduled = false
		if err = settings.Save(layout); err != nil {
			return fail(env, err)
		}
		if err = ensureMaintenance(env, layout, true); err != nil {
			return fail(env, i18n.Errorf("The policy was saved, but scheduling failed: %w", err))
		}
		settings.Scheduled = true
		printMaintenance(env, settings)
		return 0
	})
}

func runMaintenance(env Env, action string, background bool) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	// Concurrent refresh and upgrade schedules can wake together. Wait for the
	// other operation rather than starving the same task every launchd interval.
	deadline := time.Now().Add(10 * time.Minute)
	var unlock func()
	for {
		unlock, err = maintenance.Lock(layout)
		if !background || !errors.Is(err, maintenance.ErrBusy) || time.Now().After(deadline) {
			break
		}
		time.Sleep(time.Second)
	}
	if err != nil {
		return fail(env, err)
	}
	defer unlock()
	settings, err := maintenance.Load(layout)
	if err != nil {
		return fail(env, err)
	}
	p := settings.Policy(action)
	if background && !p.Enabled {
		return 0
	}
	targetTun, err := tunMaintenanceTarget(env, layout)
	if err != nil {
		return fail(env, err)
	}
	if targetTun {
		if !background {
			guard, err := tunMaintenanceGuard(env, layout)
			if err != nil {
				return fail(env, err)
			}
			defer guard.close()
			if err = tryMaintenanceSchedule(env, layout); err != nil {
				return fail(env, err)
			}
			settings, err = maintenance.Load(layout)
			if err != nil {
				return fail(env, err)
			}
		}
		return maintenanceAttempt(env, layout, settings, action, func() int {
			return runTunMaintenance(env, layout, action, background)
		})
	}
	guard, err := newStartupGuard(env, layout)
	if err != nil {
		return fail(env, err)
	}
	defer guard.close()
	env = guard.scope(env)
	if err := recoverMaintenance(env, layout); err != nil {
		return fail(env, err)
	}
	if err = guard.settle(); err != nil {
		return fail(env, err)
	}
	if _, err = os.Stat(layout.ConfigFile()); err != nil {
		return fail(env, i18n.New("sbc is not installed; run 'sbc install'"))
	}
	if !background {
		if err = tryMaintenanceSchedule(env, layout); err != nil {
			return fail(env, err)
		}
		settings, err = maintenance.Load(layout)
		if err != nil {
			return fail(env, err)
		}
		p = settings.Policy(action)
	}
	return maintenanceAttempt(env, layout, settings, action, func() int {
		if action == "refresh" {
			return runRefresh(env)
		}
		return runUpgrade(env)
	})
}

func maintenanceAttempt(env Env, layout paths.Layout, settings maintenance.Settings, action string, run func() int) int {
	p := settings.Policy(action)
	p.LastAttempt = time.Now().UTC()
	p.Result = "running"
	if err := settings.Save(layout); err != nil {
		return fail(env, err)
	}
	code := run()
	p.Result = "failed"
	if code == 0 {
		p.LastSuccess = time.Now().UTC()
		p.Result = "ok"
	}
	if err := settings.Save(layout); err != nil {
		return fail(env, err)
	}
	return code
}

func printMaintenance(env Env, settings maintenance.Settings) {
	for _, action := range []string{"refresh", "upgrade"} {
		p := settings.Policy(action)
		enabled := i18n.T("off")
		if p.Enabled {
			enabled = i18n.T("on")
		}
		result := i18n.T("not run yet")
		if p.Result != "" {
			state := i18n.T("failed")
			if p.Result == "ok" {
				state = i18n.T("ok")
			} else if p.Result == "running" {
				state = i18n.T("running or interrupted")
			}
			result = p.LastAttempt.Local().Format("2006-01-02 15:04") + " " + state
		}
		printStatusLine(env, fmt.Sprintf(i18n.T("%s: auto %s, interval %s, last attempt %s\n"), action, enabled, p.Interval(), result))
	}
	if !settings.Scheduled {
		printStatusLine(env, i18n.T("Background schedules need setup. Set either auto policy to retry.")+"\n")
	}
}

// initializeMaintenance runs only with operation, shared permission, and engine
// leases held by prepareStartup. An authorized restart skips this setup.
func initializeMaintenance(env Env, layout paths.Layout) error {
	if _, err := os.Stat(layout.ConfigFile()); err != nil {
		if _, pending := os.Stat(install.RecoveryFile(layout)); os.IsNotExist(pending) {
			return nil
		}
	}
	if _, err := os.Stat(install.RecoveryFile(layout)); !os.IsNotExist(err) {
		if _, err := env.Installer(layout, env.Stdout).Recover(nil, nil); err != nil {
			return err
		}
	}
	if _, err := os.Stat(layout.SBCVersion()); os.IsNotExist(err) {
		if err := paths.WriteFile(layout.SBCVersion(), []byte(env.Version+"\n"), 0o600); err != nil {
			return err
		}
	}
	return tryMaintenanceSchedule(env, layout)
}

// Old scheduled tasks may be the first new executable invocation on a stopped
// installation. This migration entry is hidden and never acts as a refresh alias.
func runLegacyUpdate(env Env) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	settings, err := maintenance.Load(layout)
	if err != nil {
		return fail(env, err)
	}
	if !settings.Scheduled {
		return withOperation(env, func(env Env) int {
			if _, err := os.Stat(layout.ConfigFile()); err != nil {
				return fail(env, i18n.New("sbc is not installed; run 'sbc install'"))
			}
			if err := ensureMaintenance(env, layout, true); err != nil {
				return fail(env, err)
			}
			fmt.Fprintln(env.Stdout, i18n.T("Background tasks migrated. Use 'sbc refresh' to refresh configuration."))
			return 0
		})
	}
	return withOperation(env, func(env Env) int {
		manager, err := env.Service(layout.SBC())
		if err != nil {
			return fail(env, err)
		}
		if err = manager.RetireRefresh(); err != nil {
			return fail(env, err)
		}
		return fail(env, i18n.New("'sbc update' is retired. Use 'sbc refresh' or 'sbc upgrade'"))
	})
}

// Older updater processes can write their stale hint after the new proxy starts.
// Suppress that hint using installed versions, including during shell initialization.
func discardStaleUpgradeHint(layout paths.Layout, version string) {
	if _, err := os.Stat(layout.UpgradeHint()); err != nil {
		return
	}
	unlock, err := maintenance.Lock(layout)
	if err != nil {
		return
	}
	defer unlock()
	if _, err := os.Stat(install.RecoveryFile(layout)); !os.IsNotExist(err) {
		return
	}
	data, err := os.ReadFile(layout.UpgradeHint())
	if err != nil {
		return
	}
	parts := strings.SplitN(strings.TrimSuffix(string(data), "\n"), " ", 2)
	if len(parts) != 2 {
		return
	}
	core, _ := os.ReadFile(layout.SingBoxVersion())
	if (parts[0] == "" || parts[0] == installedSBCVersion(layout, version)) && (parts[1] == "" || parts[1] == strings.TrimSpace(string(core))) {
		_ = os.Remove(layout.UpgradeHint())
	}
}

func maintenanceHooks(env Env, layout paths.Layout) (func() error, func() error, error) {
	manager, err := env.Service(layout.SBC())
	if err != nil {
		return nil, nil, err
	}
	active := manager.Active()
	restart := func() error {
		if !active {
			return nil
		}
		return startManaged(env, layout, manager, true)
	}
	// startManaged retains its permission grant until readiness completes.
	ready := func() error { return nil }
	return restart, ready, nil
}

func recoverMaintenance(env Env, layout paths.Layout) error {
	if _, err := os.Stat(install.RecoveryFile(layout)); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return err
	}
	installer := env.Installer(layout, env.Stdout)
	recovery, err := installer.PendingTunUpdate()
	if err != nil {
		return err
	}
	if recovery != nil {
		return i18n.New("A TUN update needs explicit recovery. Run 'sbc refresh' at the machine's own console.")
	}
	restart, ready, err := maintenanceHooks(env, layout)
	if err != nil {
		return err
	}
	manager, err := env.Service(layout.SBC())
	if err != nil {
		return err
	}
	installer.BeforeRestore = func() error { return env.startup.stop(manager) }
	_, err = installer.Recover(restart, ready)
	if err == nil {
		fmt.Fprintln(env.Stdout, i18n.T("Recovered the previous binaries and configuration after an interrupted upgrade."))
	}
	return err
}

func installedSBCVersion(layout paths.Layout, fallback string) string {
	if data, err := os.ReadFile(layout.SBCVersion()); err == nil && strings.TrimSpace(string(data)) != "" {
		return strings.TrimSpace(string(data))
	}
	return fallback
}
