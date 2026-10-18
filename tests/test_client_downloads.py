"""The files sbc downloads, their signed manifest, and how the portal serves them."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from sing_box_manager.release import builder as builder_module
from sing_box_manager.release.artifacts import ReleaseInfo
from sing_box_manager.release.builder import ReleaseBuilder
from sing_box_manager.release.client_downloads import (
    build_sbc,
    client_manifest,
    generate_key,
    load_client_downloads,
    public_key,
    sbc_version,
    sign,
)
from sing_box_manager.release.digest_cache import sha256_for_path, write_cached_digest
from sing_box_manager.release.manifest import MANIFEST_FILENAME, Manifest, Member
from sing_box_manager.release.platforms import MIXED_PROFILE, Platform

ROOT = Path(__file__).resolve().parents[1]
KEY = base64.b64encode(bytes(range(32))).decode()


def _verify(key: str, payload: bytes, signature: str) -> None:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key(key))).verify(
        base64.b64decode(signature), payload
    )


def test_signatures_verify_with_the_public_key_and_repeat_exactly() -> None:
    payload = b'{"files":[]}\n'
    signature = sign(KEY, payload)
    _verify(KEY, payload, signature)
    assert sign(KEY, payload) == signature
    with pytest.raises(InvalidSignature):
        _verify(KEY, payload + b" ", signature)
    assert len(base64.b64decode(generate_key())) == 32


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("not base64!", "must be base64"),
        (base64.b64encode(b"short").decode(), "32 bytes"),
    ],
)
def test_malformed_keys_are_rejected(key: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        sign(key, b"payload")


def test_manifest_lists_files_in_path_order() -> None:
    manifest = json.loads(
        client_manifest(
            [("sbc/linux-amd64/sbc", 3, "b" * 64), ("rules/a.srs", 1, "a" * 64)],
            sbc="0123456789ab",
            sing_box="1.14.2",
        )
    )
    assert manifest == {
        "version": 1,
        "sbc": "0123456789ab",
        "sing_box": "1.14.2",
        "files": [
            {"path": "rules/a.srs", "size": 1, "sha256": "a" * 64},
            {"path": "sbc/linux-amd64/sbc", "size": 3, "sha256": "b" * 64},
        ],
    }


def test_sbc_version_names_the_commit() -> None:
    info = ReleaseInfo(
        release_dir_name="rel-x",
        commit_sha="0123456789abcdef",
        dirty=False,
        date="20260928",
        upstream_version="1.14.2",
    )
    assert sbc_version(info) == "0123456789ab"
    assert sbc_version(dataclasses.replace(info, dirty=True)) == "0123456789ab-dirty"


def test_build_sbc_embeds_the_version(tmp_path: Path) -> None:
    output = tmp_path / "sbc"
    build_sbc(ROOT, "linux-amd64", "fixture-version", output)
    result = subprocess.run([str(output), "version"], capture_output=True, text=True)
    assert result.stdout == "sbc fixture-version\n"


def test_manifest_without_client_files_keeps_its_old_id() -> None:
    manifest = Manifest(upstream_version="1.14.2", prefixes=(), archives=())
    assert "client_members" not in manifest.canonical_json()
    member = Member(
        arcname="manifest.json",
        mode=0o644,
        size=1,
        sha256="c" * 64,
        src="client/manifest.json",
    )
    with_client = dataclasses.replace(manifest, client_members=(member,))
    assert with_client.release_id != manifest.release_id
    assert Manifest.from_json(with_client.to_json()).client_members == (member,)


def test_the_release_signs_every_client_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_release_builder import _build_settings

    settings = _build_settings(tmp_path).model_copy(update={"client_signing_key": KEY})
    builder = ReleaseBuilder(settings)
    linux = Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(MIXED_PROFILE,),
        extras=[],
        protocol_extras={},
    )
    monkeypatch.setattr(builder_module, "PLATFORMS", [linux])
    archive = tmp_path / "cache" / "sing-box-1.0.0-linux-amd64.tar.gz"
    archive.parent.mkdir()
    archive.write_bytes(b"upstream archive")
    write_cached_digest(archive, sha256_for_path(archive))
    builder._download_paths = {"linux-amd64": archive}
    rules = builder._staging_dir / "rules"
    rules.mkdir(parents=True)
    (rules / "abc.srs").write_bytes(b"rules")
    (rules / "files.txt").write_text("abc.srs\n")
    builder._rule_snapshot_paths = [rules / "abc.srs", rules / "files.txt"]

    members = {member.arcname: member for member in builder._build_client_downloads()}

    assert set(members) == {
        "sbc/linux-amd64/sbc",
        "sing-box/sing-box-1.0.0-linux-amd64.tar.gz",
        "rules/abc.srs",
        "manifest.json",
        "manifest.json.sig",
    }
    staging = builder._staging_dir
    manifest_bytes = (staging / members["manifest.json"].src).read_bytes()
    signature = (staging / members["manifest.json.sig"].src).read_text().strip()
    _verify(KEY, manifest_bytes, signature)
    listed = {entry["path"]: entry for entry in json.loads(manifest_bytes)["files"]}
    assert set(listed) == set(members) - {"manifest.json", "manifest.json.sig"}
    for path, entry in listed.items():
        served = staging / members[path].src
        assert entry["sha256"] == sha256_for_path(served)
        assert entry["size"] == served.stat().st_size
    archive_link = staging / members["sing-box/sing-box-1.0.0-linux-amd64.tar.gz"].src
    assert archive_link.is_symlink()
    assert sha256_for_path(archive) in builder._payload_digests


def test_a_release_without_a_key_has_no_client_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_release_builder import _build_settings

    builder = ReleaseBuilder(_build_settings(tmp_path))
    with pytest.raises(RuntimeError, match="client_signing_key is required"):
        builder._build_client_downloads()


def test_a_release_names_its_client_build_by_the_client_manifest(
    tmp_path: Path,
) -> None:
    client = b'{"sbc": "abc", "files": []}\n'
    (tmp_path / "client").mkdir()
    (tmp_path / "client" / "manifest.json").write_bytes(client)
    member = Member(
        arcname="manifest.json",
        mode=0o644,
        size=len(client),
        sha256=hashlib.sha256(client).hexdigest(),
        src="client/manifest.json",
    )
    manifest = Manifest(
        upstream_version="1.14.2", prefixes=(), archives=(), client_members=(member,)
    )
    (tmp_path / MANIFEST_FILENAME).write_text(manifest.to_json())

    downloads = load_client_downloads(tmp_path)

    assert downloads is not None
    assert downloads.sbc_version == "abc"
    assert downloads.build_id == hashlib.sha256(client).hexdigest()
