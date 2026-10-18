"""Tests for Trojan, Hysteria2, Naive, and auth snapshot renderer helpers."""

import copy
from pathlib import Path

import pytest

from sing_box_manager.release.platforms import MIXED_PROFILE, MOBILE_TUN_PROFILE
from sing_box_manager.release.profiles import build_server_config, client_base_config
from sing_box_manager.release.renderers import (
    ADGUARD_RULE_SET_TAG,
    AI_ANTHROPIC_RULE_SET_TAG,
    AI_GEOSITE_RULE_SET_TAG,
    AI_RULE_SET_URLS,
    ai_rule_sets,
    apply_desktop_route_strategy,
    render_hysteria2_client_config,
    render_naive_client_config,
    render_trojan_client_config,
)
from sing_box_manager.settings import (
    DeploymentConfig,
    Hysteria2Inventory,
    Hysteria2User,
    NaiveInventory,
    NaiveUser,
    RuntimeInventory,
    TlsConfig,
    TrojanInventory,
    TrojanUser,
    WebPortalInventory,
    WebPortalUser,
)

VALID_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "c29tZXNhbHQxMjM0NTY$"
    "c29tZWhhc2h2YWx1ZXNvbWVoaXN0b3J5"
)

CERTIFICATE_LINES = [
    "-----BEGIN CERTIFICATE-----",
    "MIIBselfsignedcertificate==",
    "-----END CERTIFICATE-----",
]

KEY_LINES = [
    "-----BEGIN RSA " + "PRIVATE KEY-----",
    "MIIEselfsignedkey==",
    "-----END RSA " + "PRIVATE KEY-----",
]


@pytest.mark.parametrize("bundled", [True, False])
def test_ai_rule_sets_use_community_binaries_and_proxied_daily_updates(
    bundled: bool,
) -> None:
    rules = ai_rule_sets(bundled=bundled)
    assert {rule["tag"]: rule["url"] for rule in rules} == AI_RULE_SET_URLS
    assert AI_RULE_SET_URLS[AI_ANTHROPIC_RULE_SET_TAG] == (
        "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/"
        "sing/geo/geosite/anthropic.srs"
    )
    for rule in rules:
        assert rule["type"] == "remote"
        assert rule["format"] == "binary"
        assert rule["http_client"] == "rule-downloads"
        assert rule["update_interval"] == "1d"
        assert "rules" not in rule
        assert "download_detour" not in rule
        if bundled:
            assert rule["initial_path"].startswith("rules/")
        else:
            assert "initial_path" not in rule


def test_desktop_caches_are_distinct_and_rule_downloads_use_named_client() -> None:
    paths = set()
    for protocol in ("trojan", "hysteria2", "naive"):
        template = client_base_config(protocol)
        for strategy in ("china", "gfw", "ai", "global"):
            config = apply_desktop_route_strategy(template, strategy, os_name="linux")
            cache = config["experimental"]["cache_file"]
            assert cache["enabled"] and cache["store_dns"]
            assert config["dns"]["optimistic"] is False
            assert not config["dns"].get("disable_expire", False)
            paths.add(cache["path"])
            assert config["http_clients"] == [
                {"tag": "rule-downloads", "detour": "proxy"}
            ]
            assert config["route"]["default_http_client"] == "rule-downloads"
            for rule in config["route"]["rule_set"]:
                if rule["type"] == "remote":
                    assert rule["http_client"] == "rule-downloads"
                    assert "download_detour" not in rule
    assert len(paths) == 12


@pytest.mark.parametrize(
    ("strategy", "route_final", "dns_final", "ntp_detour"),
    [
        ("china", "proxy", "google", "proxy"),
        ("gfw", "direct", "aliyun", "direct"),
        ("ai", "direct", "system", "direct"),
        ("global", "proxy", "google", "proxy"),
    ],
)
def test_desktop_route_strategies_have_finals_and_common_controls(
    strategy: str,
    route_final: str,
    dns_final: str,
    ntp_detour: str,
) -> None:
    template = client_base_config("trojan")
    rendered = apply_desktop_route_strategy(template, strategy, os_name="linux")

    assert rendered["route"]["final"] == route_final
    assert rendered["dns"]["final"] == dns_final
    assert rendered["ntp"]["detour"] == ntp_detour
    rules = rendered["route"]["rules"]
    assert {"ip_is_private": True, "outbound": "direct"} in rules
    assert any(rule.get("action") == "hijack-dns" for rule in rules)
    assert any(
        rule.get("action") == "reject"
        and rule.get("rule_set") == ["AdGuard-DNS-Filter"]
        for rule in rules
    )
    assert {"clash_mode": "Direct", "outbound": "direct"} in rules
    assert {"clash_mode": "Global", "outbound": "proxy"} in rules


def _rule_index(rules: list[dict], predicate) -> int:
    return next(index for index, rule in enumerate(rules) if predicate(rule))


@pytest.mark.parametrize("protocol", ["trojan", "hysteria2", "naive"])
def test_mobile_ai_rules_outrank_mode_overrides_and_china_direct(protocol: str) -> None:
    config = client_base_config(protocol, MOBILE_TUN_PROFILE)
    rules = config["route"]["rules"]
    ai_tags = list(AI_RULE_SET_URLS)
    ai_index = _rule_index(rules, lambda rule: rule.get("rule_set") == ai_tags)
    assert rules[ai_index]["outbound"] == "proxy"
    for index, rule in enumerate(rules):
        if "clash_mode" in rule or rule.get("rule_set") == [
            "geoip-cn",
            "geosite-geolocation-cn",
        ]:
            assert ai_index < index
        if rule.get("action") in {"hijack-dns", "reject"}:
            assert index < ai_index
    assert config["dns"]["rules"][0] == {"rule_set": ai_tags, "server": "google"}
    assert config["dns"]["rules"][1]["server"] == "remote"
    assert (
        next(s for s in config["dns"]["servers"] if s["tag"] == "google")["detour"]
        == "proxy"
    )
    assert config["route"]["rule_set"][:2] == ai_rule_sets(bundled=False)
    assert config["route"]["final"] == "proxy"


@pytest.mark.parametrize("strategy", ["china", "gfw", "ai", "global"])
def test_ai_rule_outranks_adguard_reject_in_all_modes(strategy: str) -> None:
    template = client_base_config("trojan")
    rules = apply_desktop_route_strategy(template, strategy, os_name="linux")["route"][
        "rules"
    ]
    adguard = _rule_index(
        rules,
        lambda rule: (
            rule.get("action") == "reject"
            and rule.get("rule_set") == [ADGUARD_RULE_SET_TAG]
        ),
    )
    quic = _rule_index(
        rules,
        lambda rule: rule.get("action") == "reject" and rule.get("type") == "logical",
    )
    proxied = [
        index
        for index, rule in enumerate(rules)
        if AI_ANTHROPIC_RULE_SET_TAG in rule.get("rule_set", [])
    ]

    # Ahead of AdGuard so a filtered feature-flag host still reaches the vendor,
    # but behind the DNS hijack and the QUIC reject so neither is bypassed.
    assert proxied == [adguard - 1]
    assert quic < proxied[0]


def test_ai_strategy_uses_bundled_community_sets_for_dns_and_route() -> None:
    template = client_base_config("trojan")
    rendered = apply_desktop_route_strategy(template, "ai", os_name="linux")
    anthropic = next(
        rule_set
        for rule_set in rendered["route"]["rule_set"]
        if rule_set["tag"] == AI_ANTHROPIC_RULE_SET_TAG
    )
    adguard = next(
        rule_set
        for rule_set in rendered["route"]["rule_set"]
        if rule_set["tag"] == ADGUARD_RULE_SET_TAG
    )
    geosite = next(
        rule_set
        for rule_set in rendered["route"]["rule_set"]
        if rule_set["tag"] == AI_GEOSITE_RULE_SET_TAG
    )
    ai_tags = list(AI_RULE_SET_URLS)

    assert all(rule["type"] == "remote" for rule in rendered["route"]["rule_set"])
    for remote in (adguard, anthropic, geosite):
        assert remote["type"] == "remote"
        assert remote["format"] == "binary"
        assert "download_detour" not in remote
        assert remote["http_client"] == "rule-downloads"
        assert remote["initial_path"].startswith("rules/")
    assert geosite["url"].startswith("https://raw.githubusercontent.com/")
    assert rendered["dns"]["rules"] == [{"rule_set": ai_tags, "server": "google"}]
    assert {"outbound": "proxy", "rule_set": ai_tags} in rendered["route"]["rules"]
    system = next(
        server for server in rendered["dns"]["servers"] if server["tag"] == "system"
    )
    assert system == {"server": "127.0.0.53", "tag": "system", "type": "udp"}


@pytest.mark.parametrize(
    ("os_name", "server"),
    [
        ("linux", {"server": "127.0.0.53", "tag": "system", "type": "udp"}),
        ("darwin", {"tag": "system", "type": "local"}),
        ("windows", {"tag": "system", "type": "local"}),
    ],
)
@pytest.mark.parametrize("strategy", ["ai-only", "direct"])
def test_system_dns_routes_use_the_host_resolver_without_a_privileged_bind(
    os_name: str, server: dict, strategy: str
) -> None:
    """The ai-only and direct routes resolve direct traffic through the host resolver.

    sing-box's `local` transport queries the link's upstream servers itself and
    binds the query socket to that link, which Linux refused without
    CAP_NET_RAW until the check was relaxed: on Ubuntu 18.04 every lookup came
    back `operation not permitted` and the route lost all direct traffic.
    Linux therefore asks systemd-resolved's stub instead, which needs no
    capability and still answers with the host's own view of DNS.
    """
    template = client_base_config("trojan")
    rendered = apply_desktop_route_strategy(template, strategy, os_name=os_name)

    servers = rendered["dns"]["servers"]
    assert _find_server(servers, "system") == server
    # The tag is what the finals and the direct outbound name, so swapping the
    # transport must not move anything else.
    assert rendered["dns"]["final"] == "system"
    assert next(
        outbound for outbound in rendered["outbounds"] if outbound["tag"] == "direct"
    )["domain_resolver"] == {"server": "system"}


@pytest.mark.parametrize("strategy", ["china", "gfw", "global"])
@pytest.mark.parametrize("os_name", ["linux", "darwin", "windows"])
def test_other_routes_carry_the_same_resolvers_on_every_os(
    strategy: str, os_name: str
) -> None:
    """These routes use Aliyun for direct DNS on every operating system."""
    template = client_base_config("trojan")
    rendered = apply_desktop_route_strategy(template, strategy, os_name=os_name)

    assert _find_server(rendered["dns"]["servers"], "aliyun") == {
        "server": "223.5.5.5",
        "tag": "aliyun",
        "type": "udp",
    }
    assert not any(
        server.get("type") == "local" for server in rendered["dns"]["servers"]
    )


def test_unsupported_client_os_is_refused() -> None:
    template = client_base_config("trojan")
    with pytest.raises(ValueError, match="Unsupported client OS"):
        apply_desktop_route_strategy(template, "ai", os_name="android")


def _find_server(servers: list[dict], tag: str) -> dict:
    return next(server for server in servers if server["tag"] == tag)


def _write_pem(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_inventory() -> RuntimeInventory:
    return RuntimeInventory(
        deployment=DeploymentConfig(
            host="vpn.example.com",
            ip="203.0.113.42",
            trojan_port=8443,
            hysteria2_port=58080,
            naive_port=9443,
            tls=TlsConfig(
                enabled=True,
                key_path="/etc/letsencrypt/live/vpn.example.com/privkey.pem",
                certificate_path="/etc/letsencrypt/live/vpn.example.com/fullchain.pem",
            ),
        ),
        web_portal=WebPortalInventory(
            users=[
                WebPortalUser(
                    username="alice",
                    password_hash=VALID_PASSWORD_HASH,
                    enabled=True,
                ),
                WebPortalUser(
                    username="carol",
                    password_hash=VALID_PASSWORD_HASH,
                    enabled=False,
                ),
            ]
        ),
        trojan=TrojanInventory(
            users=[
                TrojanUser(
                    username="alice", password="alice-trojan-secret", enabled=True
                ),
                TrojanUser(
                    username="carol", password="carol-trojan-secret", enabled=False
                ),
            ]
        ),
        hysteria2=Hysteria2Inventory(
            obfs_password="shared-obfs-secret",
            users=[
                Hysteria2User(
                    username="bob",
                    password="bob-hysteria2-secret",
                    enabled=True,
                ),
                Hysteria2User(
                    username="carol",
                    password="carol-hysteria2-secret",
                    enabled=False,
                ),
            ],
        ),
        naive=NaiveInventory(
            users=[
                NaiveUser(
                    username="alice", password="alice-naive-secret", enabled=True
                ),
                NaiveUser(
                    username="carol", password="carol-naive-secret", enabled=False
                ),
            ]
        ),
    )


class TestTrojanRenderers:
    def test_render_trojan_client_config_injects_runtime_values(self):
        inventory = _build_inventory()
        user = inventory.trojan.users[0]
        template = {
            "inbounds": [{"type": "mixed", "set_system_proxy": False}],
            "outbounds": [
                {
                    "type": "trojan",
                    "server": "127.0.0.1",
                    "server_port": 443,
                    "password": "template-pass",
                    "tls": {
                        "enabled": True,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        }

        rendered = render_trojan_client_config(
            template,
            inventory.deployment,
            user,
            set_system_proxy=True,
        )

        assert rendered["inbounds"][0]["set_system_proxy"] is True
        assert rendered["outbounds"][0]["server"] == "203.0.113.42"
        assert rendered["outbounds"][0]["server_port"] == 8443
        assert rendered["outbounds"][0]["password"] == "alice-trojan-secret"
        assert rendered["outbounds"][0]["tls"]["enabled"] is True
        assert rendered["outbounds"][0]["tls"]["server_name"] == "vpn.example.com"
        assert "key_path" not in rendered["outbounds"][0]["tls"]
        assert "certificate_path" not in rendered["outbounds"][0]["tls"]

    def test_render_trojan_client_config_requires_trojan_outbound(self):
        inventory = _build_inventory()
        user = inventory.trojan.users[0]

        with pytest.raises(
            ValueError, match="Missing trojan outbound entry in outbounds"
        ):
            render_trojan_client_config(
                {
                    "inbounds": [{"type": "mixed", "set_system_proxy": False}],
                    "outbounds": [],
                },
                inventory.deployment,
                user,
                set_system_proxy=True,
            )


class TestHysteria2Renderers:
    def test_render_hysteria2_client_config_injects_runtime_values(self):
        inventory = _build_inventory()
        user = inventory.hysteria2.users[0]
        template = {
            "inbounds": [{"type": "mixed", "set_system_proxy": False}],
            "outbounds": [
                {
                    "type": "hysteria2",
                    "server": "127.0.0.1",
                    "server_port": 443,
                    "password": "template-pass",
                    "obfs": {"type": "salamander", "password": "template-obfs"},
                    "tls": {
                        "enabled": False,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        }

        rendered = render_hysteria2_client_config(
            template,
            inventory.deployment,
            inventory.hysteria2,
            user,
            set_system_proxy=True,
        )

        assert rendered["inbounds"][0]["set_system_proxy"] is True
        assert rendered["outbounds"][0]["server"] == "203.0.113.42"
        assert rendered["outbounds"][0]["server_port"] == 58080
        assert rendered["outbounds"][0]["password"] == "bob-hysteria2-secret"
        assert rendered["outbounds"][0]["obfs"]["password"] == "shared-obfs-secret"
        assert rendered["outbounds"][0]["tls"]["enabled"] is True
        assert rendered["outbounds"][0]["tls"]["server_name"] == "vpn.example.com"
        assert "key_path" not in rendered["outbounds"][0]["tls"]
        assert "certificate_path" not in rendered["outbounds"][0]["tls"]
        # Port hopping and Brutal bandwidth are off unless the inventory opts in.
        assert "server_ports" not in rendered["outbounds"][0]
        assert "up_mbps" not in rendered["outbounds"][0]

    def test_render_hysteria2_client_config_enables_port_hopping(self):
        deployment = DeploymentConfig(
            host="vpn.example.com",
            ip="203.0.113.42",
            trojan_port=8443,
            hysteria2_port=58080,
            hysteria2_port_range="20000:50000",
            naive_port=9443,
            tls=TlsConfig(enabled=False),
        )
        hysteria2 = Hysteria2Inventory(
            obfs_password="shared-obfs-secret",
            up_mbps=50,
            down_mbps=200,
            users=[Hysteria2User(username="bob", password="bob-secret", enabled=True)],
        )
        template = {
            "inbounds": [{"type": "mixed", "set_system_proxy": False}],
            "outbounds": [
                {
                    "type": "hysteria2",
                    "server": "127.0.0.1",
                    "server_port": 443,
                    "password": "template-pass",
                    "obfs": {"type": "salamander", "password": "template-obfs"},
                    "tls": {"enabled": False},
                }
            ],
        }

        rendered = render_hysteria2_client_config(
            template,
            deployment,
            hysteria2,
            hysteria2.users[0],
            set_system_proxy=None,
        )

        outbound = rendered["outbounds"][0]
        assert outbound["server_ports"] == ["20000:50000"]
        assert outbound["hop_interval"] == "30s"
        assert outbound["up_mbps"] == 50
        assert outbound["down_mbps"] == 200
        # The single base port is kept as a fallback (ignored when hopping).
        assert outbound["server_port"] == 58080

    def test_render_hysteria2_client_config_strips_stale_hopping_keys(self):
        inventory = _build_inventory()
        user = inventory.hysteria2.users[0]
        template = {
            "inbounds": [{"type": "mixed", "set_system_proxy": False}],
            "outbounds": [
                {
                    "type": "hysteria2",
                    "server": "127.0.0.1",
                    "server_port": 443,
                    "password": "template-pass",
                    "server_ports": ["1:2"],
                    "hop_interval": "10s",
                    "up_mbps": 999,
                    "down_mbps": 999,
                    "obfs": {"type": "salamander", "password": "template-obfs"},
                    "tls": {"enabled": False},
                }
            ],
        }

        rendered = render_hysteria2_client_config(
            template,
            inventory.deployment,
            inventory.hysteria2,
            user,
            set_system_proxy=None,
        )

        outbound = rendered["outbounds"][0]
        assert "server_ports" not in outbound
        assert "hop_interval" not in outbound
        assert "up_mbps" not in outbound
        assert "down_mbps" not in outbound

    def test_render_hysteria2_client_config_requires_hysteria2_outbound(self):
        inventory = _build_inventory()
        user = inventory.hysteria2.users[0]

        with pytest.raises(
            ValueError, match="Missing hysteria2 outbound entry in outbounds"
        ):
            render_hysteria2_client_config(
                {
                    "inbounds": [{"type": "mixed", "set_system_proxy": False}],
                    "outbounds": [],
                },
                inventory.deployment,
                inventory.hysteria2,
                user,
                set_system_proxy=True,
            )


class TestNaiveRenderers:
    def test_render_naive_client_config_injects_runtime_values(self):
        inventory = _build_inventory()
        user = inventory.naive.users[0]
        template = {
            "inbounds": [{"type": "mixed", "set_system_proxy": False}],
            "outbounds": [
                {
                    "type": "naive",
                    "server": "127.0.0.1",
                    "server_port": 443,
                    "username": "template-user",
                    "password": "template-pass",
                    "tls": {
                        "enabled": False,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        }

        rendered = render_naive_client_config(
            template,
            inventory.deployment,
            user,
            set_system_proxy=True,
        )

        assert rendered["inbounds"][0]["set_system_proxy"] is True
        assert rendered["outbounds"][0]["server"] == "203.0.113.42"
        assert rendered["outbounds"][0]["server_port"] == 9443
        assert rendered["outbounds"][0]["username"] == "alice"
        assert rendered["outbounds"][0]["password"] == "alice-naive-secret"
        assert rendered["outbounds"][0]["tls"]["enabled"] is True
        assert rendered["outbounds"][0]["tls"]["server_name"] == "vpn.example.com"
        assert "key_path" not in rendered["outbounds"][0]["tls"]
        assert "certificate_path" not in rendered["outbounds"][0]["tls"]

    def test_render_naive_client_config_requires_naive_outbound(self):
        inventory = _build_inventory()
        user = inventory.naive.users[0]

        with pytest.raises(
            ValueError, match="Missing naive outbound entry in outbounds"
        ):
            render_naive_client_config(
                {
                    "inbounds": [{"type": "mixed", "set_system_proxy": False}],
                    "outbounds": [],
                },
                inventory.deployment,
                user,
                set_system_proxy=True,
            )


class TestSharedRendererBehavior:
    def test_renderers_can_disable_tls(self):
        inventory = _build_inventory()
        inventory.deployment.tls.enabled = False

        trojan_client = render_trojan_client_config(
            {
                "inbounds": [{"type": "mixed", "set_system_proxy": False}],
                "outbounds": [
                    {
                        "type": "trojan",
                        "server": "127.0.0.1",
                        "server_port": 443,
                        "password": "template-pass",
                        "tls": {
                            "enabled": True,
                            "server_name": "template.example.com",
                            "key_path": "/tmp/template.key",
                            "certificate_path": "/tmp/template.crt",
                        },
                    }
                ],
            },
            inventory.deployment,
            inventory.trojan.users[0],
            set_system_proxy=None,
        )
        server = build_server_config(inventory)

        assert trojan_client["outbounds"][0]["tls"]["enabled"] is False
        assert "key_path" not in trojan_client["outbounds"][0]["tls"]
        assert "certificate_path" not in trojan_client["outbounds"][0]["tls"]
        for inbound in server["inbounds"]:
            assert inbound["tls"]["enabled"] is False
            assert "key_path" not in inbound["tls"]
            assert "certificate_path" not in inbound["tls"]

    def test_renderers_inline_self_signed_tls_material(self, tmp_path: Path):
        inventory = _build_inventory()
        certificate_path = tmp_path / "server.crt"
        key_path = tmp_path / "server.key"
        _write_pem(certificate_path, CERTIFICATE_LINES)
        _write_pem(key_path, KEY_LINES)

        inventory.deployment.tls.certificate_path = str(certificate_path)
        inventory.deployment.tls.key_path = str(key_path)
        inventory.deployment.tls.self_signed_cert = True

        trojan_client = render_trojan_client_config(
            {
                "inbounds": [{"type": "mixed", "set_system_proxy": False}],
                "outbounds": [
                    {
                        "type": "trojan",
                        "server": "127.0.0.1",
                        "server_port": 443,
                        "password": "template-pass",
                        "tls": {
                            "enabled": True,
                            "server_name": "template.example.com",
                            "key_path": "/tmp/template.key",
                            "certificate_path": "/tmp/template.crt",
                            "key": ["template-key"],
                        },
                    }
                ],
            },
            inventory.deployment,
            inventory.trojan.users[0],
            set_system_proxy=None,
        )

        server = build_server_config(inventory)

        assert trojan_client["outbounds"][0]["tls"]["certificate"] == CERTIFICATE_LINES
        assert "certificate_path" not in trojan_client["outbounds"][0]["tls"]
        assert "key_path" not in trojan_client["outbounds"][0]["tls"]
        assert "key" not in trojan_client["outbounds"][0]["tls"]

        for inbound in server["inbounds"]:
            assert inbound["tls"]["certificate"] == CERTIFICATE_LINES
            assert inbound["tls"]["key"] == KEY_LINES
            assert "certificate_path" not in inbound["tls"]
            assert "key_path" not in inbound["tls"]

    def test_renderers_fail_when_self_signed_tls_source_file_is_missing(self):
        inventory = _build_inventory()
        inventory.deployment.tls.self_signed_cert = True
        inventory.deployment.tls.certificate_path = "/tmp/missing-self-signed.crt"
        inventory.deployment.tls.key_path = "/tmp/missing-self-signed.key"

        with pytest.raises(ValueError, match="TLS certificate file not found"):
            render_trojan_client_config(
                {
                    "inbounds": [{"type": "mixed", "set_system_proxy": False}],
                    "outbounds": [
                        {
                            "type": "trojan",
                            "server": "127.0.0.1",
                            "server_port": 443,
                            "password": "template-pass",
                            "tls": {
                                "enabled": True,
                                "server_name": "template.example.com",
                            },
                        }
                    ],
                },
                inventory.deployment,
                inventory.trojan.users[0],
                set_system_proxy=None,
            )

    def test_renderers_do_not_mutate_templates(self):
        inventory = _build_inventory()
        trojan_user = inventory.trojan.users[0]
        hysteria2_user = inventory.hysteria2.users[0]
        trojan_client_template = {
            "inbounds": [{"type": "mixed", "set_system_proxy": False}],
            "outbounds": [
                {
                    "type": "trojan",
                    "server": "127.0.0.1",
                    "server_port": 443,
                    "password": "template-pass",
                    "tls": {
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        }
        hysteria2_client_template = {
            "inbounds": [{"type": "mixed", "set_system_proxy": False}],
            "outbounds": [
                {
                    "type": "hysteria2",
                    "server": "127.0.0.1",
                    "server_port": 58080,
                    "password": "template-pass",
                    "obfs": {"type": "salamander", "password": "template-obfs"},
                    "tls": {
                        "enabled": True,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        }
        original_trojan_client_template = copy.deepcopy(trojan_client_template)
        original_hysteria2_client_template = copy.deepcopy(hysteria2_client_template)

        render_trojan_client_config(
            trojan_client_template,
            inventory.deployment,
            trojan_user,
            set_system_proxy=None,
        )
        render_hysteria2_client_config(
            hysteria2_client_template,
            inventory.deployment,
            inventory.hysteria2,
            hysteria2_user,
            set_system_proxy=None,
        )

        assert trojan_client_template == original_trojan_client_template
        assert hysteria2_client_template == original_hysteria2_client_template


@pytest.mark.parametrize("protocol", ["trojan", "hysteria2", "naive"])
@pytest.mark.parametrize(
    "profile", ["china-direct", "gfw-proxy", "ai-only", "global", "direct", "-tun"]
)
def test_direct_exceptions_survive_every_profile(protocol: str, profile: str) -> None:
    from tests.test_sing_box_114 import render_client

    deployment = DeploymentConfig(
        host=".Portal.Example.NET.",
        ip="192.0.2.1",
        trojan_port=443,
        hysteria2_port=443,
        naive_port=443,
        tls=TlsConfig(enabled=False),
        direct_domain_suffixes=[".Example.COM.", "example.com"],
    )
    config = render_client(
        protocol, profile if profile.startswith("-") else "", deployment
    )
    if not profile.startswith("-"):
        config = apply_desktop_route_strategy(config, profile, os_name="linux")
    rules = config["route"]["rules"]
    exception = next(
        rule for rule in rules if rule.get("domain") == ["portal.example.net"]
    )
    assert exception == {
        "domain": ["portal.example.net"],
        "domain_suffix": ["example.com"],
        "outbound": "direct",
    }
    index = rules.index(exception)
    assert all(
        index < i
        for i, rule in enumerate(rules)
        if "rule_set" in rule or "clash_mode" in rule
    )
    assert all(
        i < index
        for i, rule in enumerate(rules)
        if rule.get("action") == "hijack-dns"
        or (rule.get("action") == "reject" and rule.get("type") == "logical")
    )
    resolver = "system" if profile in {"ai-only", "direct"} else "aliyun"
    if profile == "direct":
        # All direct-mode DNS already uses the system resolver.
        assert config["dns"]["rules"] == []
        assert config["dns"]["final"] == resolver
    else:
        assert config["dns"]["rules"][0] == {
            "domain": ["portal.example.net"],
            "domain_suffix": ["example.com"],
            "server": resolver,
        }
    assert next(out for out in config["outbounds"] if out["tag"] == "direct")[
        "domain_resolver"
    ] == {"server": resolver}


@pytest.mark.parametrize(
    "host,cidr", [("192.0.2.8", "192.0.2.8/32"), ("2001:db8::8", "2001:db8::8/128")]
)
@pytest.mark.parametrize(
    "strategy", ["china-direct", "gfw-proxy", "ai-only", "global", "direct"]
)
def test_ip_host_is_an_exact_direct_exception(
    host: str, cidr: str, strategy: str
) -> None:
    from tests.test_sing_box_114 import render_client

    deployment = DeploymentConfig(
        host=host,
        ip="192.0.2.1",
        trojan_port=443,
        hysteria2_port=443,
        naive_port=443,
        tls=TlsConfig(enabled=False),
    )
    config = apply_desktop_route_strategy(
        render_client("trojan", "", deployment), strategy, os_name="linux"
    )
    assert {"ip_cidr": [cidr], "outbound": "direct"} in config["route"]["rules"]
    assert not any("domain" in rule for rule in config["dns"]["rules"])


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        ".",
        "..example.com",
        "example..com",
        "example.com..",
        "https://example.com",
        "*.example.com",
        "example.com/path",
        "bad_name.com",
        "-bad.com",
        "bad-.com",
        "192.0.2.1",
        "2001:db8::1",
        "a" * 64 + ".com",
    ],
)
def test_invalid_direct_suffix_is_rejected(suffix: str) -> None:
    with pytest.raises(ValueError):
        DeploymentConfig(
            host="portal.example.com",
            ip="192.0.2.1",
            trojan_port=443,
            hysteria2_port=443,
            naive_port=443,
            tls=TlsConfig(enabled=False),
            direct_domain_suffixes=[suffix],
        )


def test_normalized_host_keeps_matching_tls_name() -> None:
    deployment = DeploymentConfig(
        host="Portal.Example.COM.",
        ip="192.0.2.1",
        trojan_port=443,
        hysteria2_port=443,
        naive_port=443,
        tls=TlsConfig(
            enabled=True,
            server_name="portal.example.com",
            key_path="fixture.key",
            certificate_path="fixture.crt",
        ),
    )
    assert deployment.host == deployment.tls.server_name == "portal.example.com"


@pytest.mark.parametrize("protocol", ["trojan", "hysteria2", "naive"])
@pytest.mark.parametrize("profile", [MIXED_PROFILE, MOBILE_TUN_PROFILE])
def test_client_mixed_inbounds_listen_on_loopback_only(protocol: str, profile) -> None:
    """An unauthenticated mixed inbound on a public or LAN address is an open relay."""
    config = client_base_config(protocol, profile)

    mixed = [inbound for inbound in config["inbounds"] if inbound["type"] == "mixed"]

    assert all(inbound["listen"] == "127.0.0.1" for inbound in mixed)
