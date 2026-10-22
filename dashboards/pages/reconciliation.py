"""Reconciliation: the parallel run night by night, and the cutover it led to."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from platform_ops.dashboard import charts, page
from platform_ops.dashboard.queries import metric

st.title("Migration reconciliation")
st.caption(
    "Every night of the parallel run, the legacy Postgres tables and reports were compared "
    "with ClickHouse's raw tables and dbt marts, against thresholds fixed before the run. "
    "The sign-off is `reports/reconciliation.md`."
)
colors = page.colors()


def events(row: pd.Series) -> str:
    """What happened on a night besides the comparison, in words."""
    said = ["the job as delivered" if row["job"] == "delivered" else "the fixed job"]
    if row["backfilled"]:
        said.append("raw backfilled")
    if row["planted"]:
        said.append(f"fault {row['planted']} planted")
    if row["repaired"]:
        said.append(f"fault {row['repaired']} repaired")
    return ", ".join(said)


cutover = page.frame("cutover")
if cutover is not None and not cutover.empty:
    a, b, c = st.columns(3)
    if bool(metric(cutover, "recommended")):
        a.markdown(
            ":green[:material/check_circle: **Cutover recommended**]  \n"
            f"on {metric(cutover, 'run_at'):%Y-%m-%d}"
        )
    else:
        a.markdown(":red[:material/cancel: **Cutover not recommended**]")
    b.metric(
        "Clean nights in a row",
        f"{int(metric(cutover, 'clean_nights'))} of {int(metric(cutover, 'required'))}",
    )
    c.metric("Nights compared", int(metric(cutover, "parallel_nights")))

nights = page.frame("nights")
grades = page.frame("grades")
if nights is not None and not nights.empty:
    first = int(nights["night"].min())
    nights = nights.assign(
        n=nights["night"] - first + 1,
        date=nights["run_at"].dt.strftime("%Y-%m-%d"),
        pair=nights["level"] + ": " + nights["name"],
    )
    nights["status"] = [
        "passed"
        if passed
        else ("differs, cause upstream" if level == "report" and explained else "failed")
        for passed, level, explained in zip(
            nights["passed"], nights["level"], nights["explained"].fillna(False), strict=True
        )
    ]
    nights["detail"] = [
        upstream if status == "differs, cause upstream" else (failures or "")
        for status, upstream, failures in zip(
            nights["status"], nights["differing_upstream"], nights["failures"], strict=True
        )
    ]
    per_night = nights.drop_duplicates("n").set_index("n")

    st.subheader("Night by night")
    st.caption(
        "A cell per night and compared pair. A report that differs only because a table it is "
        "built from differs the same night is explained by lineage; anything else fails."
    )
    grid = nights[["n", "date", "pair", "status", "detail"]].rename(columns={"n": "night"})
    page.chart(charts.night_grid(grid, colors), grid, "Every cell")
    timeline = pd.DataFrame(
        {
            "night": per_night.index,
            "date": per_night["date"],
            "what happened": [events(row) for _, row in per_night.iterrows()],
        }
    )
    with st.expander("What happened each night"):
        st.dataframe(timeline, hide_index=True, width="stretch")

    st.subheader("One night in detail")
    chosen = st.select_slider(
        "Night",
        options=list(per_night.index),
        value=int(per_night.index[0]),
        format_func=lambda n: f"{n} ({per_night.loc[n, 'date']})",
    )
    st.caption(events(per_night.loc[chosen]).capitalize() + ".")
    if grades is not None:
        grade = grades[grades["night"] == chosen + first - 1]
        if not grade.empty:
            row = grade.iloc[0]
            expected, detected, hits = (
                int(row["expected"]),
                int(row["detected"]),
                int(row["true_positives"]),
            )
            a, b, c, d = st.columns(4)
            a.metric("Planted", f"{expected:,}")
            b.metric("Recall", f"{hits / expected:.2%}" if expected else "nothing planted")
            c.metric("Precision", f"{hits / detected:.2%}" if detected else "nothing found")
            d.metric(
                "Classified correctly",
                f"{int(row['correctly_classified']) / hits:.2%}" if hits else "n/a",
            )
    tonight = nights[(nights["n"] == chosen) & (nights["status"] != "passed")]
    if tonight.empty:
        st.markdown(":green[:material/check_circle: Every table and report within thresholds.]")
    else:
        page.table(
            tonight[
                [
                    "pair",
                    "status",
                    "source_rows",
                    "missing",
                    "extra",
                    "duplicate_key",
                    "rows_with_diffs",
                    "detail",
                ]
            ],
            counts=("source_rows", "missing", "extra", "duplicate_key", "rows_with_diffs"),
        )
    labels = page.frame("labels")
    if labels is not None:
        found = labels[labels["night"] == chosen + first - 1].drop(columns=["night"])
        if not found.empty:
            with st.expander("Planted against found, by kind"):
                page.table(found, counts=("planted", "found"))
    examples = page.frame("examples")
    if examples is not None:
        mine = examples[examples["night"] == chosen + first - 1].drop(columns=["night"])
        if not mine.empty:
            with st.expander(f"Examples ({len(mine)})"):
                st.dataframe(mine, hide_index=True, width="stretch")

    st.subheader("What the comparison moved")
    st.caption(
        "Rows read to compare each night: the segmented checksum diff only fetches the "
        "segments whose checksums differ; a naive comparison moves every row of both sides."
    )
    moved = nights.groupby("n", as_index=False)[["rows_moved", "rows_moved_naive"]].sum()
    page.chart(
        charts.accent_lines(
            moved.rename(columns={"n": "night"}),
            "night",
            "rows_moved",
            "rows_moved_naive",
            ("Segmented diff", "Naive comparison"),
            colors,
            log=True,
        ),
        moved.rename(columns={"n": "night"}),
    )
