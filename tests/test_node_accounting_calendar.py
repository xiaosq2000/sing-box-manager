"""Differential accounting-calendar cases for the Python oracle and the Go target."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from sing_box_manager.traffic_models import _cycle_bounds, local_date

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "node-accounting-calendar.json"
FIXTURE: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = FIXTURE["cases"]
CASES_BY_NAME: dict[str, dict[str, Any]] = {case["name"]: case for case in CASES}
INVARIANTS: dict[str, Any] = FIXTURE["invariants"]


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _local_date(value: str) -> date:
    return date.fromisoformat(value)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_python_calendar_matches_shared_case(case: dict[str, Any]) -> None:
    accounting_date = local_date(case["zone"], _instant(case["at"]))
    key, start, end = _cycle_bounds(accounting_date, case["billing_day"])
    assert {
        "local_date": accounting_date.isoformat(),
        "cycle_key": key,
        "cycle_start": start.isoformat(),
        "cycle_end": end.isoformat(),
    } == case["expected"]


@pytest.mark.parametrize(
    ("case_name", "offset_hours"),
    [
        ("new-york-spring-midnight-after-transition", -5),
        ("new-york-fall-midnight-after-transition", -4),
        ("santiago-midnight-after-skipped-midnight", -4),
    ],
    ids=["new-york-fixed-utc-5", "new-york-fixed-utc-4", "santiago-fixed-utc-4"],
)
def test_new_transition_cases_reject_fixed_offset(
    case_name: str, offset_hours: int
) -> None:
    case = CASES_BY_NAME[case_name]
    accounting_date = (
        _instant(case["at"]).astimezone(timezone(timedelta(hours=offset_hours))).date()
    )
    key, start, end = _cycle_bounds(accounting_date, case["billing_day"])
    assert {
        "local_date": accounting_date.isoformat(),
        "cycle_key": key,
        "cycle_start": start.isoformat(),
        "cycle_end": end.isoformat(),
    } != case["expected"]


def test_fixture_declares_required_calendar_coverage() -> None:
    coverage = FIXTURE["coverage"]
    assert FIXTURE["schema"] == 1
    assert len({case["name"] for case in CASES}) == len(CASES)
    cases = CASES_BY_NAME
    zones = {case["zone"] for case in CASES}
    billing_days = {case["billing_day"] for case in CASES}
    assert set(coverage["zones"]) <= zones
    assert set(coverage["billing_days"]) <= billing_days

    skipped = cases[coverage["skipped_local_midnight_case"]]
    assert skipped["zone"] == coverage["skipped_local_midnight_zone"]
    assert skipped["expected"]["local_date"] == skipped["expected"]["cycle_start"]

    for case in CASES:
        expected = case["expected"]
        assert expected["cycle_key"] == expected["cycle_start"][:7]
        assert (
            _local_date(expected["cycle_start"])
            <= _local_date(expected["local_date"])
            <= _local_date(expected["cycle_end"])
        )


def test_cycles_tile_the_fixture_range_without_gap_or_overlap() -> None:
    first = _local_date(INVARIANTS["first_date"])
    last = _local_date(INVARIANTS["last_date"])
    for billing_day in INVARIANTS["billing_days"]:
        day = first
        seen: dict[str, tuple[date, date]] = {}
        while day <= last:
            key, start, end = _cycle_bounds(day, billing_day)
            assert start <= day <= end
            if key in seen:
                assert seen[key] == (start, end)
            else:
                seen[key] = (start, end)
            day += timedelta(days=1)

        windows = sorted(seen.values())
        assert windows
        for earlier, later in pairwise(windows):
            assert earlier[1] + timedelta(days=1) == later[0]
