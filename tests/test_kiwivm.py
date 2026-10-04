"""Tests for the shared KiwiVM API client."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from sing_box_manager import kiwivm
from sing_box_manager.settings import VpsInfoSettings
from tests.conftest import utc_epoch


def test_billing_day_uses_the_billing_timezone() -> None:
    """A reset late on the 2nd UTC is the 3rd in Asia/Shanghai.

    The cycle boundary is computed in the billing timezone, so the day must be
    read there too or the two disagree for eight hours out of every day.
    """
    payload = {"data_next_reset": utc_epoch("2026-10-02T20:00:00")}

    assert kiwivm.billing_day_from_service_info(payload, "Asia/Shanghai") == 3
    assert kiwivm.billing_day_from_service_info(payload, "UTC") == 2


@pytest.mark.parametrize(
    "raw",
    [None, 0, -1, "1790000000", True, float("nan")],
    ids=["missing", "zero", "negative", "string", "bool", "nan"],
)
def test_billing_day_rejects_unusable_timestamps(raw: object) -> None:
    payload = {} if raw is None else {"data_next_reset": raw}

    assert kiwivm.billing_day_from_service_info(payload, "Asia/Shanghai") is None


def test_resolve_credentials_prefers_settings_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIWI_VEID", "env-veid")
    monkeypatch.setenv("KIWI_API_KEY", "env-key")

    assert kiwivm.resolve_credentials(VpsInfoSettings(kiwi_veid="cfg-veid", kiwi_api_key="cfg-key")) == (
        "cfg-veid",
        "cfg-key",
    )


def test_resolve_credentials_falls_back_to_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIWI_VEID", "env-veid")
    monkeypatch.setenv("KIWI_API_KEY", "env-key")

    assert kiwivm.resolve_credentials(VpsInfoSettings()) == ("env-veid", "env-key")


def test_resolve_credentials_returns_none_when_either_half_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KIWI_VEID", raising=False)
    monkeypatch.setenv("KIWI_API_KEY", "env-key")

    assert kiwivm.resolve_credentials(VpsInfoSettings(kiwi_veid="cfg-veid", kiwi_api_key="")) is None


def test_fetch_service_info_returns_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"error": 0, "data_counter": 42})

    _patch_sync_transport(monkeypatch, handler)

    assert kiwivm.fetch_service_info("v", "k") == {"error": 0, "data_counter": 42}
    assert "veid=v" in str(captured["url"])


def test_fetch_service_info_raises_on_an_error_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync_transport(
        monkeypatch, lambda _: httpx.Response(200, json={"error": 1, "message": "bad"})
    )

    with pytest.raises(kiwivm.KiwiVmError):
        kiwivm.fetch_service_info("v", "k")


def test_fetch_service_info_raises_on_an_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync_transport(monkeypatch, lambda _: httpx.Response(500))

    with pytest.raises(kiwivm.KiwiVmError, match="HTTP 500"):
        kiwivm.fetch_service_info("v", "k")


SECRET_KEY = "private-api-key-0123456789"


def _logged_failure(caplog: pytest.LogCaptureFixture, call) -> str:
    """Log a failure the way both callers do and return everything written."""
    try:
        call()
    except Exception:
        logging.getLogger("test").warning("KiwiVM failed", exc_info=True)
    return caplog.text


@pytest.mark.parametrize(
    "response",
    [httpx.Response(401), httpx.Response(200, content=b"not json")],
    ids=["http-error", "invalid-json"],
)
def test_failures_never_carry_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    response: httpx.Response,
) -> None:
    """The key travels in the query string, and httpx quotes the URL in errors."""
    _patch_sync_transport(monkeypatch, lambda _: response)

    logged = _logged_failure(caplog, lambda: kiwivm.fetch_service_info("v", SECRET_KEY))

    assert "KiwiVmError" in logged
    assert SECRET_KEY not in logged


def test_async_failures_never_carry_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda _: httpx.Response(403))
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    logged = _logged_failure(
        caplog,
        lambda: asyncio.run(kiwivm.fetch_service_info_async("v", SECRET_KEY)),
    )

    assert "HTTP 403" in logged
    assert SECRET_KEY not in logged


def test_connection_failures_never_carry_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    _patch_sync_transport(monkeypatch, refuse)

    logged = _logged_failure(caplog, lambda: kiwivm.fetch_service_info("v", SECRET_KEY))

    assert "ConnectError" in logged
    assert SECRET_KEY not in logged


def _patch_sync_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    original = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
