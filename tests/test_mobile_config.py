"""Unified mobile policy, selector and persistence checks with local fixtures."""

import copy
import socket
import ssl

import pytest

from sing_box_manager.desktop_config import (
    UserProtocols,
    build_desktop_config,
    build_mobile_config,
)
from sing_box_manager.release.platforms import ROUTE_STRATEGIES
from tests.test_desktop_config import (
    ALL_PROTOCOLS,
    DEPLOYMENT,
    PROTOCOLS,
    _api,
    _enable_api,
    _mode_view,
    _proxy_through,
)
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
from tests.test_sing_box_114 import binary as binary
from tests.test_sing_box_114 import deployment as deployment


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_mobile_has_the_desktop_policy_without_aliases_or_desktop_storage(protocol):
    original = DEPLOYMENT.model_dump()
    config = build_mobile_config(DEPLOYMENT, ALL_PROTOCOLS, default_protocol=protocol)
    desktop = build_desktop_config(
        DEPLOYMENT,
        ALL_PROTOCOLS,
        os_name="darwin",
        default_protocol=protocol,
        default_route="china-direct",
    )
    assert DEPLOYMENT.model_dump() == original
    assert config["outbounds"] == desktop["outbounds"]
    assert config["outbounds"][0]["outbounds"] == list(PROTOCOLS)
    assert config["outbounds"][0]["default"] == protocol
    assert config["experimental"] == {
        "cache_file": {"enabled": True, "store_dns": True},
        "clash_api": {"default_mode": "china-direct"},
    }
    assert config["inbounds"] == [
        {
            "type": "tun",
            "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
            "auto_route": True,
            "strict_route": True,
        }
    ]
    modes = {
        rule["clash_mode"]
        for section in ("dns", "route")
        for rule in config[section]["rules"]
        if "clash_mode" in rule
    }
    assert modes == set(ROUTE_STRATEGIES)
    assert not any(server["type"] == "fakeip" for server in config["dns"]["servers"])
    assert {server["tag"]: server for server in config["dns"]["servers"]}["system"] == {
        "type": "local",
        "tag": "system",
    }
    assert config["route"]["rules"][1]["action"] == "hijack-dns"
    assert config["route"]["rules"][2]["ip_is_private"] is True
    for mode in ROUTE_STRATEGIES:
        mobile_rules = copy.deepcopy(config["route"]["rules"])
        # Mobile intercepts captured DNS before the private-address shortcut.
        mobile_rules[1], mobile_rules[2] = mobile_rules[2], mobile_rules[1]
        assert _mode_view(mobile_rules, mode, "outbound") == _mode_view(
            desktop["route"]["rules"],
            mode,
            "outbound",
        )
        assert _mode_view(config["dns"]["rules"], mode, "server") == _mode_view(
            desktop["dns"]["rules"],
            mode,
            "server",
        )
    expected_sets = copy.deepcopy(desktop["route"]["rule_set"])
    for rule in expected_sets:
        rule.pop("initial_path")
    assert config["route"]["rule_set"] == expected_sets
    assert len(expected_sets) == 7


def test_mobile_omits_disabled_protocols_and_refuses_an_unavailable_default():
    protocols = UserProtocols(
        trojan=ALL_PROTOCOLS.trojan,
        naive=ALL_PROTOCOLS.naive.model_copy(update={"enabled": False}),
    )
    config = build_mobile_config(DEPLOYMENT, protocols, default_protocol="trojan")
    assert config["outbounds"][0]["outbounds"] == ["trojan"]
    assert not any(out["tag"] in ("naive", "hysteria2") for out in config["outbounds"])
    with pytest.raises(ValueError, match="does not hold the default protocol"):
        build_mobile_config(DEPLOYMENT, protocols, default_protocol="naive")
    with pytest.raises(ValueError, match="no enabled protocol"):
        build_mobile_config(DEPLOYMENT, UserProtocols(), default_protocol="trojan")


def fixture_config(deployment):
    return build_mobile_config(
        deployment.model_copy(
            update={"direct_domain_suffixes": ["portal.fixture.invalid"]}
        ),
        ALL_PROTOCOLS,
        default_protocol="trojan",
    )


@pytest.mark.parametrize("proxy_available", [True, False])
def test_all_protocols_and_five_modes_follow_the_desktop_routes(
    tmp_path,
    binary,
    deployment,
    proxy_available,
):
    config = fixture_config(deployment)
    # This checks core policy, not native mobile TUN capture or app controls.
    config["inbounds"] = [{"type": "mixed"}]
    with routing_fixtures() as fixtures:
        port = fixtures.proxy.server_address[1] if proxy_available else _port()
        _proxy_through(config, dict.fromkeys(PROTOCOLS, port))
        api_port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        with _running(binary, tmp_path, config):
            assert set(_api(api_port, secret, "GET", "/configs")["mode-list"]) == set(
                ROUTE_STRATEGIES
            )
            for protocol in PROTOCOLS:
                _api(api_port, secret, "PUT", "/proxies/proxy", {"name": protocol})
                for mode in ROUTE_STRATEGIES:
                    _api(api_port, secret, "PATCH", "/configs", {"mode": mode})
                    assert _api(api_port, secret, "GET", "/configs")["mode"] == mode
                    assert (
                        _api(api_port, secret, "GET", "/proxies/proxy")["now"]
                        == protocol
                    )
                    fixtures.reset_counters()
                    assert_desktop_routing(
                        config["inbounds"][0]["listen_port"],
                        mode,
                        fixtures,
                        proxy_available=proxy_available,
                        expect_updates=False,
                    )


def test_selector_paths_and_choices_survive_a_restart(tmp_path, binary, deployment):
    config = fixture_config(deployment)
    config["inbounds"] = [{"type": "mixed"}]
    with (
        routing_fixtures() as fixtures,
        _server(_SocksHandler) as hy2,
        _server(_SocksHandler) as naive,
    ):
        servers = {"trojan": fixtures.proxy, "hysteria2": hy2, "naive": naive}
        _proxy_through(
            config, {name: server.server_address[1] for name, server in servers.items()}
        )
        api_port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        with _running(binary, tmp_path, config):
            for protocol in PROTOCOLS:
                _api(api_port, secret, "PUT", "/proxies/proxy", {"name": protocol})
                host = protocol + ".fixture.invalid"
                assert b"proxied" in _connect(
                    config["inbounds"][0]["listen_port"],
                    host,
                    fixtures.direct.server_address[1],
                )
                assert servers[protocol].targets == [host]
            _api(api_port, secret, "PATCH", "/configs", {"mode": "ai-only"})
        with _running(binary, tmp_path, config):
            assert _api(api_port, secret, "GET", "/configs")["mode"] == "ai-only"
            assert _api(api_port, secret, "GET", "/proxies/proxy")["now"] == "naive"
        config["outbounds"][0]["outbounds"].remove("naive")
        config["outbounds"] = [
            out for out in config["outbounds"] if out["tag"] != "naive"
        ]
        with _running(binary, tmp_path, config):
            assert _api(api_port, secret, "GET", "/proxies/proxy")["now"] == "trojan"


def test_mode_changes_clear_real_dns_answers(tmp_path, binary, deployment):
    config = fixture_config(deployment)
    config["inbounds"] = [{"type": "mixed"}]
    with routing_fixtures() as fixtures:
        _proxy_through(
            config, dict.fromkeys(PROTOCOLS, fixtures.proxy.server_address[1])
        )
        api_port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        fixtures.google_dns.answer = "192.0.2.1"
        fixtures.dns.answer = "192.0.2.2"
        with _running(binary, tmp_path, config):
            for mode in ROUTE_STRATEGIES:
                _api(api_port, secret, "PATCH", "/configs", {"mode": mode})
                expected = (
                    "192.0.2.1" if mode in ("china-direct", "global") else "192.0.2.2"
                )
                assert (
                    _query_dns(fixtures.dns_port, "unlisted.fixture.invalid")
                    == expected
                )
        assert fixtures.google_dns.queries == 2
        assert fixtures.dns.queries == 3


def test_real_dns_and_tls_sni_keep_ai_ahead_of_china_ip_rules(
    tmp_path, binary, deployment
):
    config = fixture_config(deployment)
    config["inbounds"] = [{"type": "mixed"}]
    with routing_fixtures() as fixtures, _server(_SocksHandler) as direct_path:
        # Even a misrouted reserved address reaches only a local stand-in.
        ports = dict.fromkeys(PROTOCOLS, fixtures.proxy.server_address[1])
        ports.update(
            dict.fromkeys(("direct", "direct-system"), direct_path.server_address[1])
        )
        _proxy_through(config, ports)
        api_port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        fixtures.google_dns.answer = "203.0.113.10"
        host = "api.anthropic.com"
        with _running(binary, tmp_path, config):
            for mode in ("ai-only", "gfw-proxy", "china-direct"):
                _api(api_port, secret, "PATCH", "/configs", {"mode": mode})
                address = _query_dns(fixtures.dns_port, host)
                assert address == "203.0.113.10"
                outgoing = ssl.MemoryBIO()
                stream = ssl.create_default_context().wrap_bio(
                    ssl.MemoryBIO(),
                    outgoing,
                    server_hostname=host,
                )
                with pytest.raises(ssl.SSLWantReadError):
                    stream.do_handshake()
                # Send a real ClientHello with SNI, but no provider request.
                with socket.create_connection(
                    ("127.0.0.1", config["inbounds"][0]["listen_port"]), timeout=5
                ) as connection:
                    connection.sendall(
                        f"CONNECT {address}:443 HTTP/1.1\r\nHost: {address}\r\n\r\n".encode()
                    )
                    assert b"200" in connection.recv(1024).split(b"\r\n")[0]
                    connection.sendall(outgoing.read())
                    assert b"proxied" in connection.recv(4096)
                assert len(fixtures.proxy.targets) > 0
                assert not direct_path.targets
                fixtures.reset_counters()


def test_old_mode_case_and_removed_rule_cache(tmp_path, binary, deployment):
    config = fixture_config(deployment)
    config["inbounds"] = [{"type": "mixed"}]
    with routing_fixtures() as fixtures:
        _proxy_through(
            config, dict.fromkeys(PROTOCOLS, fixtures.proxy.server_address[1])
        )
        api_port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        # Seed a cached Rule selection, then start the new five-mode config.
        config["experimental"]["clash_api"]["default_mode"] = "Rule"
        with _running(binary, tmp_path, config):
            for old, canonical in (("Direct", "direct"), ("Global", "global")):
                _api(api_port, secret, "PATCH", "/configs", {"mode": old})
                assert _api(api_port, secret, "GET", "/configs")["mode"] == canonical
            _api(api_port, secret, "PATCH", "/configs", {"mode": "Rule"})
        config["experimental"]["clash_api"]["default_mode"] = "china-direct"
        with _running(binary, tmp_path, config):
            status = _api(api_port, secret, "GET", "/configs")
            assert status["mode"] == "china-direct"
            assert set(status["mode-list"]) == set(ROUTE_STRATEGIES)
            _api(api_port, secret, "PATCH", "/configs", {"mode": "Rule"})
            assert _api(api_port, secret, "GET", "/configs")["mode"] == "china-direct"
