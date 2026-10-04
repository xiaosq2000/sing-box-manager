"""Tests for reclaiming space from old releases and the shared store.

The failure that matters here is deleting a blob a retained release still
points at. That would be silent -- the release directory would still look
complete, and only a download would discover the missing prefix -- so the
tests below check what survives, not just what goes away.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sing_box_manager.release import artifacts
from sing_box_manager.release.artifacts import ReleaseInfo, write_release_info
from sing_box_manager.release.gc import GarbageCollectionError, collect_garbage
from sing_box_manager.release.manifest import (
    MANIFEST_FILENAME,
    TAR_GZ_FORMAT,
    ArchiveEntry,
    Manifest,
    Member,
    Prefix,
)
from sing_box_manager.release.store import STORE_DIR_NAME, Store
from sing_box_manager.settings import Settings


def _prefix(seed: str) -> Prefix:
    """A prefix whose id is a stable function of `seed`."""
    member = Member(
        arcname="sing-box/sing-box",
        mode=0o755,
        size=len(seed),
        sha256=seed[0] * 64,
    )
    from sing_box_manager.release.store import compute_prefix_id

    return Prefix(
        prefix_id=compute_prefix_id(TAR_GZ_FORMAT, [member]),
        platform="linux-amd64",
        archive_format=TAR_GZ_FORMAT,
        uncompressed_size=1024,
        entry_count=0,
        members=(member,),
    )


def _write_release(
    releases_root: Path,
    seed: str,
    *,
    upstream_version: str = "1.0.0",
    payload_digest: str | None = None,
    mtime: int = 0,
) -> tuple[Path, Prefix]:
    prefix = _prefix(seed)
    manifest = Manifest(
        upstream_version=upstream_version,
        prefixes=(prefix,),
        archives=(
            ArchiveEntry(
                filename="sing-box-v1.0.0-alice-linux-amd64.tar.gz",
                username="alice",
                platform="linux-amd64",
                archive_format=TAR_GZ_FORMAT,
                prefix_id=prefix.prefix_id,
                tail_members=(),
            ),
        ),
        payload_digests=() if payload_digest is None else (payload_digest,),
    )
    release_dir = releases_root / f"rel-{manifest.release_id[:12]}"
    release_dir.mkdir(parents=True, exist_ok=True)
    (release_dir / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")
    (release_dir / "filler").write_bytes(b"x" * 1000)

    store = Store(releases_root / STORE_DIR_NAME)
    store.prefix_dir.mkdir(parents=True, exist_ok=True)
    (store.prefix_dir / f"{prefix.prefix_id}.tar.gz.part").write_bytes(b"blob")
    (store.prefix_dir / f"{prefix.prefix_id}.json").write_text("{}", encoding="utf-8")
    if payload_digest is not None:
        payload = store.payload_dir / payload_digest
        payload.mkdir(parents=True, exist_ok=True)
        (payload / "sing-box").write_bytes(b"binary")

    if mtime:
        os.utime(release_dir, (mtime, mtime))
    return release_dir, prefix


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sing_box_version="1.0.0",
        config_root=tmp_path / "config",
        releases_root=tmp_path / "releases",
    )


def _set_active(tmp_path: Path, settings: Settings, release_dir: Path) -> None:
    write_release_info(
        Path(settings.release_info_path),
        ReleaseInfo(
            release_dir_name=release_dir.name,
            commit_sha="9237d372",
            dirty=False,
            date="20260401",
            upstream_version="1.0.0",
        ),
    )


@pytest.fixture(autouse=True)
def _project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)


def test_the_newest_releases_and_their_store_entries_survive(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    releases_root = tmp_path / "releases"
    old, old_prefix = _write_release(releases_root, "a", mtime=1_000)
    new, new_prefix = _write_release(releases_root, "b", mtime=2_000)

    result = collect_garbage(settings, keep=1)

    assert result.kept_releases == (new.name,)
    assert result.removed_releases == (old.name,)
    assert not old.exists()
    assert new.exists()

    store = Store(releases_root / STORE_DIR_NAME)
    assert (store.prefix_dir / f"{new_prefix.prefix_id}.tar.gz.part").is_file()
    assert not (store.prefix_dir / f"{old_prefix.prefix_id}.tar.gz.part").exists()


def test_the_active_release_survives_however_old_it_is(tmp_path: Path) -> None:
    """Collecting the release the portal is serving would take the portal down."""
    settings = _settings(tmp_path)
    releases_root = tmp_path / "releases"
    old, old_prefix = _write_release(releases_root, "a", mtime=1_000)
    _write_release(releases_root, "b", mtime=2_000)
    _write_release(releases_root, "c", mtime=3_000)
    _set_active(tmp_path, settings, old)

    result = collect_garbage(settings, keep=1)

    assert old.name in result.kept_releases
    assert old.exists()
    store = Store(releases_root / STORE_DIR_NAME)
    assert (store.prefix_dir / f"{old_prefix.prefix_id}.tar.gz.part").is_file()


def test_a_payload_shared_with_a_retained_release_is_not_collected(
    tmp_path: Path,
) -> None:
    """Two releases of the same upstream version point at one 58 MB binary."""
    settings = _settings(tmp_path)
    releases_root = tmp_path / "releases"
    digest = "d" * 64
    old, _ = _write_release(releases_root, "a", payload_digest=digest, mtime=1_000)
    _write_release(releases_root, "b", payload_digest=digest, mtime=2_000)

    collect_garbage(settings, keep=1)

    assert not old.exists()
    store = Store(releases_root / STORE_DIR_NAME)
    assert (store.payload_dir / digest / "sing-box").is_file()


def test_an_extracted_upstream_tree_for_a_dropped_version_is_pruned(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    releases_root = tmp_path / "releases"
    _write_release(releases_root, "a", upstream_version="1.0.0", mtime=1_000)
    _write_release(releases_root, "b", upstream_version="1.1.0", mtime=2_000)

    store = Store(releases_root / STORE_DIR_NAME)
    for version, digest in (("1.0.0", "e" * 64), ("1.1.0", "f" * 64)):
        extracted = store.upstream_dir_for("linux-amd64", digest)
        extracted.mkdir(parents=True)
        (extracted / ".upstream-version").write_text(version, encoding="utf-8")

    collect_garbage(settings, keep=1)

    assert not store.upstream_dir_for("linux-amd64", "e" * 64).exists()
    assert store.upstream_dir_for("linux-amd64", "f" * 64).is_dir()


def test_a_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    releases_root = tmp_path / "releases"
    old, old_prefix = _write_release(releases_root, "a", mtime=1_000)
    _write_release(releases_root, "b", mtime=2_000)

    result = collect_garbage(settings, keep=1, dry_run=True)

    assert result.dry_run
    assert result.removed_releases == (old.name,)
    assert f"{old_prefix.prefix_id}.tar.gz.part" in result.removed_store_entries
    assert result.reclaimed_bytes > 0
    assert old.exists()
    store = Store(releases_root / STORE_DIR_NAME)
    assert (store.prefix_dir / f"{old_prefix.prefix_id}.tar.gz.part").is_file()


def test_an_unreadable_manifest_aborts_rather_than_deleting_blind(
    tmp_path: Path,
) -> None:
    """Unreadable references are invisible references, not absent ones."""
    settings = _settings(tmp_path)
    releases_root = tmp_path / "releases"
    _write_release(releases_root, "a", mtime=1_000)
    keeper, keeper_prefix = _write_release(releases_root, "b", mtime=2_000)
    (keeper / MANIFEST_FILENAME).write_text("not json", encoding="utf-8")

    with pytest.raises(GarbageCollectionError, match="refusing to collect"):
        collect_garbage(settings, keep=1)

    store = Store(releases_root / STORE_DIR_NAME)
    assert (store.prefix_dir / f"{keeper_prefix.prefix_id}.tar.gz.part").is_file()
    assert len(list(releases_root.glob("rel-*"))) == 2


def test_keeping_fewer_than_one_release_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GarbageCollectionError, match="at least 1"):
        collect_garbage(_settings(tmp_path), keep=0)


def test_collecting_before_any_release_exists_is_not_an_error(tmp_path: Path) -> None:
    result = collect_garbage(_settings(tmp_path), keep=3)

    assert result.removed_releases == ()
    assert result.notes
