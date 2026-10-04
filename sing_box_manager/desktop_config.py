"""Build one desktop client config per user, covering every route and protocol.

Each route is a Clash mode, and each protocol is an outbound behind the `proxy`
selector, so sbc switches both through sing-box's local API without a restart.
sing-box keys its DNS cache by server and clears it when the mode changes, so
the routes share one cache file.

The config carries the modes but no API address. sbc adds the address, a
secret and the listen port before it starts sing-box, so a config that runs
unchanged never opens an unauthenticated API.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from sing_box_manager.release.platforms import ROUTE_STRATEGIES
from sing_box_manager.release.renderers import (
    ADGUARD_RULE_SET_TAG,
    ADGUARD_RULE_SET_URL,
    AI_GEOSITE_RULE_SET_TAG,
    AI_GEOSITE_RULE_SET_URL,
    AI_SERVICE_RULE_SET_TAG,
    SUPPORTED_CLIENT_OS,
    apply_tls_settings,
    bundled_remote_rule_set,
    load_ai_service_rules,
    system_dns_server,
)
from sing_box_manager.settings import (
    DeploymentConfig,
    Hysteria2Inventory,
    Hysteria2User,
    NaiveUser,
    TrojanUser,
)

PROXY_TAG = "proxy"
DIRECT_TAG = "direct"
# The ai route sends direct traffic through the host's resolver instead of a
# public one. An outbound's resolver is fixed, so that route gets its own.
DIRECT_SYSTEM_TAG = "direct-system"
MIXED_INBOUND_TAG = "mixed"
CACHE_FILE = "cache.db"

GFWLIST_TAG = "GFWList"
CHINA_DOMAIN_TAGS = ["GeoSite-CN", "GeoSite-Apple-CN"]
CHINA_IP_TAG = "GeoIP-CN"
AI_TAGS = [AI_SERVICE_RULE_SET_TAG, AI_GEOSITE_RULE_SET_TAG]

RULE_SET_URLS = {
    ADGUARD_RULE_SET_TAG: ADGUARD_RULE_SET_URL,
    AI_GEOSITE_RULE_SET_TAG: AI_GEOSITE_RULE_SET_URL,
    GFWLIST_TAG: "https://raw.githubusercontent.com/Dreista/sing-box-rule-set-cn/"
    "rule-set/gfwlist.txt.srs",
    "GeoSite-CN": "https://raw.githubusercontent.com/Dreista/sing-box-rule-set-cn/"
    "rule-set/accelerated-domains.china.conf.srs",
    "GeoSite-Apple-CN": "https://raw.githubusercontent.com/Dreista/"
    "sing-box-rule-set-cn/rule-set/apple.china.conf.srs",
    CHINA_IP_TAG: "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/"
    "sing/geo/geoip/cn.srs",
}


@dataclass(frozen=True)
class UserProtocols:
    """One user's credentials for the protocols they hold.

    `hysteria2_settings` carries the shared obfuscation secret and bandwidth,
    and is required when `hysteria2` is set.
    """

    trojan: TrojanUser | None = None
    hysteria2: Hysteria2User | None = None
    naive: NaiveUser | None = None
    hysteria2_settings: Hysteria2Inventory | None = None


def build_desktop_config(
    deployment: DeploymentConfig,
    protocols: UserProtocols,
    *,
    os_name: str,
    default_protocol: str,
    default_route: str,
) -> dict:
    """Return the sing-box config for one user's desktop client on `os_name`."""
    if os_name not in SUPPORTED_CLIENT_OS:
        raise ValueError(f"Unsupported client OS: {os_name}")
    if default_route not in ROUTE_STRATEGIES:
        raise ValueError(f"Unsupported route strategy: {default_route}")
    outbounds = _protocol_outbounds(deployment, protocols)
    tags = [outbound["tag"] for outbound in outbounds]
    if not tags:
        raise ValueError("The user holds no enabled protocol")
    if default_protocol not in tags:
        raise ValueError(
            f"The user does not hold the default protocol {default_protocol}"
        )

    exceptions = _direct_exception_matchers(deployment)
    return {
        "log": {"disabled": False, "level": "info", "timestamp": True},
        "dns": _dns(exceptions, os_name),
        # Direct, because the clock has to be right before a TLS proxy connects.
        "ntp": {
            "enabled": True,
            "server": "time.apple.com",
            "server_port": 123,
            "interval": "30m",
            "detour": DIRECT_TAG,
        },
        "inbounds": [
            {
                "type": "mixed",
                "tag": MIXED_INBOUND_TAG,
                "listen": "127.0.0.1",
                "listen_port": 1080,
                "set_system_proxy": False,
            }
        ],
        "outbounds": [
            {
                "type": "selector",
                "tag": PROXY_TAG,
                "outbounds": tags,
                "default": default_protocol,
                # A protocol switch is meant to take effect now, as the restart
                # it replaces did.
                "interrupt_exist_connections": True,
            },
            *outbounds,
            {
                "type": "direct",
                "tag": DIRECT_TAG,
                "domain_resolver": {"server": "aliyun"},
            },
            {
                "type": "direct",
                "tag": DIRECT_SYSTEM_TAG,
                "domain_resolver": {"server": "system"},
            },
        ],
        "route": _route(exceptions),
        "http_clients": [{"tag": "rule-downloads", "detour": PROXY_TAG}],
        "experimental": {
            "cache_file": {"enabled": True, "store_dns": True, "path": CACHE_FILE},
            "clash_api": {"default_mode": default_route},
        },
    }


def _protocol_outbounds(
    deployment: DeploymentConfig, protocols: UserProtocols
) -> list[dict]:
    outbounds = []
    if protocols.trojan is not None and protocols.trojan.enabled:
        outbounds.append(
            _proxy_outbound(
                deployment,
                "trojan",
                server_port=deployment.trojan_port,
                password=protocols.trojan.password,
            )
        )
    if protocols.hysteria2 is not None and protocols.hysteria2.enabled:
        outbounds.append(
            _hysteria2_outbound(
                deployment, protocols.hysteria2, protocols.hysteria2_settings
            )
        )
    if protocols.naive is not None and protocols.naive.enabled:
        outbounds.append(
            _proxy_outbound(
                deployment,
                "naive",
                server_port=deployment.naive_port,
                username=protocols.naive.username,
                password=protocols.naive.password,
            )
        )
    return outbounds


def _proxy_outbound(
    deployment: DeploymentConfig, protocol: str, **fields: object
) -> dict:
    tls: dict = {}
    apply_tls_settings(tls, deployment, include_server_paths=False)
    return {
        "type": protocol,
        "tag": protocol,
        "server": deployment.ip,
        **fields,
        "tls": tls,
        "domain_resolver": {"server": "google"},
    }


def _hysteria2_outbound(
    deployment: DeploymentConfig,
    user: Hysteria2User,
    settings: Hysteria2Inventory | None,
) -> dict:
    if settings is None:
        raise ValueError("Hysteria2 needs the shared settings")
    fields: dict[str, object] = {
        "server_port": deployment.hysteria2_port,
        "password": user.password,
        "obfs": {"type": "salamander", "password": settings.obfs_password},
    }
    # The client hops across the range; the server firewall redirects it to
    # the one port it listens on (see docs/VPS_DEPLOYMENT.md).
    if deployment.hysteria2_port_range is not None:
        fields["server_ports"] = [deployment.hysteria2_port_range]
        fields["hop_interval"] = "30s"
    # Bandwidth switches congestion control from BBR to Hysteria Brutal.
    if settings.up_mbps is not None:
        fields["up_mbps"] = settings.up_mbps
    if settings.down_mbps is not None:
        fields["down_mbps"] = settings.down_mbps
    return _proxy_outbound(deployment, "hysteria2", **fields)


def _direct_exception_matchers(deployment: DeploymentConfig) -> dict:
    """Match the portal host and the configured suffixes, which stay direct."""
    matchers: dict = {}
    try:
        address = ipaddress.ip_address(deployment.host)
    except ValueError:
        matchers["domain"] = [deployment.host]
    else:
        matchers["ip_cidr"] = [f"{address}/{address.max_prefixlen}"]
    if deployment.direct_domain_suffixes:
        matchers["domain_suffix"] = deployment.direct_domain_suffixes.copy()
    return matchers


def _in_mode(mode: str, rules: list[dict]) -> list[dict]:
    return [{"clash_mode": mode, **rule} for rule in rules]


def _dns(exceptions: dict, os_name: str) -> dict:
    domain_exceptions = {
        key: value for key, value in exceptions.items() if key != "ip_cidr"
    }
    rules = []
    if domain_exceptions:
        rules = [
            {"clash_mode": "ai", **domain_exceptions, "server": "system"},
            {**domain_exceptions, "server": "aliyun"},
        ]
    # Each mode ends with a rule that matches every query, in place of the
    # `final` a single-route config would set.
    rules += [
        {"rule_set": AI_TAGS, "server": "google"},
        *_in_mode(
            "china",
            [
                {"rule_set": [GFWLIST_TAG], "server": "google"},
                {"rule_set": CHINA_DOMAIN_TAGS, "server": "aliyun"},
                {"server": "google"},
            ],
        ),
        *_in_mode(
            "gfw",
            [{"rule_set": [GFWLIST_TAG], "server": "google"}, {"server": "aliyun"}],
        ),
        *_in_mode("ai", [{"server": "system"}]),
        *_in_mode("global", [{"server": "google"}]),
    ]
    return {
        "servers": [
            {"type": "tls", "tag": "google", "server": "8.8.8.8", "detour": PROXY_TAG},
            {"type": "udp", "tag": "aliyun", "server": "223.5.5.5"},
            system_dns_server(os_name),
        ],
        "rules": rules,
        "final": "google",
        "optimistic": False,
    }


def _route(exceptions: dict) -> dict:
    rules = [
        {"action": "sniff"},
        {"ip_is_private": True, "outbound": DIRECT_TAG},
        {
            "type": "logical",
            "mode": "or",
            "rules": [{"protocol": "dns"}, {"port": 53}],
            "action": "hijack-dns",
        },
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
        {"clash_mode": "ai", **exceptions, "outbound": DIRECT_SYSTEM_TAG},
        {**exceptions, "outbound": DIRECT_TAG},
        # Ahead of the AdGuard reject: the filter blocks feature-flag and
        # telemetry hosts that AI vendors depend on.
        {"rule_set": AI_TAGS, "outbound": PROXY_TAG},
        {"rule_set": [ADGUARD_RULE_SET_TAG], "action": "reject"},
        *_in_mode(
            "china",
            [
                {"rule_set": [GFWLIST_TAG], "outbound": PROXY_TAG},
                {"rule_set": CHINA_DOMAIN_TAGS, "outbound": DIRECT_TAG},
                {"rule_set": [CHINA_IP_TAG], "outbound": DIRECT_TAG},
                {"outbound": PROXY_TAG},
            ],
        ),
        *_in_mode(
            "gfw",
            [
                {"rule_set": [GFWLIST_TAG], "outbound": PROXY_TAG},
                {"outbound": DIRECT_TAG},
            ],
        ),
        *_in_mode("ai", [{"outbound": DIRECT_SYSTEM_TAG}]),
        *_in_mode("global", [{"outbound": PROXY_TAG}]),
    ]
    return {
        "rule_set": [
            *(
                bundled_remote_rule_set(
                    {"type": "remote", "tag": tag, "format": "binary", "url": url}
                )
                for tag, url in RULE_SET_URLS.items()
            ),
            {
                "type": "inline",
                "tag": AI_SERVICE_RULE_SET_TAG,
                "rules": load_ai_service_rules(),
            },
        ],
        "rules": rules,
        "final": PROXY_TAG,
        "default_http_client": "rule-downloads",
    }
