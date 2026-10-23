"""End to end: the cost report on the company's own query log (Phase 13).

Read from the shared ``company_warehouse`` (two weeks at scale 0.01, the second
a short parallel run), after ``cost report`` through the real CLI. Proves:

- every query the run tagged is collected, the billable ones are priced under
  all three models, and every query is attributed exactly once;
- the cluster's shares add up to its stated cost, prorated to the window, to
  the cent and beyond;
- every abandoned model is in the unused list, and no model with a dashboard
  downstream is; nobody's dashboards or ad hoc queries read an abandoned model;
- what ClickHouse says each dbt build read is that model's lineage;
- the report is a pure function of the collected data.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg import sql
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings
from platform_ops.common.dbt_invoke import invocation_from_settings
from platform_ops.common.store import open_store, ops_table
from platform_ops.cost import cluster
from platform_ops.metadata.lineage import build_graph, downstream, upstream
from platform_ops.metadata.manifest import Node, load_manifest

pytestmark = pytest.mark.integration


def _cost_report(settings: Settings) -> Path:
    patch = pytest.MonkeyPatch()
    patch.setenv(CONFIG_PATH_ENV_VAR, str(settings.root / "config" / "settings.yaml"))
    try:
        result = CliRunner().invoke(app, ["cost", "report"])
    finally:
        patch.undo()
    assert result.exit_code == 0, result.output
    return settings.resolve(settings.paths.reports) / "cost.md"


@pytest.fixture(scope="module")
def costed(company_warehouse: Settings) -> tuple[Settings, bytes]:
    """The shared company's cost report, written by the fixture through the CLI."""
    report = company_warehouse.resolve(company_warehouse.paths.reports) / "cost.md"
    return company_warehouse, report.read_bytes()


def _rows(settings: Settings, table: str, query: str) -> list[tuple[Any, ...]]:
    with open_store(settings) as store:
        return list(store.execute(sql.SQL(query).format(ops_table(settings, table))).fetchall())


def _nodes(settings: Settings) -> dict[str, Node]:
    return load_manifest(invocation_from_settings(settings).manifest_path)


def test_every_kind_of_work_is_collected_and_tagged(costed: tuple[Settings, bytes]) -> None:
    settings, _ = costed
    kinds = dict(
        _rows(
            settings,
            "query_log",
            "SELECT actor_type, count(*) FROM {} WHERE billable GROUP BY 1",
        )
    )
    assert set(kinds) == {"dbt", "ingestion", "maintenance", "reconcile", "dashboard", "adhoc"}
    assert all(n > 0 for n in kinds.values())
    ((untyped,),) = _rows(
        settings, "query_log", "SELECT count(*) FROM {} WHERE actor = '' OR started_at IS NULL"
    )
    assert untyped == 0


def test_billable_queries_are_priced_three_ways_and_attributed_once(
    costed: tuple[Settings, bytes],
) -> None:
    settings, _ = costed
    ((billable, everything),) = _rows(
        settings, "query_log", "SELECT count(*) FILTER (WHERE billable), count(*) FROM {}"
    )
    priced = dict(_rows(settings, "query_costs", "SELECT model, count(*) FROM {} GROUP BY 1"))
    # A query that read nothing gets no share of the cluster; it still has a price elsewhere.
    assert priced["scan"] == priced["compute"] == billable > 0
    assert 0 < priced["cluster"] <= billable
    ((attributed, unattributed, worst),) = _rows(
        settings,
        "query_attribution",
        "SELECT count(DISTINCT query_id), count(*) FILTER (WHERE team = 'unattributed'), "
        "max(abs(total - 1)) FROM (SELECT query_id, team, sum(share) OVER "
        "(PARTITION BY query_id) AS total FROM {}) shares",
    )
    assert attributed == everything
    assert unattributed == 0
    # A query shared among its dashboard's viewers still adds up to one query.
    assert worst < Decimal("1e-9")


def test_a_dashboards_refreshes_are_paid_by_the_teams_that_open_it(
    costed: tuple[Settings, bytes],
) -> None:
    settings, report = costed
    ((views,),) = _rows(settings, "dashboard_views", "SELECT count(*) FROM {}")
    assert views > 0
    # Some of what dashboards cost is paid by a team other than the owner.
    shared = _rows(
        settings,
        "query_attribution",
        "SELECT count(DISTINCT query_id) FROM {} a WHERE cost_kind = 'consumption' AND share < 1",
    )
    assert shared[0][0] > 0
    assert b"## Dashboards and who reads them" in report


def test_the_cluster_shares_add_up_to_the_stated_cost(costed: tuple[Settings, bytes]) -> None:
    settings, report = costed
    ((start, days),) = _rows(settings, "company_run", "SELECT window_start, days FROM {}")
    pools = cluster.pools(settings.cost.clickhouse, cluster.window_months(start, days))
    stated = sum((p.total_usd for p in pools), Decimal(0))
    ((shared,),) = _rows(
        settings, "cost_showback", "SELECT sum(usd) FROM {} WHERE model = 'cluster'"
    )
    assert shared == stated
    assert b"the shares add up to the bill exactly" in report


def test_unused_list_is_exactly_the_abandoned_models(costed: tuple[Settings, bytes]) -> None:
    settings, report = costed
    nodes = _nodes(settings)
    abandoned = {n.relation for n in nodes.values() if n.folder == "abandoned"}
    assert len(abandoned) == 12
    section = report.decode().split("### Unused tables", 1)[1].split("### Full-scan", 1)[0]
    listed = {
        line.split(" | ")[0].lstrip("| ") for line in section.splitlines() if "_marts." in line
    }
    assert listed == abandoned
    graph = build_graph(nodes)
    for relation in listed:
        (unique_id,) = (u for u, n in nodes.items() if n.relation == relation)
        exposures = {n for n in downstream(graph, unique_id) if n.startswith("exposure.")}
        assert not exposures, f"{unique_id} feeds {exposures}"


def test_consumers_never_read_abandoned_models(costed: tuple[Settings, bytes]) -> None:
    settings, _ = costed
    abandoned = {n.relation for n in _nodes(settings).values() if n.folder == "abandoned"}
    read = {
        table
        for (tables,) in _rows(
            settings,
            "query_log",
            "SELECT DISTINCT tables FROM {} WHERE actor_type IN ('dashboard', 'adhoc')",
        )
        for table in tables.split(",")
        if table
    }
    assert read and not (read & abandoned)


def test_what_each_build_read_is_its_lineage(costed: tuple[Settings, bytes]) -> None:
    """Its parents, plus the tables behind any view among them: ClickHouse
    reports reading both when a query reads a view."""
    settings, _ = costed
    nodes = _nodes(settings)
    graph = build_graph(nodes)
    rows = _rows(
        settings,
        "query_log",
        "SELECT DISTINCT node_id, tables FROM {} "
        "WHERE actor_type = 'dbt' AND query_kind = 'Insert' AND node_id LIKE 'model.%%'",
    )
    manifest = json.loads(invocation_from_settings(settings).manifest_path.read_text("utf-8"))
    tables = {
        uid
        for uid, n in nodes.items()
        if n.resource_type == "model" and manifest["nodes"][uid]["config"]["materialized"] != "view"
    }
    assert {node_id for node_id, _ in rows} == tables
    for node_id, tables in rows:
        node = nodes[node_id]
        # The table being written (or its __dbt_backup twin) is not a read.
        read = {t for t in tables.split(",") if t and not t.startswith(node.relation)}
        parents = {nodes[p].relation for p in node.depends_on}
        behind = {nodes[u].relation for u in upstream(graph, node_id)}
        assert parents <= read <= behind, node_id


def test_the_report_is_a_pure_function_of_the_collected_data(
    costed: tuple[Settings, bytes],
) -> None:
    settings, report = costed
    showback = _rows(settings, "cost_showback", "SELECT * FROM {} ORDER BY 1, 2, 3, 4, 5")
    assert _cost_report(settings).read_bytes() == report
    assert _rows(settings, "cost_showback", "SELECT * FROM {} ORDER BY 1, 2, 3, 4, 5") == showback
    for section in ("## Assumptions", "## Showback by team", "## The migration's business case"):
        assert section.encode() in report
