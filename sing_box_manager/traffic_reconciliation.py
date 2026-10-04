"""Match observation-day excess to a poll that crossed a calendar boundary.

The poller knows the total for an interval, not when its bytes crossed midnight.
Offsets are therefore a bounded accounting estimate. They never change observed
usage dates and never transfer bytes between users, services, or directions.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sing_box_manager.traffic_models import UserTrafficCounters

UsageKey = tuple[date, str, str]


@dataclass(frozen=True, slots=True)
class PollBoundary:
    start_date: date
    end_date: date
    username: str
    protocol: str
    counters: UserTrafficCounters


@dataclass(frozen=True, slots=True)
class BoundaryOffset:
    source_date: date
    target_date: date
    username: str
    protocol: str
    counters: UserTrafficCounters


def reconcile_boundaries(
    residuals: dict[UsageKey, UserTrafficCounters],
    boundaries: tuple[PollBoundary, ...],
) -> tuple[dict[UsageKey, UserTrafficCounters], tuple[BoundaryOffset, ...]]:
    """Restate gaps from signed residuals and immutable cross-day poll totals.

    An excess is eligible only on a day touched by that poll, and the matched
    amount cannot exceed the poll's own delta. Unused excess does not become an
    unlimited credit against future traffic. Recomputing from source totals
    allows a late stream flush to shrink an earlier unattributed estimate.
    """
    remaining = {
        key: [value.upload_bytes, value.download_bytes]
        for key, value in residuals.items()
    }
    dates_by_owner: dict[tuple[str, str], list[date]] = defaultdict(list)
    for day, username, protocol in sorted(remaining):
        dates_by_owner[username, protocol].append(day)
    offsets = []
    for boundary in sorted(
        boundaries, key=lambda item: (item.end_date, item.protocol, item.username)
    ):
        target_key = (boundary.end_date, boundary.username, boundary.protocol)
        target = remaining.get(target_key)
        if target is None:
            continue
        capacity = [boundary.counters.upload_bytes, boundary.counters.download_bytes]
        dates = dates_by_owner[boundary.username, boundary.protocol]
        start = bisect_left(dates, boundary.start_date)
        end = bisect_left(dates, boundary.end_date)
        for source_date in dates[start:end]:
            source_key = (source_date, boundary.username, boundary.protocol)
            source = remaining[source_key]
            matched = [
                min(max(0, -source[i]), max(0, target[i]), capacity[i])
                for i in range(2)
            ]
            if not any(matched):
                continue
            for i in range(2):
                source[i] += matched[i]
                target[i] -= matched[i]
                capacity[i] -= matched[i]
            offsets.append(
                BoundaryOffset(
                    source_key[0],
                    boundary.end_date,
                    boundary.username,
                    boundary.protocol,
                    UserTrafficCounters(*matched),
                )
            )
    gaps = {
        key: UserTrafficCounters(max(0, value[0]), max(0, value[1]))
        for key, value in remaining.items()
        if value[0] > 0 or value[1] > 0
    }
    return gaps, tuple(offsets)
