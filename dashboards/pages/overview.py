"""Overview: one headline per module, and who owns what."""

from __future__ import annotations

import streamlit as st

from platform_ops.dashboard import charts, page

st.title("Data platform operations")
st.caption(
    "The latest `make demo` run, read from the warehouse's `ops` schema. "
    "Nothing on these pages is recomputed."
)

cost, incidents, reconcile = st.columns(3)

with cost:
    st.subheader("Cost")
    showback = page.frame("showback")
    if showback is not None:
        totals = showback.groupby("pricing_model")["total_usd"].sum()
        st.metric("Compute pricing, whole window", f"${totals.get('compute', 0):,.2f}")
        st.metric("Scan pricing, same queries", f"${totals.get('scan', 0):,.2f}")
    unused = page.frame("unused_tables")
    if unused is not None:
        st.metric("Unused tables (90 days)", int((unused["lookback_days"] == 90).sum()))

with incidents:
    st.subheader("Incidents")
    metrics = page.frame("incident_metrics")
    if metrics is not None:
        wh = page.warehouse()
        raw = wh.metric(metrics, "raw_alerts") or 0
        opened = wh.metric(metrics, "incidents") or 0
        pages_sent = wh.metric(metrics, "pages") or 0
        st.metric("Failing checks", f"{raw:,.0f}")
        st.metric("Incidents opened", f"{opened:,.0f}")
        avoided = 1 - pages_sent / raw if raw else 0.0
        st.metric("Pages avoided by grouping", f"{avoided:.0%}")

with reconcile:
    st.subheader("Reconciliation")
    tables = page.frame("reconcile_tables")
    if tables is not None:
        delivered = tables[tables["pass"] == "as_delivered"]
        signed = bool(delivered["passed"].all()) if not delivered.empty else False
        st.markdown(
            "Migration as delivered: "
            + (
                ":green[:material/check_circle: **signed off**]"
                if signed
                else ":red[:material/cancel: **not signed off**]"
            )
        )
        rm = page.frame("reconcile_metrics")
        if rm is not None:
            recall = rm[(rm["pass"] == "as_delivered") & (rm["metric"] == "recall")]["value"]
            if not recall.empty:
                st.metric("Detection recall", f"{recall.min():.2%}")
        st.metric("Legacy rows compared", f"{int(delivered['source_rows'].sum()):,}")

st.divider()
st.subheader("Ownership")
st.caption(
    "Every source, model and dashboard resolves to one owner in `config/ownership.yaml`. "
    "The platform team owns the shared layers, so most incidents route there."
)
coverage = page.frame("ownership_coverage")
by_team = page.frame("owners_by_team")
if coverage is not None and by_team is not None:
    left, right = st.columns([1, 2])
    with left:
        shown = coverage.assign(
            coverage=lambda f: (f["owned"] / f["datasets"]).map("{:.0%}".format)
        )
        st.dataframe(shown, hide_index=True, width="stretch")
    with right:
        page.chart(
            charts.single_bars(
                by_team, "team", "datasets", ",.0f", page.colors(), "Datasets owned"
            ),
            by_team,
        )
