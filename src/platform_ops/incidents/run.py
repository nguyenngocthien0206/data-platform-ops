"""``platform-ops incidents run``: the scenario, its tables, metrics and reports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from platform_ops.common.config import Settings
from platform_ops.common.db import OPS_SCHEMA, insert_rows, open_connection, transaction
from platform_ops.incidents import ingest, metrics, notify
from platform_ops.incidents.metrics import IncidentMetrics
from platform_ops.incidents.report import write_reports
from platform_ops.incidents.scenario import Scenario, ScenarioResult


@dataclass(frozen=True)
class IncidentSummary:
    faults: int
    incidents: int
    raw_alerts: int
    pages: int
    runs: int
    dbt_invocations: int
    one_incident_per_fault: bool
    report_path: Path
    postmortems: tuple[Path, ...]


INCIDENTS_DDL = """
    incident_id VARCHAR PRIMARY KEY,
    root_node VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    score INTEGER NOT NULL,
    root_tier VARCHAR NOT NULL,
    owner VARCHAR NOT NULL,
    team VARCHAR NOT NULL,
    naive_owner VARCHAR NOT NULL,
    naive_team VARCHAR NOT NULL,
    routed_via VARCHAR NOT NULL,
    opened_at TIMESTAMP NOT NULL,
    acknowledged_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP,
    runs INTEGER NOT NULL,
    failing_checks INTEGER NOT NULL,
    previous_incident_id VARCHAR"""

INCIDENT_COLUMNS = (
    "incident_id", "root_node", "dataset", "severity", "score", "root_tier", "owner", "team",
    "naive_owner", "naive_team", "routed_via", "opened_at", "acknowledged_at", "resolved_at",
    "last_seen_at", "runs", "failing_checks", "previous_incident_id",
)  # fmt: skip

IMPACT_DDL = """
    incident_id VARCHAR NOT NULL,
    node_id VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    tier VARCHAR,
    weight INTEGER"""

RUNS_DDL = """
    run_id VARCHAR PRIMARY KEY,
    run_at TIMESTAMP NOT NULL,
    selected_sources VARCHAR NOT NULL,
    checks_run INTEGER NOT NULL,
    failing_checks INTEGER NOT NULL,
    opened INTEGER NOT NULL,
    appended INTEGER NOT NULL"""


def _create(connection: duckdb.DuckDBPyConnection, name: str, ddl: str) -> None:
    connection.execute(f"CREATE OR REPLACE TABLE {OPS_SCHEMA}.{name} ({ddl})")


def persist(
    connection: duckdb.DuckDBPyConnection, result: ScenarioResult, local: notify.LocalNotifier
) -> None:
    """Replace every scenario table with this run's contents."""
    _create(connection, "incidents", INCIDENTS_DDL)
    _create(connection, "incident_impact", IMPACT_DDL)
    _create(connection, "incident_runs", RUNS_DDL)
    ingest.reset_check_events(connection)
    notify.reset_notifications(connection)

    incident_rows = [
        (i.incident_id, i.root, i.dataset, i.severity, i.score, i.root_tier, i.owner, i.team,
         i.naive_owner, i.naive_team, i.routed_via, i.opened_at, i.acknowledged_at,
         i.resolved_at, i.last_seen_at, len(i.run_ids), i.checks, i.previous_incident_id)
        for i in result.incidents
    ]  # fmt: skip
    insert_rows(connection, f"{OPS_SCHEMA}.incidents", INCIDENT_COLUMNS, incident_rows)

    impact: list[tuple[str, str, str, str | None, int | None]] = []
    for i in result.incidents:
        impact += [(i.incident_id, node, "failed", None, None) for node in sorted(i.failed_nodes)]
        impact += [(i.incident_id, node, "skipped", None, None) for node in sorted(i.skipped)]
        impact += [(i.incident_id, c.unique_id, "downstream", c.tier, c.weight)
                   for c in i.consumers]  # fmt: skip
    insert_rows(connection, f"{OPS_SCHEMA}.incident_impact",
                ("incident_id", "node_id", "role", "tier", "weight"), impact)  # fmt: skip

    runs = [
        (r.run_id, r.run_at, ",".join(r.selected_sources), r.checks_run, r.failures, r.opened,
         r.appended)
        for r in result.runs
    ]  # fmt: skip
    insert_rows(connection, f"{OPS_SCHEMA}.incident_runs",
                ("run_id", "run_at", "selected_sources", "checks_run", "failing_checks", "opened",
                 "appended"), runs)  # fmt: skip

    ingest.persist_events(connection, result.events)
    local.flush(connection)


def run_incidents(
    settings: Settings, *, allow_slack: bool = True, full_run_on_days: Sequence[int] = ()
) -> tuple[IncidentSummary, ScenarioResult, IncidentMetrics]:
    scenario = Scenario(settings, allow_slack=allow_slack, full_run_on_days=full_run_on_days)
    result = scenario.run()
    days = settings.incidents.scenario_days
    with open_connection(settings) as connection, transaction(connection):
        persist(connection, result, scenario.local)
        truths = metrics.read_ground_truth(connection, scenario.nodes, scenario.registry)
        graded = metrics.compute(result, truths, scenario.graph, scenario.nodes,
                                 scenario.registry, days)  # fmt: skip
        metrics.persist(connection, graded)
    report_path, postmortems = write_reports(
        settings.resolve(settings.paths.reports), result, graded, days
    )
    summary = IncidentSummary(
        faults=len(truths),
        incidents=graded.incidents,
        raw_alerts=graded.raw_alerts,
        pages=graded.pages,
        runs=len(result.runs),
        dbt_invocations=result.dbt_invocations,
        one_incident_per_fault=graded.one_incident_per_fault,
        report_path=report_path,
        postmortems=tuple(postmortems),
    )
    return summary, result, graded
