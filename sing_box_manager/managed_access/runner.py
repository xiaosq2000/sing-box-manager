import json
import os
import sys
import time
import urllib.request
from urllib.parse import urlsplit
from uuid import uuid4

from sing_box_manager.managed_access.command import ManagedAccessCommand
from sing_box_manager.managed_access.errors import AccessRejected
from sing_box_manager.settings import Settings


def run(command: ManagedAccessCommand, runtime: Settings) -> dict:
    command.validate()
    if not runtime.control_plane.enabled or not runtime.control_plane.secret_key:
        raise AccessRejected("configuration_rejected")
    os.environ["SBM_CONFIG"] = str(runtime.require_config_path())
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    if command.action in {"fence", "node-plan", "node-apply", "node-apply-current"}:
        return _node_operation(command, runtime)
    if command.action in {"serve", "health"}:
        return _portal_operation(command, runtime)
    os.environ["DJANGO_SETTINGS_MODULE"] = (
        "sing_box_manager.control_plane.django_settings"
    )
    import django
    from django.db import connection, transaction
    from django.db.migrations.executor import MigrationExecutor

    django.setup()
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes("managed_access")):
        raise AccessRejected("migrations_required")
    from sing_box_manager.managed_access import service

    if command.action == "prepare":
        if command.namespace is None:
            raise AccessRejected("namespace_required")
        return service.prepare(runtime, command.namespace)
    if command.action == "activate":
        return service.activate(runtime)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        value = service.owner(runtime, active=command.require_active)
        return {
            "prepared": True,
            "active": value.active,
            "revision": value.revision,
            "applied_revision": value.applied_revision,
            "pending": not service.ready(value, runtime),
            "error": value.last_error,
        }


def _node_operation(command: ManagedAccessCommand, runtime: Settings) -> dict:
    from sing_box_manager.managed_access import node, storage

    if command.action == "fence":
        return node.fence(runtime, acknowledge_interrupt=command.acknowledge_interrupt)
    run_dir = command.run_dir or storage.STATE_ROOT / "runs" / ("node-" + uuid4().hex)
    if command.action == "node-apply-current" and not command.acknowledge_interrupt:
        raise AccessRejected("proxy_interruption_acknowledgement_required")
    if command.action in {"node-plan", "node-apply-current"}:
        result = node.plan(runtime, run_dir, binary=command.binary)
        if command.action == "node-plan":
            return result
    return {
        **node.apply(
            runtime, run_dir, acknowledge_interrupt=command.acknowledge_interrupt
        ),
        "run_dir": str(run_dir),
    }


def _portal_operation(command: ManagedAccessCommand, runtime: Settings) -> dict:
    if (
        not runtime.control_plane.portal_enabled
        or not runtime.control_plane.portal_origin
    ):
        raise AccessRejected("public_portal_disabled")
    if command.action == "health":
        health(runtime)
        return {"ready": True}
    os.environ["DJANGO_SETTINGS_MODULE"] = (
        "sing_box_manager.managed_access.public_settings"
    )
    os.execv(  # noqa: S606 -- fixed interpreter and service arguments
        sys.executable,
        [
            sys.executable,
            "-m",
            "gunicorn",
            "sing_box_manager.managed_access.wsgi:application",
            "--bind",
            f"127.0.0.1:{runtime.control_plane.portal_port}",
            "--workers",
            "2",
            "--timeout",
            "30",
            "--access-logfile",
            "/dev/null",
            "--error-logfile",
            "/dev/null",
            "--forwarded-allow-ips",
            "127.0.0.1",
        ],
    )
    return {}


def health(runtime: Settings) -> None:
    origin = runtime.control_plane.portal_origin
    if origin is None:
        raise AccessRejected("public_portal_disabled")
    request = urllib.request.Request(
        f"http://127.0.0.1:{runtime.control_plane.portal_port}/health/",
        headers={"Host": urlsplit(origin).netloc, "X-Forwarded-Proto": "https"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=2) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise AccessRejected("public_portal_not_ready")
