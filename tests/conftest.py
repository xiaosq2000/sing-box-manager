"""Shared test fixtures."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from sing_box_manager import runtime_env
from sing_box_manager.release import packing
from sing_box_manager.release.artifacts import ReleaseInfo, write_release_info
from sing_box_manager.release.manifest import (
    MANIFEST_FILENAME,
    TAR_GZ_FORMAT,
    ZIP_FORMAT,
    ArchiveEntry,
    Manifest,
)
from sing_box_manager.release.store import STORE_DIR_NAME, Store
from sing_box_manager.settings import Settings, WebSettings
from sing_box_manager.web.app import create_app
from sing_box_manager.web.auth import MachineTokenManager, RateLimiter, SessionManager


@pytest.fixture(autouse=True)
def isolate_local_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_env, "DEFAULT_DOTENV_PATH", tmp_path / ".env")


@pytest.fixture(autouse=True)
def isolate_managed_tun_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every managed TUN path at a scratch root, for every test.

    The shell scripts read `/var/lib/sing-box-manager-tun.status` and friends to
    decide whether a machine-wide tunnel exists. Without this the suite would
    read the developer's own machine, so a real `proxy tun on` -- even a failed
    one -- would change what unrelated installer tests do. Tests that care set
    their own value on top; this only guarantees it is never the real root.
    """
    monkeypatch.setenv("SBM_TUN_ROOT", str(tmp_path / "managed-tun-root"))


@pytest.fixture(autouse=True)
def isolate_kiwivm_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the KiwiVM credentials for every test, unless a test sets them.

    `kiwivm.resolve_credentials` falls back to the environment, so on the
    machine of whoever operates the real VPS these are exported and the portal
    fixtures build a live provider. `/api/usage` now calls that provider on the
    request path, which would make the suite fail there and only there -- while
    making real outbound calls to KiwiVM. Tests that want credentials
    `monkeypatch.setenv` them on top of this.
    """
    monkeypatch.delenv("KIWI_VEID", raising=False)
    monkeypatch.delenv("KIWI_API_KEY", raising=False)


@pytest.fixture
def session_manager() -> SessionManager:
    return SessionManager(secret="test-secret-key", expiry_seconds=3600)


@pytest.fixture
def machine_token_manager() -> MachineTokenManager:
    return MachineTokenManager(secret="test-secret-key")


@pytest.fixture
def rate_limiter() -> RateLimiter:
    return RateLimiter(max_attempts=3, lockout_seconds=10)


@pytest.fixture
def tmp_release_dir(tmp_path: Path) -> Path:
    """Create a temporary release directory with fake files."""
    release_dir = tmp_path / "releases" / "20260401-9237d372"
    release_dir.mkdir(parents=True)
    return release_dir


@pytest.fixture
def portal_passwords() -> dict[str, str]:
    return {
        "alice": "alice-portal-pass-123",
        "bob": "bob-portal-pass-456",
    }


@pytest.fixture
def trojan_passwords() -> dict[str, str]:
    return {
        "alice": "alice-trojan-pass-123",
        "bob": "bob-trojan-pass-456",
    }


@pytest.fixture
def auth_snapshot_path(tmp_path: Path, portal_passwords: dict[str, str]) -> Path:
    """Create an auth snapshot with enabled portal users."""
    password_hasher = PasswordHasher()
    snapshot = {
        "users": [
            {
                "username": username,
                "password_hash": password_hasher.hash(password),
                "enabled": True,
            }
            for username, password in portal_passwords.items()
        ]
    }
    snapshot_file = tmp_path / "generated" / "auth-users.json"
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
    return snapshot_file


@pytest.fixture
def test_settings(
    tmp_path: Path, tmp_release_dir: Path, auth_snapshot_path: Path
) -> Settings:
    """Create Settings for testing."""
    # Create fake release files that mirror real release output.
    (tmp_release_dir / "sing-box-v1.0.0-alice-linux-amd64.tar.gz").touch()
    (tmp_release_dir / "sing-box-v1.0.0-alice-linux-arm64.tar.gz").touch()
    (tmp_release_dir / "sing-box-v1.0.0-alice-darwin-arm64.tar.gz").touch()
    (tmp_release_dir / "sing-box-v1.0.0-alice-darwin-amd64.tar.gz").touch()
    (tmp_release_dir / "sing-box-v1.0.0-alice-windows-amd64.zip").touch()
    (tmp_release_dir / "sing-box-v1.0.0-alice-android-arm64.zip").touch()
    (tmp_release_dir / "sing-box-v1.0.0-bob-linux-amd64.tar.gz").touch()
    (tmp_release_dir / "sing-box-v1.0.0-server.tar.gz").touch()
    (tmp_release_dir / "sing-box-1.0.0-linux-amd64.tar.gz").touch()
    (tmp_release_dir / "sing-box-v1.0.0-alice" / "linux-amd64").mkdir(parents=True)
    write_release_info(
        tmp_path / "generated" / "release-info.json",
        ReleaseInfo(
            release_dir_name=tmp_release_dir.name,
            commit_sha="9237d372",
            dirty=False,
            date="20260401",
            upstream_version="1.0.0",
            release_id="cafebabe1234" + "0" * 52,
            client_build_id="d" * 64,
        ),
    )

    return Settings(
        sing_box_version="1.0.0",
        web=WebSettings(
            port=8080,
            session_secret="test-secret",
            allowed_hosts=["localhost", "testserver"],
        ),
        config_root=tmp_path,
        releases_root=tmp_path / "releases",
    )


@pytest.fixture
def test_app(test_settings: Settings, tmp_release_dir: Path):
    """Create a FastAPI test app."""
    app = create_app(test_settings)
    app.state.release_dir = tmp_release_dir
    return app


@pytest.fixture
def client(test_app):
    """Create a FastAPI TestClient."""
    from fastapi.testclient import TestClient

    return TestClient(test_app)


MANIFEST_USER_PROTOCOLS = {"alice": ("trojan", "naive"), "bob": ("trojan",)}
MANIFEST_PLATFORMS = (("linux-amd64", TAR_GZ_FORMAT), ("windows-amd64", ZIP_FORMAT))


def manifest_config_bytes(username: str, protocol: str) -> bytes:
    """The per-user config a manifest-backed release holds for one protocol."""
    return f'{{"outbounds":[{{"password":"{username}-{protocol}"}}]}}\n'.encode()


def utc_epoch(text: str) -> float:
    """A naive ISO timestamp read as UTC, as KiwiVM's unix-seconds fields are."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


@pytest.fixture
def manifest_settings(tmp_path: Path, auth_snapshot_path: Path) -> Settings:
    """Settings pointed at a release in the content-addressed layout.

    Distinct from ``test_settings`` on purpose: that one exercises the legacy
    prebuilt-file backend, which has to keep working for an older release, and
    this one exercises on-demand assembly.
    """
    releases_root = tmp_path / "releases"
    store = Store(releases_root / STORE_DIR_NAME)
    source_dir = tmp_path / "prefix-src"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "sing-box").write_bytes(b"\x7fELF" + b"payload" * 4000)
    (source_dir / "client-install.sh").write_text("#!/bin/bash\necho install\n")
    sources = [
        packing.SourceFile(
            "sing-box/client-install.sh", 0o755, source_dir / "client-install.sh"
        ),
        packing.SourceFile("sing-box/sing-box", 0o755, source_dir / "sing-box"),
    ]

    release_dir = releases_root / "rel-cafebabe1234"
    release_dir.mkdir(parents=True)
    prefixes = []
    archives = []
    for platform, archive_format in MANIFEST_PLATFORMS:
        prefix, _ = store.ensure_prefix(platform, archive_format, sources)
        prefixes.append(prefix)
        for username, protocols in MANIFEST_USER_PROTOCOLS.items():
            user_dir = release_dir / "users" / username / platform
            user_dir.mkdir(parents=True)
            tail_members = []
            for protocol in protocols:
                payload = manifest_config_bytes(username, protocol)
                name = f"{protocol}-client.json"
                (user_dir / name).write_bytes(payload)
                tail_members.append(
                    packing.member_for_bytes(
                        f"sing-box/{name}",
                        0o600,
                        payload,
                        f"users/{username}/{platform}/{name}",
                    )
                )
            extension = ".tar.gz" if archive_format == TAR_GZ_FORMAT else ".zip"
            archives.append(
                ArchiveEntry(
                    filename=f"sing-box-v1.0.0-{username}-{platform}{extension}",
                    username=username,
                    platform=platform,
                    archive_format=archive_format,
                    prefix_id=prefix.prefix_id,
                    tail_members=tuple(tail_members),
                )
            )

    manifest = Manifest(
        upstream_version="1.0.0",
        prefixes=tuple(prefixes),
        archives=tuple(archives),
    )
    (release_dir / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")

    write_release_info(
        tmp_path / "generated" / "release-info.json",
        ReleaseInfo(
            release_dir_name=release_dir.name,
            commit_sha="cafebabe",
            dirty=False,
            date="20260401",
            upstream_version="1.0.0",
            client_build_id="e" * 64,
            release_id=manifest.release_id,
        ),
    )

    return Settings(
        sing_box_version="1.0.0",
        web=WebSettings(
            port=8080,
            session_secret="test-secret",
            allowed_hosts=["localhost", "testserver"],
        ),
        config_root=tmp_path,
        releases_root=releases_root,
    )


@pytest.fixture
def manifest_client(manifest_settings: Settings):
    """A TestClient serving archives assembled on demand from the manifest."""
    from fastapi.testclient import TestClient

    return TestClient(create_app(manifest_settings))
