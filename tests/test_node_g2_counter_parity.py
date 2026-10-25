"""Differential counter-ledger cases for the Python oracle and the Go target."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sing_box_manager.traffic_models import UserTrafficCounters, _ServiceSnapshot
from sing_box_manager.traffic_store import TrafficStatsStore

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "node-g2-counter-parity.json"
FIXTURE: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = FIXTURE["cases"]
PARITY_CASES = [case for case in CASES if not case.get("divergence")]
DIVERGENCE_CASES = [case for case in CASES if case.get("divergence")]


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _read_python_ledger(database_path: Path) -> dict[str, list[int]]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT usage_date, username, protocol, upload_bytes, download_bytes
            FROM daily_usage
            """
        ).fetchall()
    return {
        f"{usage_date}|{username}|{protocol}": [int(upload), int(download)]
        for usage_date, username, protocol, upload, download in rows
    }


def _run_python_case(case: dict[str, Any], database_path: Path) -> dict[str, list[int]]:
    store = TrafficStatsStore(database_path)
    for sample in case["samples"]:
        if sample.get("reopen_before"):
            store = TrafficStatsStore(database_path)
        collected_at = _at(sample["at"])
        usage_date = collected_at.date()
        # finish_after belongs to the Go adapter's confirmed final sample.
        # Python's record_snapshot has no marker; the ledger effect is the same.
        for protocol in FIXTURE["protocols"]:
            counters = {
                account: UserTrafficCounters(
                    upload_bytes=sample["counters"][account][protocol][0],
                    download_bytes=sample["counters"][account][protocol][1],
                )
                for account in FIXTURE["accounts"]
            }
            store.record_snapshot(
                service_name=protocol,
                usage_date=usage_date,
                collected_at=collected_at,
                snapshot=_ServiceSnapshot(
                    uptime_seconds=sample["uptime_seconds"], counters=counters
                ),
                billing_day=1,
            )
    return _read_python_ledger(database_path)


ALL_CREDENTIALS = sorted(
    credential
    for protocols in FIXTURE["accounts"].values()
    for credential in protocols.values()
)
EXPECTED_DIVERGENCE_REJECTIONS: dict[str, list[str]] = {
    "same-invocation-counter-regression-diverges": ALL_CREDENTIALS,
    "utc-midnight-unchanged-counters-diverges": [],
    "upload-only-regression-diverges": ALL_CREDENTIALS,
    "download-only-regression-diverges": ALL_CREDENTIALS,
    "partial-credential-regression-diverges": [
        "cred-synthetic-a2",
        "cred-synthetic-b4",
    ],
}


def _go_rejections(case: dict[str, Any]) -> dict[int, list[str]]:
    rejections: dict[int, list[str]] = {}
    for rejection in case["expected"]["go"]["rejected"]:
        assert rejection["sample"] not in rejections
        rejections[rejection["sample"]] = rejection["credentials"]
    return rejections


def test_fixture_declares_the_synthetic_counter_matrix() -> None:
    assert FIXTURE["schema"] == 2
    assert FIXTURE["protocols"] == ["trojan", "hysteria2", "naive", "snell"]
    assert sorted(FIXTURE["accounts"]) == ["synthetic-alpha", "synthetic-bravo"]
    credentials = {
        credential
        for protocols in FIXTURE["accounts"].values()
        for credential in protocols.values()
    }
    assert len(credentials) == 8
    assert all(credential.startswith("cred-synthetic-") for credential in credentials)

    for case in CASES:
        for sample in case["samples"]:
            assert set(sample["counters"]) == set(FIXTURE["accounts"])
            for account in FIXTURE["accounts"]:
                assert set(sample["counters"][account]) == set(FIXTURE["protocols"])


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_python_counter_ledger_matches_shared_case(
    case: dict[str, Any], tmp_path: Path
) -> None:
    ledger = _run_python_case(case, tmp_path / "traffic.sqlite3")
    assert ledger == case["expected"]["python"]["ledger"]


@pytest.mark.parametrize(
    "case", PARITY_CASES, ids=[case["name"] for case in PARITY_CASES]
)
def test_shared_case_expects_identical_keyed_daily_values(
    case: dict[str, Any],
) -> None:
    assert case["expected"]["python"]["ledger"] == case["expected"]["go"]["ledger"]


def test_divergences_are_explicit() -> None:
    by_name = {case["name"]: case for case in DIVERGENCE_CASES}
    assert set(by_name) == set(EXPECTED_DIVERGENCE_REJECTIONS)

    for name, expected_credentials in EXPECTED_DIVERGENCE_REJECTIONS.items():
        case = by_name[name]
        assert "Intentional" in case["divergence"]
        assert "Production Python collector behavior is unchanged" in case["divergence"]
        assert case["expected"]["python"]["ledger"] != case["expected"]["go"]["ledger"]
        rejections = _go_rejections(case)
        assert sorted(rejections) == ([1] if expected_credentials else [])
        if expected_credentials:
            assert sorted(rejections[1]) == sorted(expected_credentials)
            assert len(rejections[1]) == len(set(rejections[1]))
            assert set(rejections[1]) <= set(ALL_CREDENTIALS)

    midnight = by_name["utc-midnight-unchanged-counters-diverges"]
    python_ledger = midnight["expected"]["python"]["ledger"]
    go_ledger = midnight["expected"]["go"]["ledger"]
    next_day = midnight["samples"][1]["at"][:10]
    zero_rows = {
        key: value
        for key, value in python_ledger.items()
        if key.startswith(f"{next_day}|")
    }
    assert len(zero_rows) == len(ALL_CREDENTIALS)
    assert all(value == [0, 0] for value in zero_rows.values())
    assert not any(key.startswith(f"{next_day}|") for key in go_ledger)

    for name, regressed_index in (
        ("upload-only-regression-diverges", 0),
        ("download-only-regression-diverges", 1),
    ):
        case = by_name[name]
        first = case["samples"][0]["counters"]
        regressed = case["samples"][1]["counters"]
        other_index = 1 - regressed_index
        for account, protocols in first.items():
            for protocol, values in protocols.items():
                current = regressed[account][protocol]
                assert current[regressed_index] < values[regressed_index]
                assert current[other_index] >= values[other_index]

    partial = by_name["partial-credential-regression-diverges"]
    rejections = _go_rejections(partial)
    assert sorted(rejections) == [1]
    assert sorted(rejections[1]) == ["cred-synthetic-a2", "cred-synthetic-b4"]

    python_ledger = partial["expected"]["python"]["ledger"]
    go_ledger = partial["expected"]["go"]["ledger"]
    assert set(python_ledger) == set(go_ledger)
    differing = {
        key for key, values in python_ledger.items() if values != go_ledger[key]
    }
    assert differing == {
        "2026-05-10|synthetic-alpha|hysteria2",
        "2026-05-10|synthetic-bravo|snell",
    }

    final = partial["samples"][-1]["counters"]
    initial = partial["samples"][0]["counters"]
    for account, protocols in final.items():
        for protocol, values in protocols.items():
            key = f"2026-05-10|{account}|{protocol}"
            assert go_ledger[key] == values
            if key not in differing:
                assert values != initial[account][protocol]
