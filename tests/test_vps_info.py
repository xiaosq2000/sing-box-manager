"""Tests for the portal's KiwiVM VPS info provider."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from sing_box_manager import kiwivm
from sing_box_manager.settings import VpsInfoSettings
from sing_box_manager.web.vps_info import (
    VpsInfoProvider,
    _parse_service_info,
    _raw_bytes,
    create_vps_info_provider,
)
from tests.conftest import utc_epoch

SERVICE_INFO = {
    "node_location": "JPTYO_1",
    "data_counter": 1_073_741_824 * 5,
    "plan_monthly_data": 1_073_741_824 * 1000,
}


@dataclass
class _FakeKiwiVm:
    """A KiwiVM stand-in that counts calls and fails on request."""

    payload: dict
    failing: bool = False
    calls: int = 0

    async def fetch(self, veid: str, api_key: str) -> dict:
        self.calls += 1
        # Yield the loop so concurrent callers get a chance to pile up behind
        # an in-flight fetch, which is the case worth testing.
        await asyncio.sleep(0)
        if self.failing:
            raise kiwivm.KiwiVmError("KiwiVM is down")
        return self.payload


def _provider(
    monkeypatch: pytest.MonkeyPatch, api: _FakeKiwiVm, **kwargs: int
) -> VpsInfoProvider:
    monkeypatch.setattr(kiwivm, "fetch_service_info_async", api.fetch)
    return VpsInfoProvider("veid", "key", timezone_name="Asia/Shanghai", **kwargs)


def test_reset_date_renders_in_the_billing_timezone() -> None:
    """The card must not print a different day than the cycle boundary.

    A reset at 20:00 UTC on the 2nd is already the 3rd in Asia/Shanghai, which
    is the day the traffic cycle rolls over on.
    """
    payload = {"data_next_reset": utc_epoch("2026-10-02T20:00:00")}

    assert _parse_service_info(payload, "Asia/Shanghai").bandwidth_reset_date == (
        "2026-10-03"
    )
    assert _parse_service_info(payload, "UTC").bandwidth_reset_date == "2026-10-02"


@pytest.mark.parametrize("raw", [None, 0, -1, "nope"], ids=["missing", "zero", "negative", "string"])
def test_reset_date_is_omitted_when_the_timestamp_is_unusable(raw: object) -> None:
    """An absent timestamp must not render as a reset in 1970."""
    payload = {} if raw is None else {"data_next_reset": raw}

    assert _parse_service_info(payload, "Asia/Shanghai").bandwidth_reset_date is None


def test_parse_service_info_keeps_the_bandwidth_fields() -> None:
    info = _parse_service_info(
        {
            "node_location": "USCA_2",
            "data_counter": 1_073_741_824 * 5,
            "plan_monthly_data": 1_073_741_824 * 1000,
            "data_next_reset": utc_epoch("2026-10-03T00:00:00"),
        },
        "Asia/Shanghai",
    )

    assert info.location == "US, Los Angeles"
    assert info.bandwidth_used_gb == 5.0
    assert info.bandwidth_total_gb == 1000.0
    assert info.error is None


def test_parse_service_info_keeps_the_plan_total_in_raw_bytes() -> None:
    """The gigabyte figure is for display; the byte figure is for arithmetic.

    `/api/usage` divides a byte-exact user total into this plan allowance, and
    two decimal places of gigabytes quantize it to about 10 MB.
    """
    plan_bytes = 536_871_000_000  # 499.999... GiB, so 500.0 once rounded

    info = _parse_service_info({"plan_monthly_data": plan_bytes}, "Asia/Shanghai")

    assert info.bandwidth_total_bytes == plan_bytes
    assert info.bandwidth_total_gb == 500.0


def test_plan_total_bytes_is_omitted_when_the_field_is_absent() -> None:
    assert _parse_service_info({}, "Asia/Shanghai").bandwidth_total_bytes is None


@pytest.mark.parametrize("raw", [None, "lots", True], ids=["none", "string", "bool"])
def test_a_non_numeric_byte_count_reads_as_unknown(raw: object) -> None:
    """The payload is validated for shape, not for field types.

    An unknown plan total makes the clients omit the plan share, which is the
    right degradation for a figure that would otherwise be a fabricated
    denominator.
    """
    assert _raw_bytes(raw) is None


def test_a_failed_fetch_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """An outage must not cost every caller a fresh 10 s timeout.

    `/api/usage` reaches the provider on the request path, so retrying on each
    call would stall `proxy check quota` for the full KiwiVM timeout for as
    long as the outage lasts.
    """
    api = _FakeKiwiVm(SERVICE_INFO, failing=True)
    provider = _provider(monkeypatch, api)

    async def twice() -> tuple:
        return await provider.get_info(), await provider.get_info()

    first, second = asyncio.run(twice())

    assert first.error == "unavailable"
    assert second.error == "unavailable"
    assert api.calls == 1


def test_a_cached_failure_is_retried_once_it_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered API has to be picked up without waiting out the hour."""
    api = _FakeKiwiVm(SERVICE_INFO, failing=True)
    provider = _provider(monkeypatch, api, error_cache_ttl=0)

    async def fail_then_recover() -> tuple:
        failed = await provider.get_info()
        api.failing = False
        return failed, await provider.get_info()

    failed, recovered = asyncio.run(fail_then_recover())

    assert failed.error == "unavailable"
    assert recovered.location == "JP, Tokyo"
    assert api.calls == 2


def test_an_outage_keeps_serving_the_last_good_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale reading beats a blank card."""
    api = _FakeKiwiVm(SERVICE_INFO)
    provider = _provider(monkeypatch, api, cache_ttl=0)

    async def succeed_then_fail() -> tuple:
        good = await provider.get_info()
        api.failing = True
        return good, await provider.get_info()

    good, stale = asyncio.run(succeed_then_fail())

    assert good.location == "JP, Tokyo"
    assert stale.error is None
    assert stale.bandwidth_total_gb == 1000.0


def test_concurrent_callers_share_one_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cold cache must not let N clients fire N identical requests.

    Pollers that arrive while the first fetch is in flight would otherwise
    stampede KiwiVM for an answer it is about to cache anyway.
    """
    api = _FakeKiwiVm(SERVICE_INFO)
    provider = _provider(monkeypatch, api)

    async def race() -> list:
        return await asyncio.gather(*(provider.get_info() for _ in range(5)))

    results = asyncio.run(race())

    assert api.calls == 1
    assert all(info.location == "JP, Tokyo" for info in results)


def test_create_provider_threads_the_timezone_through() -> None:
    provider = create_vps_info_provider(
        VpsInfoSettings(kiwi_veid="veid", kiwi_api_key="key"), timezone_name="Asia/Shanghai"
    )

    assert isinstance(provider, VpsInfoProvider)
    assert provider._timezone_name == "Asia/Shanghai"


def test_create_provider_returns_none_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KIWI_VEID", raising=False)
    monkeypatch.delenv("KIWI_API_KEY", raising=False)

    assert create_vps_info_provider(None, timezone_name="Asia/Shanghai") is None
