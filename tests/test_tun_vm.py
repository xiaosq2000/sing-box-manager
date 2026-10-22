"""Local checks for the TUN acceptance fixtures. No VM or host changes."""

import copy
import json
import os
import runpy
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sing_box_manager.desktop_config import UserProtocols, build_linux_tun_config
from sing_box_manager.release.platforms import ROUTE_ALIASES, ROUTE_STRATEGIES
from sing_box_manager.settings import DeploymentConfig, TlsConfig, TrojanUser
from tests.test_desktop_config import _api, _enable_api, _proxy_through
from tests.test_sing_box_114 import (
    _connect,
    _running,
    _server,
    routing_fixtures,
    use_routing_fixtures,
)
from tests.test_sing_box_114 import binary as binary  # noqa: PLC0414
from tests.test_sing_box_114 import deployment as deployment  # noqa: PLC0414

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "scripts/dev/tun-vm/fixtures.py"
PREPARE = ROOT / "scripts/dev/tun-vm/prepare.py"


def query(name: str, qtype: int = 1) -> bytes:
    labels = b"".join(bytes([len(label)]) + label.encode() for label in name.split("."))
    return (
        struct.pack(">HHHHHH", 123, 0x100, 1, 0, 0, 0)
        + labels
        + b"\0"
        + struct.pack(">HH", qtype, 1)
    )


def test_guest_entry_refuses_an_operator_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostname = tmp_path / "hostname"
    hostname.write_text("#!/bin/sh\nprintf 'operator-test\\n'\n")
    hostname.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/dev/tun-vm/test.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "outside the disposable TUN guest" in result.stderr


def test_dns_distinguishes_baseline_direct_and_proxied_answers() -> None:
    answer = runpy.run_path(str(FIXTURES))["dns_answer"]
    request = query("dns-path.fixture.test")
    for proxied, baseline, expected in [
        (False, False, "198.18.0.10"),
        (True, False, "198.18.0.20"),
        (False, True, "198.18.0.99"),
    ]:
        response = answer(request, proxied=proxied, baseline=baseline)
        assert socket.inet_ntoa(response[-4:]) == expected
        assert response[:2] == request[:2]
    for proxied, expected in [
        (False, "2001:db8:100::10"),
        (True, "2001:db8:100::20"),
    ]:
        response = answer(query("dns-path.fixture.test", 28), proxied=proxied)
        assert struct.unpack(">H", response[6:8])[0] == 1
        assert socket.inet_ntop(socket.AF_INET6, response[-16:]) == expected
    response = answer(query("unlisted.invalid"), proxied=True)
    assert struct.unpack(">H", response[6:8])[0] == 0


def _mixed_tun_fixture(deployment: DeploymentConfig) -> dict:
    """Exercise TUN policy without a privileged inbound or host route changes."""
    config = build_linux_tun_config(
        deployment,
        UserProtocols(trojan=TrojanUser(username="fixture", password="fixture")),
        default_protocol="trojan",
        default_route="global",
    )
    config["inbounds"] = [{"type": "mixed", "tag": "tun-in"}]
    config["route"].pop("auto_detect_interface")
    return config


@pytest.mark.parametrize("ipv4_only", [True, False])
def test_tun_dns_filters_dual_stack_answers_in_every_mode(
    tmp_path: Path, binary: Path, deployment: DeploymentConfig, ipv4_only: bool
) -> None:
    answer = runpy.run_path(str(FIXTURES))["dns_answer"]

    class DualStackDNS(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            data, stream = self.request
            self.server.queries += 1
            stream.sendto(answer(data, proxied=False), self.client_address)

    config = _mixed_tun_fixture(deployment)
    if not ipv4_only:
        # A negative control proves the upstream really supplies AAAA.
        config["dns"]["strategy"] = "prefer_ipv4"
    with routing_fixtures() as fixtures, _server(DualStackDNS, udp=True) as upstream:
        fixtures.dns = fixtures.google_dns = upstream
        _proxy_through(config, {"trojan": fixtures.proxy.server_address[1]})
        port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        with _running(binary, tmp_path, config):
            for mode in (*ROUTE_STRATEGIES, *ROUTE_ALIASES):
                _api(port, secret, "PATCH", "/configs", {"mode": mode})
                for qtype in (1, 28):
                    request = query("www.baidu.com", qtype)
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
                        stream.settimeout(3)
                        stream.sendto(request, ("127.0.0.1", fixtures.dns_port))
                        response, _ = stream.recvfrom(4096)
                    _, flags, questions, records, _, _ = struct.unpack(
                        ">HHHHHH", response[:12]
                    )
                    assert flags & 0xF == 0
                    assert questions == 1
                    assert response[12 : len(request)] == request[12:]
                    assert records == (0 if ipv4_only and qtype == 28 else 1)
                    if qtype == 1:
                        assert socket.inet_ntoa(response[-4:]) == "198.18.0.10"
                    elif not ipv4_only:
                        assert (
                            socket.inet_ntop(socket.AF_INET6, response[-16:])
                            == "2001:db8:100::10"
                        )


def test_tun_rejects_public_ipv6_before_mode_routing(
    tmp_path: Path, binary: Path, deployment: DeploymentConfig
) -> None:
    config = _mixed_tun_fixture(deployment)
    with routing_fixtures() as fixtures:
        _proxy_through(config, {"trojan": fixtures.proxy.server_address[1]})
        port, secret = _enable_api(config)
        use_routing_fixtures(config, tmp_path, binary, fixtures)
        control = copy.deepcopy(config)
        control["route"]["rules"] = [
            rule for rule in control["route"]["rules"] if rule.get("ip_version") != 6
        ]
        control_dir = tmp_path / "control"
        control_dir.mkdir()
        # Rule snapshot paths are relative to the fixture root.
        control["route"]["rule_set"] = config["route"]["rule_set"] = [
            {
                "type": "local",
                "tag": rule["tag"],
                "format": "binary",
                "path": str(tmp_path / rule["initial_path"]),
            }
            for rule in config["route"]["rule_set"]
        ]
        with _running(binary, control_dir, control):
            assert b"proxied" in _connect(
                config["inbounds"][0]["listen_port"], "[2001:db8:100::10]", 443
            )
        assert fixtures.proxy.targets == ["2001:db8:100::10"]
        fixtures.reset_counters()
        with _running(binary, tmp_path, config):
            for mode in (*ROUTE_STRATEGIES, *ROUTE_ALIASES):
                _api(port, secret, "PATCH", "/configs", {"mode": mode})
                assert not _connect(
                    config["inbounds"][0]["listen_port"], "[2001:db8:100::10]", 443
                )
            assert not fixtures.proxy.targets
            # Private-address access remains ahead of the IPv6 rejection.
            assert b"direct" in _connect(
                config["inbounds"][0]["listen_port"],
                "127.0.0.1",
                fixtures.direct.server_address[1],
            )


def test_preparation_preserves_product_routing(
    tmp_path: Path, binary: Path, deployment: DeploymentConfig
) -> None:
    assert deployment.tls.certificate_path is not None
    (tmp_path / "ca.crt").write_text(Path(deployment.tls.certificate_path).read_text())
    (tmp_path / "ports.json").write_text(json.dumps({"socks": 12345, "portal": 12346}))
    config = runpy.run_path(str(PREPARE))["prepare"](tmp_path, binary)
    original = build_linux_tun_config(
        DeploymentConfig(
            host="portal.fixture.test",
            ip="203.0.113.42",
            trojan_port=8443,
            hysteria2_port=4443,
            naive_port=9443,
            tls=TlsConfig(enabled=False),
        ),
        UserProtocols(
            trojan=TrojanUser(username="fixture", password="fixture-password")  # noqa: S106
        ),
        default_protocol="trojan",
        default_route="china-direct",
    )
    assert config["route"]["rules"] == original["route"]["rules"]
    assert config["dns"]["rules"] == original["dns"]["rules"]
    assert config["inbounds"] == original["inbounds"]
    assert config["inbounds"][0]["dns_mode"] == "hijack"
    assert len(list((tmp_path / "rules").glob("*.srs"))) == len(
        original["route"]["rule_set"]
    )
    subprocess.run(
        [str(binary), "check", "-D", str(tmp_path), "-c", str(tmp_path / "tun.json")],
        check=True,
        capture_output=True,
    )
    bad = json.loads((tmp_path / "failed-sub.json").read_text())["config_tun"]
    assert bad["experimental"]["clash_api"]["default_mode"] == "global"
    direct = json.loads((tmp_path / "direct-failed-sub.json").read_text())["config_tun"]
    assert direct["experimental"]["clash_api"]["default_mode"] == "direct"
    good = json.loads((tmp_path / "good-sub.json").read_text())["config_tun"]
    assert good == config


def socks_request(stream: socket.socket, host: str, port: int) -> bytes:
    stream.sendall(b"\x05\x01\x00")
    assert stream.recv(2) == b"\x05\x00"
    encoded = host.encode()
    stream.sendall(
        b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded + struct.pack(">H", port)
    )
    return stream.recv(10)


def test_host_proxy_is_allowlisted_and_serves_a_correlated_tls_response(
    tmp_path: Path, deployment: DeploymentConfig
) -> None:
    assert deployment.tls.certificate_path is not None
    assert deployment.tls.key_path is not None
    (tmp_path / "ca.crt").write_bytes(
        Path(deployment.tls.certificate_path).read_bytes()
    )
    (tmp_path / "ca.key").write_bytes(Path(deployment.tls.key_path).read_bytes())
    with (tmp_path / "fixture.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(FIXTURES), "host", str(tmp_path)],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 5
            while not (tmp_path / "ports.json").exists():
                assert process.poll() is None, (tmp_path / "fixture.log").read_text()
                if time.monotonic() >= deadline:
                    pytest.fail("fixture did not start")
                time.sleep(0.01)
            ports = json.loads((tmp_path / "ports.json").read_text())
            with socket.create_connection(
                ("127.0.0.1", ports["socks"]), timeout=5
            ) as stream:
                assert socks_request(stream, "www.gstatic.com", 443)[1] == 0
                context = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
                with context.wrap_socket(
                    stream, server_hostname="vpn.example.com"
                ) as tls:
                    tls.sendall(
                        b"GET /fixture-nonce HTTP/1.1\r\nHost: www.gstatic.com\r\nConnection: close\r\n\r\n"
                    )
                    response = b""
                    while chunk := tls.recv(4096):
                        response += chunk
                    assert b"proxy /fixture-nonce\n" in response
            for host, port in [("unlisted.invalid", 443), ("www.gstatic.com", 80)]:
                with socket.create_connection(
                    ("127.0.0.1", ports["socks"]), timeout=5
                ) as stream:
                    assert socks_request(stream, host, port)[1] == 2
        finally:
            process.terminate()
            process.wait(timeout=5)
