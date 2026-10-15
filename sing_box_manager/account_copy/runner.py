"""Local operations only. No migrations, SSH, service changes, or link activation."""

import os

from sing_box_manager.control_plane.import_schema import ImportRejected
from sing_box_manager.settings import Settings


def run(action: str, runtime: Settings, options: dict) -> dict:
    if not runtime.control_plane.enabled or not runtime.control_plane.secret_key:
        raise ImportRejected("configuration_rejected")
    if action == "export":
        from sing_box_manager.account_copy.export import export_snapshot

        return export_snapshot(
            runtime,
            options["snapshot"],
            options["auth_file"],
            options["proxy_config"],
            namespace=options["namespace"],
            source_identity=options["source_identity"],
            sequence=options["sequence"],
        )
    os.environ["SBM_CONFIG"] = str(runtime.require_config_path())
    os.environ["DJANGO_SETTINGS_MODULE"] = (
        "sing_box_manager.control_plane.django_settings"
    )
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    import django
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    django.setup()
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes("account_copy")):
        raise ImportRejected("migrations_required")
    from sing_box_manager.account_copy import service

    if action in {"authorize", "close"}:
        operation = service.authorize if action == "authorize" else service.close
        return operation(runtime, options["namespace"], options["source_identity"])
    if action == "preview":
        return service.preview(options["snapshot"], options["report"], runtime)
    if action == "apply":
        return service.apply(options["snapshot"], options["approved_report"], runtime)
    raise ImportRejected("invalid_account_copy_arguments")
