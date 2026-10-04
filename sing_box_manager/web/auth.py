"""Session management and rate limiting for the web service."""

import hashlib
import secrets
import time


class SessionManager:
    """HMAC-based session token management."""

    def __init__(self, secret: str, expiry_seconds: int = 3600) -> None:
        self._secret = secret
        self._expiry_seconds = expiry_seconds

    def create_token(self, username: str) -> str:
        """Create a signed session token for the given username."""
        timestamp = str(int(time.time()))
        token_data = f"{username}:{timestamp}:{self._secret}"
        token_hash = hashlib.sha256(token_data.encode()).hexdigest()
        return f"{username}:{timestamp}:{token_hash}"

    def validate_token(self, token: str) -> str | None:
        """Validate a session token and return the username, or None if invalid."""
        try:
            username, timestamp, token_hash = token.split(":", 2)
            current_time = int(time.time())
            token_age = current_time - int(timestamp)

            if token_age > self._expiry_seconds:
                return None

            expected_data = f"{username}:{timestamp}:{self._secret}"
            expected_hash = hashlib.sha256(expected_data.encode()).hexdigest()

            if secrets.compare_digest(token_hash, expected_hash):
                return username
            return None
        except Exception:
            return None


class MachineTokenManager:
    """HMAC-based long-lived machine token for CLI authentication."""

    def __init__(self, secret: str, expiry_seconds: int = 365 * 86400) -> None:
        self._secret = secret
        self._expiry_seconds = expiry_seconds

    def create_token(self, username: str) -> str:
        """Create a signed machine token for the given username."""
        timestamp = str(int(time.time()))
        token_data = f"machine:{username}:{timestamp}:{self._secret}"
        token_hash = hashlib.sha256(token_data.encode()).hexdigest()
        return f"{username}:{timestamp}:{token_hash}"

    def validate_token(self, token: str) -> str | None:
        """Validate a machine token and return the username, or None if invalid."""
        try:
            username, timestamp, token_hash = token.split(":", 2)
            current_time = int(time.time())
            token_age = current_time - int(timestamp)

            if token_age > self._expiry_seconds:
                return None

            expected_data = f"machine:{username}:{timestamp}:{self._secret}"
            expected_hash = hashlib.sha256(expected_data.encode()).hexdigest()

            if secrets.compare_digest(token_hash, expected_hash):
                return username
            return None
        except Exception:
            return None


class RateLimiter:
    """Track login attempts and enforce lockout after too many failures.

    The table is bounded. Without a bound, every distinct username and client IP
    that fails once stays in memory for the life of the process, so a stream of
    junk logins grows it without limit on a small VPS. When it fills, entries
    that are neither locked nor recent go first, then unlocked ones oldest
    first, so flooding the table is the slowest way to shed a lockout.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        lockout_seconds: int = 300,
        max_entries: int = 10_000,
    ) -> None:
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._max_entries = max_entries
        # key -> (attempts, lockout deadline or None, time of the last attempt)
        self._attempts: dict[str, tuple[int, float | None, float]] = {}

    def check(self, username: str, ip_address: str) -> bool:
        """Return True if the login attempt is allowed, False if rate-limited."""
        key = f"{username}:{ip_address}"
        current_time = time.time()
        entry = self._attempts.get(key)

        if entry is None:
            self._make_room(current_time)
            self._attempts[key] = (1, None, current_time)
            return True

        attempts, lockout_time, _ = entry

        # Check if user is in lockout period
        if lockout_time and current_time < lockout_time:
            return False

        # Reset counter if lockout has expired
        if lockout_time and current_time >= lockout_time:
            attempts = 0

        attempts += 1

        # If max attempts reached, set lockout time
        if attempts >= self._max_attempts:
            self._attempts[key] = (
                attempts,
                current_time + self._lockout_seconds,
                current_time,
            )
            return False

        self._attempts[key] = (attempts, None, current_time)
        return True

    def reset(self, username: str, ip_address: str) -> None:
        """Reset the attempt counter for a username/IP pair (e.g. after successful login)."""
        self._attempts.pop(f"{username}:{ip_address}", None)

    def _make_room(self, current_time: float) -> None:
        if len(self._attempts) < self._max_entries:
            return

        def locked(entry: tuple[int, float | None, float]) -> bool:
            return entry[1] is not None and current_time < entry[1]

        kept = {
            key: entry
            for key, entry in self._attempts.items()
            if locked(entry) or current_time - entry[2] < self._lockout_seconds
        }
        if len(kept) >= self._max_entries:
            # Free half the table at once, so a flood pays for the scan rarely.
            evict = sorted(kept, key=lambda key: (locked(kept[key]), kept[key][2]))
            for key in evict[: len(kept) - self._max_entries // 2]:
                del kept[key]
        self._attempts = kept
