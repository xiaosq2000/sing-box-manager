"""The release manifest: the authoritative description of every archive.

A client archive is a *shared prefix* -- the sing-box binary, the client scripts,
the docs -- plus a *tail* of a few kilobytes of rendered per-user config. The
prefix is byte-identical for every user on a platform and is compressed once at
build time; the tail is compressed per request. The manifest is what ties the two
together, and it is the only place that says which members an archive contains.

That last point matters: a user's archive holds only the ``<protocol>-client.json``
files that user actually has, so the member set genuinely varies per user and
cannot be inferred from the platform alone.

Member modes are recorded here and are authoritative. They are never re-derived
from the filesystem, because by assembly time the files have crossed rsync and
may carry a different umask than the machine that built them.

The release id is a hash of this manifest with the id itself elided -- that is, a
hash of the *outputs*. Hashing inputs instead would miss real dependencies: the
renderers inline TLS PEMs read from absolute paths outside the repo, and configs
also depend on inventory fields that no file-content hash would cover. Rendering
everything and hashing the result makes "same release id implies bit-identical
archives" a property of the build rather than a promise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from sing_box_manager.release.digest_cache import is_sha256_hex, sha256_for_text

MANIFEST_VERSION = 2
MANIFEST_FILENAME = "manifest.json"
ARCHIVE_ROOT = "sing-box"

# The on-disk shape of a release directory. Here rather than in the builder so
# that the builder, the deployer and the portal all agree on it without any of
# them having to import the others.
RELEASE_DIR_PREFIX = "rel-"
RELEASE_ID_LENGTH = 12
USERS_DIR_NAME = "users"
COMMON_DIR_NAME = "common"
SERVER_DIR_NAME = "server"

TAR_GZ_FORMAT = "tar.gz"
ZIP_FORMAT = "zip"
ARCHIVE_FORMATS = frozenset({TAR_GZ_FORMAT, ZIP_FORMAT})


class ManifestError(ValueError):
    """Raised when a manifest is malformed or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class Member:
    """One file inside an assembled archive."""

    arcname: str
    mode: int
    size: int
    sha256: str
    src: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "arcname": self.arcname,
            "mode": self.mode,
            "size": self.size,
            "sha256": self.sha256,
        }
        if self.src:
            payload["src"] = self.src
        return payload

    @classmethod
    def from_dict(cls, value: object) -> Member:
        payload = _expect_mapping(value, "member")
        arcname = _expect_str(payload.get("arcname"), "member.arcname")
        digest = _expect_str(payload.get("sha256"), "member.sha256")
        if not is_sha256_hex(digest):
            raise ManifestError(f"member {arcname} has a malformed sha256")

        src = payload.get("src", "")
        if not isinstance(src, str):
            raise ManifestError(f"member {arcname} has a non-string src")

        return cls(
            arcname=arcname,
            mode=_expect_int(payload.get("mode"), "member.mode"),
            size=_expect_int(payload.get("size"), "member.size"),
            sha256=digest,
            src=src,
        )


@dataclass(frozen=True, slots=True)
class Prefix:
    """A shared, precompressed archive prefix identified by its content."""

    prefix_id: str
    platform: str
    archive_format: str
    # tar only: uncompressed length of the prefix blocks, needed to pad the
    # concatenated stream to a record boundary.
    uncompressed_size: int
    # zip only: how many central-directory entries the template holds.
    entry_count: int
    members: tuple[Member, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "prefix_id": self.prefix_id,
            "platform": self.platform,
            "archive_format": self.archive_format,
            "uncompressed_size": self.uncompressed_size,
            "entry_count": self.entry_count,
            "members": [member.to_dict() for member in self.members],
        }

    @classmethod
    def from_dict(cls, value: object) -> Prefix:
        payload = _expect_mapping(value, "prefix")
        prefix_id = _expect_str(payload.get("prefix_id"), "prefix.prefix_id")
        if not is_sha256_hex(prefix_id):
            raise ManifestError(f"prefix {prefix_id} has a malformed id")

        archive_format = _expect_str(
            payload.get("archive_format"), "prefix.archive_format"
        )
        if archive_format not in ARCHIVE_FORMATS:
            raise ManifestError(f"prefix {prefix_id} has unknown format")

        return cls(
            prefix_id=prefix_id,
            platform=_expect_str(payload.get("platform"), "prefix.platform"),
            archive_format=archive_format,
            uncompressed_size=_expect_int(
                payload.get("uncompressed_size"), "prefix.uncompressed_size"
            ),
            entry_count=_expect_int(payload.get("entry_count"), "prefix.entry_count"),
            members=_members_from(payload.get("members"), "prefix.members"),
        )


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One downloadable archive: a shared prefix plus this user's tail."""

    filename: str
    username: str
    platform: str
    archive_format: str
    prefix_id: str
    tail_members: tuple[Member, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "username": self.username,
            "platform": self.platform,
            "archive_format": self.archive_format,
            "prefix_id": self.prefix_id,
            "tail_members": [member.to_dict() for member in self.tail_members],
        }

    @classmethod
    def from_dict(cls, value: object) -> ArchiveEntry:
        payload = _expect_mapping(value, "archive")
        filename = _expect_str(payload.get("filename"), "archive.filename")
        archive_format = _expect_str(
            payload.get("archive_format"), "archive.archive_format"
        )
        if archive_format not in ARCHIVE_FORMATS:
            raise ManifestError(f"archive {filename} has unknown format")

        return cls(
            filename=filename,
            username=_expect_str(payload.get("username"), "archive.username"),
            platform=_expect_str(payload.get("platform"), "archive.platform"),
            archive_format=archive_format,
            prefix_id=_expect_str(payload.get("prefix_id"), "archive.prefix_id"),
            tail_members=_members_from(
                payload.get("tail_members"), "archive.tail_members"
            ),
        )


@dataclass(frozen=True, slots=True)
class Manifest:
    """Everything the portal needs to serve a release."""

    upstream_version: str
    prefixes: tuple[Prefix, ...]
    archives: tuple[ArchiveEntry, ...]
    # The server package is part of the release, so it belongs in the identity
    # too: a rendered server config changing must move the release id.
    server_members: tuple[Member, ...] = ()
    # Store payloads this release points at, so gc can tell "unreferenced" from
    # "not in the newest release" without walking symlinks.
    payload_digests: tuple[str, ...] = ()
    # Files sbc downloads, by the path it asks for. `src` is relative to the
    # release directory.
    client_members: tuple[Member, ...] = ()
    manifest_version: int = MANIFEST_VERSION

    @property
    def release_id(self) -> str:
        """Content hash of this manifest, and therefore of the release."""
        return sha256_for_text(self.canonical_json())

    def canonical_json(self) -> str:
        """Serialize deterministically, without the derived release id."""
        return json.dumps(
            self._payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def to_json(self) -> str:
        """Serialize for disk, including the derived release id."""
        payload = self._payload()
        payload["release_id"] = self.release_id
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def prefix_by_id(self, prefix_id: str) -> Prefix:
        for prefix in self.prefixes:
            if prefix.prefix_id == prefix_id:
                return prefix
        raise ManifestError(f"manifest has no prefix {prefix_id}")

    def archive_by_filename(self, filename: str) -> ArchiveEntry | None:
        for archive in self.archives:
            if archive.filename == filename:
                return archive
        return None

    def archives_for_user(self, username: str) -> tuple[ArchiveEntry, ...]:
        return tuple(
            archive for archive in self.archives if archive.username == username
        )

    def _payload(self) -> dict[str, object]:
        # Sorted so two builds that produce the same content also produce the
        # same bytes, whatever order the builder happened to emit them in.
        return {
            "manifest_version": self.manifest_version,
            "upstream_version": self.upstream_version,
            "prefixes": [
                prefix.to_dict()
                for prefix in sorted(self.prefixes, key=lambda item: item.prefix_id)
            ],
            "archives": [
                archive.to_dict()
                for archive in sorted(self.archives, key=lambda item: item.filename)
            ],
            "server_members": [
                member.to_dict()
                for member in sorted(self.server_members, key=lambda item: item.arcname)
            ],
            "payload_digests": sorted(self.payload_digests),
            # Left out when empty, so a release without client downloads keeps
            # the id it had before they existed.
            **(
                {
                    "client_members": [
                        member.to_dict()
                        for member in sorted(
                            self.client_members, key=lambda item: item.arcname
                        )
                    ]
                }
                if self.client_members
                else {}
            ),
        }

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        payload = _expect_mapping(json.loads(text), "manifest")
        manifest_version = _expect_int(
            payload.get("manifest_version"), "manifest.manifest_version"
        )
        prefixes = payload.get("prefixes")
        archives = payload.get("archives")
        if not isinstance(prefixes, list) or not isinstance(archives, list):
            raise ManifestError("manifest prefixes and archives must be lists")

        raw_digests = payload.get("payload_digests", [])
        if not isinstance(raw_digests, list) or not all(
            isinstance(item, str) for item in raw_digests
        ):
            raise ManifestError("manifest payload_digests must be a list of strings")

        manifest = cls(
            upstream_version=_expect_str(
                payload.get("upstream_version"), "manifest.upstream_version"
            ),
            prefixes=tuple(Prefix.from_dict(item) for item in prefixes),
            archives=tuple(ArchiveEntry.from_dict(item) for item in archives),
            server_members=_members_from(
                payload.get("server_members", []), "manifest.server_members"
            ),
            payload_digests=tuple(cast("list[str]", raw_digests)),
            client_members=_members_from(
                payload.get("client_members", []), "manifest.client_members"
            ),
            manifest_version=manifest_version,
        )

        recorded_id = payload.get("release_id")
        if isinstance(recorded_id, str) and recorded_id != manifest.release_id:
            raise ManifestError(
                "manifest release_id does not match its contents "
                f"(recorded {recorded_id}, computed {manifest.release_id})"
            )

        known_prefix_ids = {prefix.prefix_id for prefix in manifest.prefixes}
        for archive in manifest.archives:
            if archive.prefix_id not in known_prefix_ids:
                raise ManifestError(
                    f"archive {archive.filename} references unknown prefix "
                    f"{archive.prefix_id}"
                )

        return manifest


def _expect_mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ManifestError(f"{context} must be a mapping")
    return cast("dict[str, object]", value)


def _expect_str(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{context} must be a non-empty string")
    return value


def _expect_int(value: object, context: str) -> int:
    # bool is an int subclass; a stray true/false here would be a silent bug.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestError(f"{context} must be a non-negative integer")
    return value


def _members_from(value: object, context: str) -> tuple[Member, ...]:
    if not isinstance(value, list):
        raise ManifestError(f"{context} must be a list")
    members = tuple(Member.from_dict(cast("Any", item)) for item in value)
    arcnames = [member.arcname for member in members]
    if len(set(arcnames)) != len(arcnames):
        raise ManifestError(f"{context} contains duplicate arcnames")
    return members
