"""Focused render helpers for inventory-driven config generation."""

from __future__ import annotations

import copy
import ipaddress
import json
from pathlib import Path

from sing_box_manager.release.rule_sets import snapshot_filename
from sing_box_manager.settings import (
    AuthSnapshot,
    AuthSnapshotUser,
    DeploymentConfig,
    Hysteria2Inventory,
    Hysteria2User,
    NaiveUser,
    TrojanUser,
    WebPortalInventory,
    split_listen_address,
)

AI_SERVICE_RULE_SET_TAG = "AI-Services"
AI_GEOSITE_RULE_SET_TAG = "AI-Geosite"
ADGUARD_RULE_SET_TAG = "AdGuard-DNS-Filter"
AI_SERVICE_RULES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "rules" / "ai-services.json"
)
ADGUARD_RULE_SET_URL = (
    "https://raw.githubusercontent.com/Dreista/sing-box-rule-set-cn/"
    "rule-set/filter.txt.srs"
)
AI_GEOSITE_RULE_SET_URL = (
    "https://raw.githubusercontent.com/SagerNet/sing-geosite/"
    "rule-set/geosite-category-ai-!cn.srs"
)

# systemd-resolved's stub listener, which is what /etc/resolv.conf points every
# other program on a systemd Linux desktop at.
RESOLVED_STUB_ADDRESS = "127.0.0.53"

SUPPORTED_CLIENT_OS = ("linux", "darwin", "windows")

# sing-box unions these within a single headless rule, so a catalog entry only
# has to pick the cheapest matcher for the shape of host it describes.
AI_SERVICE_MATCHERS = ("domain", "domain_keyword", "domain_suffix")


def _ai_service_matchers(rule: dict) -> dict[str, list[str]]:
    """Validate one catalog rule and return the matcher lists it declares."""
    if unknown := set(rule) - set(AI_SERVICE_MATCHERS):
        raise ValueError(f"Unsupported AI service matchers: {sorted(unknown)}")
    if not any(rule.get(matcher) for matcher in AI_SERVICE_MATCHERS):
        raise ValueError("AI service rules must contain at least one matcher")

    collected: dict[str, list[str]] = {}
    for matcher in AI_SERVICE_MATCHERS:
        values = rule.get(matcher)
        if values is None:
            continue
        if not isinstance(values, list) or not values:
            raise ValueError(f"AI service {matcher} must be a non-empty list")
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError(f"AI service {matcher} must be non-empty strings")
        collected[matcher] = values

    return collected


def load_ai_service_rules(path: Path = AI_SERVICE_RULES_PATH) -> list[dict]:
    """Load and validate the reviewed source-format AI rule catalog."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("AI service rule set must use source format version 1")

    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("AI service rule set must contain rules")

    matched: dict[str, list[str]] = {matcher: [] for matcher in AI_SERVICE_MATCHERS}
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("AI service rules must be mappings")
        for matcher, values in _ai_service_matchers(rule).items():
            matched[matcher].extend(values)

    # Each matcher is checked on its own: a keyword and a suffix can legitimately
    # overlap, and interleaving them in one list would have no readable order.
    for matcher, values in matched.items():
        if values != sorted(set(values)):
            raise ValueError(f"AI service {matcher} must be sorted and unique")

    return copy.deepcopy(rules)


def _find_tagged_entry(entries: list[dict], tag: str) -> dict | None:
    for entry in entries:
        if entry.get("tag") == tag:
            return entry
    return None


def _desktop_common_rules(priority_rules: list[dict] | None = None) -> list[dict]:
    """Build the routing controls every desktop strategy shares.

    `priority_rules` land immediately ahead of the AdGuard reject. The filter
    blocks feature-flag and telemetry hosts that AI vendors depend on, so a
    strategy that proxies those vendors has to outrank it or the reject wins and
    the catalog entry never matches.
    """
    return [
        {"action": "sniff"},
        {"ip_is_private": True, "outbound": "direct"},
        {
            "action": "hijack-dns",
            "mode": "or",
            "rules": [{"protocol": "dns"}, {"port": 53}],
            "type": "logical",
        },
        {
            "action": "reject",
            "mode": "or",
            "rules": [
                {"port": 853},
                {"network": "udp", "port": 443},
                {"protocol": "stun"},
            ],
            "type": "logical",
        },
        *(priority_rules or []),
        {"action": "reject", "rule_set": [ADGUARD_RULE_SET_TAG]},
        {"clash_mode": "Direct", "outbound": "direct"},
        {"clash_mode": "Global", "outbound": "proxy"},
    ]


def bundled_remote_rule_set(rule_set: dict) -> dict:
    """Point a remote rule-set at the snapshot the release packages for it."""
    rule_set["update_interval"] = "1d"
    rule_set.pop("download_detour", None)
    rule_set["http_client"] = "rule-downloads"
    rule_set["initial_path"] = f"rules/{snapshot_filename(rule_set)}"
    return rule_set


def _adguard_rule_set(route: dict) -> dict:
    existing = _find_tagged_entry(route.get("rule_set", []), ADGUARD_RULE_SET_TAG)
    rule_set = (
        copy.deepcopy(existing)
        if existing is not None
        else {
            "format": "binary",
            "tag": ADGUARD_RULE_SET_TAG,
            "type": "remote",
            "url": ADGUARD_RULE_SET_URL,
        }
    )
    return bundled_remote_rule_set(rule_set)


def _ai_geosite_rule_set() -> dict:
    """Carry the breadth the reviewed catalog deliberately does not.

    The inline catalog stays small enough to read in a diff; this upstream set
    supplies the long tail of AI vendors and refreshes itself between releases.
    """
    return bundled_remote_rule_set(
        {
            "format": "binary",
            "tag": AI_GEOSITE_RULE_SET_TAG,
            "type": "remote",
            "url": AI_GEOSITE_RULE_SET_URL,
        }
    )


def system_dns_server(os_name: str) -> dict:
    """Return the DNS server that resolves direct traffic through the host.

    `type: "local"` does not hand the query to the operating system. It reads
    the resolver configuration and queries the link's upstream servers itself,
    binding the query socket to that link. Linux allowed that bind without
    CAP_NET_RAW only after the capability check was relaxed, so on Ubuntu 18.04
    the unprivileged client service fails every lookup with EPERM, reported as
    `dial udp <resolver>:53: operation not permitted`, and the ai route loses
    every direct destination while the proxied ones keep working.

    Asking systemd-resolved's stub over loopback needs no capability and leaves
    the per-link routing, split-horizon answers, and caching to the component
    that owns them. macOS and Windows keep `local`: the interface bind is the
    Linux path, and their local transports resolve through platform APIs.
    """
    if os_name == "linux":
        return {"server": RESOLVED_STUB_ADDRESS, "tag": "system", "type": "udp"}
    return {"tag": "system", "type": "local"}


def _set_outbound_resolver(rendered: dict, outbound_type: str, server: str) -> None:
    for outbound in rendered.get("outbounds", []):
        if (
            outbound.get("type") == outbound_type
            or outbound.get("tag") == outbound_type
        ):
            outbound["domain_resolver"] = {"server": server}
            return


def apply_desktop_route_strategy(
    rendered: dict,
    strategy: str,
    ai_rules: list[dict] | None = None,
    *,
    os_name: str,
) -> dict:
    """Apply one persistent desktop mixed-mode routing and DNS strategy.

    `os_name` picks the system resolver the ai route sends direct traffic to,
    which is the one part of a strategy that cannot be written the same way for
    every platform. See `system_dns_server`.
    """
    if strategy not in {"china", "gfw", "ai", "global"}:
        raise ValueError(f"Unsupported route strategy: {strategy}")
    if os_name not in SUPPORTED_CLIENT_OS:
        raise ValueError(f"Unsupported client OS: {os_name}")

    configured = copy.deepcopy(rendered)
    dns = configured.setdefault("dns", {})
    route = configured.setdefault("route", {})
    clients = configured.setdefault("http_clients", [])
    if _find_tagged_entry(clients, "rule-downloads") is None:
        clients.append({"tag": "rule-downloads", "detour": "proxy"})
    route["default_http_client"] = "rule-downloads"

    proxy = _find_tagged_entry(configured.get("outbounds", []), "proxy")
    if proxy is None:
        proxy = configured.get("outbounds", [{}])[0]
    protocol = proxy.get("type")
    if protocol not in {"trojan", "hysteria2", "naive"}:
        raise ValueError("Missing supported proxy outbound")
    dns["optimistic"] = False
    configured.setdefault("experimental", {}).setdefault("cache_file", {}).update(
        enabled=True, store_dns=True, path=f"cache-{protocol}-{strategy}.db"
    )
    for rule_set in route.get("rule_set", []):
        if rule_set.get("type") == "remote":
            bundled_remote_rule_set(rule_set)

    ai_tags = [AI_SERVICE_RULE_SET_TAG, AI_GEOSITE_RULE_SET_TAG]
    ai_sets = [
        _ai_geosite_rule_set(),
        {
            "rules": copy.deepcopy(ai_rules)
            if ai_rules is not None
            else load_ai_service_rules(),
            "tag": AI_SERVICE_RULE_SET_TAG,
            "type": "inline",
        },
    ]
    direct_resolver = "system" if strategy == "ai" else "aliyun"
    dns["servers"] = [
        {"detour": "proxy", "server": "8.8.8.8", "tag": "google", "type": "tls"},
        system_dns_server(os_name)
        if strategy == "ai"
        else {"server": "223.5.5.5", "tag": "aliyun", "type": "udp"},
    ]
    dns["rules"] = [{"rule_set": ai_tags, "server": "google"}]
    route["rules"] = _desktop_common_rules([{"outbound": "proxy", "rule_set": ai_tags}])
    regional_sets = route.get("rule_set", [])
    route["rule_set"] = [_adguard_rule_set(route), *ai_sets]
    if strategy in {"china", "gfw"}:
        tags = (
            {"GFWList", "GeoSite-CN", "GeoSite-Apple-CN", "GeoIP-CN"}
            if strategy == "china"
            else {"GFWList"}
        )
        route["rule_set"].extend(rule for rule in regional_sets if rule["tag"] in tags)
        dns["rules"].append({"rule_set": ["GFWList"], "server": "google"})
        route["rules"].append({"outbound": "proxy", "rule_set": ["GFWList"]})
    if strategy == "china":
        dns["rules"].append(
            {"rule_set": ["GeoSite-CN", "GeoSite-Apple-CN"], "server": "aliyun"}
        )
        route["rules"].extend(
            [
                {"outbound": "direct", "rule_set": ["GeoSite-CN", "GeoSite-Apple-CN"]},
                {"outbound": "direct", "rule_set": ["GeoIP-CN"]},
            ]
        )
    dns["final"] = direct_resolver if strategy in {"ai", "gfw"} else "google"
    route["final"] = "direct" if strategy in {"ai", "gfw"} else "proxy"
    configured.setdefault("ntp", {})["detour"] = route["final"]
    _set_outbound_resolver(configured, "direct", direct_resolver)

    # Reapply inventory exceptions after replacing the strategy's rules.
    exceptions = [
        rule
        for rule in rendered.get("route", {}).get("rules", [])
        if rule.get("outbound") == "direct"
        and any(key in rule for key in ("domain", "domain_suffix", "ip_cidr"))
    ]
    route["rules"][4:4] = copy.deepcopy(exceptions)
    dns_exceptions = [
        dict(copy.deepcopy(rule), server=direct_resolver)
        for rule in rendered.get("dns", {}).get("rules", [])
        if any(key in rule for key in ("domain", "domain_suffix"))
        and rule.get("server") in {"aliyun", "system"}
    ]
    dns["rules"][0:0] = dns_exceptions
    return configured


def apply_direct_exceptions(rendered: dict, deployment: DeploymentConfig) -> dict:
    """Install exact-host and explicit suffix exceptions on every client profile."""
    matchers: dict = {}
    try:
        address = ipaddress.ip_address(deployment.host)
    except ValueError:
        matchers["domain"] = [deployment.host]
    else:
        matchers["ip_cidr"] = [f"{address}/{address.max_prefixlen}"]
    if deployment.direct_domain_suffixes:
        matchers["domain_suffix"] = deployment.direct_domain_suffixes.copy()
    rules = rendered.setdefault("route", {}).setdefault("rules", [])
    # Keep sniffing, DNS interception and transport restrictions before bypasses.
    controls = [
        rule
        for rule in rules
        if rule.get("action") in {"sniff", "hijack-dns"}
        or (rule.get("action") == "reject" and rule.get("type") == "logical")
    ]
    rules[:] = controls + [rule for rule in rules if rule not in controls]
    index = next(
        (
            i
            for i, rule in enumerate(rules)
            if "rule_set" in rule or "clash_mode" in rule
        ),
        len(rules),
    )
    rules.insert(index, {**matchers, "outbound": "direct"})
    dns = rendered.setdefault("dns", {})
    servers = dns.setdefault("servers", [])
    if _find_tagged_entry(servers, "aliyun") is None:
        servers.append({"server": "223.5.5.5", "tag": "aliyun", "type": "udp"})
    domain_matchers = {
        key: value for key, value in matchers.items() if key != "ip_cidr"
    }
    if domain_matchers:
        dns.setdefault("rules", []).insert(0, {**domain_matchers, "server": "aliyun"})
    _set_outbound_resolver(rendered, "direct", "aliyun")
    return rendered


def _find_entry(
    entries: list[dict], expected_type: str, entry_label: str, section_name: str
) -> dict:
    for entry in entries:
        if entry.get("type") == expected_type:
            return entry

    raise ValueError(f"Missing {entry_label} entry in {section_name}")


def _require_first_entry(
    entries: list[dict], entry_label: str, section_name: str
) -> dict:
    if not entries:
        raise ValueError(f"Missing {entry_label} entry in {section_name}")

    return entries[0]


def _set_system_proxy(entries: list[dict], set_system_proxy: bool | None) -> None:
    if set_system_proxy is None:
        return

    inbound = _require_first_entry(entries, "client inbound", "inbounds")
    inbound["set_system_proxy"] = set_system_proxy


def _load_pem_lines(path_text: str, label: str) -> list[str]:
    path = Path(path_text)

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValueError(f"TLS {label} file not found: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Unable to read TLS {label} file {path}: {exc}") from exc

    if not any(line.strip() for line in lines):
        raise ValueError(f"TLS {label} file is empty: {path}")

    return lines


def _require_tls_path(path_text: str | None, label: str) -> str:
    if path_text is None:
        raise ValueError(f"TLS {label} path is required when TLS is enabled")

    return path_text


def apply_tls_settings(
    tls: dict,
    deployment: DeploymentConfig,
    *,
    include_server_paths: bool,
) -> None:
    tls["enabled"] = deployment.tls.enabled

    if deployment.tls.server_name is None:
        tls.pop("server_name", None)
    else:
        tls["server_name"] = deployment.tls.server_name

    if not deployment.tls.enabled:
        tls.pop("certificate", None)
        tls.pop("certificate_path", None)
        tls.pop("key", None)
        tls.pop("key_path", None)
        return

    if deployment.tls.self_signed_cert:
        tls["certificate"] = _load_pem_lines(
            _require_tls_path(deployment.tls.certificate_path, "certificate"),
            "certificate",
        )
        tls.pop("certificate_path", None)

        if include_server_paths:
            tls["key"] = _load_pem_lines(
                _require_tls_path(deployment.tls.key_path, "key"),
                "key",
            )
        else:
            tls.pop("key", None)

        tls.pop("key_path", None)
        return

    tls.pop("certificate", None)
    tls.pop("key", None)

    if include_server_paths:
        tls["key_path"] = _require_tls_path(deployment.tls.key_path, "key")
        tls["certificate_path"] = _require_tls_path(
            deployment.tls.certificate_path,
            "certificate",
        )
        return

    tls.pop("key_path", None)
    tls.pop("certificate_path", None)


def _apply_v2ray_api_settings(
    rendered: dict,
    *,
    listen_address: str,
    usernames: list[str],
) -> None:
    experimental = rendered.setdefault("experimental", {})
    experimental["v2ray_api"] = {
        "listen": listen_address,
        "stats": {"enabled": True, "users": usernames},
    }


API_SERVICE_TAG = "connection-api"


def _apply_api_service_settings(
    rendered: dict,
    *,
    listen_address: str,
    secret: str,
) -> None:
    """Add the sing-box 1.14.0 API service that streams per-connection records.

    This is what carries the destination and domain that the V2Ray stats API
    cannot express; the two coexist because sing-box 1.14.0 appends trackers
    rather than replacing them.

    The dashboard is disabled explicitly rather than by omission: it defaults to
    enabled, and leaving it on would have a proxy server periodically download
    and serve a web UI nobody asked for.

    The entry replaces any existing one with the same tag rather than being
    appended blindly, so this reads like its `v2ray_api` sibling: a template that
    already carries a `connection-api` service would otherwise render two of
    them, and sing-box rejects a duplicate tag outright.
    """
    listen, listen_port = split_listen_address(listen_address)
    service = {
        "type": "api",
        "tag": API_SERVICE_TAG,
        "listen": listen,
        "listen_port": listen_port,
        "secret": secret,
        "dashboard": {"enabled": False},
    }
    services = rendered.setdefault("services", [])
    for index, existing in enumerate(services):
        if isinstance(existing, dict) and existing.get("tag") == API_SERVICE_TAG:
            services[index] = service
            return

    services.append(service)


def render_trojan_client_config(
    template: dict,
    deployment: DeploymentConfig,
    user: TrojanUser,
    set_system_proxy: bool | None,
) -> dict:
    """Render a trojan client config from a secret-free template and inventory."""
    rendered = copy.deepcopy(template)
    _set_system_proxy(rendered["inbounds"], set_system_proxy)

    outbound = _find_entry(
        rendered["outbounds"], "trojan", "trojan outbound", "outbounds"
    )
    outbound["server"] = deployment.ip
    outbound["server_port"] = deployment.trojan_port
    outbound["password"] = user.password

    tls = outbound.setdefault("tls", {})
    apply_tls_settings(tls, deployment, include_server_paths=False)

    return apply_direct_exceptions(rendered, deployment)


def render_hysteria2_client_config(
    template: dict,
    deployment: DeploymentConfig,
    hysteria2: Hysteria2Inventory,
    user: Hysteria2User,
    set_system_proxy: bool | None,
) -> dict:
    """Render a hysteria2 client config from a secret-free template and inventory."""
    rendered = copy.deepcopy(template)
    _set_system_proxy(rendered["inbounds"], set_system_proxy)

    outbound = _find_entry(
        rendered["outbounds"], "hysteria2", "hysteria2 outbound", "outbounds"
    )
    outbound["server"] = deployment.ip
    outbound["server_port"] = deployment.hysteria2_port
    outbound["password"] = user.password

    # Port hopping (sing-box >= 1.11): the client rotates its destination UDP
    # port across the range every hop_interval. The server still listens on a
    # single port, so the VPS firewall must redirect the whole range back to
    # deployment.hysteria2_port (see docs/VPS_DEPLOYMENT.md).
    if deployment.hysteria2_port_range is not None:
        outbound["server_ports"] = [deployment.hysteria2_port_range]
        outbound.setdefault("hop_interval", "30s")
    else:
        outbound.pop("server_ports", None)
        outbound.pop("hop_interval", None)

    # Bandwidth (Mbps) switches congestion control from BBR to Hysteria Brutal.
    # Left unset in inventory => keys omitted => BBR (the sing-box default).
    if hysteria2.up_mbps is not None:
        outbound["up_mbps"] = hysteria2.up_mbps
    else:
        outbound.pop("up_mbps", None)

    if hysteria2.down_mbps is not None:
        outbound["down_mbps"] = hysteria2.down_mbps
    else:
        outbound.pop("down_mbps", None)

    obfs = outbound.setdefault("obfs", {})
    obfs["password"] = hysteria2.obfs_password

    tls = outbound.setdefault("tls", {})
    apply_tls_settings(tls, deployment, include_server_paths=False)

    return apply_direct_exceptions(rendered, deployment)


def render_naive_client_config(
    template: dict,
    deployment: DeploymentConfig,
    user: NaiveUser,
    set_system_proxy: bool | None,
) -> dict:
    """Render a naive client config from a secret-free template and inventory."""
    rendered = copy.deepcopy(template)
    _set_system_proxy(rendered["inbounds"], set_system_proxy)

    outbound = _find_entry(
        rendered["outbounds"], "naive", "naive outbound", "outbounds"
    )
    outbound["server"] = deployment.ip
    outbound["server_port"] = deployment.naive_port
    outbound["username"] = user.username
    outbound["password"] = user.password

    tls = outbound.setdefault("tls", {})
    apply_tls_settings(tls, deployment, include_server_paths=False)

    return apply_direct_exceptions(rendered, deployment)


def render_auth_snapshot(web_portal: WebPortalInventory) -> AuthSnapshot:
    """Build the auth snapshot payload from runtime inventory."""
    return AuthSnapshot(
        users=[
            AuthSnapshotUser(
                username=user.username,
                password_hash=user.password_hash,
                enabled=user.enabled,
                admin=user.admin,
            )
            for user in web_portal.users
        ]
    )
