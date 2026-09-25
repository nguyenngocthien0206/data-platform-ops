"""Reconciliation: the sign-off verdict, what differs, and what the diff cost."""

from __future__ import annotations

import streamlit as st

from platform_ops.dashboard import charts, page

st.title("Migration reconciliation")
st.caption(
    "Legacy tables migrated into Iceberg and compared with a segmented checksum diff, against "
    "thresholds fixed before the run. See `reports/reconciliation.md` for the sign-off document."
)
colors = page.colors()
PASS_LABEL = {"as_delivered": "The job as delivered", "fixed": "The job after fixing its defects"}

tables = page.frame("reconcile_tables")
if tables is not None:
    engines = sorted(tables["engine"].unique())
    engine = st.selectbox("Legacy engine", engines) if len(engines) > 1 else engines[0]
    chosen_pass = (
        st.segmented_control(
            "Run", list(PASS_LABEL), default="as_delivered", format_func=PASS_LABEL.get, key="pass"
        )
        or "as_delivered"
    )
    mine = tables[(tables["engine"] == engine) & (tables["pass"] == chosen_pass)]

    st.subheader("Verdict")
    verdicts = st.columns(len(mine) or 1)
    for column, row in zip(verdicts, mine.itertuples(), strict=False):
        with column:
            st.markdown(f"**{row.table_name}**  \n{page.status(bool(row.passed))}")
            st.metric("Row match", f"{row.row_match_rate:.3%}", label_visibility="visible")

    metrics = page.frame("reconcile_metrics")
    if metrics is not None:
        m = metrics[(metrics["engine"] == engine) & (metrics["pass"] == chosen_pass)]

        def value(name: str) -> float:
            rows = m[(m["metric"] == name) & (m["dimension"] == "all")]["value"]
            return float(rows.iloc[0]) if not rows.empty else 0.0

        st.subheader("Detection against ground truth")
        a, b, c, d = st.columns(4)
        a.metric("Planted discrepancies", f"{value('expected'):,.0f}")
        b.metric("Recall", f"{value('recall'):.2%}")
        c.metric("Precision", f"{value('precision'):.2%}")
        d.metric("Classified correctly", f"{value('classification_accuracy'):.2%}")

        columns = m[m["metric"] == "column_match_rate"].rename(
            columns={"dimension": "column", "value": "match_rate"}
        )[["column", "match_rate"]]
        below = columns[columns["match_rate"] < 1]
        if not below.empty:
            with st.expander(f"{len(below)} columns below 100%"):
                st.dataframe(below.style.format({"match_rate": "{:.3%}"}), hide_index=True,
                             width="stretch")  # fmt: skip

    classes = page.frame("discrepancies_by_class")
    if classes is not None:
        st.subheader("Discrepancies by class")
        mine_classes = classes[(classes["engine"] == engine) & (classes["pass"] == chosen_pass)]
        pivot = mine_classes.pivot_table(
            index="class", columns="table_name", values="discrepancies", fill_value=0,
            aggfunc="sum",
        )  # fmt: skip
        st.dataframe(pivot, width="stretch")

    samples = page.frame("discrepancy_samples")
    if samples is not None:
        with st.expander("Sample rows per class"):
            st.dataframe(
                samples[(samples["engine"] == engine) & (samples["pass"] == chosen_pass)].drop(
                    columns=["engine", "pass"]
                ),
                hide_index=True,
                width="stretch",
            )

    st.subheader("Rows moved: segmented diff against a naive copy")
    st.caption(
        "A systematic defect makes nearly every segment differ, so the first run moves about as "
        "much as a full copy. Once the job is fixed, only the few differing segments are opened."
    )
    page.chart(
        charts.accent_against_grey(mine, "table_name", "segmented_rows", "naive_rows",
                                   ("Segmented diff", "Naive copy"), colors),
        mine[["table_name", "summary_rows", "fetched_rows", "segmented_rows", "naive_rows",
              "queries"]],
    )  # fmt: skip

    segments = page.frame("reconcile_segments")
    if segments is not None:
        with st.expander("Segments per level"):
            st.dataframe(
                segments[(segments["engine"] == engine) & (segments["pass"] == chosen_pass)],
                hide_index=True,
                width="stretch",
            )
