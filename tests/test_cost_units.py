"""Unit tests for the cost pipeline pieces, on small hand-built data."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from platform_ops.common.config import Settings
from platform_ops.common.db import connect
from platform_ops.cost import attribution, growth, recommend, sizes
from platform_ops.cost.collect import QueryLog, QueryRecord, _shape, read_dbt_run
from platform_ops.cost.estimate import estimate
from platform_ops.cost.schema import reset_collection_tables
from platform_ops.metadata.lineage import build_graph
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import DatasetRule, Registry, Team
from platform_ops.simulation import workload

T0 = datetime(2026, 1, 5, 2, 0, 0)


@pytest.fixture
def db() -> duckdb.DuckDBPyConnection:
    connection = connect(path=":memory:")
    reset_collection_tables(connection)
    return connection


# --- sizes ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "width"),
    [("BIGINT", 8), ("DATE", 4), ("DECIMAL(12,2)", 8), ("DECIMAL(38, 12)", 16), ("VARCHAR", None)],
)
def test_fixed_widths(data_type: str, width: int | None) -> None:
    assert sizes.width_of(data_type) == width


def test_logical_size_counts_non_null_values_and_string_bytes(
    db: duckdb.DuckDBPyConnection,
) -> None:
    db.execute("CREATE TABLE raw.t (id BIGINT, name VARCHAR)")
    db.execute("INSERT INTO raw.t VALUES (1, 'ab'), (2, NULL), (NULL, 'hé')")  # 'é' is 2 bytes
    rows, measured = sizes.measure(db, "raw.t")
    assert rows == 3
    assert measured["id"] == ("BIGINT", 16)
    assert measured["name"] == ("VARCHAR", 5)


def test_adding_a_delta_equals_measuring_everything(db: duckdb.DuckDBPyConnection) -> None:
    db.execute("CREATE TABLE raw.t (id BIGINT, name VARCHAR, _loaded_at TIMESTAMP)")
    db.execute(f"INSERT INTO raw.t VALUES (1, 'a', TIMESTAMP '{T0}'), (2, 'bb', TIMESTAMP '{T0}')")
    base = sizes.measure(db, "raw.t")
    later = T0 + timedelta(days=1)
    db.execute(f"INSERT INTO raw.t VALUES (3, 'ccc', TIMESTAMP '{later}')")
    delta = sizes.measure(db, "raw.t", f"_loaded_at > TIMESTAMP '{T0}'")
    assert sizes.add(base, delta) == sizes.measure(db, "raw.t")


# --- growth --------------------------------------------------------------------------


def test_growth_tells_appends_from_in_place_changes(db: duckdb.DuckDBPyConnection) -> None:
    db.execute("CREATE TABLE raw.t (id BIGINT, v VARCHAR, _loaded_at TIMESTAMP)")
    db.execute(f"INSERT INTO raw.t VALUES (1, 'a', TIMESTAMP '{T0}')")
    _, first, flag = growth.observe(db, "t", "id", T0, None)
    assert flag is None

    day2 = T0 + timedelta(days=1)
    db.execute(f"INSERT INTO raw.t VALUES (2, 'b', TIMESTAMP '{day2}')")
    _, second, appended = growth.observe(db, "t", "id", day2, (T0, first))
    assert appended is True

    day3 = T0 + timedelta(days=2)
    db.execute(f"UPDATE raw.t SET v = 'z', _loaded_at = TIMESTAMP '{day3}' WHERE id = 1")
    _, _, changed = growth.observe(db, "t", "id", day3, (day2, second))
    assert changed is False
    assert growth.append_only_sources(db) == {"raw.t": False}


# --- estimate ------------------------------------------------------------------------


def _log(db: duckdb.DuckDBPyConnection, query_id: str, at: datetime, resolved: bool = True) -> None:
    db.execute(
        "INSERT INTO ops.query_log VALUES (?, 'r', 'u', 'adhoc', NULL, ?, 'sql', false, "
        "NULL, NULL, NULL, false, ?, NULL)",
        [query_id, at, resolved],
    )


def test_estimate_uses_the_snapshot_current_when_the_query_ran(
    db: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    snapshots = [(T0, 1000), (T0 + timedelta(days=1), 3000)]
    for at, size in snapshots:
        db.execute(
            "INSERT INTO ops.table_sizes VALUES (?, 'marts.t', 'a', 'BIGINT', 1, ?), "
            "(?, 'marts.t', 'b', 'BIGINT', 1, 500)",
            [at, size, at],
        )
    _log(db, "early", T0 + timedelta(hours=1))
    _log(db, "late", T0 + timedelta(days=1, hours=1))
    _log(db, "star", T0 + timedelta(hours=1), resolved=False)
    _log(db, "count", T0 + timedelta(hours=1))
    for query_id, columns in [("early", ["a"]), ("late", ["a"]), ("star", []), ("count", [])]:
        db.execute("INSERT INTO ops.query_tables VALUES (?, 'marts.t', ?, [])", [query_id, columns])

    estimate(db, settings)
    got = dict(db.execute("SELECT query_id, bytes_scanned FROM ops.query_estimates").fetchall())
    assert got == {"early": 1000, "late": 3000, "star": 1500, "count": 0}
    modeled = db.execute(
        "SELECT modeled_ms FROM ops.query_estimates WHERE query_id = 'count'"
    ).fetchone()
    assert modeled == (settings.cost.per_query_overhead_ms,)


# --- attribution ---------------------------------------------------------------------

TEAMS = {
    "platform": Team(id="platform", name="P", channel="#p", members=["priya"]),
    "sales": Team(id="sales", name="S", channel="#s", members=["sam"]),
}
RULES = (
    DatasetRule(match="model.company.stg_*", owner="priya", team="platform", tier="important"),
    DatasetRule(match="model.company.sales_*", owner="sam", team="sales", tier="important"),
    DatasetRule(match="exposure.company.sales_*", owner="sam", team="sales", tier="important"),
)
REGISTRY = Registry(teams=TEAMS, rules=RULES)
NODES = {
    n.unique_id: n
    for n in [
        Node("model.company.stg_orders", "model", "stg_orders", schema="staging"),
        Node(
            "model.company.sales_fct",
            "model",
            "sales_fct",
            depends_on=("model.company.stg_orders",),
            schema="marts",
        ),
        Node(
            "test.company.unique_sales_fct",
            "test",
            "unique_sales_fct",
            depends_on=("model.company.sales_fct", "model.company.stg_orders"),
            attached_node="model.company.sales_fct",
        ),
        Node(
            "exposure.company.sales_board",
            "exposure",
            "sales_board",
            depends_on=("model.company.sales_fct",),
            owner_name="sam",
        ),
    ]
}
RESOLUTIONS = {uid: REGISTRY.resolve(uid) for uid in NODES}


def test_each_query_is_charged_once_to_the_right_team() -> None:
    rows = [
        ("q1", "dbt", "dbt", "model.company.stg_orders"),
        ("q2", "dbt", "dbt", "test.company.unique_sales_fct"),
        ("q3", "sam", "dashboard", "exposure.company.sales_board"),
        ("q4", "priya", "adhoc", None),
        ("q5", "stranger", "adhoc", None),
    ]
    got = {a.query_id: a for a in attribution.attribute(rows, NODES, RESOLUTIONS, REGISTRY)}
    assert (got["q1"].cost_kind, got["q1"].team) == ("production", "platform")
    # A test belongs to the model it tests, even though it also reads stg_orders.
    assert (got["q2"].team, got["q2"].subject) == ("sales", "model.company.sales_fct")
    assert (got["q3"].cost_kind, got["q3"].team) == ("consumption", "sales")
    assert (got["q4"].team, got["q4"].owner) == ("platform", "priya")
    assert got["q5"].team == attribution.UNATTRIBUTED
    assert len(got) == len(rows)


# --- recommendations -----------------------------------------------------------------


def _costs(db: duckdb.DuckDBPyConnection, rows: list[tuple[str, str, str]]) -> None:
    db.execute(
        "CREATE TABLE ops.query_costs (query_id VARCHAR, model VARCHAR, usd DECIMAL(24,12), "
        "billed_bytes BIGINT, billed_ms BIGINT, warehouse VARCHAR, burst VARCHAR)"
    )
    db.execute(
        "CREATE TABLE ops.query_attribution (query_id VARCHAR, cost_kind VARCHAR, team VARCHAR, "
        "owner VARCHAR, subject VARCHAR)"
    )
    for query_id, subject, usd in rows:
        for model in ("scan", "compute"):
            db.execute(
                "INSERT INTO ops.query_costs VALUES (?, ?, ?, 0, 0, NULL, NULL)",
                [query_id, model, usd],
            )
        db.execute(
            "INSERT INTO ops.query_attribution VALUES (?, 'production', 't', 'o', ?)",
            [query_id, subject],
        )


def _write(db: duckdb.DuckDBPyConnection, query_id: str, relation: str, at: datetime) -> None:
    db.execute(
        "INSERT INTO ops.query_log VALUES (?, 'r', 'dbt', 'dbt', NULL, ?, 'sql', false, "
        "NULL, NULL, ?, false, true, NULL)",
        [query_id, at, relation],
    )


def test_unused_tables_respect_lineage_and_lookback(db: duckdb.DuckDBPyConnection) -> None:
    nodes = {
        n.unique_id: n
        for n in [
            Node("model.company.a", "model", "a", schema="staging"),
            Node("model.company.b", "model", "b", depends_on=("model.company.a",), schema="marts"),
            Node(
                "model.company.dead",
                "model",
                "dead",
                depends_on=("model.company.a",),
                schema="marts",
            ),
            Node("model.company.stale", "model", "stale", schema="marts"),
        ]
    }
    end = T0 + timedelta(days=91)
    for i, relation in enumerate(["staging.a", "marts.b", "marts.dead", "marts.stale"]):
        _write(db, f"w{i}", relation, T0)
    # A dashboard reads b recently; stale was last read 60 days before the end.
    for query_id, relation, at in [
        ("r1", "marts.b", end - timedelta(days=1)),
        ("r2", "marts.stale", end - timedelta(days=60)),
    ]:
        db.execute(
            "INSERT INTO ops.query_log VALUES (?, 'r', 'u', 'dashboard', NULL, ?, 'sql', false, "
            "NULL, NULL, NULL, false, true, NULL)",
            [query_id, at],
        )
        db.execute("INSERT INTO ops.query_tables VALUES (?, ?, ['x'], [])", [query_id, relation])
    _costs(db, [("w2", "model.company.dead", "0.91")])

    unused = recommend.unused_tables(db, nodes, build_graph(nodes), {}, end, 91, [30, 90])
    by_days = {d: {u.relation for u in unused if u.lookback_days == d} for d in (30, 90)}
    # staging.a is only read by dbt, but it feeds b, which a dashboard reads.
    assert by_days[30] == {"marts.dead", "marts.stale"}
    assert by_days[90] == {"marts.dead"}
    dead = next(u for u in unused if u.relation == "marts.dead")
    assert abs(dead.monthly_saving["scan"] - Decimal("0.30")) < Decimal("1e-9")  # 0.91 * 30 / 91


def test_incremental_verdict_follows_the_sources(db: duckdb.DuckDBPyConnection) -> None:
    nodes = {
        n.unique_id: n
        for n in [
            Node("source.company.raw.events", "source", "events", schema="raw", alias="events"),
            Node(
                "source.company.raw.products", "source", "products", schema="raw", alias="products"
            ),
            Node(
                "model.company.stg_events",
                "model",
                "stg_events",
                depends_on=("source.company.raw.events",),
                schema="staging",
            ),
            Node(
                "model.company.fct",
                "model",
                "fct",
                depends_on=("model.company.stg_events", "source.company.raw.products"),
                schema="marts",
            ),
        ]
    }
    _costs(db, [("q1", "model.company.stg_events", "2"), ("q2", "model.company.fct", "1")])
    verdicts = recommend.incremental_candidates(
        db,
        nodes,
        build_graph(nodes),
        {},
        {"raw.events": True, "raw.products": False},
        top_n=5,
        exclude=set(),
    )
    assert [(v.relation, v.candidate) for v in verdicts] == [
        ("staging.stg_events", True),
        ("marts.fct", False),
    ]
    assert verdicts[1].blocked_by == ("raw.products",)


# --- collection ------------------------------------------------------------------------


def test_queries_differing_only_in_literals_share_a_parse() -> None:
    a = "select * from marts.t where d >= date '2026-01-01' and id = 7"
    b = "select * from marts.t where d >= date '2026-02-15' and id = 99"
    assert _shape(a) == _shape(b)
    assert _shape("select * from marts.sales_rpt_2025") != _shape(
        "select * from marts.sales_rpt_2026"
    )


def test_query_log_records_parsed_reads(db: duckdb.DuckDBPyConnection) -> None:
    log = QueryLog({"marts.t": {"id": "BIGINT", "d": "DATE"}})
    log.add(
        QueryRecord(
            "q", "r", "sam", "adhoc", None, T0, "select id from marts.t where d > '2026-01-01'"
        )
    )
    log.flush(db)
    assert db.execute("SELECT columns, filter_columns FROM ops.query_tables").fetchall() == [
        (["d", "id"], ["d"])
    ]


def test_dbt_run_order_is_dependency_order_whatever_dbt_reported(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        path = tmp_path / "run" / "company" / "models" / f"{name}.sql"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"select 1 as {name}", encoding="utf-8")
    nodes = {
        f"model.company.{n}": Node(
            f"model.company.{n}",
            "model",
            n,
            path=f"models/{n}.sql",
            package="company",
            depends_on=deps,
        )
        for n, deps in [("a", ()), ("b", ("model.company.a",)), ("c", ("model.company.a",))]
    }
    shuffled = ["model.company.c", "model.company.a", "model.company.b"]
    (tmp_path / "run_results.json").write_text(
        json.dumps({"results": [{"unique_id": u, "status": "success"} for u in shuffled]}),
        encoding="utf-8",
    )
    run = read_dbt_run(tmp_path, nodes)
    assert [n.name for n in run.nodes] == ["a", "b", "c"]


# --- workload ------------------------------------------------------------------------


def test_stable_hash_is_stable() -> None:
    assert workload.stable_hash(1, "x", 2) == workload.stable_hash(1, "x", 2)
    assert workload.stable_hash(1, "x", 2) != workload.stable_hash(1, "x", 3)


def test_dashboards_read_every_table_they_depend_on(settings: Settings) -> None:
    boards = workload.dashboards_from(dict(NODES), settings)
    assert [(b.exposure_id, b.tables) for b in boards] == [
        ("exposure.company.sales_board", ("marts.sales_fct",))
    ]
    for board in boards:
        hours = workload.refresh_hours_for(board, settings.seed)
        assert hours and min(hours) >= 3, "dashboards refresh after the 02:00 dbt run"


def test_dashboard_sql_filters_on_the_first_date_column() -> None:
    catalog = {"marts.t": {"id": "BIGINT", "day": "DATE"}, "marts.u": {"id": "BIGINT"}}
    assert workload.dashboard_sql("marts.t", catalog, T0) == (
        "select * from marts.t where day >= date '2025-10-07'"
    )
    assert workload.dashboard_sql("marts.u", catalog, T0) == "select * from marts.u"
