"""``platform-ops incidents run``: the incident report, from the store, graded.

The incidents themselves happen during ``make demo``, in the company's
production weeks (``incidents.production``). This command reads the store
back, grades it against the faults the company planted (``ops.incident_faults``),
stores the metrics, and writes ``reports/incidents.md`` and a postmortem draft
for every SEV1. It can be rerun at any time and writes the same files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.common.store import open_store, ops_table
from platform_ops.incidents import metrics
from platform_ops.incidents.desk import Context
from platform_ops.incidents.history import load_history
from platform_ops.incidents.report import write_reports


class IncidentError(RuntimeError):
    """The incident report cannot be produced from the current state."""


@dataclass(frozen=True)
class IncidentSummary:
    faults: int
    incidents: int
    raw_alerts: int
    pages: int
    runs: int
    one_incident_per_fault: bool
    report_path: Path
    postmortems: tuple[Path, ...]


def run_incidents(settings: Settings) -> IncidentSummary:
    manifest = invocation_from_settings(settings).manifest_path
    if not manifest.is_file():
        raise IncidentError(f"no dbt manifest at {manifest}; run `make demo` first")
    with open_store(settings) as connection:
        found = connection.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            [f'"{settings.company.name(settings.company.ops_schema)}".company_run'],
        ).fetchone()
        row = (
            connection.execute(
                sql.SQL("SELECT window_start, days FROM {}").format(
                    ops_table(settings, "company_run")
                )
            ).fetchone()
            if found and found[0]
            else None
        )
    if row is None:
        raise IncidentError("no company run is recorded; run `make demo` first")
    start, window_days = row
    days = min(int(window_days), settings.company.production_weeks * 7)
    context = Context.load(settings, manifest)
    history = load_history(settings, context)
    if not history.runs:
        raise IncidentError(
            "the incident store holds no production run; `make demo` runs the production "
            "weeks (company.production_weeks)"
        )
    truths = metrics.read_ground_truth(settings, context.nodes, context.registry)
    computed = metrics.compute(
        history,
        truths,
        context.graph,
        context.nodes,
        context.registry,
        days,
        start + timedelta(days=int(window_days)),
    )
    metrics.persist(settings, computed)
    report_path, postmortems = write_reports(
        settings.resolve(settings.paths.reports), history, computed, days
    )
    return IncidentSummary(
        faults=len(truths),
        incidents=computed.incidents,
        raw_alerts=computed.raw_alerts,
        pages=computed.pages,
        runs=len(history.runs),
        one_incident_per_fault=computed.one_incident_per_fault,
        report_path=report_path,
        postmortems=tuple(postmortems),
    )
