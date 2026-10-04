"""The one-config desktop client matches the per-route configs it replaces.

The structural tests compare each Clash mode's view of the config with the
single-route config for that route. The runtime tests run sing-box and skip
without it; `pixi run fetch-sing-box` provides it.
"""

import http.client
import json
import subprocess
import time
from pathlib import Path

import pytest

from sing_box_manager.config_loader import (
    DeploymentConfig,
    Hysteria2Inventory,
    Hysteria2User,
    NaiveUser,
    TlsConfig,
    TrojanUser,
)
from sing_box_manager.desktop_config import (
    DIRECT_SYSTEM_TAG,
    DIRECT_TAG,
    UserProtocols,
    build_desktop_config,
)
from sing_box_manager.release import renderers
from sing_box_manager.release.platforms import ROUTE_STRATEGIES
from tests.test_sing_box_114 import binary as binary
from tests.test_sing_box_114 import deployment as deployment
from tests.test_sing_box_114 import (
    _connect,
    _port,
    _query_dns,
    _running,
    _server,
    _SocksHandler,
    assert_desktop_routing,
    routing_fixtures,
    use_routing_fixtures,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOLS = ("trojan", "hysteria2", "naive")
HYSTERIA2 = Hysteria2Inventory(
    obfs_password="fixture-obfs", users=[], up_mbps=50, down_mbps=200
)
ALL_PROTOCOLS = UserProtocols(
    trojan=TrojanUser(username="alice", password="trojan-secret"),
    hysteria2=Hysteria2User(username="alice", password="hysteria2-secret"),
    naive=NaiveUser(username="alice", password="naive-secret"),
    hysteria2_settings=HYSTERIA2,
)
DEPLOYMENT = DeploymentConfig(
    host="vpn.example.com",
    direct_domain_suffixes=["portal.example.org"],
    ip="203.0.113.10",
    trojan_port=443,
    hysteria2_port=8443,
    hysteria2_port_range="20000:30000",
    naive_port=9443,
    # Client configs never read these; only a self-signed certificate is embedded.
    tls=TlsConfig(
        enabled=True,
        server_name="vpn.example.com",
        key_path="/fixture/key.pem",
        certificate_path="/fixture/cert.pem",
    ),
)


def _single_route_config(protocol: str, route: str, os_name: str) -> dict:
    from sing_box_manager.release.profiles import client_base_config

    template = client_base_config(protocol)
    if protocol == "trojan":
        base = renderers.render_trojan_client_config(
            template, DEPLOYMENT, ALL_PROTOCOLS.trojan, set_system_proxy=False
        )
    elif protocol == "hysteria2":
        base = renderers.render_hysteria2_client_config(
            template,
            DEPLOYMENT,
            HYSTERIA2,
            ALL_PROTOCOLS.hysteria2,
            set_system_proxy=False,
        )
    else:
        base = renderers.render_naive_client_config(
            template, DEPLOYMENT, ALL_PROTOCOLS.naive, set_system_proxy=False
        )
    return renderers.apply_desktop_route_strategy(base, route, os_name=os_name)


def _mode_view(rules: list[dict], mode: str, target: str) -> tuple[list[dict], str]:
    """The rules one mode can reach, in order, and where its catch-all sends.

    A rule gated on another mode never matches, and a rule whose conditions an
    earlier routing rule already took is unreachable.
    """
    view: list[dict] = []
    taken: list[dict] = []
    for rule in rules:
        gate = rule.get("clash_mode")
        if gate is not None and gate != mode:
            continue
        rule = {key: value for key, value in rule.items() if key != "clash_mode"}
        conditions = {key: value for key, value in rule.items() if key != target}
        if target in rule and not conditions:
            return view, rule[target]
        if target in rule:
            if conditions in taken:
                continue
            taken.append(conditions)
        view.append(rule)
    raise AssertionError(f"mode {mode} has no catch-all rule")


def _resolvers(config: dict) -> dict[str, str]:
    return {
        outbound["tag"]: outbound["domain_resolver"]["server"]
        for outbound in config["outbounds"]
        if outbound["type"] == "direct"
    }


def _with_resolver(rules: list[dict], resolvers: dict[str, str]) -> list[dict]:
    """Name direct outbounds by the resolver they use for domain destinations.

    A rule that matches only IP addresses never needs a resolver, so it names
    just the outbound type.
    """
    named = []
    for rule in rules:
        outbound = rule.get("outbound")
        if outbound in resolvers:
            ip_only = set(rule) - {"outbound"} <= {"ip_is_private", "ip_cidr"}
            label = "direct" if ip_only else f"direct via {resolvers[outbound]}"
            rule = {**rule, "outbound": label}
        named.append(rule)
    return named


def _by_tag(entries: list[dict]) -> dict[str, dict]:
    return {entry["tag"]: entry for entry in entries}


@pytest.mark.parametrize("os_name", ["linux", "darwin", "windows"])
@pytest.mark.parametrize("route", ROUTE_STRATEGIES)
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_each_mode_matches_the_single_route_config(
    protocol: str, route: str, os_name: str
) -> None:
    old = _single_route_config(protocol, route, os_name)
    new = build_desktop_config(
        DEPLOYMENT,
        ALL_PROTOCOLS,
        os_name=os_name,
        default_protocol=protocol,
        default_route="china",
    )

    # The old configs carry Direct and Global mode rules that never match,
    # because they enable no Clash API.
    old_rules = [
        rule for rule in old["route"]["rules"] if rule.get("clash_mode") is None
    ]
    new_rules, new_final = _mode_view(new["route"]["rules"], route, "outbound")
    assert _with_resolver(new_rules, _resolvers(new)) == _with_resolver(
        old_rules, _resolvers(old)
    )
    assert _with_resolver([{"outbound": new_final}], _resolvers(new)) == (
        _with_resolver([{"outbound": old["route"]["final"]}], _resolvers(old))
    )

    new_dns_rules, new_dns_final = _mode_view(new["dns"]["rules"], route, "server")
    assert new_dns_rules == old["dns"]["rules"]
    assert new_dns_final == old["dns"]["final"]
    new_servers = _by_tag(new["dns"]["servers"])
    for tag, server in _by_tag(old["dns"]["servers"]).items():
        assert new_servers[tag] == server

    new_rule_sets = _by_tag(new["route"]["rule_set"])
    for tag, rule_set in _by_tag(old["route"]["rule_set"]).items():
        assert new_rule_sets[tag] == rule_set

    old_proxy = {k: v for k, v in old["outbounds"][0].items() if k != "tag"}
    new_proxy = _by_tag(new["outbounds"])[protocol]
    assert {k: v for k, v in new_proxy.items() if k != "tag"} == old_proxy
    assert {k: v for k, v in new["inbounds"][0].items() if k != "tag"} == (
        old["inbounds"][0]
    )
    for key in ("log", "http_clients"):
        assert new[key] == old[key]
    assert new["route"]["default_http_client"] == old["route"]["default_http_client"]
    assert new["dns"]["optimistic"] == old["dns"]["optimistic"]


def test_selector_holds_the_enabled_protocols_in_order() -> None:
    protocols = UserProtocols(
        trojan=TrojanUser(username="alice", password="secret"),
        naive=NaiveUser(username="alice", password="secret", enabled=False),
        hysteria2=Hysteria2User(username="alice", password="secret"),
        hysteria2_settings=HYSTERIA2,
    )
    config = build_desktop_config(
        DEPLOYMENT,
        protocols,
        os_name="linux",
        default_protocol="hysteria2",
        default_route="gfw",
    )
    selector = config["outbounds"][0]
    assert selector["tag"] == "proxy"
    assert selector["outbounds"] == ["trojan", "hysteria2"]
    assert selector["default"] == "hysteria2"
    assert "naive" not in _by_tag(config["outbounds"])
    assert config["experimental"]["clash_api"] == {"default_mode": "gfw"}


def test_ai_mode_sends_direct_traffic_through_the_system_resolver() -> None:
    config = build_desktop_config(
        DEPLOYMENT,
        ALL_PROTOCOLS,
        os_name="linux",
        default_protocol="trojan",
        default_route="ai",
    )
    resolvers = _resolvers(config)
    assert resolvers == {DIRECT_TAG: "aliyun", DIRECT_SYSTEM_TAG: "system"}
    _, final = _mode_view(config["route"]["rules"], "ai", "outbound")
    assert resolvers[final] == "system"


def test_an_ip_host_is_a_direct_ip_exception_without_a_dns_rule() -> None:
    deployment = DEPLOYMENT.model_copy(
        update={"host": "203.0.113.10", "direct_domain_suffixes": []}
    )
    config = build_desktop_config(
        deployment,
        ALL_PROTOCOLS,
        os_name="linux",
        default_protocol="trojan",
        default_route="china",
    )
    exceptions = [rule for rule in config["route"]["rules"] if "ip_cidr" in rule]
    assert [rule["ip_cidr"] for rule in exceptions] == [["203.0.113.10/32"]] * 2
    assert not any(
        "domain" in rule or "domain_suffix" in rule for rule in config["dns"]["rules"]
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"os_name": "freebsd"}, "Unsupported client OS"),
        ({"default_route": "cn"}, "Unsupported route strategy"),
        ({"default_protocol": "vless"}, "does not hold the default protocol"),
    ],
)
def test_rejects_unknown_choices(changes: dict, message: str) -> None:
    arguments = {
        "os_name": "linux",
        "default_protocol": "trojan",
        "default_route": "china",
    }
    with pytest.raises(ValueError, match=message):
        build_desktop_config(DEPLOYMENT, ALL_PROTOCOLS, **{**arguments, **changes})


def test_rejects_a_user_without_protocols_or_hysteria2_settings() -> None:
    with pytest.raises(ValueError, match="holds no enabled protocol"):
        build_desktop_config(
            DEPLOYMENT,
            UserProtocols(),
            os_name="linux",
            default_protocol="trojan",
            default_route="china",
        )
    with pytest.raises(ValueError, match="Hysteria2 needs the shared settings"):
        build_desktop_config(
            DEPLOYMENT,
            UserProtocols(hysteria2=Hysteria2User(username="alice", password="x")),
            os_name="linux",
            default_protocol="hysteria2",
            default_route="china",
        )


# Runtime checks against the real binary.


def _fixture_config(deployment: DeploymentConfig, route: str = "china") -> dict:
    return build_desktop_config(
        deployment.model_copy(
            update={"direct_domain_suffixes": ["portal.fixture.invalid"]}
        ),
        UserProtocols(
            trojan=TrojanUser(username="alice", password="fixture"),
            hysteria2=Hysteria2User(username="alice", password="fixture"),
            naive=NaiveUser(username="alice", password="fixture"),
            hysteria2_settings=Hysteria2Inventory(obfs_password="fixture", users=[]),
        ),
        os_name="linux",
        default_protocol="trojan",
        default_route=route,
    )


def _proxy_through(config: dict, ports: dict[str, int]) -> None:
    """Send each protocol's traffic to a local SOCKS fixture instead."""
    for outbound in config["outbounds"]:
        if outbound["tag"] in ports:
            tag = outbound["tag"]
            outbound.clear()
            outbound.update(
                type="socks", tag=tag, server="127.0.0.1", server_port=ports[tag]
            )


def _enable_api(config: dict) -> tuple[int, str]:
    port = _port()
    secret = "fixture-secret"
    config["experimental"]["clash_api"].update(
        external_controller=f"127.0.0.1:{port}", secret=secret
    )
    return port, secret


def _api(port: int, secret: str, method: str, path: str, body: dict | None = None):
    """Call the Clash API, waiting briefly for it to start listening."""
    deadline = time.monotonic() + 5
    while True:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(
                method,
                path,
                body=None if body is None else json.dumps(body),
                headers={"Authorization": f"Bearer {secret}"},
            )
            response = connection.getresponse()
            payload = response.read()
        except ConnectionRefusedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
            continue
        finally:
            connection.close()
        assert response.status < 300, (response.status, payload)
        return json.loads(payload) if payload else None


def test_served_config_passes_sing_box_check_on_every_os(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
) -> None:
    for os_name in ("linux", "darwin", "windows"):
        config = build_desktop_config(
            deployment,
            ALL_PROTOCOLS,
            os_name=os_name,
            default_protocol="naive",
            default_route="gfw",
        )
        path = tmp_path / f"{os_name}.json"
        path.write_text(json.dumps(config))
        result = subprocess.run(
            [str(binary), "check", "-D", str(tmp_path), "-c", str(path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "deprecated" not in result.stderr.lower()


@pytest.mark.parametrize("proxy_available", [True, False])
def test_each_mode_routes_like_its_single_route_config(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
    proxy_available: bool,
) -> None:
    config = _fixture_config(deployment)
    with routing_fixtures() as fixtures:
        proxy_port = fixtures.proxy.server_address[1] if proxy_available else _port()
        _proxy_through(config, dict.fromkeys(PROTOCOLS, proxy_port))
        api_port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        with _running(binary, tmp_path, config):
            for mode in ROUTE_STRATEGIES:
                _api(api_port, secret, "PATCH", "/configs", {"mode": mode})
                assert _api(api_port, secret, "GET", "/configs")["mode"] == mode
                fixtures.reset_counters()
                assert_desktop_routing(
                    config["inbounds"][0]["listen_port"],
                    mode,
                    fixtures,
                    proxy_available=proxy_available,
                )


def test_selector_switches_protocol_without_a_restart(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
) -> None:
    config = _fixture_config(deployment, route="global")
    with (
        routing_fixtures() as fixtures,
        _server(_SocksHandler) as hysteria2,
        _server(_SocksHandler) as naive,
    ):
        _proxy_through(
            config,
            {
                "trojan": fixtures.proxy.server_address[1],
                "hysteria2": hysteria2.server_address[1],
                "naive": naive.server_address[1],
            },
        )
        api_port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        port = config["inbounds"][0]["listen_port"]
        destination = fixtures.direct.server_address[1]
        with _running(binary, tmp_path, config):
            assert b"proxied" in _connect(port, "first.fixture.invalid", destination)
            _api(api_port, secret, "PUT", "/proxies/proxy", {"name": "hysteria2"})
            assert b"proxied" in _connect(port, "second.fixture.invalid", destination)
            assert _api(api_port, secret, "GET", "/proxies/proxy")["now"] == "hysteria2"
    assert fixtures.proxy.targets == ["first.fixture.invalid"]
    assert hysteria2.targets == ["second.fixture.invalid"]
    assert naive.targets == []


def test_a_mode_switch_drops_dns_answers_from_the_last_mode(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
) -> None:
    config = _fixture_config(deployment)
    with routing_fixtures() as fixtures:
        _proxy_through(
            config, dict.fromkeys(PROTOCOLS, fixtures.proxy.server_address[1])
        )
        api_port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        fixtures.google_dns.answer = "192.0.2.1"
        fixtures.dns.answer = "192.0.2.2"
        host = "unlisted.fixture.invalid"
        with _running(binary, tmp_path, config):
            # china resolves unlisted names remotely, gfw through a Chinese
            # resolver, and global remotely again.
            assert _query_dns(fixtures.dns_port, host) == "192.0.2.1"
            _api(api_port, secret, "PATCH", "/configs", {"mode": "gfw"})
            assert _query_dns(fixtures.dns_port, host) == "192.0.2.2"
            _api(api_port, secret, "PATCH", "/configs", {"mode": "global"})
            assert _query_dns(fixtures.dns_port, host) == "192.0.2.1"
    assert fixtures.google_dns.queries == 2
    assert fixtures.dns.queries == 1


def test_mode_and_protocol_survive_a_restart(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
) -> None:
    config = _fixture_config(deployment)
    with routing_fixtures() as fixtures:
        _proxy_through(
            config, dict.fromkeys(PROTOCOLS, fixtures.proxy.server_address[1])
        )
        api_port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        with _running(binary, tmp_path, config):
            _api(api_port, secret, "PATCH", "/configs", {"mode": "ai"})
            _api(api_port, secret, "PUT", "/proxies/proxy", {"name": "naive"})
        with _running(binary, tmp_path, config):
            assert _api(api_port, secret, "GET", "/configs")["mode"] == "ai"
            assert _api(api_port, secret, "GET", "/proxies/proxy")["now"] == "naive"
