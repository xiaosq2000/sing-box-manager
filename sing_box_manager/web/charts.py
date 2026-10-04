"""Traffic charts, rendered to inline SVG on the server.

This portal has no JavaScript toolchain and references no external script,
because the people it serves sit behind network interference and a chart that
needs a CDN is a chart that does not draw. So charts are built here, in Python,
and embedded directly into the page markup.

Embedding inline rather than linking an ``<img>`` is what makes theming work:
an inline ``<svg>`` lives in the document, so it inherits the Rose Pine custom
properties from ``:root`` and the existing theme toggle recolours every chart
without a line of JavaScript. That is also why the colours live in
``styles.css`` keyed off pygal's ``.color-N`` classes rather than being baked
into the markup here -- see ``_finalize``.

Every pygal call in the repository is in this module, behind four functions
that return strings. If pygal ever needs replacing, this is the only file that
knows it exists.
"""

from __future__ import annotations

import importlib
import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from sing_box_manager.traffic_stats import (
    NO_DOMAIN_KEY,
    OTHER_DOMAIN_KEY,
    UNATTRIBUTED_DOMAIN_KEY,
)
from sing_box_manager.web.formatting import format_byte_count

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sing_box_manager.traffic_stats import (
        DailyUsageSeries,
        DomainUsageBreakdown,
        MonthlyUsageHistory,
        ProtocolUsageBreakdown,
        UserTrafficSummary,
    )

logger = logging.getLogger(__name__)

CHART_CLASS = "sbm-chart"
EMPTY_CHART_LABEL = "尚未采集"

UPLOAD_LABEL = "上传"
DOWNLOAD_LABEL = "下载"

_SVG_NS = "http://www.w3.org/2000/svg"

# Pre-CSS fallbacks only. styles.css overrides every one of these through the
# .color-N classes, which is what lets the theme toggle recolour the charts;
# these values just keep a chart legible if the stylesheet fails to load.
# Deliberately not --pine: #31748f on the dark #191724 base is too low-contrast.
_CHART_COLORS = (
    "#56949f",  # --foam, upload / trojan
    "#907aa9",  # --iris, download / hysteria2
    "#ea9d34",  # --gold, naive
    "#b4637a",  # --love, spare
)
_FONT_STACK = (
    '"Maple Mono NF CN", ui-monospace, "SFMono-Regular", Menlo, Monaco, '
    "Consolas, monospace"
)
_CHART_WIDTH = 520
_CHART_HEIGHT = 300

# Enough rows to cover this portal's user list without the bars turning into
# hairlines. Anything past this is dropped and reported by the caller.
DEFAULT_RANKING_LIMIT = 12


@lru_cache(maxsize=1)
def _pygal() -> Any | None:
    """Import pygal once, or ``None`` when it is unavailable.

    A deploy rsyncs code before ``pixi install --locked`` provisions the
    environment, so an interrupted one can leave a portal whose code knows about
    pygal and whose environment does not. Degrading to a chartless page beats
    500-ing every admin request until someone notices.
    """
    try:
        return cast("Any", importlib.import_module("pygal"))
    except ImportError:
        logger.warning(
            "pygal is unavailable; traffic charts will render as placeholders",
            exc_info=True,
        )
        return None


@lru_cache(maxsize=1)
def _chart_style() -> Any:
    style_module = cast("Any", importlib.import_module("pygal.style"))
    return style_module.Style(
        background="transparent",
        plot_background="transparent",
        foreground="#797593",
        foreground_strong="#575279",
        foreground_subtle="#9893a5",
        opacity=".85",
        opacity_hover=".95",
        transition="0ms",
        colors=_CHART_COLORS,
        font_family=_FONT_STACK,
        label_font_size=11,
        major_label_font_size=11,
        value_font_size=11,
        legend_font_size=11,
        title_font_size=13,
    )


def _base_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "style": _chart_style(),
        # pygal defaults to loading its tooltip script off kozea.github.io.
        "js": (),
        "disable_xml_declaration": True,
        "no_data_text": EMPTY_CHART_LABEL,
        "show_legend": True,
        "legend_at_bottom": True,
        "truncate_legend": -1,
        "margin": 8,
        "width": _CHART_WIDTH,
        "height": _CHART_HEIGHT,
        "value_formatter": format_byte_count,
        "print_values": False,
    }
    config.update(overrides)
    return config


def _empty_chart_svg(label: str = EMPTY_CHART_LABEL) -> str:
    """A placeholder that never touches pygal.

    Covers the case pygal's own ``no_data_text`` does not: series that exist but
    are all zero are not "no data" to pygal, and it renders them as a degenerate
    axis with a zero-to-zero range.
    """
    return (
        f'<svg xmlns="{_SVG_NS}" class="{CHART_CLASS} {CHART_CLASS}--empty" '
        f'viewBox="0 0 400 120" role="img" aria-label="{label}">'
        f'<text x="200" y="66" text-anchor="middle" '
        f'class="{CHART_CLASS}-empty-label">{label}</text></svg>'
    )


def _promote_value_descriptions(root: Any, series_names: Sequence[str]) -> None:
    """Turn pygal's ``<desc class="value">`` nodes into native ``<title>`` tips.

    pygal writes the formatted value into a ``<desc>`` for its JavaScript to
    read on hover. We dropped that JavaScript, but the browser will show a
    ``<title>`` as a tooltip for free -- so copy the text across and get hover
    values back with no script at all.

    Everything here is gated on the nodes actually being found, so a pygal
    upgrade that reshapes them costs us tooltips rather than an exception.
    """
    etree = cast("Any", importlib.import_module("lxml.etree"))
    for group in root.iter(f"{{{_SVG_NS}}}g"):
        descriptions = {
            (child.get("class") or ""): (child.text or "")
            for child in group
            if child.tag == f"{{{_SVG_NS}}}desc"
        }
        value = descriptions.get("value")
        if not value:
            continue

        parts = [
            descriptions.get("x_label", "").strip(),
            _series_name(group, series_names),
        ]
        prefix = " · ".join(part for part in parts if part)
        title = etree.SubElement(group, f"{{{_SVG_NS}}}title")
        title.text = f"{prefix} — {value}" if prefix else value
        # SVG shows the first <title> child, so it has to lead the group.
        group.insert(0, title)


def _series_name(group: Any, series_names: Sequence[str]) -> str:
    for ancestor in group.iterancestors():
        classes = (ancestor.get("class") or "").split()
        for token in classes:
            if token.startswith("serie-"):
                index = token.removeprefix("serie-")
                if index.isdigit() and int(index) < len(series_names):
                    return series_names[int(index)]
                return ""
    return ""


def _finalize(
    svg_markup: str,
    *,
    series_names: Sequence[str] = (),
    title: str = "",
) -> str:
    """Own every byte of emitted markup.

    Four jobs: strip the inline ``<script>`` pygal writes even when its JS is
    disabled (a few KB of config for a script that is not there); make the SVG
    fluid; stamp the class ``styles.css`` keys its theme colours off; and
    promote value descriptions to native tooltips.
    """
    etree = cast("Any", importlib.import_module("lxml.etree"))
    root = etree.fromstring(svg_markup.encode("utf-8"))

    for script in list(root.iter(f"{{{_SVG_NS}}}script")):
        script.getparent().remove(script)

    width, height = root.get("width"), root.get("height")
    if root.get("viewBox") is None and width and height:
        root.set("viewBox", f"0 0 {width} {height}")
    root.attrib.pop("width", None)
    root.attrib.pop("height", None)
    root.set("class", CHART_CLASS)

    # pygal titles the root "Pygal", which browsers surface as a tooltip over
    # the whole chart. Replace it with something true, or drop it.
    for node in list(root.iter(f"{{{_SVG_NS}}}title")):
        if node.getparent() is root:
            if title:
                node.text = title
            else:
                root.remove(node)
            break
    else:
        if title:
            node = etree.SubElement(root, f"{{{_SVG_NS}}}title")
            node.text = title
            root.insert(0, node)

    _promote_value_descriptions(root, series_names)
    return cast("str", etree.tostring(root, encoding="unicode"))


class RankingRow(NamedTuple):
    """One bar of a ranking chart."""

    label: str
    upload_bytes: int
    download_bytes: int
    # Extra pygal point keys, such as an ``xlink`` that makes the bar a link.
    extras: dict[str, Any] | None = None

    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes


def _render_ranking_chart(
    rows: Sequence[RankingRow],
    *,
    title: str,
    limit: int,
) -> str:
    """One horizontal upload/download ranking, shared by every such chart.

    Everything past the labels and the ordering is identical between the
    rankings, and a second copy of it drifts: the empty-data guard, the pygal
    fallback, the bar height and the label inversion all have to agree or two
    charts on the same page stop looking like one thing.
    """
    ranked = sorted(rows, key=lambda row: row.total_bytes, reverse=True)[:limit]
    if not ranked or all(row.total_bytes == 0 for row in ranked):
        return _empty_chart_svg()

    pygal = _pygal()
    if pygal is None:
        return _empty_chart_svg()

    # pygal draws x_labels[0] at the bottom of a horizontal bar chart, so the
    # heaviest row has to go in last to come out on top.
    ranked = list(reversed(ranked))
    chart = pygal.HorizontalStackedBar(
        **_base_config(print_values=True, height=40 * len(ranked) + 72)
    )
    chart.x_labels = [row.label for row in ranked]

    def points(pick: Callable[[RankingRow], int]) -> list[dict[str, Any]]:
        return [{"value": pick(row), **(row.extras or {})} for row in ranked]

    chart.add(UPLOAD_LABEL, points(lambda row: row.upload_bytes))
    chart.add(DOWNLOAD_LABEL, points(lambda row: row.download_bytes))
    return _finalize(
        chart.render(),
        series_names=(UPLOAD_LABEL, DOWNLOAD_LABEL),
        title=title,
    )


def render_user_ranking_chart(
    summaries: Sequence[UserTrafficSummary],
    *,
    detail_url_for: Callable[[str], str] | None = None,
    limit: int = DEFAULT_RANKING_LIMIT,
) -> str:
    """Users ranked by total traffic this cycle, heaviest first."""
    return _render_ranking_chart(
        [
            RankingRow(
                label=summary.username,
                upload_bytes=summary.upload_bytes,
                download_bytes=summary.download_bytes,
                extras=None
                if detail_url_for is None
                else {
                    "xlink": {
                        "href": detail_url_for(summary.username),
                        "target": "_top",
                    }
                },
            )
            for summary in summaries
        ],
        title="用户流量排行",
        limit=limit,
    )


DOMAIN_LABELS = {
    OTHER_DOMAIN_KEY: "其他域名",
    UNATTRIBUTED_DOMAIN_KEY: "未归属",
    NO_DOMAIN_KEY: "直连 IP",
}


def domain_display_name(domain: str) -> str:
    """Label a domain row, spelling out the two bookkeeping buckets.

    Left raw they read as bizarre hostnames; `__unattributed__` in particular is
    a signal that the stream daemon missed traffic, not a site anyone visited.
    """
    return DOMAIN_LABELS.get(domain, domain)


def render_domain_ranking_chart(
    breakdown: DomainUsageBreakdown,
    *,
    limit: int = DEFAULT_RANKING_LIMIT,
) -> str:
    """Destinations ranked by traffic over the window, heaviest first."""
    return _render_ranking_chart(
        [
            RankingRow(
                label=domain_display_name(usage.domain),
                upload_bytes=usage.upload_bytes,
                download_bytes=usage.download_bytes,
            )
            for usage in breakdown.domains
        ],
        title="域名流量排行",
        limit=limit,
    )


def render_daily_trend_chart(series: DailyUsageSeries) -> str:
    """Upload and download stacked by day, so the total reads as one volume."""
    points = series.points
    if not points or all(point.total_bytes == 0 for point in points):
        return _empty_chart_svg()

    pygal = _pygal()
    if pygal is None:
        return _empty_chart_svg()

    chart = pygal.StackedLine(
        **_base_config(
            fill=True,
            # The dots carry the value descriptions _finalize turns into
            # tooltips, so they have to stay -- just small enough to read as
            # texture on a 90-day window rather than beads on a string.
            show_dots=True,
            dots_size=1.2,
            x_label_rotation=30,
            show_minor_x_labels=False,
        )
    )
    chart.x_labels = [point.usage_date.isoformat() for point in points]
    # Roughly six dates, whether the window is a week or a quarter.
    chart.x_labels_major_every = max(1, len(points) // 6)
    chart.add(UPLOAD_LABEL, [point.upload_bytes for point in points])
    chart.add(DOWNLOAD_LABEL, [point.download_bytes for point in points])
    return _finalize(
        chart.render(),
        series_names=(UPLOAD_LABEL, DOWNLOAD_LABEL),
        title="每日流量趋势",
    )


def render_protocol_split_chart(breakdown: ProtocolUsageBreakdown) -> str:
    """Share of this cycle's traffic carried by each protocol."""
    protocols = breakdown.protocols
    if not protocols or all(usage.total_bytes == 0 for usage in protocols):
        return _empty_chart_svg()

    pygal = _pygal()
    if pygal is None:
        return _empty_chart_svg()

    chart = pygal.Pie(**_base_config(inner_radius=0.6, print_values=True))
    names = [usage.protocol for usage in protocols]
    for usage in protocols:
        chart.add(usage.protocol, usage.total_bytes)
    return _finalize(chart.render(), series_names=names, title="协议流量分布")


def render_monthly_history_chart(history: MonthlyUsageHistory) -> str:
    """Month-over-month totals, oldest first."""
    months = history.months
    if not months or all(point.total_bytes == 0 for point in months):
        return _empty_chart_svg()

    pygal = _pygal()
    if pygal is None:
        return _empty_chart_svg()

    chart = pygal.StackedBar(**_base_config(x_label_rotation=30))
    chart.x_labels = [point.cycle_month for point in months]
    chart.add(UPLOAD_LABEL, [point.upload_bytes for point in months])
    chart.add(DOWNLOAD_LABEL, [point.download_bytes for point in months])
    return _finalize(
        chart.render(),
        series_names=(UPLOAD_LABEL, DOWNLOAD_LABEL),
        title="月度流量对比",
    )


__all__ = [
    "CHART_CLASS",
    "DEFAULT_RANKING_LIMIT",
    "EMPTY_CHART_LABEL",
    "render_daily_trend_chart",
    "render_monthly_history_chart",
    "render_protocol_split_chart",
    "render_user_ranking_chart",
]
