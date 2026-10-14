"""``make cost``: collect the company's run, price it three ways, attribute it, report it.

Runs over whatever the last company run left in ClickHouse's query log and the
ops store (``make demo``), and can be rerun on its own: ownership is read from
the registry each time, so a change to ``config/ownership.yaml`` shows up in
the next report without replaying the company.

Three prices for one measured workload (version 2.0, Phase 13):

- ``cluster``: what the company actually pays, its self-managed ClickHouse's
  stated monthly cost, shared by bytes read (compute) and bytes stored;
- ``scan``: what the same queries would cost on BigQuery on demand;
- ``compute``: what they would cost on Snowflake warehouses (ADR 0005, 0006).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.common.servers import clickhouse_client
from platform_ops.common.store import open_store, ops_table, replace_ops_table
from platform_ops.cost import cluster, recommend, report
from platform_ops.cost.attribution import Attribution, Owners, attribute
from platform_ops.cost.business import BusinessCase, business_case
from platform_ops.cost.collect import (
    ACTOR_USERS,
    QueryRecord,
    clickhouse_records,
    maintenance_records,
    store,
)
from platform_ops.cost.pricing import ComputePricing, EstimatedQuery, ScanPricing
from platform_ops.metadata.lineage import build_graph
from platform_ops.metadata.manifest import load_manifest
from platform_ops.metadata.registry import Registry

MODELS = ("cluster", "scan", "compute")


class CostError(RuntimeError):
    """The cost report cannot be produced from the current state."""


@dataclass
class CostData:
    """Everything the report shows, from one collected run."""

    settings: Settings
    window_start: datetime
    window_days: int
    records: list[QueryRecord]
    costs: dict[str, dict[str, Decimal]]  # model -> query_id -> usd
    attributions: dict[str, Attribution]
    storage: dict[tuple[str, str], Decimal]  # (month, table) -> usd
    storage_owner: dict[str, tuple[str, str]]  # table -> (team, owner)
    pools: list[cluster.MonthPool]
    case: BusinessCase
    unused: list[recommend.UnusedTable] = field(default_factory=list)
    hotspots: list[recommend.Hotspot] = field(default_factory=list)
    incremental: list[recommend.IncrementalCandidate] = field(default_factory=list)
    quotas: list[recommend.QuotaSuggestion] = field(default_factory=list)


@dataclass(frozen=True)
class CostSummary:
    queries: int
    report_path: Path
    total_usd: dict[str, Decimal]
    unused: int


def _company_run(settings: Settings) -> tuple[str, datetime, int]:
    with open_store(settings) as connection:
        found = connection.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            [f'"{settings.company.name(settings.company.ops_schema)}".company_run'],
        ).fetchone()
        if not found or not found[0]:
            raise CostError("no company run is recorded; run `make demo` first")
        row = connection.execute(
            sql.SQL("SELECT run_id, window_start, days FROM {}").format(
                ops_table(settings, "company_run")
            )
        ).fetchone()
    if row is None:
        raise CostError("no company run is recorded; run `make demo` first")
    return str(row[0]), row[1], int(row[2])


def _rows(settings: Settings, table: str, columns: str, order: str) -> list[tuple[Any, ...]]:
    with open_store(settings) as connection:
        exists = connection.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            [f'"{settings.company.name(settings.company.ops_schema)}".{table}'],
        ).fetchone()
        if not exists or not exists[0]:
            return []
        return list(
            connection.execute(
                sql.SQL(f"SELECT {columns} FROM {{}} ORDER BY {order}").format(
                    ops_table(settings, table)
                )
            ).fetchall()
        )


def _estimated(record: QueryRecord, settings: Settings) -> EstimatedQuery:
    model = settings.cost
    read = record.bytes_scanned or 0
    modeled_ms = model.per_query_overhead_ms + (read * 1000) // model.throughput_bytes_per_second
    tables = record.tables or ("",)
    part, extra = divmod(read, len(tables))
    table_bytes = tuple(part + (extra if i == 0 else 0) for i in range(len(tables)))
    return EstimatedQuery(
        record.query_id, record.actor_type, record.started_at, modeled_ms, table_bytes
    )


def _materialized(manifest_path: Path) -> dict[str, str]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        uid: str((raw.get("config") or {}).get("materialized", ""))
        for uid, raw in data.get("nodes", {}).items()
        if raw.get("resource_type") == "model"
    }


def gather(settings: Settings) -> CostData:
    run_id, start, days = _company_run(settings)
    log = clickhouse_records(settings, run_id)
    for record in maintenance_records(settings, run_id):
        log.add(record)
    store(settings, log)

    manifest_path = invocation_from_settings(settings).manifest_path
    nodes = load_manifest(manifest_path)
    registry = Registry.from_config_dir(settings.root / "config")
    owners = Owners(nodes, registry)
    graph = build_graph(nodes)
    namespace = settings.company.namespace

    billable = [r for r in log.records if r.billable]
    estimated = [_estimated(r, settings) for r in billable]
    costs: dict[str, dict[str, Decimal]] = {model: {} for model in MODELS}
    for priced in ScanPricing(settings.pricing.scan).price(estimated):
        costs["scan"][priced.query_id] = priced.usd
    for priced in ComputePricing(settings.pricing.compute).price(estimated):
        costs["compute"][priced.query_id] = priced.usd

    month_pools = cluster.pools(settings.cost.clickhouse, cluster.window_months(start, days))
    by_month = {p.month: p for p in month_pools}
    costs["cluster"] = cluster.compute_shares(
        by_month,
        ((r.query_id, f"{r.started_at:%Y-%m}", r.bytes_scanned or 0) for r in billable),
    )
    snapshots = _rows(
        settings,
        "storage_snapshots",
        "month, database_name, table_name, rows, bytes",
        "month, database_name, table_name",
    )
    storage = cluster.storage_shares(
        by_month, ((m, f"{db}.{t}", int(b)) for m, db, t, _, b in snapshots)
    )
    relations = {rel.removeprefix(namespace): uid for rel, uid in owners.by_relation().items()}
    storage_owner = {table: owners.of(relations.get(table)) for _, table in storage}
    attributions = attribute(log.records, owners)

    production: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for model, by_query in costs.items():
        for query_id, usd in by_query.items():
            a = attributions[query_id]
            if a.cost_kind == "production" and a.subject:
                production[a.subject][model] += usd
    for (_, table), usd in storage.items():
        uid = relations.get(table)
        if uid:
            production[uid]["cluster"] += usd

    cutover_rows = _rows(settings, "reconcile_cutover", "recommended, run_at", "recommended")
    cutover = cutover_rows[0][1] if cutover_rows and cutover_rows[0][0] else None
    reconcile_by_month: dict[str, Decimal] = defaultdict(Decimal)
    for r in billable:
        if r.actor_type == "reconcile":
            reconcile_by_month[f"{r.started_at:%Y-%m}"] += costs["cluster"].get(
                r.query_id, Decimal(0)
            )
    case = business_case(
        settings.cost.clickhouse,
        settings.cost.legacy_reporting,
        start,
        days,
        cutover,
        reconcile_by_month,
    )

    data = CostData(
        settings=settings,
        window_start=start,
        window_days=days,
        records=log.records,
        costs=costs,
        attributions=attributions,
        storage=storage,
        storage_owner=storage_owner,
        pools=month_pools,
        case=case,
    )
    window_end = start + timedelta(days=days)
    data.unused = recommend.unused_tables(
        log.records,
        owners,
        graph,
        production,
        window_end,
        days,
        settings.recommendations.unused_lookback_days,
    )
    last_month = max((m for m, *_ in snapshots), default="")
    table_rows = {f"{namespace}{db}.{t}": int(n) for m, db, t, n, _ in snapshots if m == last_month}
    table_bytes = {
        f"{namespace}{db}.{t}": int(b) for m, db, t, _, b in snapshots if m == last_month
    }
    client = clickhouse_client(settings)
    try:
        from platform_ops.simulation.company import company_databases

        tables = client.query(
            "SELECT database, name, sorting_key, engine FROM system.tables "
            "WHERE database IN %(dbs)s",
            parameters={"dbs": company_databases(settings)},
        ).result_rows
        sorting_keys = {f"{db}.{name}": str(key) for db, name, key, _ in tables}
        engines = {f"{db}.{name}": str(engine) for db, name, _, engine in tables}
    finally:
        client.close()
    data.hotspots = recommend.hotspots(
        log.records,
        log.filters,
        table_rows,
        table_bytes,
        sorting_keys,
        settings.recommendations.hotspot_min_table_bytes,
        settings.recommendations.hotspot_min_filtered_reads,
        engines,
    )
    changed = {str(t) for (t,) in _rows(settings, "source_changes", "DISTINCT table_name", "1")}
    abandoned = {
        uid for uid, n in nodes.items() if n.resource_type == "model" and n.folder == "abandoned"
    }
    data.incremental = recommend.incremental_candidates(
        owners,
        graph,
        production,
        _materialized(manifest_path),
        changed,
        "cluster",
        settings.recommendations.incremental_top_n,
        abandoned,
    )
    team_of_user = {
        person: team
        for person in settings.workload.adhoc_users
        if (team := registry.team_of(person))
    }
    data.quotas = recommend.quotas(
        log.records, team_of_user, namespace, exclude=set(ACTOR_USERS.values())
    )
    return data


SHOWBACK_DDL = """
    month text NOT NULL, team text NOT NULL, model text NOT NULL, cost_kind text NOT NULL,
    actor_type text NOT NULL, usd numeric(30, 12) NOT NULL"""


def _derived(data: CostData) -> list[tuple[str, str, list[str], list[tuple[Any, ...]]]]:
    """What the report shows beyond the per-query facts, as ops tables, so the
    dashboards read the same numbers instead of computing them again."""
    by_id = {r.query_id: r for r in data.records}
    shared: dict[str, Decimal] = defaultdict(Decimal)
    for query_id, usd in data.costs["cluster"].items():
        shared[f"{by_id[query_id].started_at:%Y-%m}"] += usd
    for (month, _), usd in data.storage.items():
        shared[month] += usd
    case = data.case
    return [
        (
            "cost_months",
            "month text PRIMARY KEY, days integer NOT NULL, compute_usd numeric(30, 12) NOT NULL, "
            "storage_usd numeric(30, 12) NOT NULL, stated_usd numeric(30, 12) NOT NULL, "
            "shared_usd numeric(30, 12) NOT NULL",
            ["month", "days", "compute_usd", "storage_usd", "stated_usd", "shared_usd"],
            [
                (p.month, p.days, p.compute_usd, p.storage_usd, p.total_usd, shared[p.month])
                for p in data.pools
            ],
        ),
        (
            "cost_business_case",
            "month text PRIMARY KEY, days integer NOT NULL, legacy_days integer NOT NULL, "
            "legacy_usd numeric(30, 12) NOT NULL, clickhouse_usd numeric(30, 12) NOT NULL, "
            "reconcile_usd numeric(30, 12) NOT NULL",
            ["month", "days", "legacy_days", "legacy_usd", "clickhouse_usd", "reconcile_usd"],
            [
                (m.month, m.days, m.legacy_days, m.legacy_usd, m.clickhouse_usd, m.reconcile_usd)
                for m in case.months
            ],
        ),
        (
            "cost_business_summary",
            "cutover timestamp, legacy_monthly_usd numeric(30, 12) NOT NULL, "
            "clickhouse_monthly_usd numeric(30, 12) NOT NULL, "
            "monthly_saving_usd numeric(30, 12) NOT NULL, overlap_usd numeric(30, 12) NOT NULL, "
            "payback_months numeric(10, 1)",
            [
                "cutover",
                "legacy_monthly_usd",
                "clickhouse_monthly_usd",
                "monthly_saving_usd",
                "overlap_usd",
                "payback_months",
            ],
            [
                (
                    case.cutover,
                    case.legacy_monthly,
                    case.clickhouse_monthly,
                    case.monthly_saving,
                    case.overlap_usd,
                    case.payback_months,
                )
            ],
        ),
        (
            "cost_unused",
            "lookback_days integer NOT NULL, unique_id text NOT NULL, relation text NOT NULL, "
            "team text NOT NULL, owner text NOT NULL, cluster_usd_month numeric(30, 12), "
            "scan_usd_month numeric(30, 12), compute_usd_month numeric(30, 12)",
            [
                "lookback_days",
                "unique_id",
                "relation",
                "team",
                "owner",
                "cluster_usd_month",
                "scan_usd_month",
                "compute_usd_month",
            ],
            [
                (
                    u.lookback_days,
                    u.unique_id,
                    u.relation,
                    u.team,
                    u.owner,
                    u.monthly_saving.get("cluster", Decimal(0)),
                    u.monthly_saving.get("scan", Decimal(0)),
                    u.monthly_saving.get("compute", Decimal(0)),
                )
                for u in data.unused
            ],
        ),
        (
            "cost_hotspots",
            "table_name text NOT NULL, column_name text NOT NULL, reads integer NOT NULL, "
            "table_rows bigint NOT NULL, rows_scanned bigint NOT NULL, "
            "bytes_scanned bigint NOT NULL, sorting_key text NOT NULL, engine text NOT NULL, "
            "suggestion text NOT NULL",
            [
                "table_name",
                "column_name",
                "reads",
                "table_rows",
                "rows_scanned",
                "bytes_scanned",
                "sorting_key",
                "engine",
                "suggestion",
            ],
            [
                (
                    h.table,
                    h.column,
                    h.reads,
                    h.table_rows,
                    h.rows_scanned,
                    h.bytes_scanned,
                    h.sorting_key,
                    h.engine,
                    h.suggestion,
                )
                for h in data.hotspots
            ],
        ),
        (
            "cost_incremental",
            "rank integer NOT NULL, unique_id text NOT NULL, relation text NOT NULL, "
            "team text NOT NULL, cluster_usd numeric(30, 12) NOT NULL, sources text NOT NULL, "
            "blocked_by text NOT NULL, candidate boolean NOT NULL",
            [
                "rank",
                "unique_id",
                "relation",
                "team",
                "cluster_usd",
                "sources",
                "blocked_by",
                "candidate",
            ],
            [
                (
                    rank,
                    c.unique_id,
                    c.relation,
                    c.team,
                    c.production.get("cluster", Decimal(0)),
                    ", ".join(c.sources),
                    ", ".join(c.blocked_by),
                    c.candidate,
                )
                for rank, c in enumerate(data.incremental, start=1)
            ],
        ),
        (
            "cost_quotas",
            "user_name text PRIMARY KEY, team text NOT NULL, active_days integer NOT NULL, "
            "p95_daily_bytes bigint NOT NULL, max_query_bytes bigint NOT NULL, "
            "daily_quota_bytes bigint NOT NULL, query_limit_bytes bigint NOT NULL, "
            "statements text NOT NULL",
            [
                "user_name",
                "team",
                "active_days",
                "p95_daily_bytes",
                "max_query_bytes",
                "daily_quota_bytes",
                "query_limit_bytes",
                "statements",
            ],
            [
                (
                    q.user,
                    q.team,
                    q.active_days,
                    q.p95_daily_bytes,
                    q.max_query_bytes,
                    q.daily_quota_bytes,
                    q.query_limit_bytes,
                    "\n".join(f"{s};" for s in q.statements),
                )
                for q in data.quotas
            ],
        ),
    ]


def _persist(data: CostData) -> None:
    settings = data.settings
    queries = [
        (query_id, model, usd)
        for model, by_query in sorted(data.costs.items())
        for query_id, usd in sorted(by_query.items())
    ]
    by_id = {r.query_id: r for r in data.records}
    showback: dict[tuple[str, str, str, str, str], Decimal] = defaultdict(Decimal)
    for model, by_query in data.costs.items():
        for query_id, usd in by_query.items():
            record = by_id[query_id]
            a = data.attributions[query_id]
            showback[
                (f"{record.started_at:%Y-%m}", a.team, model, a.cost_kind, record.actor_type)
            ] += usd
    for (month, table), usd in data.storage.items():
        team, _ = data.storage_owner[table]
        showback[(month, team, "cluster", "storage", "storage")] += usd
    attributions = [
        (a.query_id, a.cost_kind, a.team, a.owner, a.subject)
        for a in sorted(data.attributions.values(), key=lambda a: a.query_id)
    ]
    with open_store(settings) as connection:
        replace_ops_table(
            connection,
            settings,
            "query_costs",
            "query_id text NOT NULL, model text NOT NULL, usd numeric(30, 12) NOT NULL",
            ["query_id", "model", "usd"],
            queries,
        )
        replace_ops_table(
            connection,
            settings,
            "query_attribution",
            "query_id text PRIMARY KEY, cost_kind text NOT NULL, team text NOT NULL, "
            "owner text NOT NULL, subject text",
            ["query_id", "cost_kind", "team", "owner", "subject"],
            attributions,
        )
        replace_ops_table(
            connection,
            settings,
            "cost_showback",
            SHOWBACK_DDL,
            ["month", "team", "model", "cost_kind", "actor_type", "usd"],
            [(*k, v) for k, v in sorted(showback.items())],
        )
        for name, ddl, columns, rows in _derived(data):
            replace_ops_table(connection, settings, name, ddl, columns, rows)


def run_cost(settings: Settings) -> CostSummary:
    data = gather(settings)
    _persist(data)
    path = settings.resolve(settings.paths.reports) / "cost.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.render(data), encoding="utf-8", newline="\n")
    totals = {model: sum(by_query.values(), Decimal(0)) for model, by_query in data.costs.items()}
    totals["cluster"] += sum(data.storage.values(), Decimal(0))
    unused = len({u.unique_id for u in data.unused})
    return CostSummary(len(data.records), path, totals, unused)


__all__ = ["CostData", "CostError", "CostSummary", "gather", "run_cost"]
