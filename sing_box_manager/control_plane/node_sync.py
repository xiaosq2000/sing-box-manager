"""Single-node config rendering, validation, and service synchronization."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from sing_box_manager.control_plane.projections import build_inventory_from_db
from sing_box_manager.control_plane.runner import ControlPlaneError
from sing_box_manager.deploy import SERVICE_USER
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.settings import RuntimeInventory, Settings

DEFAULT_SERVER_CONFIG_PATH = Path("/usr/local/etc/sing-box/config.json")
DEFAULT_SING_BOX_BINARY_PATH = Path("/usr/local/bin/sing-box")
DEFAULT_SING_BOX_SERVICE = "sing-box.service"


def request_node_sync(runtime: Settings, *, trigger_path: Path | None = None) -> Path:
    """Write the trigger file watched by sing-box-manager-sync.path."""
    path = trigger_path or (runtime.generated_root / "node-sync.trigger")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.time_ns()}\n", encoding="utf-8")
    return path


def _build_inventory_as_db_user(runtime: Settings) -> RuntimeInventory:
    """Drop effective privileges to the DB peer role when invoked as root."""
    if os.geteuid() != 0:
        return build_inventory_from_db(runtime)
    import pwd

    from django.db import connections

    egid = os.getegid()
    pw = pwd.getpwnam(SERVICE_USER)
    connections.close_all()
    try:
        os.setegid(pw.pw_gid)
        os.seteuid(pw.pw_uid)
        return build_inventory_from_db(runtime)
    finally:
        try:
            connections.close_all()
        finally:
            os.seteuid(0)
            os.setegid(egid)


def sync_node_config(
    runtime: Settings,
    *,
    config_path: Path | None = None,
    binary_path: Path | None = None,
    service_name: str = DEFAULT_SING_BOX_SERVICE,
    restart_service: bool = True,
) -> dict[str, object]:
    """Serialize node updates, validate the candidate, and recover on failure."""
    import fcntl

    resolved_config = config_path or Path(
        os.environ.get("SBM_SING_BOX_CONFIG", str(DEFAULT_SERVER_CONFIG_PATH))
    )
    resolved_binary = binary_path or Path(
        os.environ.get("SBM_SING_BOX_BINARY", str(DEFAULT_SING_BOX_BINARY_PATH))
    )
    resolved_config.parent.mkdir(parents=True, exist_ok=True)
    # The server installer uses the same lock. Acquire it before reading the DB
    # so a waiting sync cannot apply an obsolete snapshot after a newer update.
    with (resolved_config.parent / ".sync.lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync_node_config_locked(
            runtime, resolved_config, resolved_binary, service_name, restart_service
        )


def _restart_node_service(service_name: str) -> bool:
    try:
        restart = subprocess.run(
            ["systemctl", "restart", service_name], capture_output=True, check=False
        )
        if restart.returncode != 0:
            return False
        time.sleep(1)
        health = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name],
            capture_output=True,
            check=False,
        )
        return health.returncode == 0
    except OSError:
        return False


def _sync_node_config_locked(
    runtime: Settings,
    resolved_config: Path,
    resolved_binary: Path,
    service_name: str,
    restart_service: bool,
) -> dict[str, object]:
    inventory = _build_inventory_as_db_user(runtime)
    server_config = build_server_config(inventory, runtime.traffic_stats)
    rendered = json.dumps(server_config, indent=2, sort_keys=True) + "\n"
    result: dict[str, object] = {
        "config_path": str(resolved_config),
        "inbounds": [inbound["tag"] for inbound in server_config["inbounds"]],
    }
    fd, temp_name = tempfile.mkstemp(
        prefix=".config-", suffix=".json.tmp", dir=str(resolved_config.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        temp_path.chmod(0o600)

        if resolved_binary.is_file():
            check = subprocess.run(
                [str(resolved_binary), "check", "-c", str(temp_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode != 0:
                # Checker output can contain credentials from the candidate.
                raise ControlPlaneError(
                    "sing-box check failed. The active config is unchanged."
                )
        elif restart_service:
            raise ControlPlaneError("sing-box binary is missing. Node sync stopped.")

        previous_bytes = (
            resolved_config.read_bytes() if resolved_config.is_file() else None
        )
        try:
            unchanged = (
                previous_bytes is not None
                and json.loads(previous_bytes) == server_config
            )
        except (ValueError, UnicodeDecodeError):
            unchanged = False
        if unchanged and (
            not restart_service
            or subprocess.run(
                ["systemctl", "is-active", "--quiet", service_name],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        ):
            return result
        os.replace(temp_path, resolved_config)
        if restart_service and not _restart_node_service(service_name):
            if previous_bytes is None:
                resolved_config.unlink()
                raise ControlPlaneError(
                    "Node start failed. No previous config is available."
                )
            # Restore atomically before attempting to recover the service.
            with temp_path.open("xb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(previous_bytes)
            os.replace(temp_path, resolved_config)
            if not _restart_node_service(service_name):
                raise ControlPlaneError(
                    "Node start and service recovery failed. The previous config is restored. "
                    "Check the node service on the server."
                )
            raise ControlPlaneError(
                "Node start failed. The previous config and service are restored."
            )
    finally:
        temp_path.unlink(missing_ok=True)

    return result
