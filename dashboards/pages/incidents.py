"""Incidents: the alert storm against the incidents it became, and who got paged."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from platform_ops.dashboard import charts, page

st.title("Incident management")
st.caption(
    "Faults injected into raw data over three simulated weeks, detected by real dbt runs, "
    "grouped into one incident per root cause. See `reports/incidents.md`."
)
colors = page.colors()
wh = page.warehouse()

metrics = page.frame("incident_metrics")
if metrics is not None:
    raw = wh.metric(metrics, "raw_alerts") or 0
    opened = wh.metric(metrics, "incidents") or 0
    pages_sent = wh.metric(metrics, "pages") or 0
    right = wh.metric(metrics, "routing_accuracy", "person") or 0
    naive = wh.metric(metrics, "routing_accuracy_naive", "person") or 0
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
        ),  # fmt: skip
        per_person,
    )

    severities = ["SEV1", "SEV2", "SEV3"]
    detect, resolve = st.columns(2)
    for column, name, title in ((detect, "mttd_hours", "Time to detect (hours)"),
                                (resolve, "mttr_hours", "Time to resolve (hours)")):  # fmt: skip
        with column:
            st.subheader(title)
            rows = metrics[(metrics["metric"] == name) & metrics["dimension"].isin(severities)]
            data = rows.rename(columns={"dimension": "severity", "value": "hours"})[
                ["severity", "hours"]
            ]
            page.chart(charts.single_bars(data, "severity", "hours", ",.1f", colors), data)

incidents = page.frame("incidents")
if incidents is not None:
    st.subheader("Timeline")
    page.chart(
        charts.spans(incidents, "incident_id", "opened_at", "resolved_at", "severity",
                     ["SEV1", "SEV2", "SEV3"], colors, tooltip=["owner:N", "root_node:N"]),
        incidents,
        label="Incidents",
    )  # fmt: skip

kinds = page.frame("check_events_by_kind")
runs = page.frame("incident_runs")
left, right = st.columns(2)
if kinds is not None:
    with left:
        st.subheader("Failing checks by kind")
        st.dataframe(kinds, hide_index=True, width="stretch")
if runs is not None:
    with right:
        st.subheader("Nights dbt ran")
        st.dataframe(runs, hide_index=True, width="stretch")
