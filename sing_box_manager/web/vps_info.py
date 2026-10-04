"""KiwiVM VPS info provider with time-based caching."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sing_box_manager import kiwivm

if TYPE_CHECKING:
    from sing_box_manager.settings import VpsInfoSettings

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1_073_741_824
_DEFAULT_CACHE_TTL = 3600  # 1 hour
# A failure is cached too, or an outage would cost every request a fresh
# `kiwivm.REQUEST_TIMEOUT_SECONDS` stall. Far shorter than the success TTL so a
# recovered API is picked up in a minute rather than an hour.
_DEFAULT_ERROR_CACHE_TTL = 60

LOCATION_MAP: dict[str, str] = {
    "USCA_2": "US, Los Angeles",
    "USCA_3": "US, Los Angeles",
    "USCA_4": "US, Los Angeles",
    "USCA_6": "US, Los Angeles",
    "USCA_8": "US, Los Angeles",
    "USCA_9": "US, Los Angeles",
    "USCA_FMT": "US, Fremont",
    "USNJ": "US, New Jersey",
    "USNY_2": "US, New York",
    "USAZ_2": "US, Phoenix",
    "USWA_1": "US, Seattle",
    "EUNL_3": "EU, Amsterdam",
    "EUNL_9": "EU, Amsterdam",
    "CABC_1": "CA, British Columbia",
    "CABC_6": "CA, Vancouver",
    "JPOS_1": "JP, Osaka",
    "JPTYO_1": "JP, Tokyo",
    "APTS_1": "AU, Sydney",
    "HKHK_8": "HK, Hong Kong",
    "SGSG_1": "SG, Singapore",
    "KRSE_1": "KR, Seoul",
    "DUBAI_1": "AE, Dubai",
    "ZAZA_1": "ZA, Johannesburg",
    "GBLN_1": "GB, London",
}


@dataclass(frozen=True)
class VpsInfo:
    """Sanitized VPS info for template rendering."""

    location: str | None = None
    bandwidth_used_gb: float | None = None
    bandwidth_total_gb: float | None = None
    bandwidth_total_bytes: int | None = None
    bandwidth_reset_date: str | None = None
    error: str | None = None


def _format_location(raw: str) -> str:
    return LOCATION_MAP.get(raw, raw)


def _bytes_to_gb(b: float) -> float:
    return round(b / BYTES_PER_GB, 2)


def _raw_bytes(value: object) -> int | None:
    """A byte count as KiwiVM reported it, before the display rounding.

    `proxy check quota` divides the plan allowance into a byte-exact user
    total, so it needs the figure `bandwidth_total_gb` was rounded from:
    re-inflating two decimal places of gigabytes quantizes the denominator to
    about 10 MB. Returns ``None`` for anything non-numeric, as the payload is
    validated for shape but not for field types.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value)


def _unix_to_date(ts: object, timezone_name: str) -> str | None:
    """Format a reset timestamp in the billing timezone.

    Rendering in UTC would print a different day than the cycle boundary the
    portal shows right beside it, for eight hours out of every day in
    Asia/Shanghai. Returns ``None`` for an absent or unusable timestamp so the
    card omits the date instead of claiming a reset in 1970.
    """
    moment = kiwivm.reset_datetime(ts, timezone_name)
    return None if moment is None else moment.strftime("%Y-%m-%d")


def _parse_service_info(data: dict, timezone_name: str) -> VpsInfo:
    return VpsInfo(
        location=_format_location(data.get("node_location", "Unknown")),
        bandwidth_used_gb=_bytes_to_gb(data.get("data_counter", 0)),
        bandwidth_total_gb=_bytes_to_gb(data.get("plan_monthly_data", 0)),
        bandwidth_total_bytes=_raw_bytes(data.get("plan_monthly_data")),
        bandwidth_reset_date=_unix_to_date(data.get("data_next_reset"), timezone_name),
    )


class VpsInfoProvider:
    """Fetches and caches VPS info from the KiwiVM API.

    Every reading is cached, failures included, and concurrent refreshes are
    collapsed behind one lock. Both matter because `/api/usage` calls this on
    the request path: without them a KiwiVM outage would charge each poll a
    fresh 10 s timeout, and a cold cache would let N simultaneous clients fire
    N requests at an API that answers them all identically.
    """

    def __init__(
        self,
        veid: str,
        api_key: str,
        *,
        timezone_name: str,
        cache_ttl: int = _DEFAULT_CACHE_TTL,
        error_cache_ttl: int = _DEFAULT_ERROR_CACHE_TTL,
    ):
        self._veid = veid
        self._api_key = api_key
        self._timezone_name = timezone_name
        self._cache_ttl = cache_ttl
        self._error_cache_ttl = error_cache_ttl
        self._cache: VpsInfo | None = None
        self._cache_expiry: float = 0.0
        self._lock = asyncio.Lock()

    async def get_info(self) -> VpsInfo:
        cached = self._fresh_cache()
        if cached is not None:
            return cached

        async with self._lock:
            # A waiter that queued behind an in-flight refresh has its answer.
            cached = self._fresh_cache()
            if cached is not None:
                return cached
            return await self._refresh()

    def _fresh_cache(self) -> VpsInfo | None:
        if self._cache is None or time.monotonic() >= self._cache_expiry:
            return None
        return self._cache

    async def _refresh(self) -> VpsInfo:
        try:
            data = await kiwivm.fetch_service_info_async(self._veid, self._api_key)
        except Exception:
            logger.warning("Failed to fetch VPS info from KiwiVM API", exc_info=True)
            return self._store(self._last_good_or_error(), self._error_cache_ttl)

        info = _parse_service_info(data, self._timezone_name)
        return self._store(info, self._cache_ttl)

    def _last_good_or_error(self) -> VpsInfo:
        """A stale reading beats a blank card, so an expired one is reused."""
        if self._cache is not None and self._cache.error is None:
            return self._cache
        return VpsInfo(error="unavailable")

    def _store(self, info: VpsInfo, ttl: float) -> VpsInfo:
        self._cache = info
        self._cache_expiry = time.monotonic() + ttl
        return info


def create_vps_info_provider(
    vps_info: VpsInfoSettings | None = None,
    *,
    timezone_name: str,
) -> VpsInfoProvider | None:
    """Create a provider from settings, falling back to environment variables."""
    credentials = kiwivm.resolve_credentials(vps_info)
    if credentials is None:
        return None
    return VpsInfoProvider(*credentials, timezone_name=timezone_name)
