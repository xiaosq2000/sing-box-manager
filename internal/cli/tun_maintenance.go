package cli

import (
	"errors"
	"fmt"
	"os"
	"os/user"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/tun"
)

func tunCapture(env Env) tun.CaptureState {
	if env.TunCapture != nil {
		return env.TunCapture()
	}
	return tun.Current()
}

// tunActive preserves the production guard and lets fixtures supply host state.
func tunActive(env Env) bool {
	if env.TunCapture != nil {
		return env.TunCapture().Status != "off"
	}
	return tun.Active()
}

// tunMaintenanceTarget identifies TUN before any mixed recovery hook runs.
func tunMaintenanceTarget(env Env, layout paths.Layout) (bool, error) {
	if env.OS != "linux" {
		return false, nil
	}
	if tunCapture(env).Status != "off" {
		return true, nil
	}
	if _, err := os.Stat(install.RecoveryFile(layout)); os.IsNotExist(err) {
		return false, nil
	} else if err != nil {
		return false, err
	}
	recovery, err := env.Installer(layout, env.Stdout).PendingTunUpdate()
	return recovery != nil, err
}

func runTunMaintenance(env Env, layout paths.Layout, action string, background bool) int {
	installer := env.Installer(layout, env.Stdout)
	recovery, err := installer.PendingTunUpdate()
	if err != nil {
		return fail(env, err)
	}
	if background {
		if recovery != nil {
			return fail(env, i18n.New("Interrupted TUN maintenance needs manual recovery. Run 'sbc refresh' at the machine's own console."))
		}
		return checkTunMaintenance(env, layout, installer)
	}
	host := tun.SystemHost()
	if env.TunHost != nil {
		host = env.TunHost()
	}
	if err = tun.Check(host); err != nil {
		return fail(env, err)
	}
	if env.Sudo == nil {
		return fail(env, i18n.New("sudo is not available in this environment"))
	}
	if recovery != nil {
		stopped, err := recoverTunMaintenance(env, layout, recovery)
		if err != nil {
			return fail(env, err)
		}
		if stopped {
			return fail(env, i18n.New("Interrupted TUN maintenance was recovered. Capture is off. Upgrade the stopped client if needed. Run 'sbc tun on' to enable TUN."))
		}
	}
	capture := tunCapture(env)
	if capture.Status != "active" || capture.Phase != "active" || capture.Transaction == "" {
		return fail(env, i18n.New("TUN maintenance needs a committed active generation. Run 'sbc tun off' to repair dirty state."))
	}
	state, err := tun.ReadState(layout)
	if err != nil {
		return fail(env, i18n.New("The active TUN has no saved API state for this user. Run 'sbc tun off' as its owner."))
	}
	client := api.New(state.APIPort, state.APISecret)
	mode, _, err := client.Modes()
	if err != nil {
		return fail(env, err)
	}
	protocol, _, err := client.Selected(api.ProxySelector)
	if err != nil {
		return fail(env, err)
	}
	selection := tun.Selection{Mode: mode, Protocol: protocol}
	link, err := install.ReadLink(layout)
	if err != nil {
		return fail(env, err)
	}
	self, err := env.Self()
	if err != nil {
		return fail(env, err)
	}
	plan, err := installer.PrepareTunUpdate(link, installedSBCVersion(layout, env.Version), self, action == "upgrade")
	if err != nil {
		return fail(env, err)
	}
	defer plan.Close()
	sourceHash, err := tun.InstalledFingerprint(plan.Layout, plan.Envelope.ConfigTun)
	if err != nil {
		return fail(env, err)
	}
	transaction, err := tun.RandomSecret()
	if err != nil {
		return fail(env, err)
	}
	port, err := singbox.FreePort(0)
	if err != nil {
		return fail(env, err)
	}
	secret, err := tun.RandomSecret()
	if err != nil {
		return fail(env, err)
	}
	root := tun.DefaultPaths()
	staged, cleanup, err := tun.Prepare(plan.Layout, plan.Envelope.ConfigTun, tun.Local{
		APIPort: port, APISecret: secret, CachePath: root.Cache, RulesDir: root.RulesDir,
		Selection: selection, CacheID: "sbc-tun-" + transaction,
	})
	if err != nil {
		return fail(env, err)
	}
	defer cleanup()
	account, err := user.Current()
	if err != nil {
		return fail(env, err)
	}
	if err = plan.Begin(transaction, capture.Transaction, mode, protocol); err != nil {
		return fail(env, err)
	}
	controller := plan.Layout.SBC()
	args := []string{"_tun-apply", "--transaction", transaction, "--config", staged.Config,
		"--bin-dir", staged.BinDir, "--rules", staged.RulesDir,
		"--owner-uid", strconv.Itoa(os.Getuid()), "--owner-user", account.Username,
		"--route", mode, "--protocol", protocol}
	if err = env.Sudo(controller, args); err != nil {
		return failTunMaintenance(env, layout, plan, err)
	}
	if err = plan.Publish(); err == nil {
		inboundTag, ipv6Policy := tun.DiagnosticMetadata(plan.Envelope.ConfigTun)
		err = tun.WriteState(layout, tun.State{APIPort: port, APISecret: secret, Interface: tun.InterfaceName,
			Transaction: transaction, InboundTag: inboundTag, IPv6Policy: ipv6Policy,
			Since: time.Now().UTC().Format(time.RFC3339), SourceHash: sourceHash})
	}
	if err == nil {
		err = env.Sudo(controller, []string{"_tun-commit", "--transaction", transaction})
	}
	if err != nil {
		return failTunMaintenance(env, layout, plan, err)
	}
	if err = plan.Finish(); err != nil {
		return fail(env, i18n.Errorf("The TUN update committed, but its recovery record could not be removed: %w", err))
	}
	if err = saveTunUpdateHint(layout, plan.Pending.Any()); err != nil {
		fmt.Fprintln(env.Stderr, i18n.Errorf("The TUN update committed, but its update hint could not be saved: %w", err))
	}
	if action == "refresh" {
		refreshRegion(env, layout, plan.Envelope, false)
	}
	fmt.Fprintln(env.Stdout, i18n.T("Linux TUN maintenance committed. The mixed proxy remains off."))
	return 0
}

// checkTunMaintenance performs no root operation and changes no runtime files.
func checkTunMaintenance(env Env, layout paths.Layout, installer *install.Installer) int {
	capture := tunCapture(env)
	if capture.Status != "active" || capture.Phase != "active" {
		return fail(env, i18n.New("TUN state needs repair before automatic maintenance can check updates. Run 'sbc tun off'."))
	}
	state, err := tun.ReadState(layout)
	if err != nil {
		return fail(env, err)
	}
	link, err := install.ReadLink(layout)
	if err != nil {
		return fail(env, err)
	}
	available, manifest, err := installer.CheckUpgrade(link, installedSBCVersion(layout, env.Version))
	if err != nil {
		return fail(env, err)
	}
	version, err := os.ReadFile(layout.SingBoxVersion())
	if err != nil {
		return fail(env, err)
	}
	config, err := installer.FetchTun(link, strings.TrimSpace(string(version)))
	if err != nil {
		return fail(env, err)
	}
	fingerprint, err := tun.SourceFingerprint(config, func(name string) (string, error) {
		file, ok := manifest.Lookup("rules/" + name)
		if !ok {
			return "", i18n.Errorf("the signed release has no TUN rule snapshot %q", name)
		}
		return file.SHA256, nil
	})
	if err != nil {
		return fail(env, err)
	}
	if err = saveTunUpdateHint(layout, available.Any() || fingerprint != state.SourceHash); err != nil {
		return fail(env, err)
	}
	return 0
}

func saveTunUpdateHint(layout paths.Layout, pending bool) error {
	if pending {
		return paths.WriteFile(layout.TunUpdateHint(), []byte("Run 'sbc refresh' or 'sbc upgrade' to update the active TUN.\n"), 0o600)
	}
	err := os.Remove(layout.TunUpdateHint())
	if os.IsNotExist(err) {
		return nil
	}
	return err
}

// recoverTunMaintenance never starts a service. A durable root commit proves
// that user publication completed. Otherwise, restore only after safe shutdown
// or when the previous generation never stopped.
func recoverTunMaintenance(env Env, layout paths.Layout, recovery *install.TunRecovery) (bool, error) {
	capture := tunCapture(env)
	different := i18n.New("Another TUN generation blocks this recovery. Keep the recovery files for manual repair.")
	if capture.Transaction != recovery.Transaction && capture.Transaction != recovery.PreviousTransaction {
		return false, different
	}
	committed := func(capture tun.CaptureState) bool {
		return capture.Transaction == recovery.Transaction && (capture.Committed || capture.Phase == "active")
	}
	previousActive := capture.Transaction == recovery.PreviousTransaction && capture.Phase == "active" && capture.Status == "active"
	if !committed(capture) && !previousActive && capture.Status != "off" {
		if capture.Transaction != recovery.Transaction {
			return false, different
		}
		if env.Sudo == nil {
			return false, i18n.New("sudo is not available in this environment")
		}
		self, err := env.Self()
		if err != nil {
			return false, err
		}
		if err = env.Sudo(self, []string{"_tun-recover", "--transaction", recovery.Transaction}); err != nil {
			return false, err
		}
		capture = tunCapture(env)
		if capture.Transaction != recovery.Transaction {
			return false, different
		}
	}
	if committed(capture) {
		if capture.Status != "active" && capture.Status != "off" {
			return false, i18n.New("TUN stop is unconfirmed. Recovery files remain. Run 'sbc tun off' again.")
		}
		if capture.Status == "off" {
			if err := tun.RemoveState(layout); err != nil {
				return false, err
			}
		}
		return capture.Status == "off", recovery.Finish()
	}
	if !previousActive && capture.Status != "off" {
		return false, i18n.New("TUN stop is unconfirmed. Recovery files remain. Run 'sbc tun off' again.")
	}
	if err := recovery.Restore(); err != nil {
		return false, err
	}
	if !previousActive {
		if err := tun.RemoveState(layout); err != nil {
			return false, err
		}
	}
	return !previousActive, recovery.Finish()
}

func failTunMaintenance(env Env, layout paths.Layout, plan *install.TunUpdate, cause error) int {
	recovery := plan.Recovery()
	_, err := recoverTunMaintenance(env, layout, recovery)
	if err != nil {
		return fail(env, errors.Join(i18n.New("TUN maintenance failed. Recovery files remain. Run 'sbc tun off' before retrying."), cause, err))
	}
	capture := tunCapture(env)
	if capture.Status == "active" {
		if capture.Transaction == recovery.Transaction && capture.Phase == "active" {
			return fail(env, errors.Join(i18n.New("The maintenance command failed, but the new TUN generation is committed and remains active."), cause))
		}
		return fail(env, errors.Join(i18n.New("TUN maintenance failed before publication. The existing TUN remains active."), cause))
	}
	if capture.Status != "off" {
		return fail(env, errors.Join(i18n.New("TUN stop is unconfirmed. Recovery files remain. Run 'sbc tun off' again."), cause))
	}
	return fail(env, errors.Join(i18n.New("TUN maintenance failed. Capture is off and the mixed proxy remains off."), cause))
}

func tunMaintenanceGuard(env Env, layout paths.Layout) (*startupGuard, error) {
	guard, err := newStartupGuard(env, layout)
	if err != nil {
		return nil, err
	}
	busy, err := maintenance.EngineBusy(layout)
	if err != nil || busy {
		guard.close()
		if err == nil {
			err = maintenance.ErrBusy
		}
		return nil, err
	}
	return guard, nil
}
