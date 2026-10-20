# ruff: noqa: INP001
"""A tiny DNS server for the nested-VM TUN test.

The names the checks use resolve to fixed addresses. No query leaves the VM,
which keeps the TUN's DNS path from looping through the local upstream.
"""

import socket
import struct
import sys

ANSWERS = {
    "test.example": "127.0.0.1",
    "example.com": "93.184.216.34",
    "www.gstatic.com": "142.251.153.120",
}


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


def answer(data: bytes, end: int, address: str) -> bytes:
    header = data[:2] + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0)
    record = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(address)
    return header + data[12:end] + record


def empty(data: bytes, end: int) -> bytes:
    header = data[:2] + struct.pack(">HHHHH", 0x8180, 1, 0, 0, 0)
    return header + data[12:end]


def serve(port: int) -> None:
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
            continue
        if qtype == 1 and name in ANSWERS:
            server.sendto(answer(query, end, ANSWERS[name]), peer)
        else:
            server.sendto(empty(query, end), peer)


if __name__ == "__main__":
    serve(int(sys.argv[1]))
