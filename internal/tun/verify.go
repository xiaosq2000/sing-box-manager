package tun

import (
	"context"
	"net"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Verify accepts an activation only when the service, capture, DNS and an
// egress request all work and packets moved through the interface.
func Verify(paths Paths, runner Runner, tools Tools) error {
	if err := waitActive(runner, tools); err != nil {
		return err
	}
	if err := stableProcess(runner, tools); err != nil {
		return err
	}
	if _, err := runner.Output(tools.IP, "-o", "link", "show", InterfaceName); err != nil {
		return i18n.Errorf("the %s interface is missing", InterfaceName)
	}
	for _, family := range []string{"-4", "-6"} {
		output, err := runner.Output(tools.IP, family, "route", "show", "dev", InterfaceName)
		if err != nil || strings.TrimSpace(output) == "" {
			return i18n.Errorf("the %s interface has no %s routes", InterfaceName, family)
		}
	}
	if _, err := runner.Output(tools.IP, "rule", "show"); err != nil {
		return i18n.New("the TUN routing rules are missing")
	}
	if _, err := runner.Output(tools.Nft, "list", "ruleset"); err != nil {
		return i18n.New("the nftables ruleset is unavailable")
	}

	beforeRX, beforeTX, err := readPackets(InterfaceName)
	if err != nil {
		return err
	}
	if err := lookupThroughTunnel(); err != nil {
		return err
	}
	port, secret, err := ReadAPI(paths.Config)
	if err != nil {
		return err
	}
	client := api.New(port, secret)
	if err := client.WaitReady(15 * time.Second); err != nil {
		return i18n.Errorf("the TUN API did not become ready: %w", err)
	}
	if _, err := client.Delay(api.ProxySelector, 10*time.Second); err != nil {
		return i18n.Errorf("a page did not load through the TUN: %w", err)
	}
	afterRX, afterTX, err := readPackets(InterfaceName)
	if err != nil {
		return err
	}
	if afterRX <= beforeRX && afterTX <= beforeTX {
		return i18n.New("the TUN interface packet counters did not increase")
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

func readPackets(name string) (uint64, uint64, error) {
	base := "/sys/class/net/" + name + "/statistics/"
	rx, err := readCounter(base + "rx_packets")
	if err != nil {
		return 0, 0, err
	}
	tx, err := readCounter(base + "tx_packets")
	if err != nil {
		return 0, 0, err
	}
	return rx, tx, nil
}

func readCounter(path string) (uint64, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, i18n.Errorf("read packet counters: %w", err)
	}
	value, err := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64)
	if err != nil {
		return 0, i18n.Errorf("read packet counters: %w", err)
	}
	return value, nil
}
