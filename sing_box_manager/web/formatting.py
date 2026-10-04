"""Shared value formatting for portal pages and charts.

Lives apart from ``routes`` so ``charts`` can render axis and tooltip labels
without importing the web layer that renders the charts.
"""

from __future__ import annotations


def format_byte_count(value: int) -> str:
    suffixes = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    suffix = suffixes[0]
    for suffix in suffixes:
        if size < 1024 or suffix == suffixes[-1]:
            break
        size /= 1024
    if suffix == "B":
        return f"{int(size)} {suffix}"
    return f"{size:.2f} {suffix}"


def format_plan_usage(
    total_bytes: int,
    *,
    relay_multiplier: float,
    plan_total_bytes: int | None,
) -> str:
    """A measured total projected onto the host's bandwidth counter.

    The same row ``proxy check quota`` prints, so a user who reads the portal
    instead of running the client is told the same thing. Without it the page
    shows a measured total beside a plan quota that counts each relayed byte
    twice, and the reader concludes they have used about half the plan they
    really have.

    Returns ``""`` when there is nothing to add: at a multiplier of 1 with no
    plan total the projection equals the measured total, and repeating it under
    a second label would invite the reader to think the two figures are
    different quantities that happen to agree.
    """
    billed = round(total_bytes * relay_multiplier)
    # Named inline so the larger figure reads as derived rather than as a
    # competing measurement. `%g` drops the trailing zero from 2.0.
    relay = "" if relay_multiplier == 1 else f"x{relay_multiplier:g} relay"

    if plan_total_bytes:
        share = f"{billed * 100 / plan_total_bytes:.1f}%"
        annotation = f"{share}, {relay}" if relay else share
        return (
            f"{format_byte_count(billed)} / "
            f"{format_byte_count(plan_total_bytes)} ({annotation})"
        )
    if relay:
        return f"{format_byte_count(billed)} ({relay})"
    return ""
