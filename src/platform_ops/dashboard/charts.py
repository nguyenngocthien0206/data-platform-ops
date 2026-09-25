"""Chart builders for the dashboards, one place for colour and mark rules.

Colour follows the job, never decoration:

- teams are identity, so each team keeps one categorical slot in the order of
  ``config/teams.yaml`` wherever it appears, whatever a filter removes;
- a single measure (datasets per team, cost per workload) is magnitude, one hue;
- before and after (alerts per person) is two shades of the same hue;
- the one series that is the point (the segmented diff) is the accent, and its
  comparison (a naive copy) is grey;
- severity is ordered, so it is a light-to-dark ramp of one hue.

The categorical slots are the validated reference palette. On Streamlit's white
surface three slots sit below 3:1 contrast, so every chart carries hover
tooltips and the page shows its data table next to it. Dark mode uses the same
hues stepped for a dark surface, validated separately; both checks passed on
Streamlit's surfaces (``#ffffff`` and ``#0e1117``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import altair as alt
import pandas as pd


@dataclass(frozen=True)
class Palette:
    categorical: tuple[str, ...]
    accent: str
    muted: str
    before: str
    after: str
    ordinal: tuple[str, str, str]


LIGHT = Palette(
    categorical=(
        "#2a78d6",
        "#eb6834",
        "#1baf7a",
        "#eda100",
        "#e87ba4",
        "#008300",
        "#4a3aa7",
        "#e34948",
    ),  # fmt: skip
    accent="#2a78d6",
    muted="#898781",
    before="#86b6ef",
    after="#1c5cab",
    ordinal=("#86b6ef", "#2a78d6", "#104281"),
)
DARK = Palette(
    categorical=(
        "#3987e5",
        "#d95926",
        "#199e70",
        "#c98500",
        "#d55181",
        "#008300",
        "#9085e9",
        "#e66767",
    ),  # fmt: skip
    accent="#3987e5",
    muted="#898781",
    before="#184f95",
    after="#86b6ef",
    ordinal=("#184f95", "#3987e5", "#9ec5f4"),
)

BAR_CORNER = 4
LINE_WIDTH = 2
POINT_SIZE = 64  # an 8 px marker
GAP = 2


def palette(theme: str | None) -> Palette:
    return DARK if theme == "dark" else LIGHT


def team_colors(teams: Sequence[str], shown: Sequence[str], colors: Palette) -> alt.Scale:
    """A fixed team-to-colour mapping: a team's colour never depends on a filter."""
    order = list(teams) + sorted(set(shown) - set(teams))
    order = order[: len(colors.categorical)]
    return alt.Scale(domain=order, range=list(colors.categorical[: len(order)]))


def stacked_share(
    frame: pd.DataFrame, category: str, parts: dict[str, str], value_format: str, colors: Palette
) -> alt.Chart:
    """Horizontal stacked bars: one bar per ``category``, one segment per part."""
    long = frame.melt(
        id_vars=[category], value_vars=list(parts), var_name="part", value_name="value"
    )
    long["part"] = long["part"].map(parts)
    order = list(parts.values())
    chart: alt.Chart = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=BAR_CORNER, stroke=None)
        .encode(
            y=alt.Y(f"{category}:N", sort="-x", title=None),
            x=alt.X("sum(value):Q", title=None, axis=alt.Axis(format=value_format)),
            color=alt.Color("part:N", scale=alt.Scale(domain=order,
                            range=list(colors.categorical[: len(order)])),
                            legend=alt.Legend(title=None, orient="top")),
            order=alt.Order("part_order:Q"),
            tooltip=[alt.Tooltip(f"{category}:N"), alt.Tooltip("part:N"),
                     alt.Tooltip("value:Q", format=value_format)],
        )
        .transform_calculate(part_order=f"indexof({order!r}, datum.part)")
        .properties(height=alt.Step(28))
    )  # fmt: skip
    return chart


def single_bars(
    frame: pd.DataFrame,
    category: str,
    value: str,
    value_format: str,
    colors: Palette,
    value_title: str | None = None,
) -> alt.Chart:
    """One measure across categories: one hue, sorted, labelled by the axis."""
    chart: alt.Chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=BAR_CORNER, color=colors.accent)
        .encode(
            y=alt.Y(f"{category}:N", sort="-x", title=None),
            x=alt.X(f"{value}:Q", title=value_title, axis=alt.Axis(format=value_format)),
            tooltip=[alt.Tooltip(f"{category}:N"), alt.Tooltip(f"{value}:Q", format=value_format)],
        )
        .properties(height=alt.Step(28))
    )  # fmt: skip
    return chart


def team_lines(
    frame: pd.DataFrame, x: str, value: str, team: str, scale: alt.Scale, value_format: str
) -> alt.LayerChart:
    """One line per team over time, 2 px, with markers large enough to hover."""
    base = alt.Chart(frame).encode(
        x=alt.X(f"{x}:T", title=None, axis=alt.Axis(format="%b %Y")),
        y=alt.Y(f"{value}:Q", title=None, axis=alt.Axis(format=value_format)),
        color=alt.Color(f"{team}:N", scale=scale, legend=alt.Legend(title=None, orient="top")),
        tooltip=[alt.Tooltip(f"{team}:N"), alt.Tooltip(f"{x}:T", format="%B %Y"),
                 alt.Tooltip(f"{value}:Q", format=value_format)],
    )  # fmt: skip
    layered: alt.LayerChart = base.mark_line(strokeWidth=LINE_WIDTH) + base.mark_point(
        filled=True, size=POINT_SIZE
    )
    return layered


def dumbbell(
    frame: pd.DataFrame,
    category: str,
    before: str,
    after: str,
    labels: tuple[str, str],
    colors: Palette,
) -> alt.LayerChart:
    """Before and after per item: a thin rule between two dots, two shades of one hue."""
    long = frame.melt(id_vars=[category], value_vars=[before, after], var_name="when",
                      value_name="value")  # fmt: skip
    long["when"] = long["when"].map({before: labels[0], after: labels[1]})
    rule = (
        alt.Chart(frame)
        .mark_rule(strokeWidth=LINE_WIDTH, color=colors.muted)
        .encode(y=alt.Y(f"{category}:N", title=None), x=f"{before}:Q", x2=f"{after}:Q")
    )
    dots = (
        alt.Chart(long)
        .mark_point(filled=True, size=POINT_SIZE * 2, opacity=1)
        .encode(
            y=alt.Y(f"{category}:N", title=None),
            x=alt.X("value:Q", title=None),
            color=alt.Color("when:N", scale=alt.Scale(domain=list(labels),
                            range=[colors.before, colors.after]),
                            legend=alt.Legend(title=None, orient="top")),
            tooltip=[alt.Tooltip(f"{category}:N"), alt.Tooltip("when:N"),
                     alt.Tooltip("value:Q", format=".1f")],
        )
    )  # fmt: skip
    layered: alt.LayerChart = (rule + dots).properties(height=alt.Step(28))
    return layered


def spans(
    frame: pd.DataFrame,
    label: str,
    start: str,
    end: str,
    level: str,
    levels: Sequence[str],
    colors: Palette,
    tooltip: Sequence[str] = (),
) -> alt.Chart:
    """Horizontal spans on a time axis, shaded by an ordered level (darkest = most severe)."""
    ramp = list(reversed(colors.ordinal))[: len(levels)]
    chart: alt.Chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadius=BAR_CORNER, height=14)
        .encode(
            y=alt.Y(f"{label}:N", title=None, sort=None),
            x=alt.X(f"{start}:T", title=None),
            x2=f"{end}:T",
            color=alt.Color(f"{level}:N", scale=alt.Scale(domain=list(levels), range=ramp),
                            legend=alt.Legend(title=None, orient="top")),
            tooltip=[alt.Tooltip(f"{label}:N"), alt.Tooltip(f"{level}:N"),
                     alt.Tooltip(f"{start}:T", format="%Y-%m-%d %H:%M"),
                     alt.Tooltip(f"{end}:T", format="%Y-%m-%d %H:%M"),
                     *[alt.Tooltip(t) for t in tooltip]],
        )
        .properties(height=alt.Step(24))
    )  # fmt: skip
    return chart


def accent_against_grey(
    frame: pd.DataFrame,
    category: str,
    accent: str,
    grey: str,
    labels: tuple[str, str],
    colors: Palette,
    value_format: str = ",.0f",
) -> alt.Chart:
    """Two bars per item: the one that matters in the accent, its baseline in grey."""
    long = frame.melt(id_vars=[category], value_vars=[accent, grey], var_name="series",
                      value_name="value")  # fmt: skip
    long["series"] = long["series"].map({accent: labels[0], grey: labels[1]})
    chart: alt.Chart = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=BAR_CORNER, stroke=None)
        .encode(
            y=alt.Y("series:N", title=None, axis=None, sort=list(labels)),
            x=alt.X("value:Q", title=None, axis=alt.Axis(format=value_format)),
            row=alt.Row(f"{category}:N", title=None, header=alt.Header(labelAngle=0,
                        labelAlign="left")),
            color=alt.Color("series:N", scale=alt.Scale(domain=list(labels),
                            range=[colors.accent, colors.muted]),
                            legend=alt.Legend(title=None, orient="top")),
            tooltip=[alt.Tooltip(f"{category}:N"), alt.Tooltip("series:N"),
                     alt.Tooltip("value:Q", format=value_format)],
        )
        .properties(height=alt.Step(18))
    )  # fmt: skip
    return chart


def chart_spec(chart: Any) -> dict[str, Any]:
    """The Vega-Lite spec of a chart, for tests."""
    spec: dict[str, Any] = chart.to_dict()
    return spec
