"""Tests for traffic stats collection and reads."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import sing_box_manager.traffic_stats as traffic_stats
from sing_box_manager.settings import Settings, TrafficStatsSettings

VALID_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "c29tZXNhbHQxMjM0NTY$"
    "c29tZWhhc2h2YWx1ZXNvbWVoaXN0b3J5"
)


def _write_inventory(path: Path) -> None:
    path.write_text(
        "deployment:\n"
        "  host: vpn.example.com\n"
        "  ip: 203.0.113.10\n"
        "  trojan_port: 8443\n"
        "  hysteria2_port: 4443\n"
        "  naive_port: 9443\n"
        "  tls:\n"
        "    enabled: true\n"
        "    key_path: /etc/letsencrypt/live/vpn.example.com/privkey.pem\n"
        "    certificate_path: /etc/letsencrypt/live/vpn.example.com/fullchain.pem\n"
        "web_portal:\n"
        "  users:\n"
        "    - username: alice\n"
        f"      password_hash: {VALID_PASSWORD_HASH}\n"
        "      enabled: true\n"
        "trojan:\n"
        "  users:\n"
        "    - username: alice\n"
        "      password: trojan-secret\n"
        "      enabled: true\n"
        "hysteria2:\n"
        "  obfs_password: hy2-obfs-secret\n"
        "  users:\n"
        "    - username: alice\n"
        "      password: hysteria2-secret\n"
        "      enabled: true\n"
        "naive:\n"
        "  users:\n"
        "    - username: alice\n"
        "      password: naive-secret\n"
        "      enabled: true\n",
        encoding="utf-8",
    )


def _build_settings(
    tmp_path: Path, inventory_path: Path, database_path: Path
) -> Settings:
    return Settings(
        sing_box_version="1.12.24",
        config_root=tmp_path / "config",
        config_path=inventory_path,
        traffic_stats=TrafficStatsSettings(
            enabled=True,
            database_path=database_path,
        ),
    )


def test_collect_traffic_stats_accumulates_monthly_totals(
    tmp_path: Path, monkeypatch
) -> None:
    inventory_path = tmp_path / "runtime.yaml"
    database_path = tmp_path / "traffic.sqlite3"
    _write_inventory(inventory_path)
    settings = _build_settings(tmp_path, inventory_path, database_path)

    snapshots = {
        "trojan": [
            traffic_stats._ServiceSnapshot(
                uptime_seconds=100,
                counters={"alice": traffic_stats.UserTrafficCounters(100, 200)},
            ),
            traffic_stats._ServiceSnapshot(
                uptime_seconds=10,
                counters={"alice": traffic_stats.UserTrafficCounters(50, 75)},
            ),
        ],
        "hysteria2": [
            traffic_stats._ServiceSnapshot(
                uptime_seconds=100,
                counters={"alice": traffic_stats.UserTrafficCounters(300, 500)},
            ),
            traffic_stats._ServiceSnapshot(
                uptime_seconds=110,
                counters={"alice": traffic_stats.UserTrafficCounters(320, 540)},
            ),
        ],
        "naive": [
            traffic_stats._ServiceSnapshot(
                uptime_seconds=100,
                counters={"alice": traffic_stats.UserTrafficCounters(20, 30)},
            ),
            traffic_stats._ServiceSnapshot(
                uptime_seconds=110,
                counters={"alice": traffic_stats.UserTrafficCounters(25, 40)},
            ),
        ],
    }

    def fake_query_service_snapshot(
        target: traffic_stats._ServiceTarget,
    ) -> traffic_stats._ServiceSnapshot:
        return snapshots[target.protocol].pop(0)

    monkeypatch.setattr(
        traffic_stats,
        "_query_service_snapshot",
        fake_query_service_snapshot,
    )

    first_result = traffic_stats.collect_traffic_stats(
        settings,
        now=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
    )
    second_result = traffic_stats.collect_traffic_stats(
        settings,
        now=datetime(2026, 4, 11, 0, 0, tzinfo=UTC),
    )

    provider = traffic_stats.TrafficStatsProvider(database_path, "Asia/Shanghai")
    summary = provider.get_user_summary(
        "alice",
        now=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
    )

    assert first_result.successful_services == ("trojan", "hysteria2", "naive")
    assert second_result.failed_services == ()
    assert summary.upload_bytes == 495
    assert summary.download_bytes == 855
    assert summary.total_bytes == 1350
    assert summary.cycle_month == "2026-04"
    assert summary.updated_at == datetime(2026, 4, 11, 0, 0, tzinfo=UTC)

    # The two collections fall on distinct Asia/Shanghai days, and the daily
    # bucket must add up to exactly what the monthly bucket recorded.
    daily = _read_daily_totals(database_path)
    assert sorted(daily) == ["2026-04-01", "2026-04-11"]
    assert sum(upload for upload, _ in daily.values()) == summary.upload_bytes
    assert sum(download for _, download in daily.values()) == summary.download_bytes


def _read_daily_totals(database_path: Path) -> dict[str, tuple[int, int]]:
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            """
            SELECT usage_date, SUM(upload_bytes), SUM(download_bytes)
            FROM daily_usage
            GROUP BY usage_date
            """
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}


def _read_monthly_totals(database_path: Path) -> dict[str, tuple[int, int]]:
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            """
            SELECT cycle_month, SUM(upload_bytes), SUM(download_bytes)
            FROM monthly_usage
            GROUP BY cycle_month
            """
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}


def test_record_snapshot_writes_daily_and_monthly_from_one_delta(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "traffic.sqlite3"
    store = traffic_stats._TrafficStatsStore(database_path)
    for day, counters in (
        (date(2026, 4, 1), traffic_stats.UserTrafficCounters(100, 200)),
        (date(2026, 4, 2), traffic_stats.UserTrafficCounters(150, 260)),
    ):
        store.record_snapshot(
            service_name="trojan",
            usage_date=day,
            collected_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
            snapshot=traffic_stats._ServiceSnapshot(
                uptime_seconds=100 + day.day,
                counters={"alice": counters},
            ),
            billing_day=1,
        )

    daily = _read_daily_totals(database_path)

    assert daily == {"2026-04-01": (100, 200), "2026-04-02": (50, 60)}
    assert _read_monthly_totals(database_path) == {"2026-04": (150, 260)}


def test_daily_usage_attributes_the_whole_delta_after_a_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "traffic.sqlite3"
    store = traffic_stats._TrafficStatsStore(database_path)
    store.record_snapshot(
        service_name="trojan",
        usage_date=date(2026, 4, 1),
        collected_at=datetime(2026, 4, 1, tzinfo=UTC),
        snapshot=traffic_stats._ServiceSnapshot(
            uptime_seconds=9000,
            counters={"alice": traffic_stats.UserTrafficCounters(100, 200)},
        ),
        billing_day=1,
    )
    # Uptime went backwards: sing-box restarted and its counters reset, so the
    # whole current value is the delta and it lands on the poll's local day.
    store.record_snapshot(
        service_name="trojan",
        usage_date=date(2026, 4, 2),
        collected_at=datetime(2026, 4, 2, tzinfo=UTC),
        snapshot=traffic_stats._ServiceSnapshot(
            uptime_seconds=30,
            counters={"alice": traffic_stats.UserTrafficCounters(7, 9)},
        ),
        billing_day=1,
    )

    assert _read_daily_totals(database_path) == {
        "2026-04-01": (100, 200),
        "2026-04-02": (7, 9),
    }
    assert _read_monthly_totals(database_path) == {"2026-04": (107, 209)}


def test_daily_and_monthly_keys_agree_across_a_month_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    """The bucket key is the *local* day, and both tables must roll together."""
    inventory_path = tmp_path / "runtime.yaml"
    database_path = tmp_path / "traffic.sqlite3"
    _write_inventory(inventory_path)
    settings = _build_settings(tmp_path, inventory_path, database_path)

    counters = iter(
        [
            traffic_stats.UserTrafficCounters(100, 200),
            traffic_stats.UserTrafficCounters(130, 260),
        ]
    )
    snapshot_by_call: dict[str, list[traffic_stats._ServiceSnapshot]] = {}

    def fake_query_service_snapshot(
        target: traffic_stats._ServiceTarget,
    ) -> traffic_stats._ServiceSnapshot:
        pending = snapshot_by_call[target.protocol]
        return pending.pop(0)

    monkeypatch.setattr(
        traffic_stats,
        "_query_service_snapshot",
        fake_query_service_snapshot,
    )

    # 2026-04-30 15:30 UTC is 2026-04-30 23:30 in Asia/Shanghai;
    # 2026-04-30 17:00 UTC is 2026-05-01 01:00 the next month.
    for index, moment in enumerate(
        (
            datetime(2026, 4, 30, 15, 30, tzinfo=UTC),
            datetime(2026, 4, 30, 17, 0, tzinfo=UTC),
        )
    ):
        current = next(counters)
        snapshot_by_call.clear()
        for protocol in ("trojan", "hysteria2", "naive"):
            snapshot_by_call[protocol] = [
                traffic_stats._ServiceSnapshot(
                    uptime_seconds=1000 + index,
                    counters={"alice": current},
                )
            ]
        traffic_stats.collect_traffic_stats(settings, now=moment)

    # Three protocols each contribute the same numbers.
    assert _read_daily_totals(database_path) == {
        "2026-04-30": (300, 600),
        "2026-05-01": (90, 180),
    }
    assert _read_monthly_totals(database_path) == {
        "2026-04": (300, 600),
        "2026-05": (90, 180),
    }


def test_collect_traffic_stats_returns_partial_result(
    tmp_path: Path, monkeypatch
) -> None:
    inventory_path = tmp_path / "runtime.yaml"
    database_path = tmp_path / "traffic.sqlite3"
    _write_inventory(inventory_path)
    settings = _build_settings(tmp_path, inventory_path, database_path)

    def fake_query_service_snapshot(
        target: traffic_stats._ServiceTarget,
    ) -> traffic_stats._ServiceSnapshot:
        if target.protocol == "hysteria2":
            raise RuntimeError("connection refused")
        return traffic_stats._ServiceSnapshot(
            uptime_seconds=100,
            counters={"alice": traffic_stats.UserTrafficCounters(42, 84)},
        )

    monkeypatch.setattr(
        traffic_stats,
        "_query_service_snapshot",
        fake_query_service_snapshot,
    )

    result = traffic_stats.collect_traffic_stats(
        settings,
        now=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
    )

    summary = traffic_stats.TrafficStatsProvider(
        database_path, "Asia/Shanghai"
    ).get_user_summary(
        "alice",
        now=datetime(2026, 4, 2, 0, 0, tzinfo=UTC),
    )

    assert result.successful_services == ("trojan", "naive")
    assert len(result.failed_services) == 1
    assert "hysteria2: connection refused" in result.failed_services[0]
    assert summary.upload_bytes == 84
    assert summary.download_bytes == 168


def test_provider_reads_all_requested_user_summaries(tmp_path: Path) -> None:
    database_path = tmp_path / "traffic.sqlite3"
    store = traffic_stats._TrafficStatsStore(database_path)
    store.record_snapshot(
        service_name="trojan",
        usage_date=date(2026, 4, 1),
        collected_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        snapshot=traffic_stats._ServiceSnapshot(
            uptime_seconds=100,
            counters={
                "alice": traffic_stats.UserTrafficCounters(100, 200),
                "bob": traffic_stats.UserTrafficCounters(300, 400),
            },
        ),
        billing_day=1,
    )
    store.record_snapshot(
        service_name="naive",
        usage_date=date(2026, 4, 2),
        collected_at=datetime(2026, 4, 2, 0, 0, tzinfo=UTC),
        snapshot=traffic_stats._ServiceSnapshot(
            uptime_seconds=100,
            counters={"alice": traffic_stats.UserTrafficCounters(500, 600)},
        ),
        billing_day=1,
    )

    summaries = traffic_stats.TrafficStatsProvider(
        database_path, "Asia/Shanghai"
    ).get_user_summaries(
        ["bob", "alice", "carol"],
        now=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
    )

    assert [summary.username for summary in summaries] == ["bob", "alice", "carol"]
    assert summaries[0].upload_bytes == 300
    assert summaries[0].download_bytes == 400
    assert summaries[0].updated_at == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    assert summaries[1].upload_bytes == 600
    assert summaries[1].download_bytes == 800
    assert summaries[1].updated_at == datetime(2026, 4, 2, 0, 0, tzinfo=UTC)
    assert summaries[2].upload_bytes == 0
    assert summaries[2].download_bytes == 0
    assert summaries[2].updated_at is None


def test_provider_reads_all_collected_and_known_user_summaries(tmp_path: Path) -> None:
    database_path = tmp_path / "traffic.sqlite3"
    traffic_stats._TrafficStatsStore(database_path).record_snapshot(
        service_name="trojan",
        usage_date=date(2026, 4, 1),
        collected_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        snapshot=traffic_stats._ServiceSnapshot(
            uptime_seconds=100,
            counters={"protocol-only": traffic_stats.UserTrafficCounters(100, 200)},
        ),
        billing_day=1,
    )

    summaries = traffic_stats.TrafficStatsProvider(
        database_path, "Asia/Shanghai"
    ).get_all_user_summaries(
        ["alice"],
        now=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
    )

    assert [summary.username for summary in summaries] == ["alice", "protocol-only"]
    assert summaries[0].total_bytes == 0
    assert summaries[1].upload_bytes == 100
    assert summaries[1].download_bytes == 200


def _seed(store: traffic_stats._TrafficStatsStore) -> None:
    """Two users across two protocols and three days, spanning a month boundary."""
    running: dict[tuple[str, str], tuple[int, int]] = {}
    plan = [
        (date(2026, 4, 29), "trojan", "alice", (10, 20)),
        (date(2026, 4, 30), "trojan", "alice", (15, 35)),
        (date(2026, 4, 30), "naive", "alice", (7, 11)),
        (date(2026, 4, 30), "trojan", "bob", (100, 200)),
        (date(2026, 5, 1), "trojan", "alice", (40, 60)),
        (date(2026, 5, 1), "naive", "bob", (5, 5)),
    ]
    for day, protocol, username, counters in plan:
        running[protocol, username] = counters
        store.record_snapshot(
            service_name=protocol,
            usage_date=day,
            collected_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
            snapshot=traffic_stats._ServiceSnapshot(
                # Monotonic across the month boundary, or the reader would see a
                # restart and attribute whole counters instead of deltas.
                uptime_seconds=(day - date(2026, 1, 1)).days * 86_400,
                counters={
                    name: traffic_stats.UserTrafficCounters(*value)
                    for (service, name), value in running.items()
                    if service == protocol
                },
            ),
            billing_day=1,
        )


def test_daily_series_is_dense_over_the_requested_window(tmp_path: Path) -> None:
    database_path = tmp_path / "traffic.sqlite3"
    _seed(traffic_stats._TrafficStatsStore(database_path))

    series = traffic_stats.TrafficStatsProvider(
        database_path, "Asia/Shanghai"
    ).get_daily_series(
        "alice",
        days=5,
        now=datetime(2026, 5, 1, 4, 0, tzinfo=UTC),
    )

    assert [point.usage_date.isoformat() for point in series.points] == [
        "2026-04-27",
        "2026-04-28",
        "2026-04-29",
        "2026-04-30",
        "2026-05-01",
    ]
    # Days with no collection are present as honest zeroes, not gaps.
    assert [point.total_bytes for point in series.points[:2]] == [0, 0]
    assert series.points[2].upload_bytes == 10
    assert series.username == "alice"


def test_daily_series_for_all_users_sums_users_and_protocols(tmp_path: Path) -> None:
    database_path = tmp_path / "traffic.sqlite3"
    _seed(traffic_stats._TrafficStatsStore(database_path))

    everyone = traffic_stats.TrafficStatsProvider(
        database_path, "Asia/Shanghai"
    ).get_daily_series(days=3, now=datetime(2026, 5, 1, 4, 0, tzinfo=UTC))

    by_date = {point.usage_date.isoformat(): point for point in everyone.points}

    assert everyone.username is None
    # 2026-04-30: alice trojan delta 5/15, alice naive 7/11, bob trojan 100/200.
    assert by_date["2026-04-30"].upload_bytes == 112
    assert by_date["2026-04-30"].download_bytes == 226


def test_protocol_breakdown_keeps_protocols_separate(tmp_path: Path) -> None:
    database_path = tmp_path / "traffic.sqlite3"
    _seed(traffic_stats._TrafficStatsStore(database_path))

    breakdown = traffic_stats.TrafficStatsProvider(
        database_path, "Asia/Shanghai"
    ).get_protocol_breakdown("alice", now=datetime(2026, 4, 30, 4, 0, tzinfo=UTC))

    by_protocol = {usage.protocol: usage for usage in breakdown.protocols}

    assert [usage.protocol for usage in breakdown.protocols] == [
        "trojan",
        "hysteria2",
        "naive",
    ]
    assert by_protocol["trojan"].upload_bytes == 15
    assert by_protocol["naive"].upload_bytes == 7
    # A protocol that carried nothing keeps its slot rather than vanishing.
    assert by_protocol["hysteria2"].total_bytes == 0
    assert breakdown.cycle_month == "2026-04"


def test_monthly_history_is_dense_and_ordered(tmp_path: Path) -> None:
    database_path = tmp_path / "traffic.sqlite3"
    _seed(traffic_stats._TrafficStatsStore(database_path))

    history = traffic_stats.TrafficStatsProvider(
        database_path, "Asia/Shanghai"
    ).get_monthly_history(
        "alice",
        months=4,
        now=datetime(2026, 5, 1, 4, 0, tzinfo=UTC),
    )

    assert [point.cycle_month for point in history.months] == [
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
    ]
    assert history.months[0].total_bytes == 0
    assert history.months[2].upload_bytes == 22
    assert history.months[3].upload_bytes == 25


def test_month_range_walks_back_across_a_year_boundary() -> None:
    assert traffic_stats._month_range("2026-02", 4) == (
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    )


def test_new_readers_return_empty_when_the_database_is_missing(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.sqlite3"
    provider = traffic_stats.TrafficStatsProvider(database_path, "Asia/Shanghai")
    now = datetime(2026, 5, 1, 4, 0, tzinfo=UTC)

    series = provider.get_daily_series("alice", days=3, now=now)
    breakdown = provider.get_protocol_breakdown("alice", now=now)
    history = provider.get_monthly_history("alice", months=2, now=now)

    assert [point.total_bytes for point in series.points] == [0, 0, 0]
    assert breakdown.total_bytes == 0
    assert history.total_bytes == 0
    # Reading must not conjure the database into existence.
    assert not database_path.exists()


def test_create_traffic_stats_provider_returns_none_when_disabled(
    tmp_path: Path,
) -> None:
    settings = Settings(sing_box_version="1.12.24", config_root=tmp_path)

    assert traffic_stats.create_traffic_stats_provider(settings) is None


def test_upstream_build_tags_reads_makefile_only(tmp_path: Path) -> None:
    source_dir = tmp_path / "sing-box-src"
    source_dir.mkdir()
    (source_dir / ".goreleaser.yaml").write_text(
        "builds:\n  - &template {id: main}\narchives:\n  - &template {id: archive}\n",
        encoding="utf-8",
    )
    (source_dir / "Makefile").write_text(
        "TAGS ?= with_gvisor,with_quic,with_clash_api\n",
        encoding="utf-8",
    )

    assert traffic_stats._upstream_build_tags(source_dir) == [
        "with_gvisor",
        "with_quic",
        "with_clash_api",
    ]


def test_upstream_build_tags_reads_default_build_tags_file(tmp_path: Path) -> None:
    source_dir = tmp_path / "sing-box-src"
    release_dir = source_dir / "release"
    release_dir.mkdir(parents=True)
    (source_dir / "Makefile").write_text(
        "TAGS ?= $(shell cat release/DEFAULT_BUILD_TAGS_OTHERS)\n",
        encoding="utf-8",
    )
    (release_dir / "DEFAULT_BUILD_TAGS_OTHERS").write_text(
        "with_gvisor,with_quic,with_dhcp\n",
        encoding="utf-8",
    )

    assert traffic_stats._upstream_build_tags(source_dir) == [
        "with_gvisor",
        "with_quic",
        "with_dhcp",
    ]


def _isolated_build_settings(tmp_path: Path) -> Settings:
    """Settings whose caches live under tmp_path, never the real project cache."""
    return Settings(
        sing_box_version="1.13.11",
        config_root=tmp_path,
        upstream_cache_root=tmp_path / "cache" / "upstream",
    )


def _record_build_commands(monkeypatch) -> list[list[str]]:
    """Stub the git clone and go build, returning the recorded command list."""
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], *, cwd: Path, env=None) -> None:
        commands.append(command)
        if command[0] == "git":
            source_dir = Path(command[-1])
            release_dir = source_dir / "release"
            release_dir.mkdir(parents=True)
            (source_dir / "Makefile").write_text(
                "TAGS ?= $(shell cat release/DEFAULT_BUILD_TAGS_OTHERS)\n",
                encoding="utf-8",
            )
            (release_dir / "DEFAULT_BUILD_TAGS_OTHERS").write_text(
                "with_gvisor,with_quic\n",
                encoding="utf-8",
            )
            (release_dir / "LDFLAGS").write_text(
                "-X internal/godebug.defaultGODEBUG=multipathtcp=0 -checklinkname=0\n",
                encoding="utf-8",
            )
            return

        Path(command[command.index("-o") + 1]).write_text("binary", encoding="utf-8")

    monkeypatch.setattr(traffic_stats, "_run_command", fake_run_command)
    monkeypatch.setattr(traffic_stats, "_go_toolchain_version", lambda: "go1.25.0 test")
    return commands


def test_build_custom_server_binary_uses_upstream_build_files(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _isolated_build_settings(tmp_path)
    destination = tmp_path / "sing-box"
    commands = _record_build_commands(monkeypatch)

    assert (
        traffic_stats.build_custom_server_binary(settings, destination) == destination
    )

    build_command = commands[-1]
    assert build_command[build_command.index("-tags") + 1] == (
        "with_gvisor,with_quic,with_v2ray_api"
    )
    ldflags = build_command[build_command.index("-ldflags") + 1]
    assert "github.com/sagernet/sing-box/constant.Version=1.13.11" in ldflags
    assert "internal/godebug.defaultGODEBUG=multipathtcp=0" in ldflags
    assert "-checklinkname=0" in ldflags


def test_build_custom_server_binary_caches_outside_the_project_tree(
    tmp_path: Path, monkeypatch
) -> None:
    """The cache must follow upstream_cache_root, never the real project cache."""
    settings = _isolated_build_settings(tmp_path)
    _record_build_commands(monkeypatch)

    traffic_stats.build_custom_server_binary(settings, tmp_path / "sing-box")

    cache_root = traffic_stats.resolve_server_binary_cache_root(settings)
    assert cache_root == tmp_path / "cache" / "server-binary"
    assert list(cache_root.rglob(traffic_stats.SERVER_BINARY_NAME))


def test_build_custom_server_binary_reuses_cached_build(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _isolated_build_settings(tmp_path)
    commands = _record_build_commands(monkeypatch)

    traffic_stats.build_custom_server_binary(settings, tmp_path / "first")
    commands.clear()

    second = tmp_path / "second"
    assert traffic_stats.build_custom_server_binary(settings, second) == second
    assert second.read_text(encoding="utf-8") == "binary"
    # No clone, no build: the whole point of the cache.
    assert commands == []


def test_build_custom_server_binary_rebuilds_when_cache_entry_is_corrupt(
    tmp_path: Path, monkeypatch
) -> None:
    """A cached binary whose bytes drifted from its digest must never be served."""
    settings = _isolated_build_settings(tmp_path)
    commands = _record_build_commands(monkeypatch)

    traffic_stats.build_custom_server_binary(settings, tmp_path / "first")
    cache_root = traffic_stats.resolve_server_binary_cache_root(settings)
    cached_binary = next(iter(cache_root.rglob(traffic_stats.SERVER_BINARY_NAME)))
    cached_binary.write_text("tampered", encoding="utf-8")
    commands.clear()

    rebuilt = tmp_path / "second"
    traffic_stats.build_custom_server_binary(settings, rebuilt)

    assert rebuilt.read_text(encoding="utf-8") == "binary"
    assert [command[0] for command in commands] == ["git", "go"]


def test_build_custom_server_binary_key_tracks_the_go_toolchain(
    tmp_path: Path, monkeypatch
) -> None:
    """A Go upgrade must not silently reuse a binary built by the old toolchain."""
    settings = _isolated_build_settings(tmp_path)
    commands = _record_build_commands(monkeypatch)

    traffic_stats.build_custom_server_binary(settings, tmp_path / "first")
    commands.clear()

    monkeypatch.setattr(traffic_stats, "_go_toolchain_version", lambda: "go1.26.0 test")
    traffic_stats.build_custom_server_binary(settings, tmp_path / "second")

    assert [command[0] for command in commands] == ["git", "go"]


def test_stats_rpc_methods_use_upstream_legacy_service_name() -> None:
    assert traffic_stats._QUERY_STATS_METHOD == (
        "/v2ray.core.app.stats.command.StatsService/QueryStats"
    )
    assert traffic_stats._GET_SYS_STATS_METHOD == (
        "/v2ray.core.app.stats.command.StatsService/GetSysStats"
    )
