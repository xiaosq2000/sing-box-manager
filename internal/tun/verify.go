package tun

import (
	"context"
	"net"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

const (
	captureProbeHost = "1.1.1.1"
	captureProbePort = "443"
)

// Verify accepts an activation only when the service, capture, DNS and an
// egress request all work.
func Verify(paths Paths, runner Runner, tools Tools) error {
	if err := waitActive(runner, tools); err != nil {
		return err
	}
	if err := stableProcess(runner, tools); err != nil {
		return err
	}
	if err := waitInterface(runner, tools); err != nil {
		return err
	}
	if err := waitRoutes(runner, tools); err != nil {
		return err
	}
	if _, err := runner.Output(tools.IP, "rule", "show"); err != nil {
		return i18n.New("the TUN routing rules are missing")
	}
	if _, err := runner.Output(tools.Nft, "list", "ruleset"); err != nil {
		return i18n.New("the nftables ruleset is unavailable")
	}
	if err := lookupThroughTunnel(); err != nil {
		return err
	}
	port, secret, err := ReadAPI(paths.Config)
	if err != nil {
		return err
	}
	tag, err := ReadTunTag(paths.Config)
	if err != nil {
		return err
	}
	client := api.New(port, secret)
	if err := client.WaitReady(15 * time.Second); err != nil {
		return i18n.Errorf("the TUN API did not become ready: %w", err)
	}
	if err := verifyCapture(client, tag); err != nil {
		return err
	}
	if _, err := client.Delay(api.ProxySelector, 10*time.Second); err != nil {
		return i18n.Errorf("a page did not load through the TUN: %w", err)
	}
	return nil
}

func waitActive(runner Runner, tools Tools) error {
	for range 30 {
		if err := runner.Run(tools.Systemctl, "is-active", "--quiet", ServiceName); err == nil {
			return nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	return i18n.New("the TUN service did not become active")
}

// waitInterface waits for sing-box, which systemd reports active before it
// creates the interface.
func waitInterface(runner Runner, tools Tools) error {
	deadline := time.Now().Add(15 * time.Second)
	for {
		if _, err := runner.Output(tools.IP, "-o", "link", "show", InterfaceName); err == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return i18n.Errorf("the %s interface is missing", InterfaceName)
		}
		time.Sleep(200 * time.Millisecond)
	}
}

func waitRoutes(runner Runner, tools Tools) error {
	for _, family := range []string{"-4", "-6"} {
		deadline := time.Now().Add(10 * time.Second)
		for {
			output, err := runner.Output(tools.IP, family, "route", "show", "dev", InterfaceName)
			if err == nil && strings.TrimSpace(output) != "" {
				break
			}
			if time.Now().After(deadline) {
				return i18n.Errorf("the %s interface has no %s routes", InterfaceName, family)
			}
			time.Sleep(200 * time.Millisecond)
		}
	}
	return nil
}

func stableProcess(runner Runner, tools Tools) error {
	first, err := mainPID(runner, tools)
	if err != nil {
		return err
	}
	time.Sleep(time.Second)
	second, err := mainPID(runner, tools)
	if err != nil {
		return err
	}
	if first == 0 || first != second {
		return i18n.New("the TUN service process is not stable")
	}
	return nil
}

func mainPID(runner Runner, tools Tools) (int, error) {
	output, err := runner.Output(tools.Systemctl, "show", "-p", "MainPID", "--value", ServiceName)
	if err != nil {
		return 0, i18n.New("the TUN service has no main process")
	}
	pid, err := strconv.Atoi(strings.TrimSpace(output))
	if err != nil || pid <= 0 {
		return 0, i18n.New("the TUN service has no main process")
	}
	return pid, nil
}

// verifyCapture proves that the TUN captures locally generated traffic. Under
// auto_redirect, nftables redirects captured TCP to a local socket before
// routing, so the interface packet counters do not change. A new connection in
// sing-box's own Clash API list is the reliable evidence.
func verifyCapture(client *api.Client, tag string) error {
	before, err := connectionIDs(client)
	if err != nil {
		return err
	}
	probe, err := net.DialTimeout("tcp", net.JoinHostPort(captureProbeHost, captureProbePort), 5*time.Second)
	if err != nil {
		return i18n.Errorf("the TUN did not capture a test connection: %w", err)
	}
	defer probe.Close()
	deadline := time.Now().Add(5 * time.Second)
	for {
		connections, err := client.Connections()
		if err != nil {
			return err
		}
		for _, connection := range connections {
			if captured(connection, tag, before) {
				return nil
			}
		}
		if time.Now().After(deadline) {
			return i18n.New("the TUN did not capture a test connection")
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func connectionIDs(client *api.Client) (map[string]struct{}, error) {
	connections, err := client.Connections()
	if err != nil {
		return nil, err
	}
	ids := make(map[string]struct{}, len(connections))
	for _, connection := range connections {
		ids[connection.ID] = struct{}{}
	}
	return ids, nil
}

func captured(connection api.Connection, tag string, before map[string]struct{}) bool {
	if _, seen := before[connection.ID]; seen {
		return false
	}
	if connection.Metadata.Type != "tun/"+tag {
		return false
	}
	return connection.Metadata.DestinationIP == captureProbeHost &&
		connection.Metadata.DestinationPort == captureProbePort
}

func lookupThroughTunnel() error {
	resolver := &net.Resolver{
		PreferGo: true,
		Dial: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var dialer net.Dialer
			return dialer.DialContext(ctx, "udp", "8.8.8.8:53")
		},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := resolver.LookupHost(ctx, "example.com"); err != nil {
		return i18n.Errorf("DNS through the TUN failed: %w", err)
	}
	return nil
}
