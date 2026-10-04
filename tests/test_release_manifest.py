"""Tests for the release manifest and its content-derived release id."""

from __future__ import annotations

import json

import pytest

from sing_box_manager.release.manifest import (
    MANIFEST_VERSION,
    ArchiveEntry,
    Manifest,
    ManifestError,
    Member,
    Prefix,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
PREFIX_ID = "c" * 64


def _member(arcname: str, *, mode: int = 0o644, digest: str = DIGEST_A) -> Member:
    return Member(arcname=arcname, mode=mode, size=10, sha256=digest, src="src/x")


def _prefix(*, members: tuple[Member, ...] | None = None) -> Prefix:
    return Prefix(
        prefix_id=PREFIX_ID,
        platform="linux-amd64",
        archive_format="tar.gz",
        uncompressed_size=4096,
        entry_count=0,
        members=members if members is not None else (_member("sing-box/sing-box"),),
    )


def _archive(
    *, username: str = "alice", tail: tuple[Member, ...] | None = None
) -> ArchiveEntry:
    return ArchiveEntry(
        filename=f"sing-box-v1.13.15-{username}-linux-amd64.tar.gz",
        username=username,
        platform="linux-amd64",
        archive_format="tar.gz",
        prefix_id=PREFIX_ID,
        tail_members=(
            tail
            if tail is not None
            else (_member("sing-box/trojan-client.json", mode=0o600),)
        ),
    )


def _manifest(**overrides: object) -> Manifest:
    defaults: dict[str, object] = {
        "upstream_version": "1.13.15",
        "prefixes": (_prefix(),),
        "archives": (_archive(),),
    }
    defaults.update(overrides)
    return Manifest(**defaults)  # ty: ignore[invalid-argument-type]


def test_release_id_is_stable_for_identical_content() -> None:
    assert _manifest().release_id == _manifest().release_id


def test_release_id_ignores_the_order_things_were_emitted_in() -> None:
    """Two builds must agree even if they walked users in a different order."""
    first = _manifest(archives=(_archive(username="alice"), _archive(username="bob")))
    second = _manifest(archives=(_archive(username="bob"), _archive(username="alice")))

    assert first.release_id == second.release_id


def test_release_id_changes_when_a_member_digest_changes() -> None:
    changed = _manifest(
        archives=(
            _archive(tail=(_member("sing-box/trojan-client.json", digest=DIGEST_B),)),
        )
    )

    assert changed.release_id != _manifest().release_id


def test_release_id_changes_when_only_a_mode_changes() -> None:
    """Mode is part of what ships, so it has to be part of the identity."""
    changed = _manifest(
        archives=(_archive(tail=(_member("sing-box/trojan-client.json", mode=0o644),)),)
    )

    assert changed.release_id != _manifest().release_id


def test_release_id_changes_when_a_user_is_added() -> None:
    changed = _manifest(archives=(_archive(), _archive(username="bob")))

    assert changed.release_id != _manifest().release_id


def test_manifest_round_trips_through_json() -> None:
    manifest = _manifest()

    restored = Manifest.from_json(manifest.to_json())

    assert restored == manifest
    assert restored.release_id == manifest.release_id


def test_written_manifest_records_its_release_id() -> None:
    manifest = _manifest()

    payload = json.loads(manifest.to_json())

    assert payload["release_id"] == manifest.release_id
    assert payload["manifest_version"] == MANIFEST_VERSION


def test_from_json_rejects_a_tampered_release_id() -> None:
    payload = json.loads(_manifest().to_json())
    payload["release_id"] = "f" * 64

    with pytest.raises(ManifestError, match="does not match its contents"):
        Manifest.from_json(json.dumps(payload))


def test_from_json_rejects_an_archive_pointing_at_an_unknown_prefix() -> None:
    payload = json.loads(_manifest().to_json())
    payload["archives"][0]["prefix_id"] = "d" * 64
    payload.pop("release_id")

    with pytest.raises(ManifestError, match="unknown prefix"):
        Manifest.from_json(json.dumps(payload))


def test_from_json_rejects_duplicate_arcnames() -> None:
    payload = json.loads(_manifest().to_json())
    payload["archives"][0]["tail_members"] *= 2
    payload.pop("release_id")

    with pytest.raises(ManifestError, match="duplicate arcnames"):
        Manifest.from_json(json.dumps(payload))


def test_from_json_rejects_a_malformed_digest() -> None:
    payload = json.loads(_manifest().to_json())
    payload["archives"][0]["tail_members"][0]["sha256"] = "not-a-digest"
    payload.pop("release_id")

    with pytest.raises(ManifestError, match="malformed sha256"):
        Manifest.from_json(json.dumps(payload))


def test_from_json_rejects_a_boolean_where_an_integer_belongs() -> None:
    """bool subclasses int, so a stray true here would otherwise slip through."""
    payload = json.loads(_manifest().to_json())
    payload["archives"][0]["tail_members"][0]["mode"] = True
    payload.pop("release_id")

    with pytest.raises(ManifestError, match="non-negative integer"):
        Manifest.from_json(json.dumps(payload))


def test_archives_for_user_only_returns_that_user() -> None:
    manifest = _manifest(archives=(_archive(), _archive(username="bob")))

    owned = manifest.archives_for_user("alice")

    assert [archive.username for archive in owned] == ["alice"]


def test_archive_by_filename_returns_none_when_absent() -> None:
    assert _manifest().archive_by_filename("nope.tar.gz") is None
