"""Real Snell v6 authentication and routing, never a substitute transport."""

import copy
import socket
import socketserver
from contextlib import contextmanager, nullcontext

import pytest

from sing_box_manager.desktop_config import (
    build_desktop_config,
    build_mobile_config,
    snell_outbound,
)
from sing_box_manager.release.platforms import ROUTE_STRATEGIES
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.subscription import user_protocols
from tests.snell_fixtures import with_snell
from tests.test_desktop_config import _api, _enable_api, _proxy_through
from tests.test_m3_profiles import profile_inventory
from tests.test_m3_runtime import _destination
from tests.test_sing_box_114 import (
    _connect,
    _port,
    _running,
    _server,
    assert_desktop_routing,
    routing_fixtures,
    use_routing_fixtures,
)
from tests.test_sing_box_114 import binary as binary


@contextmanager
def snell_node(binary, tmp_path, *, client_routing=False, udp_origin=False):
    inventory = with_snell(profile_inventory())
    inventory.deployment.ip = "127.0.0.1"
    inventory.deployment.snell_port = _port()
    server = build_server_config(inventory)
    server["inbounds"] = [
        dict(i, listen="127.0.0.1") for i in server["inbounds"] if i["type"] == "snell"
    ]
    if client_routing:
        # Synthetic .invalid origins have no public DNS. This fixture checks
        # client routes and Snell transport, not the separately tested node egress policy.
        server["route"]["rules"] = []
    root = tmp_path / "node"
    root.mkdir()
    destination = nullcontext(None) if udp_origin else _destination(server)
    with destination as origin, _running(binary, root, server):
        yield inventory, origin


@pytest.mark.parametrize("user", ["alice", "bob"])
def test_each_user_authenticates_with_its_own_key(tmp_path, binary, user):
    with snell_node(binary, tmp_path) as (inventory, origin):
        protocols = user_protocols(inventory, user)
        outbound = snell_outbound(
            inventory.deployment, protocols.snell, protocols.snell_settings
        )
        outbound.pop("domain_resolver")
        port = _port()
        config = {
            "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": port}],
            "outbounds": [outbound],
            "route": {"final": "snell"},
        }
        root = tmp_path / "client"
        root.mkdir()
        with _running(binary, root, config):
            assert b"proxied" in _connect(port, "192.0.2.10", 18080)
        assert origin.targets == ["192.0.2.10"]


@pytest.mark.parametrize(
    "invalid", ["psk", "userkey", "missing-userkey", "disabled-user"]
)
def test_shared_psk_does_not_bypass_user_authentication(tmp_path, binary, invalid):
    with snell_node(binary, tmp_path) as (inventory, origin):
        protocols = user_protocols(inventory, "alice")
        outbound = snell_outbound(
            inventory.deployment, protocols.snell, protocols.snell_settings
        )
        outbound.pop("domain_resolver")
        if invalid == "psk":
            outbound["psk"] = "synthetic-incorrect-psk"
        elif invalid == "userkey":
            outbound["userkey"] = "synthetic-incorrect-key"
        elif invalid == "disabled-user":
            outbound["userkey"] = "synthetic-snell-disabled"
        else:
            outbound.pop("userkey")
        port = _port()
        config = {
            "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": port}],
            "outbounds": [outbound],
            "route": {"final": "snell"},
        }
        root = tmp_path / "client"
        root.mkdir()
        with _running(binary, root, config):
            try:
                response = _connect(port, "192.0.2.10", 18080)
            except (OSError, AssertionError):
                response = b""
            assert b"proxied" not in response
        assert origin.targets == []


@pytest.mark.parametrize("mobile", [False, True])
@pytest.mark.parametrize("available", [True, False])
def test_five_modes_with_real_snell_and_no_direct_fallback(
    tmp_path, binary, mobile, available
):
    with (
        snell_node(binary, tmp_path, client_routing=True) as (inventory, origin),
        routing_fixtures() as fixtures,
    ):
        deployment = inventory.deployment.model_copy(
            update={"direct_domain_suffixes": ["portal.fixture.invalid"]}
        )
        protocols = user_protocols(inventory, "alice")
        if mobile:
            config = build_mobile_config(
                deployment, protocols, default_protocol="snell"
            )
            config["inbounds"] = [{"type": "mixed"}]
        else:
            config = build_desktop_config(
                deployment,
                protocols,
                os_name="linux",
                default_protocol="snell",
                default_route="china-direct",
            )
        root = tmp_path / "client"
        root.mkdir()
        api, secret = _enable_api(config)
        use_routing_fixtures(config, root, binary, fixtures)
        # Snell authenticates to the real node. The node's final origin proxy
        # records routing decisions. Do not replace the client's Snell outbound.
        snell = next(o for o in config["outbounds"] if o["type"] == "snell")
        if not available:
            snell["server_port"] = _port()
        # use_routing_fixtures replaces only external DNS/rules/direct routes.
        # The Snell node is a separate process and reaches its own origin fixture.
        with _running(binary, root, config):
            for mode in ROUTE_STRATEGIES:
                _api(api, secret, "PATCH", "/configs", {"mode": mode})
                fixtures.reset_counters()
                # Replace only the assertion's proxy observation with the node's
                # actual origin. All network flows still use Snell v6.
                actual = copy.copy(fixtures)
                actual.proxy = origin
                actual.proxy.targets.clear()
                assert_desktop_routing(
                    config["inbounds"][0]["listen_port"],
                    mode,
                    actual,
                    proxy_available=available,
                    expect_updates=False,
                )


def test_real_snell_selector_switch_and_restart_persistence(tmp_path, binary):
    with (
        snell_node(binary, tmp_path, client_routing=True) as (inventory, origin),
        routing_fixtures() as fixtures,
    ):
        config = build_desktop_config(
            inventory.deployment,
            user_protocols(inventory, "alice"),
            os_name="linux",
            default_protocol="trojan",
            default_route="global",
        )
        _proxy_through(
            config,
            dict.fromkeys(
                ("trojan", "hysteria2", "naive"), fixtures.proxy.server_address[1]
            ),
        )
        root = tmp_path / "client"
        root.mkdir()
        api, secret = _enable_api(config)
        use_routing_fixtures(config, root, binary, fixtures)
        port = config["inbounds"][0]["listen_port"]
        with _running(binary, root, config):
            assert b"proxied" in _connect(port, "192.0.2.10", 18080)
            assert fixtures.proxy.targets == ["192.0.2.10"] and not origin.targets
            _api(api, secret, "PUT", "/proxies/proxy", {"name": "snell"})
            assert b"proxied" in _connect(port, "192.0.2.11", 18080)
            assert origin.targets == ["192.0.2.11"]
            _api(api, secret, "PATCH", "/configs", {"mode": "ai-only"})
        with _running(binary, root, config):
            assert _api(api, secret, "GET", "/proxies/proxy")["now"] == "snell"
            assert _api(api, secret, "GET", "/configs")["mode"] == "ai-only"
        config["outbounds"][0]["outbounds"].remove("snell")
        config["outbounds"] = [
            outbound for outbound in config["outbounds"] if outbound["tag"] != "snell"
        ]
        with _running(binary, root, config):
            assert _api(api, secret, "GET", "/proxies/proxy")["now"] == "trojan"


class _UDPEcho(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        sock.sendto(b"snell-udp:" + data, self.client_address)


@pytest.mark.parametrize("username", ["alice", "bob"])
def test_snell_udp_over_tcp_with_each_user(tmp_path, binary, username):
    with (
        snell_node(binary, tmp_path, client_routing=True, udp_origin=True) as (
            inventory,
            _,
        ),
        _server(_UDPEcho, udp=True) as echo,
    ):
        protocols = user_protocols(inventory, username)
        outbound = snell_outbound(
            inventory.deployment, protocols.snell, protocols.snell_settings
        )
        outbound.pop("domain_resolver")
        port = _port()
        udp_port = _port()
        config = {
            "inbounds": [
                {"type": "mixed", "listen": "127.0.0.1", "listen_port": port},
                {
                    "type": "direct",
                    "listen": "127.0.0.1",
                    "listen_port": udp_port,
                    "network": "udp",
                    "override_address": "127.0.0.1",
                    "override_port": echo.server_address[1],
                },
            ],
            "outbounds": [outbound],
            "route": {"final": "snell"},
        }
        root = tmp_path / "client"
        root.mkdir()
        with (
            _running(binary, root, config),
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        ):
            sock.settimeout(5)
            sock.sendto(b"synthetic-user-" + username.encode(), ("127.0.0.1", udp_port))
            assert (
                sock.recvfrom(4096)[0]
                == b"snell-udp:synthetic-user-" + username.encode()
            )
