"""End to end: the incident scenario (SPEC Phase 3 acceptance).

The full 3-week scenario at scale 0.01, run twice in separate directories: once
through the real CLI, once through ``run_incidents`` with a full ``dbt run``
and ``dbt test`` added on every fault night. Proves:

- every injected fault maps to exactly one incident, and every incident to a fault;
- grouping cuts the noise: raw alerts outnumber incidents;
- a failure that persists into the next run appends to its open incident;
- two runs write a byte-identical ``incidents.md`` and the same postmortems;
- on those fault nights, the targeted run finds exactly the failing checks a
  full run finds, which is what justifies running only what faults touch.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings, load_settings
from platform_ops.incidents.notify import SLACK_ENV_VAR
from platform_ops.incidents.run import run_incidents
from platform_ops.incidents.scenario import ScenarioResult
from platform_ops.simulation.faults import CATALOGUE

pytestmark = pytest.mark.integration

runner = CliRunner()

# Detection nights of every fault pair: orders with support tickets, payments
# with a stale source, a dropped column with a volume drop, a renamed column
# with invalid payment statuses.
FULL_RUN_DAYS = (3, 6, 10, 15)


@pytest.fixture(scope="module")
def runs(
    tmp_path_factory: pytest.TempPathFactory, isolated_config: Callable[..., Path]
) -> Iterator[tuple[Settings, Settings, ScenarioResult]]:
    patch = pytest.MonkeyPatch()
    patch.delenv(SLACK_ENV_VAR, raising=False)
    try:
        first_path = isolated_config(tmp_path_factory.mktemp("inc_a"), 0.01, weeks=3)
        patch.setenv(CONFIG_PATH_ENV_VAR, str(first_path))
        result = runner.invoke(app, ["incidents", "run"])
        assert result.exit_code == 0, f"incidents run failed:\n{result.output}"
        patch.delenv(CONFIG_PATH_ENV_VAR)

        second = load_settings(isolated_config(tmp_path_factory.mktemp("inc_b"), 0.01, weeks=3))
        _, scenario, _ = run_incidents(second, allow_slack=False, full_run_on_days=FULL_RUN_DAYS)
    finally:
        patch.undo()
    yield load_settings(first_path), second, scenario


def _query(settings: Settings, sql: str) -> list[tuple[Any, ...]]:
    connection = duckdb.connect(str(settings.resolve(settings.paths.duckdb)), read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _reports(settings: Settings) -> Path:
    return settings.resolve(settings.paths.reports)


def test_every_fault_maps_to_exactly_one_incident(runs: tuple[Settings, ...]) -> None:
    settings = runs[0]
    per_fault = _query(
        settings,
        """SELECT g.fault_id, count(f.incident_id)
           FROM ops.fault_ground_truth g
           LEFT JOIN ops.fault_incidents f USING (fault_id)
           GROUP BY g.fault_id ORDER BY g.fault_id""",
    )
    assert per_fault == [(f.fault_id, 1) for f in sorted(CATALOGUE, key=lambda f: f.fault_id)]
    unmatched = _query(
        settings,
        """SELECT incident_id FROM ops.incidents
           WHERE incident_id NOT IN (SELECT incident_id FROM ops.fault_incidents)""",
    )
    assert unmatched == []


def test_raw_alerts_outnumber_incidents(runs: tuple[Settings, ...]) -> None:
    ((alerts, incidents),) = _query(
        runs[0],
        """SELECT (SELECT count(*) FROM ops.check_events),
                  (SELECT count(*) FROM ops.incidents)""",
    )
    assert alerts > incidents


def test_a_persisting_failure_appends_to_its_open_incident(runs: tuple[Settings, ...]) -> None:
    appended = _query(runs[0], "SELECT count(*) FROM ops.incidents WHERE runs > 1")
    assert appended[0][0] >= 1
    updates = _query(runs[0], "SELECT count(*) FROM ops.notifications WHERE kind = 'update'")
    pages = _query(runs[0], "SELECT count(*) FROM ops.notifications WHERE kind = 'page'")
    incidents = _query(runs[0], "SELECT count(*) FROM ops.incidents")
    assert updates[0][0] >= 1
    assert pages == incidents


def test_every_check_event_belongs_to_an_incident(runs: tuple[Settings, ...]) -> None:
    orphans = _query(runs[0], "SELECT count(*) FROM ops.check_events WHERE incident_id IS NULL")
    assert orphans == [(0,)]


def test_reports_are_byte_identical_across_runs(runs: tuple[Settings, ...]) -> None:
    first, second = _reports(runs[0]), _reports(runs[1])
    assert (first / "incidents.md").read_bytes() == (second / "incidents.md").read_bytes()
    names = sorted(p.name for p in (first / "postmortems").glob("INC-*.md"))
    assert names, "the scenario has SEV1 incidents, so there are postmortems"
    assert names == sorted(p.name for p in (second / "postmortems").glob("INC-*.md"))
    for name in names:
        assert (first / "postmortems" / name).read_bytes() == (
            second / "postmortems" / name
        ).read_bytes()


def test_a_targeted_run_finds_what_a_full_run_finds(
    runs: tuple[Settings, Settings, ScenarioResult],
) -> None:
    scenario = runs[2]
    assert sorted(scenario.equivalence) == list(FULL_RUN_DAYS)
    for day, (targeted, full) in scenario.equivalence.items():
        assert targeted, f"day {day} is a fault night, so something must fail"
        assert targeted == full, (
            f"day {day}: only targeted {targeted - full}, only full {full - targeted}"
        )
