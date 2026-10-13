"""End to end: the company on Postgres and ClickHouse, through the real CLI (version 2.0).

The shared ``company_warehouse`` is ``make demo``'s first half at scale 0.01
over two weeks: the legacy Postgres seeded and fed daily, the raw layer pulled
into ClickHouse by watermark, the dbt project built in full on the first day
and incrementally on the last, then the ownership check. These tests read what
it left behind: the engines and keys each layer was built with, the ops store,
the query comments ClickHouse logged, freshness on simulated time, the legacy
reports against the marts that replace them, and the customer snapshot. One
more run proves that two runs leave identical data.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg import sql

from platform_ops.common.config import Settings, load_settings
from platform_ops.common.dbt_invoke import invocation_from_settings, run_dbt
from platform_ops.common.servers import clickhouse_client, connect_postgres
from platform_ops.common.store import open_store, ops_table
from platform_ops.metadata.lineage import build_graph
from platform_ops.metadata.manifest import Node, load_manifest
from platform_ops.reconcile.parallel import ParallelRun
from platform_ops.reconcile.report import write_report
from platform_ops.simulation import raw_data
from platform_ops.simulation.company import fingerprints, remove_company, run_company
from platform_ops.simulation.legacy_reports import report_names

pytestmark = pytest.mark.integration

# The large event tables, rebuilt a month at a time.
INCREMENTAL = {
    "int_order_items_enriched",
    "int_sessions_enriched",
    "sales_fct_order_items",
    "marketing_fct_sessions",
}

# How dbt's query comment starts on a model's SQL (dbt_project.yml, query-comment).
MARKER = '/* {"app": "platform-ops", "unique_id": "model.company.sales_fct_orders"} */'

# Each legacy report and the dbt mart that replaces it on ClickHouse: one per
# dashboard (exposure). Phase 12 reconciles them row by row; here their totals
# must already agree.
REPORT_MARTS = {
    "sales_daily": "sales_fct_daily_sales",
    "customer_cohorts": "sales_customer_cohorts",
    "orders_daily_by_channel": "sales_rpt_orders_daily_by_channel",
    "campaign_performance": "marketing_fct_campaign_performance",
    "sessions_daily_by_traffic_source": "marketing_rpt_sessions_daily_by_traffic_source",
    "acquisition_funnel": "marketing_acquisition_funnel",
    "revenue_monthly": "finance_fct_revenue_monthly",
    "margin_monthly_by_channel": "finance_rpt_margin_monthly_by_channel",
    "payments_reconciliation": "finance_fct_payments_reconciliation",
    "product_sales_monthly_by_category": "product_rpt_product_sales_monthly_by_category",
    "engagement_monthly_by_device": "product_rpt_engagement_monthly_by_device",
    "ticket_sla": "product_fct_ticket_sla",
}


def _clickhouse(settings: Settings, query: str, **parameters: Any) -> list[tuple[Any, ...]]:
    client = clickhouse_client(settings)
    try:
        return list(client.query(query, parameters=parameters).result_rows)
    finally:
        client.close()


def _models(settings: Settings) -> dict[str, Node]:
    nodes = load_manifest(invocation_from_settings(settings).manifest_path)
    return {n.name: n for n in nodes.values() if n.resource_type == "model"}


def _freshness(settings: Settings, at: Any) -> tuple[bool, dict[str, str]]:
    invocation = invocation_from_settings(settings, simulated_now=at)
    outcome = run_dbt(invocation, ["source", "freshness"], check=False)
    statuses = {}
    if outcome.result is not None:
        statuses = {r.node.unique_id: str(r.status) for r in outcome.result.results}
    return outcome.success, statuses


def _last_run(settings: Settings) -> Any:
    window = settings.simulation
    return window.start + timedelta(days=window.weeks * 7 - 1, hours=window.daily_run_hour)


# -- how each layer was built -------------------------------------------------------------


def test_every_layer_is_built_with_its_engine_and_key(company_warehouse: Settings) -> None:
    company = company_warehouse.company
    rows = _clickhouse(
        company_warehouse,
        "SELECT database, name, engine, sorting_key, partition_key FROM system.tables "
        "WHERE database IN %(dbs)s",
        dbs=[company.name(d) for d in ("raw", "staging", "intermediate", "marts")],
    )
    tables = {(db.removeprefix(company.namespace), name): rest for db, name, *rest in rows}
    raw = {name: rest for (layer, name), rest in tables.items() if layer == "raw"}
    assert set(raw) == set(raw_data.TABLES)
    for table, (engine, sorting_key, partition_key) in raw.items():
        assert engine == "ReplacingMergeTree", table
        assert sorting_key == raw_data.PRIMARY_KEYS[table], table
        assert partition_key == "", "a partition would keep both versions of a changed row"

    models = _models(company_warehouse)
    built = {name: rest for (layer, name), rest in tables.items() if layer != "raw"}
    assert set(built) == set(models)
    for name, (engine, sorting_key, partition_key) in built.items():
        folder = models[name].folder
        if folder == "staging":
            assert engine == "View", name
            continue
        assert engine == "MergeTree", name
        if folder != "abandoned":
            assert sorting_key, f"{name} has no sorting key"
        if name in INCREMENTAL:
            assert "toYYYYMM(" in partition_key, name
        else:
            assert partition_key == "", name


def test_executed_sql_carries_the_node_id_in_clickhouses_query_log(
    company_warehouse: Settings,
) -> None:
    """What the cost module joins on in Phase 13: the node, in the log ClickHouse keeps."""
    marts = company_warehouse.company.name("marts")
    client = clickhouse_client(company_warehouse)
    try:
        client.command("SYSTEM FLUSH LOGS")
        (count,) = client.query(
            "SELECT count() FROM system.query_log WHERE type = 'QueryFinish' "
            "AND startsWith(query, %(marker)s) AND has(databases, %(marts)s)",
            parameters={
                "marker": MARKER,
                "marts": marts,
            },
        ).result_rows[0]
    finally:
        client.close()
    assert count > 0


# -- the ops store -------------------------------------------------------------------------


def _store(settings: Settings, query: str, table: str) -> list[tuple[Any, ...]]:
    with open_store(settings) as connection:
        return connection.execute(sql.SQL(query).format(ops_table(settings, table))).fetchall()


def test_lineage_edges_match_the_manifest(company_warehouse: Settings) -> None:
    graph = build_graph(load_manifest(invocation_from_settings(company_warehouse).manifest_path))
    ((stored,),) = _store(company_warehouse, "SELECT count(*) FROM {}", "lineage_edges")
    assert stored == graph.number_of_edges() > 0


def test_node_ownership_covers_every_dataset(company_warehouse: Settings) -> None:
    rows = _store(
        company_warehouse,
        "SELECT resource_type, count(*) FROM {} GROUP BY 1 ORDER BY 1",
        "node_ownership",
    )
    assert dict(rows) == {"exposure": 12, "model": 118, "seed": 1, "snapshot": 1, "source": 9}


def test_every_load_is_recorded_with_its_range(company_warehouse: Settings) -> None:
    days = company_warehouse.simulation.weeks * 7
    rows = _store(
        company_warehouse,
        "SELECT table_name, count(*), count(loaded_from), max(loaded_until) FROM {} GROUP BY 1",
        "ingestion_loads",
    )
    assert {name for name, *_ in rows} == set(raw_data.TABLES)
    for _, loads, with_range, until in rows:
        # The first load and the fixed job's backfill read everything; every
        # other daily load starts at a watermark.
        assert (loads, with_range, until) == (days + 1, days - 1, _last_run(company_warehouse))


# -- freshness on simulated time --------------------------------------------------------------


def test_freshness_is_measured_on_simulated_time(company_warehouse: Settings) -> None:
    """Passes right after the last load, errors three simulated days later. Against
    the wall clock, the same data would be months stale in both cases."""
    orders = "source.company.raw.orders"
    last = _last_run(company_warehouse)

    _, fresh = _freshness(company_warehouse, last)
    assert fresh[orders] == "pass"

    stale_ok, stale = _freshness(company_warehouse, last + timedelta(days=3))
    assert stale[orders] == "error"
    assert not stale_ok

    # Sources without a freshness rule are never evaluated, at any time.
    assert "source.company.raw.products" not in fresh


def test_freshness_without_simulated_time_fails_loudly(company_warehouse: Settings) -> None:
    invocation = invocation_from_settings(company_warehouse)  # no simulated time given
    assert "simulated_now" not in invocation.vars
    outcome = run_dbt(invocation, ["source", "freshness"], check=False)
    assert not outcome.success


# -- the legacy reports and the marts that replace them -------------------------------------


def _report_totals(settings: Settings, report: str, columns: list[str]) -> list[Any]:
    schema = settings.company.name(settings.company.reporting_schema)
    selected = sql.SQL(", ").join(
        sql.SQL("coalesce(sum({}), 0)").format(sql.Identifier(c)) for c in columns
    )
    query = sql.SQL("SELECT count(*), {} FROM {}.{}").format(
        selected, sql.Identifier(schema), sql.Identifier(report)
    )
    with connect_postgres(settings) as connection:
        row = connection.execute(query).fetchone()
    assert row is not None
    return list(row)


def _mart_totals(settings: Settings, mart: str, columns: list[str]) -> list[Any]:
    database = settings.company.name("marts")
    selected = ", ".join(f"coalesce(sum(`{c}`), 0)" for c in columns)
    return list(_clickhouse(settings, f"SELECT count(), {selected} FROM `{database}`.`{mart}`")[0])


def _numeric_columns(settings: Settings, report: str) -> list[str]:
    schema = settings.company.name(settings.company.reporting_schema)
    with connect_postgres(settings) as connection:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "AND data_type IN ('bigint', 'integer', 'numeric', 'double precision') "
            "ORDER BY ordinal_position",
            [schema, report],
        ).fetchall()
    return [name for (name,) in rows]


def _same(left: Any, right: Any) -> bool:
    """Equal to the cent. Averages are Float64 in ClickHouse, so their sums may
    differ from Postgres's exact ones in the last bits."""
    return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal("0.01")


def test_there_is_one_legacy_report_per_dashboard() -> None:
    assert sorted(report_names()) == sorted(REPORT_MARTS)


@pytest.mark.parametrize("report", sorted(REPORT_MARTS))
def test_each_legacy_report_agrees_with_its_mart(company_warehouse: Settings, report: str) -> None:
    """Row count and the total of every numeric column, legacy Postgres against ClickHouse."""
    mart = REPORT_MARTS[report]
    mart_columns = {
        name
        for (name,) in _clickhouse(
            company_warehouse,
            "SELECT name FROM system.columns WHERE database = %(db)s AND table = %(t)s",
            db=company_warehouse.company.name("marts"),
            t=mart,
        )
    }
    columns = [c for c in _numeric_columns(company_warehouse, report) if c in mart_columns]
    assert columns, f"{report} shares no numeric column with {mart}"
    legacy = _report_totals(company_warehouse, report, columns)
    new = _mart_totals(company_warehouse, mart, columns)
    assert legacy[0] == new[0] > 0, f"{report}: {legacy[0]} rows, {mart}: {new[0]}"
    for column, a, b in zip(columns, legacy[1:], new[1:], strict=True):
        assert _same(a, b), f"{report}.{column} = {a}, {mart}.{column} = {b}"


# -- history and repeatability ---------------------------------------------------------------


def test_the_snapshot_keeps_the_customers_earlier_versions(company_warehouse: Settings) -> None:
    snapshots = company_warehouse.company.name("snapshots")
    ((rows, customers, closed),) = _clickhouse(
        company_warehouse,
        f"SELECT count(), uniqExact(customer_id), countIf(dbt_valid_to IS NOT NULL) "
        f"FROM `{snapshots}`.`snap_customers`",
    )
    assert closed > 0, "profiles changed between the two builds"
    assert rows == customers + closed


def test_two_runs_leave_identical_data(
    company_warehouse: Settings,
    tmp_path: Path,
    isolated_config: Callable[..., Path],
    short_parallel_run: dict[str, dict[str, Any]],
) -> None:
    """Every table and view in ClickHouse, by row count and content hash, every
    legacy table in Postgres, and the sign-off report, after a second run in its
    own namespace, parallel run and all."""
    second = load_settings(
        isolated_config(tmp_path, 0.01, weeks=2, namespace="t_twice_", overrides=short_parallel_run)
    )
    try:
        reconciliation = ParallelRun(second)
        run_company(second, nightly=reconciliation)
        reconciliation.finish()
        assert fingerprints(second) == fingerprints(company_warehouse)
        assert _legacy_fingerprints(second) == _legacy_fingerprints(company_warehouse)
        mine = company_warehouse.resolve(company_warehouse.paths.reports) / "reconciliation.md"
        assert write_report(second).read_bytes() == mine.read_bytes()
    finally:
        remove_company(second)


def _legacy_fingerprints(settings: Settings) -> dict[str, tuple[int, str]]:
    schema = sql.Identifier(settings.company.name(settings.company.legacy_schema))
    found = {}
    with connect_postgres(settings) as connection:
        for table in raw_data.TABLES:
            key = sql.Identifier(raw_data.PRIMARY_KEYS[table])
            row = connection.execute(
                sql.SQL(
                    "SELECT count(*), md5(string_agg(t::text, '|' ORDER BY t.{})) FROM {}.{} t"
                ).format(key, schema, sql.Identifier(table))
            ).fetchone()
            assert row is not None
            found[table] = (row[0], row[1])
    return found


def test_an_incremental_build_equals_a_full_rebuild(company_warehouse: Settings) -> None:
    """The last build was incremental; rebuilding the event tables in full changes nothing.

    Runs last in the module, because it rebuilds tables the other tests read
    (with the same content, if it passes).
    """
    names = sorted(INCREMENTAL)
    before = {k: v for k, v in fingerprints(company_warehouse).items() if k.split(".")[1] in names}
    assert len(before) == len(INCREMENTAL)
    run_dbt(
        invocation_from_settings(company_warehouse),
        ["run", "--full-refresh", "--select", *names],
    )
    after = {k: v for k, v in fingerprints(company_warehouse).items() if k.split(".")[1] in names}
    assert after == before
