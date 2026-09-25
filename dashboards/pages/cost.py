"""Cost: who spends what, under either pricing model, and where the waste is."""

from __future__ import annotations

import streamlit as st

from platform_ops.dashboard import charts, page

st.title("Cost attribution")
st.caption(
    "Every query charged once: building a table is production cost for its owner, reading it "
    "is consumption cost for the reader's team. See `reports/cost.md` for the full report."
)

model_label = st.segmented_control(
    "Pricing model", ["Compute", "Scan"], default="Compute", key="pricing_model"
)
model = (model_label or "Compute").lower()
colors = page.colors()

showback = page.frame("showback")
if showback is not None:
    chosen = showback[showback["pricing_model"] == model]
    total = chosen["total_usd"].sum()
    production = chosen["production_usd"].sum()
    a, b, c = st.columns(3)
    a.metric("Total", f"${total:,.2f}")
    b.metric("Production (building tables)", f"${production:,.2f}")
    c.metric("Consumption (reading them)", f"${total - production:,.2f}")

    st.subheader("Showback by team")
    page.chart(
        charts.stacked_share(
            chosen,
            "team",
            {"production_usd": "Production", "consumption_usd": "Consumption"},
            "$,.2f",
            colors,
        ),
        chosen.drop(columns=["pricing_model"]),
    )

left, right = st.columns(2)
with left:
    monthly = page.frame("cost_by_month")
    if monthly is not None:
        st.subheader("By month")
        chosen_month = monthly[monthly["pricing_model"] == model]
        scale = charts.team_colors(page.teams(), list(chosen_month["team"].unique()), colors)
        page.chart(
            charts.team_lines(chosen_month, "month", "total_usd", "team", scale, "$,.2f"),
            chosen_month.drop(columns=["pricing_model"]),
        )
with right:
    workload = page.frame("cost_by_workload")
    if workload is not None:
        st.subheader("By workload")
        column = f"{model}_usd"
        page.chart(
            charts.single_bars(workload, "workload", column, "$,.2f", colors),
            workload[["workload", "scan_usd", "compute_usd", "busy_seconds", "billed_seconds"]],
        )

st.subheader("Top models by cost")
models = page.frame("top_models")
if models is not None:
    st.dataframe(models, hide_index=True, width="stretch")

st.subheader("Waste and what to change")
unused_tab, hotspot_tab, incremental_tab = st.tabs(
    ["Unused tables", "Full-scan hotspots", "Incremental candidates"]
)
with unused_tab:
    unused = page.frame("unused_tables")
    if unused is not None:
        lookback = st.radio(
            "Unused for at least", [30, 90], format_func=lambda d: f"{d} days", horizontal=True
        )
        st.dataframe(unused[unused["lookback_days"] == lookback], hide_index=True,
                     width="stretch")  # fmt: skip
with hotspot_tab:
    hotspots = page.frame("hotspots")
    if hotspots is not None:
        st.caption("Large tables read again and again with a filter on the same column.")
        if hotspots.empty:
            st.caption("None in this run: no table is large enough to qualify.")
        else:
            st.dataframe(hotspots, hide_index=True, width="stretch")
with incremental_tab:
    incremental = page.frame("incremental")
    if incremental is not None:
        st.caption("Expensive full-refresh models; only those on append-only sources qualify.")
        st.dataframe(incremental, hide_index=True, width="stretch")
