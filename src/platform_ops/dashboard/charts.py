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
    ),
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
    ),
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
            color=alt.Color(
                "part:N",
                scale=alt.Scale(domain=order, range=list(colors.categorical[: len(order)])),
                legend=alt.Legend(title=None, orient="top"),
            ),
            order=alt.Order("part_order:Q"),
            tooltip=[
                alt.Tooltip(f"{category}:N"),
                alt.Tooltip("part:N"),
                alt.Tooltip("value:Q", format=value_format),
            ],
        )
        .transform_calculate(part_order=f"indexof({order!r}, datum.part)")
        .properties(height=alt.Step(28))
    )
    return chart


def single_bars(
    frame: pd.DataFrame,
    category: str,
    value: str,
    value_format: str,
    colors: Palette,
    value_title: str | None = None,
    order: Sequence[str] | None = None,
) -> alt.Chart:
    """One measure across categories: one hue, sorted by value unless ``order`` is
    given (for an ordered category such as severity), labelled by the axis."""
    sort: str | list[str] = list(order) if order is not None else "-x"
    chart: alt.Chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=BAR_CORNER, color=colors.accent)
        .encode(
            y=alt.Y(f"{category}:N", sort=sort, title=None, axis=alt.Axis(labelLimit=260)),
            x=alt.X(f"{value}:Q", title=value_title, axis=alt.Axis(format=value_format)),
            tooltip=[alt.Tooltip(f"{category}:N"), alt.Tooltip(f"{value}:Q", format=value_format)],
        )
        .properties(height=alt.Step(28))
    )
    return chart


def team_lines(
    frame: pd.DataFrame, x: str, value: str, team: str, scale: alt.Scale, value_format: str
) -> alt.LayerChart:
    """One line per team across ordered periods, 2 px, with markers large enough to hover.

    ``x`` is a label per period, in order (e.g. "2026-04 (5 days)"), so a period
    the window only partly covers says so instead of looking like a drop.
    """
    base = alt.Chart(frame).encode(
        x=alt.X(f"{x}:O", title=None, sort=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y(f"{value}:Q", title=None, axis=alt.Axis(format=value_format)),
        color=alt.Color(f"{team}:N", scale=scale, legend=alt.Legend(title=None, orient="top")),
        tooltip=[
            alt.Tooltip(f"{team}:N"),
            alt.Tooltip(f"{x}:O"),
            alt.Tooltip(f"{value}:Q", format=value_format),
        ],
    )
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
    long = frame.melt(
        id_vars=[category], value_vars=[before, after], var_name="when", value_name="value"
    )
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
            color=alt.Color(
                "when:N",
                scale=alt.Scale(domain=list(labels), range=[colors.before, colors.after]),
                legend=alt.Legend(title=None, orient="top"),
            ),
            tooltip=[
                alt.Tooltip(f"{category}:N"),
                alt.Tooltip("when:N"),
                alt.Tooltip("value:Q", format=".1f"),
            ],
        )
    )
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
            x=alt.X(f"{start}:T", title=None, axis=alt.Axis(format="%b %d")),
            x2=f"{end}:T",
            color=alt.Color(
                f"{level}:N",
                scale=alt.Scale(domain=list(levels), range=ramp),
                legend=alt.Legend(title=None, orient="top"),
            ),
            tooltip=[
                alt.Tooltip(f"{label}:N"),
                alt.Tooltip(f"{level}:N"),
                alt.Tooltip(f"{start}:T", format="%Y-%m-%d %H:%M"),
                alt.Tooltip(f"{end}:T", format="%Y-%m-%d %H:%M"),
                *[alt.Tooltip(t) for t in tooltip],
            ],
        )
        .properties(height=alt.Step(24))
    )
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
    long = frame.melt(
        id_vars=[category], value_vars=[accent, grey], var_name="series", value_name="value"
    )
    long["series"] = long["series"].map({accent: labels[0], grey: labels[1]})
    chart: alt.Chart = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=BAR_CORNER, stroke=None)
        .encode(
            y=alt.Y("series:N", title=None, axis=None, sort=list(labels)),
            x=alt.X("value:Q", title=None, axis=alt.Axis(format=value_format)),
            row=alt.Row(
                f"{category}:N", title=None, header=alt.Header(labelAngle=0, labelAlign="left")
            ),
            color=alt.Color(
                "series:N",
                scale=alt.Scale(domain=list(labels), range=[colors.accent, colors.muted]),
                legend=alt.Legend(title=None, orient="top"),
            ),
            tooltip=[
                alt.Tooltip(f"{category}:N"),
                alt.Tooltip("series:N"),
                alt.Tooltip("value:Q", format=value_format),
            ],
        )
        .properties(height=alt.Step(18))
    )
    return chart


# Status is state, not identity: the fixed status steps, never a categorical
# slot, and always with a glyph and a label beside the colour.
STATUS = {
    "passed": ("#0ca30c", ""),
    "differs, cause upstream": ("#ec835a", "↑"),
    "failed": ("#d03b3b", "✕"),
}


def night_grid(frame: pd.DataFrame, colors: Palette) -> alt.LayerChart:
    """Every night of a run against every compared pair, one cell each.

    ``frame`` has ``night`` (1, 2, ...), ``date``, ``pair``, ``status`` (a key
    of :data:`STATUS`) and ``detail``. A failing cell carries its glyph as well
    as its colour, so the grid reads in grey too.
    """
    statuses = list(STATUS)
    data = frame.assign(glyph=frame["status"].map({k: v[1] for k, v in STATUS.items()}))
    base = alt.Chart(data).encode(
        # Band padding leaves the surface showing between cells, in either theme.
        x=alt.X(
            "night:O",
            title="Night of the parallel run",
            axis=alt.Axis(labelAngle=0, grid=False),
            scale=alt.Scale(paddingInner=0.12),
        ),
        y=alt.Y(
            "pair:N",
            title=None,
            sort=None,
            axis=alt.Axis(labelLimit=320, grid=False),
            scale=alt.Scale(paddingInner=0.12),
        ),
    )
    cells = base.mark_rect(cornerRadius=2, stroke=None).encode(
        color=alt.Color(
            "status:N",
            scale=alt.Scale(domain=statuses, range=[STATUS[s][0] for s in statuses]),
            legend=alt.Legend(title=None, orient="top"),
        ),
        tooltip=[
            alt.Tooltip("night:O"),
            alt.Tooltip("date:N"),
            alt.Tooltip("pair:N"),
            alt.Tooltip("status:N"),
            alt.Tooltip("detail:N"),
        ],
    )
    glyphs = base.mark_text(fontSize=11, color="#ffffff").encode(text="glyph:N")
    layered: alt.LayerChart = (cells + glyphs).properties(height=alt.Step(24))
    return layered


def accent_lines(
    frame: pd.DataFrame,
    x: str,
    accent: str,
    grey: str,
    labels: tuple[str, str],
    colors: Palette,
    log: bool = False,
    value_format: str = ",.0f",
) -> alt.LayerChart:
    """The series that matters in the accent against its baseline in grey, one axis."""
    long = frame.melt(id_vars=[x], value_vars=[accent, grey], var_name="series", value_name="value")
    long["series"] = long["series"].map({accent: labels[0], grey: labels[1]})
    base = alt.Chart(long).encode(
        x=alt.X(f"{x}:O", title="Night of the parallel run", axis=alt.Axis(labelAngle=0)),
        y=alt.Y(
            "value:Q",
            title=None,
            scale=alt.Scale(type="log") if log else alt.Undefined,
            axis=alt.Axis(format=value_format),
        ),
        color=alt.Color(
            "series:N",
            scale=alt.Scale(domain=list(labels), range=[colors.accent, colors.muted]),
            legend=alt.Legend(title=None, orient="top"),
        ),
        tooltip=[
            alt.Tooltip(f"{x}:O"),
            alt.Tooltip("series:N"),
            alt.Tooltip("value:Q", format=value_format),
        ],
    )
    layered: alt.LayerChart = base.mark_line(strokeWidth=LINE_WIDTH) + base.mark_point(
        filled=True, size=POINT_SIZE
    )
    return layered
