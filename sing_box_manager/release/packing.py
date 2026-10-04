"""Deterministic archive writing, and splice assembly of prefix + tail.

Every client archive is ~99.98% shared bytes. Building one archive per user per
platform therefore compresses the same 70 MB once per user, ships it once per
user, and stores it once per user. Instead:

    prefix  the sing-box binary, client scripts and docs -- identical for every
            user on a platform, compressed ONCE at build time on the build host
    tail    that user's rendered ``<protocol>-client.json`` files and the
            ``default-protocol`` marker -- a few KB, compressed per request

The two are concatenated at download time. This works because gzip members
concatenate (``gzip(A) || gzip(B)`` inflates to ``A || B``) and a tar file is a
run of 512-byte blocks ended by two zero blocks; and because a zip stores each
entry independently, so putting the invariant entries first keeps their local
header offsets fixed and only the central directory has to be rebuilt.

The point is the serving host. It does no bulk compression -- it copies a blob it
was handed and deflates a few KB, which is milliseconds even on a slow VPS.

Determinism is not a nicety here: the prefix id is a content hash, so two builds
of unchanged inputs must produce byte-identical blobs or nothing is cacheable.
Hence fixed mtimes, zeroed ownership, and modes taken from the manifest rather
than from the filesystem.
"""

from __future__ import annotations

import gzip
import io
import struct
import tarfile
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from sing_box_manager.release.digest_cache import sha256_for_bytes, sha256_for_path
from sing_box_manager.release.manifest import (
    TAR_GZ_FORMAT,
    ZIP_FORMAT,
    Member,
    Prefix,
)

TAR_BLOCK_SIZE = 512
TAR_RECORD_SIZE = 10240
TAR_TERMINATOR_SIZE = TAR_BLOCK_SIZE * 2
GZIP_COMPRESS_LEVEL = 9
ZIP_COMPRESS_LEVEL = 9
FIXED_MTIME = 0
FIXED_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
# 3 == Unix, so the mode stored in external_attr is honoured on extraction. Set
# explicitly rather than inherited from the building platform.
ZIP_CREATE_SYSTEM_UNIX = 3
STREAM_CHUNK_SIZE = 256 * 1024

ZIP_EOCD_SIGNATURE = 0x06054B50
ZIP_EOCD_SIZE = 22
ZIP_CENTRAL_HEADER_SIZE = 46
# 0xFFFF / 0xFFFFFFFF are the zip64 sentinels meaning "read the real value from
# the zip64 extra field", so they are not themselves representable inline.
ZIP_MAX_ENTRIES = 0xFFFF
ZIP_MAX_OFFSET = 0xFFFFFFFF


class PackingError(RuntimeError):
    """Raised when an archive cannot be built or assembled."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A file on disk destined for an archive, with its authoritative mode."""

    arcname: str
    mode: int
    path: Path


def _tar_info(arcname: str, mode: int, size: int) -> tarfile.TarInfo:
    """A tar header with every non-content field pinned to a constant."""
    info = tarfile.TarInfo(arcname)
    info.size = size
    info.mode = mode
    info.mtime = FIXED_MTIME
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def build_tar_blocks_from_files(sources: Sequence[SourceFile]) -> tuple[bytes, int]:
    """Serialize files as tar blocks, without the end-of-archive terminator.

    Returns the blocks and their length. The length is read off the buffer
    before ``close()`` appends the terminator, which is both exact and immune to
    GNU long-name headers that a hand-rolled size calculation would miss.
    """
    buffer = io.BytesIO()
    archive = tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT)
    for source in sources:
        size = source.path.stat().st_size
        with source.path.open("rb") as file_obj:
            archive.addfile(_tar_info(source.arcname, source.mode, size), file_obj)
    content_length = buffer.tell()
    archive.close()
    return buffer.getvalue()[:content_length], content_length


def build_tar_blocks_from_bytes(
    members: Sequence[tuple[Member, bytes]],
) -> tuple[bytes, int]:
    """Serialize in-memory members as tar blocks, without the terminator."""
    buffer = io.BytesIO()
    archive = tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT)
    for member, payload in members:
        archive.addfile(
            _tar_info(member.arcname, member.mode, len(payload)),
            io.BytesIO(payload),
        )
    content_length = buffer.tell()
    archive.close()
    return buffer.getvalue()[:content_length], content_length


def tar_terminator(total_content_length: int) -> bytes:
    """Two zero blocks plus padding out to a full tar record.

    The padding is computed over the *concatenated* stream, not the tail alone,
    which is why the prefix's uncompressed length is recorded in the manifest.
    """
    unpadded = total_content_length + TAR_TERMINATOR_SIZE
    padding = -unpadded % TAR_RECORD_SIZE
    return b"\0" * (TAR_TERMINATOR_SIZE + padding)


def _zip_info(arcname: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname, date_time=FIXED_ZIP_DATE_TIME)
    info.external_attr = (mode & 0xFFFF) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = ZIP_CREATE_SYSTEM_UNIX
    return info


def _split_zip(raw: bytes) -> tuple[bytes, bytes, int]:
    """Split a complete zip into (entries, central directory, entry count)."""
    eocd_start = raw.rfind(struct.pack("<I", ZIP_EOCD_SIGNATURE))
    if eocd_start < 0:
        raise PackingError("built zip has no end-of-central-directory record")

    (
        _signature,
        _disk,
        _cd_disk,
        entries_here,
        _total_entries,
        _cd_size,
        cd_offset,
        _comment_length,
    ) = struct.unpack("<IHHHHIIH", raw[eocd_start : eocd_start + ZIP_EOCD_SIZE])

    return raw[:cd_offset], raw[cd_offset:eocd_start], entries_here


def build_zip_parts_from_files(
    sources: Sequence[SourceFile],
) -> tuple[bytes, bytes, int]:
    """Build the invariant zip prefix: entry bytes, central-directory template.

    The entries land at offset 0 and stay there, so the template's recorded
    offsets never need patching -- only the tail's do.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=ZIP_COMPRESS_LEVEL
    ) as archive:
        for source in sources:
            info = _zip_info(source.arcname, source.mode)
            info.file_size = source.path.stat().st_size
            with (
                source.path.open("rb") as file_obj,
                archive.open(info, "w") as target,
            ):
                while chunk := file_obj.read(STREAM_CHUNK_SIZE):
                    target.write(chunk)

    return _split_zip(buffer.getvalue())


def build_zip_parts_from_bytes(
    members: Sequence[tuple[Member, bytes]],
) -> tuple[bytes, bytes, int]:
    """Build the per-user zip tail: entry bytes, central-directory entries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=ZIP_COMPRESS_LEVEL
    ) as archive:
        for member, payload in members:
            archive.writestr(_zip_info(member.arcname, member.mode), payload)

    return _split_zip(buffer.getvalue())


def _shift_central_directory(central: bytes, shift: int) -> bytes:
    """Add ``shift`` to every local-header offset in a central directory."""
    shifted = bytearray()
    position = 0
    while position < len(central):
        name_length, extra_length, comment_length = struct.unpack(
            "<HHH", central[position + 28 : position + 34]
        )
        end = (
            position
            + ZIP_CENTRAL_HEADER_SIZE
            + name_length
            + extra_length
            + comment_length
        )
        if end > len(central):
            raise PackingError("central directory entry runs past the buffer")

        entry = bytearray(central[position:end])
        (offset,) = struct.unpack("<I", entry[42:46])
        new_offset = offset + shift
        if new_offset >= ZIP_MAX_OFFSET:
            raise PackingError("zip offset would overflow; zip64 is not supported")
        entry[42:46] = struct.pack("<I", new_offset)
        shifted += entry
        position = end

    return bytes(shifted)


def _zip_eocd(entry_count: int, central_size: int, central_offset: int) -> bytes:
    if entry_count >= ZIP_MAX_ENTRIES or central_offset >= ZIP_MAX_OFFSET:
        raise PackingError("zip is too large for the classic format; need zip64")
    return struct.pack(
        "<IHHHHIIH",
        ZIP_EOCD_SIGNATURE,
        0,
        0,
        entry_count,
        entry_count,
        central_size,
        central_offset,
        0,
    )


def _stream_file(path: Path) -> Iterator[bytes]:
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(STREAM_CHUNK_SIZE):
            yield chunk


@dataclass(frozen=True, slots=True)
class Segment:
    """One contiguous run of an assembled archive.

    Either a file on disk (the precompressed shared prefix) or bytes in memory
    (the per-user tail). Keeping the two addressable rather than collapsing
    them into one iterator is what makes range requests possible.
    """

    size: int
    path: Path | None = None
    data: bytes | None = None

    def read(self, start: int, stop: int) -> Iterator[bytes]:
        """Yield this segment's bytes in ``[start, stop)``."""
        if self.data is not None:
            yield self.data[start:stop]
            return
        if self.path is None:
            raise PackingError("segment has neither a path nor data")

        with self.path.open("rb") as file_obj:
            file_obj.seek(start)
            remaining = stop - start
            while remaining > 0:
                chunk = file_obj.read(min(STREAM_CHUNK_SIZE, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk


@dataclass(frozen=True, slots=True)
class AssembledArchive:
    """A ready-to-serve archive: exact length plus lazily read bytes.

    The length is exact and known before a single byte is sent, which is what
    lets the portal keep a real ``Content-Length`` and range requests instead of
    falling back to chunked encoding. That is not cosmetic: the hosted installer
    runs `curl --fail --silent`, which exits 0 on a truncated chunked response,
    so the failure would resurface later as a misattributed tar error.
    """

    segments: tuple[Segment, ...]

    @property
    def content_length(self) -> int:
        return sum(segment.size for segment in self.segments)

    def read(self, start: int = 0, stop: int | None = None) -> Iterator[bytes]:
        """Yield the archive's bytes in ``[start, stop)``."""
        end = self.content_length if stop is None else stop
        offset = 0
        for segment in self.segments:
            segment_start = offset
            segment_stop = offset + segment.size
            offset = segment_stop
            if segment_stop <= start or segment_start >= end:
                continue
            yield from segment.read(
                max(0, start - segment_start),
                min(segment.size, end - segment_start),
            )

    @property
    def chunks(self) -> Iterator[bytes]:
        """The whole archive, in order."""
        return self.read()


def assemble_tar_gz(
    prefix_blob: Path,
    prefix_uncompressed_size: int,
    tail: Sequence[tuple[Member, bytes]],
) -> AssembledArchive:
    """Splice a precompressed tar prefix with a freshly compressed tail."""
    blocks, blocks_length = build_tar_blocks_from_bytes(tail)
    terminator = tar_terminator(prefix_uncompressed_size + blocks_length)
    tail_blob = gzip.compress(blocks + terminator, GZIP_COMPRESS_LEVEL, mtime=0)

    return AssembledArchive(
        (
            Segment(size=prefix_blob.stat().st_size, path=prefix_blob),
            Segment(size=len(tail_blob), data=tail_blob),
        )
    )


def assemble_zip(
    prefix_blob: Path,
    prefix_central: Path,
    prefix_entry_count: int,
    tail: Sequence[tuple[Member, bytes]],
) -> AssembledArchive:
    """Splice a prebuilt zip prefix with a freshly deflated tail."""
    tail_entries, tail_central, tail_entry_count = build_zip_parts_from_bytes(tail)

    prefix_size = prefix_blob.stat().st_size
    prefix_central_bytes = prefix_central.read_bytes()

    central = prefix_central_bytes + _shift_central_directory(tail_central, prefix_size)
    entry_count = prefix_entry_count + tail_entry_count
    central_offset = prefix_size + len(tail_entries)
    eocd = _zip_eocd(entry_count, len(central), central_offset)

    return AssembledArchive(
        (
            Segment(size=prefix_size, path=prefix_blob),
            Segment(size=len(tail_entries), data=tail_entries),
            Segment(size=len(central), data=central),
            Segment(size=len(eocd), data=eocd),
        )
    )


def archive_from_file(path: Path) -> AssembledArchive:
    """Present an already-built archive file through the same interface.

    Lets a release built before the content-addressed layout be served by
    exactly the same code path as an assembled one.
    """
    return AssembledArchive((Segment(size=path.stat().st_size, path=path),))


def members_for_sources(sources: Sequence[SourceFile]) -> tuple[Member, ...]:
    """Describe source files as manifest members, hashing their contents."""
    return tuple(
        Member(
            arcname=source.arcname,
            mode=source.mode,
            size=source.path.stat().st_size,
            sha256=sha256_for_path(source.path),
        )
        for source in sources
    )


def member_for_bytes(arcname: str, mode: int, payload: bytes, src: str) -> Member:
    """Describe an in-memory payload as a manifest member."""
    return Member(
        arcname=arcname,
        mode=mode,
        size=len(payload),
        sha256=sha256_for_bytes(payload),
        src=src,
    )


def assemble(
    prefix: Prefix,
    prefix_blob: Path,
    prefix_central: Path,
    tail: Sequence[tuple[Member, bytes]],
) -> AssembledArchive:
    """Assemble an archive in whichever format its prefix was built for."""
    if prefix.archive_format == TAR_GZ_FORMAT:
        return assemble_tar_gz(prefix_blob, prefix.uncompressed_size, tail)
    if prefix.archive_format == ZIP_FORMAT:
        return assemble_zip(prefix_blob, prefix_central, prefix.entry_count, tail)
    raise PackingError(f"unknown archive format {prefix.archive_format}")
