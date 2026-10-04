"""How the portal finds and serves the archives a release can hand out.

Two backends behind one interface:

* ``ManifestArchiveIndex`` reads the manifest and assembles an archive on
  demand -- a byte copy of the shared prefix plus a few KB of freshly
  compressed per-user config.
* ``LegacyDirectoryArchiveIndex`` serves prebuilt archive files off disk.

The legacy backend exists so a release built before the content-addressed
layout keeps serving, and so a release can be rolled back without also rolling
back the code. Both return the same range-addressable object, so the route
layer never has to care which is in play.

Nothing here decides *whether* a caller may have a file. Authorisation happens
in the routes, against the same username matching the legacy layout used, and
must run before anything is assembled.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from sing_box_manager.release.artifacts import (
    list_user_archives,
    resolve_user_archive,
    resolve_user_archive_for_platform,
)
from sing_box_manager.release.digest_cache import sha256_for_bytes
from sing_box_manager.release.manifest import (
    MANIFEST_FILENAME,
    ArchiveEntry,
    Manifest,
    Member,
)
from sing_box_manager.release.packing import (
    AssembledArchive,
    archive_from_file,
    assemble,
)
from sing_box_manager.release.store import STORE_DIR_NAME, Store


class ArchiveNotFoundError(LookupError):
    """Raised when a release cannot produce the requested archive."""


class ArchiveIntegrityError(RuntimeError):
    """Raised when a stored file no longer matches the digest recorded for it."""


class ArchiveIndex:
    """What the routes need from a release, whatever its on-disk layout."""

    def list_filenames(
        self, username: str, known_usernames: Iterable[str]
    ) -> list[str]:
        raise NotImplementedError

    def resolve(
        self, username: str, filename: str, known_usernames: Iterable[str]
    ) -> str | None:
        raise NotImplementedError

    def resolve_for_platform(
        self, username: str, platform: str, known_usernames: Iterable[str]
    ) -> str | None:
        raise NotImplementedError

    def open(self, username: str, filename: str) -> AssembledArchive:
        raise NotImplementedError


class LegacyDirectoryArchiveIndex(ArchiveIndex):
    """Serve prebuilt archive files from a pre-manifest release directory."""

    def __init__(self, release_dir: Path) -> None:
        self._release_dir = release_dir

    def list_filenames(
        self, username: str, known_usernames: Iterable[str]
    ) -> list[str]:
        return [
            path.name
            for path in list_user_archives(
                self._release_dir, username, list(known_usernames)
            )
        ]

    def resolve(
        self, username: str, filename: str, known_usernames: Iterable[str]
    ) -> str | None:
        path = resolve_user_archive(
            self._release_dir, username, filename, list(known_usernames)
        )
        return None if path is None else path.name

    def resolve_for_platform(
        self, username: str, platform: str, known_usernames: Iterable[str]
    ) -> str | None:
        path = resolve_user_archive_for_platform(
            self._release_dir, username, platform, list(known_usernames)
        )
        return None if path is None else path.name

    def open(self, username: str, filename: str) -> AssembledArchive:
        path = self._release_dir / filename
        if not path.is_file():
            raise ArchiveNotFoundError(filename)
        return archive_from_file(path)


class ManifestArchiveIndex(ArchiveIndex):
    """Assemble archives on demand from the shared store plus per-user config."""

    def __init__(self, release_dir: Path, manifest: Manifest) -> None:
        self._release_dir = release_dir
        self._manifest = manifest
        self._store = Store(release_dir.parent / STORE_DIR_NAME)

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def list_filenames(
        self, username: str, known_usernames: Iterable[str]
    ) -> list[str]:
        return sorted(
            entry.filename for entry in self._manifest.archives_for_user(username)
        )

    def resolve(
        self, username: str, filename: str, known_usernames: Iterable[str]
    ) -> str | None:
        entry = self._manifest.archive_by_filename(filename)
        # The manifest is the authority on who owns what; a name that parses as
        # someone else's must not resolve even if the caller guessed it.
        if entry is None or entry.username != username:
            return None
        return entry.filename

    def resolve_for_platform(
        self, username: str, platform: str, known_usernames: Iterable[str]
    ) -> str | None:
        for entry in self._manifest.archives_for_user(username):
            if entry.platform == platform:
                return entry.filename
        return None

    def open(self, username: str, filename: str) -> AssembledArchive:
        entry = self._manifest.archive_by_filename(filename)
        if entry is None or entry.username != username:
            raise ArchiveNotFoundError(filename)
        return assemble(
            self._manifest.prefix_by_id(entry.prefix_id),
            self._store.blob_path(entry.prefix_id, entry.archive_format),
            self._store.central_path(entry.prefix_id),
            self._read_tail(entry),
        )

    def _read_tail(self, entry: ArchiveEntry) -> list[tuple[Member, bytes]]:
        """Read this user's config, checking it still matches the manifest.

        The bytes are being read anyway, so verifying them is nearly free. The
        shared prefix is deliberately *not* re-hashed per request: that would
        put tens of megabytes of hashing back into every download, which is the
        cost this whole design exists to remove.
        """
        tail: list[tuple[Member, bytes]] = []
        for member in entry.tail_members:
            source = self._release_dir / member.src
            resolved = source.resolve()
            if not resolved.is_relative_to(self._release_dir.resolve()):
                raise ArchiveIntegrityError(
                    f"{member.src} resolves outside the release directory"
                )
            payload = source.read_bytes()
            if sha256_for_bytes(payload) != member.sha256:
                raise ArchiveIntegrityError(
                    f"{member.src} does not match the digest recorded for it"
                )
            tail.append((member, payload))
        return tail


def build_archive_index(release_dir: Path) -> ArchiveIndex:
    """Pick the backend that matches what is actually in the release directory."""
    manifest_path = release_dir / MANIFEST_FILENAME
    if manifest_path.is_file():
        return ManifestArchiveIndex(
            release_dir,
            Manifest.from_json(manifest_path.read_text(encoding="utf-8")),
        )
    return LegacyDirectoryArchiveIndex(release_dir)
