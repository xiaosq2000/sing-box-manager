"""Stable subscription tokens, serialization, and legacy counter import."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from sing_box_manager.desktop_config import UserProtocols
from sing_box_manager.settings import SUPPORTED_PROTOCOLS, RuntimeInventory

ENVELOPE_VERSION = 1
DEFAULT_ROUTE = "china-direct"
PROTOCOLS = SUPPORTED_PROTOCOLS
TOKEN_BYTES = 16
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}")
_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_TOKEN_IN_PATH = re.compile(r'/sub/[^/?\s"\']+')


def subscription_token(secret: str, username: str, counter: int = 0) -> str:
    """Return the token in one user's current subscription link."""
    digest = hmac.new(
        secret.encode(), f"{username}\n{counter}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest[:TOKEN_BYTES]).rstrip(b"=").decode("ascii")


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.match(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def minimum_sing_box_version(pinned: str) -> str:
    parsed = parse_version(pinned)
    if parsed is None:
        raise ValueError(f"Unsupported sing-box version: {pinned}")
    return f"{parsed[0]}.{parsed[1]}.0"


def read_legacy_reset_counters(path: Path) -> dict[str, int]:
    """Read old SQLite counters for initial import. Never create or write the file."""
    if not path.exists():
        return {}
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as connection:
        if not connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'reset_counters'"
        ).fetchone():
            return {}
        return dict(connection.execute("SELECT username, counter FROM reset_counters"))


def user_protocols(inventory: RuntimeInventory, username: str) -> UserProtocols:
    """Project a user's credentials during initial import and parity checks."""

    def enabled(users):
        return next(
            (user for user in users if user.username == username and user.enabled),
            None,
        )

    return UserProtocols(
        trojan=enabled(inventory.trojan.users),
        hysteria2=enabled(inventory.hysteria2.users),
        naive=enabled(inventory.naive.users),
        hysteria2_settings=inventory.hysteria2,
        snell=enabled(inventory.snell.users),
        snell_settings=inventory.snell,
    )


def encode_envelope(envelope: dict) -> tuple[bytes, str]:
    body = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    return body, f'"{hashlib.sha256(body).hexdigest()[:32]}"'


def redact_token(text: str) -> str:
    return _TOKEN_IN_PATH.sub("/sub/[token]", text)


class TokenRedactingFilter(logging.Filter):
    """Keep subscription tokens out of request logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_token(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_token(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True
