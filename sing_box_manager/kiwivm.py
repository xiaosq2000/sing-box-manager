"""KiwiVM (BandwagonHost) API client shared by the portal and the collector.

Two callers need this API for different reasons and from different execution
models: the portal renders the bandwidth card from an async request handler,
while ``sbm stats-collect`` derives the billing-cycle boundary from a plain
synchronous process. Both fetch shapes live here so the URL, the timeout, and
the error contract have exactly one definition, and so ``traffic_stats`` never
has to import from ``sing_box_manager.web``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

if TYPE_CHECKING:
    from sing_box_manager.settings import VpsInfoSettings

SERVICE_INFO_URL = "https://api.64clouds.com/v1/getServiceInfo"
REQUEST_TIMEOUT_SECONDS = 10


class KiwiVmError(RuntimeError):
    """The KiwiVM API was reachable but reported a failure."""


def resolve_credentials(
    vps_info: VpsInfoSettings | None = None,
) -> tuple[str, str] | None:
    """VEID and API key from settings, falling back to the environment.

    Returns ``None`` when either half is missing, which is the ordinary state of
    a deployment that never configured ``vps_info``. Callers treat that as "no
    VPS data available", not as an error.
    """
    if vps_info is not None:
        veid = vps_info.kiwi_veid.strip()
        api_key = vps_info.kiwi_api_key.strip()
        if veid and api_key:
            return veid, api_key

    veid = os.getenv("KIWI_VEID", "").strip()
    api_key = os.getenv("KIWI_API_KEY", "").strip()
    if not veid or not api_key:
        return None
    return veid, api_key


def _validate(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise KiwiVmError("KiwiVM returned a non-object payload")
    if payload.get("error", 0) != 0:
        raise KiwiVmError(str(payload["error"]))
    return payload


def _redacted_failure(exc: Exception) -> KiwiVmError:
    """A failure that is safe to log.

    The API takes its key in the query string, and httpx puts the full URL in
    an HTTPStatusError's message. Both callers log failures with a traceback, so
    passing the original exception on would write a key that can stop or
    reinstall the VPS into the journal. Callers raise this ``from None`` so the
    original does not ride along as the traceback's context either.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return KiwiVmError(
            f"KiwiVM request failed with HTTP {exc.response.status_code}"
        )
    if isinstance(exc, httpx.HTTPError):
        return KiwiVmError(f"KiwiVM request failed: {type(exc).__name__}")
    return KiwiVmError("KiwiVM returned a response that is not JSON")


def fetch_service_info(veid: str, api_key: str) -> dict:
    """Synchronous ``getServiceInfo``, for the stats collector."""
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.get(
                SERVICE_INFO_URL, params={"veid": veid, "api_key": api_key}
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _redacted_failure(exc) from None
    return _validate(payload)


async def fetch_service_info_async(veid: str, api_key: str) -> dict:
    """Asynchronous ``getServiceInfo``, for the portal."""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(
                SERVICE_INFO_URL, params={"veid": veid, "api_key": api_key}
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _redacted_failure(exc) from None
    return _validate(payload)


def reset_datetime(raw: object, timezone_name: str) -> datetime | None:
    """A KiwiVM reset timestamp as a local datetime, or ``None`` if unusable.

    The portal prints this date on the bandwidth card and the collector takes its
    day-of-month as the billing-cycle boundary; the two sit side by side on the
    page and must name the same day, so they share one parse rather than two that
    can drift.

    Returns ``None`` for anything unusable rather than guessing. The day ends up
    as a database bucket key, so a malformed payload must never be able to re-key
    stored usage, and the card must not claim a reset in 1970.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=ZoneInfo(timezone_name))
    except (OverflowError, OSError, ValueError):
        return None


def billing_day_from_service_info(
    data: Mapping[str, object], timezone_name: str
) -> int | None:
    """The day of the month the VPS bandwidth counter resets on.

    Only the day-of-month is kept. ``data_next_reset`` names one specific future
    reset, but the day it lands on is the stable, repeating fact -- so reading
    just the day stays correct even if the timestamp itself has gone stale.
    """
    moment = reset_datetime(data.get("data_next_reset"), timezone_name)
    return None if moment is None else moment.day
