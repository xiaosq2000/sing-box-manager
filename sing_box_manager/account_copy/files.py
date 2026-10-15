"""Protected source reads and private handoff files; no source writes or repair."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path

from sing_box_manager.control_plane import import_files
from sing_box_manager.control_plane.import_schema import ImportRejected


@contextmanager
def directory(path: Path, *, private: bool = False):
    path = path.absolute()
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in {".", ".."}:
                raise ImportRejected("unsafe_input_path")
            new = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = new
            info = os.fstat(descriptor)
            mode = stat.S_IMODE(info.st_mode)
            sticky_root = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
            if info.st_uid not in {0, os.geteuid()} or (
                mode & 0o022 and not sticky_root
            ):
                raise ImportRejected("unsafe_directory")
        info = os.fstat(descriptor)
        if private and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ImportRejected("unsafe_directory")
        yield descriptor
    finally:
        os.close(descriptor)


def read_source(path: Path) -> bytes:
    with directory(path.parent) as parent:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o640}
                or info.st_nlink != 1
                or info.st_size > import_files.MAX_BYTES
            ):
                raise ImportRejected("unsafe_source_file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read(import_files.MAX_BYTES + 1)
            after = os.fstat(descriptor)
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if len(content) > import_files.MAX_BYTES or any(
                getattr(info, field) != getattr(other, field)
                for field in fields
                for other in (after, current)
            ):
                raise ImportRejected("source_changed")
            return content
        finally:
            os.close(descriptor)


def read_private(path: Path) -> bytes:
    with directory(path.parent, private=True) as parent:
        return import_files.read_private_at(parent, path.name)


def publish_private(path: Path, content: bytes) -> None:
    if len(content) > import_files.MAX_BYTES:
        raise ImportRejected("input_too_large")
    with directory(path.parent, private=True) as parent:
        import_files.publish_private_at(parent, path.name, content)
