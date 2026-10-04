"""Binary checks using only generated certificates and local fixtures.

`pixi run fetch-sing-box` provides the client binary for the version the example
inventory pins, and SBM_TEST_SING_BOX overrides it. Set SBM_TEST_SING_BOX_SERVER
to the custom server build. These tests never load runtime inventory or contact
a deployed proxy.
"""

import contextlib
import dataclasses
import http.server
import json
import os
import shutil
import socket
import socketserver
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from sing_box_manager.config_loader import (
    DeploymentConfig,
    Hysteria2Inventory,
    Hysteria2User,
    NaiveInventory,
    NaiveUser,
    TlsConfig,
    TrojanInventory,
    TrojanUser,
)
from sing_box_manager.desktop_config import UserProtocols
from sing_box_manager.release import renderers
from sing_box_manager.settings import TrafficStatsSettings
from tests.fetch_sing_box import TEST_BINARY, pinned_version

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def binary() -> Path:
    value = os.environ.get("SBM_TEST_SING_BOX")
    if value:
        path = Path(value).resolve()
    elif TEST_BINARY.is_file():
        path = TEST_BINARY
    else:
        if os.environ.get("SBM_REQUIRE_SING_BOX"):
            pytest.fail("CI must fetch the pinned sing-box binary")
        pytest.skip("Run `pixi run fetch-sing-box` or set SBM_TEST_SING_BOX")
    result = subprocess.run(
        [str(path), "version"], check=True, capture_output=True, text=True
    )
    version = pinned_version()
    assert result.stdout.startswith(f"sing-box version {version}\n"), (
        f"{path} is not sing-box {version}; run `pixi run fetch-sing-box`"
    )
    return path


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory) -> DeploymentConfig:
    root = tmp_path_factory.mktemp("tls-fixture")
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is not installed")
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=vpn.example.com",
            "-addext",
            "subjectAltName=DNS:vpn.example.com",
            "-keyout",
            str(root / "key.pem"),
            "-out",
            str(root / "cert.pem"),
        ],
        check=True,
        capture_output=True,
    )
    return DeploymentConfig(
        host="vpn.example.com",
        ip="127.0.0.1",
        trojan_port=18443,
        hysteria2_port=14443,
        naive_port=19443,
        tls=TlsConfig(
            enabled=True,
            key_path=str(root / "key.pem"),
            certificate_path=str(root / "cert.pem"),
        ),
    )


def render_client(protocol: str, profile: str, deployment: DeploymentConfig) -> dict:
    from sing_box_manager.release.platforms import MIXED_PROFILE, MOBILE_TUN_PROFILE
    from sing_box_manager.release.profiles import build_client_profile

    return build_client_profile(
        deployment,
        UserProtocols(
            trojan=TrojanUser(username="alice", password="fixture"),
            hysteria2=Hysteria2User(username="alice", password="fixture"),
            naive=NaiveUser(username="alice", password="fixture"),
            hysteria2_settings=Hysteria2Inventory(
                obfs_password="fixture-obfs", users=[]
            ),
        ),
        protocol,
        MOBILE_TUN_PROFILE if profile else MIXED_PROFILE,
    )


@pytest.mark.parametrize("protocol", ["trojan", "hysteria2", "naive"])
@pytest.mark.parametrize("profile", ["china", "gfw", "ai", "global", "-tun"])
# Every platform's desktop configs are rendered separately, and the ai route's
# system resolver differs between them, so all three have to pass validation.
@pytest.mark.parametrize("os_name", ["linux", "darwin", "windows"])
def test_rendered_clients_accept_v114(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
    protocol: str,
    profile: str,
    os_name: str,
) -> None:
    config = render_client(
        protocol, profile if profile.startswith("-") else "", deployment
    )
    if not profile.startswith("-"):
        config = renderers.apply_desktop_route_strategy(
            config, profile, os_name=os_name
        )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    result = subprocess.run(
        [str(binary), "check", "-D", str(tmp_path), "-c", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "deprecated" not in result.stderr.lower()


def _read_exact(connection: socket.socket, length: int) -> bytes:
    data = b""
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise EOFError("Fixture connection closed")
        data += chunk
    return data


class _SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            header = _read_exact(self.request, 2)
            _read_exact(self.request, header[1])
            self.request.sendall(b"\x05\x00")
            request = _read_exact(self.request, 4)
            if request[3] == 3:
                host = _read_exact(
                    self.request, _read_exact(self.request, 1)[0]
                ).decode()
            elif request[3] == 1:
                host = socket.inet_ntoa(_read_exact(self.request, 4))
            else:
                host = socket.inet_ntop(socket.AF_INET6, _read_exact(self.request, 16))
            _read_exact(self.request, 2)
            self.server.targets.append(host)
            self.request.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x01")
            self.request.recv(4096)
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nproxied")
        except (OSError, EOFError):
            pass


class _DirectHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.connections += 1
        self.request.settimeout(3)
        with contextlib.suppress(OSError):
            self.request.recv(4096)
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\ndirect")


class _UnavailableRules(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests += 1
        self.send_error(503)

    def log_message(self, *args):
        pass


class _DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        end = 12
        while data[end]:
            end += data[end] + 1
        end += 5
        self.server.queries += 1
        # Echo the question and return one short-lived IPv4 answer.
        response = data[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
        response += data[12:end] + b"\xc0\x0c"
        response += struct.pack("!HHIH", 1, 1, self.server.ttl, 4)
        response += socket.inet_aton(self.server.answer)
        connection.sendto(response, self.client_address)


@contextlib.contextmanager
def _server(handler, *, udp=False):
    cls = socketserver.ThreadingUDPServer if udp else socketserver.ThreadingTCPServer
    with cls(("127.0.0.1", 0), handler) as server:
        server.daemon_threads = True
        server.targets = []
        server.connections = server.queries = server.requests = 0
        server.answer = "127.0.0.1"
        server.ttl = 3
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=2)


def _port() -> int:
    """Return a loopback port that is free for both TCP and UDP.

    Callers use it for either, and a port the kernel picks for TCP can be held
    by one of the UDP DNS fixtures.
    """
    while True:
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp,
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
        ):
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            try:
                udp.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port


@contextlib.contextmanager
def _running(binary: Path, root: Path, config: dict):
    path = root / "config.json"
    path.write_text(json.dumps(config))
    with (root / "process.log").open("a+") as log:
        process = subprocess.Popen(
            [str(binary), "run", "-D", str(root), "-c", str(path)],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    with socket.create_connection(
                        ("127.0.0.1", config["inbounds"][0]["listen_port"]), timeout=0.1
                    ):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                pytest.fail("Fixture mixed inbound did not open")
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _connect(port: int, host: str, destination_port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(
            f"CONNECT {host}:{destination_port} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
        )
        assert b"200" in connection.recv(1024).split(b"\r\n")[0]
        # CONNECT succeeds before sniffing and routing. Send an HTTP request
        # through the tunnel and check the destination's response instead.
        try:
            connection.sendall(
                f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            )
            return connection.recv(4096)
        except (ConnectionResetError, BrokenPipeError):
            return b""


@dataclasses.dataclass
class RoutingFixtures:
    """Local stand-ins for the proxy, direct destinations, DNS and rule updates."""

    proxy: socketserver.BaseServer
    direct: socketserver.BaseServer
    updates: socketserver.BaseServer
    dns: socketserver.BaseServer
    google_dns: socketserver.BaseServer
    dns_port: int

    def reset_counters(self) -> None:
        self.proxy.targets.clear()
        self.direct.connections = 0
        self.dns.queries = self.google_dns.queries = 0


@contextlib.contextmanager
def routing_fixtures():
    with (
        _server(_SocksHandler) as proxy,
        _server(_DirectHandler) as direct,
        _server(_UnavailableRules) as updates,
        _server(_DNSHandler, udp=True) as dns,
        _server(_DNSHandler, udp=True) as google_dns,
    ):
        yield RoutingFixtures(proxy, direct, updates, dns, google_dns, _port())


def use_routing_fixtures(
    config: dict, tmp_path: Path, binary: Path, fixtures: RoutingFixtures
) -> None:
    """Point a desktop config's inbound, DNS, rule sets and updates at fixtures.

    The first inbound moves to a free port, and a UDP inlet on
    `fixtures.dns_port` hands queries to the config's DNS rules.
    """
    config["ntp"]["enabled"] = False
    config["inbounds"][0].update(listen="127.0.0.1", listen_port=_port())
    config["http_clients"] = [{"tag": "rule-downloads", "detour": "direct"}]
    config["inbounds"].append(
        {
            "type": "direct",
            "tag": "dns-fixture",
            "listen": "127.0.0.1",
            "listen_port": fixtures.dns_port,
            "network": "udp",
        }
    )
    config["route"]["rules"].insert(
        0, {"inbound": "dns-fixture", "action": "hijack-dns"}
    )
    for server in config["dns"]["servers"]:
        tag = server["tag"]
        upstream = fixtures.google_dns if tag == "google" else fixtures.dns
        server.clear()
        server.update(
            type="udp",
            tag=tag,
            server="127.0.0.1",
            server_port=upstream.server_address[1],
        )
    for rule_set in config["route"]["rule_set"]:
        if rule_set["type"] != "remote":
            continue
        tag = rule_set["tag"]
        # filtered.fixture.invalid is deliberately in both the AdGuard set
        # and the AI geosite set: real AI vendors depend on feature-flag and
        # telemetry hosts the filter blocks, and the ai route has to resolve
        # that overlap in the vendor's favor.
        domains = (
            ["blocked.fixture.invalid", "filtered.fixture.invalid"]
            if tag == "AdGuard-DNS-Filter"
            else ["proxy.fixture.invalid"]
            if tag == "GFWList"
            else ["geosite.fixture.invalid", "filtered.fixture.invalid"]
            if tag == "AI-Geosite"
            else ["direct.fixture.invalid", "proxy.fixture.invalid"]
        )
        domains += ["portal.fixture.invalid", "vpn.example.com"]
        rules = (
            [{"ip_cidr": ["203.0.113.0/24"]}]
            if tag.startswith("GeoIP")
            else [{"domain": domains}]
        )
        source = tmp_path / "source.json"
        source.write_text(json.dumps({"version": 1, "rules": rules}))
        initial = tmp_path / rule_set["initial_path"]
        initial.parent.mkdir(exist_ok=True)
        subprocess.run(
            [str(binary), "rule-set", "compile", str(source), "-o", str(initial)],
            check=True,
            capture_output=True,
        )
        rule_set["url"] = f"http://127.0.0.1:{fixtures.updates.server_address[1]}/{tag}"


def assert_desktop_routing(
    port: int, strategy: str, fixtures: RoutingFixtures, *, proxy_available: bool
) -> None:
    """Check what one route sends direct, through the proxy, and to which DNS."""
    proxy, direct, dns, google_dns = (
        fixtures.proxy,
        fixtures.direct,
        fixtures.dns,
        fixtures.google_dns,
    )
    dns_port = fixtures.dns_port
    assert _query_dns(dns_port, "dns.portal.fixture.invalid") == "127.0.0.1"
    assert dns.queries == 1 and google_dns.queries == 0
    if proxy_available:
        assert _query_dns(dns_port, "filtered.fixture.invalid") == "127.0.0.1"
        assert google_dns.queries == 1
        if strategy in {"china", "gfw"}:
            assert _query_dns(dns_port, "proxy.fixture.invalid") == "127.0.0.1"
            assert google_dns.queries == 2
    for host in (
        "vpn.example.com",
        "portal.fixture.invalid",
        "a.b.portal.fixture.invalid",
    ):
        assert b"direct" in _connect(port, host, direct.server_address[1])
        assert host not in proxy.targets
    assert dns.queries > 0
    if not proxy_available:
        return
    for host in (
        "unknown.fixture.invalid",
        "evilportal.fixture.invalid",
        "portal.fixture.invalid.evil",
        "sub.vpn.example.com",
    ):
        expected = b"direct" if strategy in {"ai", "gfw"} else b"proxied"
        assert expected in _connect(port, host, direct.server_address[1])
    proxied = "api.openai.com" if strategy == "ai" else "proxy.fixture.invalid"
    assert b"proxied" in _connect(port, proxied, direct.server_address[1])
    assert proxied in proxy.targets
    assert not _connect(port, "blocked.fixture.invalid", direct.server_address[1])
    assert "blocked.fixture.invalid" not in proxy.targets
    for host in ("geosite.fixture.invalid", "filtered.fixture.invalid"):
        assert b"proxied" in _connect(port, host, direct.server_address[1])
        assert host in proxy.targets
    if strategy != "global":
        assert b"direct" in _connect(
            port, "direct.fixture.invalid", direct.server_address[1]
        )
        assert "direct.fixture.invalid" not in proxy.targets
        assert direct.connections > 0
    deadline = time.monotonic() + 2
    while not fixtures.updates.requests and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fixtures.updates.requests > 0


@pytest.mark.parametrize("proxy_available", [True, False])
@pytest.mark.parametrize("strategy", ["china", "gfw", "ai", "global"])
def test_cold_start_uses_bundled_rules_when_updates_fail(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
    strategy: str,
    proxy_available: bool,
) -> None:
    deployment = deployment.model_copy(
        update={"direct_domain_suffixes": ["portal.fixture.invalid"]}
    )
    config = renderers.apply_desktop_route_strategy(
        render_client("trojan", "", deployment), strategy, os_name="linux"
    )
    with routing_fixtures() as fixtures:
        config["outbounds"][0] = {
            "type": "socks",
            "tag": "proxy",
            "server": "127.0.0.1",
            "server_port": fixtures.proxy.server_address[1]
            if proxy_available
            else _port(),
        }
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        assert not list(tmp_path.glob("*.db"))
        with _running(binary, tmp_path, config):
            assert_desktop_routing(
                config["inbounds"][0]["listen_port"],
                strategy,
                fixtures,
                proxy_available=proxy_available,
            )


def _query_dns(port: int, host: str = "cache.fixture.invalid") -> str:
    question = (
        b"".join(bytes([len(label)]) + label.encode() for label in host.split("."))
        + b"\x00"
        + struct.pack("!HH", 1, 1)
    )
    request = struct.pack("!HHHHHH", 123, 0x0100, 1, 0, 0, 0) + question
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(3)
        connection.sendto(request, ("127.0.0.1", port))
        answer, _ = connection.recvfrom(4096)
    return socket.inet_ntoa(answer[-4:])


def test_dns_cache_survives_restart_and_respects_expiry(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
) -> None:
    base = renderers.apply_desktop_route_strategy(
        render_client("trojan", "", deployment), "global", os_name="linux"
    )
    with _server(_DNSHandler, udp=True) as upstream:
        upstream.answer = "192.0.2.10"
        dns_port = _port()
        config = {
            "inbounds": [
                {"type": "mixed", "listen": "127.0.0.1", "listen_port": _port()},
                {
                    "type": "direct",
                    "tag": "dns-fixture",
                    "listen": "127.0.0.1",
                    "listen_port": dns_port,
                    "network": "udp",
                },
            ],
            "dns": {
                "servers": [
                    {
                        "type": "udp",
                        "tag": "fixture",
                        "server": "127.0.0.1",
                        "server_port": upstream.server_address[1],
                    }
                ],
                "optimistic": base["dns"]["optimistic"],
            },
            "route": {"rules": [{"inbound": "dns-fixture", "action": "hijack-dns"}]},
            "experimental": base["experimental"],
        }
        with _running(binary, tmp_path, config):
            assert _query_dns(dns_port) == "192.0.2.10"
            # v1.14 saves DNS answers asynchronously. Wait for the fixture
            # answer to reach the database before exercising restart loading.
            cache_path = tmp_path / base["experimental"]["cache_file"]["path"]
            deadline = time.monotonic() + 2
            while b"\x05cache\x07fixture\x07invalid" not in cache_path.read_bytes():
                assert time.monotonic() < deadline, "DNS answer was not persisted"
                time.sleep(0.02)
        first_queries = upstream.queries
        assert first_queries == 1
        assert (tmp_path / base["experimental"]["cache_file"]["path"]).is_file()
        upstream.answer = "192.0.2.11"
        with _running(binary, tmp_path, config):
            assert _query_dns(dns_port) == "192.0.2.10"
            assert upstream.queries == first_queries
            time.sleep(3.1)
            assert _query_dns(dns_port) == "192.0.2.11"
            assert upstream.queries == first_queries + 1


@pytest.mark.parametrize("stats_enabled", [False, True])
@pytest.mark.parametrize("naive_roster", ["enabled", "empty", "disabled"])
def test_single_server_config_accepts_v114(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
    stats_enabled: bool,
    naive_roster: str,
) -> None:
    from sing_box_manager.release.profiles import build_server_config
    from sing_box_manager.settings import RuntimeInventory, WebPortalInventory

    server_binary = os.environ.get("SBM_TEST_SING_BOX_SERVER")
    if stats_enabled and not server_binary:
        if os.environ.get("SBM_REQUIRE_SING_BOX"):
            pytest.fail("CI must build the pinned stats server binary")
        pytest.skip(
            "Run `pixi run fetch-sing-box-server` and set SBM_TEST_SING_BOX_SERVER"
        )
    selected_binary = server_binary if stats_enabled else str(binary)
    inventory = RuntimeInventory(
        deployment=deployment,
        web_portal=WebPortalInventory(users=[]),
        trojan=TrojanInventory(
            users=[TrojanUser(username="alice", password="fixture")]
        ),
        hysteria2=Hysteria2Inventory(
            obfs_password="fixture-obfs",
            users=[Hysteria2User(username="alice", password="fixture")]
            if naive_roster == "enabled"
            else [],
        ),
        naive=NaiveInventory(
            users=[]
            if naive_roster == "empty"
            else [
                NaiveUser(
                    username="alice",
                    password="fixture",
                    enabled=naive_roster == "enabled",
                )
            ]
        ),
    )
    config = build_server_config(inventory, TrafficStatsSettings(enabled=stats_enabled))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    result = subprocess.run(
        [selected_binary, "check", "-D", str(tmp_path), "-c", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "deprecated" not in result.stderr.lower()
    expected_protocols = ["trojan", "hysteria2"]
    if naive_roster == "enabled":
        expected_protocols.append("naive")
    assert [inbound["type"] for inbound in config["inbounds"]] == expected_protocols
    if stats_enabled:
        assert len(config["services"]) == 1
        assert config["experimental"]["v2ray_api"]["stats"]["users"] == (
            ["trojan:alice", "hysteria2:alice", "alice"]
            if naive_roster == "enabled"
            else ["trojan:alice"]
        )


@pytest.mark.parametrize("protocol", ["trojan", "hysteria2", "naive"])
@pytest.mark.parametrize("profile", ["-tun"])
def test_tun_exception_dns_and_access_without_proxy(
    tmp_path: Path,
    binary: Path,
    deployment: DeploymentConfig,
    protocol: str,
    profile: str,
) -> None:
    deployment = deployment.model_copy(
        update={"direct_domain_suffixes": ["portal.fixture.invalid"]}
    )
    config = render_client(protocol, profile, deployment)
    # Exercise the TUN profile's routing and DNS through a loopback mixed inlet;
    # actual interface capture remains a disposable-VM integration check.
    dns_port = _port()
    config["inbounds"] = [
        {"type": "mixed", "listen": "127.0.0.1", "listen_port": _port()},
        {
            "type": "direct",
            "tag": "dns-fixture",
            "listen": "127.0.0.1",
            "listen_port": dns_port,
            "network": "udp",
        },
    ]
    config["route"]["rules"].insert(
        0, {"inbound": "dns-fixture", "action": "hijack-dns"}
    )
    config["route"].pop("auto_detect_interface", None)
    config.pop("experimental", None)
    config["ntp"]["enabled"] = False
    config["outbounds"][0] = {
        "type": "socks",
        "tag": "proxy",
        "server": "127.0.0.1",
        "server_port": _port(),
    }
    for rule in config["route"].get("rule_set", []):
        tag = rule["tag"]
        rule.clear()
        rule.update(
            type="inline", tag=tag, rules=[{"domain": ["unused.fixture.invalid"]}]
        )
    with _server(_DNSHandler, udp=True) as dns, _server(_DirectHandler) as direct:
        for server in config["dns"]["servers"]:
            if server["tag"] != "aliyun":
                continue
            server.update(server="127.0.0.1", server_port=dns.server_address[1])
        with _running(binary, tmp_path, config):
            for host in (
                "vpn.example.com",
                "portal.fixture.invalid",
                "a.b.portal.fixture.invalid",
            ):
                assert _query_dns(dns_port, host) == "127.0.0.1"
                assert b"direct" in _connect(
                    config["inbounds"][0]["listen_port"], host, direct.server_address[1]
                )
            if profile == "-tun":
                for host in (
                    "evilportal.fixture.invalid",
                    "portal.fixture.invalid.evil",
                    "sub.vpn.example.com",
                ):
                    assert _query_dns(dns_port, host).startswith("198.18.")
