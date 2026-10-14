"""Overview: the company's run, one headline per module, and who owns what."""

from __future__ import annotations

import streamlit as st

from platform_ops.dashboard import charts, page
from platform_ops.dashboard.queries import metric

st.title("Data platform operations")
st.caption(
    "The latest `make demo` run, read from the ops store in Postgres as the dashboards' "
    "read-only role. Nothing on these pages is recomputed."
)

company = page.frame("company_run")
if company is not None and not company.empty:
    start = metric(company, "window_start")
    days = int(metric(company, "days"))
    ingestion = page.frame("ingestion")
    lineage = page.frame("lineage")
    a, b, c = st.columns(3)
    a.metric("Simulated window", f"{days} days")
    a.caption(f"From {start:%Y-%m-%d}")
    if ingestion is not None:
        b.metric("Rows ingested into ClickHouse", f"{int(metric(ingestion, 'rows_read')):,}")
    if lineage is not None:
        c.metric("Lineage edges", f"{int(metric(lineage, 'edges')):,}")

st.divider()
cost, reconcile = st.columns(2)

with cost:
    st.subheader("Cost")
    months = page.frame("cost_months")
    summary = page.frame("business_summary")
    if months is not None:
        st.metric("ClickHouse's stated cost, whole window", page.usd(months["stated_usd"].sum()))
    if summary is not None and not summary.empty:
        saving = float(metric(summary, "monthly_saving_usd"))
        payback = metric(summary, "payback_months")
        st.metric("Saving once the reporting replica is retired", f"{page.usd(saving)} a month")
        if payback is not None:
            st.metric("Payback of running both until cutover", f"{payback:.1f} months")

with reconcile:
    st.subheader("Reconciliation")
    cutover = page.frame("cutover")
    grades = page.frame("grades")
    if cutover is not None and not cutover.empty:
        if bool(metric(cutover, "recommended")):
            st.markdown(
                ":green[:material/check_circle: **Cutover recommended**] on "
                f"{metric(cutover, 'run_at'):%Y-%m-%d}"
            )
        else:
            st.markdown(":red[:material/cancel: **Cutover not recommended yet**]")
        st.metric(
            "Clean nights in a row",
            f"{int(metric(cutover, 'clean_nights'))} of {int(metric(cutover, 'required'))}",
        )
    if grades is not None and not grades.empty:
        expected = int(grades["expected"].sum())
        found = int(grades["true_positives"].sum())
        st.metric("Planted discrepancies found", f"{found:,} of {expected:,}")

st.divider()
st.subheader("Ownership")
st.caption(
    "Every source, model, seed, snapshot and dashboard resolves to one owner in "
    "`config/ownership.yaml`. The platform team owns the shared layers."
)
coverage = page.frame("ownership_coverage")
by_team = page.frame("owners_by_team")
if coverage is not None and by_team is not None:
    left, right = st.columns([2, 3])
    with left:
        st.dataframe(coverage, hide_index=True, width="stretch")
    with right:
        page.chart(
            charts.single_bars(
                by_team, "team", "datasets", ",.0f", page.colors(), "Datasets owned"
            ),
            by_team,
        )
