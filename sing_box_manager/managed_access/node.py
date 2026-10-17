"""Explicit root-run, single-node application. Retain plans and uncertain outcomes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import UUID, uuid4

from sing_box_manager.account_copy.files import (
    directory,
    publish_private,
    read_private,
    read_source,
)
from sing_box_manager.control_plane.import_parity import inventory_from_records
from sing_box_manager.control_plane.import_schema import AccessRecords, canonical
from sing_box_manager.managed_access import storage
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.settings import PROJECT_ROOT, Settings

BINARY = Path("/usr/local/bin/sing-box")
CONFIG = Path("/usr/local/etc/sing-box/config.json")
UNIT = Path("/etc/systemd/system/sing-box.service")
NODE_STATE = Path("/var/lib/sing-box")
SERVICE = "sing-box.service"
LEGACY_SERVICE = "sing-box-manager-web.service"
OUTPUT_LIMIT = 8 * 1024 * 1024


class NodeRejectedError(ValueError):
    """Fixed errors only. Process output and configuration remain private."""


NodeRejected = NodeRejectedError


def _run(argv, *, payload=None, timeout=30, cwd=None):
    # The private request fits in one pipe write. Bound output while reading it.
    if payload is not None and len(payload) > 4096:
        raise NodeRejected("private_request_too_large")
    with subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        cwd=cwd,
    ) as process:
        try:
            if payload is not None:
                if process.stdin is None:
                    raise NodeRejected("private_pipe_required")
                process.stdin.write(payload)
                process.stdin.close()
            output = _output(process, timeout)
            return subprocess.CompletedProcess(argv, process.wait(), output, b"")
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def _output(process, timeout):
    deadline = time.monotonic() + timeout
    output = bytearray()
    with selectors.DefaultSelector() as selected:
        selected.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selected.select(remaining):
                raise NodeRejected("node_command_timeout")
            block = os.read(
                process.stdout.fileno(), min(65536, OUTPUT_LIMIT + 1 - len(output))
            )
            if not block:
                break
            output.extend(block)
            if len(output) > OUTPUT_LIMIT:
                raise NodeRejected("node_command_output_limit")
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise NodeRejected("node_command_timeout") from None
    return bytes(output)


def _rpc(runtime: Settings, action: str, **payload) -> dict:
    result = _run(
        [
            "/usr/sbin/runuser",
            "-u",
            "sbm",
            "--",
            "/usr/bin/env",
            "-i",
            "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONDONTWRITEBYTECODE=1",
            "PYTHONNOUSERSITE=1",
            f"SBM_CONFIG={runtime.require_config_path()}",
            sys.executable,
            "-m",
            "sing_box_manager.managed_access.ipc",
        ],
        payload=canonical({"action": action, **payload}),
        cwd=PROJECT_ROOT,
    )
    if result.returncode or len(result.stdout) > OUTPUT_LIMIT:
        raise NodeRejected("database_operation_failed")
    return json.loads(result.stdout)


def _root_file(path: Path) -> str:
    with directory(path.parent):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != 0
                or before.st_mode & 0o022
            ):
                raise NodeRejected("unsafe_root_file")
            digest = hashlib.sha256()
            while block := os.read(fd, 65536):
                digest.update(block)
            after = os.fstat(fd)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise NodeRejected("root_file_changed")
            return digest.hexdigest()
        finally:
            os.close(fd)


@contextmanager
def _locked():
    if os.geteuid() != 0:
        raise NodeRejected("root_required")
    with directory(PROJECT_ROOT):
        pass
    group = pwd.getpwnam("sbm").pw_gid
    if not storage.STATE_ROOT.exists():
        storage.STATE_ROOT.mkdir(mode=0o750)
        os.chown(storage.STATE_ROOT, 0, group)
        # Root operators can use umask 077. Set the new directory's exact mode.
        storage.STATE_ROOT.chmod(0o750, follow_symlinks=False)
        storage.sync_directory(storage.STATE_ROOT.parent)
    with directory(storage.STATE_ROOT):
        pass
    info = storage.STATE_ROOT.stat()
    if info.st_uid != 0 or info.st_gid != group or stat.S_IMODE(info.st_mode) != 0o750:
        raise NodeRejected("unsafe_state_directory")
    fd = os.open(
        storage.STATE_ROOT / "node.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise NodeRejected("unsafe_node_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _unit_guard() -> None:
    if _root_file(UNIT) != _root_file(PROJECT_ROOT / "scripts/sing-box.service"):
        raise NodeRejected("unexpected_node_unit")
    result = _run(
        [
            "systemctl",
            "show",
            SERVICE,
            "--property=FragmentPath,DropInPaths,NeedDaemonReload",
        ]
    )
    if result.returncode:
        raise NodeRejected("node_unit_unavailable")
    fields = dict(line.split("=", 1) for line in result.stdout.decode().splitlines())
    if fields != {
        "FragmentPath": str(UNIT),
        "DropInPaths": "",
        "NeedDaemonReload": "no",
    }:
        raise NodeRejected("unexpected_loaded_unit")


def _check(binary: Path, config: Path) -> None:
    result = _run([str(binary), "check", "-D", str(NODE_STATE), "-c", str(config)])
    if result.returncode:
        raise NodeRejected("node_config_rejected")


def _publish(target: Path, source: Path, mode: int) -> None:
    # Source and target ancestors are checked before this helper is used.
    temporary = target.parent / ("." + target.name + "." + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
        shutil.copyfileobj(incoming, output)
        output.flush()
        os.fchmod(output.fileno(), mode)
        os.fsync(output.fileno())
    os.replace(temporary, target)
    storage.sync_directory(target.parent)


def _status() -> dict:
    result = _run(
        [
            "systemctl",
            "show",
            SERVICE,
            "--property=ActiveState,MainPID,InvocationID,NRestarts",
        ]
    )
    if result.returncode:
        raise NodeRejected("node_status_failed")
    return dict(line.split("=", 1) for line in result.stdout.decode().splitlines())


def _running() -> dict:
    first = _status()
    if first.get("ActiveState") != "active" or int(first.get("MainPID", "0")) < 1:
        raise NodeRejected("node_not_active")
    pid = first["MainPID"]
    if Path(f"/proc/{pid}/exe").readlink() != BINARY:
        raise NodeRejected("unexpected_node_process")
    expected = [str(BINARY), "run", "-D", str(NODE_STATE), "-c", str(CONFIG)]
    if (
        Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
        != expected
    ):
        raise NodeRejected("unexpected_node_arguments")
    time.sleep(2)
    if _status() != first:
        raise NodeRejected("node_not_stable")
    return first


def _stored_source(runtime: Settings) -> dict:
    from sing_box_manager.account_copy.export import _selected

    paths = {
        "runtime": runtime.require_config_path(),
        "auth": PROJECT_ROOT / runtime.auth_snapshot_path,
        "proxy": CONFIG,
    }
    originals = {name: read_source(path) for name, path in paths.items()}
    _selected(originals, runtime)
    if any(read_source(paths[name]) != content for name, content in originals.items()):
        raise NodeRejected("stored_source_changed")
    return {name: hashlib.sha256(raw).hexdigest() for name, raw in originals.items()}


def fence(runtime: Settings, *, acknowledge_interrupt: bool) -> dict:
    if not acknowledge_interrupt:
        raise NodeRejected("portal_interruption_acknowledgement_required")
    with _locked():
        snapshot = _rpc(runtime, "snapshot")
        identity = UUID(snapshot["identity"])
        if storage.legacy_blocked():
            storage.require_fence(identity)
            _legacy_stopped()
            return {"fenced": True, "noop": True}
        if snapshot["active"]:
            raise NodeRejected("active_ownership_without_fence")
        original = _stored_source(runtime)
        # A partial fence blocks legacy operations but cannot authorize activation.
        fd = os.open(
            storage.FENCE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640
        )
        with os.fdopen(fd, "wb") as output:
            os.fchown(output.fileno(), 0, pwd.getpwnam("sbm").pw_gid)
            os.fchmod(output.fileno(), 0o640)
            output.write(b'{"state":"fencing"}\n')
            output.flush()
            os.fsync(output.fileno())
        storage.sync_directory(storage.STATE_ROOT)
        if _run(["systemctl", "disable", "--now", LEGACY_SERVICE]).returncode:
            raise NodeRejected("legacy_portal_stop_failed")
        _legacy_stopped()
        if _stored_source(runtime) != original or _rpc(runtime, "snapshot") != snapshot:
            raise NodeRejected("handover_state_changed")
        pending = storage.STATE_ROOT / ("fence-" + uuid4().hex)
        pending.mkdir(mode=0o700)
        storage.sync_directory(storage.STATE_ROOT)
        candidate = pending / "owner.json"
        publish_private(
            candidate, canonical({"version": 1, "identity": str(identity)}) + b"\n"
        )
        _publish(storage.FENCE, candidate, 0o640)
        os.chown(storage.FENCE, 0, pwd.getpwnam("sbm").pw_gid)
        with storage.FENCE.open("rb") as handle:
            os.fsync(handle.fileno())
        storage.sync_directory(storage.STATE_ROOT)
        return {"fenced": True, "noop": False}


def _legacy_stopped() -> None:
    if _run(["systemctl", "is-active", "--quiet", LEGACY_SERVICE]).returncode != 3:
        raise NodeRejected("legacy_portal_stop_not_verified")


def plan(runtime: Settings, run_dir: Path, *, binary: Path | None = None) -> dict:
    with _locked():
        if (
            run_dir.parent == storage.STATE_ROOT / "runs"
            and not run_dir.parent.exists()
        ):
            run_dir.parent.mkdir(mode=0o700)
            storage.sync_directory(storage.STATE_ROOT)
        with directory(run_dir.parent, private=True):
            pass
        run_dir.mkdir(mode=0o700)
        storage.sync_directory(run_dir.parent)
        _unit_guard()
        snapshot = _rpc(runtime, "snapshot")
        records = AccessRecords.model_validate(snapshot["access"])
        candidate = (
            canonical(
                build_server_config(
                    inventory_from_records(runtime, records), runtime.traffic_stats
                )
            )
            + b"\n"
        )
        publish_private(run_dir / "config.json", candidate)
        selected = BINARY if binary is None else binary.resolve(strict=True)
        binary_digest = _root_file(selected)
        shutil.copyfile(selected, run_dir / "sing-box")
        (run_dir / "sing-box").chmod(0o700)
        with (run_dir / "sing-box").open("rb") as handle:
            os.fsync(handle.fileno())
        if _root_file(run_dir / "sing-box") != binary_digest:
            raise NodeRejected("candidate_binary_changed")
        _check(run_dir / "sing-box", run_dir / "config.json")
        payload = {key: snapshot[key] for key in ("identity", "revision", "context")}
        payload.update(
            {
                "config_digest": hashlib.sha256(candidate).hexdigest(),
                "binary_digest": binary_digest,
                "old_config": _root_file(CONFIG),
                "old_binary": _root_file(BINARY),
                "runtime_digest": hashlib.sha256(
                    read_source(runtime.require_config_path())
                ).hexdigest(),
                "unit_digest": _root_file(UNIT),
            }
        )
        publish_private(run_dir / "plan.json", canonical(payload) + b"\n")
        return {
            "planned": True,
            "revision": payload["revision"],
            "run_dir": str(run_dir),
        }


def _replace_and_verify(run_dir: Path, selected: dict, previous: dict) -> None:
    _publish(BINARY, run_dir / "sing-box", 0o755)
    _publish(CONFIG, run_dir / "config.json", 0o600)
    if _run(["systemctl", "restart", SERVICE]).returncode:
        raise NodeRejected("node_restart_failed")
    current = _running()
    if (
        current["InvocationID"] == previous["InvocationID"]
        or _root_file(CONFIG) != selected["config_digest"]
        or _root_file(BINARY) != selected["binary_digest"]
    ):
        raise NodeRejected("node_restart_not_verified")


def _retain_before(run_dir: Path, selected: dict) -> None:
    for source, name, digest in (
        (CONFIG, "config.before.json", selected["old_config"]),
        (BINARY, "sing-box.before", selected["old_binary"]),
    ):
        with (run_dir / name).open("xb") as output, source.open("rb") as incoming:
            os.fchmod(output.fileno(), 0o600)
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        if _root_file(run_dir / name) != digest:
            raise NodeRejected("node_backup_changed")
    storage.sync_directory(run_dir)


def apply(runtime: Settings, run_dir: Path, *, acknowledge_interrupt: bool) -> dict:
    if not acknowledge_interrupt:
        raise NodeRejected("proxy_interruption_acknowledgement_required")
    with _locked():
        with directory(run_dir, private=True):
            pass
        if (run_dir / "started.json").exists():
            raise NodeRejected("outcome_requires_inspection")
        selected = json.loads(read_private(run_dir / "plan.json"))
        storage.require_fence(UUID(selected["identity"]))
        _unit_guard()
        if (
            _root_file(CONFIG) != selected["old_config"]
            or _root_file(BINARY) != selected["old_binary"]
            or _root_file(UNIT) != selected["unit_digest"]
            or hashlib.sha256(read_source(runtime.require_config_path())).hexdigest()
            != selected["runtime_digest"]
            or hashlib.sha256(read_private(run_dir / "config.json")).hexdigest()
            != selected["config_digest"]
            or _root_file(run_dir / "sing-box") != selected["binary_digest"]
        ):
            raise NodeRejected("node_plan_invalidated")
        snapshot = _rpc(runtime, "snapshot")
        if canonical(
            {key: snapshot[key] for key in ("identity", "revision", "context")}
        ) != canonical(
            {key: selected[key] for key in ("identity", "revision", "context")}
        ):
            raise NodeRejected("node_plan_stale")
        candidate = (
            canonical(
                build_server_config(
                    inventory_from_records(
                        runtime, AccessRecords.model_validate(snapshot["access"])
                    ),
                    runtime.traffic_stats,
                )
            )
            + b"\n"
        )
        if hashlib.sha256(candidate).hexdigest() != selected["config_digest"]:
            raise NodeRejected("node_projection_changed")
        _check(run_dir / "sing-box", run_dir / "config.json")
        previous = _running()
        _retain_before(run_dir, selected)
        payload = {key: selected[key] for key in ("identity", "revision", "context")}
        publish_private(run_dir / "started.json", canonical(payload) + b"\n")
        _rpc(runtime, "begin", **payload)
        try:
            _replace_and_verify(run_dir, selected, previous)
            result = _rpc(
                runtime,
                "result",
                **payload,
                digest=selected["config_digest"],
                failed=False,
            )
            publish_private(run_dir / "result.json", canonical(result) + b"\n")
            return {"applied": True, **result}
        except Exception:
            # Never restore old credentials automatically after a revocation.
            with suppress(Exception):
                _rpc(
                    runtime,
                    "result",
                    **payload,
                    digest=selected["config_digest"],
                    failed=True,
                )
            raise NodeRejected("node_apply_failed_or_uncertain") from None
