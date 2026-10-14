"""``reports/cost.md``: who spends what on the company's ClickHouse, and what to change.

The assumptions come first, because every number after them depends on them:
the stated monthly prices and their sizing, and how the two cloud alternatives
are priced. Nothing below is timed or random, so two runs write byte-identical
files (version 2.0, Phase 13).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from platform_ops.common.config import ACTOR_TYPES
from platform_ops.common.markdown import table

if TYPE_CHECKING:
    from platform_ops.cost.run import CostData

MODEL_LABEL = {
    "cluster": "ClickHouse (stated cost)",
    "scan": "BigQuery on demand",
    "compute": "Snowflake warehouses",
}
ACTOR_LABEL = {
    "dbt": "dbt builds and tests",
    "ingestion": "ingestion from Postgres",
    "maintenance": "nightly merge of raw",
    "reconcile": "parallel run reconciliation",
    "dashboard": "dashboards (Metabase)",
    "adhoc": "ad hoc SQL",
}


def usd(value: Decimal | float | int) -> str:
    amount = Decimal(str(value))
    if amount == 0:
        return "$0.00"
    return f"${amount:,.2f}" if abs(amount) >= 1 else f"${amount:.4f}"


def size(value: int | float) -> str:
    """Bytes in MB below a gigabyte and in GB above, so small runs stay readable."""
    amount = Decimal(str(value))
    if amount < 10**9:
        return f"{amount / Decimal(10**6):,.1f} MB"
    return f"{amount / Decimal(10**9):,.2f} GB"


def _sum(values: object) -> Decimal:
    return sum(values, Decimal(0))  # type: ignore[call-overload,no-any-return]


def render(data: CostData) -> str:
    s = data.settings
    end = data.window_start + timedelta(days=data.window_days - 1)
    billable = [r for r in data.records if r.billable]
    by_id = {r.query_id: r for r in data.records}
    ch, legacy = s.cost.clickhouse, s.cost.legacy_reporting

    out = ["# Cost and FinOps: the company on ClickHouse", ""]
    out += [
        f"{data.window_days} simulated days, {data.window_start:%Y-%m-%d} to {end:%Y-%m-%d}. "
        f"Every query the company ran on ClickHouse was read back from its query log, and the "
        f"{len(billable):,} that read or wrote data are priced. Statements that only changed "
        "the catalogue or read ClickHouse's own system tables are left out, as no warehouse "
        "bills them.",
        "",
        "## Assumptions",
        "",
        table(
            ["What", "Assumption"],
            [
                (
                    "ClickHouse, self-managed",
                    f"{ch.nodes} node at {usd(ch.instance_usd_per_month)}/month and "
                    f"{ch.storage_gb} GB at {usd(ch.storage_usd_per_gb_month)}/GB-month: "
                    f"{usd(ch.compute_usd + ch.storage_usd)}/month",
                ),
                (
                    "Legacy reporting replica",
                    f"{legacy.nodes} RDS instance at "
                    f"{usd(legacy.instance_usd_per_month)}/month and "
                    f"{legacy.storage_gb} GB at {usd(legacy.storage_usd_per_gb_month)}/GB-month: "
                    f"{usd(legacy.compute_usd + legacy.storage_usd)}/month",
                ),
                ("Compute share", "bytes each query read, as ClickHouse measured them"),
                ("Storage share", "uncompressed bytes of each table at the month's end"),
                (
                    "BigQuery",
                    f"{usd(s.pricing.scan.usd_per_tib)} per TiB read, at least "
                    f"{s.pricing.scan.minimum_billed_bytes_per_table // 2**20} MB per table "
                    "(a query's bytes split evenly over the tables it read)",
                ),
                (
                    "Snowflake",
                    f"{usd(s.pricing.compute.usd_per_credit)} per credit, "
                    f"{s.pricing.compute.credits_per_hour:g} credit/hour, "
                    f"{s.pricing.compute.minimum_billed_seconds} s minimum, "
                    f"{s.pricing.compute.idle_timeout_seconds} s idle before suspending",
                ),
                (
                    "Modelled time",
                    f"{s.cost.per_query_overhead_ms} ms + bytes read at "
                    f"{s.cost.throughput_bytes_per_second // 10**6} MB/s",
                ),
            ],
        ),
        "",
        "Prices are public on-demand list prices (us-east-1); sources are next to them in "
        "`config/settings.yaml`.",
        "",
    ]

    compute_total = _sum(data.costs["cluster"].values())
    storage_total = _sum(data.storage.values())
    pool_total = _sum(p.total_usd for p in data.pools)
    out += ["## The cluster's bill", ""]
    month_compute: dict[str, Decimal] = defaultdict(Decimal)
    for query_id, value in data.costs["cluster"].items():
        month_compute[f"{by_id[query_id].started_at:%Y-%m}"] += value
    month_storage: dict[str, Decimal] = defaultdict(Decimal)
    for (month, _), value in data.storage.items():
        month_storage[month] += value
    out += [
        table(
            ["Month", "Days", "Compute", "Storage", "Stated cost", "Shared out"],
            [
                (
                    p.month,
                    p.days,
                    usd(p.compute_usd),
                    usd(p.storage_usd),
                    usd(p.total_usd),
                    usd(month_compute[p.month] + month_storage[p.month]),
                )
                for p in data.pools
            ]
            + [
                (
                    "**total**",
                    data.window_days,
                    usd(_sum(p.compute_usd for p in data.pools)),
                    usd(_sum(p.storage_usd for p in data.pools)),
                    usd(pool_total),
                    usd(compute_total + storage_total),
                )
            ],
            right=(1, 2, 3, 4, 5),
        ),
        "",
        "Every dollar of the stated cost is shared out: the shares add up to "
        + (
            "the bill exactly."
            if compute_total + storage_total == pool_total
            else f"{usd(compute_total + storage_total)}, not {usd(pool_total)}."
        ),
        "",
    ]

    teams: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for model, by_query in data.costs.items():
        for query_id, value in by_query.items():
            key = "cluster compute" if model == "cluster" else model
            teams[data.attributions[query_id].team][key] += value
    for (_, table_name), value in data.storage.items():
        teams[data.storage_owner[table_name][0]]["cluster storage"] += value
    rows = []
    for team, values in sorted(
        teams.items(),
        key=lambda kv: (-(kv[1]["cluster compute"] + kv[1]["cluster storage"]), kv[0]),
    ):
        rows.append(
            (
                team,
                usd(values["cluster compute"]),
                usd(values["cluster storage"]),
                usd(values["cluster compute"] + values["cluster storage"]),
                usd(values["scan"]),
                usd(values["compute"]),
            )
        )
    totals = {
        k: _sum(v[k] for v in teams.values())
        for k in ("cluster compute", "cluster storage", "scan", "compute")
    }
    rows.append(
        (
            "**total**",
            usd(totals["cluster compute"]),
            usd(totals["cluster storage"]),
            usd(totals["cluster compute"] + totals["cluster storage"]),
            usd(totals["scan"]),
            usd(totals["compute"]),
        )
    )
    out += [
        "## Showback by team",
        "",
        "Production cost (builds, loads, the nightly merge, storage) goes to the owner of "
        "the table; consumption (dashboards, ad hoc SQL) to the reader; the parallel run's "
        "reconciliation to the platform team, which runs the migration.",
        "",
        table(
            [
                "Team",
                "ClickHouse compute",
                "ClickHouse storage",
                "ClickHouse total",
                MODEL_LABEL["scan"],
                MODEL_LABEL["compute"],
            ],
            rows,
            right=(1, 2, 3, 4, 5),
        ),
        "",
    ]

    workload: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    counts: dict[str, int] = defaultdict(int)
    read: dict[str, int] = defaultdict(int)
    for r in billable:
        counts[r.actor_type] += 1
        read[r.actor_type] += r.bytes_scanned or 0
        for model in data.costs:
            workload[r.actor_type][model] += data.costs[model].get(r.query_id, Decimal(0))
    out += [
        "## By workload",
        "",
        table(
            [
                "Workload",
                "Queries",
                "Read",
                "ClickHouse compute",
                MODEL_LABEL["scan"],
                MODEL_LABEL["compute"],
            ],
            [
                (
                    ACTOR_LABEL[a],
                    f"{counts[a]:,}",
                    size(read[a]),
                    usd(workload[a]["cluster"]),
                    usd(workload[a]["scan"]),
                    usd(workload[a]["compute"]),
                )
                for a in ACTOR_TYPES
                if counts[a]
            ],
            right=(1, 2, 3, 4, 5),
        ),
        "",
    ]

    case = data.case
    out += ["## The migration's business case", ""]
    cut = f"{case.cutover:%Y-%m-%d}" if case.cutover else "not recommended in the window"
    out += [
        f"The legacy Postgres stays as the operational database; the migration retires its "
        f"reporting replica at cutover ({cut}). ClickHouse runs for the whole window.",
        "",
        table(
            ["Month", "Days", "Legacy replica", "ClickHouse", "Of which reconciliation"],
            [
                (
                    m.month,
                    m.days,
                    f"{usd(m.legacy_usd)} ({m.legacy_days} days)" if m.legacy_days else usd(0),
                    usd(m.clickhouse_usd),
                    usd(m.reconcile_usd),
                )
                for m in case.months
            ],
            right=(1, 2, 3, 4),
        ),
        "",
        table(
            ["", "Per month"],
            [
                ("Before: the reporting replica", usd(case.legacy_monthly)),
                ("After: ClickHouse", usd(case.clickhouse_monthly)),
                ("Saving", usd(case.monthly_saving)),
            ],
            right=(1,),
        ),
        "",
    ]
    if case.monthly_saving <= 0:
        out += ["At these prices ClickHouse does not cost less than the replica it replaces.", ""]
    elif case.cutover is None:
        out += [
            f"The reconciliation did not clear the cutover in the window, so the company still "
            f"pays for both: {usd(case.overlap_usd)} of ClickHouse so far on top of the replica. "
            f"The saving starts the day the replica is retired.",
            "",
        ]
    else:
        out += [
            f"Running both until cutover cost {usd(case.overlap_usd)} of ClickHouse on top of the "
            f"replica; the saving pays that back in {case.payback_months} months.",
            "",
        ]

    out += ["## What to change", ""]
    previous: list[str] | None = None
    for days in sorted(s.recommendations.unused_lookback_days):
        mine = [u for u in data.unused if u.lookback_days == days]
        out += [f"### Unused tables, last {days} days", ""]
        if not mine:
            out += ["None.", ""]
            previous = []
            continue
        names = [u.unique_id for u in mine]
        if names == previous:
            out += [f"The same {len(mine)} tables.", ""]
            continue
        previous = names
        out += [
            table(
                [
                    "Model",
                    "Team",
                    "Owner",
                    "ClickHouse/month",
                    f"{MODEL_LABEL['scan']}/month",
                    f"{MODEL_LABEL['compute']}/month",
                ],
                [
                    (
                        u.relation,
                        u.team,
                        u.owner,
                        usd(u.monthly_saving.get("cluster", 0)),
                        usd(u.monthly_saving.get("scan", 0)),
                        usd(u.monthly_saving.get("compute", 0)),
                    )
                    for u in mine
                ],
                right=(3, 4, 5),
            ),
            "",
        ]
    out += ["### Full-scan hotspots", ""]
    if data.hotspots:
        out += [
            "Dashboards and ad hoc SQL that filter a large table and still read nearly all of it.",
            "",
            table(
                [
                    "Table",
                    "Filtered on",
                    "Queries",
                    "Rows read",
                    "Read",
                    "Sort key now",
                    "Suggested",
                ],
                [
                    (
                        h.table,
                        h.column,
                        f"{h.reads:,}",
                        f"{h.rows_scanned:,}",
                        size(h.bytes_scanned),
                        f"`{h.sorting_key}`" if h.sorting_key else "(none)",
                        h.suggestion,
                    )
                    for h in data.hotspots
                ],
                right=(2, 3, 4),
            ),
            "",
        ]
    else:
        out += ["None above the thresholds.", ""]
    out += ["### Incremental candidates", ""]
    appendable = sum(1 for c in data.incremental if c.candidate)
    out += [
        f"The {len(data.incremental)} models still rebuilt in full that cost the most. One can "
        f"switch to appending new rows only if none of its sources change rows in place, as "
        f"the legacy database's own updates show.",
        "",
        table(
            ["Model", "Team", "ClickHouse", "Sources", "Verdict"],
            [
                (
                    c.relation,
                    c.team,
                    usd(c.production.get("cluster", 0)),
                    ", ".join(c.sources),
                    "candidate"
                    if c.candidate
                    else f"no: {', '.join(c.blocked_by)} change in place",
                )
                for c in data.incremental
            ],
            right=(2,),
        ),
        "",
    ]
    if data.incremental and not appendable:
        out += [
            "None of them can simply append: each joins a source whose rows change in place, so "
            "an incremental version would have to delete and insert by key over a lookback "
            "window to pick those changes up.",
            "",
        ]
    out += ["### Suggested quotas", ""]
    out += [
        "A daily read quota at twice each user's 95th-percentile day, and a per-query limit at "
        "twice its largest query. Suggestions for the platform team, not applied. The users "
        "that also build, load or reconcile get none: a limit there could stop the pipeline.",
        "",
        table(
            ["User", "Active days", "p95 day", "Largest query", "Daily quota", "Per query"],
            [
                (
                    q.user,
                    q.active_days,
                    size(q.p95_daily_bytes),
                    size(q.max_query_bytes),
                    size(q.daily_quota_bytes),
                    size(q.query_limit_bytes),
                )
                for q in data.quotas
            ],
            right=(1, 2, 3, 4, 5),
        ),
        "",
        "```sql",
        *[statement + ";" for q in data.quotas for statement in q.statements],
        "```",
        "",
    ]
    return "\n".join(out)
