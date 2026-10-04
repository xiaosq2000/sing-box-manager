"""Build the remaining client profiles and the single node config in code."""

from __future__ import annotations

from sing_box_manager.desktop_config import (
    RULE_SET_URLS,
    UserProtocols,
)
from sing_box_manager.release.platforms import (
    MIXED_PROFILE,
    MOBILE_TUN_PROFILE,
    ClientProfile,
)
from sing_box_manager.release.renderers import (
    _apply_api_service_settings,
    _apply_v2ray_api_settings,
    apply_desktop_route_strategy,
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
    if profile not in (MIXED_PROFILE, MOBILE_TUN_PROFILE):
        raise ValueError(f"Unsupported profile: {profile.name}")
    outbound: dict = {
        "type": protocol,
        "tag": "proxy",
        "server": "",
        "server_port": 443,
        "password": "",
        "tls": {"enabled": True},
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
    if profile == MIXED_PROFILE:
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
    common.update(
        {
            "dns": {
                "rules": [{"query_type": ["A", "AAAA"], "server": "remote"}],
                "servers": [
                    {"type": "tls", "tag": "google", "server": "8.8.8.8"},
                    {"type": "udp", "tag": "local", "server": "223.5.5.5"},
                    {
                        "type": "fakeip",
                        "tag": "remote",
                        "inet4_range": "198.18.0.0/15",
                        "inet6_range": "fc00::/18",
                    },
                ],
            },
            "inbounds": [
                {
                    "type": "tun",
                    "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
                    "auto_route": True,
                    "strict_route": True,
                }
            ],
            "route": {
                "default_domain_resolver": {"server": "local"},
                "default_http_client": "rule-downloads",
                "rule_set": [
                    {
                        "type": "remote",
                        "format": "binary",
                        "tag": tag,
                        "url": url,
                        "http_client": "rule-downloads",
                    }
                    for tag, url in (
                        (
                            "geoip-cn",
                            "https://raw.githubusercontent.com/SagerNet/sing-geoip/rule-set/geoip-cn.srs",
                        ),
                        (
                            "geosite-geolocation-cn",
                            "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-geolocation-cn.srs",
                        ),
                    )
                ],
                "rules": [
                    {"action": "sniff"},
                    {
                        "type": "logical",
                        "mode": "or",
                        "rules": [{"protocol": "dns"}, {"port": 53}],
                        "action": "hijack-dns",
                    },
                    {"ip_is_private": True, "outbound": "direct"},
                    {"clash_mode": "Direct", "outbound": "direct"},
                    {"clash_mode": "Global", "outbound": "proxy"},
                    {
                        "type": "logical",
                        "mode": "or",
                        "rules": [
                            {"port": 853},
                            {"network": "udp", "port": 443},
                            {"protocol": "stun"},
                        ],
                        "action": "reject",
                    },
                    {
                        "outbound": "direct",
                        "rule_set": ["geoip-cn", "geosite-geolocation-cn"],
                    },
                ],
            },
        }
    )
    return common


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
    raise ValueError(f"User holds no enabled {protocol} credential")


def counter_username(protocol: str, username: str) -> str:
    """Naive's username is wire-visible; the other protocols authenticate passwords."""
    return username if protocol == "naive" else f"{protocol}:{username}"


def build_server_config(
    inventory: RuntimeInventory, traffic_stats: TrafficStatsSettings | None = None
) -> dict:
    config: dict = {
        "log": {"disabled": False, "level": "info", "timestamp": True},
        "inbounds": [],
    }
    usernames = []
    for protocol in SUPPORTED_PROTOCOLS:
        section = getattr(inventory, protocol)
        users = [user for user in section.users if user.enabled]
        # Naive requires a non-empty roster in sing-box 1.14.2. Unlike the
        # other protocols, its unused listener cannot be checked or started.
        if protocol == "naive" and not users:
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
