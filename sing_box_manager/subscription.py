"""Subscription links: the token in each user's link and the envelope sbc fetches.

A token is a truncated HMAC-SHA256 of the username and a reset counter, keyed by
`web.subscription_secret`. The portal stores only the counters, and recomputes
each user's current token to find who a link belongs to. Advancing a counter
replaces the user's link and cuts off the old one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from sing_box_manager.desktop_config import UserProtocols, build_desktop_config
from sing_box_manager.settings import RuntimeInventory, Settings, load_runtime_inventory

ENVELOPE_VERSION = 1
DEFAULT_ROUTE = "china"
PROTOCOLS = ("trojan", "hysteria2", "naive")
# 128 bits, which base64url writes as 22 characters.
TOKEN_BYTES = 16
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}")
_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_TOKEN_IN_PATH = re.compile(r"/sub/[^/?\s\"]+")


def subscription_token(secret: str, username: str, counter: int = 0) -> str:
    """Return the token in one user's current subscription link."""
    digest = hmac.new(
        secret.encode(), f"{username}\n{counter}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest[:TOKEN_BYTES]).rstrip(b"=").decode("ascii")


def find_subscriber(
    secret: str,
    token: str,
    usernames: Iterable[str],
    counters: Mapping[str, int] | None = None,
) -> str | None:
    """Return the user whose current token is `token`, or None.

    Every user is compared, in constant time, so the response time does not
    show where in the list a match sits.
    """
    if TOKEN_PATTERN.fullmatch(token) is None:
        return None
    counters = counters or {}
    match = None
    for username in usernames:
        expected = subscription_token(secret, username, counters.get(username, 0))
        if hmac.compare_digest(expected, token):
            match = username
    return match


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Read `major.minor.patch` from the start of a version, ignoring a suffix."""
    match = _VERSION_PATTERN.match(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def minimum_sing_box_version(pinned: str) -> str:
    """Return the oldest sing-box that runs configs built for `pinned`.

    The configs use features of the pinned minor release, such as `store_dns`
    in 1.14.
    """
    parsed = parse_version(pinned)
    if parsed is None:
        raise ValueError(f"Unsupported sing-box version: {pinned}")
    return f"{parsed[0]}.{parsed[1]}.0"


class ResetCounters:
    """Each user's link reset counter, in a small SQLite file.

    A user without a row is at 0, so a missing file means nobody has reset.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def current(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        with closing(sqlite3.connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'reset_counters'"
            ).fetchall()
            if not rows:
                return {}
            return dict(
                connection.execute("SELECT username, counter FROM reset_counters")
            )

    def advance(self, username: str) -> int:
        """Move the user to a new link and return its counter."""
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reset_counters "
                "(username TEXT PRIMARY KEY, counter INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO reset_counters (username, counter) VALUES (?, 1) "
                "ON CONFLICT (username) DO UPDATE SET counter = counter + 1",
                (username,),
            )
            (counter,) = connection.execute(
                "SELECT counter FROM reset_counters WHERE username = ?", (username,)
            ).fetchone()
        return counter


@dataclass(frozen=True)
class SubscriptionService:
    """What the portal needs to answer subscription requests."""

    secret: str
    inventory: RuntimeInventory
    default_protocol: str
    sing_box_version: str
    counters: ResetCounters

    @property
    def minimum_sing_box_version(self) -> str:
        return minimum_sing_box_version(self.sing_box_version)

    def token_for(self, username: str) -> str:
        return subscription_token(
            self.secret, username, self.counters.current().get(username, 0)
        )

    def find(self, token: str, usernames: Iterable[str]) -> str | None:
        return find_subscriber(self.secret, token, usernames, self.counters.current())

    def reset(self, username: str) -> str:
        """Replace the user's link and return the new token."""
        return subscription_token(
            self.secret, username, self.counters.advance(username)
        )

    def holds_protocols(self, username: str) -> bool:
        protocols = user_protocols(self.inventory, username)
        return any(getattr(protocols, name) is not None for name in PROTOCOLS)


def create_subscription_service(settings: Settings) -> SubscriptionService | None:
    """Return the service, or None when no subscription secret is set."""
    secret = settings.web.subscription_secret
    if secret is None:
        return None
    return SubscriptionService(
        secret=secret,
        inventory=load_runtime_inventory(settings.require_config_path()),
        default_protocol=settings.default_protocol,
        sing_box_version=settings.sing_box_version,
        counters=ResetCounters(settings.web.subscription_database_path),
    )


def user_protocols(inventory: RuntimeInventory, username: str) -> UserProtocols:
    """Collect one user's enabled credentials from the inventory."""

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
    )


def build_envelope(
    service: SubscriptionService,
    username: str,
    *,
    os_name: str,
    sbc_version: str | None = None,
) -> dict | None:
    """Return the envelope for one user, or None when they hold no protocol."""
    protocols = user_protocols(service.inventory, username)
    held = [name for name in PROTOCOLS if getattr(protocols, name) is not None]
    if not held:
        return None
    # A release checks that every user holds the default protocol. The server's
    # inventory can differ from the released one, so fall back instead of failing.
    default_protocol = (
        service.default_protocol if service.default_protocol in held else held[0]
    )
    return {
        "version": ENVELOPE_VERSION,
        "default_route": DEFAULT_ROUTE,
        "default_protocol": default_protocol,
        "nodes": [{"name": service.inventory.deployment.host, "protocols": held}],
        "latest": {
            "sing_box": service.sing_box_version,
            **({} if sbc_version is None else {"sbc": sbc_version}),
        },
        "config": build_desktop_config(
            service.inventory.deployment,
            protocols,
            os_name=os_name,
            default_protocol=default_protocol,
            default_route=DEFAULT_ROUTE,
        ),
    }


def encode_envelope(envelope: dict) -> tuple[bytes, str]:
    """Return the response body and the ETag that names it."""
    body = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    return body, f'"{hashlib.sha256(body).hexdigest()[:32]}"'


def redact_token(text: str) -> str:
    return _TOKEN_IN_PATH.sub("/sub/[token]", text)


class TokenRedactingFilter(logging.Filter):
    """Keep subscription tokens out of access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_token(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_token(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True
