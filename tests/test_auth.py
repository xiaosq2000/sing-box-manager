"""Tests for SessionManager and RateLimiter."""

import time
from unittest.mock import patch

from sing_box_manager.web.auth import MachineTokenManager, RateLimiter, SessionManager


class TestSessionManager:
    def test_create_and_validate_token(self, session_manager: SessionManager):
        token = session_manager.create_token("alice")
        assert session_manager.validate_token(token) == "alice"

    def test_expired_token(self, session_manager: SessionManager):
        token = session_manager.create_token("alice")
        # Simulate token created 2 hours ago
        with patch("sing_box_manager.web.auth.time") as mock_time:
            mock_time.time.return_value = time.time() + 7200
            assert session_manager.validate_token(token) is None

    def test_tampered_token(self, session_manager: SessionManager):
        token = session_manager.create_token("alice")
        tampered = token[:-5] + "XXXXX"
        assert session_manager.validate_token(tampered) is None

    def test_invalid_token_format(self, session_manager: SessionManager):
        assert session_manager.validate_token("not-a-valid-token") is None
        assert session_manager.validate_token("") is None

    def test_different_secrets_reject(self):
        mgr1 = SessionManager(secret="secret-1")
        mgr2 = SessionManager(secret="secret-2")
        token = mgr1.create_token("alice")
        assert mgr2.validate_token(token) is None


class TestMachineTokenManager:
    def test_create_and_validate_token(
        self, machine_token_manager: MachineTokenManager
    ):
        token = machine_token_manager.create_token("alice")
        assert machine_token_manager.validate_token(token) == "alice"

    def test_expired_token(self, machine_token_manager: MachineTokenManager):
        token = machine_token_manager.create_token("alice")
        # Simulate time past 365 days
        with patch("sing_box_manager.web.auth.time") as mock_time:
            mock_time.time.return_value = time.time() + 366 * 86400
            assert machine_token_manager.validate_token(token) is None

    def test_tampered_token(self, machine_token_manager: MachineTokenManager):
        token = machine_token_manager.create_token("alice")
        tampered = token[:-5] + "XXXXX"
        assert machine_token_manager.validate_token(tampered) is None

    def test_session_token_rejected_as_machine_token(self):
        """A SessionManager token must not be accepted by MachineTokenManager."""
        secret = "shared-secret"
        session_mgr = SessionManager(secret=secret)
        machine_mgr = MachineTokenManager(secret=secret)
        session_token = session_mgr.create_token("alice")
        assert machine_mgr.validate_token(session_token) is None

    def test_machine_token_rejected_as_session_token(self):
        """A MachineTokenManager token must not be accepted by SessionManager."""
        secret = "shared-secret"
        session_mgr = SessionManager(secret=secret)
        machine_mgr = MachineTokenManager(secret=secret)
        machine_token = machine_mgr.create_token("alice")
        assert session_mgr.validate_token(machine_token) is None


class TestRateLimiter:
    def test_allows_initial_attempts(self, rate_limiter: RateLimiter):
        assert rate_limiter.check("user", "1.2.3.4") is True
        assert rate_limiter.check("user", "1.2.3.4") is True

    def test_blocks_after_max_attempts(self, rate_limiter: RateLimiter):
        # max_attempts=3
        rate_limiter.check("user", "1.2.3.4")
        rate_limiter.check("user", "1.2.3.4")
        assert rate_limiter.check("user", "1.2.3.4") is False

    def test_different_ips_tracked_separately(self, rate_limiter: RateLimiter):
        for _ in range(3):
            rate_limiter.check("user", "1.1.1.1")
        assert rate_limiter.check("user", "1.1.1.1") is False
        assert rate_limiter.check("user", "2.2.2.2") is True

    def test_reset_clears_attempts(self, rate_limiter: RateLimiter):
        rate_limiter.check("user", "1.2.3.4")
        rate_limiter.check("user", "1.2.3.4")
        rate_limiter.reset("user", "1.2.3.4")
        assert rate_limiter.check("user", "1.2.3.4") is True

    def test_lockout_expires(self, rate_limiter: RateLimiter):
        # Exhaust attempts
        for _ in range(3):
            rate_limiter.check("user", "1.2.3.4")
        assert rate_limiter.check("user", "1.2.3.4") is False

        # Simulate time passing beyond lockout
        with patch("sing_box_manager.web.auth.time") as mock_time:
            mock_time.time.return_value = time.time() + 20
            assert rate_limiter.check("user", "1.2.3.4") is True

    def test_table_stays_bounded(self):
        limiter = RateLimiter(max_attempts=3, lockout_seconds=10, max_entries=100)

        for index in range(1000):
            limiter.check(f"user{index}", "1.2.3.4")

        assert len(limiter._attempts) <= 100

    def test_flooding_the_table_keeps_existing_lockouts(self):
        limiter = RateLimiter(max_attempts=3, lockout_seconds=300, max_entries=100)
        for _ in range(3):
            limiter.check("victim", "1.2.3.4")

        for index in range(1000):
            limiter.check(f"junk{index}", "5.6.7.8")

        assert limiter.check("victim", "1.2.3.4") is False
