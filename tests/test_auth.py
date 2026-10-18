"""Legacy machine-token continuity and bounded attempt tracking."""

import hashlib
import time
from unittest.mock import patch

import pytest

from sing_box_manager.web.auth import MachineTokenManager, RateLimiter


@pytest.mark.parametrize("age", [0, 3600, 365 * 86400])
def test_existing_machine_tokens_still_validate(age):
    now = 2_000_000_000
    timestamp = now - age
    secret = "synthetic-session-secret"
    digest = hashlib.sha256(f"machine:alice:{timestamp}:{secret}".encode()).hexdigest()
    with patch("sing_box_manager.web.auth.time.time", return_value=now):
        assert MachineTokenManager(secret).validate_token(f"alice:{timestamp}:{digest}") == "alice"


@pytest.mark.parametrize("age", [-1, 366 * 86400])
def test_tokens_outside_the_time_window_are_rejected(age):
    manager = MachineTokenManager("synthetic-session-secret")
    with patch("sing_box_manager.web.auth.time.time", return_value=2_000_000_000 - age):
        token = manager.create_token("alice")
    with patch("sing_box_manager.web.auth.time.time", return_value=2_000_000_000):
        assert manager.validate_token(token) is None


def test_session_signatures_are_not_machine_tokens():
    timestamp = int(time.time())
    secret = "synthetic-session-secret"
    digest = hashlib.sha256(f"alice:{timestamp}:{secret}".encode()).hexdigest()
    assert MachineTokenManager(secret).validate_token(f"alice:{timestamp}:{digest}") is None


@pytest.mark.parametrize("token", ["", "invalid", "alice:invalid:signature", "alice:1:invalid"])
def test_malformed_tokens_are_rejected(token):
    assert MachineTokenManager("synthetic-session-secret").validate_token(token) is None


def test_tampered_tokens_and_other_keys_are_rejected(machine_token_manager):
    token = machine_token_manager.create_token("alice")
    assert machine_token_manager.validate_token(token) == "alice"
    assert machine_token_manager.validate_token(token[:-5] + "XXXXX") is None
    assert MachineTokenManager("other-key").validate_token(token) is None


def test_attempts_reset_lockout_and_expiry(rate_limiter):
    assert rate_limiter.check("alice", "127.0.0.1")
    assert rate_limiter.check("alice", "127.0.0.1")
    assert not rate_limiter.check("alice", "127.0.0.1")
    assert rate_limiter.check("alice", "127.0.0.2")
    with patch("sing_box_manager.web.auth.time.time", return_value=time.time() + 20):
        assert rate_limiter.check("alice", "127.0.0.1")
    rate_limiter.reset("alice", "127.0.0.1")
    assert rate_limiter.check("alice", "127.0.0.1")


def test_table_is_bounded_and_keeps_existing_lockouts():
    limiter = RateLimiter(max_attempts=3, lockout_seconds=300, max_entries=100)
    for _ in range(3):
        limiter.check("victim", "127.0.0.1")
    for index in range(1000):
        limiter.check(f"junk{index}", "127.0.0.2")
    assert len(limiter._attempts) <= 100
    assert not limiter.check("victim", "127.0.0.1")
