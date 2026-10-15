"""Private, bounded files. Never return parser errors containing input values."""

import errno
import json
import os
import secrets
import stat
from pathlib import Path

from sing_box_manager.control_plane.import_schema import ImportRejected

MAX_BYTES = 8 * 1024 * 1024


def _open_checked(path: Path | str, flags: int, *, directory: int | None = None) -> int:
    try:
        return os.open(path, flags, dir_fd=directory)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ImportRejected("unsafe_input_path") from None
        raise


def private_directory(path: Path) -> int:
    descriptor = _open_checked(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(descriptor)
        raise ImportRejected("unsafe_directory")
    return descriptor


def read_private(path: Path) -> bytes:
    directory = private_directory(path.parent)
    try:
        return read_private_at(directory, path.name)
    finally:
        os.close(directory)


def read_private_at(directory: int, name: str) -> bytes:
    """Read a basename relative to an already checked private directory."""
    if Path(name).name != name or name in {".", ".."}:
        raise ImportRejected("unsafe_input_path")
    descriptor = _open_checked(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, directory=directory
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > MAX_BYTES
        ):
            raise ImportRejected("unsafe_input_file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ImportRejected("input_too_large")
        return content
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportRejected("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ImportRejected("invalid_json_constant")


def decode_json(content: bytes) -> object:
    try:
        return json.loads(
            content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ImportRejected("invalid_json") from None


def publish_private(path: Path, content: bytes) -> None:
    directory = private_directory(path.parent)
    try:
        publish_private_at(directory, path.name, content)
    finally:
        os.close(directory)


def publish_private_at(directory: int, name: str, content: bytes) -> None:
    """Publish a basename relative to an already checked private directory."""
    if Path(name).name != name or name in {".", ".."}:
        raise ImportRejected("unsafe_input_path")
    temporary = f".import-report-{secrets.token_hex(12)}.tmp"
    created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publication without replacing an existing file or symlink.
        os.link(
            temporary,
            name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        os.fsync(directory)
    except FileExistsError:
        raise ImportRejected("report_output_exists") from None
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory)
    os.fsync(directory)
