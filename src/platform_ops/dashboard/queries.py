"""Everything the dashboards read, as named SQL over the ``ops`` schema only.

The dashboards are a window on what the modules already computed and stored
(CLAUDE.md: module outputs are tables in ``ops``). They never read raw data or
dbt models, never recompute a number, and never write. Keeping every query in
this one module, with the tables it needs, makes that checkable: a test parses
each query and fails on any table outside ``ops``.

A table that does not exist yet means a module has not run. That is reported
as :class:`MissingData` naming the command to run, not as a DuckDB error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from platform_ops.common.db import OPS_SCHEMA


class MissingData(RuntimeError):
    """A table the dashboard needs has not been produced yet."""


@dataclass(frozen=True)
class Query:
    sql: str
    tables: tuple[str, ...]


# Which command produces each table, for the message when one is missing.
PRODUCED_BY: dict[str, str] = {
    "node_ownership": "make cost",
    "cost_": "make cost",
    "incident": "make incidents",
    "check_events": "make incidents",
    "notifications": "make incidents",
    "fault_incidents": "make incidents",
    "reconcile_": "make reconcile",
}

QUERIES: dict[str, Query] = {
    # -- overview -----------------------------------------------------------------
    "ownership_coverage": Query(
        """SELECT resource_type, count(*) AS datasets, count(owner) AS owned,
                  count(DISTINCT team) AS teams
           FROM ops.node_ownership GROUP BY resource_type ORDER BY resource_type""",
        ("node_ownership",),
    ),
    "owners_by_team": Query(
        """SELECT team, count(*) AS datasets,
                  count(*) FILTER (WHERE tier = 'critical') AS critical
           FROM ops.node_ownership WHERE team IS NOT NULL
           GROUP BY team ORDER BY datasets DESC, team""",
        ("node_ownership",),
    ),
    # -- cost ---------------------------------------------------------------------
    "showback": Query(
        """SELECT team, pricing_model,
                  CAST(production_usd AS DOUBLE) AS production_usd,
                  CAST(consumption_usd AS DOUBLE) AS consumption_usd,
                  CAST(total_usd AS DOUBLE) AS total_usd
           FROM ops.cost_report_showback ORDER BY pricing_model, total_usd DESC, team""",
        ("cost_report_showback",),
    ),
    "cost_by_month": Query(
        """SELECT month, team, pricing_model, CAST(total_usd AS DOUBLE) AS total_usd
           FROM ops.cost_by_team ORDER BY month, team""",
        ("cost_by_team",),
    ),
    "cost_by_workload": Query(
        """SELECT workload, CAST(scan_usd AS DOUBLE) AS scan_usd,
                  CAST(compute_usd AS DOUBLE) AS compute_usd,
                  CAST(bytes_scanned AS DOUBLE) AS bytes_scanned,
                  busy_seconds, billed_seconds
           FROM ops.cost_report_pricing ORDER BY workload""",
        ("cost_report_pricing",),
    ),
    "top_models": Query(
        """SELECT rank, relation, team, CAST(scan_usd AS DOUBLE) AS scan_usd,
                  CAST(compute_usd AS DOUBLE) AS compute_usd
           FROM ops.cost_report_models ORDER BY rank""",
        ("cost_report_models",),
    ),
    "unused_tables": Query(
        """SELECT lookback_days, relation, team, owner,
                  CAST(monthly_saving_scan AS DOUBLE) AS monthly_saving_scan,
                  CAST(monthly_saving_compute AS DOUBLE) AS monthly_saving_compute
           FROM ops.cost_report_unused ORDER BY lookback_days, monthly_saving_compute DESC""",
        ("cost_report_unused",),
    ),
    "hotspots": Query(
        """SELECT table_name, column_name, reads, table_bytes, bytes_scanned,
                  CAST(scan_usd AS DOUBLE) AS scan_usd
           FROM ops.cost_report_hotspots ORDER BY bytes_scanned DESC""",
        ("cost_report_hotspots",),
    ),
    "incremental": Query(
        """SELECT relation, team, candidate, CAST(compute_usd AS DOUBLE) AS compute_usd,
                  array_to_string(sources, ', ') AS sources,
                  array_to_string(blocked_by, ', ') AS blocked_by
           FROM ops.cost_report_incremental ORDER BY candidate DESC, compute_usd DESC""",
        ("cost_report_incremental",),
    ),
    # -- incidents ----------------------------------------------------------------
    "incident_metrics": Query(
        "SELECT metric, dimension, value FROM ops.incident_metrics ORDER BY metric, dimension",
        ("incident_metrics",),
    ),
    "incidents": Query(
        """SELECT i.incident_id, f.fault_id, i.root_node, i.severity, i.score, i.owner, i.team,
                  i.naive_owner, i.opened_at, i.acknowledged_at, i.resolved_at, i.runs,
                  i.failing_checks, i.previous_incident_id,
                  date_diff('minute', i.opened_at, i.resolved_at) / 60.0 AS hours_to_resolve
           FROM ops.incidents AS i
           LEFT JOIN ops.fault_incidents AS f ON f.incident_id = i.incident_id
           ORDER BY i.incident_id""",
        ("incidents", "fault_incidents"),
    ),
    "check_events_by_kind": Query(
        """SELECT check_type, count(*) AS failing_checks, count(DISTINCT incident_id) AS incidents
           FROM ops.check_events GROUP BY check_type ORDER BY check_type""",
        ("check_events",),
    ),
    "incident_runs": Query(
        """SELECT run_at, selected_sources, checks_run, failing_checks, opened, appended
           FROM ops.incident_runs ORDER BY run_at""",
        ("incident_runs",),
    ),
    "notifications_by_kind": Query(
        """SELECT kind, recipient, count(*) AS sent
           FROM ops.notifications GROUP BY kind, recipient ORDER BY kind, recipient""",
        ("notifications",),
    ),
    # -- reconciliation -----------------------------------------------------------
    "reconcile_tables": Query(
        """SELECT engine, pass, table_name, source_rows, target_rows, row_match_rate, passed,
                  summary_rows, fetched_rows, summary_rows + fetched_rows AS segmented_rows,
                  naive_rows, queries
           FROM ops.reconcile_tables ORDER BY engine, pass, table_name""",
        ("reconcile_tables",),
    ),
    "reconcile_metrics": Query(
        """SELECT engine, pass, metric, dimension, value
           FROM ops.reconcile_metrics ORDER BY engine, pass, metric, dimension""",
        ("reconcile_metrics",),
    ),
    "discrepancies_by_class": Query(
        """SELECT engine, pass, table_name, class, count(*) AS discrepancies
           FROM ops.reconcile_discrepancies
           GROUP BY engine, pass, table_name, class
           ORDER BY engine, pass, table_name, class""",
        ("reconcile_discrepancies",),
    ),
    "discrepancy_samples": Query(
        """SELECT engine, pass, class, table_name, key, column_name, source_value, target_value
           FROM (SELECT *, row_number() OVER (PARTITION BY engine, pass, class
                                              ORDER BY table_name, key, column_name) AS n
                 FROM ops.reconcile_discrepancies)
           WHERE n <= 5 ORDER BY engine, pass, class, n""",
        ("reconcile_discrepancies",),
    ),
    "reconcile_segments": Query(
        """SELECT engine, pass, table_name, level, width, segments, differing
           FROM ops.reconcile_segments ORDER BY engine, pass, table_name, level""",
        ("reconcile_segments",),
    ),
}


def producer(table: str) -> str:
    for prefix, command in PRODUCED_BY.items():
        if table.startswith(prefix):
            return command
    return "make demo"


class Warehouse:
    """A read-only view of the warehouse's ``ops`` schema.

    Each call opens and closes its own read-only connection, so a dashboard left
    open never holds a lock that would stop the next ``make`` run.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> duckdb.DuckDBPyConnection:
        if not self.path.is_file():
            raise MissingData(f"No warehouse at {self.path}. Run `make demo` first.")
        return duckdb.connect(str(self.path), read_only=True)

    def available(self) -> set[str]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT table_name FROM duckdb_tables() WHERE schema_name = ?", [OPS_SCHEMA]
            ).fetchall()
        finally:
            connection.close()
        return {str(r[0]) for r in rows}

    def frame(self, name: str) -> pd.DataFrame:
        query = QUERIES[name]
        connection = self._connect()
        try:
            present = {
                str(r[0])
                for r in connection.execute(
                    "SELECT table_name FROM duckdb_tables() WHERE schema_name = ?", [OPS_SCHEMA]
                ).fetchall()
            }
            missing = [t for t in query.tables if t not in present]
            if missing:
                commands = sorted({producer(t) for t in missing})
                raise MissingData(
                    f"ops.{', ops.'.join(missing)} not found. Run `{'` and `'.join(commands)}`."
                )
            result: Any = connection.execute(query.sql).df()
            return pd.DataFrame(result)
        finally:
            connection.close()

    def metric(self, frame: pd.DataFrame, metric: str, dimension: str = "all") -> float | None:
        """One value from a long-form metrics frame, or ``None``."""
        rows = frame[(frame["metric"] == metric) & (frame["dimension"] == dimension)]
        return None if rows.empty else float(rows["value"].iloc[0])
