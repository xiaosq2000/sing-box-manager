"""Traffic values and calendar rules shared by collectors and storage."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_BILLING_DAY = 1
TRAFFIC_PROTOCOLS: tuple[str, ...] = ("trojan", "hysteria2", "naive")

OTHER_DOMAIN_KEY = "__other__"
UNATTRIBUTED_DOMAIN_KEY = "__unattributed__"
NO_DOMAIN_KEY = "__ip__"
RESERVED_DOMAIN_KEYS = frozenset(
    {OTHER_DOMAIN_KEY, UNATTRIBUTED_DOMAIN_KEY, NO_DOMAIN_KEY}
)

# Live checkpoints are uncapped; this limit applies only after retirement.
STREAM_ATTRIBUTED_RETENTION = 4000
LIVE_SNAPSHOT_MAX_AGE_SECONDS = 180


class TrafficStatsError(RuntimeError):
    """Raised when traffic stats collection cannot complete."""


@dataclass(slots=True, frozen=True)
class UserTrafficCounters:
    upload_bytes: int = 0
    download_bytes: int = 0


@dataclass(slots=True, frozen=True)
class TrafficStatsCollectionResult:
    database_path: Path
    successful_services: tuple[str, ...]
    failed_services: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class UserTrafficSummary:
    username: str
    cycle_month: str
    cycle_start: date
    cycle_end: date
    timezone: str
    upload_bytes: int = 0
    download_bytes: int = 0
    updated_at: datetime | None = None

    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes


@dataclass(slots=True, frozen=True)
class DailyUsagePoint:
    usage_date: date
    upload_bytes: int = 0
    download_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes


@dataclass(slots=True, frozen=True)
class DailyUsageSeries:
    """One point per day across the whole window, gaps included as zeroes.

    Density is deliberate. A collection outage should read as an honest zero
    rather than a missing point a chart would silently interpolate across, and
    it means neither the renderers nor the templates need gap logic.
    """

    username: str | None
    start_date: date
    end_date: date
    timezone: str
    points: tuple[DailyUsagePoint, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(point.total_bytes for point in self.points)


@dataclass(slots=True, frozen=True)
class ProtocolUsage:
    protocol: str
    upload_bytes: int = 0
    download_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes


@dataclass(slots=True, frozen=True)
class ProtocolUsageBreakdown:
    username: str | None
    cycle_month: str
    timezone: str
    protocols: tuple[ProtocolUsage, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(protocol.total_bytes for protocol in self.protocols)


@dataclass(slots=True, frozen=True)
class MonthlyUsagePoint:
    cycle_month: str
    upload_bytes: int = 0
    download_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes


@dataclass(slots=True, frozen=True)
class MonthlyUsageHistory:
    username: str | None
    timezone: str
    months: tuple[MonthlyUsagePoint, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(month.total_bytes for month in self.months)


@dataclass(slots=True, frozen=True)
class DomainUsage:
    domain: str
    upload_bytes: int = 0
    download_bytes: int = 0
    connection_count: int = 0

    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes

    @property
    def is_reserved(self) -> bool:
        """Whether this row is a bookkeeping bucket rather than a real host.

        The portal renders these differently: `__unattributed__` in particular
        is a health signal, not a destination anyone visited.
        """
        return self.domain in RESERVED_DOMAIN_KEYS


@dataclass(slots=True, frozen=True)
class DomainUsageBreakdown:
    username: str | None
    timezone: str
    days: int
    domains: tuple[DomainUsage, ...] = ()
    # Bytes across every domain in the window, not only the rows kept. `domains`
    # is truncated to what a table can show, so a share divided by their sum
    # would read as a share of the table rather than of the traffic and would
    # always add up to 100%.
    window_total_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(domain.total_bytes for domain in self.domains)


@dataclass(slots=True, frozen=True)
class DestinationUsage:
    destination_ip: str
    upload_bytes: int = 0
    download_bytes: int = 0
    connection_count: int = 0

    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes


@dataclass(slots=True, frozen=True)
class DestinationUsageBreakdown:
    username: str | None
    timezone: str
    days: int
    destinations: tuple[DestinationUsage, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(item.total_bytes for item in self.destinations)


@dataclass(slots=True, frozen=True)
class LiveConnection:
    connection_id: str
    service_name: str
    username: str
    network: str
    protocol: str
    source: str
    destination: str
    domain: str
    outbound: str
    created_at: datetime | None
    upload_bytes: int
    download_bytes: int
    updated_at: datetime | None

    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes


@dataclass(slots=True, frozen=True)
class DomainDelta:
    """One flush's worth of bytes for a single connection grouping."""

    username: str
    domain: str
    destination_ip: str
    upload_bytes: int
    download_bytes: int
    connection_count: int


@dataclass(slots=True, frozen=True)
class _ServiceSnapshot:
    uptime_seconds: int
    counters: dict[str, UserTrafficCounters]


def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def local_date(timezone_name: str, now: datetime | None = None) -> date:
    """The calendar day ``now`` falls on in the configured billing timezone.

    Collectors share the timezone conversion, but supply different observation
    times. The poller dates an interval at its end; the stream dates each event.
    """
    return _utc_now(now).astimezone(ZoneInfo(timezone_name)).date()


@dataclass(slots=True, frozen=True)
class UsageWindow:
    """The day range, and how to label it, that a breakdown covers."""

    start_date: date
    end_date: date
    days: int
    timezone_name: str

    @classmethod
    def trailing(cls, *, days: int, timezone_name: str, today: date) -> UsageWindow:
        return cls(
            start_date=today - timedelta(days=days - 1),
            end_date=today,
            days=days,
            timezone_name=timezone_name,
        )


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + offset
    return total // 12, total % 12 + 1


def _cycle_key(year: int, month: int) -> str:
    """The ``YYYY-MM`` bucket key. Zero-padded, so it sorts and compares as text."""
    return f"{year:04d}-{month:02d}"


def _clamp_billing_day(year: int, month: int, billing_day: int) -> date:
    """The cycle boundary inside one month, pulled back if the month is short.

    A VPS that resets on the 31st still has to reset in February. Clamping keeps
    every boundary inside its own month, which is what makes the sequence
    strictly increasing and therefore makes cycles tile without gap or overlap.
    """
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(billing_day, last_day))


def _cycle_bounds(local_date: date, billing_day: int) -> tuple[str, date, date]:
    """The cycle ``local_date`` falls in, as ``(key, start, end)``.

    The key stays ``YYYY-MM`` and names the month the cycle *starts* in, so a
    billing day of 3 makes ``"2026-09"`` mean 2026-09-03 through 2026-10-02.
    Keeping the key shape lets ``_month_range`` and the month-over-month chart
    go on doing plain string arithmetic.
    """
    year, month = local_date.year, local_date.month
    cycle_start = _clamp_billing_day(year, month, billing_day)
    if local_date < cycle_start:
        year, month = _shift_month(year, month, -1)
        cycle_start = _clamp_billing_day(year, month, billing_day)
    next_year, next_month = _shift_month(year, month, 1)
    cycle_end = _clamp_billing_day(next_year, next_month, billing_day) - timedelta(
        days=1
    )
    return _cycle_key(year, month), cycle_start, cycle_end


def _normalize_billing_day(value: str | None) -> int | None:
    """Coerce a stored or fetched billing day, rejecting anything out of range.

    Going through ``str`` is what lets a missing value fall into the same
    ``ValueError`` arm as a corrupted one.
    """
    try:
        day = int(str(value))
    except ValueError:
        return None
    return day if 1 <= day <= 31 else None


def _date_range(start_date: date, end_date: date) -> tuple[date, ...]:
    if end_date < start_date:
        return ()
    span = (end_date - start_date).days
    return tuple(start_date + timedelta(days=offset) for offset in range(span + 1))


def _month_range(end_cycle_month: str, months: int) -> tuple[str, ...]:
    """The ``months`` cycle keys ending at ``end_cycle_month``, oldest first."""
    if months < 1:
        return ()
    year, month = (int(part) for part in end_cycle_month.split("-", 1))
    return tuple(
        _cycle_key(*_shift_month(year, month, -offset))
        for offset in range(months - 1, -1, -1)
    )


def _parse_optional_datetime(value: object) -> datetime | None:
    if value in {None, ""}:
        return None

    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _counter_delta(current: int, previous: int, restart_detected: bool) -> int:
    if restart_detected or current < previous:
        return current
    return current - previous


@dataclass(slots=True, frozen=True)
class ConnectionCheckpoint:
    connection_id: str
    counters: UserTrafficCounters = UserTrafficCounters()
    counted: bool = True
    retired_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class DatedDomainDelta:
    usage_date: date
    delta: DomainDelta


@dataclass(slots=True, frozen=True)
class StreamBatch:
    """An immutable, retryable commit for one service, possibly spanning days."""

    service_name: str
    batch_id: str
    deltas: tuple[DatedDomainDelta, ...] = ()
    checkpoints: tuple[ConnectionCheckpoint, ...] = ()
    removed_connections: tuple[str, ...] = ()
