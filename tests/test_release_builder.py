"""Focused release builder tests for split inventory rosters."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import zipfile
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from github.GithubException import UnknownObjectException

from sing_box_manager import runtime_env
from sing_box_manager.config_loader import load_auth_snapshot, load_runtime_inventory
from sing_box_manager.release import builder as builder_module
from sing_box_manager.release import packing
from sing_box_manager.release.artifacts import ReleaseInfo, read_release_info
from sing_box_manager.release.builder import (
    AssetIntegrityError,
    ReleaseBuilder,
    UnsafeArchiveError,
)
from sing_box_manager.release.digest_cache import sha256_for_path, write_cached_digest
from sing_box_manager.release.errors import ReleaseStageError
from sing_box_manager.release.manifest import MANIFEST_FILENAME, Manifest
from sing_box_manager.release.platforms import (
    MIXED_PROFILE,
    MOBILE_TUN_PROFILE,
    Platform,
    Protocol,
)
from sing_box_manager.release.store import STORE_DIR_NAME, Store
from sing_box_manager.release.ui import PlainReleaseReporter
from sing_box_manager.settings import Settings, TrafficStatsSettings

VALID_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "c29tZXNhbHQxMjM0NTY$"
    "c29tZWhhc2h2YWx1ZXNvbWVoaXN0b3J5"
)

CERTIFICATE_LINES = [
    "-----BEGIN CERTIFICATE-----",
    "MIIBbuildercertificate==",
    "-----END CERTIFICATE-----",
]

KEY_LINES = [
    "-----BEGIN RSA " + "PRIVATE KEY-----",
    "MIIEbuilderkey==",
    "-----END RSA " + "PRIVATE KEY-----",
]

PREPARE_RULE_SETS = ReleaseBuilder._prepare_rule_sets


@pytest.fixture(autouse=True)
def fixture_rule_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep full-pipeline packaging tests independent of GitHub and executables."""

    def prepare(builder: ReleaseBuilder) -> int:
        if builder._settings.client_signing_key is None and not any(
            MIXED_PROFILE in p.client_profiles for p in builder_module.PLATFORMS
        ):
            return 0
        destination = builder._staging_dir / "rules"
        destination.mkdir(parents=True, exist_ok=True)
        name = "a" * 64 + ".srs"
        snapshot = destination / name
        snapshot.write_bytes(b"fixture rule bytes")
        manifest = destination / "files.txt"
        manifest.write_text(name + "\n")
        builder._rule_snapshot_paths = [snapshot, manifest]
        return 1

    monkeypatch.setattr(ReleaseBuilder, "_prepare_rule_sets", prepare)
    monkeypatch.setattr(ReleaseBuilder, "_check_config", lambda *args, **kwargs: None)


def _fixed_release_info(version: str) -> ReleaseInfo:
    return ReleaseInfo(
        release_dir_name="20260401-9237d372_dirty",
        commit_sha="9237d372",
        dirty=True,
        date="20260401",
        upstream_version=version,
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_pem(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_inventory(
    path: Path,
    *,
    self_signed_cert: bool = False,
    key_path: str = "/etc/letsencrypt/live/vpn.example.com/privkey.pem",
    certificate_path: str = "/etc/letsencrypt/live/vpn.example.com/fullchain.pem",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    self_signed_cert_line = "    self_signed_cert: true\n" if self_signed_cert else ""
    server_name_line = "    server_name: vpn.example.com\n" if self_signed_cert else ""
    path.write_text(
        "deployment:\n"
        "  host: vpn.example.com\n"
        "  ip: 203.0.113.99\n"
        "  trojan_port: 8443\n"
        "  hysteria2_port: 58080\n"
        "  naive_port: 9443\n"
        "  tls:\n"
        "    enabled: true\n"
        f"{server_name_line}"
        f"    key_path: {key_path}\n"
        f"    certificate_path: {certificate_path}\n"
        f"{self_signed_cert_line}"
        "web_portal:\n"
        "  users:\n"
        "    - username: alice\n"
        f"      password_hash: {VALID_PASSWORD_HASH}\n"
        "      enabled: true\n"
        "    - username: carol\n"
        f"      password_hash: {VALID_PASSWORD_HASH}\n"
        "      enabled: true\n"
        "    - username: disabled-portal\n"
        f"      password_hash: {VALID_PASSWORD_HASH}\n"
        "      enabled: false\n"
        "trojan:\n"
        "  users:\n"
        "    - username: alice\n"
        "      password: alice-trojan-runtime-password\n"
        "      enabled: true\n"
        "    - username: trojan-only\n"
        "      password: trojan-only-runtime-password\n"
        "      enabled: true\n"
        "    - username: bob\n"
        "      password: bob-trojan-runtime-password\n"
        "      enabled: true\n"
        "    - username: disabled-trojan\n"
        "      password: disabled-trojan-password\n"
        "      enabled: false\n"
        "hysteria2:\n"
        "  obfs_password: hy2-obfs-runtime-secret\n"
        "  users:\n"
        "    - username: alice\n"
        "      password: alice-hy2-runtime-password\n"
        "      enabled: true\n"
        "    - username: bob\n"
        "      password: bob-hy2-runtime-password\n"
        "      enabled: true\n"
        "    - username: disabled-hy2\n"
        "      password: disabled-hy2-password\n"
        "      enabled: false\n"
        "naive:\n"
        "  users:\n"
        "    - username: alice\n"
        "      password: alice-naive-runtime-password\n"
        "      enabled: true\n"
        "    - username: disabled-naive\n"
        "      password: disabled-naive-password\n"
        "      enabled: false\n",
        encoding="utf-8",
    )


def _write_templates(config_repo: Path) -> None:
    _write_json(
        config_repo / "templates" / "client" / "trojan-client.json",
        {
            "template_marker": "trojan-client-template",
            "inbounds": [{"type": "mixed", "set_system_proxy": True}],
            "outbounds": [
                {
                    "type": "trojan",
                    "server": "template-server",
                    "server_port": 443,
                    "password": "template-password",
                    "tls": {
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        },
    )
    _write_json(
        config_repo / "templates" / "client" / "trojan-tun-client.json",
        {
            "template_marker": "trojan-tun-client-template",
            "inbounds": [{"type": "tun"}],
            "outbounds": [
                {
                    "type": "trojan",
                    "server": "template-server",
                    "server_port": 443,
                    "password": "template-password",
                    "tls": {
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        },
    )
    _write_json(
        config_repo / "templates" / "server" / "trojan-server.json",
        {
            "template_marker": "trojan-server-template",
            "inbounds": [
                {
                    "type": "trojan",
                    "listen_port": 443,
                    "users": [{"name": "template-user", "password": "template-pass"}],
                    "tls": {
                        "enabled": True,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
            "outbounds": [{"type": "direct"}],
        },
    )
    _write_json(
        config_repo / "templates" / "client" / "hysteria2-client.json",
        {
            "template_marker": "hysteria2-client-template",
            "inbounds": [{"type": "mixed", "set_system_proxy": True}],
            "outbounds": [
                {
                    "type": "hysteria2",
                    "server": "template-server",
                    "server_port": 443,
                    "password": "template-password",
                    "obfs": {"type": "salamander", "password": "template-obfs"},
                    "tls": {
                        "enabled": True,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        },
    )
    _write_json(
        config_repo / "templates" / "client" / "hysteria2-tun-client.json",
        {
            "template_marker": "hysteria2-tun-client-template",
            "inbounds": [{"type": "tun"}],
            "outbounds": [
                {
                    "type": "hysteria2",
                    "server": "template-server",
                    "server_port": 443,
                    "password": "template-password",
                    "obfs": {"type": "salamander", "password": "template-obfs"},
                    "tls": {
                        "enabled": True,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        },
    )
    _write_json(
        config_repo / "templates" / "server" / "hysteria2-server.json",
        {
            "template_marker": "hysteria2-server-template",
            "inbounds": [
                {
                    "type": "hysteria2",
                    "listen_port": 443,
                    "obfs": {"type": "salamander", "password": "template-obfs"},
                    "users": [{"name": "template-user", "password": "template-pass"}],
                    "tls": {
                        "enabled": True,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
            "outbounds": [{"type": "direct"}],
        },
    )
    _write_json(
        config_repo / "templates" / "client" / "naive-client.json",
        {
            "template_marker": "naive-client-template",
            "inbounds": [{"type": "mixed", "set_system_proxy": True}],
            "outbounds": [
                {
                    "type": "naive",
                    "server": "template-server",
                    "server_port": 443,
                    "username": "template-user",
                    "password": "template-password",
                    "tls": {
                        "enabled": True,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        },
    )
    _write_json(
        config_repo / "templates" / "client" / "naive-tun-client.json",
        {
            "template_marker": "naive-tun-client-template",
            "inbounds": [{"type": "tun"}],
            "outbounds": [
                {
                    "type": "naive",
                    "server": "template-server",
                    "server_port": 443,
                    "username": "template-user",
                    "password": "template-password",
                    "tls": {
                        "enabled": True,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
        },
    )
    _write_json(
        config_repo / "templates" / "server" / "naive-server.json",
        {
            "template_marker": "naive-server-template",
            "inbounds": [
                {
                    "type": "naive",
                    "listen_port": 443,
                    "network": "tcp",
                    "users": [
                        {"username": "template-user", "password": "template-pass"}
                    ],
                    "tls": {
                        "enabled": True,
                        "server_name": "template.example.com",
                        "key_path": "/tmp/template.key",
                        "certificate_path": "/tmp/template.crt",
                    },
                }
            ],
            "outbounds": [{"type": "direct"}],
        },
    )


def _build_settings(tmp_path: Path) -> Settings:
    config_repo = tmp_path / "config-repo"
    config_repo.mkdir()
    inventory_path = config_repo / "inventory" / "runtime.yaml"
    _write_inventory(inventory_path)
    _write_templates(config_repo)

    return Settings(
        sing_box_version="1.0.0",
        config_root=config_repo,
        releases_root=tmp_path / "releases",
        upstream_cache_root=tmp_path / "upstream-cache",
        config_path=inventory_path,
    )


def _build_relative_settings(project_root: Path) -> Settings:
    config_repo = project_root / "config"
    config_repo.mkdir(parents=True, exist_ok=True)
    inventory_path = config_repo / "inventory" / "runtime.yaml"
    _write_inventory(inventory_path)
    _write_templates(config_repo)

    scripts_dir = project_root / "scripts" / "lib"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (project_root / "scripts" / "client-install.sh").write_text(
        "client install", encoding="utf-8"
    )
    (project_root / "scripts" / "client-uninstall.sh").write_text(
        "client uninstall", encoding="utf-8"
    )
    (project_root / "scripts" / "server-install.sh").write_text(
        "server install", encoding="utf-8"
    )
    (project_root / "scripts" / "server-uninstall.sh").write_text(
        "server uninstall", encoding="utf-8"
    )
    (project_root / "scripts" / "sing-box.service").write_text(
        "node service", encoding="utf-8"
    )
    # The server package ships ui.sh.
    (project_root / "scripts" / "lib" / "ui.sh").write_text("ui.sh", encoding="utf-8")

    return Settings(
        sing_box_version="1.0.0",
        config_root=Path("./config"),
        releases_root=Path("./releases"),
        upstream_cache_root=Path("./.cache/sing-box-manager/upstream"),
        config_path=Path("./config/inventory/runtime.yaml"),
    )


def _protocols() -> list[Protocol]:
    return [
        Protocol(name="trojan"),
        Protocol(name="hysteria2"),
        Protocol(name="naive"),
    ]


def _seed_upstream(
    builder: ReleaseBuilder, platform: Platform, binary_name: str = "sing-box"
) -> tuple[int, int]:
    """Stand in for the download and extract stages.

    Extraction now lands in the content-addressed store rather than the release
    directory, and the store path is derived from the verified digest, so a fake
    download has to leave that digest behind for the path to resolve.
    """
    version = builder._settings.sing_box_version
    archive_path = builder._upstream_cache_root / platform.local_filename(version)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(b"fake upstream archive")
    write_cached_digest(archive_path, sha256_for_path(archive_path))
    builder._download_paths = {platform.name: archive_path}

    payload_dir = builder._upstream_payload_dir(platform)
    payload_dir.mkdir(parents=True, exist_ok=True)
    (payload_dir / binary_name).write_text("binary", encoding="utf-8")
    return (1, 0)


def _linux_platform(extras: list[str] | None = None) -> Platform:
    return Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=(
            extras
            if extras is not None
            else [
                "scripts/client-install.sh",
                "scripts/client-uninstall.sh",
                "docs/README_LINUX.md",
                "scripts/setup.sh",
                "scripts/lib/ui.sh",
            ]
        ),
        protocol_extras={},
    )


def _fake_linux_download(builder: ReleaseBuilder) -> tuple[int, int]:
    return _seed_upstream(builder, _linux_platform())


def test_build_fails_when_default_protocol_is_missing_for_release_users(
    tmp_path: Path,
) -> None:
    settings = _build_settings(tmp_path)
    settings.default_protocol = "naive"
    builder = ReleaseBuilder(settings)

    with pytest.raises(
        ReleaseStageError,
        match=r"default protocol naive is not enabled for all release users: bob, trojan-only",
    ):
        builder.build()


class _FakeGitReleaseAsset:
    def __init__(
        self,
        name: str,
        payload: bytes = b"binary",
        digest: str | None = None,
    ) -> None:
        self.name = name
        self.payload = payload
        if digest is None:
            self.digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        else:
            self.digest = digest
        self.download_calls: list[tuple[str, int | None]] = []

    def download_asset(self, path: str | None = None, chunk_size: int | None = 1):
        assert path is not None
        Path(path).write_bytes(self.payload)
        self.download_calls.append((path, chunk_size))


def _build_tar_gz_payload(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            archive.addfile(info, BytesIO(payload))
    return buffer.getvalue()


def _build_zip_payload(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _release_manifest(release_dir: Path) -> Manifest:
    return Manifest.from_json(
        (release_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )


def _assemble(release_dir: Path, filename: str) -> bytes:
    """Assemble exactly what the portal would serve for one archive.

    Archives are no longer written to disk: each is a shared prefix in the store
    plus this user's few KB of config. Tests therefore assert against what a
    download would produce rather than against a file.
    """
    manifest = _release_manifest(release_dir)
    entry = manifest.archive_by_filename(filename)
    assert entry is not None, f"{filename} is not described by the manifest"
    prefix = manifest.prefix_by_id(entry.prefix_id)
    store = Store(release_dir.parent / STORE_DIR_NAME)
    tail = [
        (member, (release_dir / member.src).read_bytes())
        for member in entry.tail_members
    ]
    return b"".join(
        packing.assemble(
            prefix,
            store.blob_path(prefix.prefix_id, prefix.archive_format),
            store.central_path(prefix.prefix_id),
            tail,
        ).chunks
    )


def _read_server_json(release_dir: Path, name: str) -> dict[str, object]:
    """Inspect one protocol's inbound in the single published node config."""
    config = json.loads(
        (release_dir / "server/config.json").read_text(encoding="utf-8")
    )
    if name != "config.json":
        protocol = name.removesuffix("-server.json")
        config["inbounds"] = [
            entry for entry in config["inbounds"] if entry["type"] == protocol
        ]
    return config


def _server_member_names(release_dir: Path) -> set[str]:
    """What the server package contains, including links into the store."""
    server_dir = release_dir / "server"
    return {
        str(path.relative_to(server_dir))
        for path in server_dir.rglob("*")
        if path.is_file() or path.is_symlink()
    }


def _list_tar_gz_members(data: bytes) -> set[str]:
    with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as archive:
        return set(archive.getnames())


def _read_tar_gz_json(data: bytes, member_name: str) -> dict[str, object]:
    with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as archive:
        file_obj = archive.extractfile(member_name)

        assert file_obj is not None
        return json.loads(file_obj.read().decode("utf-8"))


def _read_tar_gz_text(data: bytes, member_name: str) -> str:
    with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as archive:
        file_obj = archive.extractfile(member_name)

        assert file_obj is not None
        return file_obj.read().decode("utf-8")


def _list_zip_members(data: bytes) -> set[str]:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        return set(archive.namelist())


def _read_zip_json(data: bytes, member_name: str) -> dict[str, object]:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        return json.loads(archive.read(member_name).decode("utf-8"))


class _FakeGitRelease:
    def __init__(self, assets: list[_FakeGitReleaseAsset]) -> None:
        self._assets = assets

    def get_assets(self) -> list[_FakeGitReleaseAsset]:
        return self._assets


class _FakeGitRepo:
    def __init__(self, release: _FakeGitRelease | Exception) -> None:
        self._release = release
        self.requested_tags: list[str] = []

    def get_release(self, tag: str) -> _FakeGitRelease:
        self.requested_tags.append(tag)
        if isinstance(self._release, Exception):
            raise self._release
        return self._release


class _FakeGithubClient:
    def __init__(self, repo: _FakeGitRepo) -> None:
        self._repo = repo
        self.requested_repos: list[str] = []
        self.closed = False

    def get_repo(self, full_name: str) -> _FakeGitRepo:
        self.requested_repos.append(full_name)
        return self._repo

    def close(self) -> None:
        self.closed = True


def test_build_uses_split_rosters_for_rendered_outputs_and_auth_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[
            "scripts/client-install.sh",
            "scripts/client-uninstall.sh",
            "docs/README_LINUX.md",
            "scripts/setup.sh",
            "scripts/lib/ui.sh",
        ],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.setattr(builder_module, "PROTOCOLS", _protocols())

    load_calls: list[Path] = []

    def fake_load_runtime_inventory(path: Path):
        load_calls.append(path)
        return load_runtime_inventory(path)

    monkeypatch.setattr(
        builder_module,
        "load_runtime_inventory",
        fake_load_runtime_inventory,
        raising=False,
    )
    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)
    monkeypatch.setattr(
        builder, "_download_binaries", lambda: _fake_linux_download(builder)
    )

    stale_dir = builder._staging_dir
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "stale.txt").write_text("stale", encoding="utf-8")

    result = builder.build()

    assert load_calls == [Path(settings.config_path)]

    release_dir = builder._release_dir
    manifest = _release_manifest(release_dir)
    assert {entry.filename for entry in manifest.archives} == {
        f"{settings.release_name}-alice-linux-amd64.tar.gz",
        f"{settings.release_name}-bob-linux-amd64.tar.gz",
        f"{settings.release_name}-trojan-only-linux-amd64.tar.gz",
    }
    assert result.release_id == manifest.release_id
    # A previous run's staging directory must not survive into the release.
    assert not (release_dir / "stale.txt").exists()

    alice_archive = _assemble(
        release_dir, f"{settings.release_name}-alice-linux-amd64.tar.gz"
    )
    bob_archive = _assemble(
        release_dir, f"{settings.release_name}-bob-linux-amd64.tar.gz"
    )
    trojan_only_archive = _assemble(
        release_dir, f"{settings.release_name}-trojan-only-linux-amd64.tar.gz"
    )
    server_dir = release_dir / "server"

    alice_members = _list_tar_gz_members(alice_archive)
    bob_members = _list_tar_gz_members(bob_archive)
    trojan_only_members = _list_tar_gz_members(trojan_only_archive)

    assert "sing-box/client-install.sh" in alice_members
    assert "sing-box/client-uninstall.sh" in alice_members
    assert "sing-box/setup.sh" in alice_members
    assert "sing-box/client-version" in alice_members
    assert "sing-box/rules/files.txt" in alice_members
    assert "sing-box/rules/" + "a" * 64 + ".srs" in alice_members
    assert "sing-box/lib/ui.sh" in alice_members
    assert "sing-box/README_LINUX.md" in alice_members
    assert "sing-box/trojan-client.json" in alice_members
    assert "sing-box/trojan-gfw-client.json" in alice_members
    assert "sing-box/trojan-ai-client.json" in alice_members
    assert "sing-box/trojan-global-client.json" in alice_members
    assert "sing-box/hysteria2-client.json" in alice_members
    assert "sing-box/hysteria2-gfw-client.json" in alice_members
    assert "sing-box/hysteria2-ai-client.json" in alice_members
    assert "sing-box/hysteria2-global-client.json" in alice_members
    assert "sing-box/naive-client.json" in alice_members
    assert "sing-box/naive-gfw-client.json" in alice_members
    assert "sing-box/naive-ai-client.json" in alice_members
    assert "sing-box/naive-global-client.json" in alice_members
    client_version = _read_tar_gz_text(alice_archive, "sing-box/client-version")
    release_info = read_release_info(builder._resolve_path(settings.release_info_path))
    assert f"client_build_id={release_info.client_build_id}\n" in client_version
    assert "commit_sha=" in client_version
    assert "portal_base_url=https://vpn.example.com\n" in client_version
    assert "sing-box/trojan-client.json" in bob_members
    assert "sing-box/hysteria2-client.json" in bob_members
    assert "sing-box/naive-client.json" not in bob_members
    assert "sing-box/default-protocol" in alice_members
    assert "sing-box/default-protocol" in bob_members
    assert "sing-box/default-protocol" in trojan_only_members
    assert _read_tar_gz_text(alice_archive, "sing-box/default-protocol") == "trojan\n"
    assert "sing-box/trojan-client.json" in trojan_only_members
    assert "sing-box/hysteria2-client.json" not in trojan_only_members
    assert "sing-box/naive-client.json" not in trojan_only_members

    trojan_client = _read_tar_gz_json(alice_archive, "sing-box/trojan-client.json")
    assert trojan_client["outbounds"][0]["server"] == "203.0.113.99"
    assert trojan_client["outbounds"][0]["password"] == "alice-trojan-runtime-password"
    assert trojan_client["outbounds"][0]["tls"]["enabled"] is True
    assert trojan_client["outbounds"][0]["tls"]["server_name"] == "vpn.example.com"
    assert "key_path" not in trojan_client["outbounds"][0]["tls"]
    assert "certificate_path" not in trojan_client["outbounds"][0]["tls"]

    for route_filename in (
        "trojan-gfw-client.json",
        "trojan-ai-client.json",
        "trojan-global-client.json",
    ):
        variant = _read_tar_gz_json(alice_archive, f"sing-box/{route_filename}")
        assert variant["outbounds"][0]["server"] == "203.0.113.99"
        assert variant["outbounds"][0]["password"] == ("alice-trojan-runtime-password")
        assert variant["outbounds"][0]["tls"] == trojan_client["outbounds"][0]["tls"]

    # The ai route resolves direct traffic through the host's own resolver, and
    # a Linux package has to name systemd-resolved's stub: sing-box's `local`
    # transport binds its query socket to the link, which needs CAP_NET_RAW.
    trojan_ai = _read_tar_gz_json(alice_archive, "sing-box/trojan-ai-client.json")
    assert {"server": "127.0.0.53", "tag": "system", "type": "udp"} in trojan_ai["dns"][
        "servers"
    ]
    assert not any(
        server.get("type") == "local" for server in trojan_ai["dns"]["servers"]
    )

    hysteria2_client = _read_tar_gz_json(bob_archive, "sing-box/hysteria2-client.json")
    assert hysteria2_client["outbounds"][0]["server"] == "203.0.113.99"
    assert hysteria2_client["outbounds"][0]["server_port"] == 58080
    assert hysteria2_client["outbounds"][0]["password"] == "bob-hy2-runtime-password"
    assert (
        hysteria2_client["outbounds"][0]["obfs"]["password"]
        == "hy2-obfs-runtime-secret"
    )
    assert hysteria2_client["outbounds"][0]["tls"]["enabled"] is True
    assert hysteria2_client["outbounds"][0]["tls"]["server_name"] == "vpn.example.com"
    assert "key_path" not in hysteria2_client["outbounds"][0]["tls"]
    assert "certificate_path" not in hysteria2_client["outbounds"][0]["tls"]

    naive_client = _read_tar_gz_json(alice_archive, "sing-box/naive-client.json")
    assert naive_client["outbounds"][0]["server"] == "203.0.113.99"
    assert naive_client["outbounds"][0]["server_port"] == 9443
    assert naive_client["outbounds"][0]["username"] == "alice"
    assert naive_client["outbounds"][0]["password"] == "alice-naive-runtime-password"
    assert naive_client["outbounds"][0]["tls"]["enabled"] is True
    assert naive_client["outbounds"][0]["tls"]["server_name"] == "vpn.example.com"
    assert "key_path" not in naive_client["outbounds"][0]["tls"]
    assert "certificate_path" not in naive_client["outbounds"][0]["tls"]

    trojan_server = _read_server_json(builder._release_dir, "trojan-server.json")
    hysteria2_server = _read_server_json(builder._release_dir, "hysteria2-server.json")
    naive_server = _read_server_json(builder._release_dir, "naive-server.json")

    assert trojan_server["inbounds"][0]["listen_port"] == 8443
    assert trojan_server["inbounds"][0]["tls"]["enabled"] is True
    assert trojan_server["inbounds"][0]["tls"]["server_name"] == "vpn.example.com"
    assert (
        trojan_server["inbounds"][0]["tls"]["key_path"]
        == "/etc/letsencrypt/live/vpn.example.com/privkey.pem"
    )
    assert (
        trojan_server["inbounds"][0]["tls"]["certificate_path"]
        == "/etc/letsencrypt/live/vpn.example.com/fullchain.pem"
    )
    assert trojan_server["inbounds"][0]["users"] == [
        {"name": "trojan:alice", "password": "alice-trojan-runtime-password"},
        {"name": "trojan:trojan-only", "password": "trojan-only-runtime-password"},
        {"name": "trojan:bob", "password": "bob-trojan-runtime-password"},
    ]

    assert hysteria2_server["inbounds"][0]["listen_port"] == 58080
    assert (
        hysteria2_server["inbounds"][0]["obfs"]["password"] == "hy2-obfs-runtime-secret"
    )
    assert hysteria2_server["inbounds"][0]["tls"]["enabled"] is True
    assert hysteria2_server["inbounds"][0]["tls"]["server_name"] == "vpn.example.com"
    assert (
        hysteria2_server["inbounds"][0]["tls"]["key_path"]
        == "/etc/letsencrypt/live/vpn.example.com/privkey.pem"
    )
    assert (
        hysteria2_server["inbounds"][0]["tls"]["certificate_path"]
        == "/etc/letsencrypt/live/vpn.example.com/fullchain.pem"
    )
    assert hysteria2_server["inbounds"][0]["users"] == [
        {"name": "hysteria2:alice", "password": "alice-hy2-runtime-password"},
        {"name": "hysteria2:bob", "password": "bob-hy2-runtime-password"},
    ]

    assert naive_server["inbounds"][0]["listen_port"] == 9443
    assert naive_server["inbounds"][0]["network"] == "tcp"
    assert naive_server["inbounds"][0]["tls"]["enabled"] is True
    assert naive_server["inbounds"][0]["tls"]["server_name"] == "vpn.example.com"
    assert (
        naive_server["inbounds"][0]["tls"]["key_path"]
        == "/etc/letsencrypt/live/vpn.example.com/privkey.pem"
    )
    assert (
        naive_server["inbounds"][0]["tls"]["certificate_path"]
        == "/etc/letsencrypt/live/vpn.example.com/fullchain.pem"
    )
    assert naive_server["inbounds"][0]["users"] == [
        {"username": "alice", "password": "alice-naive-runtime-password"}
    ]

    server_members = _server_member_names(builder._release_dir)
    assert "config.json" in server_members
    assert not any(name.endswith("-server.json") for name in server_members)
    assert "server-install.sh" in server_members
    assert "server-uninstall.sh" in server_members
    assert "sing-box.service" in server_members
    assert len([name for name in server_members if name.endswith(".service")]) == 1
    assert "lib/ui.sh" in server_members
    # The binary is a link into the content-addressed store, so a release that
    # only adds a user does not put 58 MB back on the wire.
    assert (server_dir / "sing-box").is_symlink()
    assert (server_dir / "sing-box").read_text(encoding="utf-8") == "binary"

    snapshot_users = load_auth_snapshot(Path(settings.auth_snapshot_path))
    assert list(snapshot_users) == ["alice", "carol"]
    assert snapshot_users["alice"].password_hash == VALID_PASSWORD_HASH
    assert snapshot_users["carol"].password_hash == VALID_PASSWORD_HASH


def test_build_renders_self_signed_tls_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    certificate_path = tmp_path / "self-signed.crt"
    key_path = tmp_path / "self-signed.key"
    _write_pem(certificate_path, CERTIFICATE_LINES)
    _write_pem(key_path, KEY_LINES)
    _write_inventory(
        Path(settings.config_path),
        self_signed_cert=True,
        key_path=str(key_path),
        certificate_path=str(certificate_path),
    )
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.setattr(builder_module, "PROTOCOLS", _protocols())
    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)
    monkeypatch.setattr(
        builder, "_download_binaries", lambda: _fake_linux_download(builder)
    )

    builder.build()

    alice_archive = _assemble(
        builder._release_dir, f"{settings.release_name}-alice-linux-amd64.tar.gz"
    )

    trojan_client = _read_tar_gz_json(alice_archive, "sing-box/trojan-client.json")
    hysteria2_client = _read_tar_gz_json(
        alice_archive, "sing-box/hysteria2-client.json"
    )
    naive_client = _read_tar_gz_json(alice_archive, "sing-box/naive-client.json")
    trojan_server = _read_server_json(builder._release_dir, "trojan-server.json")
    hysteria2_server = _read_server_json(builder._release_dir, "hysteria2-server.json")
    naive_server = _read_server_json(builder._release_dir, "naive-server.json")

    assert trojan_client["outbounds"][0]["tls"]["certificate"] == CERTIFICATE_LINES
    assert "certificate_path" not in trojan_client["outbounds"][0]["tls"]
    assert "key_path" not in trojan_client["outbounds"][0]["tls"]

    assert hysteria2_client["outbounds"][0]["tls"]["certificate"] == CERTIFICATE_LINES
    assert "certificate_path" not in hysteria2_client["outbounds"][0]["tls"]
    assert "key_path" not in hysteria2_client["outbounds"][0]["tls"]

    assert naive_client["outbounds"][0]["tls"]["certificate"] == CERTIFICATE_LINES
    assert "certificate_path" not in naive_client["outbounds"][0]["tls"]
    assert "key_path" not in naive_client["outbounds"][0]["tls"]

    assert trojan_server["inbounds"][0]["tls"]["certificate"] == CERTIFICATE_LINES
    assert trojan_server["inbounds"][0]["tls"]["key"] == KEY_LINES
    assert "certificate_path" not in trojan_server["inbounds"][0]["tls"]
    assert "key_path" not in trojan_server["inbounds"][0]["tls"]

    assert hysteria2_server["inbounds"][0]["tls"]["certificate"] == CERTIFICATE_LINES
    assert hysteria2_server["inbounds"][0]["tls"]["key"] == KEY_LINES
    assert "certificate_path" not in hysteria2_server["inbounds"][0]["tls"]
    assert "key_path" not in hysteria2_server["inbounds"][0]["tls"]

    assert naive_server["inbounds"][0]["tls"]["certificate"] == CERTIFICATE_LINES
    assert naive_server["inbounds"][0]["tls"]["key"] == KEY_LINES
    assert "certificate_path" not in naive_server["inbounds"][0]["tls"]
    assert "key_path" not in naive_server["inbounds"][0]["tls"]


def test_build_uses_custom_server_binary_when_traffic_stats_are_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    settings.traffic_stats = TrafficStatsSettings(enabled=True)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.setattr(builder_module, "PROTOCOLS", _protocols())
    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)
    monkeypatch.setattr(
        builder, "_download_binaries", lambda: _fake_linux_download(builder)
    )

    def fake_build_custom_server_binary() -> Path:
        custom_binary = builder._release_dir / "sing-box-linux-amd64-server"
        custom_binary.write_text("custom server binary", encoding="utf-8")
        return custom_binary

    monkeypatch.setattr(
        builder,
        "_build_custom_server_binary",
        fake_build_custom_server_binary,
    )

    builder.build()

    assert (builder._release_dir / "server" / "sing-box").read_text(
        encoding="utf-8"
    ) == "custom server binary"

    trojan_server = _read_server_json(builder._release_dir, "trojan-server.json")
    hysteria2_server = _read_server_json(builder._release_dir, "hysteria2-server.json")
    naive_server = _read_server_json(builder._release_dir, "naive-server.json")

    assert trojan_server["experimental"]["v2ray_api"] == {
        "listen": settings.traffic_stats.api_listen,
        "stats": {
            "enabled": True,
            "users": [
                "trojan:alice",
                "trojan:trojan-only",
                "trojan:bob",
                "hysteria2:alice",
                "hysteria2:bob",
                "alice",
            ],
        },
    }
    assert (
        hysteria2_server["experimental"]
        == naive_server["experimental"]
        == trojan_server["experimental"]
    )
    assert len(trojan_server["services"]) == 1


def test_build_reports_plain_text_plan_and_result_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.setattr(builder_module, "PROTOCOLS", _protocols())
    monkeypatch.setattr(
        builder, "_download_binaries", lambda: _fake_linux_download(builder)
    )
    monkeypatch.setattr(builder, "_extract_archives", lambda: 1)

    result = builder.build()

    stdout = capsys.readouterr().out
    assert "Building sing-box v1.0.0 release" in stdout
    assert (
        "Plan: 3 release users, 2 portal users, default trojan, 1 platforms, 3 protocols, 4 archives"
        in stdout
    )
    assert "done: Render client configurations (3 archives described)" in stdout
    assert "done: Build shared platform payloads (1 built, 0 reused)" in stdout
    assert "Release complete." in stdout
    assert f"Server package: {result.server_package_dir}" in stdout
    assert f"Release id: {result.release_id}" in stdout
    assert result.user_archive_count == 3
    assert result.prefix_count == 1
    assert result.reused_existing_release is False


def test_download_binaries_warns_when_github_token_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    stdout = StringIO()
    reporter = PlainReleaseReporter(stdout=stdout)
    builder = ReleaseBuilder(settings, reporter=reporter)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    asset = _FakeGitReleaseAsset(platform.local_filename(settings.sing_box_version))
    fake_client = _FakeGithubClient(_FakeGitRepo(_FakeGitRelease([asset])))
    seen_tokens: list[str | None] = []

    def fake_create_github_client(token: str | None) -> _FakeGithubClient:
        seen_tokens.append(token)
        return fake_client

    monkeypatch.setattr(builder, "_create_github_client", fake_create_github_client)

    downloaded, reused = builder._download_binaries()

    assert downloaded == 1
    assert reused == 0
    assert seen_tokens == [None]
    assert fake_client.requested_repos == ["SagerNet/sing-box"]
    assert fake_client._repo.requested_tags == ["v1.0.0"]
    assert fake_client.closed is True
    expected_path = builder._cache_path_for_platform(
        platform, settings.sing_box_version
    )
    assert len(asset.download_calls) == 1
    assert Path(asset.download_calls[0][0]).parent == expected_path.parent
    assert Path(asset.download_calls[0][0]).name.startswith(f".{expected_path.name}.")
    assert asset.download_calls[0][1] == 1048576
    assert expected_path.exists()
    assert (
        "warning: GITHUB_TOKEN is not set; using anonymous GitHub API access"
        in stdout.getvalue()
    )


def test_download_binaries_skips_github_when_archives_are_already_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    stdout = StringIO()
    reporter = PlainReleaseReporter(stdout=stdout)
    builder = ReleaseBuilder(settings, reporter=reporter)
    builder._release_dir = tmp_path / "releases"
    builder._release_dir.mkdir(parents=True, exist_ok=True)

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])

    cached_path = builder._cache_path_for_platform(platform, settings.sing_box_version)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(b"cached")
    builder._write_cached_archive_digest(
        cached_path,
        hashlib.sha256(b"cached").hexdigest(),
    )

    def fail_create_github_client(token: str | None):
        raise AssertionError("GitHub client should not be created for cached downloads")

    monkeypatch.setattr(builder, "_create_github_client", fail_create_github_client)

    downloaded, reused = builder._download_binaries()

    assert downloaded == 0
    assert reused == 1
    assert builder._download_paths[platform.name] == cached_path
    assert "GITHUB_TOKEN" not in stdout.getvalue()


def test_download_binaries_redownloads_cached_archive_when_digest_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])

    cached_path = builder._cache_path_for_platform(platform, settings.sing_box_version)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(b"tampered")

    asset = _FakeGitReleaseAsset(
        platform.local_filename(settings.sing_box_version),
        payload=b"verified-binary",
    )
    fake_client = _FakeGithubClient(_FakeGitRepo(_FakeGitRelease([asset])))
    monkeypatch.setattr(builder, "_create_github_client", lambda token: fake_client)

    downloaded, reused = builder._download_binaries()

    assert downloaded == 1
    assert reused == 0
    assert len(asset.download_calls) == 1
    assert cached_path.read_bytes() == b"verified-binary"
    assert (
        builder._load_cached_archive_digest(cached_path)
        == hashlib.sha256(b"verified-binary").hexdigest()
    )


def test_download_binaries_uses_github_token_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    stdout = StringIO()
    reporter = PlainReleaseReporter(stdout=stdout)
    builder = ReleaseBuilder(settings, reporter=reporter)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.setenv("GITHUB_TOKEN", "token-value")

    asset = _FakeGitReleaseAsset(platform.local_filename(settings.sing_box_version))
    fake_client = _FakeGithubClient(_FakeGitRepo(_FakeGitRelease([asset])))
    seen_tokens: list[str | None] = []

    def fake_create_github_client(token: str | None) -> _FakeGithubClient:
        seen_tokens.append(token)
        return fake_client

    monkeypatch.setattr(builder, "_create_github_client", fake_create_github_client)

    downloaded, reused = builder._download_binaries()

    assert downloaded == 1
    assert reused == 0
    assert seen_tokens == ["token-value"]
    expected_path = builder._cache_path_for_platform(
        platform, settings.sing_box_version
    )
    assert len(asset.download_calls) == 1
    assert Path(asset.download_calls[0][0]).parent == expected_path.parent
    assert Path(asset.download_calls[0][0]).name.startswith(f".{expected_path.name}.")
    assert asset.download_calls[0][1] == 1048576


def test_download_binaries_rejects_assets_without_sha256_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])

    asset = _FakeGitReleaseAsset(
        platform.local_filename(settings.sing_box_version),
        digest="",
    )
    fake_client = _FakeGithubClient(_FakeGitRepo(_FakeGitRelease([asset])))
    monkeypatch.setattr(builder, "_create_github_client", lambda token: fake_client)

    with pytest.raises(
        AssetIntegrityError,
        match=r"did not include a sha256 digest",
    ):
        builder._download_binaries()


def test_github_token_loads_from_repo_local_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("GITHUB_TOKEN=dotenv-token\n", encoding="utf-8")

    monkeypatch.setattr(runtime_env, "DEFAULT_DOTENV_PATH", dotenv_file)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert builder._github_token() == "dotenv-token"


def test_github_token_prefers_exported_env_over_repo_local_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("GITHUB_TOKEN=dotenv-token\n", encoding="utf-8")

    monkeypatch.setattr(runtime_env, "DEFAULT_DOTENV_PATH", dotenv_file)
    monkeypatch.setenv("GITHUB_TOKEN", "shell-token")

    assert builder._github_token() == "shell-token"


def test_extract_archives_uses_the_store_for_extracted_files_when_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"
    builder._release_dir.mkdir(parents=True, exist_ok=True)

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])

    cached_path = builder._cache_path_for_platform(platform, settings.sing_box_version)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(
        _build_tar_gz_payload({"sing-box-1.0.0-linux-amd64/sing-box": b"binary"})
    )
    # Extraction targets a store path derived from the verified digest, so
    # the sidecar the download stage would have written has to exist.
    write_cached_digest(cached_path, sha256_for_path(cached_path))
    builder._download_paths = {platform.name: cached_path}

    extracted = builder._extract_archives()

    assert extracted == 1
    assert (builder._upstream_payload_dir(platform) / "sing-box").read_text(
        encoding="utf-8"
    ) == "binary"


def test_fetch_upstream_payload_fetches_only_the_requested_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    linux = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    darwin = Platform(
        name="darwin-arm64",
        url_filename="sing-box-{version}-darwin-arm64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [linux, darwin])
    # The release has no darwin asset, so fetching every platform would fail.
    asset = _FakeGitReleaseAsset(
        linux.local_filename(settings.sing_box_version),
        payload=_build_tar_gz_payload(
            {
                "sing-box-1.0.0-linux-amd64/sing-box": b"binary",
                "sing-box-1.0.0-linux-amd64/libcronet.so": b"library",
            }
        ),
    )
    fake_client = _FakeGithubClient(_FakeGitRepo(_FakeGitRelease([asset])))
    monkeypatch.setattr(builder, "_create_github_client", lambda token: fake_client)

    payload = builder.fetch_upstream_payload("linux-amd64")

    assert sorted(path.name for path in payload.iterdir()) == [
        "libcronet.so",
        "sing-box",
    ]
    assert (payload / "sing-box").read_bytes() == b"binary"
    assert len(asset.download_calls) == 1
    with pytest.raises(ValueError, match="No upstream platform is named windows-arm64"):
        builder.fetch_upstream_payload("windows-arm64")


def test_extract_archives_rejects_tar_members_outside_release_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"
    builder._release_dir.mkdir(parents=True, exist_ok=True)

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])

    cached_path = builder._cache_path_for_platform(platform, settings.sing_box_version)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _build_tar_gz_payload({"../escaped.txt": b"malicious"})
    cached_path.write_bytes(payload)
    # Extraction targets a store path derived from the verified digest, so
    # the sidecar the download stage would have written has to exist.
    write_cached_digest(cached_path, sha256_for_path(cached_path))
    builder._download_paths = {platform.name: cached_path}

    asset_name = platform.local_filename(settings.sing_box_version)
    replacement_asset = _FakeGitReleaseAsset(asset_name, payload=payload)
    monkeypatch.setattr(
        builder,
        "_load_release_assets",
        lambda version, missing_platforms: {asset_name: replacement_asset},
    )

    with pytest.raises(
        UnsafeArchiveError,
        match=r"would extract outside release directory",
    ):
        builder._extract_archives()

    assert not (tmp_path / "escaped.txt").exists()


def test_extract_archives_rejects_zip_members_outside_release_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"
    builder._release_dir.mkdir(parents=True, exist_ok=True)

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="windows-amd64",
        url_filename="sing-box-{version}-windows-amd64.zip",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])

    cached_path = builder._cache_path_for_platform(platform, settings.sing_box_version)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _build_zip_payload({"../escaped.txt": b"malicious"})
    cached_path.write_bytes(payload)
    # Extraction targets a store path derived from the verified digest, so
    # the sidecar the download stage would have written has to exist.
    write_cached_digest(cached_path, sha256_for_path(cached_path))
    builder._download_paths = {platform.name: cached_path}

    asset_name = platform.local_filename(settings.sing_box_version)
    replacement_asset = _FakeGitReleaseAsset(asset_name, payload=payload)
    monkeypatch.setattr(
        builder,
        "_load_release_assets",
        lambda version, missing_platforms: {asset_name: replacement_asset},
    )

    with pytest.raises(
        UnsafeArchiveError,
        match=r"would extract outside release directory",
    ):
        builder._extract_archives()

    assert not (tmp_path / "escaped.txt").exists()


def test_download_release_asset_cleans_up_temporary_file_on_failure(
    tmp_path: Path,
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    destination = tmp_path / "cache" / "archive.tar.gz"

    class FailingAsset:
        digest = f"sha256:{hashlib.sha256(b'partial').hexdigest()}"

        def download_asset(self, path: str | None = None, chunk_size: int | None = 1):
            assert path is not None
            Path(path).write_bytes(b"partial")
            raise RuntimeError("download failed")

    with pytest.raises(RuntimeError, match="download failed"):
        builder._download_release_asset(FailingAsset(), destination)

    assert not destination.exists()
    assert list(destination.parent.glob("*.tmp")) == []


def test_extract_archives_redownloads_corrupt_cached_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"
    builder._release_dir.mkdir(parents=True, exist_ok=True)

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])

    cached_path = builder._cache_path_for_platform(platform, settings.sing_box_version)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(b"corrupt")
    # Extraction targets a store path derived from the verified digest, so
    # the sidecar the download stage would have written has to exist.
    write_cached_digest(cached_path, sha256_for_path(cached_path))
    builder._download_paths = {platform.name: cached_path}

    asset_name = platform.local_filename(settings.sing_box_version)
    replacement_asset = _FakeGitReleaseAsset(
        asset_name,
        payload=_build_tar_gz_payload(
            {"sing-box-1.0.0-linux-amd64/sing-box": b"recovered"}
        ),
    )
    requested_versions: list[str] = []

    def fake_load_release_assets(version: str, missing_platforms: list[Platform]):
        requested_versions.append(version)
        assert missing_platforms == [platform]
        return {asset_name: replacement_asset}

    monkeypatch.setattr(builder, "_load_release_assets", fake_load_release_assets)

    extracted = builder._extract_archives()

    assert extracted == 1
    assert requested_versions == [settings.sing_box_version]
    assert len(replacement_asset.download_calls) == 1
    assert Path(replacement_asset.download_calls[0][0]).parent == cached_path.parent
    assert Path(replacement_asset.download_calls[0][0]).name.startswith(
        f".{cached_path.name}."
    )
    assert replacement_asset.download_calls[0][1] == 1048576
    assert cached_path.exists()
    assert (builder._upstream_payload_dir(platform) / "sing-box").read_text(
        encoding="utf-8"
    ) == "recovered"


def test_download_binaries_reports_missing_release_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    fake_client = _FakeGithubClient(
        _FakeGitRepo(UnknownObjectException(404, {"message": "Not Found"}, None))
    )
    monkeypatch.setattr(builder, "_create_github_client", lambda token: fake_client)

    with pytest.raises(
        ReleaseStageError,
        match=r"GitHub release tag v1.0.0 was not found in SagerNet/sing-box",
    ):
        builder._download_binaries()


def test_download_binaries_reports_missing_release_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    fake_client = _FakeGithubClient(
        _FakeGitRepo(_FakeGitRelease([_FakeGitReleaseAsset("unexpected-file.tar.gz")]))
    )
    monkeypatch.setattr(builder, "_create_github_client", lambda token: fake_client)

    with pytest.raises(
        ReleaseStageError,
        match=(
            r"GitHub release v1.0.0 is missing required assets: "
            r"sing-box-1.0.0-linux-amd64.tar.gz"
        ),
    ):
        builder._download_binaries()


def test_build_uses_tun_templates_and_only_writes_enabled_protocol_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = _build_settings(tmp_path)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="android-arm64",
        url_filename="sing-box-{version}-android-arm64.tar.gz",
        client_profiles=(MOBILE_TUN_PROFILE,),
        package_format_override="zip",
        package_ext_override=".zip",
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.setattr(builder_module, "PROTOCOLS", _protocols())

    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)

    monkeypatch.setattr(
        builder, "_download_binaries", lambda: _seed_upstream(builder, platform)
    )

    builder.build()

    alice_archive = _assemble(
        builder._release_dir, f"{settings.release_name}-alice-android-arm64.zip"
    )
    bob_archive = _assemble(
        builder._release_dir, f"{settings.release_name}-bob-android-arm64.zip"
    )
    trojan_only_archive = _assemble(
        builder._release_dir,
        f"{settings.release_name}-trojan-only-android-arm64.zip",
    )

    alice_members = _list_zip_members(alice_archive)
    bob_members = _list_zip_members(bob_archive)
    trojan_only_members = _list_zip_members(trojan_only_archive)

    assert "sing-box/trojan-tun-client.json" in alice_members
    assert not [name for name in alice_members if name.startswith("sing-box/rules/")]
    assert "sing-box/hysteria2-tun-client.json" in alice_members
    assert "sing-box/naive-tun-client.json" in alice_members
    assert not [name for name in alice_members if "-gfw-client.json" in name]
    assert not [name for name in alice_members if "-ai-client.json" in name]
    assert not [name for name in alice_members if "-global-client.json" in name]
    assert "sing-box/default-protocol" in alice_members
    assert "sing-box/trojan-tun-client.json" in bob_members
    assert "sing-box/hysteria2-tun-client.json" in bob_members
    assert "sing-box/naive-tun-client.json" not in bob_members
    assert "sing-box/default-protocol" in bob_members
    assert "sing-box/trojan-tun-client.json" in trojan_only_members
    assert "sing-box/hysteria2-tun-client.json" not in trojan_only_members
    assert "sing-box/naive-tun-client.json" not in trojan_only_members
    assert "sing-box/default-protocol" in trojan_only_members

    hysteria2_tun = _read_zip_json(alice_archive, "sing-box/hysteria2-tun-client.json")
    assert hysteria2_tun["outbounds"][0]["server"] == "203.0.113.99"
    assert hysteria2_tun["outbounds"][0]["server_port"] == 58080
    assert hysteria2_tun["outbounds"][0]["password"] == "alice-hy2-runtime-password"

    naive_tun = _read_zip_json(alice_archive, "sing-box/naive-tun-client.json")
    assert naive_tun["outbounds"][0]["server"] == "203.0.113.99"
    assert naive_tun["outbounds"][0]["server_port"] == 9443
    assert naive_tun["outbounds"][0]["username"] == "alice"
    assert naive_tun["outbounds"][0]["password"] == "alice-naive-runtime-password"


def test_build_resolves_relative_paths_from_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    settings = _build_relative_settings(project_root)
    monkeypatch.setattr(
        "sing_box_manager.release.builder.build_release_info",
        lambda version, *, project_root: _fixed_release_info(version),
    )
    builder = ReleaseBuilder(settings, project_root=project_root)

    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    platform = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [platform])
    monkeypatch.setattr(builder_module, "PROTOCOLS", _protocols())

    original_cwd = Path.cwd()
    monkeypatch.setattr(
        builder, "_download_binaries", lambda: _fake_linux_download(builder)
    )
    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)

    try:
        builder.build()
    finally:
        os.chdir(original_cwd)

    trojan_client = _read_tar_gz_json(
        _assemble(
            builder._release_dir, f"{settings.release_name}-alice-linux-amd64.tar.gz"
        ),
        "sing-box/trojan-client.json",
    )
    hysteria2_client = _read_tar_gz_json(
        _assemble(
            builder._release_dir, f"{settings.release_name}-bob-linux-amd64.tar.gz"
        ),
        "sing-box/hysteria2-client.json",
    )
    snapshot_users = load_auth_snapshot(
        project_root / "config" / "generated" / "auth-users.json"
    )

    assert trojan_client["outbounds"][0]["server"] == "203.0.113.99"
    assert hysteria2_client["outbounds"][0]["server"] == "203.0.113.99"
    assert list(snapshot_users) == ["alice", "carol"]
    assert builder._release_dir.parent == project_root / "releases"
    assert (
        project_root
        / ".cache/sing-box-manager/upstream/sing-box-1.0.0-linux-amd64.tar.gz"
    ).is_file()


def test_build_preserves_runtime_inventory_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = _build_settings(tmp_path)
    monkeypatch.setattr(
        "sing_box_manager.release.builder.build_release_info",
        lambda version, *, project_root: _fixed_release_info(version),
    )
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"

    inventory_path = Path(settings.config_path)
    preserved_inventory = inventory_path.read_text(encoding="utf-8")
    monkeypatch.setattr(builder, "_download_binaries", lambda: (0, 0))
    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)
    monkeypatch.setattr(builder, "_build_prefixes", lambda: (0, 0))
    monkeypatch.setattr(builder, "_render_user_configs", lambda users, deployment: [])
    monkeypatch.setattr(builder, "_build_server_release", lambda inventory: ())

    builder.build()

    assert inventory_path.read_text(encoding="utf-8") == preserved_inventory
    migrated_inventory = load_runtime_inventory(inventory_path)
    assert migrated_inventory.deployment.hysteria2_port == 58080
    assert migrated_inventory.deployment.naive_port == 9443


def test_build_runs_only_release_naming_git_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = _build_settings(tmp_path)

    commands: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(command: list[str], *args, **kwargs):
        commands.append(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases"
    monkeypatch.setattr(builder, "_download_binaries", lambda: (0, 0))
    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)
    monkeypatch.setattr(builder, "_build_prefixes", lambda: (0, 0))
    monkeypatch.setattr(builder, "_render_user_configs", lambda users, deployment: [])
    monkeypatch.setattr(builder, "_build_server_release", lambda inventory: ())

    builder.build()

    assert commands == [
        ["git", "rev-parse", "--short=8", "HEAD"],
        ["git", "status", "--short"],
    ]


def test_build_writes_release_info_with_upstream_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    monkeypatch.setattr(
        "sing_box_manager.release.builder.build_release_info",
        lambda version, *, project_root: _fixed_release_info(version),
    )
    builder = ReleaseBuilder(settings)
    builder._release_dir = tmp_path / "releases" / "20260401-9237d372_dirty"
    monkeypatch.setattr(builder, "_download_binaries", lambda: (0, 0))
    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)
    monkeypatch.setattr(builder, "_build_prefixes", lambda: (0, 0))
    monkeypatch.setattr(builder, "_render_user_configs", lambda users, deployment: [])
    monkeypatch.setattr(builder, "_build_server_release", lambda inventory: ())

    builder.build()

    release_info = read_release_info(
        tmp_path / "config-repo" / "generated" / "release-info.json"
    )
    # The directory is named after the content hash, not the commit. The commit
    # stays as provenance -- what built this -- and no longer gates deployment.
    assert release_info.release_id is not None
    assert release_info.release_dir_name == f"rel-{release_info.release_id[:12]}"
    assert release_info == ReleaseInfo(
        release_dir_name=release_info.release_dir_name,
        commit_sha="9237d372",
        dirty=True,
        date="20260401",
        upstream_version="1.0.0",
        deployment_host="vpn.example.com",
        release_id=release_info.release_id,
        client_build_id=release_info.client_build_id,
    )


def _build_linux_release(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    """Run a full build against a single-platform fake upstream."""
    builder = ReleaseBuilder(settings)
    builder_module = __import__("sing_box_manager.release.builder", fromlist=["_"])
    monkeypatch.setattr(builder_module, "PLATFORMS", [_linux_platform()])
    monkeypatch.setattr(builder_module, "PROTOCOLS", _protocols())
    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)
    monkeypatch.setattr(
        builder, "_download_binaries", lambda: _fake_linux_download(builder)
    )
    return builder.build()


def test_rule_preparation_covers_every_desktop_template_and_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path).model_copy(
        update={"client_signing_key": "key"}
    )
    builder = ReleaseBuilder(settings)
    _fake_linux_download(builder)
    calls = []

    def prepare(rules, destination, binary, token):
        calls.append(rules)
        # Five regional/filter sets plus the shared AI geosite snapshot.
        assert len(rules) == 6
        assert destination == builder._staging_dir / "rules"
        assert binary == builder._upstream_payload_dir(_linux_platform()) / "sing-box"
        assert token is None
        for name, rule in rules.items():
            assert rule["initial_path"] == f"rules/{name}"
        return [destination / name for name in (*rules, "files.txt")]

    monkeypatch.setattr(builder, "_github_token", lambda: None)
    monkeypatch.setattr(builder_module, "prepare_rule_snapshots", prepare)
    assert PREPARE_RULE_SETS(builder) == 6
    assert len(calls) == 1
    assert len(builder._rule_snapshot_paths) == 7


def test_failed_rule_refresh_preserves_published_release_and_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    first = _build_linux_release(settings, monkeypatch)
    builder = ReleaseBuilder(settings)
    auth = builder._resolve_path(settings.auth_snapshot_path)
    auth_before = auth.read_bytes()
    info = builder._resolve_path(settings.release_info_path)
    info_before = info.read_bytes()
    published = sorted(settings.releases_root.glob("rel-*"))

    def fail(_):
        raise ValueError("Rule source unavailable")

    monkeypatch.setattr(ReleaseBuilder, "_prepare_rule_sets", fail)
    with pytest.raises(ReleaseStageError, match="Rule source unavailable"):
        _build_linux_release(settings, monkeypatch)
    assert auth.read_bytes() == auth_before
    assert info.read_bytes() == info_before
    assert sorted(settings.releases_root.glob("rel-*")) == published
    assert first.release_dir.is_dir()


def test_changed_rule_bytes_change_release_and_archive_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _build_settings(tmp_path)
    first = _build_linux_release(settings, monkeypatch)
    prepare = ReleaseBuilder._prepare_rule_sets

    def refreshed(builder):
        count = prepare(builder)
        builder._rule_snapshot_paths[0].write_bytes(b"updated fixture rule bytes")
        return count

    monkeypatch.setattr(ReleaseBuilder, "_prepare_rule_sets", refreshed)
    second = _build_linux_release(settings, monkeypatch)
    assert first.release_id != second.release_id
    assert second.prefixes_built == 1
    name = f"{settings.release_name}-alice-linux-amd64.tar.gz"
    first_bytes = _assemble(first.release_dir, name)
    second_bytes = _assemble(second.release_dir, name)
    assert hashlib.sha256(first_bytes).digest() != hashlib.sha256(second_bytes).digest()
    for result, archive in ((first, first_bytes), (second, second_bytes)):
        manifest = _release_manifest(result.release_dir)
        entry = manifest.archive_by_filename(name)
        assert entry is not None
        prefix = manifest.prefix_by_id(entry.prefix_id)
        with tarfile.open(fileobj=BytesIO(archive), mode="r:gz") as opened:
            for member in prefix.members:
                if member.arcname.startswith("sing-box/rules/"):
                    content = opened.extractfile(member.arcname).read()
                    assert member.sha256 == hashlib.sha256(content).hexdigest()


def test_rebuilding_unchanged_inputs_reuses_the_existing_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline property: an unchanged rebuild must cost nothing to deploy."""
    settings = _build_settings(tmp_path)

    first = _build_linux_release(settings, monkeypatch)
    second = _build_linux_release(settings, monkeypatch)

    assert second.release_id == first.release_id
    assert second.release_dir == first.release_dir
    assert second.reused_existing_release is True
    assert second.prefixes_built == 0
    assert second.prefixes_reused == 1


def test_rotating_a_password_reuses_the_shared_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config-only change must not recompress or re-ship the 70 MB payload."""
    settings = _build_settings(tmp_path)
    first = _build_linux_release(settings, monkeypatch)

    inventory_path = Path(settings.config_path)
    inventory_path.write_text(
        inventory_path.read_text(encoding="utf-8").replace(
            "alice-trojan-runtime-password", "alice-trojan-rotated-password"
        ),
        encoding="utf-8",
    )

    second = _build_linux_release(settings, monkeypatch)

    # A new release, because what ships changed...
    assert second.release_id != first.release_id
    assert second.release_dir != first.release_dir
    assert second.reused_existing_release is False
    # ...but the expensive part was not redone, and both releases point at the
    # same stored payload, so the deploy carries only the rendered configs.
    assert second.prefixes_built == 0
    assert second.prefixes_reused == 1

    first_manifest = _release_manifest(first.release_dir)
    second_manifest = _release_manifest(second.release_dir)
    assert {p.prefix_id for p in first_manifest.prefixes} == {
        p.prefix_id for p in second_manifest.prefixes
    }
    assert first_manifest.payload_digests == second_manifest.payload_digests

    archive = _assemble(
        second.release_dir, f"{settings.release_name}-alice-linux-amd64.tar.gz"
    )
    config = _read_tar_gz_json(archive, "sing-box/trojan-client.json")
    assert config["outbounds"][0]["password"] == "alice-trojan-rotated-password"


def test_editing_a_client_script_moves_the_shared_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conversely, a script change must invalidate the shared payload."""
    settings = _build_settings(tmp_path)
    first = _build_linux_release(settings, monkeypatch)
    first_prefix = next(iter(_release_manifest(first.release_dir).prefixes))

    builder = ReleaseBuilder(settings)
    script = builder._root_dir / "scripts" / "client-install.sh"
    original = script.read_bytes()
    try:
        script.write_bytes(original + b"\n# a change\n")
        second = _build_linux_release(settings, monkeypatch)
    finally:
        script.write_bytes(original)

    second_prefix = next(iter(_release_manifest(second.release_dir).prefixes))
    assert second_prefix.prefix_id != first_prefix.prefix_id
    assert second.prefixes_built == 1


def test_rendered_configs_are_written_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They hold the user's credentials and are now loose files on two hosts."""
    settings = _build_settings(tmp_path)
    result = _build_linux_release(settings, monkeypatch)

    users_dir = result.release_dir / "users"
    assert users_dir.stat().st_mode & 0o777 == 0o700
    rendered = sorted(users_dir.rglob("*.json"))
    assert rendered
    for path in rendered:
        assert path.stat().st_mode & 0o777 == 0o600

    for path in (result.release_dir / "server").glob("config.json"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_archive_modes_survive_a_hostile_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modes come from the manifest, so the build machine's umask cannot leak in."""
    settings = _build_settings(tmp_path)
    original_umask = os.umask(0o077)
    try:
        result = _build_linux_release(settings, monkeypatch)
    finally:
        os.umask(original_umask)

    archive = _assemble(
        result.release_dir, f"{settings.release_name}-alice-linux-amd64.tar.gz"
    )
    with tarfile.open(fileobj=BytesIO(archive), mode="r:gz") as opened:
        modes = {member.name: member.mode for member in opened.getmembers()}

    assert modes["sing-box/client-install.sh"] == 0o755
    assert modes["sing-box/trojan-client.json"] == 0o644
    assert modes["sing-box/default-protocol"] == 0o644
