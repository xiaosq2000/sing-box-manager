"""Upstream download integrity and safe extraction. All network operations are fixtures."""


from __future__ import annotations

import hashlib
import tarfile
import zipfile
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from github.GithubException import UnknownObjectException

from sing_box_manager import runtime_env
from sing_box_manager.release import builder as builder_module
from sing_box_manager.release.artifacts import ReleaseInfo
from sing_box_manager.release.builder import (
    AssetIntegrityError,
    ReleaseBuilder,
    UnsafeArchiveError,
)
from sing_box_manager.release.digest_cache import sha256_for_path, write_cached_digest
from sing_box_manager.release.errors import ReleaseStageError
from sing_box_manager.release.platforms import (
    MIXED_PROFILE,
    Platform,
)
from sing_box_manager.release.ui import PlainReleaseReporter
from sing_box_manager.settings import Settings

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
        if builder._settings.client_signing_key is None:
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


def _build_settings(tmp_path: Path) -> Settings:
    config_repo = tmp_path / "config-repo"
    config_repo.mkdir()
    inventory_path = config_repo / "inventory" / "runtime.yaml"
    _write_inventory(inventory_path)

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

    scripts_dir = project_root / "scripts" / "lib"
    scripts_dir.mkdir(parents=True, exist_ok=True)
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
    binary = payload_dir / binary_name
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    return (1, 0)


def _linux_platform(extras: list[str] | None = None) -> Platform:
    return Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        # A synthetic mixed archive exercises the generic packing backend.
        # Current desktop platforms have no per-user archive or shell extras.
        extras=extras if extras is not None else [],
        protocol_extras={},
    )


def _fake_linux_download(builder: ReleaseBuilder) -> tuple[int, int]:
    return _seed_upstream(builder, _linux_platform())


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
