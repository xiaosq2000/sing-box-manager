"""Tests for server-rendered traffic charts."""

from __future__ import annotations

import pathlib
from datetime import UTC, date, datetime

import pytest
from lxml import etree

import sing_box_manager.web.charts as charts
from sing_box_manager.traffic_stats import (
    UNATTRIBUTED_DOMAIN_KEY,
    DailyUsagePoint,
    DailyUsageSeries,
    DomainUsage,
    DomainUsageBreakdown,
    MonthlyUsageHistory,
    MonthlyUsagePoint,
    ProtocolUsage,
    ProtocolUsageBreakdown,
    UserTrafficSummary,
)

SVG_NS = "http://www.w3.org/2000/svg"
GIB = 1024**3


def _summaries() -> tuple[UserTrafficSummary, ...]:
    return tuple(
        UserTrafficSummary(
            username=username,
            cycle_month="2026-08",
            cycle_start=date(2026, 8, 1),
            cycle_end=date(2026, 8, 31),
            timezone="Asia/Shanghai",
            upload_bytes=upload,
            download_bytes=download,
            updated_at=datetime(2026, 8, 3, tzinfo=UTC),
        )
        for username, upload, download in (
            ("alice", 3 * GIB, 9 * GIB),
            ("bob", 2 * GIB, 5 * GIB),
            ("carol", 1 * GIB, 2 * GIB),
        )
    )


def _series() -> DailyUsageSeries:
    return DailyUsageSeries(
        username="alice",
        start_date=date(2026, 7, 30),
        end_date=date(2026, 8, 3),
        timezone="Asia/Shanghai",
        points=tuple(
            DailyUsagePoint(
                usage_date=day,
                upload_bytes=upload,
                download_bytes=download,
            )
            for day, upload, download in (
                (date(2026, 7, 30), 1 * GIB, 2 * GIB),
                (date(2026, 7, 31), 0, 0),
                (date(2026, 8, 1), 3 * GIB, 4 * GIB),
                (date(2026, 8, 2), 2 * GIB, 1 * GIB),
                (date(2026, 8, 3), 5 * GIB, 6 * GIB),
            )
        ),
    )


def _breakdown() -> ProtocolUsageBreakdown:
    return ProtocolUsageBreakdown(
        username="alice",
        cycle_month="2026-08",
        timezone="Asia/Shanghai",
        protocols=(
            ProtocolUsage(protocol="trojan", upload_bytes=3 * GIB, download_bytes=6 * GIB),
            ProtocolUsage(protocol="hysteria2", upload_bytes=1 * GIB, download_bytes=2 * GIB),
            ProtocolUsage(protocol="naive", upload_bytes=0, download_bytes=0),
        ),
    )


def _history() -> MonthlyUsageHistory:
    return MonthlyUsageHistory(
        username="alice",
        timezone="Asia/Shanghai",
        months=(
            MonthlyUsagePoint(cycle_month="2026-06", upload_bytes=4 * GIB, download_bytes=8 * GIB),
            MonthlyUsagePoint(cycle_month="2026-07", upload_bytes=0, download_bytes=0),
            MonthlyUsagePoint(cycle_month="2026-08", upload_bytes=6 * GIB, download_bytes=9 * GIB),
        ),
    )


def _all_rendered() -> dict[str, str]:
    return {
        "ranking": charts.render_user_ranking_chart(_summaries()),
        "trend": charts.render_daily_trend_chart(_series()),
        "protocols": charts.render_protocol_split_chart(_breakdown()),
        "months": charts.render_monthly_history_chart(_history()),
    }


@pytest.mark.parametrize("name", ["ranking", "trend", "protocols", "months"])
def test_charts_have_no_xml_prolog_and_no_script(name: str) -> None:
    """A chart that needs a CDN is a chart that does not draw behind the GFW."""
    svg = _all_rendered()[name]

    assert not svg.lstrip().startswith("<?xml")
    assert "<script" not in svg
    assert "kozea" not in svg
    assert "xml-stylesheet" not in svg


@pytest.mark.parametrize("name", ["ranking", "trend", "protocols", "months"])
def test_charts_parse_as_responsive_svg_fragments(name: str) -> None:
    root = etree.fromstring(_all_rendered()[name].encode("utf-8"))

    assert root.tag == f"{{{SVG_NS}}}svg"
    assert charts.CHART_CLASS in (root.get("class") or "")
    assert root.get("viewBox")
    # width/height would pin the chart and stop it shrinking on a phone.
    assert root.get("width") is None
    assert root.get("height") is None


@pytest.mark.parametrize("name", ["ranking", "trend", "protocols", "months"])
def test_charts_reference_only_relative_urls(name: str) -> None:
    root = etree.fromstring(_all_rendered()[name].encode("utf-8"))

    hrefs = [
        value
        for element in root.iter()
        for key, value in element.attrib.items()
        if key.endswith("href")
    ]

    assert all(href.startswith("/") for href in hrefs), hrefs


def test_series_use_stable_color_classes() -> None:
    """styles.css themes charts through these classes; a pygal bump must not move them."""
    svg = charts.render_daily_trend_chart(_series())

    assert "color-0" in svg
    assert "color-1" in svg


@pytest.mark.parametrize(
    ("renderer", "dataset"),
    [
        (charts.render_user_ranking_chart, _summaries()),
        (charts.render_protocol_split_chart, _breakdown()),
    ],
)
def test_printed_values_stay_in_the_text_overlay(renderer, dataset) -> None:
    """styles.css repaints these; pygal hard-codes them black, invisible in dark mode."""
    root = etree.fromstring(renderer(dataset).encode("utf-8"))

    labelled = [
        node
        for node in root.iter(f"{{{SVG_NS}}}text")
        if node.get("class") == "value" and node.text and "iB" in node.text
    ]

    assert labelled, "no printed value labels found"
    for node in labelled:
        ancestors = " ".join(
            (parent.get("class") or "") for parent in node.iterancestors()
        )
        assert "text-overlay" in ancestors


def test_stylesheet_repaints_printed_values() -> None:
    """The chart CSS has to target where the labels actually are."""
    css = (
        pathlib.Path(charts.__file__).parent / "static" / "styles.css"
    ).read_text(encoding="utf-8")

    assert ".sbm-chart .text-overlay text" in css
    assert "paint-order: stroke fill !important" in css


def test_ranking_chart_links_to_each_user_detail_page() -> None:
    svg = charts.render_user_ranking_chart(
        _summaries(),
        detail_url_for=lambda username: f"/admin/users/{username}",
    )

    for username in ("alice", "bob", "carol"):
        assert f"/admin/users/{username}" in svg


def test_ranking_chart_puts_the_heaviest_user_on_top() -> None:
    """pygal draws x_labels[0] at the bottom, so the sort has to be inverted."""
    root = etree.fromstring(
        charts.render_user_ranking_chart(_summaries()).encode("utf-8")
    )

    positions = {
        node.text: float(node.get("y"))
        for node in root.iter(f"{{{SVG_NS}}}text")
        if node.text in {"alice", "bob", "carol"} and node.get("y")
    }

    assert positions["alice"] < positions["bob"] < positions["carol"]


def test_value_descriptions_become_native_titles() -> None:
    """Hover values without a line of JavaScript."""
    root = etree.fromstring(
        charts.render_daily_trend_chart(_series()).encode("utf-8")
    )

    titles = [node.text or "" for node in root.iter(f"{{{SVG_NS}}}title")]

    assert any("GiB" in title for title in titles)
    assert any(charts.UPLOAD_LABEL in title for title in titles)
    # The root <title>Pygal</title> branding must not survive.
    assert "Pygal" not in titles


@pytest.mark.parametrize(
    ("renderer", "empty", "all_zero"),
    [
        (
            charts.render_user_ranking_chart,
            (),
            tuple(
                UserTrafficSummary(
                    username=name,
                    cycle_month="2026-08",
                    cycle_start=date(2026, 8, 1),
                    cycle_end=date(2026, 8, 31),
                    timezone="Asia/Shanghai",
                )
                for name in ("alice", "bob")
            ),
        ),
        (
            charts.render_daily_trend_chart,
            DailyUsageSeries(
                username="alice",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 1),
                timezone="Asia/Shanghai",
            ),
            DailyUsageSeries(
                username="alice",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 2),
                timezone="Asia/Shanghai",
                points=(
                    DailyUsagePoint(usage_date=date(2026, 8, 1)),
                    DailyUsagePoint(usage_date=date(2026, 8, 2)),
                ),
            ),
        ),
        (
            charts.render_protocol_split_chart,
            ProtocolUsageBreakdown(
                username="alice", cycle_month="2026-08", timezone="Asia/Shanghai"
            ),
            ProtocolUsageBreakdown(
                username="alice",
                cycle_month="2026-08",
                timezone="Asia/Shanghai",
                protocols=(
                    ProtocolUsage(protocol="trojan"),
                    ProtocolUsage(protocol="naive"),
                ),
            ),
        ),
        (
            charts.render_monthly_history_chart,
            MonthlyUsageHistory(username="alice", timezone="Asia/Shanghai"),
            MonthlyUsageHistory(
                username="alice",
                timezone="Asia/Shanghai",
                months=(
                    MonthlyUsagePoint(cycle_month="2026-07"),
                    MonthlyUsagePoint(cycle_month="2026-08"),
                ),
            ),
        ),
    ],
)
def test_empty_and_all_zero_datasets_render_a_placeholder(
    renderer, empty, all_zero
) -> None:
    """All-zero is not 'no data' to pygal; it draws a degenerate zero-to-zero axis."""
    for dataset in (empty, all_zero):
        svg = renderer(dataset)

        assert f"{charts.CHART_CLASS}--empty" in svg
        assert charts.EMPTY_CHART_LABEL in svg
        etree.fromstring(svg.encode("utf-8"))


def test_charts_degrade_when_pygal_is_unavailable(monkeypatch) -> None:
    """An interrupted deploy ships code before the environment that supports it."""
    monkeypatch.setattr(charts, "_pygal", lambda: None)

    rendered = (
        charts.render_user_ranking_chart(_summaries()),
        charts.render_daily_trend_chart(_series()),
        charts.render_protocol_split_chart(_breakdown()),
        charts.render_monthly_history_chart(_history()),
    )

    for svg in rendered:
        assert f"{charts.CHART_CLASS}--empty" in svg


def _domain_breakdown(
    domains: tuple[charts.DomainUsage, ...] | None = None,
) -> DomainUsageBreakdown:
    rows = domains if domains is not None else (
        DomainUsage("example.com", 1 * GIB, 3 * GIB, 12),
        DomainUsage("wikipedia.org", 512, 1024, 3),
        DomainUsage(UNATTRIBUTED_DOMAIN_KEY, 256, 512, 0),
    )
    return DomainUsageBreakdown(
        username=None,
        timezone="Asia/Shanghai",
        days=30,
        domains=rows,
        window_total_bytes=sum(row.total_bytes for row in rows),
    )


def test_domain_chart_renders_inline_svg_without_scripts() -> None:
    """Same guarantee as every other chart: no bundle, no CDN, no XML prolog."""
    svg = charts.render_domain_ranking_chart(_domain_breakdown())

    assert 'class="sbm-chart' in svg
    assert "<script" not in svg
    assert "<?xml" not in svg
    assert "kozea" not in svg


def test_domain_chart_spells_out_the_bookkeeping_buckets() -> None:
    svg = charts.render_domain_ranking_chart(_domain_breakdown())

    assert "未归属" in svg
    assert UNATTRIBUTED_DOMAIN_KEY not in svg


def test_domain_chart_puts_the_heaviest_domain_on_top() -> None:
    root = etree.fromstring(
        charts.render_domain_ranking_chart(_domain_breakdown()).encode("utf-8")
    )

    positions = {
        node.text: float(node.get("y"))
        for node in root.iter(f"{{{SVG_NS}}}text")
        if node.text in {"example.com", "wikipedia.org"} and node.get("y")
    }

    assert positions["example.com"] < positions["wikipedia.org"]


def test_domain_chart_is_empty_without_data() -> None:
    svg = charts.render_domain_ranking_chart(_domain_breakdown(domains=()))

    assert charts.EMPTY_CHART_LABEL in svg
