"""End to end: simulate, then cost, through the real CLI (SPEC Phase 2 acceptance).

One simulated week at scale 0.01, run twice in separate directories. Proves:

- ``make simulate && make cost`` produce the report deterministically: two runs
  give byte-identical ``cost.md`` files;
- every abandoned model is in the unused list, and no model with a live
  dashboard downstream is;
- nobody's dashboards or ad hoc queries read an abandoned model;
- for every dbt build query, the tables the SQL parser found are exactly the
  model's parents in dbt's lineage.
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
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.metadata.lineage import build_graph, downstream
from platform_ops.metadata.manifest import Node, load_manifest

pytestmark = pytest.mark.integration

runner = CliRunner()


def _simulate_and_cost(root: Path, isolated_config: Callable[..., Path]) -> Settings:
    settings_path = isolated_config(root, 0.01, weeks=1)
    patch = pytest.MonkeyPatch()
    patch.setenv(CONFIG_PATH_ENV_VAR, str(settings_path))
    try:
        for command in ("simulation run", "cost report"):
            result = runner.invoke(app, command.split())
            assert result.exit_code == 0, f"{command} failed:\n{result.output}"
    finally:
        patch.undo()
    return load_settings(settings_path)


@pytest.fixture(scope="module")
def runs(
    tmp_path_factory: pytest.TempPathFactory, isolated_config: Callable[..., Path]
) -> Iterator[tuple[Settings, Settings]]:
    first = _simulate_and_cost(tmp_path_factory.mktemp("cost_a"), isolated_config)
    second = _simulate_and_cost(tmp_path_factory.mktemp("cost_b"), isolated_config)
    yield first, second


def _query(settings: Settings, sql: str) -> list[tuple[Any, ...]]:
    connection = duckdb.connect(str(settings.resolve(settings.paths.duckdb)), read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _nodes(settings: Settings) -> dict[str, Node]:
    return load_manifest(invocation_from_settings(settings).manifest_path)


def test_two_runs_produce_byte_identical_cost_reports(runs: tuple[Settings, Settings]) -> None:
    first, second = runs
    report_a = (first.resolve(first.paths.reports) / "cost.md").read_bytes()
    report_b = (second.resolve(second.paths.reports) / "cost.md").read_bytes()
    assert report_a == report_b
    assert b"## Showback by team" in report_a


def test_unused_list_is_exactly_the_abandoned_models(runs: tuple[Settings, Settings]) -> None:
    settings, _ = runs
    nodes = _nodes(settings)
    abandoned = {n.relation for n in nodes.values() if n.folder == "abandoned"}
    assert len(abandoned) == 12
    for days in settings.recommendations.unused_lookback_days:
        unused = {
            r[0]
            for r in _query(
                settings,
                f"SELECT relation FROM ops.cost_report_unused WHERE lookback_days = {days}",
            )
        }
        assert unused == abandoned, f"{days}-day list"


def test_no_model_feeding_a_dashboard_is_called_unused(runs: tuple[Settings, Settings]) -> None:
    settings, _ = runs
    nodes = _nodes(settings)
    graph = build_graph(nodes)
    unused = {r[0] for r in _query(settings, "SELECT unique_id FROM ops.cost_report_unused")}
    for unique_id in unused:
        exposures = {n for n in downstream(graph, unique_id) if n.startswith("exposure.")}
        assert not exposures, f"{unique_id} feeds {exposures}"


def test_consumers_never_read_abandoned_models(runs: tuple[Settings, Settings]) -> None:
    settings, _ = runs
    abandoned = {n.relation for n in _nodes(settings).values() if n.folder == "abandoned"}
    read = {
        r[0]
        for r in _query(
            settings,
            """SELECT DISTINCT qt.table_name FROM ops.query_tables qt
               JOIN ops.query_log ql USING (query_id)
               WHERE ql.actor_type IN ('dashboard', 'adhoc')""",
        )
    }
    assert read and not (read & abandoned)


def test_parsed_reads_of_every_model_build_match_dbt_lineage(
    runs: tuple[Settings, Settings],
) -> None:
    settings, _ = runs
    nodes = _nodes(settings)
    rows = _query(
        settings,
        """SELECT ql.node_id, list(qt.table_name ORDER BY qt.table_name)
           FROM ops.query_log ql JOIN ops.query_tables qt USING (query_id)
           WHERE ql.actor_type = 'dbt' AND ql.node_id LIKE 'model.%' AND NOT ql.replayed
           GROUP BY ql.node_id""",
    )
    assert len(rows) == sum(1 for n in nodes.values() if n.resource_type == "model")
    for node_id, tables in rows:
        expected = sorted(nodes[d].relation for d in nodes[node_id].depends_on)
        assert tables == expected, node_id


def test_every_query_is_priced_under_both_models_and_attributed_once(
    runs: tuple[Settings, Settings],
) -> None:
    settings, _ = runs
    (counts,) = _query(
        settings,
        """SELECT (SELECT count(*) FROM ops.query_log),
                  (SELECT count(*) FROM ops.query_costs WHERE model = 'scan'),
                  (SELECT count(*) FROM ops.query_costs WHERE model = 'compute'),
                  (SELECT count(*) FROM ops.query_attribution),
                  (SELECT count(*) FROM ops.query_attribution WHERE team = 'unattributed')""",
    )
    queries, scan, compute, attributed, unattributed = counts
    assert queries == scan == compute == attributed > 0
    assert unattributed == 0
    (totals,) = _query(
        settings,
        """SELECT (SELECT sum(total_usd) FROM ops.cost_by_team WHERE pricing_model = 'scan'),
                  (SELECT sum(usd) FROM ops.query_costs WHERE model = 'scan')""",
    )
    assert totals[0] == totals[1], "team totals add up to the bill"
