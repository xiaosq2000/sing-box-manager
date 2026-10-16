"""Minimal public portal. No operator admin, legacy authentication, or traffic UI."""

from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.db import connection, transaction
from django.http import (
    FileResponse,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotFound,
    JsonResponse,
)
from django.middleware.csrf import rotate_token
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from sing_box_manager.control_plane.import_parity import inventory_from_records
from sing_box_manager.managed_access import auth, service
from sing_box_manager.release import artifacts
from sing_box_manager.release.client_downloads import (
    CLIENT_MANIFEST_NAME,
    CLIENT_SIGNATURE_NAME,
    load_client_downloads,
)
from sing_box_manager.release.renderers import SUPPORTED_CLIENT_OS
from sing_box_manager.settings import Settings
from sing_box_manager.subscription import (
    build_envelope,
    encode_envelope,
    minimum_sing_box_version,
    parse_version,
)

INSTALLERS = Path(__file__).resolve().parent.parent / "web/client"


class PublicBoundaryMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            request.get_host()
        except DisallowedHost:
            response = HttpResponseBadRequest()
        else:
            response = (
                self.get_response(request)
                if request.is_secure()
                else HttpResponseBadRequest("HTTPS is required.")
            )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = (
            "no-referrer" if request.path.startswith("/sub/") else "same-origin"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def boundary(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            runtime = settings.SBM_RUNTIME
            if request.method in {"GET", "HEAD"}:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                    cursor.execute("SET LOCAL statement_timeout = '5s'")
                    return view(
                        request,
                        runtime,
                        service.owner(runtime, active=True),
                        *args,
                        **kwargs,
                    )
            return view(
                request, runtime, service.owner(runtime, active=True), *args, **kwargs
            )
        except Exception:
            # No exception text, request path, token, or SQL payload is logged.
            return HttpResponse(
                "Service unavailable.", status=503, headers={"Retry-After": "30"}
            )

    return wrapped


def address(request) -> str:
    # The loopback-only listener trusts nginx to replace this header.
    return request.META.get(
        "HTTP_X_REAL_IP", request.META.get("REMOTE_ADDR", "unknown")
    )[:64]


def downloads(runtime):
    try:
        return load_client_downloads(artifacts.resolve_release_dir(runtime))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class RenderSource:
    inventory: Settings
    default_protocol: str
    sing_box_version: str


@boundary
@sensitive_post_parameters("password")
def login(request, runtime, value):
    if request.method == "GET":
        if auth.session_account(request, runtime, value) is not None:
            return redirect("portal-files")
        return render(request, "managed_access/login.html")
    if request.method != "POST":
        return HttpResponse(status=405)
    account = auth.authenticate(
        runtime,
        value,
        request.POST.get("username", ""),
        request.POST.get("password", ""),
        address(request),
    )
    if account is None:
        return render(
            request, "managed_access/login.html", {"failed": True}, status=401
        )
    request.session.flush()
    rotate_token(request)
    request.session["proxy_subject"] = str(account[0].pk)
    request.session["proxy_verifier"] = service.session_identity(
        runtime, account[0].pk, account[1].password_hash
    )
    return redirect("portal-files")


@require_GET
@boundary
def files(request, runtime, value):
    account = auth.session_account(request, runtime, value)
    if account is None:
        return redirect("portal-login")
    return render(
        request,
        "managed_access/files.html",
        {
            "username": account[0].username,
            "link": service.link_for(runtime, value, account[0].pk),
            "pending": not service.ready(value, runtime),
            "origin": runtime.control_plane.portal_origin,
        },
    )


@require_POST
@boundary
def reset(request, runtime, value):
    with transaction.atomic():
        value = service.owner(runtime, active=True, lock=True)
        account = auth.session_account(request, runtime, value)
        if account is None:
            return HttpResponse(status=403)
        if not service.holds_protocols(runtime, account[2], account[0].username):
            return HttpResponseNotFound()
        service.reset_link(runtime, account[0].pk)
    return redirect("portal-files")


@require_POST
def logout(request):
    request.session.flush()
    return redirect("portal-login")


@csrf_exempt
@require_POST
@boundary
def exchange(request, runtime, value):
    # Deliberately accept password Basic auth only, never cookies or old machine tokens.
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or len(header) > 8192:
        return HttpResponse(
            status=401, headers={"WWW-Authenticate": 'Basic realm="subscriptions"'}
        )
    try:
        username, password = (
            base64.b64decode(encoded.lstrip(" "), validate=True).decode().split(":", 1)
        )
    except (ValueError, UnicodeError, binascii.Error):
        return HttpResponse(status=401)
    account = auth.authenticate(runtime, value, username, password, address(request))
    if account is None:
        return HttpResponse(status=401)
    # Do not issue a replacement link if the verifier changed during the KDF.
    with transaction.atomic():
        value = service.owner(runtime, active=True, lock=True)
        current = service.portal_account(value, account[0].pk)
        if current is None or not hmac.compare_digest(
            current[1].password_hash, account[1].password_hash
        ):
            return HttpResponse(status=401)
        url = service.link_for(runtime, value, account[0].pk)
    return (
        HttpResponseNotFound()
        if url is None
        else JsonResponse({"url": url, "username": username})
    )


def _parameters(request, runtime) -> tuple[HttpResponse | None, str]:
    os_name = request.GET.get("os")
    version = parse_version(request.GET.get("sing-box", ""))
    if (
        request.GET.get("format") != "sbc"
        or not os_name
        or os_name not in SUPPORTED_CLIENT_OS
        or version is None
    ):
        return JsonResponse(
            {"detail": "Invalid subscription parameters."}, status=400
        ), ""
    minimum = minimum_sing_box_version(runtime.sing_box_version)
    required = parse_version(minimum)
    if required is None or version < required:
        return JsonResponse(
            {
                "detail": "A newer sing-box version is required.",
                "sing_box_minimum": minimum,
            },
            status=409,
        ), ""
    return None, os_name


@require_GET
@boundary
def subscription(request, runtime, value, token):
    found = service.resolve(runtime, value, token)
    if found is None or not service.holds_protocols(
        runtime, found[1], found[0].username
    ):
        return HttpResponseNotFound()
    subject, records = found
    error, os_name = _parameters(request, runtime)
    if error is not None:
        return error
    if not service.ready(value, runtime):
        return HttpResponse(
            "Node application pending.", status=503, headers={"Retry-After": "30"}
        )
    assets = downloads(runtime)
    source = RenderSource(
        inventory_from_records(runtime, records),
        runtime.default_protocol,
        runtime.sing_box_version,
    )
    envelope = build_envelope(
        source,
        subject.username,
        os_name=os_name,
        sbc_version=None if assets is None else assets.sbc_version,
    )
    if envelope is None:
        return HttpResponseNotFound()
    body, etag = encode_envelope(envelope)
    candidates = {
        part.strip().removeprefix("W/")
        for part in request.headers.get("If-None-Match", "").split(",")
    }
    if etag in candidates or "*" in candidates:
        return HttpResponse(status=304, headers={"ETag": etag})
    return HttpResponse(body, content_type="application/json", headers={"ETag": etag})


@require_GET
@boundary
def client_file(request, runtime, value, token, name):
    if service.resolve(runtime, value, token) is None:
        return HttpResponseNotFound()
    assets = downloads(runtime)
    path = None if assets is None else assets.path_for(name)
    if path is None or not path.is_file():
        return HttpResponseNotFound()
    content_type = (
        "application/json"
        if name == CLIENT_MANIFEST_NAME
        else "text/plain"
        if name == CLIENT_SIGNATURE_NAME
        else "application/octet-stream"
    )
    return FileResponse(path.open("rb"), content_type=content_type)


@require_GET
def installer(request, *, windows=False):
    path = INSTALLERS / ("install.ps1" if windows else "install.sh")
    return HttpResponse(
        path.read_text(encoding="utf-8-sig"),
        content_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="install.ps1"'}
        if windows
        else None,
    )


@require_GET
@boundary
def health(request, runtime, value):
    if not service.ready(value, runtime) or downloads(runtime) is None:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
