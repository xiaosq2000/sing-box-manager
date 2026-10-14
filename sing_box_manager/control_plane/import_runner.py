"""Local fixture command dispatch. No migrations, SSH, or live capture."""

import os
from pathlib import Path
from typing import Literal

from sing_box_manager.control_plane.import_schema import ImportRejected
from sing_box_manager.settings import Settings


def run(
    action: Literal["preview", "apply"], snapshot: Path, report: Path, runtime: Settings
) -> dict:
    if not runtime.control_plane.enabled or not runtime.control_plane.secret_key:
        raise ImportRejected("configuration_rejected")
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
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        raise ImportRejected("migrations_required")
    from sing_box_manager.control_plane import import_service

    operation = import_service.preview if action == "preview" else import_service.apply
    return operation(snapshot, report, runtime)
