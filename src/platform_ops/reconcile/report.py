"""``reports/reconciliation.md``: the migration sign-off.

The thresholds come first, as they were fixed before the run, so the document
can be signed as it stands. Every number comes from the run: nothing here is
timed or random, so two runs write byte-identical files.
"""

from __future__ import annotations

from pathlib import Path

from platform_ops.common.config import DISCREPANCY_CLASSES, Settings
from platform_ops.cost.report import table
from platform_ops.reconcile.canonical import NULL
from platform_ops.reconcile.run import PassResult, ReconcileResult
from platform_ops.reconcile.schema import TABLES
from platform_ops.simulation.migration_faults import CATALOGUE

ENGINE_LABEL = {"postgres": "Postgres", "sqlserver": "SQL Server", "duckdb": "DuckDB"}
PASS_LABEL = {
    "as_delivered": "the job as delivered",
    "fixed": "the job after fixing D1 to D4",
}
LABELS = {
    "D1": "payments.paid_local converted with a fixed UTC-5 offset (daylight saving ignored)",
    "D2": "orders.amount passed through DOUBLE",
    "D3": "customers.company_name right-trimmed",
    "D4": "first order of every 25,000-key batch dropped (`>` instead of `>=`)",
}
for _fault in CATALOGUE:
    _what = f"{_fault.table}.{_fault.column}" if _fault.column else _fault.table
    LABELS[_fault.fault_id] = f"planted: {_fault.rows} {_fault.klass} in {_what}"
SAMPLES_PER_CLASS = 3


def pct(value: float) -> str:
    """Three decimals, and never 100% unless it is exactly 100%.

    Six differing rows out of 300,000 is 99.998%; two decimals would print
    100.00% next to a table that fails sign-off.
    """
    text = f"{value:.3%}"
    return "99.999%" if value < 1 and text == "100.000%" else text


def shown(value: str) -> str:
    """A canonical value readable in a Markdown table: quoted, pipes escaped."""
    if value == NULL:
        return "NULL"
    return '"' + value.replace("\\|", "|").replace("|", "\\|") + '"'


def _verdict_rows(result: ReconcileResult) -> list[tuple[str, ...]]:
    rows = []
    for p in result.passes:
        cells = ["PASS" if p.verdicts[t.name].passed else "FAIL" for t in TABLES]
        rows.append((ENGINE_LABEL.get(p.engine, p.engine), PASS_LABEL[p.name], *cells,
                     "**PASS**" if p.passed else "**FAIL**"))  # fmt: skip
    return rows


def _match_section(p: PassResult) -> list[str]:
    rows = []
    for spec in TABLES:
        d, v = p.diffs[spec.name], p.verdicts[spec.name]
        rows.append((spec.name, f"{d.source_rows:,}", f"{d.target_rows:,}",
                     f"{len(d.missing):,}", f"{len(d.extra):,}", f"{d.rows_with_cell_diffs:,}",
                     pct(v.row_match_rate)))  # fmt: skip
    columns = [
        (f"{spec.name}.{name}", pct(rate))
        for spec in TABLES
        for name, rate in p.verdicts[spec.name].column_match_rates.items()
        if rate < 1
    ]
    lines = [
        table(
            [
                "Table",
                "Legacy rows",
                "Target rows",
                "Missing",
                "Extra",
                "Rows with differences",
                "Row match",
            ],
            rows,
            right=[1, 2, 3, 4, 5, 6],
        ),  # fmt: skip
        "",
    ]
    if columns:
        lines += ["Columns below 100%:", "", table(["Column", "Match"], columns, right=[1]), ""]
    else:
        lines += ["Every column matches 100%.", ""]
    return lines


def _class_section(p: PassResult) -> list[str]:
    rows = []
    for klass in DISCREPANCY_CLASSES:
        counts = [p.verdicts[t.name].by_class[klass] for t in TABLES]
        if any(counts):
            rows.append((klass, *[f"{n:,}" for n in counts], f"{sum(counts):,}"))
    if not rows:
        return ["No discrepancies.", ""]
    headers = ["Class", *[t.name for t in TABLES], "Total"]
    return [table(headers, rows, right=list(range(1, len(headers)))), ""]


def _sample_section(p: PassResult) -> list[str]:
    rows = []
    for klass in DISCREPANCY_CLASSES:
        picked = [d for d in p.discrepancies if d.klass == klass][:SAMPLES_PER_CLASS]
        for d in picked:
            if d.column:
                rows.append((klass, d.table, str(d.key), d.column, shown(d.source_value),
                             shown(d.target_value)))  # fmt: skip
            else:
                where = "legacy only" if klass == "missing_in_target" else "target only"
                rows.append((klass, d.table, str(d.key), "(whole row)", where, ""))
    if not rows:
        return []
    return [
        f"Up to {SAMPLES_PER_CLASS} per class, lowest keys first, as canonical values.",
        "",
        table(["Class", "Table", "Key", "Column", "Legacy", "Target"], rows),
        "",
    ]


def _grade_section(p: PassResult) -> list[str]:
    g = p.grade
    lines = [
        table(
            ["Measure", "Value"],
            [
                ("Planted discrepancies (cells or rows)", f"{g.expected:,}"),
                ("Reported by the diff", f"{g.detected:,}"),
                ("Detection recall", pct(g.recall)),
                ("Precision", pct(g.precision)),
                ("Classification accuracy", pct(g.classification_accuracy)),
                ("Planted, but equal under the source's policy", f"{g.equivalent_under_policy:,}"),
            ],
            right=[1],
        ),
        "",
        table(
            ["Cause", "What", "Planted", "Found"],
            [
                (label, LABELS.get(label, label), f"{planted:,}", f"{found:,}")
                for label, (planted, found) in g.by_label.items()
            ],  # fmt: skip
            right=[2, 3],
        ),
        "",
    ]
    return lines


def _benchmark_section(p: PassResult) -> list[str]:
    rows = []
    for spec in TABLES:
        d = p.diffs[spec.name]
        levels = ", ".join(f"{w:,}: {diff}/{compared}" for w, compared, diff in d.levels)
        saving = 1 - d.transferred / d.naive_transferred if d.naive_transferred else 0.0
        rows.append((spec.name, levels, f"{d.summary_rows:,}", f"{d.fetched_rows:,}",
                     f"{d.transferred:,}", f"{d.naive_transferred:,}", pct(saving)))  # fmt: skip
    return [
        "Rows moved from the engines to the comparer, both sides together. Levels read "
        "`segment width: differing/compared`.",
        "",
        table(
            [
                "Table",
                "Levels",
                "Summary rows",
                "Fetched rows",
                "Segmented total",
                "Naive",
                "Saving",
            ],
            rows,
            right=[2, 3, 4, 5, 6],
        ),  # fmt: skip
        "",
    ]


def _policy_section(settings: Settings, result: ReconcileResult) -> list[str]:
    r = settings.reconcile
    rows = []
    for engine in result.engines:
        rules = result.rules[engine]
        for spec in TABLES:
            for column in spec.columns:
                rule = rules[spec.name][column.name]
                if column.kind == "text":
                    detail = f"rtrim {'on' if rule.rtrim else 'off'}, case folding " + (
                        "on" if rule.casefold else "off"
                    )
                elif column.kind == "decimal":
                    detail = f"scale {rule.scale}"
                elif column.kind == "local_ts":
                    detail = f"local time in {r.legacy_timezone}, rendered as UTC"
                elif column.kind == "utc_ts":
                    detail = "rendered as UTC"
                else:
                    continue
                rows.append((ENGINE_LABEL.get(engine, engine), f"{spec.name}.{column.name}",
                             detail))  # fmt: skip
    return [
        "Both sides of a comparison are rendered with the legacy engine's rules before hashing "
        "(ADR 0009). Decimals round half away from zero; timestamps are UTC with microseconds; "
        "NULL is a sentinel distinct from an empty string.",
        "",
        table(["Engine", "Column", "Rule"], rows),
        "",
        f"Segments split {r.fanout} ways down to {r.leaf_width} keys. A decimal difference of up "
        f"to {r.rounding_tolerance} is classed as rounding.",
        "",
    ]


def render(settings: Settings, result: ReconcileResult) -> str:
    t = settings.reconcile.thresholds
    engines = ", ".join(ENGINE_LABEL.get(e, e) for e in result.engines)
    sizes = ", ".join(f"{n:,} {name}" for name, n in result.rows.items())
    signed = result.signed_off()
    lines = [
        "# Migration reconciliation sign-off",
        "",
        f"Legacy {engines} ({sizes}) migrated into Iceberg (PyIceberg, SQLite catalog, local "
        "warehouse) and compared with a segmented checksum diff. Generated by `make reconcile`; "
        "every number below comes from that run.",
        "",
        "## Acceptance thresholds",
        "",
        "Fixed in `config/settings.yaml` before the run. A table passes only if it meets all of "
        "them.",
        "",
        table(
            ["Criterion", "Threshold"],
            [
                ("Row match rate", f"at least {pct(t.min_row_match_rate)}"),
                ("Match rate of every column", f"at least {pct(t.min_column_match_rate)}"),
            ]
            + [
                (f"`{klass}` discrepancies", f"at most {limit:,}")
                for klass, limit in t.max_discrepancies.items()
            ],  # fmt: skip
        ),
        "",
        "## Verdict",
        "",
        (
            "**Signed off.** The migration as delivered meets every threshold."
            if signed
            else "**Not signed off.** The migration as delivered fails the thresholds below."
        ),  # fmt: skip
        "",
        table(["Engine", "Run", *[s.name for s in TABLES], "Overall"], _verdict_rows(result)),
        "",
    ]
    for p in result.passes:
        heading = f"{ENGINE_LABEL.get(p.engine, p.engine)}: {PASS_LABEL[p.name]}"
        failures = [
            f"- {name}: {reason}" for name, v in p.verdicts.items() for reason in v.failures
        ]
        lines += [f"## {heading}", ""]
        if failures:
            lines += ["Why it fails:", "", *failures, ""]
        lines += ["### Match rates", "", *_match_section(p)]
        lines += ["### Discrepancies by class", "", *_class_section(p)]
        samples = _sample_section(p)
        if samples:
            lines += ["### Sample rows", "", *samples]
        lines += ["### Detection against ground truth", "", *_grade_section(p)]
        lines += ["### Segmented diff against a naive comparison", "", *_benchmark_section(p)]
    lines += ["## Canonical policy", "", *_policy_section(settings, result)]
    return "\n".join(lines)


def write_report(settings: Settings, result: ReconcileResult) -> Path:
    reports = settings.resolve(settings.paths.reports)
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / "reconciliation.md"
    path.write_text(render(settings, result), encoding="utf-8", newline="\n")
    return path
