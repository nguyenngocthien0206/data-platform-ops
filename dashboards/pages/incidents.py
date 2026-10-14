"""Incidents: the alert storm against the incidents it became, and who got paged."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from platform_ops.dashboard import charts, page

st.title("Incident management")
st.caption(
    "The company's weeks in production on ClickHouse: faults happened in operation, every night's "
    "dbt invocations were ingested as they finished, and failing checks became one incident per "
    "root cause, paged to whoever can fix it. The full report is `reports/incidents.md`."
)
colors = page.colors()


def value(frame: pd.DataFrame, metric: str, dimension: str = "all") -> float:
    rows = frame[(frame["metric"] == metric) & (frame["dimension"] == dimension)]["value"]
    return float(rows.iloc[0]) if not rows.empty else 0.0


metrics = page.frame("incident_metrics")
if metrics is not None:
    raw = value(metrics, "raw_alerts")
    opened = value(metrics, "incidents")
    pages_sent = value(metrics, "pages")
    right = value(metrics, "routing_accuracy", "person")
    naive = value(metrics, "routing_accuracy_naive", "person")
    a, b, c, d = st.columns(4)
    a.metric("Failing checks", f"{raw:,.0f}")
    b.metric("Incidents", f"{opened:,.0f}")
    c.metric("Pages avoided", f"{1 - pages_sent / raw if raw else 0:.0%}")
    d.metric("Paged the right person", f"{right:.0%}", f"{right - naive:+.0%} vs the root's owner")

    st.subheader("Alerts per person per week")
    st.caption(
        "Before grouping, every failing check pages the owner of the node it is about. After, "
        "each incident pages once, and staging failures page the owner of the source."
    )
    before = metrics[metrics["metric"] == "alerts_per_week_before"]
    after = metrics[metrics["metric"] == "alerts_per_week_after"]
    per_person = pd.merge(
        before[["dimension", "value"]].rename(columns={"dimension": "person", "value": "before"}),
        after[["dimension", "value"]].rename(columns={"dimension": "person", "value": "after"}),
        on="person",
        how="outer",
    ).fillna(0)
    page.chart(
        charts.dumbbell(
            per_person, "person", "before", "after", ("Before grouping", "After"), colors
        ),
        per_person,
    )

    severities = ["SEV1", "SEV2", "SEV3"]
    detect, resolve = st.columns(2)
    for column, name, title in (
        (detect, "mttd_hours", "Time to detect (hours)"),
        (resolve, "mttr_hours", "Time to resolve (hours)"),
    ):
        with column:
            st.subheader(title)
            rows = metrics[(metrics["metric"] == name) & metrics["dimension"].isin(severities)]
            data = rows.rename(columns={"dimension": "severity", "value": "hours"})[
                ["severity", "hours"]
            ]
            page.chart(
                charts.single_bars(data, "severity", "hours", ",.1f", colors, order=severities),
                data,
            )

incidents = page.frame("incidents")
nights = page.frame("incident_nights")
if incidents is not None and not incidents.empty:
    st.subheader("Timeline")
    st.caption(
        "From the run that opened each incident to the run that found its root healthy again. "
        "An incident still open runs to the last night."
    )
    last = nights["detected_at"].max() if nights is not None and not nights.empty else None
    timeline = incidents.assign(
        until=incidents["resolved_at"].fillna(last if last is not None else incidents["opened_at"]),
        state=incidents["resolved_at"].map(lambda r: "resolved" if pd.notna(r) else "open"),
    )
    page.chart(
        charts.spans(
            timeline,
            "incident_id",
            "opened_at",
            "until",
            "severity",
            ["SEV1", "SEV2", "SEV3"],
            colors,
            tooltip=["owner:N", "root:N", "state:N", "fault_id:N"],
        ),
        incidents,
        label="Incidents",
    )

kinds = page.frame("incident_checks")
left, right = st.columns(2)
if kinds is not None:
    with left:
        st.subheader("Failing checks by kind")
        page.table(kinds, counts=("failing_checks", "incidents"))
if nights is not None:
    with right:
        st.subheader("Production nights")
        page.table(nights, counts=("invocations", "checks_run"))
