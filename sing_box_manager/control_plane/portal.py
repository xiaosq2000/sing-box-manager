"""Subscription pages and private operator traffic views."""

from __future__ import annotations

import hashlib
from functools import lru_cache

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_safe

from sing_box_manager.control_plane.models import Account
from sing_box_manager.release import artifacts
from sing_box_manager.settings import Settings
from sing_box_manager.traffic_stats import create_traffic_stats_provider
from sing_box_manager.web import charts
from sing_box_manager.web.formatting import format_plan_usage
from sing_box_manager.web.vps_info import create_vps_info_provider

RANGE_DAYS = (7, 30, 90)


@lru_cache(maxsize=1)
def vps_provider():
    from sing_box_manager.control_plane.views import _runtime

    runtime = _runtime()
    return create_vps_info_provider(
        runtime.vps_info, timezone_name=runtime.traffic_stats.timezone
    )


def plan_total_bytes() -> int | None:
    provider = vps_provider()
    if provider is None:
        return None
    info = provider.get_info()
    return info.bandwidth_total_bytes if info.error is None else None


def release_context(runtime) -> dict:
    """Keep the legacy update protocol without loading an authentication snapshot."""
    from sing_box_manager.control_plane.views import _client_downloads

    try:
        info = artifacts.read_release_info(artifacts.resolve_release_info_path(runtime))
    except (OSError, ValueError):
        return {}
    deployed_at = ""
    try:
        deployment = artifacts.read_deployment_info(runtime.deployment_info_path)
        if info.release_id and info.release_id == deployment.release_id:
            deployed_at = deployment.deployed_at
    except (OSError, ValueError):
        pass
    downloads = _client_downloads(runtime)
    build_id = downloads.build_id if downloads else info.client_build_id or ""
    upgrade = runtime.client_upgrade
    notice = f"{build_id}\0{upgrade.policy}\0{upgrade.message}".encode()
    return {
        "commit_sha": info.commit_sha,
        "dirty": info.dirty,
        "deployed_at": deployed_at,
        "client_build_id": build_id,
        "upgrade_policy": upgrade.policy,
        "upgrade_message": upgrade.message,
        "notice_id": hashlib.sha256(notice).hexdigest(),
    }


def user_usage(
    runtime, username: str, *, days: int = 30, operator: bool = False
) -> dict:
    provider = create_traffic_stats_provider(runtime)
    if provider is None:
        return {}
    summary = provider.get_user_summary(username)
    context: dict = {
        "summary": summary,
        "plan_usage": format_plan_usage(
            summary.total_bytes,
            relay_multiplier=runtime.traffic_stats.relay_multiplier,
            plan_total_bytes=plan_total_bytes(),
        ),
    }
    if operator or runtime.traffic_stats.show_user_domains:
        domains = provider.get_domain_breakdown(username, days=days, limit=20)
        context["domains"] = domains.domains
        context["domain_chart"] = charts.render_domain_ranking_chart(domains)
    if operator:
        protocols = provider.get_protocol_breakdown(username)
        context.update(
            protocols=protocols.protocols,
            destinations=provider.get_destination_breakdown(
                username, days=days, limit=20
            ).destinations,
            trend_chart=charts.render_daily_trend_chart(
                provider.get_daily_series(username, days=days)
            ),
            protocol_chart=charts.render_protocol_split_chart(protocols),
            month_chart=charts.render_monthly_history_chart(
                provider.get_monthly_history(username, months=6)
            ),
        )
    return context


def _operator_context(request) -> tuple[Settings, dict]:
    from sing_box_manager.control_plane.views import _runtime

    if not request.user.has_perm("control_plane.view_traffic"):
        raise PermissionDenied
    runtime = _runtime()
    try:
        days = int(request.GET.get("days", "30"))
    except ValueError:
        days = 30
    if days not in RANGE_DAYS:
        days = 30
    return runtime, {
        **admin.site.each_context(request),
        "days": days,
        "ranges": RANGE_DAYS,
        "traffic_enabled": runtime.traffic_stats.enabled,
    }


def _render(request, runtime, context):
    with timezone.override(runtime.traffic_stats.timezone):
        return render(request, "control_plane/traffic.html", context)


@require_safe
def traffic(request):
    runtime, context = _operator_context(request)
    context["title"] = "Traffic statistics"
    provider = create_traffic_stats_provider(runtime)
    if provider is not None:
        summaries = provider.get_all_user_summaries(
            list(Account.objects.values_list("username", flat=True))
        )
        domains = provider.get_domain_breakdown(days=context["days"], limit=20)
        context.update(
            summaries=summaries,
            upload=sum(s.upload_bytes for s in summaries),
            download=sum(s.download_bytes for s in summaries),
            plan_usage=format_plan_usage(
                sum(s.total_bytes for s in summaries),
                relay_multiplier=runtime.traffic_stats.relay_multiplier,
                plan_total_bytes=plan_total_bytes(),
            ),
            domains=domains.domains,
            ranking_chart=charts.render_user_ranking_chart(
                summaries,
                detail_url_for=lambda username: reverse(
                    "traffic_user", args=[username]
                ),
            ),
            trend_chart=charts.render_daily_trend_chart(
                provider.get_daily_series(days=context["days"])
            ),
            protocol_chart=charts.render_protocol_split_chart(
                provider.get_protocol_breakdown()
            ),
            domain_chart=charts.render_domain_ranking_chart(domains),
        )
    return _render(request, runtime, context)


@require_safe
def traffic_user(request, username: str):
    runtime, context = _operator_context(request)
    provider = create_traffic_stats_provider(runtime)
    if not Account.objects.filter(username=username).exists() and (
        provider is None or provider.get_user_summary(username).updated_at is None
    ):
        raise Http404
    context.update(title=f"Traffic: {username}", username=username)
    context.update(user_usage(runtime, username, days=context["days"], operator=True))
    return _render(request, runtime, context)


@require_safe
def connections(request):
    runtime, context = _operator_context(request)
    provider = create_traffic_stats_provider(runtime)
    context.update(
        title="Active connections",
        connections=[]
        if provider is None
        else provider.get_live_connections(limit=100),
        live=True,
    )
    return _render(request, runtime, context)
