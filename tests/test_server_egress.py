"""Real protocol routing with local DNS and a recording outbound stand-in.

Documentation addresses represent Internet destinations. The SOCKS stand-in
records them and returns fixture data. It never connects to a requested address.
No test changes host networking, services, inventory, or firewall rules.
"""

import contextlib
import ipaddress
import os
import select
import socket
import socketserver
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sing_box_manager.desktop_config import _protocol_outbounds
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.subscription import user_protocols
from tests.test_m3_profiles import profile_inventory
from tests.test_sing_box_114 import binary as binary
from tests.test_sing_box_114 import deployment as deployment
from tests.test_sing_box_114 import _port, _read_exact, _running, _server


PRIVATE_DESTINATIONS = (
    "127.0.0.1",
    "127.0.0.2",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.0.1",
    "169.254.169.254",
    "100.100.100.200",
    "0.0.0.0",
    "0.1.2.3",
    "224.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "fd00:ec2::254",
    "fe80::1",
    "ff02::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:100.100.100.200",
    "::ffff:0.1.2.3",
)
PUBLIC_DESTINATIONS = ("192.0.2.10", "2001:db8::10", "::ffff:192.0.2.10")
MARKER = b"synthetic-egress-ok"


def test_server_policy_resolves_before_rejecting_private_answers():
    inventory = profile_inventory()
    before = inventory.model_dump()
    config = build_server_config(inventory)
    assert config["dns"] == {"servers": [{"type": "local", "tag": "server-local"}]}
    assert config["route"]["rules"] == [
        {"action": "resolve", "server": "server-local"},
        {"network": "udp", "action": "route-options", "udp_connect": True},
        {"ip_is_private": True, "action": "reject"},
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
    ]
    assert config["route"]["final"] == "direct"
    assert config["outbounds"] == [{"type": "direct", "tag": "direct"}]
    assert inventory.model_dump() == before
    # Each rendering must own its lists. No fixture can mutate another config.
    config["route"]["rules"].clear()
    assert len(build_server_config(inventory)["route"]["rules"]) == 4


def _address(host, port):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        encoded = host.encode("ascii")
        return b"\x03" + bytes([len(encoded)]) + encoded + struct.pack("!H", port)
    return bytes([1 if ip.version == 4 else 4]) + ip.packed + struct.pack("!H", port)


def _destination(read):
    kind = read(1)[0]
    if kind == 3:
        host = read(read(1)[0]).decode("ascii")
    else:
        host = str(ipaddress.ip_address(read(4 if kind == 1 else 16)))
    port = struct.unpack("!H", read(2))[0]
    return host, port


class _RelayTCP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        read = lambda count: _read_exact(self.request, count)
        try:
            header = read(2)
            assert header[0] == 5
            read(header[1])
            self.request.sendall(b"\x05\x00")
            header = read(3)
            destination = _destination(read)
            if header[1] == 1:
                self.server.targets.append(destination)
                self.request.sendall(b"\x05\x00\x00" + _address("127.0.0.1", 1))
                self.request.recv(4096)
                self.request.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(MARKER)).encode()
                    + b"\r\n\r\n"
                    + MARKER
                )
            elif header[1] == 3:
                self.request.sendall(
                    b"\x05\x00\x00" + _address("127.0.0.1", self.server.udp_port)
                )
                self.request.recv(1)
        except (OSError, EOFError):
            pass


class _RelayUDP(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        offset = 3

        def read(count):
            nonlocal offset
            result = data[offset : offset + count]
            offset += count
            return result

        assert data[:3] == b"\0\0\0"
        self.server.targets.append(_destination(read))
        connection.sendto(data[:offset] + MARKER, self.client_address)


@contextlib.contextmanager
def relay():
    with _server(_RelayUDP, udp=True) as udp, _server(_RelayTCP) as tcp:
        tcp.udp_port = udp.server_address[1]
        tcp.targets = udp.targets = []
        yield tcp


class _DNS(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        offset, labels = 12, []
        while data[offset]:
            length = data[offset]
            labels.append(data[offset + 1 : offset + 1 + length].decode("ascii"))
            offset += length + 1
        offset += 1
        kind, _ = struct.unpack("!HH", data[offset : offset + 4])
        question = data[12 : offset + 4]
        name = ".".join(labels).lower()
        self.server.lookups.append((name, kind))
        canonical = self.server.aliases.get(name, name)
        values = self.server.answers.get(canonical, ())
        sequence = getattr(self.server, "sequences", {}).get((canonical, kind))
        if sequence:
            values = sequence.pop(0)
        answers = []
        owner = b"\xc0\x0c"
        if canonical != name:
            owner = (
                b"".join(
                    bytes([len(label)]) + label.encode()
                    for label in canonical.split(".")
                )
                + b"\0"
            )
            answers.append(
                b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 0, len(owner)) + owner
            )
        for value in values:
            ip = ipaddress.ip_address(value)
            if (kind == 1 and ip.version == 4) or (kind == 28 and ip.version == 6):
                answers.append(
                    owner + struct.pack("!HHIH", kind, 1, 0, len(ip.packed)) + ip.packed
                )
        # No external resolver, even for unknown or deliberately failed names.
        flags = 0x8182 if name == "failed.example.test" else 0x8180
        response = data[:2] + struct.pack("!HHHHH", flags, 1, len(answers), 0, 0)
        connection.sendto(response + question + b"".join(answers), self.client_address)


@pytest.fixture
def routed_node(tmp_path, deployment, binary):
    value = os.environ.get("SBM_TEST_SING_BOX_SERVER")
    if not value:
        if os.environ.get("SBM_REQUIRE_SING_BOX"):
            pytest.fail("The pinned custom server binary is required")
        pytest.skip("Set SBM_TEST_SING_BOX_SERVER to the pinned custom server build")
    inventory = profile_inventory()
    inventory.deployment_data = deployment.model_copy(
        update={
            "trojan_port": _port(),
            "hysteria2_port": _port(),
            "naive_port": _port(),
        }
    )
    server = build_server_config(inventory)
    for inbound in server["inbounds"]:
        inbound["listen"] = "127.0.0.1"
    with relay() as outbound, _server(_DNS, udp=True) as dns:
        dns.lookups = []
        dns.aliases = {
            "alias.example.test": "private.example.test",
            "public-alias.example.test": "public.example.test",
        }
        dns.answers = {
            "public.example.test": list(PUBLIC_DESTINATIONS),
            "private.example.test": ["127.0.0.1"],
            "private6.example.test": ["::1"],
            "mixed.example.test": ["192.0.2.10", "127.0.0.1"],
            "mixed-family.example.test": ["192.0.2.10", "::1"],
            "metadata.example.test": ["169.254.169.254"],
            "shared-metadata.example.test": ["100.100.100.200"],
            "mixed-shared.example.test": ["192.0.2.10", "100.100.100.200"],
            "mapped-shared.example.test": ["::ffff:100.100.100.200"],
            "mapped-zero.example.test": ["::ffff:0.1.2.3"],
            "mapped-private.example.test": ["::ffff:127.0.0.1", "::ffff:10.0.0.1"],
            "mapped-reserved.example.test": ["::ffff:255.255.255.255"],
        }
        server["dns"] = {
            "servers": [
                {
                    "type": "udp",
                    "tag": "server-local",
                    "server": "127.0.0.1",
                    "server_port": dns.server_address[1],
                }
            ]
        }
        server["outbounds"] = [
            {
                "type": "socks",
                "tag": "direct",
                "server": "127.0.0.1",
                "server_port": outbound.server_address[1],
            }
        ]
        root = tmp_path / "node"
        root.mkdir()
        with _running(Path(value), root, server):
            yield inventory, outbound, dns


@contextlib.contextmanager
def client(binary, deployment, inventory, protocol, root, *, transport_port=None):
    outbound = next(
        value
        for value in _protocol_outbounds(
            inventory.deployment, user_protocols(inventory, "alice")
        )
        if value["type"] == protocol.removesuffix("-mux")
    )
    outbound["tls"]["certificate"] = (
        Path(deployment.tls.certificate_path).read_text().splitlines()
    )
    outbound.pop("domain_resolver", None)
    if protocol == "trojan-mux":
        outbound["multiplex"] = {"enabled": True, "max_connections": 1}
    if transport_port is not None:
        outbound["server_port"] = transport_port
    outbound["tag"] = "proxy"
    port = _port()
    config = {
        "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": port}],
        "outbounds": [outbound],
        "route": {"final": "proxy"},
    }
    root.mkdir()
    with _running(binary, root, config):
        yield port


def request(port, host, destination_port=18080):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        authority = (
            f"[{host}]:{destination_port}"
            if ":" in host
            else f"{host}:{destination_port}"
        )
        connection.sendall(
            f"CONNECT {authority} HTTP/1.1\r\nHost: fixture.example.test\r\n\r\n".encode()
        )
        assert b"200" in connection.recv(1024).split(b"\r\n")[0]
        try:
            connection.sendall(
                b"GET / HTTP/1.1\r\nHost: fixture.example.test\r\nConnection: close\r\n\r\n"
            )
            return connection.recv(4096)
        except (ConnectionResetError, BrokenPipeError):
            return b""


@pytest.mark.parametrize("protocol", ["trojan", "trojan-mux", "hysteria2", "naive"])
def test_private_literals_and_dns_answers_never_reach_outbound(
    tmp_path,
    binary,
    deployment,
    routed_node,
    protocol,
):
    inventory, outbound, dns = routed_node
    with client(binary, deployment, inventory, protocol, tmp_path / "client") as port:
        for host in (
            *PUBLIC_DESTINATIONS,
            "public.example.test",
            "public-alias.example.test",
        ):
            assert MARKER in request(port, host)
        before = len(outbound.targets)
        for host in (
            *PRIVATE_DESTINATIONS,
            "private.example.test",
            "private6.example.test",
            "mixed.example.test",
            "mixed-family.example.test",
            "metadata.example.test",
            "failed.example.test",
            "empty.example.test",
            "alias.example.test",
            "shared-metadata.example.test",
            "mixed-shared.example.test",
            "mapped-shared.example.test",
            "mapped-zero.example.test",
            "mapped-private.example.test",
            "mapped-reserved.example.test",
        ):
            assert MARKER not in request(port, host), (
                f"Private destination was not rejected: {host}"
            )
            assert len(outbound.targets) == before
        assert MARKER in request(port, "public.example.test")
        assert {name for name, _ in dns.lookups} >= {
            "public.example.test",
            "private.example.test",
            "private6.example.test",
            "mixed.example.test",
            "mixed-family.example.test",
            "metadata.example.test",
            "failed.example.test",
            "empty.example.test",
            "alias.example.test",
            "shared-metadata.example.test",
            "mixed-shared.example.test",
            "mapped-shared.example.test",
            "mapped-zero.example.test",
            "mapped-private.example.test",
            "mapped-reserved.example.test",
        }
        # The SOCKS stand-in receives checked numeric addresses, not a hostname.
        dns.answers["rebind.example.test"] = ["192.0.2.10"]
        assert MARKER in request(port, "rebind.example.test")
        assert outbound.targets[-1] == ("192.0.2.10", 18080)
        assert dns.lookups.count(("rebind.example.test", 1)) == 1
        assert dns.lookups.count(("rebind.example.test", 28)) == 1
        before = len(outbound.targets)
        dns.answers["rebind.example.test"] = ["127.0.0.1"]
        assert MARKER not in request(port, "rebind.example.test")
        assert len(outbound.targets) == before


@pytest.mark.parametrize("protocol", ["trojan", "trojan-mux", "hysteria2"])
def test_udp_destination_changes_keep_private_addresses_blocked(
    tmp_path,
    binary,
    deployment,
    routed_node,
    protocol,
):
    inventory, outbound, _ = routed_node
    with client(binary, deployment, inventory, protocol, tmp_path / "client") as port:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=3) as control,
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
        ):
            control.sendall(b"\x05\x01\x00")
            assert _read_exact(control, 2) == b"\x05\x00"
            control.sendall(b"\x05\x03\x00" + _address("127.0.0.1", 0))
            assert _read_exact(control, 3) == b"\x05\x00\x00"
            target = _destination(lambda count: _read_exact(control, count))
            assert ipaddress.ip_address(target[0]).is_loopback
            udp.settimeout(0.25)
            for host in (
                "192.0.2.10",
                "127.0.0.1",
                "private.example.test",
                "::1",
                "169.254.169.254",
                "192.0.2.10",
            ):
                before = len(outbound.targets)
                udp.sendto(b"\0\0\0" + _address(host, 18080) + MARKER, target)
                if host == "192.0.2.10":
                    assert udp.recv(4096).endswith(MARKER)
                    assert len(outbound.targets) > before
                else:
                    # A reused association stays at its first checked peer.
                    # A reply can come from that peer, never from the new target.
                    try:
                        udp.recv(4096)
                    except socket.timeout:
                        pass
                    assert all(
                        target == ("192.0.2.10", 18080) for target in outbound.targets
                    )


@contextlib.contextmanager
def udp_client(port):
    with (
        socket.create_connection(("127.0.0.1", port), timeout=3) as control,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
    ):
        control.sendall(b"\x05\x01\x00")
        assert _read_exact(control, 2) == b"\x05\x00"
        control.sendall(b"\x05\x03\x00" + _address("127.0.0.1", 0))
        assert _read_exact(control, 3) == b"\x05\x00\x00"
        target = _destination(lambda count: _read_exact(control, count))
        assert ipaddress.ip_address(target[0]).is_loopback
        udp.settimeout(0.25)
        yield udp, target


@pytest.mark.parametrize("protocol", ["trojan", "trojan-mux", "hysteria2"])
def test_udp_first_private_destination_is_rejected(
    tmp_path, binary, deployment, routed_node, protocol
):
    inventory, outbound, _ = routed_node
    with client(binary, deployment, inventory, protocol, tmp_path / "client") as port:
        for host in (
            *PRIVATE_DESTINATIONS,
            "private.example.test",
            "mixed-family.example.test",
        ):
            with udp_client(port) as (udp, target):
                udp.sendto(b"\0\0\0" + _address(host, 18080) + MARKER, target)
                with pytest.raises(socket.timeout):
                    udp.recv(4096)
                assert not outbound.targets


class _EchoUDP(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        self.server.requests += 1
        connection.sendto(data, self.client_address)


@pytest.mark.parametrize("protocol", ["trojan", "trojan-mux", "hysteria2"])
@pytest.mark.parametrize("connected", [True, False])
def test_real_direct_udp_cannot_retarget_checked_peer(
    tmp_path, binary, deployment, protocol, connected
):
    value = os.environ.get("SBM_TEST_SING_BOX_SERVER")
    if not value:
        if os.environ.get("SBM_REQUIRE_SING_BOX"):
            pytest.fail("The pinned custom server binary is required")
        pytest.skip("Set SBM_TEST_SING_BOX_SERVER to the pinned custom server build")
    inventory = profile_inventory()
    inventory.deployment_data = deployment.model_copy(
        update={
            "trojan_port": _port(),
            "hysteria2_port": _port(),
            "naive_port": _port(),
        }
    )
    server = build_server_config(inventory)
    for inbound in server["inbounds"]:
        inbound["listen"] = "127.0.0.1"
    with _server(_EchoUDP, udp=True) as allowed, _server(_EchoUDP, udp=True) as blocked:
        rules = server["route"]["rules"]
        if not connected:
            # Negative control: demonstrate why route checks alone do not suffice.
            rules[:] = [rule for rule in rules if not rule.get("udp_connect")]
        # Fixture-only permission for exactly one socket. This isolates direct
        # UDP peer pinning without host address changes or Internet traffic.
        index = next(
            index for index, rule in enumerate(rules) if rule["action"] == "reject"
        )
        rules.insert(
            index,
            {
                "network": "udp",
                "ip_cidr": ["127.0.0.1/32"],
                "port": allowed.server_address[1],
                "action": "route",
                "outbound": "direct",
            },
        )
        root = tmp_path / "node"
        root.mkdir()
        with (
            _running(Path(value), root, server),
            client(
                binary, deployment, inventory, protocol, tmp_path / "client"
            ) as port,
        ):
            # A new association to the blocked peer must fail in both controls.
            with udp_client(port) as (udp, target):
                udp.sendto(
                    b"\0\0\0" + _address(*blocked.server_address) + MARKER, target
                )
                with pytest.raises(socket.timeout):
                    udp.recv(4096)
                assert blocked.requests == 0
            with udp_client(port) as (udp, target):
                udp.sendto(
                    b"\0\0\0" + _address(*allowed.server_address) + MARKER, target
                )
                assert udp.recv(4096).endswith(MARKER)
                assert allowed.requests == 1
                udp.sendto(
                    b"\0\0\0" + _address(*blocked.server_address) + MARKER, target
                )
                assert udp.recv(4096).endswith(MARKER)
                assert blocked.requests == (0 if connected else 1)
                assert allowed.requests == (2 if connected else 1)


class _Tunnel(socketserver.BaseRequestHandler):
    """Count outer TCP connections without terminating their TLS or multiplexing."""

    def handle(self):
        self.server.connections += 1
        try:
            with socket.create_connection(
                ("127.0.0.1", self.server.target_port), timeout=3
            ) as upstream:
                peers = (self.request, upstream)
                while True:
                    readable, _, _ = select.select(peers, [], [], 3)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = (
                            upstream if source is self.request else self.request
                        )
                        destination.sendall(data)
        except OSError:
            pass


@pytest.mark.parametrize("multiplex", [True, False])
def test_inner_streams_share_one_transport_only_with_multiplex(
    tmp_path, binary, deployment, routed_node, multiplex
):
    inventory, outbound, _ = routed_node
    with _server(_Tunnel) as tunnel:
        tunnel.target_port = inventory.deployment.trojan_port
        protocol = "trojan-mux" if multiplex else "trojan"
        with client(
            binary,
            deployment,
            inventory,
            protocol,
            tmp_path / "client",
            transport_port=tunnel.server_address[1],
        ) as port:
            hosts = [
                "public.example.test",
                "127.0.0.1",
                "mixed.example.test",
                "192.0.2.10",
            ]
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(
                    pool.map(lambda host: MARKER in request(port, host), hosts)
                )
            assert results == [True, False, False, True]
            assert len(outbound.targets) == 2
            # UDP inner streams use the same transport, including a denied stream.
            for host, accepted in [
                ("public.example.test", True),
                ("private.example.test", False),
                ("192.0.2.10", True),
            ]:
                before = len(outbound.targets)
                with udp_client(port) as (udp, target):
                    udp.sendto(b"\0\0\0" + _address(host, 18080) + MARKER, target)
                    if accepted:
                        assert udp.recv(4096).endswith(MARKER)
                        assert len(outbound.targets) == before + 1
                    else:
                        with pytest.raises(socket.timeout):
                            udp.recv(4096)
                        assert len(outbound.targets) == before
            if multiplex:
                assert tunnel.connections == 1
            else:
                assert tunnel.connections > 1


class _EchoHTTP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        self.request.recv(4096)
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(MARKER)).encode()
            + b"\r\n\r\n"
            + MARKER
        )


@contextlib.contextmanager
def _destination_server(host, port, network):
    base = (
        socketserver.ThreadingTCPServer
        if network == "tcp"
        else socketserver.ThreadingUDPServer
    )

    class Server(base):
        arrivals = 0
        requests = 0
        daemon_threads = True

        def get_request(self):
            result = super().get_request()
            self.arrivals += 1
            return result

    handler = _EchoHTTP if network == "tcp" else _EchoUDP
    with Server((host, port), handler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize(
    "protocol,network",
    [
        ("trojan", "tcp"),
        ("trojan-mux", "tcp"),
        ("hysteria2", "tcp"),
        ("naive", "tcp"),
        ("trojan", "udp"),
        ("trojan-mux", "udp"),
        ("hysteria2", "udp"),
    ],
)
@pytest.mark.parametrize("resolve_first", [True, False])
def test_real_direct_hostname_uses_checked_answers_and_fallbacks(
    tmp_path, binary, deployment, protocol, network, resolve_first
):
    value = os.environ.get("SBM_TEST_SING_BOX_SERVER")
    if not value:
        if os.environ.get("SBM_REQUIRE_SING_BOX"):
            pytest.fail("The pinned custom server binary is required")
        pytest.skip("Set SBM_TEST_SING_BOX_SERVER to the pinned custom server build")
    inventory = profile_inventory()
    inventory.deployment_data = deployment.model_copy(
        update={
            "trojan_port": _port(),
            "hysteria2_port": _port(),
            "naive_port": _port(),
        }
    )
    server = build_server_config(inventory)
    for inbound in server["inbounds"]:
        inbound["listen"] = "127.0.0.1"
    with (
        _destination_server("127.0.0.1", 0, network) as allowed,
        _destination_server("127.0.0.2", allowed.server_address[1], network) as blocked,
        socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM if network == "tcp" else socket.SOCK_DGRAM,
        ) as unavailable,
        _server(_DNS, udp=True) as dns,
    ):
        destination_port = allowed.server_address[1]
        # Reserve the same port on a third loopback address, without listening.
        # TCP must fall back from this refused peer to the second checked answer.
        unavailable.bind(("127.0.0.3", destination_port))
        fallback = (
            ["127.0.0.3", "127.0.0.1"]
            if network == "tcp"
            else ["127.0.0.1", "127.0.0.3"]
        )
        dns.lookups, dns.aliases = [], {}
        dns.answers = {
            name: ["127.0.0.2"]
            for name in ("once.example.test", "fallback.example.test")
        }
        dns.answers["unavailable.example.test"] = ["127.0.0.3"]
        dns.sequences = {
            ("once.example.test", 1): [["127.0.0.1"]],
            ("fallback.example.test", 1): [fallback],
        }
        server["dns"] = {
            "disable_cache": True,
            "servers": [
                {
                    "type": "udp",
                    "tag": "server-local",
                    "server": "127.0.0.1",
                    "server_port": dns.server_address[1],
                }
            ],
        }
        rules = server["route"]["rules"]
        if not resolve_first:
            # Negative control: an unchecked hostname reaches the blocked socket.
            rules[:] = [rule for rule in rules if rule["action"] != "resolve"]
        # Fixture-only permission for two checked addresses at one exact port.
        # The live blocked socket is a third address at that same port.
        index = next(
            index for index, rule in enumerate(rules) if rule["action"] == "reject"
        )
        rules.insert(
            index,
            {
                "network": network,
                "ip_cidr": ["127.0.0.1/32", "127.0.0.3/32"],
                "port": destination_port,
                "action": "route",
                "outbound": "direct",
            },
        )
        assert server["outbounds"] == [{"type": "direct", "tag": "direct"}]
        root = tmp_path / "node"
        root.mkdir()
        with (
            _running(Path(value), root, server),
            client(
                binary, deployment, inventory, protocol, tmp_path / "client"
            ) as port,
        ):

            def send(host):
                if network == "tcp":
                    return request(port, host, destination_port)
                with udp_client(port) as (udp, target):
                    udp.sendto(
                        b"\0\0\0" + _address(host, destination_port) + MARKER, target
                    )
                    try:
                        return udp.recv(4096)
                    except socket.timeout:
                        return b""

            if network == "tcp":
                assert MARKER not in send("unavailable.example.test")
                assert allowed.arrivals == blocked.arrivals == 0
            for name in ("once.example.test", "fallback.example.test"):
                before_allowed, before_blocked = allowed.arrivals, blocked.arrivals
                assert MARKER in send(name)
                assert allowed.arrivals == before_allowed + 1
                assert blocked.arrivals == before_blocked
                # Any lookup after the first A answer returns the blocked peer.
                # A cache cannot hide a second lookup during this connection.
                assert dns.lookups.count((name, 1)) == 1
                assert (MARKER in send(name)) == (not resolve_first)
                assert allowed.arrivals == before_allowed + 1
                assert blocked.arrivals == before_blocked + int(not resolve_first)
                assert dns.lookups.count((name, 1)) == 2
