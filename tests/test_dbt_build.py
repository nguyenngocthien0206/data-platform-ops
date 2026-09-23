"""End to end: seed, build and check through the real CLI, in a temp directory.

This is the SPEC acceptance path (`make seed && make build`, then
`platform-ops metadata check`) at scale 0.01, plus the two things that only a
real run can prove: that dbt-duckdb really appends the query comment to the SQL
it executes, and that the freshness override really measures simulated time.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings, load_settings
from platform_ops.common.dbt_invoke import invocation_from_settings, run_dbt
from platform_ops.metadata.lineage import build_graph
from platform_ops.metadata.manifest import load_manifest

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture(scope="module")
def built(
    tmp_path_factory: pytest.TempPathFactory,
    isolated_config: Callable[[Path, float], Path],
) -> Iterator[tuple[Settings, dict[str, Any]]]:
    root = tmp_path_factory.mktemp("build")
    settings_path = isolated_config(root, 0.01)
    patch = pytest.MonkeyPatch()
    patch.setenv(CONFIG_PATH_ENV_VAR, str(settings_path))
    results = {
        command: runner.invoke(app, command.split())
        for command in ("seed", "build", "metadata check")
    }
    yield load_settings(settings_path), results
    patch.undo()


def _query(settings: Settings, sql: str) -> list[tuple[Any, ...]]:
    connection = duckdb.connect(str(settings.resolve(settings.paths.duckdb)), read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _freshness(settings: Settings, **vars_override: Any) -> tuple[bool, dict[str, str]]:
    invocation = invocation_from_settings(settings)
    invocation = dataclasses.replace(invocation, vars={**invocation.vars, **vars_override})
    outcome = run_dbt(invocation, ["source", "freshness"], check=False)
    statuses = {}
    if outcome.result is not None:
        statuses = {r.node.unique_id: str(r.status) for r in outcome.result.results}
    return outcome.success, statuses


def test_seed_build_and_check_all_succeed(built: tuple[Settings, dict[str, Any]]) -> None:
    _, results = built
    for command, result in results.items():
        assert result.exit_code == 0, f"{command} failed:\n{result.output}"


def test_every_model_was_built_as_a_table(built: tuple[Settings, dict[str, Any]]) -> None:
    settings, _ = built
    tables = _query(
        settings,
        """SELECT count(*) FROM duckdb_tables()
           WHERE schema_name IN ('staging', 'intermediate', 'marts')""",
    )[0][0]
    models = sum(
        1
        for n in load_manifest(invocation_from_settings(settings).manifest_path).values()
        if n.resource_type == "model"
    )
    assert tables == models


def test_lineage_edges_match_the_manifest(built: tuple[Settings, dict[str, Any]]) -> None:
    settings, _ = built
    graph = build_graph(load_manifest(invocation_from_settings(settings).manifest_path))
    stored = _query(settings, "SELECT count(*) FROM ops.lineage_edges")[0][0]
    assert stored == graph.number_of_edges() > 0


def test_node_ownership_covers_every_dataset(built: tuple[Settings, dict[str, Any]]) -> None:
    settings, _ = built
    rows = _query(
        settings,
        "SELECT resource_type, count(*) FROM ops.node_ownership GROUP BY 1 ORDER BY 1",
    )
    assert dict(rows) == {"exposure": 12, "model": 118, "source": 9}


def test_executed_sql_carries_the_node_id_as_a_trailing_comment(
    built: tuple[Settings, dict[str, Any]],
) -> None:
    settings, _ = built
    log = (settings.resolve(settings.paths.dbt_target).parent / "logs" / "dbt.log").read_text(
        encoding="utf-8", errors="replace"
    )
    marker = '/* {"app": "platform-ops", "unique_id": "model.company.sales_fct_orders"} */'
    assert marker in log


def test_freshness_is_measured_on_simulated_time(built: tuple[Settings, dict[str, Any]]) -> None:
    """Passes at the window start, errors three simulated days later. Against the
    wall clock, the same data would be months stale in both cases."""
    settings, _ = built
    orders = "source.company.raw.orders"

    _, fresh = _freshness(settings)
    assert fresh[orders] == "pass"

    later = (settings.simulation.start + timedelta(days=3)).isoformat(sep=" ")
    stale_ok, stale = _freshness(settings, simulated_now=later)
    assert stale[orders] == "error"
    assert not stale_ok

    # Sources without a freshness rule are never evaluated, at any time.
    assert "source.company.raw.products" not in fresh


def test_freshness_without_simulated_time_fails_loudly(
    built: tuple[Settings, dict[str, Any]],
) -> None:
    settings, _ = built
    invocation = invocation_from_settings(settings)
    without = {k: v for k, v in invocation.vars.items() if k != "simulated_now"}
    outcome = run_dbt(
        dataclasses.replace(invocation, vars=without), ["source", "freshness"], check=False
    )
    assert not outcome.success
