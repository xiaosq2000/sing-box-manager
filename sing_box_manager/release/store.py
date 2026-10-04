"""The content-addressed store for shared release payloads.

Everything here is named after a hash of its own contents, which buys three
things at once:

* a rebuild of unchanged inputs lands on the path that already exists, so the
  build skips the work and the deploy transfers nothing;
* two releases that share a payload share one copy of it on both hosts;
* a release directory can be deleted without reasoning about what else might
  still be pointing at the bytes inside it -- that is what ``gc`` is for.

The store is append-only during a build. Nothing is ever overwritten in place,
so a crashed build leaves at worst an unreferenced entry, never a corrupt one.

``prefix/`` holds the precompressed shared archive prefixes and ``payload/`` the
large files a release links to rather than copies. Both are deployed.
``upstream/`` holds extracted upstream release assets and is a *build input*: it
never leaves the build host.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

from sing_box_manager.release.digest_cache import sha256_for_path, sha256_for_text
from sing_box_manager.release.manifest import (
    TAR_GZ_FORMAT,
    ZIP_FORMAT,
    Member,
    Prefix,
)
from sing_box_manager.release.packing import (
    GZIP_COMPRESS_LEVEL,
    PackingError,
    SourceFile,
    build_tar_blocks_from_files,
    build_zip_parts_from_files,
    members_for_sources,
)

STORE_DIR_NAME = "store"
PREFIX_DIR_NAME = "prefix"
PAYLOAD_DIR_NAME = "payload"
UPSTREAM_DIR_NAME = "upstream"

TAR_BLOB_SUFFIX = ".tar.gz.part"
ZIP_BLOB_SUFFIX = ".zip.part"
ZIP_CENTRAL_SUFFIX = ".zip.cd"
METADATA_SUFFIX = ".json"
UPSTREAM_VERSION_MARKER = ".upstream-version"


def compute_prefix_id(archive_format: str, members: Sequence[Member]) -> str:
    """Hash a prefix's format and member list into its content address.

    The platform is deliberately absent: the payload bytes already differ per
    platform, and leaving it out means anything that genuinely is identical
    gets stored once.
    """
    payload = json.dumps(
        {
            "archive_format": archive_format,
            "members": [member.to_dict() for member in members],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256_for_text(payload)


class Store:
    """Paths and write operations for one content-addressed store root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def prefix_dir(self) -> Path:
        return self._root / PREFIX_DIR_NAME

    @property
    def payload_dir(self) -> Path:
        return self._root / PAYLOAD_DIR_NAME

    @property
    def upstream_dir(self) -> Path:
        return self._root / UPSTREAM_DIR_NAME

    def ensure_payload(self, source: Path, name: str) -> tuple[Path, str]:
        """Adopt a large file into the store under its own content hash.

        The server package needs the sing-box binary at a real path, but that
        binary is ~58 MB and changes only with the upstream version. Storing it
        by content and pointing at it keeps it off the wire on every release
        that merely adds a user or rotates a password.
        """
        digest = sha256_for_path(source)
        target = self.payload_dir / digest / name
        if target.is_file():
            return target, digest

        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f"{name}.tmp")
        try:
            # copy2 so the executable bit survives; the binary needs it.
            shutil.copy2(source, staged)
            os.replace(staged, target)
        except OSError:
            staged.unlink(missing_ok=True)
            raise

        return target, digest

    def upstream_dir_for(self, platform: str, asset_digest: str) -> Path:
        """Where an upstream asset is extracted, keyed by its verified digest.

        Because the digest has already been checked against the one GitHub
        published, an existing directory here needs no re-verification and the
        extraction can be skipped outright.
        """
        return self.upstream_dir / platform / asset_digest

    def blob_path(self, prefix_id: str, archive_format: str) -> Path:
        suffix = TAR_BLOB_SUFFIX if archive_format == TAR_GZ_FORMAT else ZIP_BLOB_SUFFIX
        return self.prefix_dir / f"{prefix_id}{suffix}"

    def central_path(self, prefix_id: str) -> Path:
        return self.prefix_dir / f"{prefix_id}{ZIP_CENTRAL_SUFFIX}"

    def _metadata_path(self, prefix_id: str) -> Path:
        return self.prefix_dir / f"{prefix_id}{METADATA_SUFFIX}"

    def ensure_prefix(
        self,
        platform: str,
        archive_format: str,
        sources: Sequence[SourceFile],
    ) -> tuple[Prefix, bool]:
        """Build the shared prefix for a platform, or reuse the stored one.

        Returns the prefix record and whether anything had to be built. The
        expensive part -- compressing ~70 MB at level 9 -- happens at most once
        per platform per release, and not at all when the content is unchanged.
        """
        ordered = sorted(sources, key=lambda source: source.arcname)
        members = members_for_sources(ordered)
        prefix_id = compute_prefix_id(archive_format, members)

        blob_path = self.blob_path(prefix_id, archive_format)
        central_path = self.central_path(prefix_id)
        metadata_path = self._metadata_path(prefix_id)

        if blob_path.is_file() and metadata_path.is_file():
            stored = json.loads(metadata_path.read_text(encoding="utf-8"))
            return (
                Prefix(
                    prefix_id=prefix_id,
                    platform=platform,
                    archive_format=archive_format,
                    uncompressed_size=int(stored["uncompressed_size"]),
                    entry_count=int(stored["entry_count"]),
                    members=members,
                ),
                False,
            )

        self.prefix_dir.mkdir(parents=True, exist_ok=True)

        if archive_format == TAR_GZ_FORMAT:
            blocks, uncompressed_size = build_tar_blocks_from_files(ordered)
            _atomic_write(
                blob_path, gzip.compress(blocks, GZIP_COMPRESS_LEVEL, mtime=0)
            )
            entry_count = 0
        elif archive_format == ZIP_FORMAT:
            entries, central, entry_count = build_zip_parts_from_files(ordered)
            _atomic_write(blob_path, entries)
            _atomic_write(central_path, central)
            uncompressed_size = 0
        else:
            raise PackingError(f"unknown archive format {archive_format}")

        _atomic_write(
            metadata_path,
            (
                json.dumps(
                    {
                        "uncompressed_size": uncompressed_size,
                        "entry_count": entry_count,
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )

        return (
            Prefix(
                prefix_id=prefix_id,
                platform=platform,
                archive_format=archive_format,
                uncompressed_size=uncompressed_size,
                entry_count=entry_count,
                members=members,
            ),
            True,
        )

    def gc(
        self,
        referenced_prefix_ids: Iterable[str],
        referenced_payload_digests: Iterable[str] = (),
    ) -> list[Path]:
        """Delete store entries no retained release points at.

        Deliberately a separate operation rather than an rsync ``--delete``:
        the store is shared across releases, so "not in this release" and
        "unreferenced" are different questions and only the second one is safe
        to act on.
        """
        removed: list[Path] = []

        keep_prefixes = set(referenced_prefix_ids)
        if self.prefix_dir.is_dir():
            for path in sorted(self.prefix_dir.iterdir()):
                if not path.is_file():
                    continue
                if prefix_id_of(path.name) in keep_prefixes:
                    continue
                path.unlink()
                removed.append(path)

        keep_payloads = set(referenced_payload_digests)
        if self.payload_dir.is_dir():
            for path in sorted(self.payload_dir.iterdir()):
                if not path.is_dir() or path.name in keep_payloads:
                    continue
                shutil.rmtree(path)
                removed.append(path)

        return removed

    def prune_upstream(self, keep_versions: Iterable[str]) -> list[Path]:
        """Drop extracted upstream trees for versions no longer in play."""
        if not self.upstream_dir.is_dir():
            return []

        keep = set(keep_versions)
        removed: list[Path] = []
        for platform_dir in sorted(self.upstream_dir.iterdir()):
            if not platform_dir.is_dir():
                continue
            for digest_dir in sorted(platform_dir.iterdir()):
                marker = digest_dir / UPSTREAM_VERSION_MARKER
                if not marker.is_file():
                    continue
                if marker.read_text(encoding="utf-8").strip() in keep:
                    continue
                shutil.rmtree(digest_dir)
                removed.append(digest_dir)

        return removed


def prefix_id_of(filename: str) -> str:
    """Recover the prefix id a store filename belongs to."""
    for suffix in (
        TAR_BLOB_SUFFIX,
        ZIP_BLOB_SUFFIX,
        ZIP_CENTRAL_SUFFIX,
        METADATA_SUFFIX,
    ):
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write via a sibling temp file so a crash cannot leave a partial entry.

    A truncated file under a content-addressed name is the one failure this
    store must never produce: nothing downstream would ever look at it twice.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.tmp")
    try:
        staged.write_bytes(payload)
        os.replace(staged, path)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
