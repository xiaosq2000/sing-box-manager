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


def test_fixture_declares_the_synthetic_counter_matrix() -> None:
    assert FIXTURE["schema"] == 1
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


def test_counter_regression_divergence_is_explicit() -> None:
    assert len(DIVERGENCE_CASES) == 1
    (case,) = DIVERGENCE_CASES
    assert "Intentional contract difference" in case["divergence"]
    assert "rejected" in case["divergence"]
    assert case["expected"]["python"]["ledger"] != case["expected"]["go"]["ledger"]
    assert case["expected"]["go"]["rejected_samples"] == [1]
