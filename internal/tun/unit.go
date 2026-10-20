package tun

import "strconv"

// Unit returns the root systemd unit for the TUN service.
func Unit(paths Paths) string {
	quote := strconv.Quote
	return `[Unit]
Description=sbc Linux TUN (sing-box)
After=network-pre.target
Wants=network-pre.target
Before=network.target
ConditionPathExists=/dev/net/tun

[Service]
Type=simple
User=` + ServiceUser + `
Group=` + ServiceGroup + `
WorkingDirectory=` + paths.StateDir + `
RuntimeDirectory=sbc-tun
RuntimeDirectoryMode=0755
StateDirectory=sbc-tun
StateDirectoryMode=0700
Environment=` + quote("LD_LIBRARY_PATH="+paths.BinaryDir) + `
ExecStartPre=+/bin/touch ` + quote(paths.Active) + `
ExecStart=` + quote(paths.Binary) + ` run -c ` + quote(paths.Config) + `
ExecStopPost=+/bin/rm -f ` + quote(paths.Active) + `
Restart=on-failure
RestartSec=5s
AmbientCapabilities=CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=no
DevicePolicy=closed
DeviceAllow=/dev/net/tun rw
ReadWritePaths=` + quote(paths.StateDir) + ` /run/sbc-tun
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
LockPersonality=yes
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
`
}
