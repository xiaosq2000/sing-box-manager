from django.contrib.staticfiles.storage import staticfiles_storage
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.http import require_safe


@require_safe
def health(_request):
    """Report readiness without disclosing database or exception details."""
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if pending or not staticfiles_storage.exists(
            staticfiles_storage.stored_name("admin/css/base.css")
        ):
            return JsonResponse({"status": "not-ready"}, status=503)
    except (DatabaseError, ValueError):
        return JsonResponse({"status": "not-ready"}, status=503)
    return JsonResponse({"status": "ok"})
