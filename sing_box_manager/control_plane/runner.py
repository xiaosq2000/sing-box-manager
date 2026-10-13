"""Explicit management operations; ordinary release commands never touch Django."""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Literal

from sing_box_manager.settings import Settings

Action = Literal[
    "serve", "migrate", "check", "create-admin", "collect-static", "health"
]
MIGRATION_LOCK = 1396854083


def run(action: Action, runtime: Settings) -> None:
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
    from django.core.management import call_command
    from django.db import connection

    django.setup()
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
