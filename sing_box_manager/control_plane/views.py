from __future__ import annotations

import os
from pathlib import Path

from django.contrib.staticfiles.storage import staticfiles_storage
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import (
    FileResponse,
    HttpResponse,
    HttpResponseNotFound,
    JsonResponse,
)
from django.views.decorators.http import require_safe

from sing_box_manager.control_plane.services import (
    build_account_envelope,
    find_db_subscriber,
)
from sing_box_manager.release import artifacts
from sing_box_manager.release.client_downloads import (
    CLIENT_MANIFEST_NAME,
    CLIENT_SIGNATURE_NAME,
    ClientDownloads,
    load_client_downloads,
)
from sing_box_manager.release.renderers import SUPPORTED_CLIENT_OS
from sing_box_manager.settings import Settings, load_settings
from sing_box_manager.subscription import (
    encode_envelope,
    minimum_sing_box_version,
    parse_version,
)

INSTALL_SCRIPT = (
    Path(__file__).resolve().parent.parent / "web" / "client" / "install.sh"
)


def _runtime() -> Settings:
    config_path = os.environ.get("SBM_CONFIG")
    if config_path:
        return load_settings(config_path)
    from sing_box_manager.control_plane import django_settings

    return django_settings.runtime


def _client_downloads(runtime: Settings) -> ClientDownloads | None:
    try:
        release_dir = artifacts.resolve_release_dir(runtime)
    except (FileNotFoundError, RuntimeError, ValueError):
        release_dir = runtime.release_dir
    if not release_dir.is_dir():
        return None
    return load_client_downloads(release_dir)


def _matches_etag(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    candidates = {
        value.strip().removeprefix("W/") for value in if_none_match.split(",")
    }
    return etag in candidates or "*" in candidates


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


def _validate_subscription_query(
    request, runtime: Settings
) -> tuple[str | None, JsonResponse | None]:
    if request.GET.get("format") != "sbc":
        return None, JsonResponse({"detail": "format must be sbc"}, status=400)
    os_name = request.GET.get("os")
    if os_name not in SUPPORTED_CLIENT_OS:
        return None, JsonResponse(
            {"detail": f"os must be one of {', '.join(SUPPORTED_CLIENT_OS)}"},
            status=400,
        )
    client_version = parse_version(request.GET.get("sing-box", ""))
    if client_version is None:
        return None, JsonResponse(
            {"detail": "sing-box must name the client's sing-box version"},
            status=400,
        )
    minimum = minimum_sing_box_version(runtime.sing_box_version)
    minimum_version = parse_version(minimum)
    if minimum_version is not None and client_version < minimum_version:
        return None, JsonResponse(
            {
                "detail": f"This config needs sing-box {minimum} or later",
                "sing_box_minimum": minimum,
            },
            status=409,
        )
    return os_name, None


@require_safe
def subscription(request, token: str):
    """Serve the sbc subscription config envelope from PostgreSQL accounts."""
    runtime = _runtime()
    secret = runtime.web.subscription_secret
    account = find_db_subscriber(secret, token) if secret else None
    if account is None:
        return HttpResponseNotFound()

    os_name, error_response = _validate_subscription_query(request, runtime)
    if error_response is not None or os_name is None:
        return error_response

    downloads = _client_downloads(runtime)
    envelope = build_account_envelope(
        account,
        runtime,
        os_name=os_name,
        sbc_version=None if downloads is None else downloads.sbc_version,
    )
    if envelope is None:
        return HttpResponseNotFound()

    body, etag = encode_envelope(envelope)
    headers = {
        "ETag": etag,
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, no-store",
    }
    if _matches_etag(request.headers.get("If-None-Match"), etag):
        return HttpResponse(status=304, headers=headers)
    return HttpResponse(body, content_type="application/json", headers=headers)


@require_safe
def subscription_file(_request, token: str, name: str):
    """Serve a manifest-listed client download for an enabled account token."""
    runtime = _runtime()
    secret = runtime.web.subscription_secret
    if not secret or find_db_subscriber(secret, token) is None:
        return HttpResponseNotFound()
    downloads = _client_downloads(runtime)
    path = None if downloads is None else downloads.path_for(name)
    if path is None or not path.is_file():
        return HttpResponseNotFound()
    media_type = (
        "application/json"
        if name == CLIENT_MANIFEST_NAME
        else "text/plain"
        if name == CLIENT_SIGNATURE_NAME
        else "application/octet-stream"
    )
    return FileResponse(
        path.open("rb"),
        content_type=media_type,
        headers={
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "private, no-store",
        },
    )


@require_safe
def install_sh(_request):
    """Serve the POSIX sh installer for Linux and macOS."""
    return HttpResponse(
        INSTALL_SCRIPT.read_text(encoding="utf-8"),
        content_type="text/plain; charset=utf-8",
    )


@require_safe
def install_ps1(_request):
    """Serve the Windows PowerShell 5.1 bootstrapper."""
    return HttpResponse(
        INSTALL_SCRIPT.with_suffix(".ps1").read_text(encoding="utf-8-sig"),
        content_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="install.ps1"'},
    )
