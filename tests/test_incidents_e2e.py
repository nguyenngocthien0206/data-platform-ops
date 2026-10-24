"""End to end: incidents in the company's production week (Phase 14).

Read from the shared ``company_warehouse``: three weeks at scale 0.01, the
third in production, where four faults happened in operation (a null spike and
an invalid category one day, orphan payments and a stale feed the next). Every
night ran freshness, source tests, run, snapshot and test, and the incident desk ingested
each invocation through the same code ``platform-ops incidents ingest`` runs.
``incidents run`` wrote the report from the store. Proves:

- every planted fault maps to exactly one incident and every incident to a
  fault, paged to the owner of the faulted source;
- every fault is caught on the first night after it happens, a stopped feed
  included (the volume test, not the two-day freshness rule);
- the owner's fix lands, the root and everything below it are rebuilt right
  then, and that rerun resolves the incident the same day;
- the report is a pure function of the store, and ``incidents summary`` reads it.
"""

from __future__ import annotations

from datetime import timedelta
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


def test_every_fault_is_caught_the_first_night_after_it_happens(
    company_warehouse: Settings,
) -> None:
    ops = f'"{company_warehouse.company.name("ops")}"'
    caught = _rows(
        company_warehouse,
        store.table(company_warehouse, "incidents"),
        "SELECT f.fault_id, f.fault_type, i.opened_at - f.injected_at FROM {} i "
        f"JOIN {ops}.fault_incidents m USING (incident_id) "
        f"JOIN {ops}.incident_faults f USING (fault_id) ORDER BY 1",
    )
    assert [fault for fault, *_ in caught] == ["F1", "F2", "F3", "F4"]
    # Planted at 10:00, caught by the 02:00 run after: 16 hours, the stale feed
    # (F4) too, which the two-day freshness rule alone caught after 64.
    assert all(delay == timedelta(hours=16) for *_, delay in caught), caught


def test_every_incident_resolves_at_the_rerun_after_its_fix(
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
        assert opened < fixed, incident_id
        # The rerun right after the fix finds the root healthy: no wait for 02:00.
        assert resolved == fixed, incident_id
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
    nights = [r for r in runs if r[0].startswith("night-")]
    assert len(nights) == 7
    # Freshness writes sources.json; the source tests, run, snapshot and the other
    # tests write run_results.json.
    assert all(
        n == 5 and kinds == "run_results,run_results,run_results,run_results,sources"
        for _, n, kinds in nights
    )
    # One rerun per incident, after its fix: a build, and freshness first for a source root.
    reruns = {run_id: kinds for run_id, _, kinds in runs if run_id.startswith("rerun-")}
    assert len(reruns) == 4 and len(runs) == len(nights) + len(reruns)
    assert set(reruns.values()) <= {"run_results", "run_results,sources"}


def test_triage_shares_a_node_two_faults_break_and_raises_what_stays_open(
    company_warehouse: Settings,
) -> None:
    """F1 and F2 land the same day, and a support model reads both orders and tickets."""
    shared = _rows(
        company_warehouse,
        store.table(company_warehouse, "incident_shared_nodes"),
        "SELECT node, count(DISTINCT incident_id) FROM {} GROUP BY node ORDER BY node",
    )
    assert shared and all(incidents >= 2 for _, incidents in shared)
    raised = _rows(
        company_warehouse,
        store.table(company_warehouse, "incident_escalations"),
        "SELECT from_severity, to_severity, reason FROM {} ORDER BY at",
    )
    assert raised, "an incident left open past its severity's hours is raised"
    assert all(reason.startswith("open ") for *_, reason in raised)
    # Nothing in the window is a finance close: the suite's production week is mid-January.
