"""Local checks for the TUN acceptance fixtures. No VM or host changes."""

import json
import os
import runpy
import socket
import ssl
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sing_box_manager.desktop_config import UserProtocols, build_linux_tun_config
from sing_box_manager.settings import DeploymentConfig, TlsConfig, TrojanUser
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
    for request in [query("unlisted.invalid"), query("www.gstatic.com", 28)]:
        response = answer(request, proxied=True)
        assert struct.unpack(">H", response[6:8])[0] == 0


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
