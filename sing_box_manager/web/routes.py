"""FastAPI route handlers for the authenticated download service."""

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse

from sing_box_manager.release import artifacts
from sing_box_manager.release.artifacts import resolve_user_archive_owner
from sing_box_manager.release.packing import AssembledArchive
from sing_box_manager.release.platforms import ARCHIVE_PLATFORMS
from sing_box_manager.settings import AuthSnapshotUser
from sing_box_manager.traffic_stats import (
    DestinationUsageBreakdown,
    DomainUsageBreakdown,
    LiveConnection,
    ProtocolUsageBreakdown,
    TrafficStatsProvider,
    UserTrafficSummary,
)
from sing_box_manager.web import charts
from sing_box_manager.web.archives import ArchiveIndex
from sing_box_manager.web.auth import MachineTokenManager, RateLimiter, SessionManager
from sing_box_manager.web.formatting import format_byte_count, format_plan_usage
from sing_box_manager.web.vps_info import VpsInfoProvider

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic(auto_error=False)
password_hasher = PasswordHasher()

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")
# Trend windows offered as server-rendered links, since the portal ships no
# JavaScript to re-fetch a chart.
TRAFFIC_RANGE_DAYS: tuple[int, ...] = (7, 30, 90)
DEFAULT_TRAFFIC_RANGE_DAYS = 30
MONTHLY_HISTORY_MONTHS = 6
# Rows in the domain and destination tables. Long enough to be useful, short
# enough that the page stays scannable without pagination the portal has no
# JavaScript to drive.
DOMAIN_TABLE_LIMIT = 20
LIVE_CONNECTION_TABLE_LIMIT = 100
# The ops view refreshes itself with a meta tag, since there is no JavaScript to
# poll with. Slow enough not to fight with reading the page.
LIVE_VIEW_REFRESH_SECONDS = 10
# Domains shown to an ordinary user on their own download page, when the
# operator has opted in. Deliberately shorter than the admin table.
USER_DOMAIN_TABLE_LIMIT = 10
ARCHIVE_PLATFORM_SUFFIXES: tuple[tuple[str, str], ...] = tuple(
    (f"-{platform.name}{platform.package_ext}", platform.name)
    for platform in ARCHIVE_PLATFORMS
)
DEFAULT_ARCHIVE_UI: dict[str, str] = {
    "icon_class": "file-icon-pair--default",
    "icon_left_kind": "material",
    "icon_left_name": "folder_zip",
    "icon_right_kind": "material",
    "icon_right_name": "deployed_code",
    "platform_label": "压缩包",
}
ARCHIVE_PLATFORM_UI: dict[str, dict[str, str]] = {
    "android-arm64": {
        "icon_class": "file-icon-pair--mobile",
        "icon_left_kind": "logo",
        "icon_left_name": "apple",
        "icon_right_kind": "logo",
        "icon_right_name": "android",
        "platform_label": "Android ARM64",
    },
}


def _verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against an Argon2id hash."""
    try:
        return password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def _get_session_manager(request: Request) -> SessionManager:
    return request.app.state.session_manager


def _get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def _get_users(request: Request) -> dict[str, AuthSnapshotUser]:
    return request.app.state.users


def _get_archive_index(request: Request) -> ArchiveIndex:
    return request.app.state.archive_index


def _archive_response(
    archive: AssembledArchive, filename: str, range_header: str | None
) -> Response:
    """Serve an assembled archive, honouring a single-range request.

    Content-Length is exact because assembly computes the length before any
    byte is produced, so this keeps the semantics a plain FileResponse had:
    truncation is detectable and interrupted downloads can resume.
    """
    total = archive.content_length
    headers = {
        "content-disposition": f'attachment; filename="{filename}"',
        "accept-ranges": "bytes",
    }

    requested = _parse_single_byte_range(range_header, total)
    if requested is None:
        return StreamingResponse(
            archive.read(),
            media_type="application/octet-stream",
            headers={**headers, "content-length": str(total)},
        )

    start, stop = requested
    return StreamingResponse(
        archive.read(start, stop),
        status_code=206,
        media_type="application/octet-stream",
        headers={
            **headers,
            "content-length": str(stop - start),
            "content-range": f"bytes {start}-{stop - 1}/{total}",
        },
    )


def _parse_single_byte_range(
    range_header: str | None, total: int
) -> tuple[int, int] | None:
    """Parse `Range: bytes=a-b`, returning None when it should be ignored.

    Only a single range is supported; anything else falls back to the whole
    body, which is a valid response to any range request.
    """
    if not range_header or total == 0:
        return None

    units, _, spec = range_header.partition("=")
    if units.strip().lower() != "bytes" or "," in spec:
        return None

    first, _, last = spec.strip().partition("-")
    try:
        if first:
            start = int(first)
            stop = total if not last else min(total, int(last) + 1)
        else:
            # A suffix range: the final N bytes.
            start = max(0, total - int(last))
            stop = total
    except ValueError:
        return None

    if start >= total or start >= stop:
        return None
    return start, stop


def _session_cookie_secure(request: Request) -> bool:
    return request.url.scheme == "https"


def _resolve_archive_platform(filename: str) -> str | None:
    for suffix, platform_name in ARCHIVE_PLATFORM_SUFFIXES:
        if filename.endswith(suffix):
            return platform_name
    return None


def _build_file_entry(filename: str) -> dict[str, str]:
    platform_name = _resolve_archive_platform(filename)
    if platform_name is None:
        ui = DEFAULT_ARCHIVE_UI
    else:
        ui = ARCHIVE_PLATFORM_UI.get(platform_name, DEFAULT_ARCHIVE_UI)
    return {
        "name": filename,
        "platform": platform_name or "archive",
        **ui,
    }


def unix_install_command(request: Request) -> str:
    """The one line that installs sbc on Linux and macOS."""
    return f'curl -fsSL "{request.url_for("install_script")}" | sh'


def _build_install_targets(request: Request) -> list[dict[str, str]]:
    """Build copy-safe one-line installer commands for the login page."""
    windows_url = request.url_for("windows_sbc_install_script")
    return [
        {
            "id": "unix",
            "label": "Linux and macOS",
            "command": unix_install_command(request),
        },
        {
            "id": "windows",
            "label": "Windows",
            "command": f"& ([scriptblock]::Create((irm '{windows_url}')))",
        },
    ]


def _get_client_ip(request: Request) -> str:
    client = request.client
    if client is None:
        return "unknown"

    return client.host


def _get_machine_token_manager(request: Request) -> MachineTokenManager:
    return request.app.state.machine_token_manager


def _authenticate_bearer_token(request: Request, token: str) -> str:
    """Validate a Bearer token and return the username, or raise 401."""
    machine_mgr = _get_machine_token_manager(request)
    users = _get_users(request)

    username = machine_mgr.validate_token(token)
    if username is None or username not in users:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return username


def _authenticate_request(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    """Authenticate via Bearer token or Basic auth."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return _authenticate_bearer_token(request, auth_header[7:])

    if credentials is not None:
        return _authenticate_basic_credentials(request, credentials)

    raise HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


def _authenticate_basic_credentials(
    request: Request, credentials: HTTPBasicCredentials
) -> str:
    rate_limiter = _get_rate_limiter(request)
    users = _get_users(request)
    username = credentials.username
    password = credentials.password
    client_ip = _get_client_ip(request)

    # Reject malformed names before the rate limiter records them, so junk
    # usernames cannot fill its table.
    if not USERNAME_PATTERN.match(username):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    if not rate_limiter.check(username, client_ip):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    user = users.get(username)
    if user is not None and _verify_password(password, user.password_hash):
        rate_limiter.reset(username, client_ip)
        return username

    raise HTTPException(
        status_code=401,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


async def _get_file_list(
    archive_index: ArchiveIndex,
    username: str,
    known_usernames: list[str],
) -> list[dict[str, str]]:
    """List release files belonging to a specific user."""
    return [
        _build_file_entry(filename)
        for filename in archive_index.list_filenames(username, known_usernames)
    ]


def _get_vps_info_provider(request: Request) -> VpsInfoProvider | None:
    return request.app.state.vps_info_provider


def _get_vps_hostname(request: Request) -> str | None:
    return request.app.state.vps_hostname


def _get_traffic_stats_provider(request: Request) -> TrafficStatsProvider | None:
    return request.app.state.traffic_stats_provider


def _get_timezone(request: Request) -> str:
    return request.app.state.traffic_timezone


def _get_show_user_domains(request: Request) -> bool:
    return request.app.state.show_user_domains


def _get_relay_multiplier(request: Request) -> float:
    # Read like every sibling accessor rather than through a defaulted getattr.
    # `create_app` sets this unconditionally, so a missing value means the app
    # was built wrong, and answering 2.0 would hide that behind a number that
    # silently disagrees with the configured one.
    return request.app.state.relay_multiplier


def _is_admin_user(request: Request, username: str) -> bool:
    user = _get_users(request).get(username)
    return user is not None and user.admin


def _resolve_session_user(request: Request, session: str | None) -> str | None:
    """Return the username a session cookie authenticates, or None."""
    if not session:
        return None
    username = _get_session_manager(request).validate_token(session)
    if not username or username not in _get_users(request):
        return None
    return username


def _require_admin_username(request: Request, session: str | None) -> str | None:
    """Return the admin this session belongs to.

    ``None`` means "re-render the login page" -- the admin pages answer a bad
    session with the login form rather than a 401, so this cannot be a
    dependency that returns a value. A valid session belonging to a non-admin
    raises 403 here, so every admin page agrees on the rule.
    """
    username = _resolve_session_user(request, session)
    if username is None:
        return None
    if not _is_admin_user(request, username):
        raise HTTPException(status_code=403, detail="Admin access required")
    return username


def _is_known_traffic_user(request: Request, username: str) -> bool:
    """Whether a per-user traffic page should exist for this name.

    The admin table deliberately lists users the collector has seen but the auth
    snapshot no longer has -- someone removed from the inventory still has this
    month's bytes attributed to them. Their rows link somewhere, so those links
    have to resolve. ``updated_at`` is exactly "the database has a row", because
    a summary built from no row leaves it None.
    """
    if username in _get_users(request):
        return True
    provider = _get_traffic_stats_provider(request)
    if provider is None:
        return False
    return provider.get_user_summary(username).updated_at is not None


def _resolve_range_days(value: int | None) -> int:
    """Clamp ``?days=`` to an offered range; anything else is the default."""
    return value if value in TRAFFIC_RANGE_DAYS else DEFAULT_TRAFFIC_RANGE_DAYS


def _build_range_options(base_path: str, selected: int) -> list[dict[str, object]]:
    return [
        {
            "days": days,
            "label": f"{days} 天",
            "active": days == selected,
            "url": f"{base_path}?days={days}",
        }
        for days in TRAFFIC_RANGE_DAYS
    ]


class _HasByteTotals(Protocol):
    """Anything the usage tables can show an upload/download/total row for."""

    @property
    def upload_bytes(self) -> int: ...
    @property
    def download_bytes(self) -> int: ...
    @property
    def total_bytes(self) -> int: ...


def _byte_columns(usage: _HasByteTotals) -> dict[str, str]:
    """The three pre-formatted byte cells every usage table shares.

    The templates are deliberately dumb about byte counts, so each table used to
    repeat the same three ``format_byte_count`` calls; a change to how bytes
    read has to land in one place or the tables stop agreeing with each other.
    """
    return {
        "upload": format_byte_count(usage.upload_bytes),
        "download": format_byte_count(usage.download_bytes),
        "total": format_byte_count(usage.total_bytes),
    }


def _serialize_protocol_rows(
    breakdown: ProtocolUsageBreakdown,
) -> list[dict[str, str]]:
    return [
        {"protocol": usage.protocol, **_byte_columns(usage)}
        for usage in breakdown.protocols
    ]


def _serialize_domain_rows(
    breakdown: DomainUsageBreakdown,
) -> list[dict[str, object]]:
    # Every domain in the window, not just the rows that fit the table, or each
    # share would be a share of the table and the column would always total 100%.
    total = breakdown.window_total_bytes
    return [
        {
            "domain": charts.domain_display_name(usage.domain),
            **_byte_columns(usage),
            "connections": usage.connection_count,
            "share": f"{(usage.total_bytes / total * 100):.1f}%" if total else "-",
            # Lets the template mark the bookkeeping buckets apart from sites.
            "reserved": usage.is_reserved,
        }
        for usage in breakdown.domains
    ]


def _serialize_destination_rows(
    breakdown: DestinationUsageBreakdown,
) -> list[dict[str, object]]:
    return [
        {
            "destination_ip": usage.destination_ip,
            **_byte_columns(usage),
            "connections": usage.connection_count,
        }
        for usage in breakdown.destinations
    ]


def _serialize_live_connections(
    connections: Sequence[LiveConnection], timezone_name: str
) -> list[dict[str, object]]:
    zone = ZoneInfo(timezone_name)
    return [
        {
            "username": item.username,
            "protocol": item.service_name,
            "network": item.network,
            "source": item.source,
            # `destination` already carries the hostname for the protocols this
            # serves, so the resolved `domain` would be a second copy of it.
            "destination": item.destination,
            "outbound": item.outbound,
            **_byte_columns(item),
            "created": (
                ""
                if item.created_at is None
                else item.created_at.astimezone(zone).strftime("%H:%M:%S")
            ),
        }
        for item in connections
    ]


def _format_last_updated(summary: UserTrafficSummary) -> str:
    if summary.updated_at is None:
        return "尚未采集"
    return summary.updated_at.astimezone(ZoneInfo(summary.timezone)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _serialize_traffic_stats(
    summary: UserTrafficSummary,
    *,
    relay_multiplier: float,
    plan_total_bytes: int | None,
) -> dict[str, str]:
    return {
        "cycle_label": f"{summary.cycle_start.isoformat()} ~ {summary.cycle_end.isoformat()}",
        "upload": format_byte_count(summary.upload_bytes),
        "download": format_byte_count(summary.download_bytes),
        "total": format_byte_count(summary.total_bytes),
        "plan_usage": format_plan_usage(
            summary.total_bytes,
            relay_multiplier=relay_multiplier,
            plan_total_bytes=plan_total_bytes,
        ),
        "last_updated": _format_last_updated(summary),
    }


def _portal_release_context(request: Request) -> dict[str, object]:
    release_info: artifacts.ReleaseInfo = request.app.state.release_info
    deployed_at = ""
    deployment_path: Path = request.app.state.deployment_info_path
    if deployment_path.is_file():
        try:
            deployment_info = artifacts.read_deployment_info(deployment_path)
        except (OSError, ValueError):
            logger.warning("Failed to read deployment metadata", exc_info=True)
        else:
            if (
                release_info.release_id is not None
                and deployment_info.release_id == release_info.release_id
            ):
                deployed_at = deployment_info.deployed_at

    upgrade = request.app.state.client_upgrade
    # Bash clients compare this with the build they run. A release that carries
    # sbc names the sbc build, which no bash client runs, so each one sees an
    # upgrade, and `proxy upgrade` moves it to sbc.
    downloads = request.app.state.client_downloads
    client_build_id = (
        downloads.build_id
        if downloads is not None
        else release_info.client_build_id or ""
    )
    notice_source = (f"{client_build_id}\0{upgrade.policy}\0{upgrade.message}").encode()
    return {
        "commit_sha": release_info.commit_sha,
        "dirty": release_info.dirty,
        "deployed_at": deployed_at,
        "client_build_id": client_build_id,
        "upgrade_policy": upgrade.policy,
        "upgrade_message": upgrade.message,
        "notice_id": hashlib.sha256(notice_source).hexdigest(),
    }


def _serialize_usage_summary(
    summary: UserTrafficSummary,
    *,
    relay_multiplier: float,
    plan_total_bytes: int | None,
) -> dict[str, str | int | float | None]:
    """Serialize one user's cycle usage for ``/api/usage``.

    ``upload_bytes``/``download_bytes``/``total_bytes`` stay exactly as sing-box
    measured them, because a user can check those against their own client and
    an admin can check them against the SQLite tables. ``billed_bytes`` is the
    derived projection onto the host's counter and is reported alongside the
    multiplier that produced it, so nobody has to guess where it came from.
    """
    return {
        "username": summary.username,
        "cycle_month": summary.cycle_month,
        "cycle_start": summary.cycle_start.isoformat(),
        "cycle_end": summary.cycle_end.isoformat(),
        "timezone": summary.timezone,
        "upload_bytes": summary.upload_bytes,
        "download_bytes": summary.download_bytes,
        "total_bytes": summary.total_bytes,
        "relay_multiplier": relay_multiplier,
        "billed_bytes": round(summary.total_bytes * relay_multiplier),
        "plan_total_bytes": plan_total_bytes,
        "updated_at": None
        if summary.updated_at is None
        else summary.updated_at.isoformat(),
        "error": None,
    }


def _serialize_admin_traffic_stats(
    summaries: tuple[UserTrafficSummary, ...],
    *,
    relay_multiplier: float,
    plan_total_bytes: int | None,
) -> dict[str, object]:
    updated_at_values = [
        summary.updated_at for summary in summaries if summary.updated_at
    ]
    latest_updated_at = max(updated_at_values) if updated_at_values else None
    last_updated = "尚未采集"
    if latest_updated_at is not None and summaries:
        last_updated = latest_updated_at.astimezone(
            ZoneInfo(summaries[0].timezone)
        ).strftime("%Y-%m-%d %H:%M:%S")

    cycle_label = ""
    if summaries:
        first = summaries[0]
        cycle_label = f"{first.cycle_start.isoformat()} ~ {first.cycle_end.isoformat()}"

    total_bytes = sum(summary.total_bytes for summary in summaries)
    return {
        "cycle_label": cycle_label,
        "last_updated": last_updated,
        "upload": format_byte_count(sum(summary.upload_bytes for summary in summaries)),
        "download": format_byte_count(
            sum(summary.download_bytes for summary in summaries)
        ),
        "total": format_byte_count(total_bytes),
        # The figure an admin reconciles against the VPS counter. Summing the
        # measured per-user totals and comparing that against the host's own
        # number is exactly where the phantom 2x shortfall shows up.
        "plan_usage": format_plan_usage(
            total_bytes,
            relay_multiplier=relay_multiplier,
            plan_total_bytes=plan_total_bytes,
        ),
        "users": [
            {
                "username": summary.username,
                "upload": format_byte_count(summary.upload_bytes),
                "download": format_byte_count(summary.download_bytes),
                "total": format_byte_count(summary.total_bytes),
                "last_updated": _format_last_updated(summary),
                "detail_url": f"/admin/users/{summary.username}",
            }
            for summary in summaries
        ],
    }


def subscription_url(request: Request, username: str) -> str | None:
    """Return the user's current subscription link, or None when they have none."""
    service = request.app.state.subscriptions
    if service is None or not service.holds_protocols(username):
        return None
    return str(request.url_for("subscription", token=service.token_for(username)))


async def _build_file_list_context(
    request: Request,
    *,
    username: str,
    archive_index: ArchiveIndex,
    known_usernames: list[str],
) -> dict[str, object]:
    context: dict[str, object] = {
        "request": request,
        "files": await _get_file_list(archive_index, username, known_usernames),
        "is_admin": _is_admin_user(request, username),
        "portal_release": _portal_release_context(request),
        # Reads the counter file, which is blocking I/O.
        "subscription_url": await run_in_threadpool(
            subscription_url, request, username
        ),
        "install_targets": _build_install_targets(request),
    }
    provider = _get_traffic_stats_provider(request)
    if provider is not None:
        context["traffic_stats"] = _serialize_traffic_stats(
            provider.get_user_summary(username),
            relay_multiplier=_get_relay_multiplier(request),
            plan_total_bytes=await _resolve_plan_total_bytes(request),
        )
        # Off by default: this shows people which sites their own traffic went
        # to, which is theirs to see but is still a change in what the page
        # discloses, so the operator opts in.
        if _get_show_user_domains(request):
            context["domain_rows"] = _serialize_domain_rows(
                provider.get_domain_breakdown(
                    username,
                    days=DEFAULT_TRAFFIC_RANGE_DAYS,
                    limit=USER_DOMAIN_TABLE_LIMIT,
                )
            )
    return context


async def _login_context(
    request: Request, error: str | None = None
) -> dict[str, object]:
    """Build template context for login.html, including optional VPS info."""
    ctx: dict[str, object] = {
        "request": request,
        "install_targets": _build_install_targets(request),
        "portal_release": _portal_release_context(request),
    }
    vps_hostname = _get_vps_hostname(request)
    if vps_hostname is not None:
        ctx["vps_hostname"] = vps_hostname
    if error:
        ctx["error"] = error
    provider = _get_vps_info_provider(request)
    if provider is not None:
        ctx["vps_info"] = await provider.get_info()
    return ctx


async def _admin_context(request: Request, *, days: int) -> dict[str, object]:
    users = _get_users(request)
    provider = _get_traffic_stats_provider(request)
    context: dict[str, object] = {
        "request": request,
        "portal_release": _portal_release_context(request),
        "traffic_stats_available": provider is not None,
        "selected_days": days,
        "range_options": _build_range_options("/admin", days),
    }
    if provider is None:
        return context

    summaries = provider.get_all_user_summaries(sorted(users))
    context["admin_traffic_stats"] = _serialize_admin_traffic_stats(
        summaries,
        relay_multiplier=_get_relay_multiplier(request),
        plan_total_bytes=await _resolve_plan_total_bytes(request),
    )
    # Charts are markup, not data: rendering them here keeps the template as
    # dumb as it already is about pre-formatted byte counts.
    domains = provider.get_domain_breakdown(days=days, limit=DOMAIN_TABLE_LIMIT)
    context["domain_rows"] = _serialize_domain_rows(domains)
    context["charts"] = {
        "ranking": charts.render_user_ranking_chart(
            summaries,
            detail_url_for=lambda username: f"/admin/users/{username}",
        ),
        "trend": charts.render_daily_trend_chart(provider.get_daily_series(days=days)),
        "protocols": charts.render_protocol_split_chart(
            provider.get_protocol_breakdown()
        ),
        "domains": charts.render_domain_ranking_chart(domains),
    }
    return context


async def _admin_user_context(
    request: Request, username: str, *, days: int
) -> dict[str, object]:
    provider = _get_traffic_stats_provider(request)
    context: dict[str, object] = {
        "request": request,
        "portal_release": _portal_release_context(request),
        "username": username,
        "traffic_stats_available": provider is not None,
        "selected_days": days,
        "range_options": _build_range_options(f"/admin/users/{username}", days),
    }
    if provider is None:
        return context

    breakdown = provider.get_protocol_breakdown(username)
    context["traffic_stats"] = _serialize_traffic_stats(
        provider.get_user_summary(username),
        relay_multiplier=_get_relay_multiplier(request),
        plan_total_bytes=await _resolve_plan_total_bytes(request),
    )
    context["protocol_rows"] = _serialize_protocol_rows(breakdown)
    domains = provider.get_domain_breakdown(
        username, days=days, limit=DOMAIN_TABLE_LIMIT
    )
    context["domain_rows"] = _serialize_domain_rows(domains)
    context["destination_rows"] = _serialize_destination_rows(
        provider.get_destination_breakdown(
            username, days=days, limit=DOMAIN_TABLE_LIMIT
        )
    )
    context["charts"] = {
        "trend": charts.render_daily_trend_chart(
            provider.get_daily_series(username, days=days)
        ),
        "protocols": charts.render_protocol_split_chart(breakdown),
        "months": charts.render_monthly_history_chart(
            provider.get_monthly_history(username, months=MONTHLY_HISTORY_MONTHS)
        ),
        "domains": charts.render_domain_ranking_chart(domains),
    }
    return context


@router.get("/api/vps-info")
async def get_vps_info(request: Request):
    payload: dict[str, object] = {
        "hostname": _get_vps_hostname(request),
        "relay_multiplier": _get_relay_multiplier(request),
    }
    provider = _get_vps_info_provider(request)
    if provider is None:
        payload["error"] = "unavailable"
        return JSONResponse(payload)

    payload.update(asdict(await provider.get_info()))
    return JSONResponse(payload)


@router.get("/api/client-update")
async def get_client_update(request: Request):
    release = _portal_release_context(request)
    lines = (
        "schema=1",
        f"client_build_id={release['client_build_id']}",
        f"commit_sha={release['commit_sha']}",
        f"dirty={'true' if release['dirty'] else 'false'}",
        f"deployed_at={release['deployed_at']}",
        f"policy={release['upgrade_policy']}",
        f"notice_id={release['notice_id']}",
        f"message={release['upgrade_message']}",
    )
    return PlainTextResponse("\n".join(lines) + "\n")


async def _resolve_plan_total_bytes(request: Request) -> int | None:
    """The plan's monthly allowance in bytes, or ``None`` when unknown.

    Carried in the usage payload so ``proxy check quota`` can render the plan
    share from a single request instead of correlating two endpoints, and so
    both sides of the comparison are raw byte counts rather than one figure
    pre-rounded to gigabytes. Reads ``bandwidth_total_bytes`` for that reason:
    ``bandwidth_total_gb`` is a display value, rounded to two decimals before
    it ever reaches here. The provider caches successes for an hour and
    failures for a minute, so a call here is normally free and an outage costs
    one timeout a minute rather than one per request.
    """
    provider = _get_vps_info_provider(request)
    if provider is None:
        return None

    info = await provider.get_info()
    if info.error is not None or not info.bandwidth_total_bytes:
        return None

    return info.bandwidth_total_bytes


@router.get("/api/usage")
async def get_usage(
    request: Request,
    username: str = Depends(_authenticate_request),
):
    provider = _get_traffic_stats_provider(request)
    if provider is None:
        return JSONResponse({"error": "unavailable"})

    payload: dict[str, object] = dict(
        _serialize_usage_summary(
            provider.get_user_summary(username),
            relay_multiplier=_get_relay_multiplier(request),
            plan_total_bytes=await _resolve_plan_total_bytes(request),
        )
    )
    # Raw byte counts and raw hostnames, unlike the pre-formatted table the
    # templates get: this feeds `proxy check quota` on the client side.
    payload["top_domains"] = [
        {
            "domain": usage.domain,
            "upload_bytes": usage.upload_bytes,
            "download_bytes": usage.download_bytes,
            "total_bytes": usage.total_bytes,
            "connection_count": usage.connection_count,
        }
        for usage in provider.get_domain_breakdown(
            username, days=DEFAULT_TRAFFIC_RANGE_DAYS, limit=USER_DOMAIN_TABLE_LIMIT
        ).domains
    ]
    return JSONResponse(payload)


@router.get("/")
async def login_page(request: Request):
    ctx = await _login_context(request)
    return request.app.state.templates.TemplateResponse(request, "login.html", ctx)


@router.post("/login")
async def handle_login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    templates = request.app.state.templates
    session_mgr = _get_session_manager(request)
    rate_limiter = _get_rate_limiter(request)
    users = _get_users(request)
    archive_index = _get_archive_index(request)
    client_ip = _get_client_ip(request)

    # Validate the username format before the rate limiter records it, so junk
    # usernames cannot fill its table.
    if not USERNAME_PATTERN.match(username):
        logger.info("Invalid username format attempt")
        ctx = await _login_context(request, error="Invalid username or password")
        return templates.TemplateResponse(request, "login.html", ctx)

    # Check rate limiting
    if not rate_limiter.check(username, client_ip):
        logger.warning(f"Rate limit exceeded for IP: {client_ip}")
        ctx = await _login_context(
            request, error="Too many attempts. Please try again later."
        )
        return templates.TemplateResponse(request, "login.html", ctx)

    # Argon2 takes a large fraction of a second and 64 MiB by design, so it runs
    # in the threadpool rather than stalling every other request on the loop.
    user = users.get(username)
    if user is not None and await run_in_threadpool(
        _verify_password, password, user.password_hash
    ):
        logger.info("Successful login")

        session_token = session_mgr.create_token(username)
        rate_limiter.reset(username, client_ip)

        resp = templates.TemplateResponse(
            request,
            "file_list.html",
            await _build_file_list_context(
                request,
                username=username,
                archive_index=archive_index,
                known_usernames=list(users),
            ),
        )
        resp.set_cookie(
            key="session",
            value=session_token,
            httponly=True,
            secure=_session_cookie_secure(request),
            samesite="lax",
            max_age=3600,
        )
        return resp

    logger.info("Failed login attempt")
    ctx = await _login_context(request, error="Invalid username or password")
    return templates.TemplateResponse(request, "login.html", ctx)


@router.get("/files")
async def show_file_list(
    request: Request,
    username: str | None = None,
    session: str | None = Cookie(None),
):
    templates = request.app.state.templates
    archive_index = _get_archive_index(request)
    users = _get_users(request)

    username = _resolve_session_user(request, session)
    if username is None:
        ctx = await _login_context(request, error="Session expired")
        return templates.TemplateResponse(request, "login.html", ctx)

    return templates.TemplateResponse(
        request,
        "file_list.html",
        await _build_file_list_context(
            request,
            username=username,
            archive_index=archive_index,
            known_usernames=list(users),
        ),
    )


@router.get("/files/{filename:path}")
async def download_file(
    request: Request, filename: str, session: str | None = Cookie(None)
):
    archive_index = _get_archive_index(request)
    users = _get_users(request)

    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")

    # A session outlives the user's removal from the inventory, so check that
    # the user still exists rather than trusting the signature alone.
    username = _resolve_session_user(request, session)
    if username is None:
        raise HTTPException(status_code=401, detail="Session expired")

    # Prevent path traversal
    if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    archive_owner = resolve_user_archive_owner(filename, users)
    if archive_owner is not None and archive_owner != username:
        raise HTTPException(status_code=403, detail="Access denied")

    # Authorisation is settled before anything is read or assembled.
    resolved = archive_index.resolve(username, filename, users)
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found")

    archive = await run_in_threadpool(archive_index.open, username, resolved)
    return _archive_response(archive, resolved, request.headers.get("range"))


@router.get("/admin")
async def admin_traffic_stats(
    request: Request,
    days: int | None = None,
    session: str | None = Cookie(None),
):
    templates = request.app.state.templates

    if _require_admin_username(request, session) is None:
        ctx = await _login_context(request, error="Session expired")
        return templates.TemplateResponse(request, "login.html", ctx)

    return templates.TemplateResponse(
        request,
        "admin_traffic.html",
        await _admin_context(request, days=_resolve_range_days(days)),
    )


@router.get("/admin/connections")
async def admin_connections(
    request: Request,
    session: str | None = Cookie(None),
):
    """Live connections, for debugging rather than accounting.

    Refreshed with a meta tag rather than a poll, because the portal ships no
    JavaScript on purpose -- see web/charts.py for the reasoning.
    """
    templates = request.app.state.templates

    if _require_admin_username(request, session) is None:
        ctx = await _login_context(request, error="Session expired")
        return templates.TemplateResponse(request, "login.html", ctx)

    provider = _get_traffic_stats_provider(request)
    context: dict[str, object] = {
        "request": request,
        "portal_release": _portal_release_context(request),
        "traffic_stats_available": provider is not None,
        "refresh_seconds": LIVE_VIEW_REFRESH_SECONDS,
        "connection_rows": [],
    }
    if provider is not None:
        context["connection_rows"] = _serialize_live_connections(
            provider.get_live_connections(limit=LIVE_CONNECTION_TABLE_LIMIT),
            _get_timezone(request),
        )

    return templates.TemplateResponse(request, "admin_connections.html", context)


@router.get("/admin/users/{username}")
async def admin_user_traffic(
    request: Request,
    username: str,
    days: int | None = None,
    session: str | None = Cookie(None),
):
    templates = request.app.state.templates

    if _require_admin_username(request, session) is None:
        ctx = await _login_context(request, error="Session expired")
        return templates.TemplateResponse(request, "login.html", ctx)

    if not _is_known_traffic_user(request, username):
        raise HTTPException(status_code=404, detail="User not found")

    return templates.TemplateResponse(
        request,
        "admin_user_traffic.html",
        await _admin_user_context(request, username, days=_resolve_range_days(days)),
    )


@router.post("/api/token")
async def create_machine_token(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    # The Basic-auth check runs Argon2, so keep it off the event loop.
    username = await run_in_threadpool(
        _authenticate_basic_credentials, request, credentials
    )
    machine_mgr = _get_machine_token_manager(request)
    token = machine_mgr.create_token(username)
    return {"token": token, "username": username}


@router.get("/auth")
async def authenticate(
    request: Request,
    username: str = Depends(_authenticate_request),
):
    return {"authenticated": True}


@router.get("/download")
async def download_platform_archive(
    request: Request,
    platform: str,
    username: str = Depends(_authenticate_request),
):
    archive_index = _get_archive_index(request)
    users = _get_users(request)

    resolved = archive_index.resolve_for_platform(username, platform, users)
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found")

    # Assembly is a byte copy plus a few KB of compression, but it still does
    # blocking I/O, and every handler here is async.
    archive = await run_in_threadpool(archive_index.open, username, resolved)
    return _archive_response(archive, resolved, request.headers.get("range"))


@router.get("/logout")
async def logout(response: Response):
    response.delete_cookie(key="session")
    return {"message": "Logged out successfully"}
