"""FastAPI application factory."""

import hashlib
import logging
import socket
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sing_box_manager.release import artifacts
from sing_box_manager.release.client_downloads import load_client_downloads
from sing_box_manager.runtime_env import load_local_env
from sing_box_manager.settings import Settings, load_auth_snapshot
from sing_box_manager.subscription import create_subscription_service
from sing_box_manager.traffic_stats import create_traffic_stats_provider
from sing_box_manager.web.archives import build_archive_index
from sing_box_manager.web.auth import MachineTokenManager, RateLimiter, SessionManager
from sing_box_manager.web.routes import router
from sing_box_manager.web.subscriptions import router as subscription_router
from sing_box_manager.web.vps_info import create_vps_info_provider

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent
PORTAL_DYNAMIC_CACHE_CONTROL = "no-store"
STATIC_ASSET_CACHE_CONTROL = "no-cache, must-revalidate"


def _resolve_project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return artifacts.PROJECT_ROOT / candidate


def _resolve_allowed_hosts(
    settings: Settings, deployment_host: str | None = None
) -> list[str]:
    allowed_hosts: list[str] = []

    for host in [*settings.web.allowed_hosts, deployment_host or ""]:
        normalized = host.strip()
        if normalized and normalized not in allowed_hosts:
            allowed_hosts.append(normalized)

    return allowed_hosts


def _validate_release_dir(release_dir: Path) -> None:
    if not release_dir.exists():
        raise RuntimeError(f"Release directory not found: {release_dir}")

    if not release_dir.is_dir():
        raise RuntimeError(f"Release path is not a directory: {release_dir}")

    try:
        next(release_dir.iterdir(), None)
    except PermissionError as exc:
        raise RuntimeError(f"Release directory is not readable: {release_dir}") from exc


def _build_versioned_static_asset_url(asset_path: str) -> str:
    normalized_path = asset_path.lstrip("/")
    digest = hashlib.sha256(
        (_WEB_DIR / "static" / normalized_path).read_bytes()
    ).hexdigest()
    return f"/static/{normalized_path}?v={digest[:12]}"


def _resolve_vps_hostname() -> str | None:
    try:
        hostname = socket.gethostname().strip()
    except OSError:
        logger.warning("Failed to resolve VPS hostname", exc_info=True)
        return None

    return hostname or None


def create_app(settings: Settings) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI()
    deployment_host = artifacts.resolve_deployment_host(settings)

    # Security middleware
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=_resolve_allowed_hosts(settings, deployment_host),
    )

    @app.middleware("http")
    async def set_cache_control_headers(request: Request, call_next):
        """Default-deny caching: only ``/static/`` assets are cacheable.

        This used to be an allowlist of exact paths, which failed twice over. It
        was maintained by hand, so the macOS installer once shipped cacheable;
        and matching by exact path meant a parameterized route could never be
        listed at all, leaving ``/files/{filename}`` uncovered and any future
        ``/admin/users/{username}`` silently cacheable by an intermediary.

        Every non-static response this portal serves is per-user and private, so
        inverting the default makes the safe case automatic and retires the list.
        """
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = STATIC_ASSET_CACHE_CONTROL
        else:
            response.headers["Cache-Control"] = PORTAL_DYNAMIC_CACHE_CONTROL
        return response

    # Static files and templates
    app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=_WEB_DIR / "templates")
    portal_stylesheet_url = _build_versioned_static_asset_url("styles.css")
    cast("dict[str, object]", templates.env.globals)["portal_stylesheet_url"] = (
        portal_stylesheet_url
    )
    app.state.templates = templates
    app.state.portal_stylesheet_url = portal_stylesheet_url

    # Application state
    app.state.vps_hostname = _resolve_vps_hostname()
    session_secret = settings.web.session_secret
    if session_secret is None:
        # A generated secret would sign everyone out, installer machine tokens
        # included, at every restart.
        raise RuntimeError(
            "web.session_secret must be set: it signs browser sessions and "
            "installer machine tokens"
        )
    app.state.session_manager = SessionManager(session_secret)
    app.state.machine_token_manager = MachineTokenManager(session_secret)
    app.state.rate_limiter = RateLimiter()
    app.state.default_protocol = settings.default_protocol
    app.state.client_upgrade = settings.client_upgrade
    app.state.deployment_info_path = _resolve_project_path(
        settings.deployment_info_path
    )
    app.state.release_dir = artifacts.resolve_release_dir(settings)
    _validate_release_dir(app.state.release_dir)
    app.state.release_info = artifacts.read_release_info(
        _resolve_project_path(settings.release_info_path)
    )
    # Picks the manifest-driven backend when the release has one and the
    # prebuilt-file backend when it does not, so an older release keeps serving.
    app.state.archive_index = build_archive_index(app.state.release_dir)
    app.state.users = load_auth_snapshot(
        _resolve_project_path(settings.auth_snapshot_path)
    )

    if not app.state.users:
        raise RuntimeError("No enabled auth users in auth snapshot")

    load_local_env()
    app.state.vps_info_provider = create_vps_info_provider(
        settings.vps_info, timezone_name=settings.traffic_stats.timezone
    )
    app.state.traffic_stats_provider = create_traffic_stats_provider(settings)
    app.state.relay_multiplier = settings.traffic_stats.relay_multiplier
    app.state.traffic_timezone = settings.traffic_stats.timezone
    app.state.show_user_domains = settings.traffic_stats.show_user_domains
    app.state.subscriptions = create_subscription_service(settings)
    app.state.client_downloads = load_client_downloads(app.state.release_dir)
    if app.state.subscriptions is None:
        logger.warning(
            "web.subscription_secret is unset, so subscription links are off"
        )

    logger.info("Authentication service ready")

    # Routes
    app.include_router(router)
    app.include_router(subscription_router)

    return app
