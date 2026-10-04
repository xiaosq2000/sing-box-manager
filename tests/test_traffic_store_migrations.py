"""Upgrade populated traffic databases without losing replay or usage state."""

import sqlite3
from pathlib import Path

import pytest

from sing_box_manager.traffic_models import TrafficStatsError
from sing_box_manager.traffic_store import SCHEMA_VERSION, TrafficStatsStore
from tests.traffic_fixtures import Clock, NOW, apply, collector, new, totals

LEGACY_SCHEMA = Path(__file__).parent / "fixtures" / "traffic-v2.sql"


def legacy_database(path):
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA.read_text())
        db.execute(
            "INSERT INTO daily_usage VALUES (?, 'alice', 'trojan', 100, 200, ?)",
            (NOW.date().isoformat(), NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO monthly_usage VALUES ('2026-04', 'alice', 'trojan', 100, 200, ?)",
            (NOW.isoformat(),),
        )
        db.execute(
            "INSERT INTO daily_domain_usage VALUES (?, 'alice', 'trojan', 'example.com', 100, 200, 1, ?)",
            (NOW.date().isoformat(), NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO service_state VALUES ('trojan', 100, ?)", (NOW.isoformat(),)
        )
        db.execute(
            "INSERT INTO service_user_counters VALUES ('trojan', 'alice', 100, 200)"
        )
        db.execute(
            "INSERT INTO stream_attributed_connections VALUES ('trojan', 'c1', 100, 200, ?)",
            (NOW.isoformat(),),
        )
        db.execute(
            "INSERT INTO live_connections VALUES ('c1', 'trojan', 'alice', 'tcp', '', '', 'example.com:443', 'example.com', 'direct', '', 100, 200, ?)",
            (NOW.isoformat(),),
        )


def test_populated_legacy_database_preserves_usage_and_deduplicates_replay(tmp_path):
    path = tmp_path / "stats.db"
    legacy_database(path)
    store = TrafficStatsStore(path)
    cp = store.read_connection_checkpoints("trojan")["c1"]
    assert cp.counters.upload_bytes == 100
    assert cp.counted is True
    assert cp.retired_at is None  # Unknown lifecycle stays protected until replay.
    assert totals(store) == (100, 200, 1)
    assert len(store.read_live_connections(now=NOW)) == 1
    with store._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            db.execute("SELECT usage_date FROM service_state").fetchone()[0]
            == NOW.date().isoformat()
        )
        assert tuple(
            db.execute(
                "SELECT upload_bytes, download_bytes FROM monthly_usage"
            ).fetchone()
        ) == (100, 200)
        assert (
            db.execute("SELECT compacted_at FROM domain_usage_days").fetchone()[0]
            is None
        )
    c = collector(path, Clock())
    apply(c, Clock(), new(up=120, down=230), reset=True)
    c._flush()
    assert totals(c._store) == (120, 230, 1)
    assert (
        TrafficStatsStore(path)
        .read_connection_checkpoints("trojan")["c1"]
        .counters.upload_bytes
        == 120
    )


def test_failed_migration_rolls_back_schema_and_version(tmp_path):
    path = tmp_path / "stats.db"
    legacy_database(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.set_authorizer(
            lambda action, *args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_ALTER_TABLE
                else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            TrafficStatsStore(path)._initialize(db)
        db.set_authorizer(None)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'stream_commits'"
            ).fetchone()
            is None
        )
        assert "retired_at" not in {
            row[1]
            for row in db.execute("PRAGMA table_info(stream_attributed_connections)")
        }
    # The same fixture can be upgraded normally after the transient failure.
    assert TrafficStatsStore(path).read_connection_checkpoints("trojan")["c1"].counted


def test_newer_schema_is_rejected_without_rewriting_its_version(tmp_path):
    path = tmp_path / "stats.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(TrafficStatsError, match="Unsupported traffic schema version"):
        TrafficStatsStore(path).read_connection_checkpoints("trojan")
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_unversioned_monthly_only_database_keeps_its_history(tmp_path):
    path = tmp_path / "stats.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE monthly_usage (cycle_month TEXT, username TEXT, protocol TEXT, upload_bytes INTEGER, download_bytes INTEGER, updated_at TEXT, PRIMARY KEY(cycle_month, username, protocol))"
        )
        db.execute(
            "INSERT INTO monthly_usage VALUES ('2025-01', 'alice', 'trojan', 100, 200, ?)",
            (NOW.isoformat(),),
        )
    store = TrafficStatsStore(path)
    assert store.read_connection_checkpoints("trojan") == {}
    with store._connect() as db:
        assert tuple(
            db.execute(
                "SELECT cycle_month, upload_bytes, download_bytes FROM monthly_usage"
            ).fetchone()
        ) == ("2025-01", 100, 200)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_legacy_writer_restamping_version_does_not_repeat_the_upgrade(tmp_path):
    path = tmp_path / "stats.db"
    legacy_database(path)
    c = collector(path, Clock())
    apply(c, Clock(), new(up=120, down=230, closed_at=NOW), reset=True)
    c._flush()
    expected = c._store.read_connection_checkpoints("trojan")
    # Old binaries used to write their own version unconditionally on connect.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 2")
    reopened = TrafficStatsStore(path)
    assert reopened.read_connection_checkpoints("trojan") == expected
    assert totals(reopened) == (120, 230, 1)
    with reopened._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
