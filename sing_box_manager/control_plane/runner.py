"""Explicit management operations; ordinary release commands never touch Django."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sing_box_manager.settings import Settings

CanonicalAction = Literal[
    "serve",
    "migrate",
    "check",
    "create-admin",
    "collect-static",
    "health",
    "import-inventory",
    "sync-node",
]
Action = Literal[CanonicalAction, "createsuperuser", "import", "sync"]
MIGRATION_LOCK = 1396854083


class ControlPlaneError(RuntimeError):
    """An operator-facing error that contains no credentials or backend output."""


def normalize_action(action: Action) -> CanonicalAction:
    if action == "createsuperuser":
        return "create-admin"
    if action == "import":
        return "import-inventory"
    if action == "sync":
        return "sync-node"
    return action


def run(action: Action, runtime: Settings) -> None:
    action = normalize_action(action)
    control = runtime.control_plane
    if not control.enabled:
        raise ValueError("The control plane is disabled")
    if not control.secret_key:
        raise ValueError("control_plane.secret_key is required for this operation")
    os.environ["SBM_CONFIG"] = str(runtime.require_config_path())
    os.environ["DJANGO_SETTINGS_MODULE"] = (
        "sing_box_manager.control_plane.django_settings"
    )
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    if action == "health":
        wait_for_health(control.port)
        return
    if action == "serve":
        os.execv(  # noqa: S606 -- fixed interpreter and argv; replace the service process
            sys.executable,
            [
                sys.executable,
                "-m",
                "gunicorn",
                "sing_box_manager.control_plane.wsgi:application",
                "--bind",
                f"127.0.0.1:{control.port}",
                "--workers",
                "2",
                "--timeout",
                "30",
                "--access-logfile",
                "/dev/null",
                "--error-logfile",
                "-",
                "--forwarded-allow-ips",
                "",
            ],
        )
        return

    import django

    django.setup()
    _run_django_action(action, runtime)


def _run_django_action(action: CanonicalAction, runtime: Settings) -> None:
    from django.core.management import call_command
    from django.db import connection

    if action == "migrate":
        # Session lock serializes concurrent deployments, including migrations
        # whose individual SQL transactions finish before the whole plan does.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK])
            try:
                call_command("migrate", interactive=False, verbosity=1)
            finally:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK])
    elif action == "collect-static":
        call_command("collectstatic", interactive=False, verbosity=0)
    elif action == "create-admin":
        if not sys.stdin.isatty():
            raise ValueError("create-admin requires an interactive terminal")
        call_command("createsuperuser", interactive=True)
    elif action == "import-inventory":
        from sing_box_manager.control_plane.services import import_inventory

        counts = import_inventory(runtime)
        print(f"Imported inventory: {json.dumps(counts, sort_keys=True)}")
    elif action == "sync-node":
        from sing_box_manager.control_plane.services import sync_node_config

        result = sync_node_config(runtime)
        print(f"Synced node config: {json.dumps(result, sort_keys=True)}")
    else:
        call_command("check", fail_level="WARNING")


def wait_for_health(port: int) -> None:
    deadline = time.monotonic() + 30
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/health/", timeout=2) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Control-plane readiness check failed")
