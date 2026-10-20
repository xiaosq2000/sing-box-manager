# ruff: noqa: INP001
"""A tiny DNS server for the nested-VM TUN test.

test.example resolves to the local site. Every other query is forwarded to
8.8.8.8 so the TUN verification and an HTTP request can use ordinary names.
"""

import socket
import struct
import sys


def question_end(data: bytes) -> tuple[str, int]:
    offset = 12
    labels = []
    while True:
        length = data[offset]
        offset += 1
        if length == 0:
            break
        labels.append(data[offset : offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels).lower(), offset + 4


def answer_for_test(data: bytes, end: int, name: str) -> bytes:
    header = data[:2] + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0)
    question = data[12:end]
    record = (
        b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("127.0.0.1")
    )
    return header + question + record


def fail(data: bytes, rcode: int) -> bytes:
    return data[:2] + struct.pack(">HHHHH", 0x8180 | rcode, 0, 0, 0, 0)


def serve(port: int) -> None:
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.settimeout(5)
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", port))
    while True:
        query, peer = server.recvfrom(4096)
        if len(query) < 17:
            continue
        try:
            name, end = question_end(query)
            qtype = struct.unpack(">H", query[end - 4 : end - 2])[0]
        except (IndexError, struct.error):
            server.sendto(fail(query, 1), peer)
            continue
        if name == "test.example" and qtype == 1:
            server.sendto(answer_for_test(query, end, name), peer)
            continue
        try:
            upstream.sendto(query, ("8.8.8.8", 53))
            response, _ = upstream.recvfrom(4096)
        except OSError:
            server.sendto(fail(query, 2), peer)
            continue
        server.sendto(response, peer)


if __name__ == "__main__":
    serve(int(sys.argv[1]))
