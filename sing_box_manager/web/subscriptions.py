"""The subscription endpoint sbc fetches its config from."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from sing_box_manager.release.client_downloads import (
    CLIENT_MANIFEST_NAME,
    CLIENT_SIGNATURE_NAME,
    ClientDownloads,
)
from sing_box_manager.release.renderers import SUPPORTED_CLIENT_OS
from sing_box_manager.subscription import (
    SubscriptionService,
    build_envelope,
    encode_envelope,
    parse_version,
)
from sing_box_manager.web.routes import (
    _authenticate_request,
    _resolve_session_user,
    subscription_url,
)

router = APIRouter()

INSTALL_SCRIPT = Path(__file__).resolve().parent / "client" / "install.sh"


def _usage_header(request: Request, username: str) -> str | None:
    """Report this billing cycle's traffic the way subscription clients read it.

    Per-user quota and expiry arrive with plans, so only usage is known.
    """
    provider = request.app.state.traffic_stats_provider
    if provider is None:
        return None
    summary = provider.get_user_summary(username)
    return f"upload={summary.upload_bytes}; download={summary.download_bytes}"


def _matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    candidates = {
        value.strip().removeprefix("W/") for value in if_none_match.split(",")
    }
    return etag in candidates or "*" in candidates


@router.get("/sub/{token}", name="subscription")
def get_subscription(
    request: Request,
    token: str,
    output_format: str | None = Query(None, alias="format"),
    os_name: str | None = Query(None, alias="os"),
    sing_box: str | None = Query(None, alias="sing-box"),
) -> Response:
    service: SubscriptionService | None = request.app.state.subscriptions
    if service is None:
        raise HTTPException(status_code=404)
    username = service.find(token, request.app.state.users)
    if username is None:
        raise HTTPException(status_code=404)

    if output_format != "sbc":
        raise HTTPException(status_code=400, detail="format must be sbc")
    if os_name not in SUPPORTED_CLIENT_OS:
        raise HTTPException(
            status_code=400,
            detail=f"os must be one of {', '.join(SUPPORTED_CLIENT_OS)}",
        )
    client_version = parse_version(sing_box or "")
    if client_version is None:
        raise HTTPException(
            status_code=400, detail="sing-box must name the client's sing-box version"
        )
    minimum = service.minimum_sing_box_version
    minimum_version = parse_version(minimum)
    if minimum_version is not None and client_version < minimum_version:
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"This config needs sing-box {minimum} or later",
                "sing_box_minimum": minimum,
            },
        )

    downloads: ClientDownloads | None = request.app.state.client_downloads
    envelope = build_envelope(
        service,
        username,
        os_name=os_name,
        sbc_version=None if downloads is None else downloads.sbc_version,
    )
    if envelope is None:
        raise HTTPException(status_code=404)
    body, etag = encode_envelope(envelope)
    headers = {"ETag": etag, "Referrer-Policy": "no-referrer"}
    usage = _usage_header(request, username)
    if usage is not None:
        headers["subscription-userinfo"] = usage
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.post("/subscription/reset")
def reset_subscription(request: Request, session: str | None = Cookie(None)):
    """Replace the signed-in user's link, which cuts off the old one.

    The session cookie is SameSite=Lax, so another site cannot post this form
    with it.
    """
    username = _resolve_session_user(request, session)
    if username is None:
        return RedirectResponse("/", status_code=303)
    service: SubscriptionService | None = request.app.state.subscriptions
    if service is None or not service.holds_protocols(username):
        raise HTTPException(status_code=404)
    service.reset(username)
    return RedirectResponse("/files", status_code=303)


@router.post("/api/sub")
async def exchange_for_subscription(
    request: Request, username: str = Depends(_authenticate_request)
) -> dict[str, str]:
    """Return the caller's link for a machine token, so migration needs no paste."""
    url = await run_in_threadpool(subscription_url, request, username)
    if url is None:
        raise HTTPException(status_code=404)
    return {"url": url, "username": username}


@router.get("/sub/{token}/files/{name:path}")
def get_subscription_file(request: Request, token: str, name: str) -> Response:
    """Serve one file the signed client manifest lists, and nothing else."""
    service: SubscriptionService | None = request.app.state.subscriptions
    if service is None or service.find(token, request.app.state.users) is None:
        raise HTTPException(status_code=404)
    downloads: ClientDownloads | None = request.app.state.client_downloads
    path = None if downloads is None else downloads.path_for(name)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404)
    media_type = (
        "application/json"
        if name == CLIENT_MANIFEST_NAME
        else "text/plain"
        if name == CLIENT_SIGNATURE_NAME
        else "application/octet-stream"
    )
    return FileResponse(
        path, media_type=media_type, headers={"Referrer-Policy": "no-referrer"}
    )


# The bash client's `proxy upgrade` runs whatever /install/linux.sh or
# /install/macos.sh serves, so those paths serve install.sh too, which moves
# that client to sbc.
@router.get("/install.sh", name="install_script")
@router.get("/install/linux.sh")
@router.get("/install/macos.sh")
def get_install_script() -> Response:
    """Serve the installer that sets sbc up from a subscription link."""
    return Response(
        content=INSTALL_SCRIPT.read_text(encoding="utf-8"),
        media_type="text/plain; charset=utf-8",
    )


@router.get("/install.ps1", name="windows_sbc_install_script")
@router.get("/install/windows.ps1", name="windows_install_script")
def get_windows_sbc_install_script() -> Response:
    """Serve the PowerShell 5.1 bootstrapper and legacy-client migration."""
    return Response(
        content=INSTALL_SCRIPT.with_suffix(".ps1").read_text(encoding="utf-8-sig"),
        media_type="text/plain; charset=utf-8",
        headers={"content-disposition": 'inline; filename="install.ps1"'},
    )
