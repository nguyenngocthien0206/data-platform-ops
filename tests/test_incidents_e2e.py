"""End to end: incidents in the company's production week (Phase 14).

Read from the shared ``company_warehouse``: three weeks at scale 0.01, the
third in production, where four faults happened in operation (a null spike and
an invalid category one day, orphan payments and a stale feed the next). Every
night ran freshness, run, snapshot and test, and the incident desk ingested
each invocation through the same code ``platform-ops incidents ingest`` runs.
``incidents run`` wrote the report from the store. Proves:

- every planted fault maps to exactly one incident and every incident to a
  fault, paged to the owner of the faulted source;
- the owner's fix lands, the next run finds the root healthy, and the incident
  resolves on its own;
- the report is a pure function of the store, and ``incidents summary`` reads it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from psycopg import sql
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings
from platform_ops.common.store import open_store, ops_table
from platform_ops.incidents import store

pytestmark = pytest.mark.integration

runner = CliRunner()


def _rows(settings: Settings, table: sql.Composed, query: str) -> list[tuple[Any, ...]]:
    with open_store(settings) as connection:
        return list(connection.execute(sql.SQL(query).format(table)).fetchall())


def _cli(settings: Settings, *command: str) -> Any:
    patch = pytest.MonkeyPatch()
    patch.setenv(CONFIG_PATH_ENV_VAR, str(settings.root / "config" / "settings.yaml"))
    try:
        return runner.invoke(app, list(command))
    finally:
        patch.undo()


def _report(settings: Settings) -> Path:
    return settings.resolve(settings.paths.reports) / "incidents.md"


def test_every_planted_fault_is_one_incident_paged_to_its_source_owner(
    company_warehouse: Settings,
) -> None:
    pairs = _rows(
        company_warehouse,
        ops_table(company_warehouse, "fault_incidents"),
        "SELECT fault_id, incident_id FROM {} ORDER BY fault_id",
    )
    assert [fault for fault, _ in pairs] == ["F1", "F2", "F3", "F4"]
    assert len({incident for _, incident in pairs}) == 4
    report = _report(company_warehouse).read_text("utf-8")
    assert "Every injected fault maps to exactly one incident" in report
    metrics = {
        f"{m}:{d}": v
        for m, d, v in _rows(
            company_warehouse,
            ops_table(company_warehouse, "incident_metrics"),
            "SELECT metric, dimension, value FROM {}",
        )
    }
    assert metrics["routing_accuracy:person"] == 1.0
    assert metrics["unmatched_incidents:all"] == 0
    assert metrics["raw_alerts:all"] > metrics["incidents:all"], "grouping cuts the noise"


def test_every_incident_resolves_after_its_fix_on_the_next_healthy_run(
    company_warehouse: Settings,
) -> None:
    incidents = _rows(
        company_warehouse,
        store.table(company_warehouse, "incidents"),
        "SELECT i.incident_id, i.opened_at, i.resolved_at, r.fixed_at FROM {} i "
        "JOIN " + f'"{company_warehouse.company.name("ops")}".incident_responses r '
        "USING (incident_id) WHERE i.merged_into IS NULL ORDER BY 1",
    )
    assert len(incidents) == 4
    for incident_id, opened, resolved, fixed in incidents:
        assert resolved is not None, f"{incident_id} still open"
        assert opened < fixed < resolved, incident_id
        # Resolution is the first nightly run after the fix: 02:00.
        assert (resolved.hour, resolved.minute) == (2, 0)
    faults = _rows(
        company_warehouse,
        ops_table(company_warehouse, "incident_faults"),
        "SELECT fault_id, repaired_at FROM {} ORDER BY 1",
    )
    assert all(repaired is not None for _, repaired in faults)


def test_the_report_is_rewritten_identically_and_the_summary_reads_the_store(
    company_warehouse: Settings,
) -> None:
    report = _report(company_warehouse).read_bytes()
    result = _cli(company_warehouse, "incidents", "run")
    assert result.exit_code == 0, result.output
    assert _report(company_warehouse).read_bytes() == report
    summary = _cli(company_warehouse, "incidents", "summary")
    assert summary.exit_code == 0, summary.output
    assert "| Week | Person | Failing checks | Pages |" in summary.output
    assert "priya" in summary.output


def test_production_nights_ran_every_invocation(company_warehouse: Settings) -> None:
    runs = _rows(
        company_warehouse,
        store.table(company_warehouse, "incident_invocations"),
        "SELECT run_id, count(*), string_agg(kind, ',' ORDER BY kind) FROM {} "
        "GROUP BY run_id ORDER BY run_id",
    )
    assert len(runs) == 7
    # Freshness writes sources.json; run, snapshot and test write run_results.json.
    assert all(
        n == 4 and kinds == "run_results,run_results,run_results,sources" for _, n, kinds in runs
    )
