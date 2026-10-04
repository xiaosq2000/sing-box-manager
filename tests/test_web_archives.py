"""Tests for how the portal turns a release directory into downloadable bytes.

Two things are being defended here. The first is that a request can only ever
reach its own user's archive: assembly is driven by the manifest, so a filename
that parses as someone else's must not resolve even when guessed exactly. The
second is that what comes out is a real archive -- prefix members plus exactly
this user's tail, at the modes the manifest recorded rather than whatever the
files happen to carry after crossing rsync.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from sing_box_manager.release import packing
from sing_box_manager.release.manifest import (
    MANIFEST_FILENAME,
    TAR_GZ_FORMAT,
    ZIP_FORMAT,
    ArchiveEntry,
    Manifest,
)
from sing_box_manager.release.store import STORE_DIR_NAME, Store
from sing_box_manager.web.archives import (
    ArchiveIntegrityError,
    ArchiveNotFoundError,
    LegacyDirectoryArchiveIndex,
    ManifestArchiveIndex,
    build_archive_index,
)

USERS = ("alice", "bob")
# alice has two protocols, bob has three: the member set genuinely varies per
# user, which is why assembly cannot be a blind concatenation.
USER_PROTOCOLS = {"alice": ("trojan", "naive"), "bob": ("trojan", "naive", "hysteria2")}
PLATFORM_FORMATS = (("linux-amd64", TAR_GZ_FORMAT), ("windows-amd64", ZIP_FORMAT))


def _config_bytes(username: str, protocol: str) -> bytes:
    return (
        f'{{"outbounds":[{{"password":"{username}-{protocol}-secret"}}]}}\n' * 30
    ).encode()


def _prefix_sources(tmp_path: Path, platform: str) -> list[packing.SourceFile]:
    source_dir = tmp_path / "upstream" / platform
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "sing-box").write_bytes(b"\x7fELF" + platform.encode() * 2000)
    (source_dir / "client-install.sh").write_text("#!/bin/bash\necho install\n")
    return [
        packing.SourceFile(
            "sing-box/client-install.sh", 0o755, source_dir / "client-install.sh"
        ),
        packing.SourceFile("sing-box/sing-box", 0o755, source_dir / "sing-box"),
    ]


@pytest.fixture
def release_dir(tmp_path: Path) -> Path:
    """A release in the content-addressed layout, with a real store beside it."""
    releases_root = tmp_path / "releases"
    store = Store(releases_root / STORE_DIR_NAME)
    release = releases_root / "rel-0123456789ab"
    release.mkdir(parents=True)

    prefixes = []
    archives = []
    for platform, archive_format in PLATFORM_FORMATS:
        prefix, _ = store.ensure_prefix(
            platform, archive_format, _prefix_sources(tmp_path, platform)
        )
        prefixes.append(prefix)

        for username in USERS:
            user_dir = release / "users" / username / platform
            user_dir.mkdir(parents=True)
            tail_members = []
            for protocol in USER_PROTOCOLS[username]:
                payload = _config_bytes(username, protocol)
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
    (release / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")
    return release


def _index(release_dir: Path) -> ManifestArchiveIndex:
    index = build_archive_index(release_dir)
    assert isinstance(index, ManifestArchiveIndex)
    return index


def _tar_members(data: bytes) -> dict[str, tuple[int, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = {}
        for info in archive.getmembers():
            extracted = archive.extractfile(info)
            members[info.name] = (
                info.mode,
                b"" if extracted is None else extracted.read(),
            )
        return members


def _zip_members(data: bytes) -> dict[str, tuple[int, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {
            info.filename: (
                (info.external_attr >> 16) & 0xFFFF,
                archive.read(info.filename),
            )
            for info in archive.infolist()
        }


def test_a_manifest_selects_the_assembling_backend(release_dir: Path) -> None:
    assert isinstance(build_archive_index(release_dir), ManifestArchiveIndex)


def test_a_release_without_a_manifest_falls_back_to_prebuilt_files(
    tmp_path: Path,
) -> None:
    """An older release must keep serving, so a rollback needs no code rollback."""
    legacy = tmp_path / "releases" / "20260401-9237d372"
    legacy.mkdir(parents=True)

    assert isinstance(build_archive_index(legacy), LegacyDirectoryArchiveIndex)


def test_listing_shows_only_the_callers_own_archives(release_dir: Path) -> None:
    index = _index(release_dir)

    assert index.list_filenames("alice", USERS) == [
        "sing-box-v1.0.0-alice-linux-amd64.tar.gz",
        "sing-box-v1.0.0-alice-windows-amd64.zip",
    ]


def test_resolving_another_users_archive_fails_even_when_named_exactly(
    release_dir: Path,
) -> None:
    index = _index(release_dir)
    bobs = "sing-box-v1.0.0-bob-linux-amd64.tar.gz"

    assert index.resolve("bob", bobs, USERS) == bobs
    assert index.resolve("alice", bobs, USERS) is None


def test_opening_another_users_archive_raises_rather_than_assembling(
    release_dir: Path,
) -> None:
    index = _index(release_dir)

    with pytest.raises(ArchiveNotFoundError):
        index.open("alice", "sing-box-v1.0.0-bob-linux-amd64.tar.gz")


def test_resolving_by_platform_returns_that_users_archive(release_dir: Path) -> None:
    index = _index(release_dir)

    assert (
        index.resolve_for_platform("alice", "windows-amd64", USERS)
        == "sing-box-v1.0.0-alice-windows-amd64.zip"
    )
    assert index.resolve_for_platform("alice", "linux-arm64", USERS) is None


def test_an_assembled_tarball_holds_the_prefix_and_only_this_users_configs(
    release_dir: Path,
) -> None:
    index = _index(release_dir)
    archive = index.open("alice", "sing-box-v1.0.0-alice-linux-amd64.tar.gz")
    data = b"".join(archive.chunks)

    members = _tar_members(data)
    assert sorted(members) == [
        "sing-box/client-install.sh",
        "sing-box/naive-client.json",
        "sing-box/sing-box",
        "sing-box/trojan-client.json",
    ]
    assert archive.content_length == len(data)
    assert members["sing-box/client-install.sh"][0] == 0o755
    assert members["sing-box/trojan-client.json"] == (
        0o600,
        _config_bytes("alice", "trojan"),
    )


def test_an_assembled_zip_holds_the_prefix_and_only_this_users_configs(
    release_dir: Path,
) -> None:
    index = _index(release_dir)
    archive = index.open("bob", "sing-box-v1.0.0-bob-windows-amd64.zip")
    data = b"".join(archive.chunks)

    members = _zip_members(data)
    assert sorted(members) == [
        "sing-box/client-install.sh",
        "sing-box/hysteria2-client.json",
        "sing-box/naive-client.json",
        "sing-box/sing-box",
        "sing-box/trojan-client.json",
    ]
    assert archive.content_length == len(data)
    assert members["sing-box/sing-box"][0] == 0o755
    assert members["sing-box/hysteria2-client.json"] == (
        0o600,
        _config_bytes("bob", "hysteria2"),
    )


def test_a_tampered_user_config_is_refused_rather_than_served(
    release_dir: Path,
) -> None:
    """The tail bytes are read anyway, so checking them costs almost nothing."""
    index = _index(release_dir)
    (
        release_dir / "users" / "alice" / "linux-amd64" / "trojan-client.json"
    ).write_bytes(b"{}\n")

    with pytest.raises(ArchiveIntegrityError, match="does not match the digest"):
        index.open("alice", "sing-box-v1.0.0-alice-linux-amd64.tar.gz")


def test_a_member_pointing_outside_the_release_is_refused(
    release_dir: Path, tmp_path: Path
) -> None:
    """A `src` is a release-relative path, and a symlink must not widen that."""
    outside = tmp_path / "outside.json"
    payload = b'{"secret":"not mine"}\n'
    outside.write_bytes(payload)
    (release_dir / "users" / "alice" / "linux-amd64" / "escape.json").symlink_to(
        outside
    )

    manifest = Manifest.from_json(
        (release_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    entry = manifest.archive_by_filename("sing-box-v1.0.0-alice-linux-amd64.tar.gz")
    assert entry is not None
    # A correct digest, so the path check is what has to reject this.
    escaping = ArchiveEntry(
        filename=entry.filename,
        username=entry.username,
        platform=entry.platform,
        archive_format=entry.archive_format,
        prefix_id=entry.prefix_id,
        tail_members=(
            packing.member_for_bytes(
                "sing-box/escape.json",
                0o600,
                payload,
                "users/alice/linux-amd64/escape.json",
            ),
        ),
    )
    tampered = Manifest(
        upstream_version=manifest.upstream_version,
        prefixes=manifest.prefixes,
        archives=(escaping,),
    )

    with pytest.raises(ArchiveIntegrityError, match="outside the release directory"):
        ManifestArchiveIndex(release_dir, tampered).open(
            "alice", "sing-box-v1.0.0-alice-linux-amd64.tar.gz"
        )


def test_the_legacy_backend_serves_a_prebuilt_file_unchanged(tmp_path: Path) -> None:
    legacy = tmp_path / "releases" / "20260401-9237d372"
    legacy.mkdir(parents=True)
    payload = b"prebuilt archive bytes" * 100
    (legacy / "sing-box-v1.0.0-alice-linux-amd64.tar.gz").write_bytes(payload)

    index = LegacyDirectoryArchiveIndex(legacy)
    archive = index.open("alice", "sing-box-v1.0.0-alice-linux-amd64.tar.gz")

    assert archive.content_length == len(payload)
    assert b"".join(archive.chunks) == payload


def test_the_legacy_backend_reports_a_missing_file(tmp_path: Path) -> None:
    legacy = tmp_path / "releases" / "20260401-9237d372"
    legacy.mkdir(parents=True)

    with pytest.raises(ArchiveNotFoundError):
        LegacyDirectoryArchiveIndex(legacy).open("alice", "missing.tar.gz")
