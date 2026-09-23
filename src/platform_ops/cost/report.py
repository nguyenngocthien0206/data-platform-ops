"""The cost report: ``reports/cost.md``, plus the proxy-accuracy note.

``cost.md`` is built only from priced, integer and ``Decimal`` data, so two runs
produce byte-identical files. The proxy-accuracy note compares the estimate with
DuckDB's profiler, whose row counts are not guaranteed to be stable across runs,
so it lives in its own file (``cost_proxy_accuracy.md``) and never affects
``cost.md``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import duckdb

from platform_ops.common.config import Settings
from platform_ops.common.db import OPS_SCHEMA
from platform_ops.cost import recommend
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import Resolution

WORKLOADS = ("dbt", "dashboard", "adhoc")
WORKLOAD_LABEL = {"dbt": "dbt builds and tests", "dashboard": "dashboards", "adhoc": "ad hoc"}


def usd(value: Decimal | float | int) -> str:
    amount = Decimal(str(value))
    return f"${amount:,.2f}" if abs(amount) >= 1 else f"${amount:.4f}"


def gigabytes(value: int | float) -> str:
    return f"{Decimal(str(value)) / Decimal(10**9):,.2f} GB"


def table(
    headers: Sequence[str], rows: Sequence[Sequence[object]], right: Sequence[int] = ()
) -> str:
    align = ["---:" if i in right else "---" for i in range(len(headers))]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(align) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


@dataclass
class ReportData:
    showback: list[tuple[str, str, Decimal, Decimal, Decimal]] = field(default_factory=list)
    monthly: list[tuple[object, str, str, Decimal]] = field(default_factory=list)
    totals: dict[str, Decimal] = field(default_factory=dict)
    top_models: list[tuple[int, str, str, str, Decimal, Decimal]] = field(default_factory=list)
    by_workload: list[tuple[str, Decimal, Decimal, int, float, float]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    real_builds: int = 0
    replayed_builds: int = 0
    days: int = 0


def gather(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    nodes: Mapping[str, Node],
    resolutions: Mapping[str, Resolution],
    window_days: int,
) -> ReportData:
    data = ReportData(days=window_days)
    data.showback = [
        (str(t), str(m), Decimal(p), Decimal(c), Decimal(total))
        for t, m, p, c, total in connection.execute(
            f"""SELECT team, pricing_model, sum(production_usd), sum(consumption_usd),
                       sum(total_usd)
                FROM {OPS_SCHEMA}.cost_by_team GROUP BY 1, 2 ORDER BY 1, 2"""
        ).fetchall()
    ]
    data.monthly = [
        (month, str(t), str(m), Decimal(total))
        for month, t, m, total in connection.execute(
            f"""SELECT month, team, pricing_model, total_usd
                FROM {OPS_SCHEMA}.cost_by_team ORDER BY 1, 2, 3"""
        ).fetchall()
    ]
    for model, total in connection.execute(
        f"SELECT model, sum(usd) FROM {OPS_SCHEMA}.query_costs GROUP BY 1 ORDER BY 1"
    ).fetchall():
        data.totals[str(model)] = Decimal(total)

    costs = recommend.production_costs(connection)
    models = [
        (c.get("scan", Decimal(0)), c.get("compute", Decimal(0)), uid)
        for uid, c in costs.items()
        if uid in nodes and nodes[uid].resource_type == "model"
    ]
    for rank, (scan, compute, uid) in enumerate(
        sorted(models, key=lambda m: (-m[0], m[2]))[:10], start=1
    ):
        rule = resolutions[uid].rule if uid in resolutions else None
        data.top_models.append(
            (rank, uid, nodes[uid].relation, rule.team if rule else "unattributed", scan, compute)
        )

    rows = connection.execute(
        f"""
        WITH per_query AS (
            SELECT e.actor_type, e.bytes_scanned, e.modeled_ms,
                   sum(c.usd) FILTER (WHERE c.model = 'scan') AS scan_usd,
                   sum(c.usd) FILTER (WHERE c.model = 'compute') AS compute_usd,
                   sum(c.billed_ms) FILTER (WHERE c.model = 'compute') AS billed_ms
            FROM {OPS_SCHEMA}.query_estimates e
            JOIN {OPS_SCHEMA}.query_costs c USING (query_id)
            GROUP BY e.query_id, e.actor_type, e.bytes_scanned, e.modeled_ms
        )
        SELECT actor_type, sum(scan_usd), sum(compute_usd), sum(bytes_scanned),
               sum(modeled_ms) / 1000.0, sum(billed_ms) / 1000.0
        FROM per_query GROUP BY actor_type
        """
    ).fetchall()
    found = {str(r[0]): r for r in rows}
    for workload in WORKLOADS:
        if workload in found:
            _, scan, compute, scanned, busy, billed = found[workload]
            data.by_workload.append(
                (
                    workload,
                    Decimal(scan),
                    Decimal(compute),
                    int(scanned),
                    float(busy),
                    float(billed),
                )
            )

    for actor_type, n in connection.execute(
        f"SELECT actor_type, count(*) FROM {OPS_SCHEMA}.query_log GROUP BY 1 ORDER BY 1"
    ).fetchall():
        data.counts[str(actor_type)] = int(n)
    runs = connection.execute(
        f"""SELECT count(DISTINCT run_id) FILTER (WHERE NOT replayed),
                   count(DISTINCT run_id) FILTER (WHERE replayed)
            FROM {OPS_SCHEMA}.query_log WHERE actor_type = 'dbt'"""
    ).fetchone()
    if runs:
        data.real_builds, data.replayed_builds = int(runs[0]), int(runs[1])
    return data


def render(
    settings: Settings,
    data: ReportData,
    unused: list[recommend.UnusedTable],
    spots: list[recommend.Hotspot],
    incremental: list[recommend.IncrementalCandidate],
) -> str:
    sim = settings.simulation
    scan = settings.pricing.scan
    compute = settings.pricing.compute
    minimum_mb = scan.minimum_billed_bytes_per_table // (1024 * 1024)
    end = sim.start.date().fromordinal(sim.start.date().toordinal() + data.days - 1)
    out: list[str] = []
    out.append("# Cost report")
    out.append("")
    out.append(
        f"Simulated window: {sim.start:%Y-%m-%d} to {end:%Y-%m-%d} ({data.days} days), "
        f"scale factor {settings.scale_factor}. "
        f"{data.counts.get('dbt', 0):,} dbt queries ({data.real_builds} real builds, "
        f"{data.replayed_builds} replayed days), {data.counts.get('dashboard', 0):,} dashboard "
        f"queries and {data.counts.get('adhoc', 0):,} ad hoc queries."
    )
    out.append("")
    out.append(
        "**How the numbers are made.** Bytes scanned are the uncompressed size of every column a "
        "query reads, whatever its filter, which is how BigQuery bills. Compute time is modelled "
        f"as {settings.cost.per_query_overhead_ms} ms per query plus bytes at "
        f"{settings.cost.throughput_bytes_per_second / 1e6:,.0f} MB/s; both values are declared "
        "assumptions, not vendor figures (docs/adr/0005). Scan pricing: "
        f"${scan.usd_per_tib:.2f}/TiB with {minimum_mb} MB "
        "billed at least per table referenced. Compute pricing: "
        f"${compute.usd_per_credit:.2f}/credit, {compute.credits_per_hour:g} credit/hour, "
        f"{compute.minimum_billed_seconds} s minimum per start, suspends after "
        f"{compute.idle_timeout_seconds} s idle, one warehouse per workload."
    )
    out.append("")

    out.append("## Showback by team")
    out.append("")
    teams = sorted({t for t, *_ in data.showback})
    by = {(t, m): (p, c, total) for t, m, p, c, total in data.showback}
    rows = []
    for team in teams:
        s = by.get((team, "scan"), (Decimal(0),) * 3)
        c = by.get((team, "compute"), (Decimal(0),) * 3)
        rows.append([team, usd(s[0]), usd(s[1]), usd(s[2]), usd(c[0]), usd(c[1]), usd(c[2])])
    rows.append(
        [
            "**total**",
            "",
            "",
            usd(data.totals.get("scan", 0)),
            "",
            "",
            usd(data.totals.get("compute", 0)),
        ]
    )
    out.append(
        table(
            [
                "Team",
                "Scan: production",
                "Scan: consumption",
                "Scan: total",
                "Compute: production",
                "Compute: consumption",
                "Compute: total",
            ],
            rows,
            right=range(1, 7),
        )
    )
    out.append("")
    out.append(
        "Production is what it costs to build and test the tables a team owns. "
        "Consumption is what a team's dashboards and people spend reading data, "
        "whoever owns it. Each query is charged exactly once."
    )
    out.append("")
    out.append("### By month")
    out.append("")
    months = sorted({str(m) for m, *_ in data.monthly})
    monthly = {(str(m), t, p): v for m, t, p, v in data.monthly}
    month_rows = []
    for team in teams:
        cells = [team]
        for month in months:
            cells += [
                usd(monthly.get((month, team, "scan"), 0)),
                usd(monthly.get((month, team, "compute"), 0)),
            ]
        month_rows.append(cells)
    headers = ["Team"] + [f"{m[:7]} {p}" for m in months for p in ("scan", "compute")]
    out.append(table(headers, month_rows, right=range(1, len(headers))))
    out.append("")

    out.append("## Top 10 most expensive models")
    out.append("")
    out.append("Build plus test cost over the window, ranked by scan pricing.")
    out.append("")
    out.append(
        table(
            ["#", "Model", "Team", "Scan", "Compute"],
            [[r, rel, team, usd(s), usd(c)] for r, _, rel, team, s, c in data.top_models],
            right=(0, 3, 4),
        )
    )
    out.append("")

    out.append("## Unused tables")
    out.append("")
    out.append(
        "Built during the window, but neither the table nor anything downstream of it was "
        "read by a dashboard or a person in the lookback period. Savings are the monthly "
        "cost of building and testing them."
    )
    out.append("")
    for days in sorted({u.lookback_days for u in unused}):
        subset = [u for u in unused if u.lookback_days == days]
        saving_scan = sum((u.monthly_saving["scan"] for u in subset), Decimal(0))
        saving_compute = sum((u.monthly_saving["compute"] for u in subset), Decimal(0))
        out.append(f"### Not read in the last {days} days: {len(subset)} tables")
        out.append("")
        out.append(
            table(
                ["Table", "Team", "Owner", "Monthly saving (scan)", "Monthly saving (compute)"],
                [
                    [
                        u.relation,
                        u.team,
                        u.owner,
                        usd(u.monthly_saving["scan"]),
                        usd(u.monthly_saving["compute"]),
                    ]
                    for u in subset
                ]
                + [["**total**", "", "", usd(saving_scan), usd(saving_compute)]],
                right=(3, 4),
            )
        )
        out.append("")

    out.append("## Recommendations")
    out.append("")
    out.append("### Full-scan hotspots")
    out.append("")
    if spots:
        out.append(
            "Large tables that dashboards and people filter on the same column over and over, "
            "scanning the whole column every time. Partition or cluster on that column."
        )
        out.append("")
        out.append(
            table(
                [
                    "Table",
                    "Filtered on",
                    "Reads",
                    "Table size",
                    "Scanned by those reads",
                    "Scan cost",
                ],
                [
                    [
                        h.table,
                        h.column,
                        f"{h.reads:,}",
                        gigabytes(h.table_bytes),
                        gigabytes(h.bytes_scanned),
                        usd(h.scan_usd),
                    ]
                    for h in spots
                ],
                right=(2, 3, 4, 5),
            )
        )
    else:
        out.append("None above the configured thresholds.")
    out.append("")
    out.append("### Incremental candidates")
    out.append("")
    out.append(
        f"The {len(incremental)} most expensive live models to build (scan pricing), checked "
        "against their sources. Incremental builds are only safe when every source only "
        "ever gains rows."
    )
    out.append("")
    out.append(
        table(
            ["Model", "Team", "Build cost (scan)", "Build cost (compute)", "Verdict"],
            [
                [
                    c.relation,
                    c.team,
                    usd(c.production.get("scan", 0)),
                    usd(c.production.get("compute", 0)),
                    "candidate: all sources append-only"
                    if c.candidate
                    else "not safe: " + ", ".join(c.blocked_by) + " change in place",
                ]
                for c in incremental
            ],
            right=(2, 3),
        )
    )
    out.append("")

    out.append("## Scan pricing versus compute pricing")
    out.append("")
    rows = []
    for workload, scan_usd, compute_usd, scanned, busy, billed in data.by_workload:
        idle = 1 - busy / billed if billed else 0.0
        rows.append(
            [
                WORKLOAD_LABEL[workload],
                usd(scan_usd),
                usd(compute_usd),
                gigabytes(scanned),
                f"{busy / 3600:,.1f} h",
                f"{billed / 3600:,.1f} h",
                f"{idle:.0%}",
            ]
        )
    out.append(
        table(
            [
                "Workload",
                "Scan",
                "Compute",
                "Bytes scanned",
                "Busy time",
                "Billed time",
                "Idle share",
            ],
            rows,
            right=range(1, 7),
        )
    )
    out.append("")
    out.append(
        "Busy time is the modelled time queries were running; billed time adds each "
        "warehouse's minimum charge per start and the idle minutes before it suspends."
    )
    out.append("")
    out.append(
        "How close the bytes estimate is to what DuckDB actually scanned is in "
        "`cost_proxy_accuracy.md`, kept separate because profiler counts are not "
        "guaranteed to be identical between runs."
    )
    out.append("")
    return "\n".join(out)


def proxy_accuracy(
    connection: duckdb.DuckDBPyConnection,
) -> list[tuple[str, int, int, int, float, float]]:
    """Per workload: rows the proxy assumes scanned versus rows DuckDB's profiler saw."""
    rows = connection.execute(
        f"""
        WITH table_rows AS (
            SELECT DISTINCT snapshot_at, table_name, row_count FROM {OPS_SCHEMA}.table_sizes
        ),
        reads AS (
            SELECT qt.query_id, qt.table_name, ql.started_at
            FROM {OPS_SCHEMA}.query_tables qt
            JOIN {OPS_SCHEMA}.query_log ql USING (query_id)
            WHERE ql.rows_scanned IS NOT NULL
        ),
        proxy AS (
            SELECT r.query_id, sum(tr.row_count) AS proxy_rows
            FROM reads r
            ASOF JOIN table_rows tr
              ON tr.table_name = r.table_name AND r.started_at >= tr.snapshot_at
            GROUP BY r.query_id
        )
        SELECT ql.actor_type,
               count(*) AS queries,
               sum(p.proxy_rows) AS proxy_rows,
               sum(ql.rows_scanned) AS scanned_rows,
               median(p.proxy_rows / ql.rows_scanned) AS median_ratio,
               avg(CASE WHEN abs(p.proxy_rows - ql.rows_scanned) <= 0.1 * ql.rows_scanned
                        THEN 1.0 ELSE 0.0 END) AS within_10pct
        FROM {OPS_SCHEMA}.query_log ql
        JOIN proxy p USING (query_id)
        WHERE ql.rows_scanned > 0
        GROUP BY ql.actor_type ORDER BY ql.actor_type
        """
    ).fetchall()
    return [(str(a), int(q), int(p), int(s), float(m), float(w)) for a, q, p, s, m, w in rows]


def render_accuracy(
    settings: Settings, accuracy: list[tuple[str, int, int, int, float, float]]
) -> str:
    out = ["# How close is the bytes estimate?", ""]
    out.append(
        "The estimate assumes a query reads every row of every table it references, the "
        "way on-demand warehouses bill. DuckDB can skip whole row groups when a filter "
        "rules them out, so it often scans fewer. This compares the rows the estimate "
        "assumes with the rows DuckDB's profiler reported, for queries that ran to "
        "completion (a result cut short at the first page has no final count)."
    )
    out.append("")
    out.append(
        table(
            [
                "Workload",
                "Queries",
                "Rows assumed",
                "Rows scanned",
                "Median overestimate",
                "Within 10%",
            ],
            [
                [WORKLOAD_LABEL.get(a, a), f"{q:,}", f"{p:,}", f"{s:,}", f"{m:.2f}x", f"{w:.0%}"]
                for a, q, p, s, m, w in accuracy
            ],
            right=range(1, 6),
        )
    )
    out.append("")
    out.append(
        "Not part of `cost.md` on purpose: profiler counts depend on physical row order, "
        "which DuckDB does not guarantee to be identical between runs."
    )
    out.append("")
    return "\n".join(out)
