"""Tests for the VPS-derived billing cycle boundary."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import sing_box_manager.traffic_stats as traffic_stats
from sing_box_manager.settings import Settings, TrafficStatsSettings, VpsInfoSettings
from tests.conftest import utc_epoch


def _store(tmp_path: Path) -> traffic_stats._TrafficStatsStore:
    return traffic_stats._TrafficStatsStore(tmp_path / "traffic.sqlite3")


def _seed_daily(
    store: traffic_stats._TrafficStatsStore,
    rows: list[tuple[str, str, str, int, int, str]],
) -> None:
    with store._connect() as connection:
        connection.executemany(
            """
            INSERT INTO daily_usage (
                usage_date, username, protocol,
                upload_bytes, download_bytes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _monthly_rows(store: traffic_stats._TrafficStatsStore) -> dict[str, tuple]:
    connection = sqlite3.connect(store._database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT cycle_month, username, protocol, upload_bytes,"
            " download_bytes, updated_at FROM monthly_usage"
        ).fetchall()
    finally:
        connection.close()
    return {
        f"{r['cycle_month']}|{r['username']}|{r['protocol']}": (
            int(r["upload_bytes"]),
            int(r["download_bytes"]),
            str(r["updated_at"]),
        )
        for r in rows
    }


# --------------------------------------------------------------------------
# Cycle arithmetic
# --------------------------------------------------------------------------


def test_billing_day_one_is_the_calendar_month() -> None:
    for day in (date(2026, 4, 1), date(2026, 4, 15), date(2026, 4, 30)):
        assert traffic_stats._cycle_bounds(day, 1) == (
            "2026-04",
            date(2026, 4, 1),
            date(2026, 4, 30),
        )


def test_cycle_starts_on_the_billing_day() -> None:
    """The 1st and 2nd belong to the cycle that opened last month."""
    assert traffic_stats._cycle_bounds(date(2026, 9, 2), 3) == (
        "2026-08",
        date(2026, 8, 3),
        date(2026, 9, 2),
    )
    assert traffic_stats._cycle_bounds(date(2026, 9, 3), 3) == (
        "2026-09",
        date(2026, 9, 3),
        date(2026, 10, 2),
    )


def test_cycle_start_is_clamped_in_short_months() -> None:
    """A VPS that resets on the 31st still has to reset in February."""
    assert traffic_stats._cycle_bounds(date(2026, 2, 28), 31)[1] == date(2026, 2, 28)


@pytest.mark.parametrize("billing_day", [1, 3, 28, 29, 30, 31])
def test_cycles_tile_the_calendar_without_gap_or_overlap(billing_day: int) -> None:
    """Every day maps to exactly one cycle, and cycles are contiguous.

    This is the guard for months shorter than the billing day, where a naive
    implementation either skips days or double-counts them.
    """
    day = date(2026, 1, 1)
    seen: dict[str, tuple[date, date]] = {}
    while day < date(2028, 1, 1):
        key, start, end = traffic_stats._cycle_bounds(day, billing_day)
        assert start <= day <= end
        if key in seen:
            assert seen[key] == (start, end)
        else:
            seen[key] = (start, end)
        day += timedelta(days=1)

    windows = sorted(seen.values())
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert earlier[1] + timedelta(days=1) == later[0]


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------


def test_record_snapshot_buckets_by_the_billing_day(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for day, counters in (
        (date(2026, 9, 2), traffic_stats.UserTrafficCounters(100, 200)),
        (date(2026, 9, 3), traffic_stats.UserTrafficCounters(300, 500)),
    ):
        store.record_snapshot(
            service_name="trojan",
            usage_date=day,
            collected_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
            snapshot=traffic_stats._ServiceSnapshot(
                uptime_seconds=100 + day.day,
                counters={"alice": counters},
            ),
            billing_day=3,
        )

    rows = _monthly_rows(store)
    assert rows["2026-08|alice|trojan"][:2] == (100, 200)
    assert rows["2026-09|alice|trojan"][:2] == (200, 300)


def test_daily_and_monthly_totals_agree_over_a_cycle_window(tmp_path: Path) -> None:
    """The restated invariant: the sum over cycle_start..cycle_end matches."""
    store = _store(tmp_path)
    for offset in range(40):
        day = date(2026, 8, 20) + timedelta(days=offset)
        store.record_snapshot(
            service_name="trojan",
            usage_date=day,
            collected_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
            snapshot=traffic_stats._ServiceSnapshot(
                uptime_seconds=10_000 + offset,
                counters={"alice": traffic_stats.UserTrafficCounters(offset, offset)},
            ),
            billing_day=3,
        )

    key, start, end = traffic_stats._cycle_bounds(date(2026, 9, 10), 3)
    connection = sqlite3.connect(store._database_path)
    try:
        daily = connection.execute(
            "SELECT SUM(upload_bytes), SUM(download_bytes) FROM daily_usage"
            " WHERE usage_date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        monthly = connection.execute(
            "SELECT SUM(upload_bytes), SUM(download_bytes) FROM monthly_usage"
            " WHERE cycle_month = ?",
            (key,),
        ).fetchone()
    finally:
        connection.close()

    assert daily == monthly
    assert daily[0] > 0


# --------------------------------------------------------------------------
# Backfill
# --------------------------------------------------------------------------


def test_backfill_leaves_cycles_older_than_the_daily_data_alone(
    tmp_path: Path,
) -> None:
    """monthly_usage predates daily_usage by four months and was never backfilled.

    Those months are still rendered in the admin history chart, so a rebuild that
    is not bounded by the daily table's coverage would silently blank them.
    """
    store = _store(tmp_path)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO monthly_usage VALUES ('2026-05','alice','trojan',"
            "111,222,'2026-05-31T00:00:00+00:00')"
        )
        # The cycle that straddles the start of the daily data. Daily covers only
        # part of it, so a rebuild would understate it -- it must be left alone
        # and must not be swept up by the delete range either.
        connection.execute(
            "INSERT INTO monthly_usage VALUES ('2026-08','alice','trojan',"
            "333,444,'2026-08-31T00:00:00+00:00')"
        )
    _seed_daily(
        store,
        [
            ("2026-08-10", "alice", "trojan", 10, 20, "2026-08-10T00:00:00+00:00"),
            ("2026-09-05", "alice", "trojan", 30, 40, "2026-09-05T00:00:00+00:00"),
        ],
    )

    assert store.reconcile_billing_day(3, now=datetime(2026, 9, 6, tzinfo=UTC)) is True

    rows = _monthly_rows(store)
    assert rows["2026-05|alice|trojan"] == (111, 222, "2026-05-31T00:00:00+00:00")
    assert rows["2026-08|alice|trojan"] == (333, 444, "2026-08-31T00:00:00+00:00")
    # Only the fully covered cycle is rebuilt.
    assert rows["2026-09|alice|trojan"][:2] == (30, 40)


def test_backfill_recomputes_monthly_usage_under_the_new_billing_day(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed_daily(
        store,
        [
            ("2026-08-03", "alice", "trojan", 1, 1, "2026-08-03T00:00:00+00:00"),
            ("2026-09-01", "alice", "trojan", 2, 2, "2026-09-01T00:00:00+00:00"),
            ("2026-09-02", "alice", "trojan", 4, 4, "2026-09-02T00:00:00+00:00"),
            ("2026-09-03", "alice", "trojan", 8, 8, "2026-09-03T00:00:00+00:00"),
        ],
    )

    store.reconcile_billing_day(3, now=datetime(2026, 9, 4, tzinfo=UTC))

    rows = _monthly_rows(store)
    # 08-03 through 09-02 is one cycle; 09-03 opens the next.
    assert rows["2026-08|alice|trojan"][:2] == (7, 7)
    assert rows["2026-09|alice|trojan"][:2] == (8, 8)


def test_backfill_preserves_the_collection_timestamps(tmp_path: Path) -> None:
    """updated_at must stay the last collection, not the migration moment."""
    store = _store(tmp_path)
    _seed_daily(
        store,
        [
            ("2026-09-03", "alice", "trojan", 1, 1, "2026-09-03T04:00:00+00:00"),
            ("2026-09-04", "alice", "trojan", 2, 2, "2026-09-04T05:00:00+00:00"),
        ],
    )

    store.reconcile_billing_day(3, now=datetime(2027, 1, 1, tzinfo=UTC))

    assert _monthly_rows(store)["2026-09|alice|trojan"][2] == (
        "2026-09-04T05:00:00+00:00"
    )


def test_backfill_is_a_no_op_when_the_basis_already_matches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_daily(
        store,
        [("2026-09-03", "alice", "trojan", 1, 1, "2026-09-03T00:00:00+00:00")],
    )
    now = datetime(2026, 9, 4, tzinfo=UTC)

    assert store.reconcile_billing_day(3, now=now) is True
    assert store.reconcile_billing_day(3, now=now) is False


def test_a_database_without_metadata_is_treated_as_day_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_daily(
        store,
        [("2026-09-03", "alice", "trojan", 1, 1, "2026-09-03T00:00:00+00:00")],
    )

    assert store.read_cycle_basis_day() == 1
    # Day 1 is the stored basis, so moving to 3 must actually recompute.
    assert store.reconcile_billing_day(3, now=datetime(2026, 9, 4, tzinfo=UTC)) is True
    assert store.read_cycle_basis_day() == 3


def test_backfill_does_nothing_without_daily_data(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO monthly_usage VALUES ('2026-05','alice','trojan',"
            "111,222,'2026-05-31T00:00:00+00:00')"
        )

    store.reconcile_billing_day(3, now=datetime(2026, 9, 4, tzinfo=UTC))

    assert _monthly_rows(store)["2026-05|alice|trojan"][:2] == (111, 222)


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------


def _settings(
    tmp_path: Path, *, configured: bool, inventory_path: Path | None = None
) -> Settings:
    return Settings(
        sing_box_version="1.13.21",
        config_root=tmp_path / "config",
        config_path=inventory_path,
        traffic_stats=TrafficStatsSettings(
            enabled=True,
            database_path=tmp_path / "traffic.sqlite3",
            timezone="Asia/Shanghai",
        ),
        vps_info=VpsInfoSettings(
            kiwi_veid="veid" if configured else "",
            kiwi_api_key="key" if configured else "",
        ),
    )


@pytest.fixture(autouse=True)
def _no_env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIWI_VEID", raising=False)
    monkeypatch.delenv("KIWI_API_KEY", raising=False)


def _stub_fetch(monkeypatch: pytest.MonkeyPatch, calls: list, result) -> None:
    def fake(veid: str, api_key: str) -> dict:
        calls.append((veid, api_key))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(traffic_stats.kiwivm, "fetch_service_info", fake)


def test_refresh_derives_and_persists_the_billing_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list = []
    _stub_fetch(
        monkeypatch,
        calls,
        {"error": 0, "data_next_reset": utc_epoch("2026-10-02T20:00:00")},
    )

    day = traffic_stats._refresh_billing_day(
        _settings(tmp_path, configured=True), store, now=datetime(2026, 9, 3, tzinfo=UTC)
    )

    # 20:00 UTC on the 2nd is already the 3rd in Asia/Shanghai.
    assert day == 3
    assert store.read_reported_billing_day() == 3
    assert len(calls) == 1


def test_refresh_skips_the_api_inside_the_refresh_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list = []
    _stub_fetch(
        monkeypatch,
        calls,
        {"error": 0, "data_next_reset": utc_epoch("2026-10-03T00:00:00")},
    )
    settings = _settings(tmp_path, configured=True)

    traffic_stats._refresh_billing_day(
        settings, store, now=datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    )
    day = traffic_stats._refresh_billing_day(
        settings, store, now=datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
    )

    assert day == 3
    assert len(calls) == 1, "a healthy deployment must not call KiwiVM every run"


def test_refresh_retries_sooner_while_no_day_has_been_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list = []
    _stub_fetch(monkeypatch, calls, RuntimeError("kiwivm down"))
    settings = _settings(tmp_path, configured=True)

    traffic_stats._refresh_billing_day(
        settings, store, now=datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    )
    day = traffic_stats._refresh_billing_day(
        settings, store, now=datetime(2026, 9, 3, 0, 20, tzinfo=UTC)
    )

    assert day == 1
    assert len(calls) == 2


def test_refresh_keeps_the_stored_day_when_kiwivm_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.record_billing_day_check(3, checked_at=datetime(2026, 9, 1, tzinfo=UTC))
    calls: list = []
    _stub_fetch(monkeypatch, calls, RuntimeError("kiwivm down"))

    day = traffic_stats._refresh_billing_day(
        _settings(tmp_path, configured=True), store, now=datetime(2026, 9, 3, tzinfo=UTC)
    )

    assert day == 3
    assert len(calls) == 1


def test_refresh_makes_no_call_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list = []
    _stub_fetch(monkeypatch, calls, {"error": 0, "data_next_reset": 1_790_000_000})

    day = traffic_stats._refresh_billing_day(
        _settings(tmp_path, configured=False),
        store,
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )

    assert day == 1
    assert calls == []


def test_refresh_ignores_a_junk_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list = []
    _stub_fetch(monkeypatch, calls, {"error": 0})

    day = traffic_stats._refresh_billing_day(
        _settings(tmp_path, configured=True), store, now=datetime(2026, 9, 3, tzinfo=UTC)
    )

    assert day == 1
    assert store.read_reported_billing_day() is None
    # The attempt is still recorded, so a broken API backs off.
    assert store.read_billing_day_checked_at() is not None


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_collect_records_under_the_derived_billing_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collection on the 2nd belongs to the cycle that opened on the 3rd."""
    from tests.test_traffic_stats import _write_inventory

    inventory_path = tmp_path / "runtime.yaml"
    _write_inventory(inventory_path)
    settings = _settings(tmp_path, configured=True, inventory_path=inventory_path)

    _stub_fetch(
        monkeypatch,
        [],
        {"error": 0, "data_next_reset": utc_epoch("2026-10-03T00:00:00")},
    )
    monkeypatch.setattr(
        traffic_stats,
        "_query_service_snapshot",
        lambda target: traffic_stats._ServiceSnapshot(
            uptime_seconds=100,
            counters={"alice": traffic_stats.UserTrafficCounters(10, 20)},
        ),
    )

    traffic_stats.collect_traffic_stats(
        settings, now=datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
    )

    assert {key.split("|")[0] for key in _monthly_rows(_store(tmp_path))} == {"2026-08"}


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def test_provider_uses_the_basis_the_rows_were_built_under(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_daily(
        store,
        [("2026-09-05", "alice", "trojan", 1, 1, "2026-09-05T00:00:00+00:00")],
    )
    store.reconcile_billing_day(3, now=datetime(2026, 9, 6, tzinfo=UTC))

    summary = traffic_stats.TrafficStatsProvider(
        store._database_path, "Asia/Shanghai"
    ).get_user_summary("alice", now=datetime(2026, 9, 6, tzinfo=UTC))

    assert summary.cycle_start == date(2026, 9, 3)
    assert summary.cycle_end == date(2026, 10, 2)
    assert summary.cycle_month == "2026-09"


def test_provider_defaults_to_the_calendar_month_on_a_fresh_database(
    tmp_path: Path,
) -> None:
    """Reading the billing day must not create the database as a side effect.

    The portal runs as a reader. If looking up the basis mkdir'd its way to an
    empty database, a misconfigured path would silently start reporting zeroes
    instead of surfacing that stats were never collected.
    """
    missing = tmp_path / "missing.sqlite3"

    summary = traffic_stats.TrafficStatsProvider(
        missing, "Asia/Shanghai"
    ).get_user_summary("alice", now=datetime(2026, 9, 6, tzinfo=UTC))

    assert summary.cycle_start == date(2026, 9, 1)
    assert summary.cycle_end == date(2026, 9, 30)
    assert not missing.exists()
