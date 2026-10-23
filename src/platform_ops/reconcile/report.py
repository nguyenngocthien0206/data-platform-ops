"""``reports/reconciliation.md``: the cutover sign-off of the parallel run.

Read back from the ops store, so ``make reconcile`` can write it again at any
time without replaying the company. The thresholds come first, as they were
fixed before the run, so the document can be signed as it stands. Nothing in
it is timed or random: two runs write byte-identical files.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from psycopg import sql

from platform_ops.common.config import DISCREPANCY_CLASSES, Settings
from platform_ops.common.markdown import table
from platform_ops.common.store import open_store, ops_table
from platform_ops.reconcile.canonical import NULL
from platform_ops.simulation.migration import CATALOGUE

DEFECTS = {
    "D1 local time read as UTC": "D1",
    "D2 decimal through a float": "D2",
    "D3 trailing spaces trimmed": "D3",
    "D4 last key of every page lost": "D4",
}
SAMPLES_PER_CLASS = 3


class NoParallelRun(RuntimeError):
    """The ops store holds no parallel run; ``make demo`` writes one."""


def pct(value: float) -> str:
    """Three decimals, and never 100% unless it is exactly 100%."""
    text = f"{value:.3%}"
    return "99.999%" if value < 1 and text == "100.000%" else text


def shown(value: str) -> str:
    """A canonical value readable in a Markdown table: quoted, pipes escaped."""
    if value == NULL:
        return "NULL"
    return '"' + value.replace("\\|", "|").replace("|", "\\|") + '"'


def _rows(settings: Settings, name: str, order: str) -> list[dict[str, Any]]:
    with open_store(settings) as store:
        exists = store.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            [f'"{settings.company.name(settings.company.ops_schema)}".{name}'],
        ).fetchone()
        if not exists or not exists[0]:
            raise NoParallelRun(f"ops.{name} does not exist; run `make demo` first")
        cursor = store.execute(
            sql.SQL("SELECT * FROM {} ORDER BY " + order).format(ops_table(settings, name))
        )
        columns = [c.name for c in cursor.description or []]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _differing(night: dict[str, Any]) -> int:
    """Rows of a pair that did not match that night, whatever the reason."""
    return int(
        night["missing"] + night["extra"] + night["duplicate_key"] + night["rows_with_diffs"]
    )


def _day(value: Any) -> str:
    return f"{value:%Y-%m-%d}"


def _policy(settings: Settings) -> str:
    """The equality policy as the data owner set it, in words, from the settings."""
    base = settings.reconcile.canonical.engines.get("postgres")
    spaces = "ignored" if base and base.rtrim else "count as differences"
    case = "ignored" if base and base.casefold else "counts as a difference"
    text = (
        "Equality policy (the data owner's, applied to both sides): trailing spaces "
        f"{spaces}; letter case {case}; decimals are compared at their own scale"
    )
    overrides = sorted(settings.reconcile.canonical.columns)
    if overrides:
        text += "; overridden for " + ", ".join(f"`{name}`" for name in overrides)
    return text + "."


def render(settings: Settings) -> str:
    nights = _rows(settings, "reconcile_nights", "night, level DESC, name")
    if not nights:
        raise NoParallelRun("the parallel run recorded no night; run `make demo` first")
    grades = {g["night"]: g for g in _rows(settings, "reconcile_grades", "night")}
    labels = _rows(settings, "reconcile_labels", "night, label")
    examples = _rows(
        settings, "reconcile_examples", "night, level DESC, name, class, key, column_name"
    )
    (cutover,) = _rows(settings, "reconcile_cutover", "recommended")
    r = settings.reconcile
    by_night: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in nights:
        by_night[row["night"]].append(row)
    order = sorted(by_night)
    first, last = by_night[order[0]][0], by_night[order[-1]][0]
    tables = sorted({row["name"] for row in nights if row["level"] == "table"})
    reports = sorted({row["name"] for row in nights if row["level"] == "report"})

    out = ["# Migration sign-off: legacy Postgres to ClickHouse", ""]
    out += [
        f"Parallel run of {len(order)} nights, {_day(first['run_at'])} to {_day(last['run_at'])}: "
        f"every night, after the ingestion and the dbt build, the {len(tables)} operational "
        f"tables in Postgres were compared with ClickHouse's raw layer, and the "
        f"{len(reports)} legacy reports with the dbt marts that replace them.",
        "",
        "## Acceptance criteria",
        "",
        "Fixed before the run. A night is clean when every table and every report meets "
        "all of them, and no report differs for a reason its upstream tables do not show. "
        f"The cutover is recommended after {r.clean_nights_for_cutover} consecutive clean nights.",
        "",
        table(
            ["Criterion", "Threshold"],
            [
                ("Row match rate", f">= {r.thresholds.min_row_match_rate:.1%}"),
                ("Column match rate", f">= {r.thresholds.min_column_match_rate:.1%}"),
                *(
                    (f"{k} differences", f"<= {r.thresholds.max_discrepancies[k]:,}")
                    for k in DISCREPANCY_CLASSES
                ),
                ("Rounding tolerance", f"{r.rounding_tolerance}"),
                (
                    "Legacy side behind the comparison",
                    f"<= {r.thresholds.max_watermark_lag_hours:g} hours",
                ),
            ],
        ),
        "",
        _policy(settings),
        "",
    ]

    out += ["## Verdict", ""]
    if cutover["recommended"]:
        out += [
            f"**Cutover recommended on {_day(cutover['run_at'])}** (night {cutover['night']}), "
            f"the night that completed {cutover['required']} consecutive clean nights.",
        ]
    else:
        out += [
            f"**Cutover not recommended.** The run ended {cutover['clean_nights']} clean nights "
            f"into a required streak of {cutover['required']}.",
        ]
    out += [""]

    out += ["## Night by night", ""]
    timeline = []
    for night in order:
        rows = by_night[night]
        head = rows[0]
        t_pass = sum(1 for x in rows if x["level"] == "table" and x["passed"])
        r_pass = sum(1 for x in rows if x["level"] == "report" and x["passed"])
        unexplained = sum(1 for x in rows if x["explained"] is False)
        events = []
        if head["backfilled"]:
            events.append("fixed job, backfill")
        if head["planted"]:
            events.append(f"{head['planted']} planted")
        if head["repaired"]:
            events.append(f"{head['repaired']} repaired")
        g = grades.get(night)
        night_recall = (
            "" if not g or not g["expected"] else pct(g["true_positives"] / g["expected"])
        )
        clean = t_pass == len(tables) and r_pass == len(reports) and not unexplained
        written_after = sum(x["after_watermark"] for x in rows)
        timeline.append(
            (
                _day(head["run_at"]),
                head["job"],
                ", ".join(events),
                f"{written_after:,}",
                f"{t_pass}/{len(tables)}",
                f"{r_pass}/{len(reports)}",
                f"{unexplained}",
                night_recall,
                "clean" if clean else "**not clean**",
            )
        )
    out += [
        "Every night compares the legacy side as of the copy: each table up to the "
        "copy's watermark, each report as frozen just before the copy read. Rows the "
        "application wrote after it are left for the next night, once the copy has them.",
        "",
        table(
            [
                "Night",
                "Job",
                "Events",
                "Written after the copy",
                "Tables passing",
                "Reports passing",
                "Unexplained reports",
                "Recall",
                "Night",
            ],
            timeline,
            right=(3, 4, 5, 6, 7),
        ),
        "",
    ]

    out += ["## What the parallel run found", ""]
    first_found: dict[str, int] = {}
    planted_found: dict[str, tuple[int, int]] = {}
    for row in labels:
        label = row["label"]
        if label not in planted_found:
            planted_found[label] = (row["planted"], row["found"])
        if row["found"] and label not in first_found:
            first_found[label] = row["night"]
    found_rows = []
    for label, (planted, found) in sorted(planted_found.items(), key=lambda kv: kv[0]):
        cause = "job defect" if label.startswith("D") else "one-off fault"
        found_night = first_found.get(label)
        found_rows.append(
            (
                label,
                cause,
                f"{planted:,}",
                f"{found:,}",
                "" if found_night is None else _day(by_night[found_night][0]["run_at"]),
            )
        )
    out += [
        "The first night each one appeared. A job defect stays until the fixed job ships; a "
        "one-off fault is repaired the day after it is planted.",
        "",
        table(
            ["Planted", "Cause", "Discrepancies", "Found", "First found"], found_rows, right=(2, 3)
        ),
        "",
    ]
    tolerated = []
    for night in order:
        rows = by_night[night]
        head = rows[0]
        if head["planted"] and all(x["passed"] for x in rows):
            tolerated.append(f"{head['planted']} ({_day(head['run_at'])})")
    if tolerated:
        out += [
            "Found but within the thresholds, so its night counted as clean: "
            + ", ".join(tolerated)
            + ". The criteria are the data owner's statement of what is acceptable, and a "
            "few cents of rounding is within them.",
            "",
        ]
    totals: dict[str, int] = defaultdict(int)
    for g in grades.values():
        for k in (
            "expected",
            "detected",
            "true_positives",
            "correctly_classified",
            "equivalent_under_policy",
        ):
            totals[k] += g[k]
    recall = totals["true_positives"] / totals["expected"] if totals["expected"] else 1.0
    precision = totals["true_positives"] / totals["detected"] if totals["detected"] else 1.0
    accuracy = (
        totals["correctly_classified"] / totals["true_positives"]
        if totals["true_positives"]
        else 1.0
    )
    out += [
        "Detection over the whole run, operational tables, graded against what was planted:",
        "",
        table(
            ["Planted", "Detected", "Recall", "Precision", "Classification accuracy"],
            [
                (
                    f"{totals['expected']:,}",
                    f"{totals['detected']:,}",
                    pct(recall),
                    pct(precision),
                    pct(accuracy),
                )
            ],
            right=(0, 1, 2, 3, 4),
        ),
        "",
    ]

    unexplained_rows = [x for x in nights if x["explained"] is False]
    out += ["## Reports the tables do not explain", ""]
    out += [
        "A report difference is explained only through the columns the report reads: each "
        "report column that differs needs an upstream column it is computed from, or a "
        "table it reads with rows missing or extra, to differ the same night (column "
        "lineage from dbt's compiled SQL).",
        "",
    ]
    untraced = sorted({x["name"] for x in nights if x["traced"] is False})
    if untraced:
        out += [
            "Column lineage could not be traced for "
            + ", ".join(untraced)
            + ": their differences were explained by the tables their marts read, which is looser.",
            "",
        ]
    if unexplained_rows:
        out += [
            "A report that differs while nothing it reads differs upstream is a difference "
            "between the legacy SQL and the dbt model. Each needs a decision before cutover.",
            "",
            table(
                ["Night", "Report", "Rows with differences"],
                [
                    (
                        _day(x["run_at"]),
                        x["name"],
                        f"{_differing(x):,}",
                    )
                    for x in unexplained_rows
                ],
                right=(2,),
            ),
            "",
        ]
    else:
        out += [
            "None. On every night, every report column that differed was computed from an "
            "upstream column or table that differed too: the new marts reproduce the legacy "
            "reports' logic.",
            "",
        ]

    out += ["## Segmented diff against a naive comparison", ""]
    moved: dict[tuple[str, bool], list[int]] = defaultdict(lambda: [0, 0])
    for x in nights:
        clean_pair = not _differing(x)
        tally = moved[(x["level"], clean_pair)]
        tally[0] += x["rows_moved"]
        tally[1] += x["rows_moved_naive"]
    saving_rows = []
    for (level, clean_pair), (segmented, naive) in sorted(moved.items()):
        saving = 1 - segmented / naive if naive else 0.0
        saving_rows.append(
            (
                level,
                "matching" if clean_pair else "differing",
                f"{segmented:,}",
                f"{naive:,}",
                pct(saving),
            )
        )
    out += [
        "Rows moved from the engines to the comparison over the whole run, for pairs that "
        "matched and pairs that differed that night.",
        "",
        table(
            ["Level", "Pairs", "Rows moved, segmented", "Rows moved, naive", "Saving"],
            saving_rows,
            right=(2, 3, 4),
        ),
        "",
    ]

    out += ["## The first night in detail", ""]
    head_rows = [
        (
            x["level"],
            x["name"],
            f"{x['source_rows']:,}",
            f"{x['target_rows']:,}",
            f"{x['missing']:,}",
            f"{x['extra']:,}",
            f"{x['rows_with_diffs']:,}",
            pct(x["row_match_rate"]),
            "PASS" if x["passed"] else "FAIL",
        )
        for x in by_night[order[0]]
    ]
    out += [
        table(
            [
                "Level",
                "Name",
                "Legacy rows",
                "ClickHouse rows",
                "Missing",
                "Extra",
                "Rows with differences",
                "Row match",
                "Verdict",
            ],
            head_rows,
            right=(2, 3, 4, 5, 6, 7),
        ),
        "",
        "Examples, at most three per class:",
        "",
    ]
    shown_examples = []
    seen: dict[tuple[str, str], int] = defaultdict(int)
    for x in examples:
        if x["night"] != order[0]:
            continue
        slot = (x["name"], x["class"])
        if seen[slot] >= SAMPLES_PER_CLASS:
            continue
        seen[slot] += 1
        shown_examples.append(
            (
                x["name"],
                x["key"],
                x["column_name"] or "(row)",
                x["class"],
                shown(x["source_value"]) if x["column_name"] else "",
                shown(x["target_value"]) if x["column_name"] else "",
            )
        )
    out += [
        table(["Name", "Key", "Column", "Class", "Legacy", "ClickHouse"], shown_examples),
        "",
        "One-off faults planted in the run: "
        + "; ".join(f"{f.label} ({f.rows} rows)" for f in CATALOGUE)
        + ".",
        "",
    ]
    return "\n".join(out)


def write_report(settings: Settings) -> Path:
    path = settings.resolve(settings.paths.reports) / "reconciliation.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(settings), encoding="utf-8", newline="\n")
    return path


__all__ = ["DEFECTS", "NoParallelRun", "render", "write_report"]
