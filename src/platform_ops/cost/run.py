"""`make cost`: price the collected workload, attribute it, and write the report.

Runs over whatever `make simulate` collected, and can be rerun on its own. It
refreshes ownership from the registry first, so a change to
``config/ownership.yaml`` shows up in the next report without re-simulating.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import duckdb

from platform_ops.common.config import Settings
from platform_ops.common.db import OPS_SCHEMA, insert_rows, open_connection, transaction
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.cost import attribution, growth, recommend, report
from platform_ops.cost.estimate import estimate
from platform_ops.cost.pricing import ComputePricing, EstimatedQuery, PricedQuery, ScanPricing
from platform_ops.metadata.check import persist_ownership, run_check
from platform_ops.metadata.lineage import build_graph
from platform_ops.metadata.manifest import load_manifest
from platform_ops.metadata.registry import Registry


class CostError(RuntimeError):
    """The cost report cannot be produced from the current state."""


@dataclass(frozen=True)
class CostSummary:
    queries: int
    report_path: Path
    accuracy_path: Path
    total_usd: dict[str, Decimal]
    unused: int


def _estimated_queries(connection: duckdb.DuckDBPyConnection) -> list[EstimatedQuery]:
    rows = connection.execute(
        f"""SELECT e.query_id, e.actor_type, e.started_at, e.modeled_ms,
                   list(b.bytes ORDER BY b.table_name) FILTER (WHERE b.bytes IS NOT NULL)
            FROM {OPS_SCHEMA}.query_estimates e
            LEFT JOIN {OPS_SCHEMA}.query_table_bytes b USING (query_id)
            GROUP BY e.query_id, e.actor_type, e.started_at, e.modeled_ms
            ORDER BY e.query_id"""
    ).fetchall()
    return [
        EstimatedQuery(str(q), str(a), s, int(ms), tuple(int(b) for b in (tb or ())))  # type: ignore[arg-type]
        for q, a, s, ms, tb in rows
    ]


def _persist_costs(connection: duckdb.DuckDBPyConnection, priced: list[PricedQuery]) -> None:
    connection.execute(
        f"""CREATE OR REPLACE TABLE {OPS_SCHEMA}.query_costs (
                query_id VARCHAR NOT NULL,
                model VARCHAR NOT NULL,
                usd DECIMAL(24, 12) NOT NULL,
                billed_bytes BIGINT NOT NULL,
                billed_ms BIGINT NOT NULL,
                warehouse VARCHAR,
                burst VARCHAR
            )"""
    )
    rows = [
        (p.query_id, p.model, p.usd, p.billed_bytes, p.billed_ms, p.warehouse, p.burst)
        for p in priced
    ]
    with transaction(connection):
        insert_rows(
            connection,
            f"{OPS_SCHEMA}.query_costs",
            ("query_id", "model", "usd", "billed_bytes", "billed_ms", "warehouse", "burst"),
            rows,
        )


def _cost_by_team(connection: duckdb.DuckDBPyConnection) -> None:
    """``ops.cost_by_team``: team and month, per pricing model (SPEC Phase 2)."""
    connection.execute(
        f"""CREATE OR REPLACE TABLE {OPS_SCHEMA}.cost_by_team AS
            SELECT CAST(date_trunc('month', ql.started_at) AS DATE) AS month,
                   a.team,
                   c.model AS pricing_model,
                   coalesce(sum(c.usd) FILTER (WHERE a.cost_kind = 'production'), 0)
                       AS production_usd,
                   coalesce(sum(c.usd) FILTER (WHERE a.cost_kind = 'consumption'), 0)
                       AS consumption_usd,
                   sum(c.usd) AS total_usd
            FROM {OPS_SCHEMA}.query_costs c
            JOIN {OPS_SCHEMA}.query_attribution a USING (query_id)
            JOIN {OPS_SCHEMA}.query_log ql USING (query_id)
            GROUP BY ALL
            ORDER BY month, team, pricing_model"""
    )


def _write_table(
    connection: duckdb.DuckDBPyConnection,
    name: str,
    ddl: str,
    columns: tuple[str, ...],
    rows: list[tuple[object, ...]],
) -> None:
    connection.execute(f"CREATE OR REPLACE TABLE {OPS_SCHEMA}.{name} ({ddl})")
    with transaction(connection):
        insert_rows(connection, f"{OPS_SCHEMA}.{name}", columns, rows)


def run_cost(settings: Settings) -> CostSummary:
    invocation = invocation_from_settings(settings)
    if not invocation.manifest_path.is_file():
        raise CostError("No dbt manifest. Run `make simulate` (or `make build`) first.")
    nodes = load_manifest(invocation.manifest_path)
    graph = build_graph(nodes)
    registry = Registry.from_config_dir(settings.root / "config")
    check = run_check(registry, nodes)
    if not check.passed:
        raise CostError("ownership check failed: " + "; ".join(check.errors[:5]))

    window_start = settings.simulation.start
    window_days = settings.simulation.weeks * 7
    window_end = window_start + timedelta(days=window_days)

    with open_connection(settings) as connection:
        has_log = connection.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE schema_name = ? AND table_name = ?",
            [OPS_SCHEMA, "query_log"],
        ).fetchone()
        if not has_log or not has_log[0]:
            raise CostError("No collected workload. Run `make simulate` first.")

        persist_ownership(connection, check, nodes)
        queries = estimate(connection, settings)
        estimated = _estimated_queries(connection)
        priced = ScanPricing(settings.pricing.scan).price(estimated)
        priced += ComputePricing(settings.pricing.compute).price(estimated)
        _persist_costs(connection, priced)

        log_rows = connection.execute(
            f"""SELECT query_id, actor, actor_type, node_id
                FROM {OPS_SCHEMA}.query_log ORDER BY query_id"""
        ).fetchall()
        attribution.persist(
            connection,
            attribution.attribute(
                [(str(q), str(a), str(t), n) for q, a, t, n in log_rows],
                nodes,
                check.resolutions,
                registry,
            ),
        )
        _cost_by_team(connection)

        recs = settings.recommendations
        unused = recommend.unused_tables(
            connection,
            nodes,
            graph,
            check.resolutions,
            window_end,
            window_days,
            recs.unused_lookback_days,
        )
        spots = recommend.hotspots(
            connection, recs.hotspot_min_table_bytes, recs.hotspot_min_filtered_reads
        )
        shortest = min(recs.unused_lookback_days)
        dead = {u.unique_id for u in unused if u.lookback_days == shortest}
        incremental = recommend.incremental_candidates(
            connection,
            nodes,
            graph,
            check.resolutions,
            growth.append_only_sources(connection),
            recs.incremental_top_n,
            dead,
        )

        data = report.gather(connection, settings, nodes, check.resolutions, window_days)
        _write_report_tables(connection, data, unused, spots, incremental)
        accuracy = report.proxy_accuracy(connection)
        _write_table(
            connection,
            "cost_proxy_accuracy",
            "actor_type VARCHAR, queries BIGINT, proxy_rows HUGEINT, scanned_rows HUGEINT, "
            "median_ratio DOUBLE, within_10pct DOUBLE",
            ("actor_type", "queries", "proxy_rows", "scanned_rows", "median_ratio", "within_10pct"),
            [tuple(a) for a in accuracy],
        )

    reports_dir = settings.resolve(settings.paths.reports)
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "cost.md"
    report_path.write_text(
        report.render(settings, data, unused, spots, incremental), encoding="utf-8", newline="\n"
    )
    accuracy_path = reports_dir / "cost_proxy_accuracy.md"
    accuracy_path.write_text(
        report.render_accuracy(settings, accuracy), encoding="utf-8", newline="\n"
    )
    return CostSummary(
        queries=queries,
        report_path=report_path,
        accuracy_path=accuracy_path,
        total_usd=data.totals,
        unused=len({u.unique_id for u in unused}),
    )


def _write_report_tables(
    connection: duckdb.DuckDBPyConnection,
    data: report.ReportData,
    unused: list[recommend.UnusedTable],
    spots: list[recommend.Hotspot],
    incremental: list[recommend.IncrementalCandidate],
) -> None:
    money = "DECIMAL(38, 12)"
    _write_table(
        connection,
        "cost_report_showback",
        f"team VARCHAR, pricing_model VARCHAR, production_usd {money}, "
        f"consumption_usd {money}, total_usd {money}",
        ("team", "pricing_model", "production_usd", "consumption_usd", "total_usd"),
        [tuple(r) for r in data.showback],
    )
    _write_table(
        connection,
        "cost_report_models",
        f"rank INTEGER, unique_id VARCHAR, relation VARCHAR, team VARCHAR, "
        f"scan_usd {money}, compute_usd {money}",
        ("rank", "unique_id", "relation", "team", "scan_usd", "compute_usd"),
        [tuple(r) for r in data.top_models],
    )
    _write_table(
        connection,
        "cost_report_unused",
        f"lookback_days INTEGER, unique_id VARCHAR, relation VARCHAR, team VARCHAR, "
        f"owner VARCHAR, monthly_saving_scan {money}, monthly_saving_compute {money}",
        (
            "lookback_days",
            "unique_id",
            "relation",
            "team",
            "owner",
            "monthly_saving_scan",
            "monthly_saving_compute",
        ),
        [
            (
                u.lookback_days,
                u.unique_id,
                u.relation,
                u.team,
                u.owner,
                u.monthly_saving["scan"],
                u.monthly_saving["compute"],
            )
            for u in unused
        ],
    )
    _write_table(
        connection,
        "cost_report_hotspots",
        f"table_name VARCHAR, column_name VARCHAR, reads BIGINT, table_bytes BIGINT, "
        f"bytes_scanned BIGINT, scan_usd {money}",
        ("table_name", "column_name", "reads", "table_bytes", "bytes_scanned", "scan_usd"),
        [(h.table, h.column, h.reads, h.table_bytes, h.bytes_scanned, h.scan_usd) for h in spots],
    )
    _write_table(
        connection,
        "cost_report_incremental",
        f"unique_id VARCHAR, relation VARCHAR, team VARCHAR, scan_usd {money}, "
        f"compute_usd {money}, sources VARCHAR[], blocked_by VARCHAR[], candidate BOOLEAN",
        (
            "unique_id",
            "relation",
            "team",
            "scan_usd",
            "compute_usd",
            "sources",
            "blocked_by",
            "candidate",
        ),
        [
            (
                c.unique_id,
                c.relation,
                c.team,
                c.production.get("scan", Decimal(0)),
                c.production.get("compute", Decimal(0)),
                list(c.sources),
                list(c.blocked_by),
                c.candidate,
            )
            for c in incremental
        ],
    )
    _write_table(
        connection,
        "cost_report_pricing",
        f"workload VARCHAR, scan_usd {money}, compute_usd {money}, bytes_scanned HUGEINT, "
        "busy_seconds DOUBLE, billed_seconds DOUBLE",
        ("workload", "scan_usd", "compute_usd", "bytes_scanned", "busy_seconds", "billed_seconds"),
        [tuple(r) for r in data.by_workload],
    )
