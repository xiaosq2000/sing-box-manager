"""Tests for the content-addressed store.

The store is what makes a release incremental: unchanged content lands on a path
that already exists, so the build skips the compression and the deploy transfers
nothing. These tests pin that behaviour down, including the cases where the
address *must* move.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from sing_box_manager.release import packing
from sing_box_manager.release.store import Store


def _sources(tmp_path: Path, *, script_body: str = "echo install\n") -> list:
    source_dir = tmp_path / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "sing-box").write_bytes(b"\x7fELF" + b"payload" * 2000)
    (source_dir / "client-install.sh").write_text(f"#!/bin/bash\n{script_body}")
    return [
        packing.SourceFile("sing-box/sing-box", 0o755, source_dir / "sing-box"),
        packing.SourceFile(
            "sing-box/client-install.sh", 0o755, source_dir / "client-install.sh"
        ),
    ]


def test_ensure_prefix_builds_once_then_reuses(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    sources = _sources(tmp_path)

    first, built_first = store.ensure_prefix("linux-amd64", "tar.gz", sources)
    second, built_second = store.ensure_prefix("linux-amd64", "tar.gz", sources)

    assert built_first is True
    assert built_second is False
    assert first.prefix_id == second.prefix_id
    assert first == second


def test_prefix_id_moves_when_content_changes(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")

    original, _ = store.ensure_prefix("linux-amd64", "tar.gz", _sources(tmp_path))
    changed, rebuilt = store.ensure_prefix(
        "linux-amd64", "tar.gz", _sources(tmp_path, script_body="echo different\n")
    )

    assert changed.prefix_id != original.prefix_id
    assert rebuilt is True
    # The old blob is still there; nothing is overwritten in place.
    assert store.blob_path(original.prefix_id, "tar.gz").is_file()
    assert store.blob_path(changed.prefix_id, "tar.gz").is_file()


def test_prefix_id_moves_when_only_a_mode_changes(tmp_path: Path) -> None:
    """A mode change alters what ships, so it has to alter the address."""
    store = Store(tmp_path / "store")
    sources = _sources(tmp_path)

    original, _ = store.ensure_prefix("linux-amd64", "tar.gz", sources)
    remoded = [
        packing.SourceFile(source.arcname, 0o644, source.path) for source in sources
    ]
    changed, _ = store.ensure_prefix("linux-amd64", "tar.gz", remoded)

    assert changed.prefix_id != original.prefix_id


def test_prefix_id_ignores_the_order_sources_were_listed_in(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    sources = _sources(tmp_path)

    forward, _ = store.ensure_prefix("linux-amd64", "tar.gz", sources)
    backward, rebuilt = store.ensure_prefix(
        "linux-amd64", "tar.gz", list(reversed(sources))
    )

    assert forward.prefix_id == backward.prefix_id
    assert rebuilt is False


def test_tar_and_zip_prefixes_are_stored_separately(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    sources = _sources(tmp_path)

    tar_prefix, _ = store.ensure_prefix("linux-amd64", "tar.gz", sources)
    zip_prefix, _ = store.ensure_prefix("windows-amd64", "zip", sources)

    assert tar_prefix.prefix_id != zip_prefix.prefix_id
    assert store.blob_path(tar_prefix.prefix_id, "tar.gz").is_file()
    assert store.blob_path(zip_prefix.prefix_id, "zip").is_file()
    assert store.central_path(zip_prefix.prefix_id).is_file()


def test_stored_prefix_assembles_into_a_readable_archive(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    prefix, _ = store.ensure_prefix("linux-amd64", "tar.gz", _sources(tmp_path))

    payload = b'{"password":"secret"}'
    tail = [
        (
            packing.member_for_bytes(
                "sing-box/trojan-client.json", 0o600, payload, "users/a/t.json"
            ),
            payload,
        )
    ]
    assembled = packing.assemble(
        prefix,
        store.blob_path(prefix.prefix_id, prefix.archive_format),
        store.central_path(prefix.prefix_id),
        tail,
    )
    data = b"".join(assembled.chunks)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        names = archive.getnames()
    assert names == [
        "sing-box/client-install.sh",
        "sing-box/sing-box",
        "sing-box/trojan-client.json",
    ]


def test_gc_removes_only_unreferenced_prefixes(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    keep, _ = store.ensure_prefix("linux-amd64", "tar.gz", _sources(tmp_path))
    drop, _ = store.ensure_prefix(
        "linux-amd64", "tar.gz", _sources(tmp_path, script_body="echo stale\n")
    )

    removed = store.gc([keep.prefix_id])

    assert store.blob_path(keep.prefix_id, "tar.gz").is_file()
    assert not store.blob_path(drop.prefix_id, "tar.gz").exists()
    assert all(drop.prefix_id in path.name for path in removed)


def test_gc_on_an_empty_store_is_a_no_op(tmp_path: Path) -> None:
    assert Store(tmp_path / "store").gc([]) == []


def test_upstream_dir_is_keyed_by_verified_digest(tmp_path: Path) -> None:
    """The digest is already checked against GitHub's, so a hit needs no recheck."""
    store = Store(tmp_path / "store")

    path = store.upstream_dir_for("linux-amd64", "d" * 64)

    assert path == store.upstream_dir / "linux-amd64" / ("d" * 64)


def test_atomic_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    store.ensure_prefix("linux-amd64", "tar.gz", _sources(tmp_path))

    assert not list(store.prefix_dir.glob("*.tmp"))
