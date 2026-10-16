"""Portal credentials and sessions are separate from Django operator authentication."""

import hmac
import threading
import time
from uuid import UUID

from argon2 import PasswordHasher
from django.db import connection, transaction

from sing_box_manager.managed_access import service
from sing_box_manager.settings import ARGON2ID_ENCODING, USERNAME_PATTERN
from sing_box_manager.web.auth import RateLimiter


class _WindowLimiter:
    """Bound attempts in each 60-second window, including successful logins."""

    def __init__(self, *, max_entries: int = 1024):
        self._max_entries = max_entries
        self._attempts: dict[str, tuple[float, int]] = {}

    def check(self, username: str, address: str) -> bool:
        now = time.monotonic()
        key = f"{username}:{address}"
        started, attempts = self._attempts.get(key, (now, 0))
        if now - started >= 60:
            started, attempts = now, 0
        if attempts >= 30:
            return False
        if key not in self._attempts and len(self._attempts) >= self._max_entries:
            self._attempts = {
                name: entry
                for name, entry in self._attempts.items()
                if now - entry[0] < 60
            }
            if len(self._attempts) >= self._max_entries:
                return False
        self._attempts[key] = (started, attempts + 1)
        return True


_LIMITER = RateLimiter()
_IP_LIMITER = _WindowLimiter()
_GLOBAL_LIMITER = _WindowLimiter(max_entries=1)
_LOCK = threading.Lock()
_HASHER = PasswordHasher()
_PASSWORD_LOCK = 0x53424D50


def authenticate(runtime, value, username: str, password: str, address: str):
    if not USERNAME_PATTERN.fullmatch(username) or len(password) > 1024:
        return None
    with _LOCK:
        allowed = (
            _GLOBAL_LIMITER.check("portal", "all")
            and _IP_LIMITER.check("portal", address)
            and _LIMITER.check(username, address)
        )
    if not allowed:
        return None
    from sing_box_manager.control_plane.models import ProxySubject

    subject = ProxySubject.objects.filter(
        source=value.source, username=username, present=True
    ).first()
    account = None if subject is None else service.portal_account(value, subject.pk)
    if account is None:
        return None
    if not _verify_with_capacity(account[1].password_hash, password):
        return None
    with _LOCK:
        _LIMITER.reset(username, address)
    return account


def _verify_with_capacity(verifier: str, password: str) -> bool:
    # Permit one KDF across web workers. The transaction releases the lock.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [_PASSWORD_LOCK])
        if not cursor.fetchone()[0]:
            return False
        return _verify_password(verifier, password)


def verifier_supported(verifier: str) -> bool:
    match = ARGON2ID_ENCODING.fullmatch(verifier)
    return match is not None and all(
        int(cost) <= bound
        for cost, bound in zip(match.groups()[:3], (131072, 6, 8), strict=True)
    )


def _verify_password(verifier: str, password: str) -> bool:
    # Check work limits before invoking the KDF, not while copying accounts.
    if not verifier_supported(verifier):
        return False
    try:
        return _HASHER.verify(verifier, password)
    except Exception:
        return False


def session_account(request, runtime, value):
    try:
        subject_id = UUID(request.session.get("proxy_subject", ""))
        account = service.portal_account(value, subject_id)
        expected = request.session.get("proxy_verifier", "")
        if (
            account is not None
            and isinstance(expected, str)
            and hmac.compare_digest(
                expected,
                service.session_identity(runtime, subject_id, account[1].password_hash),
            )
        ):
            return account
    except (ValueError, TypeError):
        pass
    return None
