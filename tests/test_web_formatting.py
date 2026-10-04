"""Tests for the portal's shared value formatting."""

from __future__ import annotations

import pytest

from sing_box_manager.web.formatting import format_plan_usage

GIB = 1024**3


@pytest.mark.parametrize(
    ("total_bytes", "multiplier", "plan_total_bytes", "expected"),
    [
        pytest.param(
            round(110.5 * GIB),
            2.0,
            1000 * GIB,
            "221.00 GiB / 1000.00 GiB (22.1%, x2 relay)",
            id="doubled-with-plan",
        ),
        pytest.param(
            round(110.5 * GIB),
            2.0,
            None,
            "221.00 GiB (x2 relay)",
            id="doubled-without-plan",
        ),
        pytest.param(
            100 * GIB,
            1.0,
            1000 * GIB,
            "100.00 GiB / 1000.00 GiB (10.0%)",
            id="egress-only-with-plan",
        ),
        pytest.param(
            100 * GIB,
            2.1,
            1000 * GIB,
            "210.00 GiB / 1000.00 GiB (21.0%, x2.1 relay)",
            id="calibrated-multiplier",
        ),
    ],
)
def test_plan_usage_reads_the_same_as_the_shell_clients(
    total_bytes: int,
    multiplier: float,
    plan_total_bytes: int | None,
    expected: str,
) -> None:
    """Byte-for-byte the strings `_proxy_format_plan_usage` prints.

    A user who reads the portal and a user who runs `proxy check quota` are
    looking at the same number, so the two must not disagree about how it is
    written, down to the dropped zero in `x2`.
    """
    assert (
        format_plan_usage(
            total_bytes,
            relay_multiplier=multiplier,
            plan_total_bytes=plan_total_bytes,
        )
        == expected
    )


def test_plan_usage_is_empty_when_it_would_repeat_the_total() -> None:
    """An egress-only host with no plan total has nothing to project.

    The projection equals the measured total, and a second row repeating it
    reads as a different quantity that happens to agree. Callers render the row
    only when this returns something.
    """
    assert (
        format_plan_usage(100 * GIB, relay_multiplier=1.0, plan_total_bytes=None) == ""
    )


def test_plan_usage_treats_an_unknown_plan_as_absent() -> None:
    """A zero plan total is a host that reported no allowance, not a full one."""
    assert (
        format_plan_usage(100 * GIB, relay_multiplier=2.0, plan_total_bytes=0)
        == "200.00 GiB (x2 relay)"
    )
