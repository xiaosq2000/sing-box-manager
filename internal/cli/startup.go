package cli

import (
	"errors"
	"net"
	"os"
	"strconv"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/maintenance"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/service"
)

// startupGuard exists only under the operation lock. Shared permission blocks
// grants from older operations before we inspect an engine or change its files.
type startupGuard struct {
	env        Env
	layout     paths.Layout
	permission *maintenance.Lease
	attempted  service.Manager
}

func newStartupGuard(env Env, layout paths.Layout) (*startupGuard, error) {
	permission, err := maintenance.Permission(layout, false)
	if err != nil {
		return nil, err
	}
	return &startupGuard{env: env, layout: layout, permission: permission}, nil
}

func (g *startupGuard) close() { g.permission.Close() }
func (g *startupGuard) timeout() time.Duration {
	if g.env.startupTimeout > 0 {
		return g.env.startupTimeout
	}
	return 10 * time.Second
}

func (g *startupGuard) block() error {
	g.permission.Close()
	g.permission = nil
	var err error
	g.permission, err = maintenance.Permission(g.layout, false)
	return err
}

// waitEngine requires a free engine lease for shutdown, or a held lease and a
// ready API for launch. A ready old listener alone cannot satisfy launch.
func (g *startupGuard) waitEngine(started bool) error {
	deadline := time.Now().Add(g.timeout())
	for {
		busy, err := maintenance.EngineBusy(g.layout)
		if err != nil {
			return err
		}
		if !started && !busy {
			return nil
		}
		if started && busy {
			client, _, err := localAPI(g.layout)
			if err != nil {
				return err
			}
			if client.Ready(min(100*time.Millisecond, max(time.Millisecond, time.Until(deadline)))) == nil {
				return nil
			}
		}
		if time.Now().After(deadline) {
			if started {
				return api.ErrNotRunning
			}
			return maintenance.ErrBusy
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// settle runs before live writes, never during the permission window. A wrapper
// that acquired its lease under an older grant must finish startup or exit first.
func (g *startupGuard) settle() error {
	deadline := time.Now().Add(g.timeout())
	for {
		busy, err := maintenance.EngineBusy(g.layout)
		if err != nil || !busy {
			return err
		}
		client, _, err := localAPI(g.layout)
		if err != nil {
			return err
		}
		if client.Ready(min(100*time.Millisecond, max(time.Millisecond, time.Until(deadline)))) == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return maintenance.ErrBusy
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func (g *startupGuard) stop(manager service.Manager) error {
	busy, err := maintenance.EngineBusy(g.layout)
	if err != nil {
		return err
	}
	if manager.Active() || busy {
		if err := manager.Stop(); err != nil {
			return err
		}
	}
	if err := g.waitEngine(false); err != nil {
		return err
	}
	// Task Scheduler can report Running briefly after the child exits.
	deadline := time.Now().Add(g.timeout())
	for manager.Active() {
		if time.Now().After(deadline) {
			return maintenance.ErrBusy
		}
		time.Sleep(10 * time.Millisecond)
	}
	return nil
}

func (g *startupGuard) beforeRestore() error {
	if g.permission == nil {
		if err := g.block(); err != nil {
			return err
		}
	}
	if g.attempted != nil {
		return g.stop(g.attempted)
	}
	return nil
}

// start retains the grant through readiness, including asynchronous native
// starts. Every exit revokes it before a caller can restore files or unlock.
func (g *startupGuard) start(manager service.Manager, restart bool) (result error) {
	if !restart && manager.Active() {
		return nil
	}
	busy, err := maintenance.EngineBusy(g.layout)
	if err != nil {
		return err
	}
	if busy && !manager.Active() {
		return maintenance.ErrBusy
	}
	g.attempted = manager
	if err = g.stop(manager); err != nil {
		return err
	}
	if err = g.env.CheckVacant(g.layout); err != nil {
		return err
	}
	g.permission.Close()
	g.permission = nil
	grant, err := maintenance.Permission(g.layout, true)
	if err != nil {
		return errors.Join(err, g.block())
	}
	defer func() {
		grant.Close()
		result = errors.Join(result, g.block())
		if result != nil {
			result = errors.Join(result, g.beforeRestore())
		}
	}()
	if err = manager.Start(); err != nil {
		return err
	}
	return g.waitEngine(true)
}

func startManaged(env Env, layout paths.Layout, manager service.Manager, restart bool) error {
	if env.startup != nil {
		return env.startup.start(manager, restart)
	}
	// Internal callers in fixtures can invoke installation without the command
	// wrapper. Real command paths retain their operation lock across all writes.
	unlock, err := maintenance.Lock(layout)
	if err != nil {
		return err
	}
	defer unlock()
	guard, err := newStartupGuard(env, layout)
	if err != nil {
		return err
	}
	defer guard.close()
	return guard.start(manager, restart)
}

func controllerVacant(layout paths.Layout) error {
	if _, err := os.Stat(layout.ConfigFile()); os.IsNotExist(err) {
		return nil
	}
	_, local, err := localAPI(layout)
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(local.APIPort)))
	if err != nil {
		return i18n.New("The local API port is occupied. Stop the other proxy before starting sing-box.")
	}
	return listener.Close()
}

// A readiness observer can briefly hold the engine lock while probing it. Retry
// that collision, but never turn a persistent busy engine into startup permission.
func startupEngineLease(layout paths.Layout) (*maintenance.Lease, error) {
	deadline := time.Now().Add(100 * time.Millisecond)
	for {
		engine, err := maintenance.LockEngine(layout)
		if !errors.Is(err, maintenance.ErrBusy) || time.Now().After(deadline) {
			return engine, err
		}
		time.Sleep(time.Millisecond)
	}
}

// prepareStartup keeps the engine lease across authorization and exec. It never
// trusts a file's existence or an operation lock's busy state as permission.
func prepareStartup(env Env, layout paths.Layout) (*maintenance.Lease, error) {
	unlock, err := maintenance.Lock(layout)
	if err == nil {
		defer unlock()
		guard, err := newStartupGuard(env, layout)
		if err != nil {
			return nil, err
		}
		defer guard.close()
		engine, err := startupEngineLease(layout)
		if err != nil {
			return nil, err
		}
		if err = initializeMaintenance(env, layout); err != nil {
			engine.Close()
			return nil, err
		}
		return engine, nil
	}
	if !errors.Is(err, maintenance.ErrBusy) {
		return nil, err
	}
	engine, err := startupEngineLease(layout)
	if err != nil {
		return nil, err
	}
	granted, err := maintenance.StartGranted(layout)
	if err == nil && granted {
		return engine, nil
	}
	engine.Close()
	if err != nil {
		return nil, err
	}
	// A completed operation may revoke permission just before releasing its
	// operation lock. Fail closed; the caller or native manager can retry.
	return nil, maintenance.ErrBusy
}
