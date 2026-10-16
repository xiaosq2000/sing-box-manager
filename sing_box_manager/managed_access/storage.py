"""Host ownership fence. It survives application-tree replacement."""

import os
import stat
from pathlib import Path
from uuid import UUID

from sing_box_manager.account_copy.files import read_source
from sing_box_manager.control_plane.import_schema import canonical

STATE_ROOT = Path("/var/lib/sing-box-manager-control-plane")
FENCE = STATE_ROOT / "owner.json"


def _marker_stat(path: Path):
    # A missing child below a symlink or non-directory is not safe absence.
    # Keep the standalone deploy/installer probes and their shared tests aligned.
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("unsafe_ownership_path")
    for parent in reversed(path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("unsafe_ownership_path")
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def legacy_blocked() -> bool:
    try:
        return _marker_stat(FENCE) is not None
    except OSError:
        return True


def require_fence(identity: UUID) -> None:
    from sing_box_manager.managed_access.errors import AccessRejected

    try:
        raw = read_source(FENCE)
    except Exception:
        raise AccessRejected("host_ownership_fence_required") from None
    expected = {"version": 1, "identity": str(identity)}
    if raw != canonical(expected) + b"\n":
        raise AccessRejected("host_ownership_fence_required")


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
