"""Synthetic host-marker checks. No service or runtime configuration is used."""

import shlex
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from sing_box_manager.control_plane.import_schema import canonical
from sing_box_manager.managed_access import deployment, storage
from sing_box_manager.web.app import create_app

IDENTITY = UUID("00000000-0000-4000-8000-000000000123")


def test_missing_marker_does_not_block_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "FENCE", tmp_path / "absent" / "owner.json")
    assert not storage.legacy_blocked()


@pytest.mark.parametrize("kind", ["partial", "symlink", "directory"])
def test_any_marker_blocks_legacy_before_config_access(tmp_path, monkeypatch, kind):
    marker = tmp_path / "owner.json"
    monkeypatch.setattr(storage, "FENCE", marker)
    if kind == "partial":
        marker.write_bytes(b'{"state":"fencing"}\n')
        marker.chmod(0o640)
    elif kind == "symlink":
        marker.symlink_to(tmp_path / "absent")
    else:
        marker.mkdir()
    assert storage.legacy_blocked()
    with pytest.raises(RuntimeError, match="legacy portal is disabled"):
        create_app(None)  # No configuration is accessed before this gate.
    with pytest.raises(ValueError, match="host_ownership_fence_required"):
        storage.require_fence(IDENTITY)


def test_marker_requires_exact_canonical_binding(tmp_path, monkeypatch):
    marker = tmp_path / "owner.json"
    monkeypatch.setattr(storage, "FENCE", marker)
    expected = canonical({"version": 1, "identity": str(IDENTITY)}) + b"\n"
    marker.write_bytes(expected)
    marker.chmod(0o640)
    storage.require_fence(IDENTITY)
    assert storage.legacy_blocked()
    for rejected in (expected[:-1], b" " + expected, expected.replace(b":1", b":true")):
        marker.write_bytes(rejected)
        with pytest.raises(ValueError, match="host_ownership_fence_required"):
            storage.require_fence(IDENTITY)
    marker.write_bytes(expected)
    with pytest.raises(ValueError, match="host_ownership_fence_required"):
        storage.require_fence(UUID("00000000-0000-4000-8000-000000000124"))


@pytest.mark.parametrize(
    "kind", ["dangling-parent", "symlink-parent", "file-parent", "dangling-grandparent"]
)
@pytest.mark.parametrize("guard", ["python", "deploy", "installer"])
def test_unsafe_marker_ancestors_fail_closed(tmp_path, monkeypatch, kind, guard):
    root = tmp_path / "sandbox"
    parent = root / "var/lib/sing-box-manager-control-plane"
    parent.parent.mkdir(parents=True)
    if kind == "file-parent":
        parent.write_text("synthetic not a directory")
    elif kind == "dangling-grandparent":
        parent.parent.rmdir()
        parent.parent.symlink_to(tmp_path / "absent")
    else:
        target = tmp_path / "target"
        if kind == "symlink-parent":
            target.mkdir()
        parent.symlink_to(target)
    marker = parent / "owner.json"
    if guard == "python":
        monkeypatch.setattr(storage, "FENCE", marker)
        assert storage.legacy_blocked()
    elif guard == "deploy":
        monkeypatch.setattr(deployment, "FENCE", str(marker))
        argv = shlex.split(deployment.guard_command(managed=False))
        assert argv.pop(0) == "sudo"
        result = subprocess.run(argv, capture_output=True, timeout=5)
        assert result.returncode != 0
    else:
        source = Path("scripts/server-install.sh").read_text()
        fragment = source[
            source.index('fence="${root}/var/lib/') : source.index(
                'prefix="${root}/usr/local"'
            )
        ]
        # Execute only the guard, without privilege checks, installation, or services.
        result = subprocess.run(
            [
                "bash",
                "-c",
                "root=" + shlex.quote(str(root)) + "\nerror() { :; }\n" + fragment,
            ],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode != 0


def test_unreadable_marker_fails_closed(monkeypatch):
    class Unreadable(type(Path())):
        def lstat(self):
            raise PermissionError("synthetic denied metadata")

    monkeypatch.setattr(storage, "FENCE", Unreadable("/synthetic/owner.json"))
    assert storage.legacy_blocked()
