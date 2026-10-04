"""Collector commits, failure recovery, lifecycle, and completed-day retention."""

import sqlite3
from datetime import timedelta

import grpc
import pytest

from sing_box_manager import connection_stats
from sing_box_manager.traffic_models import (
    STREAM_ATTRIBUTED_RETENTION,
    LIVE_SNAPSHOT_MAX_AGE_SECONDS,
)
from tests.traffic_fixtures import (
    Clock,
    NOW,
    apply,
    collector,
    new,
    update,
    totals,
    usage_rows,
)


@pytest.mark.parametrize(
    "failure_table",
    ["daily_domain_usage", "daily_destination_usage", "stream_attributed_connections"],
)
def test_all_dates_usage_destinations_and_checkpoints_roll_back_together(
    tmp_path, failure_table
):
    clock = Clock(NOW.replace(hour=23, minute=59))
    c = collector(tmp_path / "stats.db", clock)
    apply(c, clock, new(up=100, down=200, ip="192.0.2.1"))
    first_day = clock().date()
    clock.now += timedelta(minutes=2)
    apply(c, clock, update(up=20, down=30))
    with c._store._connect() as db:
        # Fail on the second date for usage; checkpoints follow all dated writes.
        condition = (
            "1"
            if failure_table == "stream_attributed_connections"
            else f"NEW.usage_date <> '{first_day}'"
        )
        db.execute(
            f"CREATE TRIGGER fail_flush BEFORE INSERT ON {failure_table} WHEN {condition} BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END"
        )
    c._flush()
    assert totals(c._store) == (0, 0, 0)
    assert totals(c._store, table="daily_destination_usage") == (0, 0, 0)
    assert c._store.read_connection_checkpoints("trojan") == {}
    assert c._aggregators["trojan"].prepare_batch() is not None
    with c._store._connect() as db:
        db.execute("DROP TRIGGER fail_flush")
    # Recreate the process and let the server replay its cumulative total.
    restarted = collector(tmp_path / "stats.db", clock)
    apply(restarted, clock, new(up=120, down=230, ip="192.0.2.1"), reset=True)
    restarted._flush()
    assert totals(restarted._store) == (120, 230, 1)
    assert totals(restarted._store, table="daily_destination_usage") == (120, 230, 1)


def test_failed_batch_is_retried_before_newer_checkpoints(tmp_path, monkeypatch):
    clock = Clock()
    c = collector(tmp_path / "stats.db", clock)
    apply(c, clock, new(up=100))
    commit = c._store.commit_stream_batch

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("fixture")

    monkeypatch.setattr(c._store, "commit_stream_batch", fail)
    c._flush()
    failed_batch = c._aggregators["trojan"].prepare_batch()
    apply(c, clock, update(up=20))
    assert c._aggregators["trojan"].prepare_batch() is failed_batch
    monkeypatch.setattr(c._store, "commit_stream_batch", commit)
    c._flush()
    assert totals(c._store) == (120, 0, 1)
    assert (
        c._store.read_connection_checkpoints("trojan")["c1"].counters.upload_bytes
        == 120
    )
    restarted = collector(tmp_path / "stats.db", clock)
    apply(restarted, clock, new(up=120), reset=True)
    restarted._flush()
    assert totals(restarted._store) == (120, 0, 1)


def test_retry_after_lost_commit_acknowledgement_is_idempotent(tmp_path, monkeypatch):
    clock = Clock()
    c = collector(tmp_path / "stats.db", clock)
    apply(c, clock, new(up=100))
    commit = c._store.commit_stream_batch

    def committed_then_failed(*args, **kwargs):
        commit(*args, **kwargs)
        raise sqlite3.OperationalError("fixture lost acknowledgement")

    monkeypatch.setattr(c._store, "commit_stream_batch", committed_then_failed)
    c._flush()
    assert totals(c._store) == (100, 0, 1)
    apply(c, clock, update(up=20))
    monkeypatch.setattr(c._store, "commit_stream_batch", commit)
    c._flush()
    assert totals(c._store) == (120, 0, 1)


def test_snapshot_failure_does_not_retry_committed_usage(tmp_path, monkeypatch):
    clock = Clock()
    c = collector(tmp_path / "stats.db", clock)
    apply(c, clock, new(up=100))

    def fail(**kwargs):
        raise sqlite3.OperationalError("fixture snapshot failure")

    monkeypatch.setattr(c._store, "replace_live_connections", fail)
    c._flush()
    c._flush()
    assert totals(c._store) == (100, 0, 1)
    assert c._aggregators["trojan"].prepare_batch() is None


@pytest.mark.parametrize("closed", [False, True])
def test_zero_byte_count_survives_restart(tmp_path, closed):
    clock = Clock()
    path = tmp_path / "stats.db"
    event = new(closed_at=clock() if closed else None)
    for _ in range(3):
        c = collector(path, clock)
        apply(c, clock, event, reset=True)
        c._flush()
        assert totals(c._store) == (0, 0, 1)
        assert c._store.read_connection_checkpoints("trojan")["c1"].counted


def test_all_live_checkpoints_survive_closed_connection_churn_and_restart(tmp_path):
    clock = Clock()
    path = tmp_path / "stats.db"
    c = collector(path, clock)
    # More live connections than either the UI cap or retired checkpoint cap.
    live = [new(f"live-{i}", up=1) for i in range(STREAM_ATTRIBUTED_RETENTION + 1)]
    apply(c, clock, *live, reset=True)
    c._flush()
    closed = [
        new(f"closed-{i}", up=1, closed_at=clock() + timedelta(milliseconds=i))
        for i in range(STREAM_ATTRIBUTED_RETENTION + 100)
    ]
    apply(c, clock, *closed)
    c._flush()
    saved = c._store.read_connection_checkpoints("trojan")
    assert sum(cp.retired_at is None for cp in saved.values()) == len(live)
    assert (
        sum(cp.retired_at is not None for cp in saved.values())
        == STREAM_ATTRIBUTED_RETENTION
    )
    assert "closed-0" not in saved
    assert "closed-100" in saved
    before = totals(c._store)
    restarted = collector(path, clock)
    apply(restarted, clock, *live, *reversed(closed[-1000:]), reset=True)
    restarted._flush()
    assert totals(restarted._store) == before


def test_idle_connection_retirement_is_persisted_without_new_bytes(tmp_path):
    clock = Clock()
    c = collector(tmp_path / "stats.db", clock)
    apply(c, clock, new(up=100))
    c._flush()
    clock.now += timedelta(days=1)
    apply(c, clock, new(up=100, closed_at=clock()), reset=True)
    c._flush()
    cp = c._store.read_connection_checkpoints("trojan")["c1"]
    assert cp.retired_at == clock()
    assert totals(c._store) == (100, 0, 1)


def test_reset_discards_only_checkpoints_absent_from_complete_replay(tmp_path):
    clock = Clock()
    c = collector(tmp_path / "stats.db", clock)
    apply(c, clock, new("gone", up=100), new("retained", up=10))
    c._flush()
    apply(c, clock, new("retained", up=20), reset=True)
    c._flush()
    assert set(c._store.read_connection_checkpoints("trojan")) == {"retained"}
    assert totals(c._store) == (120, 0, 2)


@pytest.mark.parametrize("failure", [None, grpc.RpcError, RuntimeError])
def test_subscription_end_invalidates_only_that_service(tmp_path, monkeypatch, failure):
    clock = Clock()
    c = collector(tmp_path / "stats.db", clock, protocols=("trojan", "naive"))
    apply(c, clock, new("same-id", up=200), protocol="naive")

    def stream(**kwargs):
        from sing_box_manager.proto import sing_box_api_pb2 as pb

        yield pb.ConnectionEvents(reset=True, events=[new("same-id", up=100)])
        c._flush()
        assert len(c._store.read_live_connections(now=clock())) == 2
        if failure:
            raise failure("fixture disconnect")

    monkeypatch.setattr(connection_stats, "open_stream", stream)
    if failure:
        with pytest.raises(failure):
            c._consume(c.endpoints[0])
    else:
        c._consume(c.endpoints[0])
    for _ in range(3):
        clock.now += timedelta(minutes=1)
        c._flush()
        live = c._store.read_live_connections(now=clock())
        assert [row.service_name for row in live] == ["naive"]
    assert (
        c._store.read_connection_checkpoints("trojan")["same-id"].counters.upload_bytes
        == 100
    )
    apply(c, clock, new("same-id", up=120), reset=True)
    c._flush()
    assert totals(c._store) == (320, 0, 2)


def test_live_snapshot_expires_after_unclean_shutdown(tmp_path):
    clock = Clock()
    c = collector(tmp_path / "stats.db", clock)
    apply(c, clock, new(up=100))
    c._flush()
    assert len(c._store.read_live_connections(now=clock())) == 1
    clock.now += timedelta(seconds=LIVE_SNAPSHOT_MAX_AGE_SECONDS + 1)
    assert c._store.read_live_connections(now=clock()) == ()
    assert totals(c._store) == (100, 0, 1)


def test_restart_does_not_compact_today_and_later_winner_survives(tmp_path):
    clock = Clock()
    path = tmp_path / "stats.db"
    c = collector(path, clock, top_n=1)
    apply(
        c,
        clock,
        new("a", up=100, domain="a.example"),
        new("b", up=90, domain="b.example"),
    )
    c._flush()
    restarted = collector(path, clock, top_n=1)
    apply(
        restarted,
        clock,
        new("a", up=100, domain="a.example"),
        new("b", up=110, domain="b.example"),
        reset=True,
    )
    restarted._flush()
    assert {r["domain"] for r in usage_rows(restarted._store)} == {
        "a.example",
        "b.example",
    }
    # A prolonged outage must still compact the unfinished historical day.
    clock.now += timedelta(days=4)
    restarted = collector(path, clock, top_n=1)
    restarted._flush()
    assert {r["domain"]: r["upload_bytes"] for r in usage_rows(restarted._store)} == {
        "b.example": 110,
        "__other__": 100,
    }
    with restarted._store._connect() as db:
        assert (
            db.execute("SELECT compacted_at FROM domain_usage_days").fetchone()[0]
            is not None
        )


def test_failed_old_batch_blocks_compaction_until_all_its_updates_commit(
    tmp_path, monkeypatch
):
    clock = Clock(NOW.replace(hour=23, minute=59))
    c = collector(tmp_path / "stats.db", clock, top_n=1)
    apply(
        c,
        clock,
        new("a", up=100, domain="a.example"),
        new("b", up=90, domain="b.example"),
    )
    c._flush()
    apply(c, clock, update("b", up=20))
    commit = c._store.commit_stream_batch

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("fixture")

    monkeypatch.setattr(c._store, "commit_stream_batch", fail)
    clock.now += timedelta(minutes=2)
    c._flush()
    assert {r["domain"] for r in usage_rows(c._store)} == {"a.example", "b.example"}
    apply(c, clock, update("b", up=3))
    monkeypatch.setattr(c._store, "commit_stream_batch", commit)
    c._flush()
    rows = {
        (r["usage_date"], r["domain"]): r["upload_bytes"] for r in usage_rows(c._store)
    }
    assert rows[(NOW.date().isoformat(), "b.example")] == 110
    assert rows[(NOW.date().isoformat(), "__other__")] == 100
    assert rows[(clock().date().isoformat(), "b.example")] == 3


def test_final_flush_commits_usage_and_clears_live_snapshot(tmp_path):
    clock = Clock()
    c = collector(tmp_path / "stats.db", clock)
    apply(c, clock, new(up=100))
    c._flush(final=True)
    assert totals(c._store) == (100, 0, 1)
    assert c._store.read_live_connections(now=clock()) == ()
