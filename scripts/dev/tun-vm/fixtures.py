"""Bounded, allowlisted network fixtures for disposable TUN tests.

Host mode serves a SOCKS proxy, TLS targets, proxied DNS, and test assets.
Guest mode serves direct DNS. Direct mode runs in a guest network namespace.
No fixture forwards a request to the internet.
"""
# ruff: noqa: INP001

import argparse
import http.server
import json
import re
import select
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DIRECT = "198.18.0.10"
PROXY = "198.18.0.20"
DIRECT_V6 = "2001:db8:100::10"
PROXY_V6 = "2001:db8:100::20"
NAMES = {
    "www.baidu.com",
    "www.gstatic.com",
    "ai.fixture.test",
    "gfw.fixture.test",
    "china.fixture.test",
    "ordinary.fixture.test",
    "portal.fixture.test",
    "dns-path.fixture.test",
    "api.ipify.org",
    "api6.ipify.org",
    "4.ident.me",
    "6.ident.me",
    "ipwho.is",
    "speed.cloudflare.com",
}


def dns_answer(query: bytes, *, proxied: bool, baseline: bool = False) -> bytes:
    offset, labels = 12, []
    while query[offset]:
        length = query[offset]
        offset += 1
        labels.append(query[offset : offset + length].decode("ascii"))
        offset += length
    name = ".".join(labels).lower()
    end = offset + 5
    qtype = struct.unpack(">H", query[offset + 1 : offset + 3])[0]
    found = name in NAMES and qtype in {1, 28}
    direct = not proxied or name in {
        "www.baidu.com",
        "china.fixture.test",
        "portal.fixture.test",
    }
    address = (
        (DIRECT if direct else PROXY)
        if qtype != 28
        else (DIRECT_V6 if direct else PROXY_V6)
    )
    if baseline:
        address = "198.18.0.99" if qtype != 28 else "2001:db8:100::99"
    header = query[:2] + struct.pack(">HHHHH", 0x8180, 1, int(found), 0, 0)
    packed = (
        socket.inet_pton(socket.AF_INET6, address)
        if qtype == 28
        else socket.inet_aton(address)
    )
    record = b"\xc0\x0c" + struct.pack(">HHIH", qtype, 1, 1, len(packed)) + packed
    return header + query[12:end] + (record if found else b"")


def exact(stream: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        part = stream.recv(size - len(data))
        if not part:
            raise EOFError
        data += part
    return data


def socks_destination(stream: socket.socket) -> tuple[str, int]:
    version, count = exact(stream, 2)
    methods = exact(stream, count)
    if version != 5 or 0 not in methods:
        raise ValueError("unsupported SOCKS authentication")
    stream.sendall(b"\x05\x00")
    version, command, _, atyp = exact(stream, 4)
    if version != 5 or command != 1:
        raise ValueError("only CONNECT is supported")
    if atyp == 1:
        address = socket.inet_ntoa(exact(stream, 4))
    elif atyp == 3:
        address = exact(stream, exact(stream, 1)[0]).decode("ascii")
    elif atyp == 4:
        address = socket.inet_ntop(socket.AF_INET6, exact(stream, 16))
    else:
        raise ValueError("unsupported address type")
    return address, struct.unpack(">H", exact(stream, 2))[0]


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(server: socketserver.BaseServer) -> None:
    threading.Thread(target=server.serve_forever, daemon=True).start()


def context(assets: Path) -> ssl.SSLContext:
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(assets / "ca.crt", assets / "ca.key")
    return tls


def download_response(handler: http.server.BaseHTTPRequestHandler, query: str) -> None:
    try:
        size = int(parse_qs(query).get("bytes", ["0"])[0])
    except ValueError:
        size = 0
    if not 1 <= size <= 1_000_000:
        handler.send_error(400)
        return
    handler.send_response(200)
    handler.send_header("Content-Length", str(size))
    handler.end_headers()
    while size:
        count = min(size, 32768)
        handler.wfile.write(b"x" * count)
        handler.wfile.flush()
        size -= count
        time.sleep(0.002)


def target(
    assets: Path, address: str, identity: str
) -> http.server.ThreadingHTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            name = self.headers.get("Host", "").split(":")[0]
            if parsed.path == "/diagnostic-empty":
                self.send_response(204)
                self.end_headers()
                return
            if parsed.path == "/diagnostic-slow":
                time.sleep(0.05)
            if name in {"api.ipify.org", "4.ident.me"}:
                time.sleep(0.05)
                body = (
                    b"203.0.113.7" if identity.startswith("proxy") else b"198.51.100.7"
                )
            elif name in {"api6.ipify.org", "6.ident.me"}:
                body = b"2001:db8::7"
            elif name == "ipwho.is":
                body = json.dumps(
                    {"success": True, "ip": parsed.path.lstrip("/")}
                ).encode()
            elif name == "speed.cloudflare.com" and parsed.path == "/__down":
                download_response(self, parsed.query)
                return
            else:
                body = (identity + " " + self.path + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *_args: object) -> None:  # noqa: A002
            pass

    class IPv6Server(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6

    server_type = IPv6Server if ":" in address else http.server.ThreadingHTTPServer
    server = server_type(
        (address, 443 if address in {DIRECT, DIRECT_V6} else 0), Handler
    )
    server.socket = context(assets).wrap_socket(server.socket, server_side=True)
    start(server)
    return server


def host(assets: Path) -> None:  # noqa: C901 -- nested fixture server handlers
    site = target(assets, "127.0.0.1", "proxy")
    alternate = target(assets, "127.0.0.1", "proxy-alt")
    tls = context(assets)

    class TLSStream(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            try:
                with tls.wrap_socket(self.request, server_side=True) as stream:
                    stream.settimeout(10)
                    while True:
                        size = struct.unpack(">H", exact(stream, 2))[0]
                        answer = dns_answer(exact(stream, size), proxied=True)
                        stream.sendall(struct.pack(">H", len(answer)) + answer)
            except (EOFError, OSError, ValueError, IndexError):
                return

    dns = TCPServer(("127.0.0.1", 0), TLSStream)
    start(dns)

    class SOCKS(socketserver.BaseRequestHandler):
        site_port = site.server_address[1]

        def handle(self) -> None:
            stream = self.request
            stream.settimeout(20)
            try:
                address, port = socks_destination(stream)
                if address == "198.18.0.53" and port == 853:
                    destination = ("127.0.0.1", dns.server_address[1])
                elif (
                    address in NAMES | {DIRECT, PROXY, DIRECT_V6, PROXY_V6}
                    and port == 443
                ):
                    destination = ("127.0.0.1", self.site_port)
                else:
                    stream.sendall(b"\x05\x02\x00\x01" + bytes(6))
                    return
                with socket.create_connection(destination, timeout=5) as upstream:
                    stream.sendall(b"\x05\x00\x00\x01" + bytes(6))
                    while True:
                        ready, _, _ = select.select([stream, upstream], [], [], 15)
                        if not ready:
                            return
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            (upstream if source is stream else stream).sendall(data)
            except (EOFError, OSError, ValueError):
                return

    class AlternateSOCKS(SOCKS):
        site_port = alternate.server_address[1]

    socks = TCPServer(("127.0.0.1", 0), SOCKS)
    socks_alt = TCPServer(("127.0.0.1", 0), AlternateSOCKS)
    start(socks)
    start(socks_alt)

    class Assets(http.server.SimpleHTTPRequestHandler):
        release_choice = "good"

        def do_GET(self) -> None:
            # Keep the subscription endpoint a file, with authenticated-style
            # download paths below it, as the real portal exposes them.
            if "/files/" in self.path and self.path.startswith("/sub/"):
                prefix, relative = self.path.split("/files/", 1)
                subscription = next((assets / "sub").iterdir())
                allowed = {"manifest.json", "manifest.json.sig", "sbc/linux-amd64/sbc"}
                allowed.update(
                    "rules/" + path.name
                    for path in (assets / "release/rules").glob("*.srs")
                )
                allowed.update(
                    "sing-box/" + path.name
                    for path in (assets / "release/sing-box").glob(
                        "sing-box-*-linux-amd64.tar.gz"
                    )
                )
                if prefix != "/sub/" + subscription.name or relative not in allowed:
                    self.send_error(404)
                    return
                self.path = "/release/" + relative
            super().do_GET()

        def do_POST(self) -> None:
            if self.path == "/fixture/lifecycle-evidence":
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    self.send_error(400)
                    return
                payload = json.loads(self.rfile.read(length))
                name = payload.get("name", "")
                if (
                    not re.fullmatch("[a-z-]+", name)
                    or payload.get("run_id") != (assets / "run-id").read_text().strip()
                ):
                    self.send_error(400)
                    return
                directory = assets.parent / "lifecycle-evidence"
                directory.mkdir(mode=0o700, exist_ok=True)
                with (directory / (name + ".json")).open("x") as output:
                    json.dump(payload, output, indent=2)
                    output.write("\n")
                self.send_response(204)
                self.end_headers()
                return
            if self.path == "/fixture/release/new":
                if not (assets / "old-release").is_dir():
                    self.send_error(404)
                    return
                replacement = assets / ".next-release"
                replacement.symlink_to("new-release", target_is_directory=True)
                replacement.replace(assets / "release")
                self.send_response(204)
                self.end_headers()
                return
            choices = {
                "/fixture/sub/good": "good-sub.json",
                "/fixture/sub/failed": "failed-sub.json",
                "/fixture/sub/direct-failed": "direct-failed-sub.json",
                "/fixture/sub/changed": "good-sub.json",
            }
            if self.path in choices:
                target = next((assets / "sub").iterdir())
                target.write_bytes((assets / choices[self.path]).read_bytes())
                type(self).release_choice = (
                    "changed" if self.path.endswith("/changed") else "good"
                )
            elif self.path not in {
                "/fixture/manifest/tampered",
                "/fixture/manifest/valid",
            }:
                self.send_error(404)
                return
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("release.py")),
                    str(assets),
                    "--choice",
                    self.release_choice,
                ],
                check=True,
            )
            if self.path.endswith("/tampered"):
                (assets / "release/manifest.json.sig").write_text(
                    "invalid-fixture-signature\n"
                )
            self.send_response(204)
            self.end_headers()

        def __init__(
            self,
            request: socket.socket,
            client_address: tuple[str, int],
            server: socketserver.BaseServer,
        ) -> None:
            super().__init__(request, client_address, server, directory=str(assets))

        def log_message(self, format: str, *_args: object) -> None:  # noqa: A002
            pass

    portal = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Assets)
    (assets / "ports.json").write_text(
        json.dumps(
            {
                "socks": socks.server_address[1],
                "socks_alt": socks_alt.server_address[1],
                "portal": portal.server_address[1],
            }
        )
    )
    portal.serve_forever()


def guest_dns(address: str, port: int, baseline: bool) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((address, port))
    while True:
        query, peer = server.recvfrom(4096)
        try:
            server.sendto(dns_answer(query, proxied=False, baseline=baseline), peer)
        except (ValueError, IndexError, struct.error):
            continue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["host", "guest-dns", "direct"])
    parser.add_argument("assets", type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5353)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--ipv6", action="store_true")
    args = parser.parse_args()
    if args.mode == "host":
        host(args.assets)
    elif args.mode == "guest-dns":
        guest_dns(args.bind, args.port, args.baseline)
    else:
        server = target(args.assets, DIRECT, "direct")
        if args.ipv6:
            target(args.assets, DIRECT_V6, "direct-v6")
        threading.Event().wait()
        server.server_close()


if __name__ == "__main__":
    main()
