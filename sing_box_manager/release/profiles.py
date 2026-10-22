"""Build the remaining client profiles and the single node config in code."""

from __future__ import annotations

from sing_box_manager.desktop_config import (
    RULE_SET_URLS,
    UserProtocols,
    snell_outbound,
)
from sing_box_manager.release.platforms import (
    MIXED_PROFILE,
    ClientProfile,
)
from sing_box_manager.release.renderers import (
    _apply_api_service_settings,
    _apply_v2ray_api_settings,
    apply_desktop_route_strategy,
    apply_direct_exceptions,
    apply_tls_settings,
    bundled_remote_rule_set,
    render_hysteria2_client_config,
    render_naive_client_config,
    render_trojan_client_config,
)
from sing_box_manager.settings import (
    SUPPORTED_PROTOCOLS,
    DeploymentConfig,
    RuntimeInventory,
    TrafficStatsSettings,
)


def client_base_config(protocol: str, profile: ClientProfile = MIXED_PROFILE) -> dict:
    """Fresh, secret-free structure shared by the inventory render helpers."""
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"Unsupported protocol: {protocol}")
    if profile != MIXED_PROFILE:
        raise ValueError(f"Unsupported profile: {profile.name}")
    outbound: dict = {
        "type": protocol,
        "tag": "proxy",
        "server": "",
        "server_port": 443,
        "password": "",
        "tls": {"enabled": True},
    }
    if protocol == "snell":
        outbound = {
            "type": "snell",
            "tag": "proxy",
            "server": "",
            "server_port": 443,
            "version": 6,
            "psk": "synthetic-validation-psk",
            "userkey": "synthetic-userkey",
            "mode": "default",
            "reuse": False,
        }
    if protocol == "naive":
        outbound["username"] = ""
    if protocol == "hysteria2":
        outbound["obfs"] = {"type": "salamander", "password": ""}
    common: dict = {
        "log": {"disabled": False, "level": "info", "timestamp": True},
        "ntp": {
            "enabled": True,
            "interval": "30m",
            "server": "time.apple.com",
            "server_port": 123,
        },
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "http_clients": [{"tag": "rule-downloads", "detour": "proxy"}],
    }
    outbound["domain_resolver"] = {"server": "google"}
    common.update(
        {
            "inbounds": [
                {
                    "type": "mixed",
                    "listen": "127.0.0.1",
                    "listen_port": 1080,
                    "set_system_proxy": False,
                }
            ],
            "route": {
                "rule_set": [
                    bundled_remote_rule_set(
                        {
                            "type": "remote",
                            "format": "binary",
                            "tag": tag,
                            "url": url,
                        }
                    )
                    for tag, url in RULE_SET_URLS.items()
                ]
            },
        }
    )
    return apply_desktop_route_strategy(common, "china", os_name="linux")


def build_client_profile(
    deployment: DeploymentConfig,
    protocols: UserProtocols,
    protocol: str,
    profile: ClientProfile,
) -> dict:
    """Build the same credential-bearing profile for releases and golden checks."""
    config = client_base_config(protocol, profile)
    if (
        protocol == "trojan"
        and protocols.trojan is not None
        and protocols.trojan.enabled
    ):
        return render_trojan_client_config(
            config, deployment, protocols.trojan, profile.set_system_proxy
        )
    if (
        protocol == "hysteria2"
        and protocols.hysteria2 is not None
        and protocols.hysteria2.enabled
    ):
        if protocols.hysteria2_settings is None:
            raise ValueError("Hysteria2 needs the shared settings")
        return render_hysteria2_client_config(
            config,
            deployment,
            protocols.hysteria2_settings,
            protocols.hysteria2,
            profile.set_system_proxy,
        )
    if protocol == "naive" and protocols.naive is not None and protocols.naive.enabled:
        return render_naive_client_config(
            config, deployment, protocols.naive, profile.set_system_proxy
        )
    if protocol == "snell" and protocols.snell is not None and protocols.snell.enabled:
        config["outbounds"][0] = dict(
            snell_outbound(deployment, protocols.snell, protocols.snell_settings),
            tag="proxy",
        )
        return apply_direct_exceptions(config, deployment)
    raise ValueError(f"User holds no enabled {protocol} credential")


def counter_username(protocol: str, username: str) -> str:
    """Keep Naive's wire username. Qualify internal labels for other protocols."""
    return username if protocol == "naive" else f"{protocol}:{username}"


def build_server_config(
    inventory: RuntimeInventory, traffic_stats: TrafficStatsSettings | None = None
) -> dict:
    config: dict = {
        "log": {"disabled": False, "level": "info", "timestamp": True},
        "inbounds": [],
        "dns": {"servers": [{"type": "local", "tag": "server-local"}]},
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {
            "final": "direct",
            "rules": [
                # Resolve first. Inspect every answer before the direct outbound
                # uses that same address list, including its fallback addresses.
                {"action": "resolve", "server": "server-local"},
                # A UDP association must not change peers after its route check.
                {"network": "udp", "action": "route-options", "udp_connect": True},
                {"ip_is_private": True, "action": "reject"},
                # Include IPv4-mapped forms: DNS answers need not be unmapped
                # before CIDR matching. The private matcher omits these ranges.
                {
                    "ip_cidr": [
                        "0.0.0.0/8",
                        "100.64.0.0/10",
                        "240.0.0.0/4",
                        "::ffff:0:0/104",
                        "::ffff:6440:0/106",
                        "::ffff:f000:0/100",
                    ],
                    "action": "reject",
                },
            ],
        },
    }
    usernames = []
    for protocol in SUPPORTED_PROTOCOLS:
        section = getattr(inventory, protocol)
        users = [user for user in section.users if user.enabled]
        # Empty Snell users enable PSK-only authentication upstream. Never
        # publish that fallback. Naive also requires a non-empty roster.
        if protocol in {"naive", "snell"} and not users:
            continue
        if protocol == "snell":
            if not inventory.snell.psk or inventory.deployment.snell_port is None:
                raise ValueError("Snell requires shared settings and a port")
            config["inbounds"].append(
                {
                    "type": "snell",
                    "tag": "snell",
                    "listen": "::",
                    "listen_port": inventory.deployment.snell_port,
                    "version": 6,
                    "psk": inventory.snell.psk,
                    "mode": "default",
                    "users": [
                        {
                            "name": counter_username(protocol, user.username),
                            "userkey": user.password,
                        }
                        for user in users
                    ],
                }
            )
            usernames.extend(
                counter_username(protocol, user.username) for user in users
            )
            continue
        tls: dict = {}
        apply_tls_settings(tls, inventory.deployment, include_server_paths=True)
        inbound: dict = {
            "type": protocol,
            "tag": protocol,
            "listen": "::",
            "listen_port": getattr(inventory.deployment, f"{protocol}_port"),
            "users": [
                {
                    "username" if protocol == "naive" else "name": counter_username(
                        protocol, user.username
                    ),
                    "password": user.password,
                }
                for user in users
            ],
            "tls": tls,
        }
        usernames.extend(counter_username(protocol, user.username) for user in users)
        if protocol == "trojan":
            inbound["multiplex"] = {"enabled": True}
        elif protocol == "hysteria2":
            inbound["obfs"] = {
                "type": "salamander",
                "password": inventory.hysteria2.obfs_password,
            }
        else:
            inbound["network"] = "tcp"
        config["inbounds"].append(inbound)
    if traffic_stats is not None and traffic_stats.enabled:
        _apply_v2ray_api_settings(
            config, listen_address=traffic_stats.api_listen, usernames=usernames
        )
        _apply_api_service_settings(
            config,
            listen_address=traffic_stats.connection_api_listen,
            secret=traffic_stats.connection_api_secret,
        )
    return config
