"""Tests for the per-domain breakdown written by the connection-stream daemon.

These tests exercise storage operations on dated fixture usage. Collector tests
cover transactions, replay, failures, and completion scheduling together.
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from pathlib import Path

from sing_box_manager import traffic_stats
from sing_box_manager.traffic_models import (
    ConnectionCheckpoint,
    StreamBatch,
    DatedDomainDelta,
)
from sing_box_manager.traffic_store import SCHEMA_VERSION

DAY = date(2026, 4, 1)
NOW = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
WINDOW = traffic_stats.UsageWindow(
    start_date=DAY, end_date=DAY, days=1, timezone_name="UTC"
)


def _store(tmp_path: Path) -> traffic_stats._TrafficStatsStore:
    return traffic_stats._TrafficStatsStore(tmp_path / "traffic.sqlite3")


def _write(
    store,
    *,
    service_name,
    usage_date,
    collected_at,
    deltas,
    attributions=None,
    retired=False,
):
    store.commit_stream_batch(
        StreamBatch(
            service_name,
            str(uuid4()),
            tuple(DatedDomainDelta(usage_date, delta) for delta in deltas),
            tuple(
                ConnectionCheckpoint(
                    key, value, retired_at=collected_at if retired else None
                )
                for key, value in (attributions or {}).items()
            ),
        ),
        collected_at=collected_at,
    )


def _checkpoint_counters(store, service_name):
    return {
        key: cp.counters
        for key, cp in store.read_connection_checkpoints(service_name).items()
    }


def _delta(
    domain: str, up: int, down: int, *, user: str = "alice", ip: str = ""
) -> traffic_stats.DomainDelta:
    return traffic_stats.DomainDelta(
        username=user,
        domain=domain,
        destination_ip=ip,
        upload_bytes=up,
        download_bytes=down,
        connection_count=1,
    )


def _domain_rows(
    path: Path, *, day=DAY, user="alice", protocol="trojan"
) -> dict[str, tuple[int, int]]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT domain, upload_bytes, download_bytes FROM daily_domain_usage WHERE usage_date = ? AND username = ? AND protocol = ?",
            (day.isoformat(), user, protocol),
        ).fetchall()
    return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}


def _record_authoritative(
    store: traffic_stats._TrafficStatsStore, upload: int, download: int
) -> None:
    """Write a per-user total the way the V2Ray poller does."""
    store.record_snapshot(
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        snapshot=traffic_stats._ServiceSnapshot(
            uptime_seconds=100,
            counters={"alice": traffic_stats.UserTrafficCounters(upload, download)},
        ),
        billing_day=1,
    )


def test_schema_version_is_stamped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta("example.com", 1, 2)],
    )

    with sqlite3.connect(tmp_path / "traffic.sqlite3") as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert version == SCHEMA_VERSION


def test_stream_deltas_accumulate_per_domain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(3):
        _write(
            store,
            service_name="trojan",
            usage_date=DAY,
            collected_at=NOW,
            deltas=[_delta("example.com", 10, 20), _delta("wikipedia.org", 1, 2)],
        )

    assert _domain_rows(tmp_path / "traffic.sqlite3") == {
        "example.com": (30, 60),
        "wikipedia.org": (3, 6),
    }


def test_stream_deltas_do_not_touch_the_authoritative_tables(tmp_path: Path) -> None:
    """The daemon must never write quota numbers.

    daily_usage stays the exclusive property of the V2Ray poller, so a daemon
    bug cannot inflate anyone's bill.
    """
    store = _store(tmp_path)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta("example.com", 999, 999)],
    )

    with sqlite3.connect(tmp_path / "traffic.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM monthly_usage").fetchone()[0] == 0
        )


def test_destination_rows_are_written_only_when_an_ip_is_known(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[
            _delta("example.com", 10, 20, ip="93.184.216.34"),
            _delta("nowhere.invalid", 5, 5),
        ],
    )

    breakdown = store.read_destination_breakdown(
        username=None,
        window=WINDOW,
        limit=10,
    )

    assert [item.destination_ip for item in breakdown.destinations] == ["93.184.216.34"]


def test_compaction_folds_the_tail_into_other_without_losing_bytes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    deltas = [_delta(f"host{index}.example", index, index) for index in range(1, 11)]
    _write(
        store, service_name="trojan", usage_date=DAY, collected_at=NOW, deltas=deltas
    )
    total_before = sum(d.upload_bytes + d.download_bytes for d in deltas)

    folded = store.compact_completed_days(
        before_date=DAY + timedelta(days=1), top_n=3, collected_at=NOW
    )

    rows = _domain_rows(tmp_path / "traffic.sqlite3")
    assert folded == 7
    # The three largest survive by name; everything else is one bucket.
    assert set(rows) == {
        "host10.example",
        "host9.example",
        "host8.example",
        traffic_stats.OTHER_DOMAIN_KEY,
    }
    assert sum(up + down for up, down in rows.values()) == total_before


def test_compaction_is_idempotent(tmp_path: Path) -> None:
    """Running twice must not fold the __other__ bucket into itself."""
    store = _store(tmp_path)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta(f"host{index}.example", index, index) for index in range(1, 8)],
    )

    store.compact_completed_days(
        before_date=DAY + timedelta(days=1), top_n=2, collected_at=NOW
    )
    first = _domain_rows(tmp_path / "traffic.sqlite3")
    store.compact_completed_days(
        before_date=DAY + timedelta(days=1), top_n=2, collected_at=NOW
    )

    assert _domain_rows(tmp_path / "traffic.sqlite3") == first


def test_reconciliation_parks_the_shortfall_the_stream_missed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_authoritative(store, 1000, 2000)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta("example.com", 400, 500)],
    )

    parked = store.reconcile_domain_attribution(collected_at=NOW)

    rows = _domain_rows(tmp_path / "traffic.sqlite3")
    assert rows[traffic_stats.UNATTRIBUTED_DOMAIN_KEY] == (600, 1500)
    assert parked == 600 + 1500
    # The whole point: the breakdown now sums to the quota number.
    assert sum(up for up, _ in rows.values()) == 1000
    assert sum(down for _, down in rows.values()) == 2000


def test_reconciliation_is_idempotent(tmp_path: Path) -> None:
    """A second pass with no new traffic must not double the gap."""
    store = _store(tmp_path)
    _record_authoritative(store, 1000, 2000)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta("example.com", 400, 500)],
    )

    store.reconcile_domain_attribution(collected_at=NOW)
    assert store.reconcile_domain_attribution(collected_at=NOW) == 0
    assert _domain_rows(tmp_path / "traffic.sqlite3")[
        traffic_stats.UNATTRIBUTED_DOMAIN_KEY
    ] == (600, 1500)


def test_reconciliation_clamps_when_the_stream_runs_ahead(tmp_path: Path) -> None:
    """The daemon flushes more often than the poller runs.

    Between a flush and the next five-minute poll the stream legitimately holds
    more bytes than daily_usage. That must produce no row at all rather than a
    negative one.
    """
    store = _store(tmp_path)
    _record_authoritative(store, 100, 100)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta("example.com", 400, 500)],
    )

    parked = store.reconcile_domain_attribution(collected_at=NOW)

    assert parked == 0
    assert traffic_stats.UNATTRIBUTED_DOMAIN_KEY not in _domain_rows(
        tmp_path / "traffic.sqlite3"
    )


def test_reconciliation_gives_the_gap_back_once_the_stream_catches_up(
    tmp_path: Path,
) -> None:
    """The bucket has to shrink as well as grow.

    The stream flushes once a minute and the poller runs every five, so a burst
    is routinely counted by the poller before the stream has flushed it. If that
    gap were added rather than restated, the stream's own bytes would land on
    top of it and the day would stay overstated by the largest lag ever seen.
    """
    store = _store(tmp_path)
    _record_authoritative(store, 1000, 0)

    assert store.reconcile_domain_attribution(collected_at=NOW) == 1000

    # The same bytes, now flushed by the daemon.
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta("example.com", 1000, 0)],
    )
    store.reconcile_domain_attribution(collected_at=NOW)

    rows = _domain_rows(tmp_path / "traffic.sqlite3")
    assert traffic_stats.UNATTRIBUTED_DOMAIN_KEY not in rows
    assert sum(up for up, _ in rows.values()) == 1000


def test_domain_breakdown_reports_the_whole_window_not_just_the_rows_kept(
    tmp_path: Path,
) -> None:
    """A share divided by the returned rows would always come to 100%."""
    store = _store(tmp_path)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta(f"host{index}.example", 100, 0) for index in range(5)],
    )

    breakdown = store.read_domain_breakdown(username="alice", window=WINDOW, limit=2)

    assert breakdown.total_bytes == 200
    assert breakdown.window_total_bytes == 500


def test_compaction_preserves_the_reconciliation_bucket(tmp_path: Path) -> None:
    """__unattributed__ is a health signal; folding it away would hide it."""
    store = _store(tmp_path)
    _record_authoritative(store, 10_000, 0)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta(f"host{index}.example", 100, 0) for index in range(1, 6)],
    )
    store.reconcile_domain_attribution(collected_at=NOW)

    store.compact_completed_days(
        before_date=DAY + timedelta(days=1), top_n=1, collected_at=NOW
    )

    rows = _domain_rows(tmp_path / "traffic.sqlite3")
    assert traffic_stats.UNATTRIBUTED_DOMAIN_KEY in rows
    assert sum(up for up, _ in rows.values()) == 10_000


def test_attributed_connections_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta("example.com", 1, 1)],
        attributions={
            "id-a": traffic_stats.UserTrafficCounters(10, 20),
            "id-b": traffic_stats.UserTrafficCounters(1, 2),
        },
    )

    assert _checkpoint_counters(store, "trojan") == {
        "id-a": traffic_stats.UserTrafficCounters(10, 20),
        "id-b": traffic_stats.UserTrafficCounters(1, 2),
    }
    # Ids are namespaced per service: the same uuid on another service is new.
    assert _checkpoint_counters(store, "naive") == {}


def test_attributed_connections_overwrite_rather_than_accumulate(
    tmp_path: Path,
) -> None:
    """The stored value is a running total, not a delta.

    Adding on conflict would inflate it on every flush of a long-lived
    connection and make the replay remainder go permanently negative.
    """
    store = _store(tmp_path)
    for counters in (
        traffic_stats.UserTrafficCounters(10, 20),
        traffic_stats.UserTrafficCounters(30, 40),
    ):
        _write(
            store,
            service_name="trojan",
            usage_date=DAY,
            collected_at=NOW,
            deltas=[],
            attributions={"id-a": counters},
        )

    assert _checkpoint_counters(store, "trojan") == {
        "id-a": traffic_stats.UserTrafficCounters(30, 40)
    }


def test_retired_connections_are_pruned_by_retirement(tmp_path: Path) -> None:
    """Pruning has to be bounded, but must outlast the server's replay ring."""
    store = _store(tmp_path)
    limit = traffic_stats.STREAM_ATTRIBUTED_RETENTION
    _write(
        store,
        service_name="trojan",
        usage_date=DAY,
        collected_at=NOW,
        deltas=[_delta("example.com", 1, 1)],
        retired=True,
        attributions={
            f"id-{index}": traffic_stats.UserTrafficCounters(1, 1)
            for index in range(limit + 500)
        },
    )

    assert len(_checkpoint_counters(store, "trojan")) == limit


def test_live_connections_snapshot_is_replaced_not_merged(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def snapshot(connection_id: str) -> traffic_stats.LiveConnection:
        return traffic_stats.LiveConnection(
            connection_id=connection_id,
            service_name="trojan",
            username="alice",
            network="tcp",
            protocol="",
            source="10.0.0.1:5000",
            destination="example.com:443",
            domain="example.com",
            outbound="direct",
            created_at=NOW,
            upload_bytes=1,
            download_bytes=2,
            updated_at=NOW,
        )

    store.replace_live_connections(
        service_name="trojan", connections=[snapshot("a")], collected_at=NOW
    )
    store.replace_live_connections(
        service_name="trojan", connections=[snapshot("b")], collected_at=NOW
    )

    assert [item.connection_id for item in store.read_live_connections(now=NOW)] == [
        "b"
    ]


def test_live_connections_of_one_service_survive_another_service_update(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    def snapshot(service: str, connection_id: str) -> traffic_stats.LiveConnection:
        return traffic_stats.LiveConnection(
            connection_id=connection_id,
            service_name=service,
            username="alice",
            network="tcp",
            protocol="",
            source="10.0.0.1:5000",
            destination="example.com:443",
            domain="example.com",
            outbound="direct",
            created_at=None,
            upload_bytes=1,
            download_bytes=2,
            updated_at=NOW,
        )

    store.replace_live_connections(
        service_name="trojan", connections=[snapshot("trojan", "a")], collected_at=NOW
    )
    store.replace_live_connections(
        service_name="naive", connections=[snapshot("naive", "b")], collected_at=NOW
    )

    assert {item.connection_id for item in store.read_live_connections(now=NOW)} == {
        "a",
        "b",
    }
    store.clear_live_connections("naive")
    assert {item.connection_id for item in store.read_live_connections(now=NOW)} == {
        "a"
    }


def test_domain_reads_tolerate_a_missing_database(tmp_path: Path) -> None:
    """The portal reads before the daemon has ever run."""
    store = traffic_stats._TrafficStatsStore(tmp_path / "absent.sqlite3")

    assert (
        store.read_domain_breakdown(
            username="alice",
            window=WINDOW,
            limit=10,
        ).domains
        == ()
    )
    assert store.read_live_connections(now=NOW) == ()
    assert _checkpoint_counters(store, "trojan") == {}
    assert not (tmp_path / "absent.sqlite3").exists()
