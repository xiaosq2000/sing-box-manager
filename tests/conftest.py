"""Fixture isolation shared by the suite."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sing_box_manager import runtime_env
from sing_box_manager.web.auth import MachineTokenManager, RateLimiter


@pytest.fixture(autouse=True)
def isolate_local_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_env, "DEFAULT_DOTENV_PATH", tmp_path / ".env")


@pytest.fixture(autouse=True)
def isolate_legacy_tun_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBM_TUN_ROOT", str(tmp_path / "managed-tun-root"))


@pytest.fixture(autouse=True)
def isolate_kiwivm_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIWI_VEID", raising=False)
    monkeypatch.delenv("KIWI_API_KEY", raising=False)


@pytest.fixture
def machine_token_manager() -> MachineTokenManager:
    return MachineTokenManager(secret="test-secret-key")


@pytest.fixture
def rate_limiter() -> RateLimiter:
    return RateLimiter(max_attempts=3, lockout_seconds=10)


def utc_epoch(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()
