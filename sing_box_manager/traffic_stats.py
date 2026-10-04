"""Traffic statistics collection, persistence, and portal reads."""

from __future__ import annotations

import importlib
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sing_box_manager import kiwivm
from sing_box_manager.release.digest_cache import (
    digest_sidecar_path,
    load_cached_digest,
    sha256_for_path,
    sha256_for_text,
    write_cached_digest,
)
from sing_box_manager.release.profiles import counter_username
from sing_box_manager.runtime_env import load_local_env
from sing_box_manager.settings import (
    PROJECT_ROOT,
    RuntimeInventory,
    load_runtime_inventory,
)
from sing_box_manager.traffic_models import (
    DEFAULT_BILLING_DAY,
    NO_DOMAIN_KEY,
    OTHER_DOMAIN_KEY,
    RESERVED_DOMAIN_KEYS,
    STREAM_ATTRIBUTED_RETENTION,
    TRAFFIC_PROTOCOLS,
    UNATTRIBUTED_DOMAIN_KEY,
    DailyUsagePoint,
    DailyUsageSeries,
    DestinationUsage,
    DestinationUsageBreakdown,
    DomainDelta,
    DomainUsage,
    DomainUsageBreakdown,
    LiveConnection,
    MonthlyUsageHistory,
    MonthlyUsagePoint,
    ProtocolUsage,
    ProtocolUsageBreakdown,
    TrafficStatsCollectionResult,
    TrafficStatsError,
    UsageWindow,
    UserTrafficCounters,
    UserTrafficSummary,
    _cycle_bounds,
    _month_range,
    _ServiceSnapshot,
    _utc_now,
    local_date,
)
from sing_box_manager.traffic_store import TrafficStatsStore as _TrafficStatsStore

if TYPE_CHECKING:
    from sing_box_manager.settings import Settings

__all__ = [
    "DEFAULT_BILLING_DAY",
    "NO_DOMAIN_KEY",
    "OTHER_DOMAIN_KEY",
    "RESERVED_DOMAIN_KEYS",
    "STREAM_ATTRIBUTED_RETENTION",
    "TRAFFIC_PROTOCOLS",
    "UNATTRIBUTED_DOMAIN_KEY",
    "DailyUsagePoint",
    "DailyUsageSeries",
    "DestinationUsage",
    "DestinationUsageBreakdown",
    "DomainDelta",
    "DomainUsage",
    "DomainUsageBreakdown",
    "LiveConnection",
    "MonthlyUsageHistory",
    "MonthlyUsagePoint",
    "ProtocolUsage",
    "ProtocolUsageBreakdown",
    "TrafficStatsCollectionResult",
    "TrafficStatsError",
    "UsageWindow",
    "UserTrafficCounters",
    "UserTrafficSummary",
    "_ServiceSnapshot",
    "_cycle_bounds",
    "_month_range",
    "_utc_now",
    "local_date",
]

logger = logging.getLogger(__name__)

# Refresh windows for the derived billing day. Once a day is known it is a fact
# that essentially never changes, so one call a day is plenty; until then, retry
# often enough that a fresh deployment converges within the hour.
_BILLING_DAY_REFRESH_SECONDS = 86_400
_BILLING_DAY_RETRY_SECONDS = 900


_QUERY_STATS_METHOD = "/v2ray.core.app.stats.command.StatsService/QueryStats"
_GET_SYS_STATS_METHOD = "/v2ray.core.app.stats.command.StatsService/GetSysStats"
_GRPC_TIMEOUT_SECONDS = 10

# Bump when the way build tags or ldflags are derived changes. Those are read
# out of the upstream clone, so they cannot be part of the cache key before the
# clone exists -- this constant stands in for them.
BUILD_RECIPE_VERSION = 1
SERVER_BINARY_CACHE_DIR_NAME = "server-binary"
SERVER_BINARY_NAME = "sing-box"
SERVER_BINARY_GOOS = "linux"
SERVER_BINARY_GOARCH = "amd64"
SERVER_BINARY_CGO_ENABLED = "0"


@dataclass(slots=True, frozen=True)
class _ServiceTarget:
    protocol: str
    listen_address: str
    usernames: tuple[str, ...]


def _resolve_project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def open_store(database_path: Path) -> _TrafficStatsStore:
    """Open the traffic stats database for writing."""
    return _TrafficStatsStore(database_path)


def _build_service_targets(
    settings: Settings, inventory: RuntimeInventory
) -> tuple[_ServiceTarget, ...]:
    trojan_users = tuple(
        user.username for user in inventory.trojan.users if user.enabled
    )
    hysteria2_users = tuple(
        user.username for user in inventory.hysteria2.users if user.enabled
    )
    naive_users = tuple(user.username for user in inventory.naive.users if user.enabled)
    targets: list[_ServiceTarget] = []
    if trojan_users:
        targets.append(
            _ServiceTarget(
                protocol="trojan",
                listen_address=settings.traffic_stats.api_listen,
                usernames=trojan_users,
            )
        )
    if hysteria2_users:
        targets.append(
            _ServiceTarget(
                protocol="hysteria2",
                listen_address=settings.traffic_stats.api_listen,
                usernames=hysteria2_users,
            )
        )
    if naive_users:
        targets.append(
            _ServiceTarget(
                protocol="naive",
                listen_address=settings.traffic_stats.api_listen,
                usernames=naive_users,
            )
        )
    return tuple(targets)


def _load_inventory(settings: Settings) -> RuntimeInventory:
    load_local_env()
    return load_runtime_inventory(_resolve_project_path(settings.require_config_path()))


def _add_proto_field(
    message_descriptor: Any,
    field_spec: tuple[str, int, int, int, str | None],
) -> None:
    name, number, field_type, label, type_name = field_spec
    field = message_descriptor.field.add()
    field.name = name
    field.number = number
    field.label = label
    field.type = field_type
    if type_name is not None:
        field.type_name = type_name


@lru_cache(maxsize=1)
def _v2ray_api_message_types() -> tuple[
    Any,
    Any,
    Any,
    Any,
]:
    descriptor_pb2: Any = importlib.import_module("google.protobuf.descriptor_pb2")
    descriptor_pool: Any = importlib.import_module("google.protobuf.descriptor_pool")
    message_factory: Any = importlib.import_module("google.protobuf.message_factory")

    # Generated protobuf members are exposed dynamically on the imported modules.
    descriptor_pb2_module = cast("Any", descriptor_pb2)
    descriptor_pool_module = cast("Any", descriptor_pool)
    message_factory_module = cast("Any", message_factory)

    file_descriptor_proto = descriptor_pb2_module.FileDescriptorProto
    field_descriptor_proto = descriptor_pb2_module.FieldDescriptorProto
    descriptor_pool_type = descriptor_pool_module.DescriptorPool
    get_message_class = message_factory_module.GetMessageClass

    file_descriptor = file_descriptor_proto()
    file_descriptor.name = "experimental/v2rayapi/stats.proto"
    file_descriptor.package = "experimental.v2rayapi"
    file_descriptor.syntax = "proto3"

    stat_message = file_descriptor.message_type.add()
    stat_message.name = "Stat"
    for number, name, field_type in [
        (1, "name", field_descriptor_proto.TYPE_STRING),
        (2, "value", field_descriptor_proto.TYPE_INT64),
    ]:
        _add_proto_field(
            stat_message,
            (
                name,
                number,
                field_type,
                field_descriptor_proto.LABEL_OPTIONAL,
                None,
            ),
        )

    query_request = file_descriptor.message_type.add()
    query_request.name = "QueryStatsRequest"
    for number, name, field_type, label in [
        (
            1,
            "pattern",
            field_descriptor_proto.TYPE_STRING,
            field_descriptor_proto.LABEL_OPTIONAL,
        ),
        (
            2,
            "reset",
            field_descriptor_proto.TYPE_BOOL,
            field_descriptor_proto.LABEL_OPTIONAL,
        ),
        (
            3,
            "patterns",
            field_descriptor_proto.TYPE_STRING,
            field_descriptor_proto.LABEL_REPEATED,
        ),
        (
            4,
            "regexp",
            field_descriptor_proto.TYPE_BOOL,
            field_descriptor_proto.LABEL_OPTIONAL,
        ),
    ]:
        _add_proto_field(query_request, (name, number, field_type, label, None))

    query_response = file_descriptor.message_type.add()
    query_response.name = "QueryStatsResponse"
    _add_proto_field(
        query_response,
        (
            "stat",
            1,
            field_descriptor_proto.TYPE_MESSAGE,
            field_descriptor_proto.LABEL_REPEATED,
            ".experimental.v2rayapi.Stat",
        ),
    )

    sys_stats_request = file_descriptor.message_type.add()
    sys_stats_request.name = "SysStatsRequest"

    sys_stats_response = file_descriptor.message_type.add()
    sys_stats_response.name = "SysStatsResponse"
    for number, name, field_type in [
        (1, "NumGoroutine", field_descriptor_proto.TYPE_UINT32),
        (2, "NumGC", field_descriptor_proto.TYPE_UINT32),
        (3, "Alloc", field_descriptor_proto.TYPE_UINT64),
        (4, "TotalAlloc", field_descriptor_proto.TYPE_UINT64),
        (5, "Sys", field_descriptor_proto.TYPE_UINT64),
        (6, "Mallocs", field_descriptor_proto.TYPE_UINT64),
        (7, "Frees", field_descriptor_proto.TYPE_UINT64),
        (8, "LiveObjects", field_descriptor_proto.TYPE_UINT64),
        (9, "PauseTotalNs", field_descriptor_proto.TYPE_UINT64),
        (10, "Uptime", field_descriptor_proto.TYPE_UINT32),
    ]:
        _add_proto_field(
            sys_stats_response,
            (
                name,
                number,
                field_type,
                field_descriptor_proto.LABEL_OPTIONAL,
                None,
            ),
        )

    pool = descriptor_pool_type()
    pool.Add(file_descriptor)

    query_stats_request = get_message_class(
        pool.FindMessageTypeByName("experimental.v2rayapi.QueryStatsRequest")
    )
    query_stats_response = get_message_class(
        pool.FindMessageTypeByName("experimental.v2rayapi.QueryStatsResponse")
    )
    sys_stats_request = get_message_class(
        pool.FindMessageTypeByName("experimental.v2rayapi.SysStatsRequest")
    )
    sys_stats_response = get_message_class(
        pool.FindMessageTypeByName("experimental.v2rayapi.SysStatsResponse")
    )

    return (
        query_stats_request,
        query_stats_response,
        sys_stats_request,
        sys_stats_response,
    )


def _query_service_snapshot(target: _ServiceTarget) -> _ServiceSnapshot:
    grpc: Any = importlib.import_module("grpc")

    (
        query_stats_request_type,
        query_stats_response_type,
        sys_stats_request_type,
        sys_stats_response_type,
    ) = _v2ray_api_message_types()

    with grpc.insecure_channel(target.listen_address) as channel:
        query_stats = channel.unary_unary(
            _QUERY_STATS_METHOD,
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=query_stats_response_type.FromString,
        )
        get_sys_stats = channel.unary_unary(
            _GET_SYS_STATS_METHOD,
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=sys_stats_response_type.FromString,
        )

        query_request = query_stats_request_type()
        query_request.reset = False
        query_request.regexp = False
        query_request.patterns.extend(
            [
                f"user>>>{counter_username(target.protocol, username)}>>>traffic>>>"
                for username in target.usernames
            ]
        )
        stats_response = query_stats(query_request, timeout=_GRPC_TIMEOUT_SECONDS)
        sys_stats_response = get_sys_stats(
            sys_stats_request_type(),
            timeout=_GRPC_TIMEOUT_SECONDS,
        )

    counters = {username: UserTrafficCounters() for username in target.usernames}
    wire_names = {
        counter_username(target.protocol, username): username
        for username in target.usernames
    }
    for stat in stats_response.stat:
        parts = str(stat.name).split(">>>")
        if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
            continue

        username = wire_names.get(parts[1], "")
        direction = parts[3]
        if username not in counters:
            continue

        current = counters[username]
        value = max(int(stat.value), 0)
        if direction == "uplink":
            counters[username] = UserTrafficCounters(
                upload_bytes=value,
                download_bytes=current.download_bytes,
            )
        elif direction == "downlink":
            counters[username] = UserTrafficCounters(
                upload_bytes=current.upload_bytes,
                download_bytes=value,
            )

    return _ServiceSnapshot(
        uptime_seconds=max(int(sys_stats_response.Uptime), 0),
        counters=counters,
    )


def resolve_database_path(settings: Settings) -> Path:
    return _resolve_project_path(settings.traffic_stats.database_path)


def _refresh_billing_day(
    settings: Settings, store: _TrafficStatsStore, *, now: datetime
) -> int:
    """The billing day to bucket by, refreshed from KiwiVM at most once a day.

    Never raises. The billing day is a convenience; a KiwiVM outage must not stop
    a traffic collection, so every failure falls back to the last known value and
    ultimately to the calendar month.
    """
    stored = store.read_reported_billing_day()
    fallback = DEFAULT_BILLING_DAY if stored is None else stored

    credentials = kiwivm.resolve_credentials(settings.vps_info)
    if credentials is None:
        # No KiwiVM configured is the ordinary state of a plain deployment.
        # Cost it nothing: no request, no write, no log noise.
        return fallback

    checked_at = store.read_billing_day_checked_at()
    window = (
        _BILLING_DAY_RETRY_SECONDS if stored is None else _BILLING_DAY_REFRESH_SECONDS
    )
    if checked_at is not None:
        elapsed = (now - checked_at).total_seconds()
        # The lower bound means a backwards clock jump suppresses a refresh
        # rather than triggering one on every run until the clock catches up.
        if 0 <= elapsed < window:
            return fallback

    try:
        data = kiwivm.fetch_service_info(*credentials)
        billing_day = kiwivm.billing_day_from_service_info(
            data, settings.traffic_stats.timezone
        )
    except Exception:
        logger.warning("Failed to refresh the billing day from KiwiVM", exc_info=True)
        billing_day = None

    store.record_billing_day_check(billing_day, checked_at=now)
    return fallback if billing_day is None else billing_day


def _reconcile_domain_attribution(
    store: _TrafficStatsStore, collected_at: datetime
) -> None:
    """Update the optional breakdown without failing an authoritative poll."""
    try:
        parked = store.reconcile_domain_attribution(collected_at=collected_at)
    except Exception:
        logger.warning("Failed to reconcile per-domain attribution", exc_info=True)
        return
    if parked:
        logger.info("Parked %d bytes in %s", parked, UNATTRIBUTED_DOMAIN_KEY)


def collect_traffic_stats(
    settings: Settings,
    *,
    now: datetime | None = None,
) -> TrafficStatsCollectionResult:
    if not settings.traffic_stats.enabled:
        raise TrafficStatsError("traffic_stats is disabled in config")

    inventory = _load_inventory(settings)
    targets = _build_service_targets(settings, inventory)
    database_path = resolve_database_path(settings)
    usage_date = local_date(settings.traffic_stats.timezone, now)
    collected_at = _utc_now(now)
    store = _TrafficStatsStore(database_path)

    # After _load_inventory, which is what calls load_local_env() and so
    # populates the KIWI_VEID/KIWI_API_KEY fallback; and before the first
    # record_snapshot, so this run's deltas land in the right bucket rather than
    # being rewritten out from under it by the reconcile.
    billing_day = _refresh_billing_day(settings, store, now=collected_at)
    store.reconcile_billing_day(billing_day, now=collected_at)

    successful_services: list[str] = []
    failed_services: list[str] = []
    for target in targets:
        try:
            snapshot = _query_service_snapshot(target)
            store.record_snapshot(
                service_name=target.protocol,
                usage_date=usage_date,
                collected_at=collected_at,
                snapshot=snapshot,
                billing_day=billing_day,
            )
            successful_services.append(target.protocol)
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            failed_services.append(f"{target.protocol}: {detail}")

    if failed_services and not successful_services:
        joined = "; ".join(failed_services)
        raise TrafficStatsError(f"Traffic stats collection failed: {joined}")

    if successful_services:
        _reconcile_domain_attribution(store, collected_at)

    return TrafficStatsCollectionResult(
        database_path=database_path,
        successful_services=tuple(successful_services),
        failed_services=tuple(failed_services),
    )


class TrafficStatsProvider:
    def __init__(self, database_path: Path, timezone_name: str) -> None:
        self._database_path = database_path
        self._timezone_name = timezone_name

    def _cycle_window(self, now: datetime | None) -> tuple[str, date, date]:
        """The cycle to render, as ``(key, start, end)``.

        Every getter goes through here, so none of them can pick a different
        boundary than the rows it is about to sum. The day comes from the basis
        the stored rows were built under, not the latest day KiwiVM reported --
        the collector rewrites the rows and the basis together, so reading the
        basis is what keeps the window and the totals describing the same cycle.
        """
        billing_day = _TrafficStatsStore(self._database_path).read_cycle_basis_day()
        return _cycle_bounds(local_date(self._timezone_name, now), billing_day)

    def get_user_summary(
        self, username: str, *, now: datetime | None = None
    ) -> UserTrafficSummary:
        cycle_month, cycle_start, cycle_end = self._cycle_window(now)
        return _TrafficStatsStore(self._database_path).read_user_summary(
            username=username,
            cycle_month=cycle_month,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            timezone_name=self._timezone_name,
        )

    def get_user_summaries(
        self, usernames: Sequence[str], *, now: datetime | None = None
    ) -> tuple[UserTrafficSummary, ...]:
        cycle_month, cycle_start, cycle_end = self._cycle_window(now)
        return _TrafficStatsStore(self._database_path).read_user_summaries(
            usernames=usernames,
            cycle_month=cycle_month,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            timezone_name=self._timezone_name,
        )

    def get_all_user_summaries(
        self, known_usernames: Sequence[str], *, now: datetime | None = None
    ) -> tuple[UserTrafficSummary, ...]:
        cycle_month, cycle_start, cycle_end = self._cycle_window(now)
        return _TrafficStatsStore(self._database_path).read_all_user_summaries(
            known_usernames=known_usernames,
            cycle_month=cycle_month,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            timezone_name=self._timezone_name,
        )

    def get_daily_series(
        self,
        username: str | None = None,
        *,
        days: int = 30,
        now: datetime | None = None,
    ) -> DailyUsageSeries:
        """Daily totals for the last ``days`` days, ending today. None == everyone."""
        end_date = local_date(self._timezone_name, now)
        start_date = end_date - timedelta(days=max(days, 1) - 1)
        return _TrafficStatsStore(self._database_path).read_daily_series(
            username=username,
            start_date=start_date,
            end_date=end_date,
            timezone_name=self._timezone_name,
        )

    def _usage_window(self, days: int, now: datetime | None) -> UsageWindow:
        return UsageWindow.trailing(
            days=max(days, 1),
            timezone_name=self._timezone_name,
            today=local_date(self._timezone_name, now),
        )

    def get_domain_breakdown(
        self,
        username: str | None = None,
        *,
        days: int = 30,
        limit: int = 20,
        now: datetime | None = None,
    ) -> DomainUsageBreakdown:
        """Top destinations over the last ``days`` days. None == everyone."""
        return _TrafficStatsStore(self._database_path).read_domain_breakdown(
            username=username,
            window=self._usage_window(days, now),
            limit=limit,
        )

    def get_destination_breakdown(
        self,
        username: str | None = None,
        *,
        days: int = 30,
        limit: int = 20,
        now: datetime | None = None,
    ) -> DestinationUsageBreakdown:
        """Top destination IPs over the last ``days`` days. None == everyone."""
        return _TrafficStatsStore(self._database_path).read_destination_breakdown(
            username=username,
            window=self._usage_window(days, now),
            limit=limit,
        )

    def get_live_connections(
        self,
        username: str | None = None,
        *,
        limit: int = 500,
        now: datetime | None = None,
    ) -> tuple[LiveConnection, ...]:
        """The stream daemon's most recent snapshot of open connections.

        A clean disconnect clears the service's rows. Reads also hide expired
        snapshots after an unclean daemon shutdown.
        """
        return _TrafficStatsStore(self._database_path).read_live_connections(
            username=username, limit=limit, now=now
        )

    def get_protocol_breakdown(
        self, username: str | None = None, *, now: datetime | None = None
    ) -> ProtocolUsageBreakdown:
        cycle_month, _, _ = self._cycle_window(now)
        return _TrafficStatsStore(self._database_path).read_protocol_breakdown(
            username=username,
            cycle_month=cycle_month,
            timezone_name=self._timezone_name,
        )

    def get_monthly_history(
        self,
        username: str | None = None,
        *,
        months: int = 6,
        now: datetime | None = None,
    ) -> MonthlyUsageHistory:
        cycle_month, _, _ = self._cycle_window(now)
        return _TrafficStatsStore(self._database_path).read_monthly_history(
            username=username,
            end_cycle_month=cycle_month,
            months=months,
            timezone_name=self._timezone_name,
        )


def create_traffic_stats_provider(settings: Settings) -> TrafficStatsProvider | None:
    if not settings.traffic_stats.enabled:
        return None

    return TrafficStatsProvider(
        resolve_database_path(settings),
        settings.traffic_stats.timezone,
    )


def resolve_server_binary_cache_root(settings: Settings) -> Path:
    """Resolve the cache directory holding previously built server binaries."""
    upstream_root = Path(settings.upstream_cache_root)
    if not upstream_root.is_absolute():
        upstream_root = PROJECT_ROOT / upstream_root
    return upstream_root.parent / SERVER_BINARY_CACHE_DIR_NAME


@lru_cache(maxsize=1)
def _go_toolchain_version() -> str:
    """Return the exact `go version` string this machine would build with."""
    try:
        completed = subprocess.run(
            ["go", "version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TrafficStatsError("Required command not found: go") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise TrafficStatsError(
            detail or "Unable to determine the Go toolchain version"
        ) from exc

    return completed.stdout.strip()


def _server_binary_cache_key(release_tag: str) -> str:
    """Identify a build recipe so an unchanged one can be reused from cache.

    `GOTOOLCHAIN=local` pins the build to whichever Go pixi resolved, and the
    `go >=1.25,<1.26` constraint lets that drift inside the range, so the exact
    version string has to be part of the key. The upstream build tags and
    ldflags are read out of the clone and so cannot be known before it exists;
    they are a deterministic function of the tag, which the tag already covers.
    BUILD_RECIPE_VERSION busts the cache when the way they are derived changes.
    """
    payload = "\n".join(
        [
            f"recipe={BUILD_RECIPE_VERSION}",
            f"tag={release_tag}",
            f"goos={SERVER_BINARY_GOOS}",
            f"goarch={SERVER_BINARY_GOARCH}",
            f"cgo={SERVER_BINARY_CGO_ENABLED}",
            f"toolchain={_go_toolchain_version()}",
        ]
    )
    return sha256_for_text(payload)


def _server_binary_cache_path(settings: Settings, release_tag: str) -> Path:
    return (
        resolve_server_binary_cache_root(settings)
        / release_tag
        / _server_binary_cache_key(release_tag)
        / SERVER_BINARY_NAME
    )


def _cached_server_binary(cache_path: Path) -> Path | None:
    """Return the cached binary only when its bytes still match its digest."""
    if not cache_path.is_file():
        return None

    expected_digest = load_cached_digest(cache_path)
    if expected_digest is None:
        return None

    if sha256_for_path(cache_path) != expected_digest:
        # A truncated or tampered entry is worse than no entry at all, because
        # it would silently ship a binary nobody built.
        cache_path.unlink(missing_ok=True)
        digest_sidecar_path(cache_path).unlink(missing_ok=True)
        return None

    return cache_path


def _store_server_binary_in_cache(cache_path: Path, source: Path) -> None:
    """Publish a freshly built binary into the cache atomically."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path = cache_path.with_name(f"{cache_path.name}.tmp")
    try:
        shutil.copy2(source, staged_path)
        os.replace(staged_path, cache_path)
    except OSError:
        staged_path.unlink(missing_ok=True)
        raise

    write_cached_digest(cache_path, sha256_for_path(cache_path))


def build_custom_server_binary(
    settings: Settings,
    destination: Path,
) -> Path:
    """Build the traffic-stats server binary, reusing a cached build if possible."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    release_tag = f"v{settings.sing_box_version}"

    cache_path = _server_binary_cache_path(settings, release_tag)
    cached_binary = _cached_server_binary(cache_path)
    if cached_binary is not None:
        logger.debug("Reusing cached sing-box server binary at %s", cached_binary)
        shutil.copy2(cached_binary, destination)
        return destination

    _compile_custom_server_binary(settings, release_tag, destination)
    _store_server_binary_in_cache(cache_path, destination)
    return destination


def _compile_custom_server_binary(
    settings: Settings,
    release_tag: str,
    destination: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="sing-box-src-") as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        source_dir = temp_dir / "sing-box"
        _run_command(
            [
                "git",
                "clone",
                "--branch",
                release_tag,
                "--depth",
                "1",
                "https://github.com/SagerNet/sing-box.git",
                str(source_dir),
            ],
            cwd=temp_dir,
        )
        build_tags = _upstream_build_tags(source_dir)
        if "with_v2ray_api" not in build_tags:
            build_tags.append("with_v2ray_api")
        ldflags = _build_ldflags(
            settings.sing_box_version,
            _upstream_ldflags(source_dir),
        )

        _run_command(
            [
                "go",
                "build",
                "-trimpath",
                "-o",
                str(destination),
                "-tags",
                ",".join(build_tags),
                "-ldflags",
                ldflags,
                "./cmd/sing-box",
            ],
            cwd=source_dir,
            env={
                "CGO_ENABLED": SERVER_BINARY_CGO_ENABLED,
                "GOOS": SERVER_BINARY_GOOS,
                "GOARCH": SERVER_BINARY_GOARCH,
                "GOTOOLCHAIN": "local",
            },
        )


def _parse_build_tags(raw_tags: str) -> list[str]:
    return [tag for tag in raw_tags.replace(",", " ").split() if tag]


def _tags_from_default_build_tags(source_dir: Path) -> list[str] | None:
    default_tags_path = source_dir / "release" / "DEFAULT_BUILD_TAGS_OTHERS"
    if not default_tags_path.is_file():
        return None

    tags = _parse_build_tags(default_tags_path.read_text(encoding="utf-8"))
    if tags:
        return tags

    return None


def _tags_from_makefile(source_dir: Path) -> list[str] | None:
    makefile_path = source_dir / "Makefile"
    if not makefile_path.is_file():
        return None

    for line in makefile_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("TAGS ?="):
            continue
        _, _, raw_tags = line.partition("=")
        if "$(" in raw_tags:
            return None

        tags = _parse_build_tags(raw_tags)
        if tags:
            return tags

    return None


def _upstream_build_tags(source_dir: Path) -> list[str]:
    default_build_tags = _tags_from_default_build_tags(source_dir)
    if default_build_tags is not None:
        return default_build_tags

    makefile_tags = _tags_from_makefile(source_dir)
    if makefile_tags is not None:
        return makefile_tags

    raise TrafficStatsError("Unable to determine upstream sing-box build tags")


def _upstream_ldflags(source_dir: Path) -> list[str]:
    ldflags_path = source_dir / "release" / "LDFLAGS"
    if not ldflags_path.is_file():
        return []

    raw_ldflags = ldflags_path.read_text(encoding="utf-8").strip()
    if not raw_ldflags:
        return []

    try:
        return shlex.split(raw_ldflags)
    except ValueError as exc:
        raise TrafficStatsError("Unable to parse upstream sing-box ldflags") from exc


def _build_ldflags(version: str, upstream_ldflags: list[str]) -> str:
    return shlex.join(
        [
            "-s",
            "-buildid=",
            "-X",
            f"github.com/sagernet/sing-box/constant.Version={version}",
            *upstream_ldflags,
        ]
    )


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=None if env is None else {**os.environ, **env},
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TrafficStatsError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if detail:
            raise TrafficStatsError(detail) from exc
        raise TrafficStatsError(f"Command failed: {' '.join(command)}") from exc

    if completed.stderr:
        logger.debug(completed.stderr.strip())
