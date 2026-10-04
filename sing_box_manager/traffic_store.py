"""SQLite transactions and queries for traffic accounting."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from sing_box_manager.traffic_models import (
    DEFAULT_BILLING_DAY,
    LIVE_SNAPSHOT_MAX_AGE_SECONDS,
    OTHER_DOMAIN_KEY,
    RESERVED_DOMAIN_KEYS,
    STREAM_ATTRIBUTED_RETENTION,
    TRAFFIC_PROTOCOLS,
    UNATTRIBUTED_DOMAIN_KEY,
    ConnectionCheckpoint,
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
    StreamBatch,
    TrafficStatsError,
    UsageWindow,
    UserTrafficCounters,
    UserTrafficSummary,
    _counter_delta,
    _cycle_bounds,
    _date_range,
    _month_range,
    _normalize_billing_day,
    _parse_optional_datetime,
    _ServiceSnapshot,
    _utc_now,
)
from sing_box_manager.traffic_reconciliation import PollBoundary, reconcile_boundaries

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 3
_BILLING_DAY_KEY = "billing_day"
_BILLING_DAY_CHECKED_AT_KEY = "billing_day_checked_at"
_MONTHLY_USAGE_BILLING_DAY_KEY = "monthly_usage_billing_day"
_METADATA_SELECT = "SELECT value FROM stats_metadata WHERE key = ?"


class TrafficStatsStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            self._initialize(connection)
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise TrafficStatsError(f"Unsupported traffic schema version: {version}")
        if version == SCHEMA_VERSION:
            return
        # Serialize migrations across the poller, stream daemon, and portal.
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            connection.commit()
            return
        schema = """
            CREATE TABLE IF NOT EXISTS service_state (
                service_name TEXT PRIMARY KEY,
                uptime_seconds INTEGER NOT NULL,
                collected_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS service_user_counters (
                service_name TEXT NOT NULL,
                username TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                PRIMARY KEY (service_name, username)
            );

            CREATE TABLE IF NOT EXISTS monthly_usage (
                cycle_month TEXT NOT NULL,
                username TEXT NOT NULL,
                protocol TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (cycle_month, username, protocol)
            );

            CREATE TABLE IF NOT EXISTS daily_usage (
                usage_date TEXT NOT NULL,
                username TEXT NOT NULL,
                protocol TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (usage_date, username, protocol)
            );

            CREATE INDEX IF NOT EXISTS daily_usage_username_date
                ON daily_usage (username, usage_date);

            CREATE TABLE IF NOT EXISTS stats_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Observed domain usage, compacted tails, and the poller's
            -- estimated unattributed shortfall. Daily dates need not match
            -- daily_usage because the two collectors sample independently.
            CREATE TABLE IF NOT EXISTS daily_domain_usage (
                usage_date TEXT NOT NULL,
                username TEXT NOT NULL,
                protocol TEXT NOT NULL,
                domain TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                connection_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (usage_date, username, protocol, domain)
            );

            CREATE INDEX IF NOT EXISTS daily_domain_usage_username_date
                ON daily_domain_usage (username, usage_date);

            CREATE TABLE IF NOT EXISTS daily_destination_usage (
                usage_date TEXT NOT NULL,
                username TEXT NOT NULL,
                destination_ip TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                connection_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (usage_date, username, destination_ip)
            );

            CREATE INDEX IF NOT EXISTS daily_destination_usage_username_date
                ON daily_destination_usage (username, usage_date);

            -- A snapshot, not a log: the daemon replaces each service's rows on
            -- every tick and clears them on shutdown, so the ops view reads
            -- SQLite instead of opening its own gRPC stream.
            CREATE TABLE IF NOT EXISTS live_connections (
                connection_id TEXT PRIMARY KEY,
                service_name TEXT NOT NULL,
                username TEXT NOT NULL,
                network TEXT NOT NULL,
                protocol TEXT NOT NULL,
                source TEXT NOT NULL,
                destination TEXT NOT NULL,
                domain TEXT NOT NULL,
                outbound TEXT NOT NULL,
                created_at TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS live_connections_service
                ON live_connections (service_name);

            -- How many bytes of each connection are already in
            -- daily_domain_usage. Every subscription starts with a frame
            -- replaying live connections and up to 1000 already-closed ones, so
            -- without this a daemon restart would attribute them a second time.
            -- The counters are cumulative rather than a bare id set because a
            -- connection can be partly attributed through UPDATE deltas and then
            -- replayed with its final total; only the remainder is owed.
            CREATE TABLE IF NOT EXISTS stream_attributed_connections (
                service_name TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                attributed_at TEXT NOT NULL,
                PRIMARY KEY (service_name, connection_id)
            );

            CREATE INDEX IF NOT EXISTS stream_attributed_connections_service_time
                ON stream_attributed_connections (service_name, attributed_at);
            CREATE TABLE IF NOT EXISTS stream_commits (
                service_name TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS domain_usage_days (
                usage_date TEXT PRIMARY KEY,
                compacted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS poll_boundaries (
                protocol TEXT NOT NULL,
                username TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                PRIMARY KEY (protocol, username, end_date)
            );
            CREATE TABLE IF NOT EXISTS domain_boundary_offsets (
                protocol TEXT NOT NULL,
                username TEXT NOT NULL,
                source_date TEXT NOT NULL,
                target_date TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                PRIMARY KEY (protocol, username, source_date, target_date)
            );
            """
        try:
            statement = ""
            for line in schema.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            self._migrate_connection_state(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_connection_state(connection: sqlite3.Connection) -> None:
        """Add lifecycle and day-completion state while retaining legacy rows."""
        # Version 2 writers stamped user_version on every connection.
        # During an upgrade one can overwrite the marker after another
        # process has migrated. Inspect the additive columns as well.
        checkpoint_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(stream_attributed_connections)"
            )
        }
        if "counted" not in checkpoint_columns:
            connection.execute(
                "ALTER TABLE stream_attributed_connections ADD COLUMN counted INTEGER NOT NULL DEFAULT 1"
            )
        if "retired_at" not in checkpoint_columns:
            connection.execute(
                "ALTER TABLE stream_attributed_connections ADD COLUMN retired_at TEXT"
            )
        state_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(service_state)")
        }
        if "usage_date" not in state_columns:
            connection.execute("ALTER TABLE service_state ADD COLUMN usage_date TEXT")
            connection.execute("""
                UPDATE service_state SET usage_date = (
                    SELECT MAX(usage_date) FROM daily_usage
                    WHERE protocol = service_state.service_name
                )
            """)
        # Old checkpoints carry no lifecycle information. Protect them
        # until a complete replay establishes which connections remain.
        connection.execute(
            "DROP INDEX IF EXISTS stream_attributed_connections_service_time"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS stream_retirement ON stream_attributed_connections(service_name, retired_at)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO domain_usage_days SELECT DISTINCT usage_date, NULL FROM daily_domain_usage"
        )
        live_key = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(live_connections)")
            if row["pk"]
        }
        if live_key != {"service_name", "connection_id"}:
            connection.execute(
                "ALTER TABLE live_connections RENAME TO legacy_live_connections"
            )
            connection.execute("""
                CREATE TABLE live_connections (
                    connection_id TEXT NOT NULL, service_name TEXT NOT NULL,
                    username TEXT NOT NULL, network TEXT NOT NULL,
                    protocol TEXT NOT NULL, source TEXT NOT NULL,
                    destination TEXT NOT NULL, domain TEXT NOT NULL,
                    outbound TEXT NOT NULL, created_at TEXT NOT NULL,
                    upload_bytes INTEGER NOT NULL, download_bytes INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(service_name, connection_id)
                )
            """)
            connection.execute(
                "INSERT INTO live_connections SELECT * FROM legacy_live_connections"
            )
            connection.execute("DROP TABLE legacy_live_connections")

    def _previous_uptime(
        self, connection: sqlite3.Connection, service_name: str
    ) -> int | None:
        row = connection.execute(
            "SELECT uptime_seconds FROM service_state WHERE service_name = ?",
            (service_name,),
        ).fetchone()
        if row is None:
            return None
        return int(row["uptime_seconds"])

    def _previous_counters(
        self, connection: sqlite3.Connection, service_name: str
    ) -> dict[str, UserTrafficCounters]:
        rows = connection.execute(
            """
            SELECT username, upload_bytes, download_bytes
            FROM service_user_counters
            WHERE service_name = ?
            """,
            (service_name,),
        ).fetchall()
        return {
            str(row["username"]): UserTrafficCounters(
                upload_bytes=int(row["upload_bytes"]),
                download_bytes=int(row["download_bytes"]),
            )
            for row in rows
        }

    def record_snapshot(
        self,
        *,
        service_name: str,
        usage_date: date,
        collected_at: datetime,
        snapshot: _ServiceSnapshot,
        billing_day: int,
    ) -> None:
        """Fold one poll's deltas into the daily and monthly buckets.

        The caller passes the local calendar day, not the cycle key, because the
        cycle is a pure function of the day and the billing day. Deriving it here
        means the two tables are structurally unable to disagree at a cycle
        boundary, and the invariant below holds exactly, forever::

            SUM(daily_usage WHERE usage_date BETWEEN cycle_start AND cycle_end)
                == SUM(monthly_usage WHERE cycle_month = key)

        where ``(key, cycle_start, cycle_end) = _cycle_bounds(usage_date,
        billing_day)``. With the default billing day of 1 that window is exactly
        the calendar month.

        ``billing_day`` is required rather than defaulted: silently bucketing by
        the calendar month is the bug this parameter exists to fix.

        Both buckets consume the *same* delta values, computed once, so restart
        and counter-reset handling stays in one place and nothing double-counts.
        """
        cycle_month, _, _ = _cycle_bounds(usage_date, billing_day)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous_state = connection.execute(
                "SELECT usage_date FROM service_state WHERE service_name = ?",
                (service_name,),
            ).fetchone()
            previous_date = previous_state["usage_date"] if previous_state else None
            previous_uptime = self._previous_uptime(connection, service_name)
            previous_counters = self._previous_counters(connection, service_name)
            restart_detected = (
                previous_uptime is not None
                and snapshot.uptime_seconds < previous_uptime
            )

            for username, current in snapshot.counters.items():
                previous = previous_counters.get(username, UserTrafficCounters())
                upload_delta = _counter_delta(
                    current.upload_bytes,
                    previous.upload_bytes,
                    restart_detected,
                )
                download_delta = _counter_delta(
                    current.download_bytes,
                    previous.download_bytes,
                    restart_detected,
                )
                if (
                    previous_date
                    and previous_date < usage_date.isoformat()
                    and not restart_detected
                ):
                    connection.execute(
                        """INSERT INTO poll_boundaries VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(protocol, username, end_date) DO NOTHING""",
                        (
                            service_name,
                            username,
                            previous_date,
                            usage_date.isoformat(),
                            upload_delta
                            if current.upload_bytes >= previous.upload_bytes
                            else 0,
                            download_delta
                            if current.download_bytes >= previous.download_bytes
                            else 0,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO monthly_usage (
                        cycle_month,
                        username,
                        protocol,
                        upload_bytes,
                        download_bytes,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cycle_month, username, protocol) DO UPDATE SET
                        upload_bytes = monthly_usage.upload_bytes + excluded.upload_bytes,
                        download_bytes = monthly_usage.download_bytes + excluded.download_bytes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        cycle_month,
                        username,
                        service_name,
                        upload_delta,
                        download_delta,
                        collected_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO daily_usage (
                        usage_date,
                        username,
                        protocol,
                        upload_bytes,
                        download_bytes,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(usage_date, username, protocol) DO UPDATE SET
                        upload_bytes = daily_usage.upload_bytes + excluded.upload_bytes,
                        download_bytes =
                            daily_usage.download_bytes + excluded.download_bytes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        usage_date.isoformat(),
                        username,
                        service_name,
                        upload_delta,
                        download_delta,
                        collected_at.isoformat(),
                    ),
                )

            connection.execute(
                "DELETE FROM service_user_counters WHERE service_name = ?",
                (service_name,),
            )
            connection.executemany(
                """
                INSERT INTO service_user_counters (
                    service_name,
                    username,
                    upload_bytes,
                    download_bytes
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        service_name,
                        username,
                        counters.upload_bytes,
                        counters.download_bytes,
                    )
                    for username, counters in snapshot.counters.items()
                ],
            )
            connection.execute(
                """
                INSERT INTO service_state (service_name, uptime_seconds, collected_at, usage_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(service_name) DO UPDATE SET
                    uptime_seconds = excluded.uptime_seconds,
                    collected_at = excluded.collected_at,
                    usage_date = excluded.usage_date
                """,
                (
                    service_name,
                    snapshot.uptime_seconds,
                    collected_at.isoformat(),
                    usage_date.isoformat(),
                ),
            )

    def read_connection_checkpoints(
        self, service_name: str
    ) -> dict[str, ConnectionCheckpoint]:
        """How much of each connection is already folded into the daily rows.

        The daemon subtracts these from the totals sing-box reports, so a
        connection replayed after a restart contributes only what it owes.
        """
        if not self._database_path.exists():
            return {}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stream_attributed_connections WHERE service_name = ?",
                (service_name,),
            ).fetchall()
        return {
            str(row["connection_id"]): ConnectionCheckpoint(
                str(row["connection_id"]),
                UserTrafficCounters(
                    int(row["upload_bytes"]), int(row["download_bytes"])
                ),
                counted=bool(row["counted"]),
                retired_at=_parse_optional_datetime(row["retired_at"]),
            )
            for row in rows
        }

    def commit_stream_batch(
        self, batch: StreamBatch, *, collected_at: datetime
    ) -> None:
        """Commit every dated delta and checkpoint together, safely retrying a batch.

        There is one ordered writer and at most one unacknowledged batch per
        service. Keeping its last ID is enough to recognize a retry after a
        commit whose acknowledgement was lost, without retaining a batch log.
        """
        service_name = batch.service_name
        updated_at = _utc_now(collected_at).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT batch_id FROM stream_commits WHERE service_name = ?",
                (service_name,),
            ).fetchone()
            if previous and previous["batch_id"] == batch.batch_id:
                return
            for item in batch.deltas:
                delta = item.delta
                date_text = item.usage_date.isoformat()
                day = connection.execute(
                    "SELECT compacted_at FROM domain_usage_days WHERE usage_date = ?",
                    (date_text,),
                ).fetchone()
                if day and day["compacted_at"] is not None:
                    raise TrafficStatsError(
                        f"Cannot attribute bytes to compacted day {date_text}"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO domain_usage_days VALUES (?, NULL)",
                    (date_text,),
                )
                connection.execute(
                    """
                    INSERT INTO daily_domain_usage (
                        usage_date,
                        username,
                        protocol,
                        domain,
                        upload_bytes,
                        download_bytes,
                        connection_count,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(usage_date, username, protocol, domain) DO UPDATE SET
                        upload_bytes =
                            daily_domain_usage.upload_bytes + excluded.upload_bytes,
                        download_bytes =
                            daily_domain_usage.download_bytes + excluded.download_bytes,
                        connection_count =
                            daily_domain_usage.connection_count
                            + excluded.connection_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        date_text,
                        delta.username,
                        service_name,
                        delta.domain,
                        delta.upload_bytes,
                        delta.download_bytes,
                        delta.connection_count,
                        updated_at,
                    ),
                )
                if not delta.destination_ip:
                    continue

                connection.execute(
                    """
                    INSERT INTO daily_destination_usage (
                        usage_date,
                        username,
                        destination_ip,
                        upload_bytes,
                        download_bytes,
                        connection_count,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(usage_date, username, destination_ip) DO UPDATE SET
                        upload_bytes =
                            daily_destination_usage.upload_bytes
                            + excluded.upload_bytes,
                        download_bytes =
                            daily_destination_usage.download_bytes
                            + excluded.download_bytes,
                        connection_count =
                            daily_destination_usage.connection_count
                            + excluded.connection_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        date_text,
                        delta.username,
                        delta.destination_ip,
                        delta.upload_bytes,
                        delta.download_bytes,
                        delta.connection_count,
                        updated_at,
                    ),
                )

            self._write_checkpoints(connection, batch, updated_at)
            connection.execute(
                """INSERT INTO stream_commits VALUES (?, ?)
                   ON CONFLICT(service_name) DO UPDATE SET batch_id = excluded.batch_id""",
                (service_name, batch.batch_id),
            )

    @staticmethod
    def _write_checkpoints(
        connection: sqlite3.Connection, batch: StreamBatch, updated_at: str
    ) -> None:
        connection.executemany(
            """DELETE FROM stream_attributed_connections
               WHERE service_name = ? AND connection_id = ?""",
            [
                (batch.service_name, connection_id)
                for connection_id in batch.removed_connections
            ],
        )
        connection.executemany(
            """INSERT INTO stream_attributed_connections (
                service_name, connection_id, upload_bytes, download_bytes,
                attributed_at, counted, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service_name, connection_id) DO UPDATE SET
                upload_bytes = excluded.upload_bytes,
                download_bytes = excluded.download_bytes,
                attributed_at = excluded.attributed_at,
                counted = excluded.counted, retired_at = excluded.retired_at""",
            [
                (
                    batch.service_name,
                    cp.connection_id,
                    cp.counters.upload_bytes,
                    cp.counters.download_bytes,
                    updated_at,
                    cp.counted,
                    cp.retired_at.isoformat() if cp.retired_at else None,
                )
                for cp in batch.checkpoints
            ],
        )
        connection.execute(
            """DELETE FROM stream_attributed_connections
               WHERE service_name = ? AND retired_at IS NOT NULL
               AND connection_id NOT IN (
                   SELECT connection_id FROM stream_attributed_connections
                   WHERE service_name = ? AND retired_at IS NOT NULL
                   ORDER BY retired_at DESC, connection_id DESC LIMIT ?
               )""",
            (batch.service_name, batch.service_name, STREAM_ATTRIBUTED_RETENTION),
        )

    def replace_live_connections(
        self,
        *,
        service_name: str,
        connections: Sequence[LiveConnection],
        collected_at: datetime,
    ) -> None:
        """Swap one service's live snapshot atomically.

        Replacing rather than merging means a connection that vanished while the
        daemon was disconnected cannot linger in the ops view forever.
        """
        updated_at = _utc_now(collected_at).isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM live_connections WHERE service_name = ?",
                (service_name,),
            )
            connection.executemany(
                """
                INSERT INTO live_connections (
                    connection_id, service_name, username, network, protocol,
                    source, destination, domain, outbound, created_at,
                    upload_bytes, download_bytes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.connection_id,
                        service_name,
                        item.username,
                        item.network,
                        item.protocol,
                        item.source,
                        item.destination,
                        item.domain,
                        item.outbound,
                        "" if item.created_at is None else item.created_at.isoformat(),
                        item.upload_bytes,
                        item.download_bytes,
                        updated_at,
                    )
                    for item in connections
                ],
            )

    def clear_live_connections(self, service_name: str | None = None) -> None:
        with self._connect() as connection:
            if service_name is None:
                connection.execute("DELETE FROM live_connections")
            else:
                connection.execute(
                    "DELETE FROM live_connections WHERE service_name = ?",
                    (service_name,),
                )

    def compact_completed_days(
        self, *, before_date: date, top_n: int, collected_at: datetime
    ) -> int:
        """Compact unfinished days strictly before the collector's safe cutoff."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            days = connection.execute(
                "SELECT usage_date FROM domain_usage_days WHERE usage_date < ? AND compacted_at IS NULL ORDER BY usage_date",
                (before_date.isoformat(),),
            ).fetchall()
            folded = 0
            for row in days:
                usage_date = date.fromisoformat(row["usage_date"])
                folded += self._compact_domain_usage(
                    connection,
                    usage_date=usage_date,
                    top_n=top_n,
                    collected_at=collected_at,
                )
                connection.execute(
                    "UPDATE domain_usage_days SET compacted_at = ? WHERE usage_date = ?",
                    (collected_at.isoformat(), row["usage_date"]),
                )
            return folded

    @staticmethod
    def _compact_domain_usage(
        connection: sqlite3.Connection,
        *,
        usage_date: date,
        top_n: int,
        collected_at: datetime,
    ) -> int:
        """Fold the completed day's tail without changing bytes or counts."""
        date_text = usage_date.isoformat()
        updated_at = _utc_now(collected_at).isoformat()
        reserved = tuple(sorted(RESERVED_DOMAIN_KEYS))
        placeholders = ", ".join("?" for _ in reserved)
        folded = 0
        groups = connection.execute(
            """
            SELECT DISTINCT username, protocol FROM daily_domain_usage
            WHERE usage_date = ?
            """,
            (date_text,),
        ).fetchall()
        for group in groups:
            username = str(group["username"])
            protocol = str(group["protocol"])
            rows = connection.execute(
                f"""
                SELECT domain, upload_bytes, download_bytes, connection_count
                FROM daily_domain_usage
                WHERE usage_date = ? AND username = ? AND protocol = ?
                  AND domain NOT IN ({placeholders})
                ORDER BY (upload_bytes + download_bytes) DESC, domain ASC
                """,  # noqa: S608
                (date_text, username, protocol, *reserved),
            ).fetchall()
            tail = rows[top_n:]
            if not tail:
                continue

            upload = sum(int(row["upload_bytes"]) for row in tail)
            download = sum(int(row["download_bytes"]) for row in tail)
            count = sum(int(row["connection_count"]) for row in tail)
            connection.executemany(
                """
                DELETE FROM daily_domain_usage
                WHERE usage_date = ? AND username = ? AND protocol = ?
                  AND domain = ?
                """,
                [(date_text, username, protocol, str(row["domain"])) for row in tail],
            )
            connection.execute(
                """
                INSERT INTO daily_domain_usage (
                    usage_date, username, protocol, domain,
                    upload_bytes, download_bytes, connection_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(usage_date, username, protocol, domain) DO UPDATE SET
                    upload_bytes =
                        daily_domain_usage.upload_bytes + excluded.upload_bytes,
                    download_bytes =
                        daily_domain_usage.download_bytes + excluded.download_bytes,
                    connection_count =
                        daily_domain_usage.connection_count
                        + excluded.connection_count,
                    updated_at = excluded.updated_at
                """,
                (
                    date_text,
                    username,
                    protocol,
                    OTHER_DOMAIN_KEY,
                    upload,
                    download,
                    count,
                    updated_at,
                ),
            )
            folded += len(tail)
        return folded

    def reconcile_domain_attribution(self, *, collected_at: datetime) -> int:
        """Restate unattributed estimates and boundary offsets atomically.

        Revisit all source days, including late writes after a prolonged outage.
        The daily tables are small and already retain the needed aggregates;
        keeping a second cumulative balance would introduce another checkpoint.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT usage_date, username, protocol,
                          SUM(upload_bytes) AS upload_bytes,
                          SUM(download_bytes) AS download_bytes
                   FROM (
                       SELECT usage_date, username, protocol, upload_bytes, download_bytes
                       FROM daily_usage
                       UNION ALL
                       SELECT usage_date, username, protocol, -upload_bytes, -download_bytes
                       FROM daily_domain_usage WHERE domain <> ?
                   ) GROUP BY usage_date, username, protocol""",
                (UNATTRIBUTED_DOMAIN_KEY,),
            ).fetchall()
            residuals = {
                (
                    date.fromisoformat(row["usage_date"]),
                    row["username"],
                    row["protocol"],
                ): UserTrafficCounters(row["upload_bytes"], row["download_bytes"])
                for row in rows
            }
            boundaries = tuple(
                PollBoundary(
                    date.fromisoformat(row["start_date"]),
                    date.fromisoformat(row["end_date"]),
                    row["username"],
                    row["protocol"],
                    UserTrafficCounters(row["upload_bytes"], row["download_bytes"]),
                )
                for row in connection.execute("SELECT * FROM poll_boundaries")
            )
            gaps, offsets = reconcile_boundaries(residuals, boundaries)
            previous = {
                (
                    date.fromisoformat(row["usage_date"]),
                    row["username"],
                    row["protocol"],
                ): UserTrafficCounters(row["upload_bytes"], row["download_bytes"])
                for row in connection.execute(
                    "SELECT * FROM daily_domain_usage WHERE domain = ?",
                    (UNATTRIBUTED_DOMAIN_KEY,),
                )
            }
            parked = sum(
                max(
                    0,
                    gap.upload_bytes
                    - previous.get(key, UserTrafficCounters()).upload_bytes,
                )
                + max(
                    0,
                    gap.download_bytes
                    - previous.get(key, UserTrafficCounters()).download_bytes,
                )
                for key, gap in gaps.items()
            )
            connection.execute(
                "DELETE FROM daily_domain_usage WHERE domain = ?",
                (UNATTRIBUTED_DOMAIN_KEY,),
            )
            connection.executemany(
                """INSERT INTO daily_domain_usage VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                [
                    (
                        key[0].isoformat(),
                        key[1],
                        key[2],
                        UNATTRIBUTED_DOMAIN_KEY,
                        gap.upload_bytes,
                        gap.download_bytes,
                        collected_at.isoformat(),
                    )
                    for key, gap in gaps.items()
                ],
            )
            connection.execute("DELETE FROM domain_boundary_offsets")
            connection.executemany(
                "INSERT INTO domain_boundary_offsets VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        offset.protocol,
                        offset.username,
                        offset.source_date.isoformat(),
                        offset.target_date.isoformat(),
                        offset.counters.upload_bytes,
                        offset.counters.download_bytes,
                    )
                    for offset in offsets
                ],
            )
        return parked

    def _summary_from_row(
        self,
        *,
        username: str,
        cycle_window: tuple[str, date, date],
        timezone_name: str,
        row: sqlite3.Row | None,
    ) -> UserTrafficSummary:
        cycle_month, cycle_start, cycle_end = cycle_window
        updated_at_text = None if row is None else row["updated_at"]
        updated_at = (
            None
            if updated_at_text in {None, ""}
            else datetime.fromisoformat(str(updated_at_text))
        )
        return UserTrafficSummary(
            username=username,
            cycle_month=cycle_month,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            timezone=timezone_name,
            upload_bytes=0 if row is None else int(row["upload_bytes"]),
            download_bytes=0 if row is None else int(row["download_bytes"]),
            updated_at=updated_at,
        )

    def read_user_summary(
        self,
        *,
        username: str,
        cycle_month: str,
        cycle_start: date,
        cycle_end: date,
        timezone_name: str,
    ) -> UserTrafficSummary:
        if not self._database_path.exists():
            return UserTrafficSummary(
                username=username,
                cycle_month=cycle_month,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
                timezone=timezone_name,
            )

        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(upload_bytes), 0) AS upload_bytes,
                        COALESCE(SUM(download_bytes), 0) AS download_bytes,
                        MAX(updated_at) AS updated_at
                    FROM monthly_usage
                    WHERE cycle_month = ? AND username = ?
                    """,
                    (cycle_month, username),
                ).fetchone()
        except sqlite3.DatabaseError:
            logger.warning(
                "Failed to read traffic stats database %s",
                self._database_path,
                exc_info=True,
            )
            return UserTrafficSummary(
                username=username,
                cycle_month=cycle_month,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
                timezone=timezone_name,
            )

        return self._summary_from_row(
            username=username,
            cycle_window=(cycle_month, cycle_start, cycle_end),
            timezone_name=timezone_name,
            row=row,
        )

    def read_user_summaries(
        self,
        *,
        usernames: Sequence[str],
        cycle_month: str,
        cycle_start: date,
        cycle_end: date,
        timezone_name: str,
    ) -> tuple[UserTrafficSummary, ...]:
        ordered_usernames = tuple(dict.fromkeys(usernames))
        if not ordered_usernames:
            return ()

        if not self._database_path.exists():
            return tuple(
                self._summary_from_row(
                    username=username,
                    cycle_window=(cycle_month, cycle_start, cycle_end),
                    timezone_name=timezone_name,
                    row=None,
                )
                for username in ordered_usernames
            )

        try:
            with self._connect() as connection:
                connection.execute(
                    "CREATE TEMP TABLE requested_usernames (username TEXT PRIMARY KEY)"
                )
                connection.executemany(
                    "INSERT INTO requested_usernames (username) VALUES (?)",
                    [(username,) for username in ordered_usernames],
                )
                rows = connection.execute(
                    """
                    SELECT
                        monthly_usage.username,
                        COALESCE(SUM(monthly_usage.upload_bytes), 0) AS upload_bytes,
                        COALESCE(SUM(monthly_usage.download_bytes), 0) AS download_bytes,
                        MAX(monthly_usage.updated_at) AS updated_at
                    FROM monthly_usage
                    INNER JOIN requested_usernames
                        ON requested_usernames.username = monthly_usage.username
                    WHERE monthly_usage.cycle_month = ?
                    GROUP BY monthly_usage.username
                    """,
                    (cycle_month,),
                ).fetchall()
        except sqlite3.DatabaseError:
            logger.warning(
                "Failed to read traffic stats database %s",
                self._database_path,
                exc_info=True,
            )
            rows = []

        rows_by_username = {str(row["username"]): row for row in rows}
        return tuple(
            self._summary_from_row(
                username=username,
                cycle_window=(cycle_month, cycle_start, cycle_end),
                timezone_name=timezone_name,
                row=rows_by_username.get(username),
            )
            for username in ordered_usernames
        )

    def read_all_user_summaries(
        self,
        *,
        known_usernames: Sequence[str],
        cycle_month: str,
        cycle_start: date,
        cycle_end: date,
        timezone_name: str,
    ) -> tuple[UserTrafficSummary, ...]:
        if not self._database_path.exists():
            return self.read_user_summaries(
                usernames=known_usernames,
                cycle_month=cycle_month,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
                timezone_name=timezone_name,
            )

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        username,
                        COALESCE(SUM(upload_bytes), 0) AS upload_bytes,
                        COALESCE(SUM(download_bytes), 0) AS download_bytes,
                        MAX(updated_at) AS updated_at
                    FROM monthly_usage
                    WHERE cycle_month = ?
                    GROUP BY username
                    """,
                    (cycle_month,),
                ).fetchall()
        except sqlite3.DatabaseError:
            logger.warning(
                "Failed to read traffic stats database %s",
                self._database_path,
                exc_info=True,
            )
            rows = []

        rows_by_username = {str(row["username"]): row for row in rows}
        usernames = sorted({*known_usernames, *rows_by_username})
        return tuple(
            self._summary_from_row(
                username=username,
                cycle_window=(cycle_month, cycle_start, cycle_end),
                timezone_name=timezone_name,
                row=rows_by_username.get(username),
            )
            for username in usernames
        )

    @staticmethod
    def _read_metadata(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute(_METADATA_SELECT, (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _write_metadata(
        connection: sqlite3.Connection, key: str, value: str, updated_at: datetime
    ) -> None:
        connection.execute(
            """
            INSERT INTO stats_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, updated_at.isoformat()),
        )

    def _read_metadata_value(self, key: str) -> str | None:
        """One metadata value on its own connection, tolerating a missing file."""
        rows = self._aggregate_rows(_METADATA_SELECT, (key,))
        return None if not rows else str(rows[0]["value"])

    def read_reported_billing_day(self) -> int | None:
        """The day KiwiVM last reported, or ``None`` if never derived.

        For the collector only: this is the day the *next* recompute will move to,
        which is not necessarily the one the stored rows are bucketed by. Anything
        that reads usage wants ``read_cycle_basis_day``.
        """
        return _normalize_billing_day(self._read_metadata_value(_BILLING_DAY_KEY))

    def read_cycle_basis_day(self) -> int:
        """The billing day the stored ``monthly_usage`` rows were built under.

        Written inside the same transaction as the recompute, so the window a
        caller renders and the rows it sums can never describe different
        boundaries. An absent value means day 1, which is correct for every
        database written before this table existed.
        """
        stored = _normalize_billing_day(
            self._read_metadata_value(_MONTHLY_USAGE_BILLING_DAY_KEY)
        )
        return DEFAULT_BILLING_DAY if stored is None else stored

    def read_billing_day_checked_at(self) -> datetime | None:
        raw = self._read_metadata_value(_BILLING_DAY_CHECKED_AT_KEY)
        if raw is None:
            return None
        try:
            return _utc_now(datetime.fromisoformat(raw))
        except ValueError:
            return None

    def record_billing_day_check(
        self, billing_day: int | None, *, checked_at: datetime
    ) -> None:
        """Note that KiwiVM was asked, recording the day only if the ask worked.

        The timestamp advances either way so a persistent outage backs off
        instead of retrying on every five-minute collection.
        """
        with self._connect() as connection:
            self._write_metadata(
                connection,
                _BILLING_DAY_CHECKED_AT_KEY,
                checked_at.isoformat(),
                checked_at,
            )
            if billing_day is not None:
                self._write_metadata(
                    connection, _BILLING_DAY_KEY, str(billing_day), checked_at
                )

    def reconcile_billing_day(self, billing_day: int, *, now: datetime) -> bool:
        """Rebuild ``monthly_usage`` when the billing day differs from its basis.

        The recompute and the basis write share one transaction, so a crash can
        never leave the recorded basis ahead of the rows it claims to describe.
        Returns whether a rebuild happened.
        """
        with self._connect() as connection:
            stored = _normalize_billing_day(
                self._read_metadata(connection, _MONTHLY_USAGE_BILLING_DAY_KEY)
            )
            if stored is None:
                stored = DEFAULT_BILLING_DAY
            if stored == billing_day:
                return False

            _recompute_monthly_usage(connection, billing_day=billing_day)
            self._write_metadata(
                connection, _MONTHLY_USAGE_BILLING_DAY_KEY, str(billing_day), now
            )
            return True

    def _aggregate_rows(
        self, statement: str, parameters: tuple[object, ...]
    ) -> list[sqlite3.Row]:
        """Run one grouped read, or return nothing if the database cannot serve it.

        A missing file is not an error here -- stats may simply never have been
        collected -- and it must not be created as a side effect, because
        ``_connect`` would happily mkdir its way to an empty database on a read.
        """
        if not self._database_path.exists():
            return []

        try:
            with self._connect() as connection:
                return connection.execute(statement, parameters).fetchall()
        except sqlite3.DatabaseError:
            logger.warning(
                "Failed to read traffic stats database %s",
                self._database_path,
                exc_info=True,
            )
            return []

    def read_daily_series(
        self,
        *,
        username: str | None,
        start_date: date,
        end_date: date,
        timezone_name: str,
    ) -> DailyUsageSeries:
        if username is None:
            statement = """
                SELECT
                    usage_date,
                    COALESCE(SUM(upload_bytes), 0) AS upload_bytes,
                    COALESCE(SUM(download_bytes), 0) AS download_bytes
                FROM daily_usage
                WHERE usage_date BETWEEN ? AND ?
                GROUP BY usage_date
            """
            parameters: tuple[object, ...] = (
                start_date.isoformat(),
                end_date.isoformat(),
            )
        else:
            statement = """
                SELECT
                    usage_date,
                    COALESCE(SUM(upload_bytes), 0) AS upload_bytes,
                    COALESCE(SUM(download_bytes), 0) AS download_bytes
                FROM daily_usage
                WHERE usage_date BETWEEN ? AND ? AND username = ?
                GROUP BY usage_date
            """
            parameters = (start_date.isoformat(), end_date.isoformat(), username)

        rows_by_date = {
            str(row["usage_date"]): row
            for row in self._aggregate_rows(statement, parameters)
        }
        points = tuple(
            DailyUsagePoint(
                usage_date=day,
                upload_bytes=(
                    0
                    if (row := rows_by_date.get(day.isoformat())) is None
                    else int(row["upload_bytes"])
                ),
                download_bytes=(0 if row is None else int(row["download_bytes"])),
            )
            for day in _date_range(start_date, end_date)
        )
        return DailyUsageSeries(
            username=username,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone_name,
            points=points,
        )

    def read_protocol_breakdown(
        self,
        *,
        username: str | None,
        cycle_month: str,
        timezone_name: str,
    ) -> ProtocolUsageBreakdown:
        """Split one cycle's usage by protocol.

        Reads monthly_usage rather than daily_usage on purpose: the protocol
        column has been there all along, only ever summed away, so this works
        retroactively instead of waiting for daily buckets to accumulate.
        """
        if username is None:
            statement = """
                SELECT
                    protocol,
                    COALESCE(SUM(upload_bytes), 0) AS upload_bytes,
                    COALESCE(SUM(download_bytes), 0) AS download_bytes
                FROM monthly_usage
                WHERE cycle_month = ?
                GROUP BY protocol
            """
            parameters: tuple[object, ...] = (cycle_month,)
        else:
            statement = """
                SELECT
                    protocol,
                    COALESCE(SUM(upload_bytes), 0) AS upload_bytes,
                    COALESCE(SUM(download_bytes), 0) AS download_bytes
                FROM monthly_usage
                WHERE cycle_month = ? AND username = ?
                GROUP BY protocol
            """
            parameters = (cycle_month, username)

        rows_by_protocol = {
            str(row["protocol"]): row
            for row in self._aggregate_rows(statement, parameters)
        }
        # Known protocols first and always present, then anything the database
        # knows about that this build does not -- the same tolerance
        # read_all_user_summaries shows towards unrecognised usernames.
        ordered = [
            *TRAFFIC_PROTOCOLS,
            *sorted(set(rows_by_protocol) - set(TRAFFIC_PROTOCOLS)),
        ]
        return ProtocolUsageBreakdown(
            username=username,
            cycle_month=cycle_month,
            timezone=timezone_name,
            protocols=tuple(
                ProtocolUsage(
                    protocol=protocol,
                    upload_bytes=(
                        0
                        if (row := rows_by_protocol.get(protocol)) is None
                        else int(row["upload_bytes"])
                    ),
                    download_bytes=(0 if row is None else int(row["download_bytes"])),
                )
                for protocol in ordered
            ),
        )

    def _ranked_usage_rows(
        self,
        *,
        table: str,
        key_column: str,
        username: str | None,
        window: UsageWindow,
        limit: int,
    ) -> list[sqlite3.Row]:
        """Rank one daily breakdown table by traffic over a window of days.

        The two breakdown tables differ only in their name and their key column,
        so they share the statement rather than keeping two copies of it that
        can drift apart. ``window_total_bytes`` is a window function so it spans
        every group, not just the ``LIMIT`` rows: SQLite evaluates it before the
        limit, which is exactly the total a share has to be measured against.
        """
        user_filter = "" if username is None else "AND username = ?"
        statement = f"""
            SELECT
                {key_column},
                COALESCE(SUM(upload_bytes), 0) AS upload_bytes,
                COALESCE(SUM(download_bytes), 0) AS download_bytes,
                COALESCE(SUM(connection_count), 0) AS connection_count,
                SUM(SUM(upload_bytes) + SUM(download_bytes)) OVER ()
                    AS window_total_bytes
            FROM {table}
            WHERE usage_date BETWEEN ? AND ? {user_filter}
            GROUP BY {key_column}
            ORDER BY (SUM(upload_bytes) + SUM(download_bytes)) DESC,
                     {key_column} ASC
            LIMIT ?
        """  # noqa: S608
        window_bounds = (window.start_date.isoformat(), window.end_date.isoformat())
        parameters: tuple[object, ...] = (
            (*window_bounds, limit)
            if username is None
            else (*window_bounds, username, limit)
        )
        return self._aggregate_rows(statement, parameters)

    def read_domain_breakdown(
        self,
        *,
        username: str | None,
        window: UsageWindow,
        limit: int,
    ) -> DomainUsageBreakdown:
        """Rank destinations over a window of days, largest first."""
        rows = self._ranked_usage_rows(
            table="daily_domain_usage",
            key_column="domain",
            username=username,
            window=window,
            limit=limit,
        )
        return DomainUsageBreakdown(
            username=username,
            timezone=window.timezone_name,
            days=window.days,
            domains=tuple(
                DomainUsage(
                    domain=str(row["domain"]),
                    upload_bytes=int(row["upload_bytes"]),
                    download_bytes=int(row["download_bytes"]),
                    connection_count=int(row["connection_count"]),
                )
                for row in rows
            ),
            window_total_bytes=int(rows[0]["window_total_bytes"] or 0) if rows else 0,
        )

    def read_destination_breakdown(
        self,
        *,
        username: str | None,
        window: UsageWindow,
        limit: int,
    ) -> DestinationUsageBreakdown:
        return DestinationUsageBreakdown(
            username=username,
            timezone=window.timezone_name,
            days=window.days,
            destinations=tuple(
                DestinationUsage(
                    destination_ip=str(row["destination_ip"]),
                    upload_bytes=int(row["upload_bytes"]),
                    download_bytes=int(row["download_bytes"]),
                    connection_count=int(row["connection_count"]),
                )
                for row in self._ranked_usage_rows(
                    table="daily_destination_usage",
                    key_column="destination_ip",
                    username=username,
                    window=window,
                    limit=limit,
                )
            ),
        )

    def read_live_connections(
        self,
        *,
        username: str | None = None,
        limit: int = 500,
        now: datetime | None = None,
    ) -> tuple[LiveConnection, ...]:
        user_filter = "" if username is None else "AND username = ?"
        statement = f"""
            SELECT * FROM live_connections WHERE updated_at >= ? {user_filter}
            ORDER BY (upload_bytes + download_bytes) DESC, connection_id ASC
            LIMIT ?
        """  # noqa: S608
        cutoff = (
            _utc_now(now) - timedelta(seconds=LIVE_SNAPSHOT_MAX_AGE_SECONDS)
        ).isoformat()
        parameters: tuple[object, ...] = (
            (cutoff, limit) if username is None else (cutoff, username, limit)
        )

        return tuple(
            LiveConnection(
                connection_id=str(row["connection_id"]),
                service_name=str(row["service_name"]),
                username=str(row["username"]),
                network=str(row["network"]),
                protocol=str(row["protocol"]),
                source=str(row["source"]),
                destination=str(row["destination"]),
                domain=str(row["domain"]),
                outbound=str(row["outbound"]),
                created_at=_parse_optional_datetime(row["created_at"]),
                upload_bytes=int(row["upload_bytes"]),
                download_bytes=int(row["download_bytes"]),
                updated_at=_parse_optional_datetime(row["updated_at"]),
            )
            for row in self._aggregate_rows(statement, parameters)
        )

    def read_monthly_history(
        self,
        *,
        username: str | None,
        end_cycle_month: str,
        months: int,
        timezone_name: str,
    ) -> MonthlyUsageHistory:
        cycle_months = _month_range(end_cycle_month, months)
        if not cycle_months:
            return MonthlyUsageHistory(username=username, timezone=timezone_name)

        if username is None:
            statement = """
                SELECT
                    cycle_month,
                    COALESCE(SUM(upload_bytes), 0) AS upload_bytes,
                    COALESCE(SUM(download_bytes), 0) AS download_bytes
                FROM monthly_usage
                WHERE cycle_month BETWEEN ? AND ?
                GROUP BY cycle_month
            """
            parameters: tuple[object, ...] = (cycle_months[0], cycle_months[-1])
        else:
            statement = """
                SELECT
                    cycle_month,
                    COALESCE(SUM(upload_bytes), 0) AS upload_bytes,
                    COALESCE(SUM(download_bytes), 0) AS download_bytes
                FROM monthly_usage
                WHERE cycle_month BETWEEN ? AND ? AND username = ?
                GROUP BY cycle_month
            """
            parameters = (cycle_months[0], cycle_months[-1], username)

        rows_by_month = {
            str(row["cycle_month"]): row
            for row in self._aggregate_rows(statement, parameters)
        }
        return MonthlyUsageHistory(
            username=username,
            timezone=timezone_name,
            months=tuple(
                MonthlyUsagePoint(
                    cycle_month=cycle_month,
                    upload_bytes=(
                        0
                        if (row := rows_by_month.get(cycle_month)) is None
                        else int(row["upload_bytes"])
                    ),
                    download_bytes=(0 if row is None else int(row["download_bytes"])),
                )
                for cycle_month in cycle_months
            ),
        )


def _recompute_monthly_usage(
    connection: sqlite3.Connection, *, billing_day: int
) -> tuple[str, ...]:
    """Rebuild ``monthly_usage`` from ``daily_usage`` under ``billing_day``.

    Only cycles starting on or after the first day ``daily_usage`` covers are
    rewritten. The daily table arrived four months after the monthly one and was
    never backfilled, so older cycles cannot be reconstructed from it -- a blanket
    rebuild would silently blank months that are still on screen in the admin
    history chart. Those rows are left exactly as they were collected.

    Returns the cycle keys that were rebuilt.
    """
    floor_row = connection.execute(
        "SELECT MIN(usage_date) AS first_daily FROM daily_usage"
    ).fetchone()
    if floor_row is None or floor_row["first_daily"] is None:
        return ()
    first_daily = date.fromisoformat(str(floor_row["first_daily"]))

    # No GROUP BY: (usage_date, username, protocol) is daily_usage's primary key,
    # so the folding that matters is by cycle, and only Python knows the cycle.
    rows = connection.execute(
        """
        SELECT usage_date, username, protocol, upload_bytes, download_bytes, updated_at
        FROM daily_usage
        """
    ).fetchall()

    totals: dict[tuple[str, str, str], tuple[int, int, str]] = {}
    for row in rows:
        usage_date = date.fromisoformat(str(row["usage_date"]))
        cycle_month, cycle_start, _ = _cycle_bounds(usage_date, billing_day)
        if cycle_start < first_daily:
            # Partially covered by daily data: the stored row is more complete
            # than anything we could rebuild, so leave it alone.
            continue
        key = (cycle_month, str(row["username"]), str(row["protocol"]))
        upload, download, updated_at = totals.get(key, (0, 0, ""))
        totals[key] = (
            upload + int(row["upload_bytes"]),
            download + int(row["download_bytes"]),
            max(updated_at, str(row["updated_at"])),
        )

    if not totals:
        return ()

    cycle_months = sorted({key[0] for key in totals})
    # Zero-padded YYYY-MM compares correctly as text, which this module already
    # relies on for history reads. Clearing the whole span also drops a stale row
    # for a month inside it that has no daily coverage at all.
    connection.execute(
        "DELETE FROM monthly_usage WHERE cycle_month BETWEEN ? AND ?",
        (cycle_months[0], cycle_months[-1]),
    )
    connection.executemany(
        """
        INSERT INTO monthly_usage (
            cycle_month, username, protocol, upload_bytes, download_bytes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        # The key and value tuples concatenate in exactly the column order above.
        [key + value for key, value in sorted(totals.items())],
    )
    return tuple(cycle_months)
