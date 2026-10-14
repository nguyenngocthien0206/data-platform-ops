"""Everything the dashboards read, as named SQL over the ops store only (version 2.0).

The dashboards are a window on what the modules already computed and stored in
the Postgres ops store (``common.store``). They never read the company's data,
never recompute a number the report states, and never write: they connect as
their own read-only role (``ensure_dashboard_role``). Keeping every query in
this one module, with the tables it needs, makes that checkable: a test parses
each query and fails on any table outside ``ops``.

Queries name the schema ``{ops}``, filled in with the namespaced ops schema at
run time. A table that does not exist yet means a module has not run, which is
reported as :class:`MissingData` naming the command to run, not as an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from psycopg import sql

from platform_ops.common.config import Settings


class MissingData(RuntimeError):
    """Something the dashboard needs is not there yet: a table, a password, a server."""


@dataclass(frozen=True)
class Query:
    sql: str
    tables: tuple[str, ...]


# Which command produces each table, for the message when one is missing.
PRODUCED_BY: dict[str, str] = {
    "query_": "make cost",
    "cost_": "make cost",
    "incident_metrics": "make incidents",
    "fault_incidents": "make incidents",
}

QUERIES: dict[str, Query] = {
    # -- overview -----------------------------------------------------------------
    "company_run": Query(
        "SELECT window_start, days FROM {ops}.company_run",
        ("company_run",),
    ),
    "ingestion": Query(
        """SELECT count(*) AS loads, coalesce(sum(rows_read), 0) AS rows_read
           FROM {ops}.ingestion_loads""",
        ("ingestion_loads",),
    ),
    "ownership_coverage": Query(
        """SELECT resource_type, count(*) AS datasets, count(DISTINCT team) AS teams,
                  count(*) FILTER (WHERE tier = 'critical') AS critical
           FROM {ops}.node_ownership GROUP BY resource_type ORDER BY resource_type""",
        ("node_ownership",),
    ),
    "owners_by_team": Query(
        """SELECT team, count(*) AS datasets,
                  count(*) FILTER (WHERE tier = 'critical') AS critical
           FROM {ops}.node_ownership GROUP BY team ORDER BY datasets DESC, team""",
        ("node_ownership",),
    ),
    "lineage": Query(
        "SELECT count(*) AS edges FROM {ops}.lineage_edges",
        ("lineage_edges",),
    ),
    # -- cost ---------------------------------------------------------------------
    "cost_months": Query(
        """SELECT month, days, compute_usd::float8 AS compute_usd,
                  storage_usd::float8 AS storage_usd, stated_usd::float8 AS stated_usd,
                  shared_usd::float8 AS shared_usd
           FROM {ops}.cost_months ORDER BY month""",
        ("cost_months",),
    ),
    "showback": Query(
        """SELECT month, team, model, cost_kind, actor_type, usd::float8 AS usd
           FROM {ops}.cost_showback ORDER BY month, team, model, cost_kind, actor_type""",
        ("cost_showback",),
    ),
    "workload": Query(
        """SELECT actor_type, count(*) AS queries, sum(bytes_scanned)::float8 AS bytes_read
           FROM {ops}.query_log GROUP BY actor_type ORDER BY actor_type""",
        ("query_log",),
    ),
    "business_case": Query(
        """SELECT month, days, legacy_days, legacy_usd::float8 AS legacy_usd,
                  clickhouse_usd::float8 AS clickhouse_usd,
                  reconcile_usd::float8 AS reconcile_usd
           FROM {ops}.cost_business_case ORDER BY month""",
        ("cost_business_case",),
    ),
    "business_summary": Query(
        """SELECT cutover, legacy_monthly_usd::float8 AS legacy_monthly_usd,
                  clickhouse_monthly_usd::float8 AS clickhouse_monthly_usd,
                  monthly_saving_usd::float8 AS monthly_saving_usd,
                  overlap_usd::float8 AS overlap_usd, payback_months::float8 AS payback_months
           FROM {ops}.cost_business_summary""",
        ("cost_business_summary",),
    ),
    "unused": Query(
        """SELECT lookback_days, relation, team, owner,
                  cluster_usd_month::float8 AS cluster_usd_month,
                  scan_usd_month::float8 AS scan_usd_month,
                  compute_usd_month::float8 AS compute_usd_month
           FROM {ops}.cost_unused ORDER BY lookback_days, cluster_usd_month DESC, relation""",
        ("cost_unused",),
    ),
    "hotspots": Query(
        """SELECT table_name, column_name, reads, table_rows, rows_scanned, bytes_scanned,
                  sorting_key, suggestion
           FROM {ops}.cost_hotspots ORDER BY bytes_scanned DESC, table_name, column_name""",
        ("cost_hotspots",),
    ),
    "incremental": Query(
        """SELECT rank, relation, team, cluster_usd::float8 AS cluster_usd, sources,
                  blocked_by, candidate
           FROM {ops}.cost_incremental ORDER BY rank""",
        ("cost_incremental",),
    ),
    "quotas": Query(
        """SELECT user_name, team, active_days, p95_daily_bytes, max_query_bytes,
                  daily_quota_bytes, query_limit_bytes, statements
           FROM {ops}.cost_quotas ORDER BY user_name""",
        ("cost_quotas",),
    ),
    # -- incidents ----------------------------------------------------------------
    "incident_metrics": Query(
        "SELECT metric, dimension, value FROM {ops}.incident_metrics ORDER BY metric, dimension",
        ("incident_metrics",),
    ),
    "incidents": Query(
        """SELECT i.incident_id, i.root, i.severity, i.score, i.owner, i.team, i.opened_at,
                  r.acknowledged_at, r.fixed_at, i.resolved_at, f.fault_id
           FROM {ops}.incidents AS i
           LEFT JOIN {ops}.incident_responses AS r ON r.incident_id = i.incident_id
           LEFT JOIN {ops}.fault_incidents AS f ON f.incident_id = i.incident_id
           WHERE i.merged_into IS NULL ORDER BY i.seq""",
        ("incidents", "incident_responses", "fault_incidents"),
    ),
    "incident_checks": Query(
        """SELECT check_type, count(*) AS failing_checks,
                  count(DISTINCT incident_id) AS incidents
           FROM {ops}.incident_check_events GROUP BY check_type ORDER BY check_type""",
        ("incident_check_events",),
    ),
    "incident_nights": Query(
        """SELECT run_id, min(detected_at) AS detected_at, count(*) AS invocations,
                  sum(checks_run) AS checks_run
           FROM {ops}.incident_invocations GROUP BY run_id ORDER BY min(detected_at), run_id""",
        ("incident_invocations",),
    ),
    # -- reconciliation -----------------------------------------------------------
    "cutover": Query(
        """SELECT recommended, night, run_at, clean_nights, required, parallel_nights
           FROM {ops}.reconcile_cutover""",
        ("reconcile_cutover",),
    ),
    "nights": Query(
        """SELECT night, run_at, job, backfilled, planted, repaired, level, name, passed,
                  explained, source_rows, missing, extra, rows_with_diffs, row_match_rate,
                  failures, differing_upstream, rows_moved, rows_moved_naive
           FROM {ops}.reconcile_nights ORDER BY night, level DESC, name""",
        ("reconcile_nights",),
    ),
    "grades": Query(
        """SELECT night, expected, detected, true_positives, correctly_classified
           FROM {ops}.reconcile_grades ORDER BY night""",
        ("reconcile_grades",),
    ),
    "labels": Query(
        "SELECT night, label, planted, found FROM {ops}.reconcile_labels ORDER BY night, label",
        ("reconcile_labels",),
    ),
    "examples": Query(
        """SELECT night, level, name, class, key, column_name, source_value, target_value
           FROM {ops}.reconcile_examples
           ORDER BY night, level DESC, name, class, key, column_name""",
        ("reconcile_examples",),
    ),
}


def producer(table: str) -> str:
    for prefix, command in PRODUCED_BY.items():
        if table.startswith(prefix):
            return command
    return "make demo"


class Store:
    """A read-only view of the ops store, as the dashboards' role.

    Each call opens and closes its own connection, so a dashboard left open
    never holds a lock or a session that would get in the next run's way.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.schema = settings.company.name(settings.company.ops_schema)

    def _connect(self) -> Any:
        from platform_ops.common.servers import MissingPasswordError
        from platform_ops.common.store import connect_dashboard

        try:
            return connect_dashboard(self.settings)
        except MissingPasswordError as error:
            raise MissingData(f"No password for the dashboards: {error}.") from error
        except Exception as error:  # the server is down, or the role is not there yet
            raise MissingData(
                f"Cannot reach the ops store as the dashboards' role ({type(error).__name__}). "
                "Run `make up`, then `make demo`, which creates the role."
            ) from error

    def frame(self, name: str) -> pd.DataFrame:
        query = QUERIES[name]
        connection = self._connect()
        try:
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                    [self.schema],
                ).fetchall()
            }
            missing = [t for t in query.tables if t not in present]
            if missing:
                commands = sorted({producer(t) for t in missing})
                raise MissingData(
                    f"ops.{', ops.'.join(missing)} not found. Run `{'` and `'.join(commands)}`."
                )
            cursor = connection.execute(sql.SQL(query.sql).format(ops=sql.Identifier(self.schema)))
            columns = [column.name for column in cursor.description or []]
            return pd.DataFrame(cursor.fetchall(), columns=columns)
        finally:
            connection.close()


def metric(frame: pd.DataFrame, column: str) -> Any:
    """The single value of a one-row frame's column, or ``None``."""
    return None if frame.empty else frame[column].iloc[0]
