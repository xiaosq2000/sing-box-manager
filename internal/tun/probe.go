package tun

import (
	"fmt"
	"os"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Probe proves that the service's user, capabilities and device policy can
// create a TUN interface and change nftables. It creates only disposable
// objects and removes them before returning.
func Probe(runner Runner, tools Tools, paths Paths) error {
	probeInterface := ProbePrefix + "0"
	ipUnit := fmt.Sprintf("%s-ip-%d", ProbePrefix, os.Getpid())
	if err := runner.Run(
		tools.SystemdRun,
		probeArgs(ipUnit, tools.IP, "tuntap", "add", "dev", probeInterface, "mode", "tun")...,
	); err != nil {
		return i18n.New("the TUN capability probe could not create an interface; /dev/net/tun, CAP_NET_ADMIN and nftables are required")
	}
	_ = runner.Run(
		tools.SystemdRun,
		probeArgs(ipUnit+"-cleanup", tools.IP, "tuntap", "del", "dev", probeInterface, "mode", "tun")...,
	)

	table := fmt.Sprintf("%s_%d", "sbc_tun_probe", os.Getpid())
	nftUnit := fmt.Sprintf("%s-nft-%d", ProbePrefix, os.Getpid())
	if err := runner.Run(
		tools.SystemdRun,
		probeArgs(nftUnit, tools.Nft, "add", "table", "inet", table)...,
	); err != nil {
		return i18n.New("the TUN capability probe could not create an nftables table; nftables support is required")
	}
	_ = runner.Run(
		tools.SystemdRun,
		probeArgs(nftUnit+"-cleanup", tools.Nft, "delete", "table", "inet", table)...,
	)
	// The transient unit can fail to clean up after itself; root can always
	// remove the table.
	_ = runner.Run(tools.Nft, "delete", "table", "inet", table)
	return nil
}

func probeArgs(unit, program string, args ...string) []string {
	result := []string{
		"--quiet", "--pipe", "--wait", "--collect", "--unit=" + unit,
		"-p", "User=" + ServiceUser,
		"-p", "Group=" + ServiceGroup,
		"-p", "AmbientCapabilities=CAP_NET_ADMIN",
		"-p", "CapabilityBoundingSet=CAP_NET_ADMIN",
		"-p", "DevicePolicy=closed",
		"-p", "DeviceAllow=/dev/net/tun rw",
		"-p", "PrivateDevices=no",
		"-p", "NoNewPrivileges=yes",
		program,
	}
	return append(result, args...)
}
