"""Private pipes between the root supervisor and the restricted database role."""

import json
import os
import stat
import sys
from uuid import UUID


def _dispatch() -> dict:
    # Snapshot output contains credentials. Do not allow terminal/file output.
    if not stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode):
        raise ValueError("private_pipe_required")
    raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("request_too_large")
    request = json.loads(raw)
    os.environ["DJANGO_SETTINGS_MODULE"] = (
        "sing_box_manager.control_plane.django_settings"
    )
    import django
    from django.db import connection

    django.setup()
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = '15s'")
        cursor.execute("SET lock_timeout = '3s'")
    from sing_box_manager.control_plane.django_settings import runtime
    from sing_box_manager.managed_access import service

    action = request.pop("action")
    if action == "snapshot" and not request:
        return service.node_snapshot(runtime)
    if action == "begin":
        service.begin_application(
            runtime, UUID(request["identity"]), request["revision"], request["context"]
        )
        return {"started": True}
    if action == "result":
        return service.record_application(
            runtime,
            UUID(request["identity"]),
            request["revision"],
            request["context"],
            request["digest"],
            failed=request["failed"],
        )
    raise ValueError("unsupported_private_operation")


def main() -> None:
    try:
        result = _dispatch()
        sys.stdout.write(json.dumps(result, separators=(",", ":")))
    except Exception:
        sys.stdout.write('{"error":"managed_database_operation_failed"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
