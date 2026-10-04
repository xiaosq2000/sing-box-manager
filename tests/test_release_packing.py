"""Tests for deterministic packing and prefix/tail splice assembly.

The splice is the load-bearing trick of the release format: it is what lets the
serving host hand out per-user archives without compressing the 70 MB payload
per request. So these tests check it against the tools that actually consume the
archives -- GNU tar and unzip -- not just against Python's own readers.
"""

from __future__ import annotations

import gzip
import io
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from sing_box_manager.release import packing
from sing_box_manager.release.manifest import Member

TROJAN_CONFIG = b'{"outbounds":[{"password":"trojan-secret"}]}\n' * 40
NAIVE_CONFIG = b'{"outbounds":[{"password":"naive-secret"}]}\n' * 40
HYSTERIA2_CONFIG = b'{"outbounds":[{"password":"hy2-secret"}]}\n' * 40


def _write_sources(tmp_path: Path) -> list[packing.SourceFile]:
    source_dir = tmp_path / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "sing-box").write_bytes(b"\x7fELF" + b"binary" * 5000)
    (source_dir / "client-install.sh").write_text("#!/bin/bash\necho install\n")
    (source_dir / "ui.sh").write_text("# shared ui helpers\n")
    return [
        packing.SourceFile("sing-box/sing-box", 0o755, source_dir / "sing-box"),
        packing.SourceFile(
            "sing-box/client-install.sh", 0o755, source_dir / "client-install.sh"
        ),
        packing.SourceFile("sing-box/lib/ui.sh", 0o644, source_dir / "ui.sh"),
    ]


def _tail(*protocols: str) -> list[tuple[Member, bytes]]:
    """A per-user tail holding only the protocols that user actually has."""
    payloads = {
        "trojan": TROJAN_CONFIG,
        "naive": NAIVE_CONFIG,
        "hysteria2": HYSTERIA2_CONFIG,
    }
    members: list[tuple[Member, bytes]] = []
    for protocol in protocols:
        payload = payloads[protocol]
        members.append(
            (
                packing.member_for_bytes(
                    f"sing-box/{protocol}-client.json",
                    0o600,
                    payload,
                    f"users/alice/linux-amd64/{protocol}-client.json",
                ),
                payload,
            )
        )
    marker = f"{protocols[0]}\n".encode()
    members.append(
        (
            packing.member_for_bytes(
                "sing-box/default-protocol", 0o644, marker, "users/alice/default"
            ),
            marker,
        )
    )
    return members


def _build_tar_prefix(
    tmp_path: Path, sources: list[packing.SourceFile]
) -> tuple[Path, int]:
    blocks, length = packing.build_tar_blocks_from_files(sources)
    blob = tmp_path / "prefix.tar.gz.part"
    blob.write_bytes(gzip.compress(blocks, packing.GZIP_COMPRESS_LEVEL, mtime=0))
    return blob, length


def _build_zip_prefix(
    tmp_path: Path, sources: list[packing.SourceFile]
) -> tuple[Path, Path, int]:
    entries, central, count = packing.build_zip_parts_from_files(sources)
    blob = tmp_path / "prefix.zip.part"
    blob.write_bytes(entries)
    central_path = tmp_path / "prefix.zip.cd"
    central_path.write_bytes(central)
    return blob, central_path, count


def _reference_tar_gz(
    sources: list[packing.SourceFile], tail: list[tuple[Member, bytes]]
) -> bytes:
    """The same archive built the ordinary, unspliced way."""
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT
    ) as archive:
        for source in sources:
            info = packing._tar_info(
                source.arcname, source.mode, source.path.stat().st_size
            )
            with source.path.open("rb") as file_obj:
                archive.addfile(info, file_obj)
        for member, payload in tail:
            archive.addfile(
                packing._tar_info(member.arcname, member.mode, len(payload)),
                io.BytesIO(payload),
            )
    return buffer.getvalue()


def _members_of_tar(data: bytes) -> list[tuple[str, int, int, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        result = []
        for info in archive.getmembers():
            extracted = archive.extractfile(info)
            payload = b"" if extracted is None else extracted.read()
            result.append((info.name, info.mode, info.size, payload))
        return result


def _members_of_zip(data: bytes) -> list[tuple[str, int, int, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [
            (
                info.filename,
                (info.external_attr >> 16) & 0xFFFF,
                info.file_size,
                archive.read(info.filename),
            )
            for info in archive.infolist()
        ]


def test_spliced_tar_gz_matches_an_unspliced_archive(tmp_path: Path) -> None:
    sources = _write_sources(tmp_path)
    tail = _tail("trojan", "naive")
    blob, length = _build_tar_prefix(tmp_path, sources)

    assembled = packing.assemble_tar_gz(blob, length, tail)
    data = b"".join(assembled.chunks)

    assert _members_of_tar(data) == _members_of_tar(_reference_tar_gz(sources, tail))


def test_spliced_zip_matches_an_unspliced_archive(tmp_path: Path) -> None:
    sources = _write_sources(tmp_path)
    tail = _tail("trojan", "naive")
    blob, central, count = _build_zip_prefix(tmp_path, sources)

    assembled = packing.assemble_zip(blob, central, count, tail)
    data = b"".join(assembled.chunks)

    expected = [
        ("sing-box/sing-box", 0o755, sources[0].path.stat().st_size),
        ("sing-box/client-install.sh", 0o755, sources[1].path.stat().st_size),
        ("sing-box/lib/ui.sh", 0o644, sources[2].path.stat().st_size),
        ("sing-box/trojan-client.json", 0o600, len(TROJAN_CONFIG)),
        ("sing-box/naive-client.json", 0o600, len(NAIVE_CONFIG)),
        ("sing-box/default-protocol", 0o644, len(b"trojan\n")),
    ]
    assert [entry[:3] for entry in _members_of_zip(data)] == expected


@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_content_length_is_exact_before_any_byte_is_sent(
    tmp_path: Path, archive_format: str
) -> None:
    """Exactness is what keeps real Content-Length and range requests working."""
    sources = _write_sources(tmp_path)
    tail = _tail("trojan")

    if archive_format == "tar.gz":
        blob, length = _build_tar_prefix(tmp_path, sources)
        assembled = packing.assemble_tar_gz(blob, length, tail)
    else:
        blob, central, count = _build_zip_prefix(tmp_path, sources)
        assembled = packing.assemble_zip(blob, central, count, tail)

    assert assembled.content_length == len(b"".join(assembled.chunks))


@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_ranged_reads_match_the_whole_archive(
    tmp_path: Path, archive_format: str
) -> None:
    """Resume has to land on the same bytes, including across segment seams.

    A zip assembles from four segments and a tar.gz from two, so the boundaries
    a range can fall on are exactly where an off-by-one would hide.
    """
    sources = _write_sources(tmp_path)
    tail = _tail("trojan", "naive")

    if archive_format == "tar.gz":
        blob, length = _build_tar_prefix(tmp_path, sources)
        assembled = packing.assemble_tar_gz(blob, length, tail)
    else:
        blob, central, count = _build_zip_prefix(tmp_path, sources)
        assembled = packing.assemble_zip(blob, central, count, tail)

    whole = b"".join(assembled.chunks)
    total = len(whole)

    # Every segment seam, plus one byte either side of it.
    seams = []
    offset = 0
    for segment in assembled.segments:
        offset += segment.size
        seams.extend([offset - 1, offset, offset + 1])
    boundaries = sorted(
        {0, 1, total // 3, total // 2, total - 1, total}
        | {seam for seam in seams if 0 <= seam <= total}
    )

    for start in boundaries:
        for stop in boundaries:
            if stop <= start:
                continue
            assert b"".join(assembled.read(start, stop)) == whole[start:stop], (
                f"range {start}-{stop} diverged"
            )


def test_prefix_blobs_are_byte_identical_across_builds(tmp_path: Path) -> None:
    """The prefix id is a content hash, so unchanged inputs must not move."""
    sources = _write_sources(tmp_path)

    first_blocks, first_length = packing.build_tar_blocks_from_files(sources)
    second_blocks, second_length = packing.build_tar_blocks_from_files(sources)
    assert first_blocks == second_blocks
    assert first_length == second_length

    first_zip = packing.build_zip_parts_from_files(sources)
    second_zip = packing.build_zip_parts_from_files(sources)
    assert first_zip == second_zip


def test_modes_come_from_the_manifest_not_the_filesystem(tmp_path: Path) -> None:
    """By assembly time the files have crossed rsync under another umask."""
    sources = _write_sources(tmp_path)
    for source in sources:
        source.path.chmod(0o600)

    blob, length = _build_tar_prefix(tmp_path, sources)
    data = b"".join(packing.assemble_tar_gz(blob, length, _tail("trojan")).chunks)

    modes = {name: mode for name, mode, _, _ in _members_of_tar(data)}
    assert modes["sing-box/sing-box"] == 0o755
    assert modes["sing-box/client-install.sh"] == 0o755
    assert modes["sing-box/lib/ui.sh"] == 0o644


def test_tail_member_set_varies_per_user(tmp_path: Path) -> None:
    """Most users have only two protocols; their archive must reflect that."""
    sources = _write_sources(tmp_path)
    blob, length = _build_tar_prefix(tmp_path, sources)

    two = b"".join(
        packing.assemble_tar_gz(blob, length, _tail("trojan", "naive")).chunks
    )
    three = b"".join(
        packing.assemble_tar_gz(
            blob, length, _tail("trojan", "naive", "hysteria2")
        ).chunks
    )

    two_names = {name for name, _, _, _ in _members_of_tar(two)}
    three_names = {name for name, _, _, _ in _members_of_tar(three)}

    assert "sing-box/hysteria2-client.json" not in two_names
    assert "sing-box/hysteria2-client.json" in three_names
    # The shared prefix is untouched by the difference.
    assert "sing-box/sing-box" in two_names & three_names


def test_tail_payloads_survive_the_splice_intact(tmp_path: Path) -> None:
    sources = _write_sources(tmp_path)
    blob, length = _build_tar_prefix(tmp_path, sources)

    data = b"".join(
        packing.assemble_tar_gz(blob, length, _tail("trojan", "naive")).chunks
    )
    payloads = {name: payload for name, _, _, payload in _members_of_tar(data)}

    assert payloads["sing-box/trojan-client.json"] == TROJAN_CONFIG
    assert payloads["sing-box/naive-client.json"] == NAIVE_CONFIG
    assert payloads["sing-box/sing-box"] == sources[0].path.read_bytes()


@pytest.mark.skipif(shutil.which("tar") is None, reason="tar is not installed")
def test_gnu_tar_accepts_the_spliced_archive(tmp_path: Path) -> None:
    """Python's tarfile is more forgiving than the tar the clients actually run."""
    sources = _write_sources(tmp_path)
    blob, length = _build_tar_prefix(tmp_path, sources)
    archive_path = tmp_path / "client.tar.gz"
    archive_path.write_bytes(
        b"".join(packing.assemble_tar_gz(blob, length, _tail("trojan", "naive")).chunks)
    )

    listed = subprocess.run(
        ["tar", "-tzf", str(archive_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert listed.returncode == 0
    # A warning on stderr still exits 0; the clients would surface it as noise.
    assert listed.stderr == ""

    destination = tmp_path / "extracted"
    destination.mkdir()
    extracted = subprocess.run(
        ["tar", "-xzf", str(archive_path), "-C", str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert extracted.returncode == 0
    assert extracted.stderr == ""
    assert (destination / "sing-box" / "sing-box").stat().st_mode & 0o777 == 0o755
    assert (
        destination / "sing-box" / "trojan-client.json"
    ).stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(shutil.which("unzip") is None, reason="unzip is not installed")
def test_unzip_accepts_the_spliced_archive(tmp_path: Path) -> None:
    sources = _write_sources(tmp_path)
    blob, central, count = _build_zip_prefix(tmp_path, sources)
    archive_path = tmp_path / "client.zip"
    archive_path.write_bytes(
        b"".join(packing.assemble_zip(blob, central, count, _tail("trojan")).chunks)
    )

    tested = subprocess.run(
        ["unzip", "-t", str(archive_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tested.returncode == 0
    assert "No errors detected" in tested.stdout


def test_tar_stream_is_padded_to_a_record_boundary(tmp_path: Path) -> None:
    """Padding is computed over the concatenation, not the tail alone."""
    sources = _write_sources(tmp_path)
    blob, length = _build_tar_prefix(tmp_path, sources)

    data = b"".join(packing.assemble_tar_gz(blob, length, _tail("trojan")).chunks)
    inflated = gzip.decompress(data)

    assert len(inflated) % packing.TAR_RECORD_SIZE == 0
    assert inflated.endswith(b"\0" * packing.TAR_TERMINATOR_SIZE)


def test_zip_rejects_an_offset_that_would_need_zip64() -> None:
    """Silently emitting a broken zip would be far worse than failing."""
    central = packing.build_zip_parts_from_bytes(
        [(packing.member_for_bytes("sing-box/x", 0o644, b"payload", "x"), b"payload")]
    )[1]

    with pytest.raises(packing.PackingError, match="zip64"):
        packing._shift_central_directory(central, packing.ZIP_MAX_OFFSET)
