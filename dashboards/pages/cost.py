"""Cost: the cluster's bill shared out by team, the migration's business case, what to change."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from platform_ops.dashboard import charts, page
from platform_ops.dashboard.queries import metric

MODELS = {
    "cluster": "ClickHouse (stated cost)",
    "scan": "BigQuery on demand",
    "compute": "Snowflake warehouses",
}
KINDS = {
    "production": "Production",
    "consumption": "Consumption",
    "migration": "Migration",
    "storage": "Storage",
}
WORKLOADS = {
    "dbt": "dbt builds and tests",
    "ingestion": "ingestion from Postgres",
    "maintenance": "nightly merge of raw",
    "reconcile": "parallel run reconciliation",
    "dashboard": "dashboards (Metabase)",
    "adhoc": "ad hoc SQL",
}

st.title("Cost and FinOps")
st.caption(
    "The self-managed cluster's stated monthly cost, shared out by the bytes each query read "
    "and the size of each table; the same workload priced on BigQuery and Snowflake. Building "
    "a table is production cost for its owner, reading it is consumption cost for the reader. "
    "The full report is `reports/cost.md`."
)
colors = page.colors()

model = (
    st.segmented_control(
        "Priced as", list(MODELS), default="cluster", format_func=MODELS.get, key="model"
    )
    or "cluster"
)

showback = page.frame("showback")
months = page.frame("cost_months")
if showback is not None:
    chosen = showback[showback["model"] == model]
    total = chosen["usd"].sum()
    by_kind = chosen.groupby("cost_kind")["usd"].sum()
    kinds = [kind for kind in KINDS if kind in by_kind.index]
    columns = st.columns(1 + len(kinds))
    columns[0].metric("Total", page.usd(total))
    for column, kind in zip(columns[1:], kinds, strict=True):
        column.metric(KINDS[kind], page.usd(float(by_kind[kind])))
    if model == "cluster" and months is not None:
        page.caption(
            f"Stated cost over the window: {page.usd(months['stated_usd'].sum())}; "
            f"shared out: {page.usd(months['shared_usd'].sum())}."
        )

    st.subheader("Showback by team")
    wide = (
        chosen.pivot_table(index="team", columns="cost_kind", values="usd", aggfunc="sum")
        .fillna(0.0)
        .reset_index()
    )
    parts = {kind: label for kind, label in KINDS.items() if kind in wide.columns}
    page.chart(charts.stacked_share(wide, "team", parts, "$,.2f", colors), wide)

    st.subheader("By month")
    labels = (
        {row.month: f"{row.month} ({row.days} days)" for row in months.itertuples()}
        if months is not None
        else {}
    )
    monthly = chosen.groupby(["month", "team"], as_index=False)["usd"].sum()
    monthly["period"] = monthly["month"].map(lambda m: labels.get(m, m))
    scale = charts.team_colors(page.teams(), list(monthly["team"].unique()), colors)
    page.chart(
        charts.team_lines(monthly, "period", "usd", "team", scale, "$,.2f"),
        monthly.drop(columns=["period"]),
    )

workload = page.frame("workload")
if showback is not None and workload is not None:
    st.subheader("By workload")
    spend = showback[showback["model"] == model].groupby("actor_type", as_index=False)["usd"].sum()
    table = workload.merge(spend, on="actor_type", how="left").fillna({"usd": 0.0})
    table["workload"] = table["actor_type"].map(lambda a: WORKLOADS.get(a, a))
    table["read"] = table["bytes_read"].map(page.size)
    page.chart(
        charts.single_bars(table, "workload", "usd", "$,.2f", colors, MODELS[model]),
        table[["workload", "queries", "read", "usd"]],
    )

st.subheader("The migration's business case")
case = page.frame("business_case")
summary = page.frame("business_summary")
if case is not None and summary is not None and not summary.empty:
    cutover = metric(summary, "cutover")
    st.caption(
        "The legacy Postgres stays as the operational database; the migration retires its "
        "reporting replica at cutover"
        + (f" ({cutover:%Y-%m-%d})" if pd.notna(cutover) else " (not recommended yet)")
        + ". ClickHouse runs for the whole window."
    )
    a, b, c, d = st.columns(4)
    a.metric("Reporting replica", f"{page.usd(metric(summary, 'legacy_monthly_usd'))}/month")
    b.metric("ClickHouse", f"{page.usd(metric(summary, 'clickhouse_monthly_usd'))}/month")
    c.metric("Overlap until cutover", page.usd(metric(summary, "overlap_usd")))
    payback = metric(summary, "payback_months")
    d.metric("Payback", f"{payback:.1f} months" if pd.notna(payback) else "none")
    page.chart(
        charts.accent_against_grey(
            case,
            "month",
            "clickhouse_usd",
            "legacy_usd",
            ("ClickHouse", "Legacy replica"),
            colors,
            "$,.2f",
        ),
        case,
    )

st.subheader("What to change")
st.caption("Suggestions from the measured workload. Nothing here is applied.")
unused_tab, hotspot_tab, incremental_tab, quota_tab = st.tabs(
    ["Unused tables", "Full-scan hotspots", "Incremental candidates", "Quotas"]
)
with unused_tab:
    unused = page.frame("unused")
    if unused is not None:
        lookbacks = sorted(unused["lookback_days"].unique()) or [30, 90]
        lookback = st.radio(
            "Unused for at least", lookbacks, format_func=lambda d: f"{d} days", horizontal=True
        )
        mine = unused[unused["lookback_days"] == lookback].drop(columns=["lookback_days"])
        page.caption(
            f"{len(mine)} tables nobody read, directly or downstream: "
            f"{page.usd(mine['cluster_usd_month'].sum())} a month of the cluster."
        )
        page.table(mine, cents=("cluster_usd_month", "scan_usd_month", "compute_usd_month"))
with hotspot_tab:
    hotspots = page.frame("hotspots")
    if hotspots is not None:
        st.caption("Filtered reads of a large table that still read nearly all of it.")
        if hotspots.empty:
            st.caption("None above the thresholds in this run.")
        else:
            shown = hotspots.assign(read=hotspots["bytes_scanned"].map(page.size)).drop(
                columns=["bytes_scanned"]
            )
            page.table(shown, counts=("reads", "table_rows", "rows_scanned"))
with incremental_tab:
    incremental = page.frame("incremental")
    if incremental is not None:
        st.caption(
            "The most expensive models still rebuilt in full. One can append only if none of "
            "its sources change rows in place."
        )
        page.table(incremental, dollars=("cluster_usd",))
with quota_tab:
    quotas = page.frame("quotas")
    if quotas is not None:
        st.caption(
            "A daily read quota at twice each user's 95th-percentile day and a per-query limit "
            "at twice its largest query. Users that also build, load or reconcile get none."
        )
        shown = quotas.drop(columns=["statements"])
        for column in (
            "p95_daily_bytes",
            "max_query_bytes",
            "daily_quota_bytes",
            "query_limit_bytes",
        ):
            shown[column] = shown[column].map(page.size)
        page.table(shown)
        st.code("\n".join(quotas["statements"]), language="sql")
