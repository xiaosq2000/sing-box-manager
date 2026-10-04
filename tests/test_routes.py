"""Tests for web routes."""

import html
import io
import re
import tarfile
import time
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from sing_box_manager.release import artifacts
from sing_box_manager.settings import Settings
from sing_box_manager.traffic_stats import (
    UNATTRIBUTED_DOMAIN_KEY,
    DailyUsagePoint,
    DailyUsageSeries,
    DestinationUsage,
    DestinationUsageBreakdown,
    DomainUsage,
    DomainUsageBreakdown,
    LiveConnection,
    MonthlyUsageHistory,
    MonthlyUsagePoint,
    ProtocolUsage,
    ProtocolUsageBreakdown,
    UserTrafficSummary,
)
from sing_box_manager.web.routes import _build_install_targets
from sing_box_manager.web.vps_info import VpsInfo
from tests.conftest import manifest_config_bytes


class _StaticVpsInfoProvider:
    def __init__(self, info: VpsInfo) -> None:
        self._info = info

    async def get_info(self) -> VpsInfo:
        return self._info


class _StaticTrafficStatsProvider:
    def __init__(
        self,
        summary: UserTrafficSummary | None = None,
        summaries: tuple[UserTrafficSummary, ...] = (),
    ) -> None:
        self._summary = summary
        self._summaries = summaries
        # Set by the tests that exercise the live connections view.
        self.live_connections: tuple[LiveConnection, ...] = ()

    def _known(self) -> dict[str, UserTrafficSummary]:
        known = {summary.username: summary for summary in self._summaries}
        if self._summary is not None:
            known.setdefault(self._summary.username, self._summary)
        return known

    def get_user_summary(self, username: str) -> UserTrafficSummary:
        known = self._known()
        if username in known:
            return known[username]
        # Never collected: updated_at stays None, which is how the routes decide
        # a per-user page should 404.
        return UserTrafficSummary(
            username=username,
            cycle_month="2026-04",
            cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
            cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
            timezone="Asia/Shanghai",
        )

    def get_user_summaries(
        self, usernames: list[str], *, now: datetime | None = None
    ) -> tuple[UserTrafficSummary, ...]:
        summaries_by_username = {
            summary.username: summary for summary in self._summaries
        }
        return tuple(summaries_by_username[username] for username in usernames)

    def get_all_user_summaries(
        self, known_usernames: list[str], *, now: datetime | None = None
    ) -> tuple[UserTrafficSummary, ...]:
        return self._summaries

    def _has_data(self, username: str | None) -> bool:
        """Mirror the real provider: an unseen user gets dense zeroes, not data."""
        return username is None or username in self._known()

    def get_daily_series(
        self,
        username: str | None = None,
        *,
        days: int = 30,
        now: datetime | None = None,
    ) -> DailyUsageSeries:
        start = date(2026, 4, 30) - timedelta(days=days - 1)
        scale = 1 if self._has_data(username) else 0
        return DailyUsageSeries(
            username=username,
            start_date=start,
            end_date=date(2026, 4, 30),
            timezone="Asia/Shanghai",
            points=tuple(
                DailyUsagePoint(
                    usage_date=start + timedelta(days=offset),
                    upload_bytes=1024 * (offset + 1) * scale,
                    download_bytes=2048 * (offset + 1) * scale,
                )
                for offset in range(days)
            ),
        )

    def get_protocol_breakdown(
        self, username: str | None = None, *, now: datetime | None = None
    ) -> ProtocolUsageBreakdown:
        scale = 1 if self._has_data(username) else 0
        return ProtocolUsageBreakdown(
            username=username,
            cycle_month="2026-04",
            timezone="Asia/Shanghai",
            protocols=(
                ProtocolUsage(
                    protocol="trojan",
                    upload_bytes=1024 * scale,
                    download_bytes=3072 * scale,
                ),
                ProtocolUsage(
                    protocol="hysteria2",
                    upload_bytes=512 * scale,
                    download_bytes=1024 * scale,
                ),
                ProtocolUsage(protocol="naive"),
            ),
        )

    def get_domain_breakdown(
        self,
        username: str | None = None,
        *,
        days: int = 30,
        limit: int = 20,
        now: datetime | None = None,
    ) -> DomainUsageBreakdown:
        if not self._has_data(username):
            return DomainUsageBreakdown(
                username=username, timezone="Asia/Shanghai", days=days
            )
        return DomainUsageBreakdown(
            username=username,
            timezone="Asia/Shanghai",
            days=days,
            window_total_bytes=6400,
            domains=(
                DomainUsage("example.com", 1024, 3072, 4),
                DomainUsage("wikipedia.org", 512, 1024, 2),
                # The reserved buckets are part of the real shape, so the
                # templates get exercised against them too.
                DomainUsage(UNATTRIBUTED_DOMAIN_KEY, 256, 512, 0),
            )[:limit],
        )

    def get_destination_breakdown(
        self,
        username: str | None = None,
        *,
        days: int = 30,
        limit: int = 20,
        now: datetime | None = None,
    ) -> DestinationUsageBreakdown:
        if not self._has_data(username):
            return DestinationUsageBreakdown(
                username=username, timezone="Asia/Shanghai", days=days
            )
        return DestinationUsageBreakdown(
            username=username,
            timezone="Asia/Shanghai",
            days=days,
            destinations=(DestinationUsage("93.184.216.34", 128, 256, 1),)[:limit],
        )

    def get_live_connections(
        self, username: str | None = None, *, limit: int = 500
    ) -> tuple[LiveConnection, ...]:
        return self.live_connections[:limit]

    def get_monthly_history(
        self,
        username: str | None = None,
        *,
        months: int = 6,
        now: datetime | None = None,
    ) -> MonthlyUsageHistory:
        scale = 1 if self._has_data(username) else 0
        return MonthlyUsageHistory(
            username=username,
            timezone="Asia/Shanghai",
            months=(
                MonthlyUsagePoint(
                    cycle_month="2026-03",
                    upload_bytes=2048 * scale,
                    download_bytes=4096 * scale,
                ),
                MonthlyUsagePoint(
                    cycle_month="2026-04",
                    upload_bytes=1536 * scale,
                    download_bytes=4096 * scale,
                ),
            ),
        )


def _get_session_cookie(client: TestClient) -> str:
    """Login as alice and return a valid session token."""
    session_mgr = client.app.state.session_manager
    return session_mgr.create_token("alice")


class TestLoginPage:
    def test_login_page_renders(self, client: TestClient):
        client.app.state.vps_hostname = "vps-host"

        response = client.get("/")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert 'action="/login"' in response.text
        assert 'name="username"' in response.text
        assert 'name="password"' in response.text
        assert "vps-host" in response.text
        assert "curl -fsSL" in response.text
        assert "http://testserver/install.sh" in response.text
        assert "http://testserver/install.ps1" in response.text
        assert "/static/styles.css?v=" in response.text
        assert "wget" not in response.text
        assert "fonts.googleapis.com" not in response.text
        assert "material-symbols-rounded" not in response.text

    def test_login_page_places_vps_info_above_login_form(self, client: TestClient):
        client.app.state.vps_hostname = "vps-host"
        client.app.state.vps_info_provider = _StaticVpsInfoProvider(
            VpsInfo(
                location="JP, Tokyo",
                bandwidth_used_gb=12.34,
                bandwidth_total_gb=500.0,
                bandwidth_reset_date="2026-05-01",
            )
        )

        response = client.get("/")

        assert response.status_code == 200
        assert response.text.count('class="vps-item"') == 3
        assert "vps-host" in response.text
        assert "JP, Tokyo" in response.text
        assert "12.34 / 500 GB (Resets: 2026-05-01)" in response.text
        assert response.text.index("vps-host") < response.text.index('action="/login"')
        assert response.text.index("12.34 / 500 GB") < response.text.index(
            'action="/login"'
        )

    def test_login_page_shows_hostname_without_kiwivm_info(self, client: TestClient):
        client.app.state.vps_hostname = "vps-host"
        client.app.state.vps_info_provider = None

        response = client.get("/")

        assert response.status_code == 200
        assert "vps-host" in response.text
        assert response.text.count('class="vps-item"') == 1
        assert "VPS info unavailable" not in response.text

    def test_login_page_uses_absolute_installer_url(self, test_app):
        test_app.state.vps_hostname = "vps-host"
        secure_client = TestClient(test_app, base_url="https://testserver")

        response = secure_client.get("/")

        assert response.status_code == 200
        assert "curl -fsSL" in response.text
        assert "https://testserver/install.sh" in response.text


class TestClientUpdateMetadata:
    def test_public_endpoint_returns_shell_parseable_metadata(
        self, client: TestClient
    ) -> None:
        response = client.get("/api/client-update")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "schema=1\n" in response.text
        assert f"client_build_id={'d' * 64}\n" in response.text
        assert "commit_sha=9237d372\n" in response.text
        assert "policy=suggested\n" in response.text

    def test_footer_reads_matching_successful_deployment_dynamically(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        artifacts.write_deployment_info(
            test_settings.deployment_info_path,
            artifacts.DeploymentInfo(
                release_id="cafebabe1234" + "0" * 52,
                commit_sha="9237d372",
                dirty=False,
                deployed_at="2026-08-24T09:30:00Z",
                client_build_id="d" * 64,
            ),
        )

        response = client.get("/")

        assert "9237d372" in response.text
        assert 'datetime="2026-08-24T09:30:00Z"' in response.text
        assert "data-local-time" in response.text

    def test_footer_rejects_stale_deployment_metadata(
        self, client: TestClient, test_settings: Settings
    ) -> None:
        artifacts.write_deployment_info(
            test_settings.deployment_info_path,
            artifacts.DeploymentInfo(
                release_id="f" * 64,
                commit_sha="9237d372",
                dirty=False,
                deployed_at="2026-08-24T09:30:00Z",
            ),
        )

        response = client.get("/")

        assert "尚无成功部署记录" in response.text
        assert 'datetime="2026-08-24T09:30:00Z"' not in response.text


class TestVpsInfoApi:
    def test_vps_info_api_returns_public_sanitized_payload(self, client: TestClient):
        client.app.state.vps_hostname = "vps-host"
        client.app.state.vps_info_provider = _StaticVpsInfoProvider(
            VpsInfo(
                location="JP, Tokyo",
                bandwidth_used_gb=12.34,
                bandwidth_total_gb=500.0,
                bandwidth_total_bytes=500 * 1024**3,
                bandwidth_reset_date="2026-05-01",
            )
        )

        response = client.get("/api/vps-info")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "hostname": "vps-host",
            "location": "JP, Tokyo",
            "bandwidth_used_gb": 12.34,
            "bandwidth_total_gb": 500.0,
            "bandwidth_total_bytes": 500 * 1024**3,
            "bandwidth_reset_date": "2026-05-01",
            "relay_multiplier": 2.0,
            "error": None,
        }

    def test_vps_info_api_reports_unavailable_when_provider_is_unconfigured(
        self, client: TestClient
    ):
        client.app.state.vps_hostname = "vps-host"
        client.app.state.vps_info_provider = None

        response = client.get("/api/vps-info")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "hostname": "vps-host",
            "relay_multiplier": 2.0,
            "error": "unavailable",
        }


class TestUsageApi:
    def test_usage_api_returns_authenticated_users_monthly_summary(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            UserTrafficSummary(
                username="alice",
                cycle_month="2026-04",
                cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=1536,
                download_bytes=4096,
                updated_at=datetime(2026, 4, 11, 12, 34, 56, tzinfo=UTC),
            )
        )

        response = client.get("/api/usage", auth=("alice", portal_passwords["alice"]))

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "username": "alice",
            "cycle_month": "2026-04",
            "cycle_start": "2026-04-01",
            "cycle_end": "2026-04-30",
            "timezone": "Asia/Shanghai",
            "upload_bytes": 1536,
            "download_bytes": 4096,
            "total_bytes": 5632,
            "relay_multiplier": 2.0,
            "billed_bytes": 11264,
            "plan_total_bytes": None,
            "updated_at": "2026-04-11T12:34:56+00:00",
            "error": None,
            # Raw bytes and raw hostnames: the client formats these itself.
            "top_domains": [
                {
                    "domain": "example.com",
                    "upload_bytes": 1024,
                    "download_bytes": 3072,
                    "total_bytes": 4096,
                    "connection_count": 4,
                },
                {
                    "domain": "wikipedia.org",
                    "upload_bytes": 512,
                    "download_bytes": 1024,
                    "total_bytes": 1536,
                    "connection_count": 2,
                },
                {
                    "domain": UNATTRIBUTED_DOMAIN_KEY,
                    "upload_bytes": 256,
                    "download_bytes": 512,
                    "total_bytes": 768,
                    "connection_count": 0,
                },
            ],
        }

    def test_usage_api_projects_measured_bytes_onto_the_host_counter(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        """The measured fields must survive the projection untouched.

        A relayed byte crosses the VPS interface twice, so the plan share a user
        sees has to be the doubled figure. Doubling `total_bytes` in place would
        make it disagree with what the user's own client reports, so the derived
        value lands in `billed_bytes` beside the multiplier that produced it.
        """
        client.app.state.relay_multiplier = 2.0
        client.app.state.vps_info_provider = _StaticVpsInfoProvider(
            VpsInfo(bandwidth_total_gb=1000.0, bandwidth_total_bytes=1000 * 1024**3)
        )
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            UserTrafficSummary(
                username="alice",
                cycle_month="2026-04",
                cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=1024,
                download_bytes=3072,
            )
        )

        payload = client.get(
            "/api/usage", auth=("alice", portal_passwords["alice"])
        ).json()

        assert payload["upload_bytes"] == 1024
        assert payload["download_bytes"] == 3072
        assert payload["total_bytes"] == 4096
        assert payload["billed_bytes"] == 8192
        assert payload["relay_multiplier"] == 2.0
        assert payload["plan_total_bytes"] == 1000 * 1024**3

    def test_usage_api_reports_the_plan_total_as_the_host_stated_it(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        """The denominator must not come back through the gigabyte rounding.

        `bandwidth_total_gb` is a display value rounded to two decimals, so
        re-inflating it quantizes the plan to about 10 MB. Carrying the plan
        total in bytes only helps if it is the byte count the host reported.
        """
        plan_bytes = 536_871_000_000  # 499.999... GiB, so 500.0 once rounded
        client.app.state.vps_info_provider = _StaticVpsInfoProvider(
            VpsInfo(bandwidth_total_gb=500.0, bandwidth_total_bytes=plan_bytes)
        )
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            UserTrafficSummary(
                username="alice",
                cycle_month="2026-04",
                cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=1024,
                download_bytes=3072,
            )
        )

        payload = client.get(
            "/api/usage", auth=("alice", portal_passwords["alice"])
        ).json()

        assert payload["plan_total_bytes"] == plan_bytes

    def test_usage_api_billed_bytes_track_a_non_default_multiplier(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        """An egress-only host bills each relayed byte once, not twice.

        The multiplier is configurable precisely so those hosts do not get a
        doubled plan share, so 1.0 has to leave the projection at parity.
        """
        client.app.state.relay_multiplier = 1.0
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            UserTrafficSummary(
                username="alice",
                cycle_month="2026-04",
                cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=1024,
                download_bytes=3072,
            )
        )

        payload = client.get(
            "/api/usage", auth=("alice", portal_passwords["alice"])
        ).json()

        assert payload["relay_multiplier"] == 1.0
        assert payload["billed_bytes"] == payload["total_bytes"] == 4096

    def test_usage_api_omits_plan_total_when_vps_info_is_unavailable(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        """No plan total means the clients print no plan share.

        `plan_total_bytes` is the denominator of the percentage the clients
        render, so guessing one would print a share against a fabricated plan.
        """
        client.app.state.vps_info_provider = _StaticVpsInfoProvider(
            VpsInfo(error="unavailable")
        )
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            UserTrafficSummary(
                username="alice",
                cycle_month="2026-04",
                cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=1024,
                download_bytes=3072,
            )
        )

        payload = client.get(
            "/api/usage", auth=("alice", portal_passwords["alice"])
        ).json()

        assert payload["plan_total_bytes"] is None
        assert payload["billed_bytes"] == 8192

    def test_usage_api_reports_a_cycle_that_is_not_a_calendar_month(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        """The clients render cycle_start/cycle_end verbatim.

        `proxy check quota` in both setup.sh and sing-box-proxy.psm1 concatenates
        these two fields without parsing them, so a VPS that bills on the 3rd
        needs no client change -- but only if the API passes the real window
        through untouched.
        """
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            UserTrafficSummary(
                username="alice",
                cycle_month="2026-09",
                cycle_start=datetime(2026, 9, 3, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 10, 2, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=1,
                download_bytes=2,
            )
        )

        payload = client.get(
            "/api/usage", auth=("alice", portal_passwords["alice"])
        ).json()

        assert payload["cycle_start"] == "2026-09-03"
        assert payload["cycle_end"] == "2026-10-02"
        assert payload["cycle_month"] == "2026-09"

    def test_usage_api_reports_unavailable_when_provider_is_unconfigured(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        client.app.state.traffic_stats_provider = None

        response = client.get("/api/usage", auth=("alice", portal_passwords["alice"]))

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"error": "unavailable"}


class TestAdminTrafficPage:
    def test_admin_page_requires_session(self, client: TestClient):
        response = client.get("/admin")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "Session expired" in response.text

    def test_admin_page_rejects_regular_users(self, client: TestClient):
        session_mgr = client.app.state.session_manager
        client.cookies.set("session", session_mgr.create_token("bob"))

        response = client.get("/admin")

        assert response.status_code == 403
        client.cookies.clear()

    def test_admin_page_renders_all_users_traffic_stats(self, client: TestClient):
        client.app.state.users["alice"] = client.app.state.users["alice"].model_copy(
            update={"admin": True}
        )
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            summaries=(
                UserTrafficSummary(
                    username="alice",
                    cycle_month="2026-04",
                    cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                    cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                    timezone="Asia/Shanghai",
                    upload_bytes=1536,
                    download_bytes=4096,
                    updated_at=datetime(2026, 4, 11, 12, 34, 56, tzinfo=UTC),
                ),
                UserTrafficSummary(
                    username="bob",
                    cycle_month="2026-04",
                    cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                    cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                    timezone="Asia/Shanghai",
                    upload_bytes=2048,
                    download_bytes=1024,
                    updated_at=datetime(2026, 4, 12, 0, 0, tzinfo=UTC),
                ),
                UserTrafficSummary(
                    username="protocol-only",
                    cycle_month="2026-04",
                    cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                    cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                    timezone="Asia/Shanghai",
                    upload_bytes=512,
                    download_bytes=512,
                    updated_at=datetime(2026, 4, 12, 0, 0, tzinfo=UTC),
                ),
            )
        )
        token = _get_session_cookie(client)
        client.cookies.set("session", token)

        response = client.get("/admin")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "管理员流量" in response.text
        assert "本月总流量" in response.text
        assert "2026-04-01 ~ 2026-04-30" in response.text
        assert "4.00 KiB" in response.text
        assert "5.50 KiB" in response.text
        assert "9.50 KiB" in response.text
        assert "alice" in response.text
        assert "bob" in response.text
        assert "protocol-only" in response.text
        assert response.text.count('class="admin-user-row"') == 3
        assert "2026-04-12 08:00:00" in response.text
        assert "material-symbols-rounded" not in response.text
        client.cookies.clear()

    def test_admin_page_reports_unavailable_when_stats_are_disabled(
        self, client: TestClient
    ):
        client.app.state.users["alice"] = client.app.state.users["alice"].model_copy(
            update={"admin": True}
        )
        client.app.state.traffic_stats_provider = None
        token = _get_session_cookie(client)
        client.cookies.set("session", token)

        response = client.get("/admin")

        assert response.status_code == 200
        assert "流量统计未启用" in response.text
        client.cookies.clear()

    def test_admin_page_projects_the_summed_totals_onto_the_plan(
        self, client: TestClient
    ):
        """The sum an admin reconciles against the VPS counter.

        Adding up the measured per-user totals and comparing that against the
        host's own number is where the phantom 2x shortfall shows up, so the
        card has to carry the projection the CLI prints.
        """
        client.app.state.relay_multiplier = 2.0
        client.app.state.vps_info_provider = _StaticVpsInfoProvider(
            VpsInfo(bandwidth_total_bytes=1000 * 1024**3)
        )
        client.app.state.users["alice"] = client.app.state.users["alice"].model_copy(
            update={"admin": True}
        )
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            summaries=(
                UserTrafficSummary(
                    username="alice",
                    cycle_month="2026-04",
                    cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                    cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                    timezone="Asia/Shanghai",
                    upload_bytes=10 * 1024**3,
                    download_bytes=90 * 1024**3,
                ),
                UserTrafficSummary(
                    username="bob",
                    cycle_month="2026-04",
                    cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                    cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                    timezone="Asia/Shanghai",
                    upload_bytes=5 * 1024**3,
                    download_bytes=5 * 1024**3,
                ),
            )
        )
        client.cookies.set("session", _get_session_cookie(client))

        response = client.get("/admin")

        assert response.status_code == 200
        assert "套餐占用" in response.text
        assert "220.00 GiB / 1000.00 GiB (22.0%, x2 relay)" in response.text
        client.cookies.clear()

    def test_admin_page_embeds_inline_charts(self, client: TestClient):
        _make_admin(client)

        response = client.get("/admin")

        assert response.status_code == 200
        assert 'class="sbm-chart' in response.text
        # The whole point of server-rendering: no bundle, no CDN, no <?xml?>.
        assert "<script src" not in response.text
        assert "kozea" not in response.text
        assert "<?xml" not in response.text
        for heading in ("用户排行", "流量趋势", "协议分布"):
            assert heading in response.text
        client.cookies.clear()

    def test_admin_page_renders_the_domain_breakdown(self, client: TestClient):
        _make_admin(client)

        response = client.get("/admin")

        assert response.status_code == 200
        assert "域名排行" in response.text
        assert "example.com" in response.text
        assert "wikipedia.org" in response.text
        assert "未归属流量为估算值" in response.text
        # Share of the window's total, so a big row is obvious at a glance:
        # example.com is 4096 of the 6400 bytes across all three rows.
        assert "64.0%" in response.text
        client.cookies.clear()

    def test_admin_page_labels_the_bookkeeping_bucket(self, client: TestClient):
        """`__unattributed__` reads as a broken hostname if shown raw.

        It means the stream daemon missed traffic, so it has to be legible as a
        health signal rather than as a site someone visited.
        """
        _make_admin(client)

        response = client.get("/admin")

        assert "未归属" in response.text
        assert UNATTRIBUTED_DOMAIN_KEY not in response.text
        assert "admin-row--reserved" in response.text
        client.cookies.clear()

    def test_admin_page_links_to_the_live_connections_view(self, client: TestClient):
        _make_admin(client)

        response = client.get("/admin")

        assert 'href="/admin/connections"' in response.text
        client.cookies.clear()

    def test_admin_page_renders_server_side_range_links(self, client: TestClient):
        _make_admin(client)

        default = client.get("/admin")
        narrowed = client.get("/admin?days=7")

        for days in (7, 30, 90):
            assert f'href="/admin?days={days}"' in default.text
        assert 'href="/admin?days=7" class' not in default.text
        assert "range-pill--active" in narrowed.text
        client.cookies.clear()

    def test_admin_page_ignores_an_unoffered_range(self, client: TestClient):
        _make_admin(client)

        response = client.get("/admin?days=99999")

        assert response.status_code == 200
        assert "range-pill--active" in response.text
        client.cookies.clear()

    def test_admin_table_links_to_each_user_detail_page(self, client: TestClient):
        _make_admin(client)

        response = client.get("/admin")

        for username in ("alice", "bob", "protocol-only"):
            assert f'href="/admin/users/{username}"' in response.text
        client.cookies.clear()


def _alice_summary() -> UserTrafficSummary:
    return UserTrafficSummary(
        username="alice",
        cycle_month="2026-04",
        cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
        cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
        timezone="Asia/Shanghai",
        upload_bytes=1536,
        download_bytes=4096,
        updated_at=datetime(2026, 4, 11, 12, 34, 56, tzinfo=UTC),
    )


def _make_admin(client: TestClient) -> None:
    """Grant alice admin, install a populated stats provider, and log her in."""
    client.app.state.users["alice"] = client.app.state.users["alice"].model_copy(
        update={"admin": True}
    )
    client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
        summaries=tuple(
            UserTrafficSummary(
                username=username,
                cycle_month="2026-04",
                cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=upload,
                download_bytes=download,
                updated_at=datetime(2026, 4, 12, 0, 0, tzinfo=UTC),
            )
            for username, upload, download in (
                ("alice", 1536, 4096),
                ("bob", 2048, 1024),
                ("protocol-only", 512, 512),
            )
        )
    )
    client.cookies.set("session", _get_session_cookie(client))


class TestAdminConnectionsPage:
    def test_connections_page_requires_session(self, client: TestClient):
        response = client.get("/admin/connections")

        assert response.status_code == 200
        assert "Session expired" in response.text

    def test_connections_page_rejects_regular_users(self, client: TestClient):
        session_mgr = client.app.state.session_manager
        client.cookies.set("session", session_mgr.create_token("bob"))

        response = client.get("/admin/connections")

        assert response.status_code == 403
        client.cookies.clear()

    def test_connections_page_renders_open_connections(self, client: TestClient):
        _make_admin(client)
        client.app.state.traffic_stats_provider.live_connections = (
            LiveConnection(
                connection_id="c1",
                service_name="trojan",
                username="alice",
                network="tcp",
                protocol="tls",
                source="203.0.113.7:51000",
                destination="example.com:443",
                domain="example.com",
                outbound="direct",
                created_at=datetime(2026, 4, 12, 10, 30, tzinfo=UTC),
                upload_bytes=1024,
                download_bytes=4096,
                updated_at=datetime(2026, 4, 12, 10, 31, tzinfo=UTC),
            ),
        )

        response = client.get("/admin/connections")

        assert response.status_code == 200
        assert "example.com:443" in response.text
        assert "203.0.113.7:51000" in response.text
        assert "alice" in response.text
        client.cookies.clear()

    def test_connections_page_refreshes_without_javascript(self, client: TestClient):
        """The portal ships no JavaScript, so a live view needs a meta refresh."""
        _make_admin(client)

        response = client.get("/admin/connections")

        assert 'http-equiv="refresh"' in response.text
        assert "<script src" not in response.text
        client.cookies.clear()

    def test_connections_page_explains_an_empty_snapshot(self, client: TestClient):
        """Empty also means the daemon is down, which is worth saying."""
        _make_admin(client)

        response = client.get("/admin/connections")

        assert response.status_code == 200
        assert "当前没有活动连接" in response.text
        assert "快照超过 3 分钟未更新" in response.text
        client.cookies.clear()


class TestAdminUserTrafficPage:
    def test_user_page_requires_session(self, client: TestClient):
        response = client.get("/admin/users/alice")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "Session expired" in response.text

    def test_user_page_rejects_regular_users(self, client: TestClient):
        session_mgr = client.app.state.session_manager
        client.cookies.set("session", session_mgr.create_token("bob"))

        response = client.get("/admin/users/alice")

        assert response.status_code == 403
        client.cookies.clear()

    def test_user_page_returns_404_for_unknown_user(self, client: TestClient):
        _make_admin(client)

        response = client.get("/admin/users/nobody")

        assert response.status_code == 404
        client.cookies.clear()

    def test_user_page_resolves_a_user_only_the_collector_knows(
        self, client: TestClient
    ):
        """The admin table links these rows, so the links have to work."""
        _make_admin(client)
        assert "protocol-only" not in client.app.state.users

        response = client.get("/admin/users/protocol-only")

        assert response.status_code == 200
        client.cookies.clear()

    def test_user_page_renders_charts_and_protocol_table(self, client: TestClient):
        _make_admin(client)

        response = client.get("/admin/users/alice")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "alice" in response.text
        assert 'class="sbm-chart' in response.text
        assert "<script src" not in response.text
        for heading in ("本月流量", "流量趋势", "协议分布", "月度对比"):
            assert heading in response.text
        for protocol in ("trojan", "hysteria2", "naive"):
            assert protocol in response.text
        assert 'href="/admin/users/alice?days=90"' in response.text
        client.cookies.clear()

    def test_user_page_renders_an_empty_state_for_a_never_collected_user(
        self, client: TestClient
    ):
        _make_admin(client)
        # bob is in the auth snapshot, so the page exists, but the stub reports
        # no collection for a user outside its summaries.
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider()

        response = client.get("/admin/users/bob")

        assert response.status_code == 200
        assert "尚未采集" in response.text
        assert "sbm-chart--empty" in response.text
        client.cookies.clear()

    def test_user_page_reports_unavailable_when_stats_are_disabled(
        self, client: TestClient
    ):
        _make_admin(client)
        client.app.state.traffic_stats_provider = None

        response = client.get("/admin/users/alice")

        assert response.status_code == 200
        assert "流量统计未启用" in response.text
        client.cookies.clear()


class TestLogin:
    def test_successful_login_uses_portal_password(
        self,
        client: TestClient,
        portal_passwords: dict[str, str],
        tmp_release_dir: Path,
    ):
        release_entries = {path.name for path in tmp_release_dir.iterdir()}
        assert {
            "sing-box-v1.0.0-alice-linux-amd64.tar.gz",
            "sing-box-v1.0.0-alice-linux-arm64.tar.gz",
            "sing-box-v1.0.0-alice-darwin-arm64.tar.gz",
            "sing-box-v1.0.0-alice-darwin-amd64.tar.gz",
            "sing-box-v1.0.0-alice-windows-amd64.zip",
            "sing-box-v1.0.0-alice-android-arm64.zip",
            "sing-box-v1.0.0-bob-linux-amd64.tar.gz",
            "sing-box-v1.0.0-server.tar.gz",
            "sing-box-1.0.0-linux-amd64.tar.gz",
        } <= release_entries

        response = client.post(
            "/login",
            data={"username": "alice", "password": portal_passwords["alice"]},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.cookies.get("session") is not None
        assert "Invalid username or password" not in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-linux-amd64.tar.gz"' in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-linux-arm64.tar.gz"' in response.text
        assert (
            'href="/files/sing-box-v1.0.0-alice-darwin-arm64.tar.gz"' in response.text
        )
        assert (
            'href="/files/sing-box-v1.0.0-alice-darwin-amd64.tar.gz"' in response.text
        )
        assert 'href="/files/sing-box-v1.0.0-alice-windows-amd64.zip"' in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-android-arm64.zip"' in response.text
        assert "sing-box-v1.0.0-bob-linux-amd64.tar.gz" not in response.text
        assert "sing-box-v1.0.0-server.tar.gz" not in response.text
        assert "sing-box-1.0.0-linux-amd64.tar.gz" not in response.text

    def test_login_rejects_trojan_password(
        self, client: TestClient, trojan_passwords: dict[str, str]
    ):
        response = client.post(
            "/login",
            data={"username": "alice", "password": trojan_passwords["alice"]},
        )
        assert response.status_code == 200
        assert "Invalid username or password" in response.text

    def test_wrong_password(self, client: TestClient):
        response = client.post(
            "/login",
            data={"username": "alice", "password": "wrong"},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "Invalid username or password" in response.text

    def test_invalid_username_format(self, client: TestClient):
        response = client.post(
            "/login",
            data={"username": "../evil", "password": "anything"},
        )
        assert response.status_code == 200
        assert "Invalid username or password" in response.text

    def test_rate_limiting(self, client: TestClient):
        for _ in range(6):
            client.post(
                "/login",
                data={"username": "alice", "password": "wrong"},
            )
        response = client.post(
            "/login",
            data={"username": "alice", "password": "wrong"},
        )
        assert "Too many attempts" in response.text


class TestFileList:
    def test_file_list_requires_session(self, client: TestClient):
        response = client.get("/files")
        assert response.status_code == 200
        assert "Session expired" in response.text

    def test_file_list_with_username_query_requires_session(self, client: TestClient):
        response = client.get("/files", params={"username": "alice"})
        assert response.status_code == 200
        assert "Session expired" in response.text
        assert 'action="/login"' in response.text
        assert "sing-box-v1.0.0-alice-linux-amd64.tar.gz" not in response.text

    def test_file_list_with_valid_session(
        self, client: TestClient, tmp_release_dir: Path
    ):
        release_entries = {path.name for path in tmp_release_dir.iterdir()}
        assert {
            "sing-box-v1.0.0-alice-linux-amd64.tar.gz",
            "sing-box-v1.0.0-alice-linux-arm64.tar.gz",
            "sing-box-v1.0.0-alice-windows-amd64.zip",
            "sing-box-v1.0.0-alice-android-arm64.zip",
            "sing-box-v1.0.0-bob-linux-amd64.tar.gz",
            "sing-box-v1.0.0-server.tar.gz",
            "sing-box-1.0.0-linux-amd64.tar.gz",
            "sing-box-v1.0.0-alice",
        } <= release_entries

        token = _get_session_cookie(client)
        client.cookies.set("session", token)
        response = client.get("/files")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "Session expired" not in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-linux-amd64.tar.gz"' in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-linux-arm64.tar.gz"' in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-windows-amd64.zip"' in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-android-arm64.zip"' in response.text
        assert "sing-box-v1.0.0-bob-linux-amd64.tar.gz" not in response.text
        assert "sing-box-v1.0.0-server.tar.gz" not in response.text
        assert "sing-box-1.0.0-linux-amd64.tar.gz" not in response.text
        assert 'href="/files/sing-box-v1.0.0-alice"' not in response.text
        client.cookies.clear()

    def test_file_list_renders_platform_icons(self, client: TestClient):
        token = _get_session_cookie(client)
        client.cookies.set("session", token)

        response = client.get("/files")

        assert response.status_code == 200
        assert "file-icon-pair" in response.text
        assert 'title="Android ARM64"' in response.text
        assert "file-icon-logo--android" in response.text
        # Linux, macOS and Windows install sbc, so releases no longer pack archives for
        # them, and one an older release left gets the plain archive icon.
        assert 'title="Linux x64"' not in response.text
        assert 'title="macOS Intel"' not in response.text
        assert 'title="Windows x64"' not in response.text
        assert "file-icon-logo--linux" not in response.text
        assert "file-icon-logo--windows" not in response.text
        assert "file-icon-pair--macos" not in response.text
        assert "icon-lucide" in response.text
        assert "fonts.googleapis.com" not in response.text

    def test_file_list_renders_monthly_traffic_stats_when_available(
        self, client: TestClient
    ):
        token = _get_session_cookie(client)
        client.cookies.set("session", token)
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            UserTrafficSummary(
                username="alice",
                cycle_month="2026-04",
                cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=1536,
                download_bytes=4096,
                updated_at=datetime(2026, 4, 11, 12, 34, 56, tzinfo=UTC),
            )
        )

        response = client.get("/files")

        assert response.status_code == 200
        assert "本月流量" in response.text
        assert "2026-04-01 ~ 2026-04-30" in response.text
        assert "1.50 KiB" in response.text
        assert "4.00 KiB" in response.text
        assert "5.50 KiB" in response.text
        assert "最近更新" in response.text

    def test_file_list_hides_domains_from_users_by_default(
        self, client: TestClient
    ):
        """Showing someone their own destinations is a disclosure change.

        It is their own data, but it is more than the page said before, so the
        operator opts in rather than being opted in by an upgrade.
        """
        client.cookies.set("session", _get_session_cookie(client))
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            _alice_summary()
        )
        assert client.app.state.show_user_domains is False

        response = client.get("/files")

        assert "近 30 天域名" not in response.text
        assert "example.com" not in response.text

    def test_file_list_shows_domains_when_the_operator_opts_in(
        self, client: TestClient
    ):
        client.cookies.set("session", _get_session_cookie(client))
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            _alice_summary()
        )
        client.app.state.show_user_domains = True

        response = client.get("/files")

        assert "近 30 天域名" in response.text
        assert "example.com" in response.text
        client.app.state.show_user_domains = False

    def test_file_list_projects_the_measured_total_onto_the_plan(
        self, client: TestClient
    ):
        """The browser has to say what `proxy check quota` says.

        A user who never runs the client reads the measured total against the
        plan quota and concludes they have used about half of what the host has
        actually charged them, which is the misreading the projection exists to
        correct.
        """
        client.app.state.relay_multiplier = 2.0
        client.app.state.vps_info_provider = _StaticVpsInfoProvider(
            VpsInfo(bandwidth_total_bytes=1000 * 1024**3)
        )
        client.cookies.set("session", _get_session_cookie(client))
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            UserTrafficSummary(
                username="alice",
                cycle_month="2026-04",
                cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=round(10.5 * 1024**3),
                download_bytes=100 * 1024**3,
            )
        )

        response = client.get("/files")

        assert response.status_code == 200
        assert "110.50 GiB" in response.text
        assert "套餐占用" in response.text
        assert "221.00 GiB / 1000.00 GiB (22.1%, x2 relay)" in response.text

    def test_file_list_omits_a_plan_row_that_would_repeat_the_total(
        self, client: TestClient
    ):
        """An egress-only host with no plan total has nothing to project."""
        client.app.state.relay_multiplier = 1.0
        client.app.state.vps_info_provider = None
        client.cookies.set("session", _get_session_cookie(client))
        client.app.state.traffic_stats_provider = _StaticTrafficStatsProvider(
            UserTrafficSummary(
                username="alice",
                cycle_month="2026-04",
                cycle_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
                cycle_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
                timezone="Asia/Shanghai",
                upload_bytes=1536,
                download_bytes=4096,
            )
        )

        response = client.get("/files")

        assert response.status_code == 200
        assert "5.50 KiB" in response.text
        assert "套餐占用" not in response.text
        assert "material-symbols-rounded" not in response.text
        client.cookies.clear()

    def test_file_list_shows_admin_link_for_admin_users(self, client: TestClient):
        client.app.state.users["alice"] = client.app.state.users["alice"].model_copy(
            update={"admin": True}
        )
        token = _get_session_cookie(client)
        client.cookies.set("session", token)

        response = client.get("/files")

        assert response.status_code == 200
        assert 'href="/admin"' in response.text
        client.cookies.clear()

    def test_file_list_hides_admin_link_for_regular_users(self, client: TestClient):
        token = _get_session_cookie(client)
        client.cookies.set("session", token)

        response = client.get("/files")

        assert response.status_code == 200
        assert 'href="/admin"' not in response.text
        client.cookies.clear()

    def test_file_list_ignores_username_query_when_session_is_valid(
        self, client: TestClient
    ):
        token = _get_session_cookie(client)
        client.cookies.set("session", token)

        response = client.get("/files", params={"username": "bob"})

        assert response.status_code == 200
        assert 'href="/files/sing-box-v1.0.0-alice-linux-amd64.tar.gz"' in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-linux-arm64.tar.gz"' in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-windows-amd64.zip"' in response.text
        assert 'href="/files/sing-box-v1.0.0-alice-android-arm64.zip"' in response.text
        assert "sing-box-v1.0.0-bob-linux-amd64.tar.gz" not in response.text
        client.cookies.clear()


class TestDownload:
    def test_download_requires_auth(self, client: TestClient):
        response = client.get("/files/some-file.tar.gz")
        assert response.status_code == 401

    def test_download_after_http_login_uses_session_cookie(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        login_response = client.post(
            "/login",
            data={"username": "alice", "password": portal_passwords["alice"]},
            follow_redirects=False,
        )

        assert login_response.status_code == 200

        download_response = client.get(
            "/files/sing-box-v1.0.0-alice-linux-amd64.tar.gz"
        )

        assert download_response.status_code == 200

    def test_download_rejects_path_traversal(self, client: TestClient):
        token = _get_session_cookie(client)
        client.cookies.set("session", token)
        # Use a filename with ".." embedded (not in the URL path, but in the name)
        response = client.get("/files/..%2F..%2Fetc%2Fpasswd")
        assert response.status_code == 400
        client.cookies.clear()

    def test_download_returns_authenticated_users_archive(self, client: TestClient):
        token = _get_session_cookie(client)
        client.cookies.set("session", token)

        response = client.get("/files/sing-box-v1.0.0-alice-linux-amd64.tar.gz")

        assert response.status_code == 200
        assert response.content == b""
        client.cookies.clear()

    def test_download_rejects_other_users_files(self, client: TestClient):
        token = _get_session_cookie(client)
        client.cookies.set("session", token)
        response = client.get(
            "/files/sing-box-v1.0.0-bob-linux-amd64.tar.gz",
        )
        assert response.status_code == 403
        client.cookies.clear()

    def test_file_list_rejects_session_of_removed_user(self, client: TestClient):
        session_mgr = client.app.state.session_manager
        client.cookies.set("session", session_mgr.create_token("carol"))

        response = client.get("/files")

        assert "Session expired" in response.text
        assert 'action="/login"' in response.text
        client.cookies.clear()

    def test_download_rejects_session_of_removed_user(
        self, client: TestClient, tmp_release_dir: Path
    ):
        (tmp_release_dir / "sing-box-v1.0.0-carol-linux-amd64.tar.gz").touch()
        session_mgr = client.app.state.session_manager
        client.cookies.set("session", session_mgr.create_token("carol"))

        response = client.get("/files/sing-box-v1.0.0-carol-linux-amd64.tar.gz")

        assert response.status_code == 401
        client.cookies.clear()

    def test_download_nonexistent_file(self, client: TestClient):
        token = _get_session_cookie(client)
        client.cookies.set("session", token)
        response = client.get(
            "/files/sing-box-v1.0.0-alice-nonexistent.tar.gz",
        )
        assert response.status_code == 404
        client.cookies.clear()


class TestAssembledDownload:
    """The portal assembles per-user archives instead of serving prebuilt files.

    The prebuilt-file path is still covered by ``TestDownload`` above, which runs
    against a release that has no manifest.
    """

    def _login(self, manifest_client: TestClient, portal_passwords: dict[str, str]):
        response = manifest_client.post(
            "/login",
            data={"username": "alice", "password": portal_passwords["alice"]},
            follow_redirects=False,
        )
        assert response.status_code == 200

    def test_assembled_archive_is_a_real_tarball_with_this_users_config(
        self, manifest_client: TestClient, portal_passwords: dict[str, str]
    ):
        self._login(manifest_client, portal_passwords)

        response = manifest_client.get(
            "/files/sing-box-v1.0.0-alice-linux-amd64.tar.gz"
        )

        assert response.status_code == 200
        # Exact, not chunked: the hosted installer runs `curl --fail --silent`,
        # which exits 0 on a truncated chunked body.
        assert int(response.headers["content-length"]) == len(response.content)
        assert response.headers["accept-ranges"] == "bytes"

        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            names = sorted(archive.getnames())
            extracted = archive.extractfile("sing-box/trojan-client.json")
            assert extracted is not None
            assert extracted.read() == manifest_config_bytes("alice", "trojan")

        assert names == [
            "sing-box/client-install.sh",
            "sing-box/naive-client.json",
            "sing-box/sing-box",
            "sing-box/trojan-client.json",
        ]

    def test_a_users_archive_omits_protocols_they_do_not_have(
        self, manifest_client: TestClient, portal_passwords: dict[str, str]
    ):
        """bob has trojan only, so his archive must not carry a naive config."""
        response = manifest_client.post(
            "/login",
            data={"username": "bob", "password": portal_passwords["bob"]},
            follow_redirects=False,
        )
        assert response.status_code == 200

        response = manifest_client.get("/files/sing-box-v1.0.0-bob-linux-amd64.tar.gz")

        assert response.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            assert "sing-box/naive-client.json" not in archive.getnames()

    def test_download_by_platform_serves_a_zip_for_windows(
        self, manifest_client: TestClient, portal_passwords: dict[str, str]
    ):
        response = manifest_client.get(
            "/download",
            params={"platform": "windows-amd64"},
            auth=("alice", portal_passwords["alice"]),
        )

        assert response.status_code == 200
        assert (
            response.headers["content-disposition"]
            == 'attachment; filename="sing-box-v1.0.0-alice-windows-amd64.zip"'
        )
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            assert "sing-box/trojan-client.json" in archive.namelist()

    def test_another_users_archive_is_refused_before_assembly(
        self, manifest_client: TestClient, portal_passwords: dict[str, str]
    ):
        self._login(manifest_client, portal_passwords)

        response = manifest_client.get("/files/sing-box-v1.0.0-bob-linux-amd64.tar.gz")

        assert response.status_code == 403

    def test_a_range_request_resumes_at_the_right_byte(
        self, manifest_client: TestClient, portal_passwords: dict[str, str]
    ):
        self._login(manifest_client, portal_passwords)
        name = "sing-box-v1.0.0-alice-linux-amd64.tar.gz"
        whole = manifest_client.get(f"/files/{name}").content
        total = len(whole)
        start = total // 2

        response = manifest_client.get(
            f"/files/{name}", headers={"range": f"bytes={start}-"}
        )

        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes {start}-{total - 1}/{total}"
        assert int(response.headers["content-length"]) == total - start
        assert response.content == whole[start:]
        # Resuming has to reconstruct the identical archive, not merely a valid one.
        assert whole[:start] + response.content == whole

    def test_a_suffix_range_returns_the_final_bytes(
        self, manifest_client: TestClient, portal_passwords: dict[str, str]
    ):
        self._login(manifest_client, portal_passwords)
        name = "sing-box-v1.0.0-alice-linux-amd64.tar.gz"
        whole = manifest_client.get(f"/files/{name}").content

        response = manifest_client.get(f"/files/{name}", headers={"range": "bytes=-64"})

        assert response.status_code == 206
        assert response.content == whole[-64:]

    def test_an_unsatisfiable_range_falls_back_to_the_whole_archive(
        self, manifest_client: TestClient, portal_passwords: dict[str, str]
    ):
        """Serving everything is a valid answer to any range request."""
        self._login(manifest_client, portal_passwords)
        name = "sing-box-v1.0.0-alice-linux-amd64.tar.gz"
        whole = manifest_client.get(f"/files/{name}").content

        response = manifest_client.get(
            f"/files/{name}", headers={"range": "bytes=99999999-"}
        )

        assert response.status_code == 200
        assert response.content == whole

    def test_the_file_list_shows_only_this_users_archives(
        self, manifest_client: TestClient, portal_passwords: dict[str, str]
    ):
        response = manifest_client.post(
            "/login",
            data={"username": "alice", "password": portal_passwords["alice"]},
            follow_redirects=False,
        )

        assert response.status_code == 200
        assert "sing-box-v1.0.0-alice-linux-amd64.tar.gz" in response.text
        assert "sing-box-v1.0.0-alice-windows-amd64.zip" in response.text
        assert "bob" not in response.text


class TestLogout:
    def test_logout(self, client: TestClient):
        response = client.get("/logout")
        assert response.status_code == 200


class TestWindowsInstallScript:
    def test_install_commands_are_exactly_one_line(self):
        request = Mock()
        request.url_for.side_effect = lambda name: f"https://vpn.example/{name}"

        targets = _build_install_targets(request)

        assert [target["command"] for target in targets] == [
            'curl -fsSL "https://vpn.example/install_script" | sh',
            "& ([scriptblock]::Create((irm 'https://vpn.example/windows_sbc_install_script')))",
        ]
        assert all(
            "\n" not in target["command"] and "\r" not in target["command"]
            for target in targets
        )

    def test_windows_install_script_serves_bootstrapper(self, client: TestClient):
        response = client.get("/install/windows.ps1")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert (
            response.headers["content-disposition"]
            == 'inline; filename="install.ps1"'
        )
        assert response.headers["content-type"].startswith("text/plain")
        expected = (
            artifacts.PROJECT_ROOT
            / "sing_box_manager"
            / "web"
            / "client"
            / "install.ps1"
        ).read_text(encoding="utf-8-sig")
        assert response.text == expected

    def test_windows_install_script_matches_install_ps1(self, client: TestClient):
        assert client.get("/install/windows.ps1").text == client.get("/install.ps1").text

    def test_login_page_offers_a_one_liner_for_every_platform(self, client: TestClient):
        response = client.get("/")

        assert response.status_code == 200
        assert "Linux and macOS" in response.text

        copied_commands = [
            html.unescape(value)
            for value in re.findall(r'data-copy-text="([^"]*)"', response.text)
        ]
        assert copied_commands == [
            'curl -fsSL "http://testserver/install.sh" | sh',
            "& ([scriptblock]::Create((irm 'http://testserver/install.ps1')))",
        ]
        assert all(
            "\n" not in command and "\r" not in command for command in copied_commands
        )


class TestStaticAssets:
    def test_stylesheet_requires_revalidation(self, client: TestClient):
        response = client.get("/static/styles.css")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache, must-revalidate"
        assert "@font-face" in response.text


class TestBasicAuth:
    def test_valid_basic_auth_uses_portal_password(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        response = client.get("/auth", auth=("alice", portal_passwords["alice"]))
        assert response.status_code == 200
        assert response.json() == {"authenticated": True}

    def test_basic_auth_rejects_trojan_password(
        self, client: TestClient, trojan_passwords: dict[str, str]
    ):
        response = client.get("/auth", auth=("alice", trojan_passwords["alice"]))
        assert response.status_code == 401

    def test_invalid_basic_auth(self, client: TestClient):
        response = client.get("/auth", auth=("alice", "wrong"))
        assert response.status_code == 401

    def test_basic_auth_rate_limits_repeated_bad_attempts(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        for _ in range(5):
            response = client.get("/auth", auth=("alice", "wrong"))
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Basic"

        blocked_response = client.get(
            "/auth", auth=("alice", portal_passwords["alice"])
        )

        assert blocked_response.status_code == 401
        assert blocked_response.headers["www-authenticate"] == "Basic"


class TestDirectDownload:
    def test_download_by_platform_returns_linux_archive(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        response = client.get(
            "/download",
            params={"platform": "linux-amd64"},
            auth=("alice", portal_passwords["alice"]),
        )

        assert response.status_code == 200
        assert response.content == b""
        assert (
            "sing-box-v1.0.0-alice-linux-amd64.tar.gz"
            in response.headers["content-disposition"]
        )

    def test_download_by_platform_returns_windows_archive(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        response = client.get(
            "/download",
            params={"platform": "windows-amd64"},
            auth=("alice", portal_passwords["alice"]),
        )

        assert response.status_code == 200
        assert (
            "sing-box-v1.0.0-alice-windows-amd64.zip"
            in response.headers["content-disposition"]
        )

    def test_download_by_platform_returns_macos_archives(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        for platform, expected in (
            ("darwin-arm64", "sing-box-v1.0.0-alice-darwin-arm64.tar.gz"),
            ("darwin-amd64", "sing-box-v1.0.0-alice-darwin-amd64.tar.gz"),
        ):
            response = client.get(
                "/download",
                params={"platform": platform},
                auth=("alice", portal_passwords["alice"]),
            )

            assert response.status_code == 200
            assert expected in response.headers["content-disposition"]

    def test_download_by_platform_rejects_wrong_password(self, client: TestClient):
        response = client.get(
            "/download",
            params={"platform": "linux-amd64"},
            auth=("alice", "wrong"),
        )

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Basic"

    def test_download_by_platform_rate_limits_repeated_bad_attempts(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        for _ in range(5):
            response = client.get(
                "/download",
                params={"platform": "linux-amd64"},
                auth=("alice", "wrong"),
            )
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Basic"

        blocked_response = client.get(
            "/download",
            params={"platform": "linux-amd64"},
            auth=("alice", portal_passwords["alice"]),
        )

        assert blocked_response.status_code == 401
        assert blocked_response.headers["www-authenticate"] == "Basic"

    def test_download_by_platform_returns_404_for_unknown_platform(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        # Upstream publishes this asset; we deliberately do not package it.
        response = client.get(
            "/download",
            params={"platform": "linux-riscv64"},
            auth=("alice", portal_passwords["alice"]),
        )

        assert response.status_code == 404

    def test_download_by_platform_requires_platform_query(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        response = client.get(
            "/download",
            auth=("alice", portal_passwords["alice"]),
        )

        assert response.status_code == 422

    def test_download_by_platform_returns_404_for_other_users_platform_archive(
        self,
        client: TestClient,
        portal_passwords: dict[str, str],
        tmp_release_dir: Path,
    ):
        (tmp_release_dir / "sing-box-v1.0.0-alice-linux-arm64.tar.gz").unlink()
        (tmp_release_dir / "sing-box-v1.0.0-bob-linux-arm64.tar.gz").touch()

        response = client.get(
            "/download",
            params={"platform": "linux-arm64"},
            auth=("alice", portal_passwords["alice"]),
        )

        assert response.status_code == 404


def _get_machine_token(client: TestClient, username: str = "alice") -> str:
    """Create a machine token for testing."""
    machine_mgr = client.app.state.machine_token_manager
    return machine_mgr.create_token(username)


class TestTokenEndpoint:
    def test_create_token_with_valid_credentials(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        response = client.post("/api/token", auth=("alice", portal_passwords["alice"]))

        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert data["username"] == "alice"

        # Verify the returned token is actually valid
        machine_mgr = client.app.state.machine_token_manager
        assert machine_mgr.validate_token(data["token"]) == "alice"

    def test_create_token_rejects_bad_password(self, client: TestClient):
        response = client.post("/api/token", auth=("alice", "wrong"))

        assert response.status_code == 401

    def test_create_token_rate_limits(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        for _ in range(5):
            client.post("/api/token", auth=("alice", "wrong"))

        blocked_response = client.post(
            "/api/token", auth=("alice", portal_passwords["alice"])
        )

        assert blocked_response.status_code == 401


class TestBearerAuth:
    def test_bearer_auth_on_auth_endpoint(self, client: TestClient):
        token = _get_machine_token(client)

        response = client.get("/auth", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        assert response.json() == {"authenticated": True}

    def test_bearer_auth_on_download(self, client: TestClient):
        token = _get_machine_token(client)

        response = client.get(
            "/download",
            params={"platform": "linux-amd64"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        assert (
            "sing-box-v1.0.0-alice-linux-amd64.tar.gz"
            in response.headers["content-disposition"]
        )

    def test_expired_bearer_token_rejected(self, client: TestClient):
        from unittest.mock import patch

        machine_mgr = client.app.state.machine_token_manager
        token = machine_mgr.create_token("alice")

        import time

        with patch("sing_box_manager.web.auth.time") as mock_time:
            mock_time.time.return_value = time.time() + 366 * 86400
            response = client.get("/auth", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 401

    def test_bearer_for_disabled_user_rejected(self, client: TestClient):
        token = _get_machine_token(client, username="alice")

        # Remove alice from the users dict
        original_users = client.app.state.users.copy()
        del client.app.state.users["alice"]

        response = client.get("/auth", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 401

        # Restore
        client.app.state.users = original_users

    def test_basic_auth_still_works_on_download(
        self, client: TestClient, portal_passwords: dict[str, str]
    ):
        response = client.get(
            "/download",
            params={"platform": "linux-amd64"},
            auth=("alice", portal_passwords["alice"]),
        )

        assert response.status_code == 200
        assert (
            "sing-box-v1.0.0-alice-linux-amd64.tar.gz"
            in response.headers["content-disposition"]
        )


def test_password_checks_run_off_the_event_loop(
    client: TestClient,
    portal_passwords: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    """Argon2 on the loop stalls every request, downloads included."""
    import asyncio

    from sing_box_manager.web import routes

    verify = routes._verify_password
    on_loop: list[bool] = []

    def spy(password: str, password_hash: str) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return verify(password, password_hash)

    monkeypatch.setattr(routes, "_verify_password", spy)
    credentials = ("alice", portal_passwords["alice"])

    client.post("/login", data=dict(zip(("username", "password"), credentials)))
    client.post("/api/token", auth=credentials)
    client.get("/auth", auth=credentials)

    assert on_loop == [False, False, False]


def test_malformed_usernames_are_not_tracked_by_the_rate_limiter(
    client: TestClient,
):
    rate_limiter = client.app.state.rate_limiter

    client.post("/login", data={"username": "x" * 5000, "password": "p"})
    client.get("/auth", auth=("../../etc", "p"))

    assert rate_limiter._attempts == {}
