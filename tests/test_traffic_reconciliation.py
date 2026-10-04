"""Observed-day attribution and bounded cross-day poll reconciliation."""

from datetime import UTC, date, datetime, timedelta

import pytest

from sing_box_manager.traffic_store import TrafficStatsStore
from tests.traffic_fixtures import (
    Clock,
    apply,
    collector,
    new,
    update,
    poll,
    totals,
    usage_rows,
    write_usage,
)


def offsets(store):
    with store._connect() as db:
        return [
            dict(row) for row in db.execute("SELECT * FROM domain_boundary_offsets")
        ]


@pytest.mark.parametrize("stream_first", [False, True])
@pytest.mark.parametrize(
    "timezone,before",
    [
        ("UTC", datetime(2026, 3, 31, 23, 55, tzinfo=UTC)),
        ("Asia/Shanghai", datetime(2026, 3, 31, 15, 55, tzinfo=UTC)),
    ],
)
def test_midnight_excess_is_matched_before_unattributed_bytes_are_added(
    tmp_path, stream_first, timezone, before
):
    clock = Clock(before)
    c = collector(tmp_path / "stats.db", clock, timezone=timezone)
    apply(c, clock, new(up=100, down=200))
    c._flush()
    poll(c._store, clock(), 100, 200, timezone=timezone)
    clock.now += timedelta(minutes=4)
    apply(c, clock, update(up=50, down=70))
    if stream_first:
        c._flush()
    clock.now += timedelta(minutes=6)
    poll(c._store, clock(), 150, 270, timezone=timezone)
    c._store.reconcile_domain_attribution(collected_at=clock())
    c._flush()
    c._store.reconcile_domain_attribution(collected_at=clock())
    assert totals(c._store) == (150, 270, 1)
    assert {r["domain"] for r in usage_rows(c._store)} == {"example.com"}
    assert [
        (r["source_date"], r["target_date"], r["upload_bytes"], r["download_bytes"])
        for r in offsets(c._store)
    ] == [("2026-03-31", "2026-04-01", 50, 70)]
    # Both source totals remain on their original days, even across a cycle.
    assert {r["usage_date"]: r["upload_bytes"] for r in usage_rows(c._store)} == {
        "2026-03-31": 150
    }
    assert {
        r["usage_date"]: r["upload_bytes"]
        for r in usage_rows(c._store, table="daily_usage")
    } == {"2026-03-31": 100, "2026-04-01": 50}
    reopened = TrafficStatsStore(tmp_path / "stats.db")
    assert reopened.reconcile_domain_attribution(collected_at=clock()) == 0
    assert totals(reopened) == (150, 270, 1)


def test_boundary_capacity_cannot_hide_later_same_day_missing_traffic(tmp_path):
    store = TrafficStatsStore(tmp_path / "stats.db")
    before = datetime(2026, 4, 1, 23, 55, tzinfo=UTC)
    after = before + timedelta(minutes=10)
    poll(store, before, 100)
    write_usage(store, before.date(), 200)
    poll(store, after, 150)  # Only these 50 bytes can have crossed midnight.
    poll(store, after + timedelta(hours=1), 250)
    store.reconcile_domain_attribution(collected_at=after)
    assert offsets(store)[0]["upload_bytes"] == 50
    assert [
        (r["usage_date"], r["upload_bytes"])
        for r in usage_rows(store)
        if r["domain"] == "__unattributed__"
    ] == [(after.date().isoformat(), 100)]


def test_excess_cannot_be_carried_into_unrelated_later_days(tmp_path):
    store = TrafficStatsStore(tmp_path / "stats.db")
    before = datetime(2026, 4, 1, 23, 55, tzinfo=UTC)
    after = before + timedelta(minutes=10)
    poll(store, before, 100)
    write_usage(store, before.date(), 150)
    poll(store, after, 100)
    poll(store, after + timedelta(days=1), 150)
    store.reconcile_domain_attribution(collected_at=after)
    assert offsets(store) == []
    assert (
        sum(
            r["upload_bytes"]
            for r in usage_rows(store)
            if r["domain"] == "__unattributed__"
        )
        == 50
    )


def test_poll_outage_links_only_the_days_that_interval_spans(tmp_path):
    store = TrafficStatsStore(tmp_path / "stats.db")
    before = datetime(2026, 4, 1, 23, 55, tzinfo=UTC)
    after = before + timedelta(days=3, minutes=10)
    poll(store, before, 100)
    write_usage(store, before.date(), 110)
    write_usage(store, before.date() + timedelta(days=1), 20)
    write_usage(store, before.date() + timedelta(days=2), 30)
    poll(store, after, 170)  # 60 observed, 10 missing over the outage.
    store.reconcile_domain_attribution(collected_at=after)
    assert sum(r["upload_bytes"] for r in offsets(store)) == 60
    assert totals(store)[0] == 170
    assert (
        sum(
            r["upload_bytes"]
            for r in usage_rows(store)
            if r["domain"] == "__unattributed__"
        )
        == 10
    )


@pytest.mark.parametrize("user,protocol", [("bob", "trojan"), ("alice", "naive")])
def test_boundary_matching_never_crosses_users_or_services(tmp_path, user, protocol):
    store = TrafficStatsStore(tmp_path / "stats.db")
    before = datetime(2026, 4, 1, 23, 55, tzinfo=UTC)
    after = before + timedelta(minutes=10)
    poll(store, before, 100)
    write_usage(store, before.date(), 150)
    poll(store, after, 100)
    poll(store, before, 0, user=user, protocol=protocol)
    poll(store, after, 50, user=user, protocol=protocol)
    store.reconcile_domain_attribution(collected_at=after)
    assert offsets(store) == []
    gap = [r for r in usage_rows(store) if r["domain"] == "__unattributed__"]
    assert [(r["username"], r["protocol"], r["upload_bytes"]) for r in gap] == [
        (user, protocol, 50)
    ]


def test_upload_excess_cannot_cancel_download_gap(tmp_path):
    store = TrafficStatsStore(tmp_path / "stats.db")
    before = datetime(2026, 4, 1, 23, 55, tzinfo=UTC)
    after = before + timedelta(minutes=10)
    poll(store, before, 100)
    write_usage(store, before.date(), 150)
    poll(store, after, 100, 50)
    store.reconcile_domain_attribution(collected_at=after)
    assert offsets(store) == []
    assert [
        (r["upload_bytes"], r["download_bytes"])
        for r in usage_rows(store)
        if r["domain"] == "__unattributed__"
    ] == [(0, 50)]


@pytest.mark.parametrize("up,uptime", [(150, 1), (50, 100)])
def test_counter_resets_do_not_create_boundary_credit(tmp_path, up, uptime):
    store = TrafficStatsStore(tmp_path / "stats.db")
    before = datetime(2026, 4, 1, 23, 55, tzinfo=UTC)
    after = before + timedelta(minutes=10)
    poll(store, before, 100)
    write_usage(store, before.date(), 200)
    poll(store, after, up, uptime=uptime)
    store.reconcile_domain_attribution(collected_at=after)
    assert offsets(store) == []
    assert (
        sum(
            r["upload_bytes"]
            for r in usage_rows(store)
            if r["domain"] == "__unattributed__"
        )
        == up
    )


def test_late_historical_flush_restates_both_gap_and_offsets(tmp_path):
    store = TrafficStatsStore(tmp_path / "stats.db")
    before = datetime(2026, 4, 1, 23, 55, tzinfo=UTC)
    after = before + timedelta(minutes=10)
    poll(store, before, 100)
    write_usage(store, before.date(), 100)
    poll(store, after, 150)
    store.reconcile_domain_attribution(collected_at=after)
    assert totals(store)[0] == 150
    assert offsets(store) == []
    write_usage(store, before.date(), 50, now=after + timedelta(days=5))
    store.reconcile_domain_attribution(collected_at=after + timedelta(days=5))
    assert totals(store)[0] == 150
    assert offsets(store)[0]["upload_bytes"] == 50
    assert {r["domain"] for r in usage_rows(store)} == {"example.com"}
