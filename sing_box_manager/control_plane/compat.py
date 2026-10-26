"""Database-backed migration endpoints for installed legacy clients.

These endpoints do not accept browser sessions or read generated user snapshots.
Keep the machine-token format and signing secret until legacy clients migrate.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
from functools import wraps

from argon2.exceptions import VerificationError
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_safe

from sing_box_manager.control_plane.models import Account
from sing_box_manager.control_plane.portal import (
    plan_total_bytes,
    release_context,
    vps_provider,
)
from sing_box_manager.control_plane.projections import account_protocols
from sing_box_manager.control_plane.views import _runtime, public_origin
from sing_box_manager.settings import ARGON2ID_VERIFIER, USERNAME_PATTERN
from sing_box_manager.subscription import PROTOCOLS, subscription_token
from sing_box_manager.traffic_stats import create_traffic_stats_provider
from sing_box_manager.web.auth import MachineTokenManager, RateLimiter

_limiter = RateLimiter()


def _account(request) -> Account | None:
    runtime = _runtime()
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    username = None
    password = None
    if scheme.lower() == "bearer" and runtime.web.session_secret:
        username = MachineTokenManager(runtime.web.session_secret).validate_token(value)
    elif scheme.lower() == "basic":
        try:
            username, password = (
                base64.b64decode(value, validate=True).decode().split(":", 1)
            )
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return None
    if username is None or not USERNAME_PATTERN.fullmatch(username):
        return None
    # REMOTE_ADDR is the direct peer, never an untrusted forwarded header.
    # Nginx also limits requests by source IP across Gunicorn workers.
    ip = request.META.get("REMOTE_ADDR", "unknown")
    if password is not None and not _limiter.check(username, ip):
        return None
    account = Account.objects.filter(
        username=username, enabled=True, portal_enabled=True
    ).first()
    if account is None:
        return None
    if password is not None:
        try:
            ARGON2ID_VERIFIER.verify(str(account.portal_password_hash), password)
        except (VerificationError, ValueError):
            return None
        _limiter.reset(username, ip)
    return account


def authenticated(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        account = _account(request)
        if account is None:
            return JsonResponse(
                {"detail": "Invalid credentials"},
                status=401,
                headers={"WWW-Authenticate": "Basic"},
            )
        return view(request, account, *args, **kwargs)

    return wrapped


@csrf_exempt
@require_POST
@authenticated
def exchange_subscription(request, account):
    runtime = _runtime()
    protocols = account_protocols(account, runtime)
    if not runtime.web.subscription_secret or not any(
        getattr(protocols, name) is not None for name in PROTOCOLS
    ):
        return HttpResponseNotFound()
    token = subscription_token(
        runtime.web.subscription_secret, account.username, account.subscription_counter
    )
    return JsonResponse(
        {"username": account.username, "url": f"{public_origin(request)}/sub/{token}"}
    )


@csrf_exempt
@require_POST
def machine_token(request):
    # Do not extend a bearer token's expiry without the legacy password.
    if not request.headers.get("Authorization", "").lower().startswith("basic "):
        return JsonResponse({"detail": "Basic authentication required"}, status=401)
    account = _account(request)
    secret = _runtime().web.session_secret
    if account is None or not secret:
        return JsonResponse({"detail": "Invalid credentials"}, status=401)
    return JsonResponse(
        {
            "username": account.username,
            "token": MachineTokenManager(secret).create_token(str(account.username)),
        }
    )


@require_safe
@authenticated
def authenticate(_request, _account):
    return JsonResponse({"authenticated": True})


@require_safe
@authenticated
def usage(_request, account):
    runtime = _runtime()
    provider = create_traffic_stats_provider(runtime)
    if provider is None:
        return JsonResponse({"error": "unavailable"})
    summary = provider.get_user_summary(account.username)
    multiplier = runtime.traffic_stats.relay_multiplier
    payload = {
        **asdict(summary),
        "cycle_start": summary.cycle_start.isoformat(),
        "cycle_end": summary.cycle_end.isoformat(),
        "updated_at": None
        if summary.updated_at is None
        else summary.updated_at.isoformat(),
        "total_bytes": summary.total_bytes,
        "relay_multiplier": multiplier,
        "billed_bytes": round(summary.total_bytes * multiplier),
        "plan_total_bytes": plan_total_bytes(),
        "error": None,
        "top_domains": [
            {**asdict(row), "total_bytes": row.total_bytes}
            for row in provider.get_domain_breakdown(
                account.username, days=30, limit=10
            ).domains
        ]
        if runtime.traffic_stats.show_user_domains
        else [],
    }
    return JsonResponse(payload)


@require_safe
def client_update(_request):
    release = release_context(_runtime())
    if not release:
        return HttpResponseNotFound()
    values = {
        "schema": 1,
        "client_build_id": release["client_build_id"],
        "commit_sha": release["commit_sha"],
        "dirty": str(release["dirty"]).lower(),
        "deployed_at": release["deployed_at"],
        "policy": release["upgrade_policy"],
        "notice_id": release["notice_id"],
        "message": release["upgrade_message"],
    }
    return HttpResponse(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        content_type="text/plain",
    )


@require_safe
@authenticated
def vps_info(_request, _account):
    runtime = _runtime()
    provider = vps_provider()
    payload = (
        {"error": "unavailable"} if provider is None else asdict(provider.get_info())
    )
    return JsonResponse(
        {
            "hostname": runtime.deployment.host,
            "relay_multiplier": runtime.traffic_stats.relay_multiplier,
            **payload,
        }
    )


@require_safe
def retired(_request):
    return HttpResponse(
        "This download portal is retired. Open your subscription page, or ask the operator for its link.\n",
        status=410,
        content_type="text/plain; charset=utf-8",
    )
