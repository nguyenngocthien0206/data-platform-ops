"""Unit tests for the cost pipeline pieces, on small hand-built data (version 2.0)."""

from __future__ import annotations

import dataclasses
import itertools
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from platform_ops.common import tagging
from platform_ops.common.config import StatedCost
from platform_ops.common.hashing import stable_hash
from platform_ops.common.tagging import Tag, parse
from platform_ops.cost import attribution, cluster, recommend
from platform_ops.cost.attribution import Owners
from platform_ops.cost.business import business_case
from platform_ops.cost.collect import _INVOCATION_ID, QueryLog, QueryRecord, redact
from platform_ops.metadata.lineage import build_graph
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.registry import DatasetRule, Registry, Team
from platform_ops.simulation import workload

T0 = datetime(2026, 1, 5, 2, 0, 0)


def record(
    query_id: str,
    actor_type: str,
    *,
    actor: str = "",
    node_id: str | None = None,
    at: datetime = T0,
    kind: str = "Select",
    rows: int = 0,
    read: int = 0,
    tables: tuple[str, ...] = (),
) -> QueryRecord:
    return QueryRecord(
        query_id,
        "run",
        actor or actor_type,
        actor_type,  # type: ignore[arg-type]
        node_id,
        at,
        "select 1",
        rows_scanned=rows,
        bytes_scanned=read,
        query_kind=kind,
        tables=tables,
    )


# --- the cluster's bill, shared out ----------------------------------------------------


def test_window_months_count_the_days_in_each_month() -> None:
    assert cluster.window_months(T0, 91) == {
        "2026-01": 27,
        "2026-02": 28,
        "2026-03": 31,
        "2026-04": 5,
    }


def test_a_month_pool_is_the_stated_cost_prorated_by_days() -> None:
    stated = StatedCost(instance_usd_per_month=300, storage_gb=10, storage_usd_per_gb_month=1)
    pools = {p.month: p for p in cluster.pools(stated, cluster.window_months(T0, 91))}
    assert pools["2026-01"].compute_usd == (Decimal("300") * 27 / 31).quantize(cluster.MICRO)
    assert pools["2026-02"].total_usd == Decimal("310")
    assert pools["2026-04"].storage_usd == (Decimal("10") * 5 / 30).quantize(cluster.MICRO)


def test_shares_add_up_to_the_total_exactly() -> None:
    total = Decimal("100")
    shares = cluster.share(total, {"a": 1, "b": 1, "c": 1, "zero": 0})
    assert sum(shares.values()) == total
    assert "zero" not in shares
    # The remainder of 100/3 goes to the largest share, ties to the first key.
    assert shares["a"] == Decimal("33.333333333334")
    assert shares["b"] == shares["c"] == Decimal("33.333333333333")
    assert cluster.share(total, {"a": 0}) == {}


def test_every_query_and_table_share_its_own_month() -> None:
    stated = StatedCost(instance_usd_per_month=310, storage_gb=31, storage_usd_per_gb_month=1)
    pools = {p.month: p for p in cluster.pools(stated, {"2026-01": 31, "2026-02": 28})}
    compute = cluster.compute_shares(
        pools, [("q1", "2026-01", 3), ("q2", "2026-01", 1), ("q3", "2026-02", 7)]
    )
    assert compute == {"q1": Decimal("232.5"), "q2": Decimal("77.5"), "q3": Decimal("310")}
    storage = cluster.storage_shares(pools, [("2026-01", "marts.a", 1), ("2026-02", "marts.a", 0)])
    assert storage == {("2026-01", "marts.a"): Decimal("31")}


# --- attribution -----------------------------------------------------------------------

TEAMS = {
    "platform": Team(id="platform", name="P", channel="#p", members=["priya"]),
    "sales": Team(id="sales", name="S", channel="#s", members=["sam"]),
}
RULES = (
    DatasetRule(match="source.company.*", owner="priya", team="platform", tier="important"),
    DatasetRule(match="model.company.stg_*", owner="priya", team="platform", tier="important"),
    DatasetRule(match="model.company.sales_*", owner="sam", team="sales", tier="important"),
    DatasetRule(match="exposure.company.sales_*", owner="sam", team="sales", tier="important"),
)
REGISTRY = Registry(teams=TEAMS, rules=RULES)
NODES = {
    n.unique_id: n
    for n in [
        Node("source.company.raw.orders", "source", "orders", schema="raw"),
        Node(
            "model.company.stg_orders",
            "model",
            "stg_orders",
            depends_on=("source.company.raw.orders",),
            schema="staging",
        ),
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
OWNERS = Owners(NODES, REGISTRY)


def test_each_query_is_charged_once_to_the_right_team() -> None:
    records = [
        record("q1", "dbt", node_id="model.company.stg_orders", kind="Insert"),
        record("q2", "dbt", node_id="test.company.unique_sales_fct"),
        record("q3", "dbt"),
        record("q4", "ingestion", node_id="source.company.raw.orders", kind="Insert"),
        record("q5", "maintenance", node_id="source.company.raw.orders"),
        record("q6", "reconcile", node_id="table.orders"),
        record("q7", "dashboard", node_id="exposure.company.sales_board"),
        record("q8", "adhoc", actor="sam"),
        record("q9", "adhoc", actor="stranger"),
    ]
    got = attribution.attribute(records, OWNERS)
    assert len(got) == len(records)
    view = {q: (a.cost_kind, a.team, a.owner) for q, a in got.items()}
    assert view["q1"] == ("production", "platform", "priya")
    # A test belongs to the model it tests, even though it also reads stg_orders.
    assert view["q2"] == ("production", "sales", "sam")
    assert got["q2"].subject == "model.company.sales_fct"
    # dbt's own bookkeeping, outside any model, is the platform team's: it runs dbt.
    assert view["q3"] == ("production", "platform", "priya")
    assert view["q4"] == view["q5"] == ("production", "platform", "priya")
    assert view["q6"] == ("migration", "platform", "priya")
    # The metabase user runs the dashboard; its owner pays.
    assert view["q7"] == ("consumption", "sales", "sam")
    assert view["q8"] == ("consumption", "sales", "sam")
    assert view["q9"][1] == attribution.UNATTRIBUTED


# --- collection --------------------------------------------------------------------------


def test_redaction_drops_literals_and_keeps_comments() -> None:
    text = '/* {"run": "a1", "n": 12} */ select 1, \'x\' from t where a > 3.5 /* c2 */'
    redacted = redact(text)
    assert redacted == '/* {"run": "a1", "n": 12} */ select ?, ? from t where a > ? /* c2 */'
    assert "\0" not in redacted


def test_dbt_invocation_ids_are_taken_out_of_table_names() -> None:
    """An incremental model's staging table is named after the run, so two runs differ."""
    name = "ns_marts.fct__dbt_new_data_42d7668c_d1c2_4a5e_92e3_4713d765413e"
    assert _INVOCATION_ID.sub("", name) == "ns_marts.fct__dbt_new_data"
    assert _INVOCATION_ID.sub("", "ns_marts.fct_2025_q1") == "ns_marts.fct_2025_q1"


def test_queries_differing_only_in_literals_share_a_parse() -> None:
    a = "select * from marts.t where d >= toDate('2026-01-01') and id = 7"
    b = "select * from marts.t where d >= toDate('2026-02-15') and id = 99"
    assert redact(a) == redact(b)
    assert redact("select * from marts.rpt_2025") != redact("select * from marts.rpt_2026")


def test_the_query_log_keeps_filter_columns_and_redacted_text() -> None:
    log = QueryLog({"marts.t": {"id": "Int64", "d": "Date"}})
    sql = "select id from marts.t where d > toDate('2026-01-01')"
    log.add(QueryRecord("q", "r", "sam", "adhoc", None, T0, sql), raw_sql=sql)
    assert log.filters == [("q", "marts.t", ("d",))]
    assert log.records[0].sql_text == "select id from marts.t where d > toDate(?)"


def test_a_tag_round_trips_and_only_rides_on_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(tagging._RUN, "id", "")
    assert Tag(T0, "adhoc", person="sam").settings() == {}
    monkeypatch.setitem(tagging._RUN, "id", "abc")
    comment = Tag(T0, "adhoc", person="sam").comment()
    assert Tag(T0, "adhoc", person="sam").settings() == {"log_comment": comment}
    assert parse(comment) == {
        "run": "abc",
        "sim": T0.isoformat(),
        "actor": "adhoc",
        "subject": "",
        "person": "sam",
    }
    assert parse("not json") is None
    assert parse("") is None


# --- the business case -------------------------------------------------------------------


def test_the_overlap_until_cutover_and_its_payback() -> None:
    clickhouse = StatedCost(instance_usd_per_month=300, storage_gb=10, storage_usd_per_gb_month=1)
    legacy = StatedCost(instance_usd_per_month=600, storage_gb=20, storage_usd_per_gb_month=1)
    cutover = datetime(2026, 3, 20, 2, 0, 0)
    case = business_case(clickhouse, legacy, T0, 91, cutover, {"2026-03": Decimal("4")})
    by_month = {m.month: m for m in case.months}
    assert [m.legacy_days for m in case.months] == [27, 28, 20, 0]
    assert by_month["2026-03"].reconcile_usd == Decimal("4")
    assert case.monthly_saving == Decimal("310")
    # January (27/31 of 310), all of February, and 20 of March's 31 days.
    assert case.overlap_usd == Decimal("780")
    assert case.payback_months == Decimal("2.5")


def test_no_payback_when_clickhouse_costs_more() -> None:
    cheap = StatedCost(instance_usd_per_month=10, storage_gb=0, storage_usd_per_gb_month=0)
    dear = StatedCost(instance_usd_per_month=20, storage_gb=0, storage_usd_per_gb_month=0)
    assert business_case(dear, cheap, T0, 30, None, {}).payback_months is None


# --- recommendations ---------------------------------------------------------------------

DEAD_NODES = {
    n.unique_id: n
    for n in [
        Node("model.company.a", "model", "a", schema="staging"),
        Node("model.company.b", "model", "b", depends_on=("model.company.a",), schema="marts"),
        Node(
            "model.company.dead", "model", "dead", depends_on=("model.company.a",), schema="marts"
        ),
        Node("model.company.stale", "model", "stale", schema="marts"),
    ]
}


def test_unused_tables_respect_lineage_and_lookback() -> None:
    owners = Owners(DEAD_NODES, Registry(teams={}, rules=()))
    end = T0 + timedelta(days=91)
    builds = [
        record(f"w{i}", "dbt", node_id=uid, kind="Insert")
        for i, uid in enumerate(sorted(DEAD_NODES))
    ]
    reads = [
        # A dashboard reads b recently; stale was last read 60 days before the end.
        record("r1", "dashboard", at=end - timedelta(days=1), tables=("marts.b",)),
        record("r2", "adhoc", at=end - timedelta(days=60), tables=("marts.stale",)),
        # dbt reading a table to build another is not a use.
        record("r3", "dbt", at=end - timedelta(days=1), tables=("marts.dead",)),
    ]
    production = {"model.company.dead": {"cluster": Decimal("0.91")}}
    unused = recommend.unused_tables(
        builds + reads, owners, build_graph(DEAD_NODES), production, end, 91, [30, 90]
    )
    by_days = {d: {u.relation for u in unused if u.lookback_days == d} for d in (30, 90)}
    # staging.a is only read by dbt, but it feeds b, which a dashboard reads.
    assert by_days[30] == {"marts.dead", "marts.stale"}
    assert by_days[90] == {"marts.dead"}
    dead = next(u for u in unused if u.relation == "marts.dead")
    assert dead.monthly_saving["cluster"] == Decimal("0.3000")  # 0.91 * 30 / 91


def test_a_hotspot_is_a_filtered_read_of_nearly_the_whole_table() -> None:
    records = [
        record(f"h{i}", "adhoc", actor="sam", rows=950, read=9_500, tables=("marts.t",))
        for i in range(3)
    ] + [
        # Uses the sort key: reads a tenth of the table.
        record("k", "adhoc", actor="sam", rows=100, read=1_000, tables=("marts.t",)),
        # A join is not a single-table hotspot.
        record("j", "adhoc", actor="sam", rows=1000, read=10_000, tables=("marts.t", "marts.u")),
        record("d", "dbt", rows=1000, read=10_000, tables=("marts.t",)),
    ]
    filters = [(q, "marts.t", ("customer_id",)) for q in ("h0", "h1", "h2", "k", "j", "d")]
    found = recommend.hotspots(
        records,
        filters,
        {"marts.t": 1000},
        {"marts.t": 10_000},
        {"marts.t": "(order_date, id)"},
        min_table_bytes=5_000,
        min_reads=3,
        engines={"marts.t": "MergeTree"},
    )
    assert [(h.table, h.column, h.reads, h.rows_scanned) for h in found] == [
        ("marts.t", "customer_id", 3, 2850)
    ]
    assert found[0].suggestion == "sort key `(customer_id, order_date, id)`"
    replacing = dataclasses.replace(found[0], engine="ReplacingMergeTree", sorting_key="id")
    assert replacing.suggestion.startswith("a minmax skip index on `customer_id`")
    assert not recommend.hotspots(
        records, filters, {"marts.t": 1000}, {"marts.t": 10_000}, {}, 20_000, 3
    ), "small tables are left alone"


SOURCE_NODES = {
    n.unique_id: n
    for n in [
        Node("source.company.raw.events", "source", "events", schema="raw"),
        Node("source.company.raw.products", "source", "products", schema="raw"),
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
        Node(
            "model.company.already",
            "model",
            "already",
            depends_on=("model.company.stg_events",),
            schema="marts",
        ),
    ]
}


def test_incremental_verdict_follows_the_sources() -> None:
    owners = Owners(SOURCE_NODES, Registry(teams={}, rules=()))
    production = {
        "model.company.stg_events": {"cluster": Decimal("2")},
        "model.company.fct": {"cluster": Decimal("1")},
        "model.company.already": {"cluster": Decimal("9")},
    }
    verdicts = recommend.incremental_candidates(
        owners,
        build_graph(SOURCE_NODES),
        production,
        {"model.company.already": "incremental"},
        {"products"},
        "cluster",
        top_n=5,
        exclude=set(),
    )
    assert [(v.relation, v.candidate) for v in verdicts] == [
        ("staging.stg_events", True),
        ("marts.fct", False),
    ]
    assert verdicts[1].blocked_by == ("products",)
    assert verdicts[1].sources == ("events", "products")


def test_quotas_sit_above_the_busiest_days_and_skip_the_pipeline_users() -> None:
    day = timedelta(days=1)
    records = [
        record("a1", "adhoc", actor="sam", at=T0, read=1000),
        record("a2", "adhoc", actor="sam", at=T0 + day, read=3000),
        record("a3", "adhoc", actor="sam", at=T0 + 2 * day, read=1500),
        record("a4", "adhoc", actor="sam", at=T0 + 2 * day, read=500),
        record("p1", "adhoc", actor="priya", at=T0, read=10**9),
        record("m1", "dashboard", at=T0, read=123_456),
        record("x1", "dbt", at=T0, read=10**12),
    ]
    team_of_user = {"sam": "sales", "priya": "platform"}
    got = {q.user: q for q in recommend.quotas(records, team_of_user, "ns_", exclude={"platform"})}
    assert set(got) == {"sales", "metabase"}
    sales = got["sales"]
    assert (sales.active_days, sales.p95_daily_bytes, sales.max_query_bytes) == (3, 3000, 3000)
    assert (sales.daily_quota_bytes, sales.query_limit_bytes) == (6000, 6000)
    assert sales.statements == (
        "CREATE QUOTA ns_sales_daily FOR INTERVAL 1 day MAX read_bytes = 6000 TO ns_sales",
        "ALTER USER ns_sales SETTINGS PROFILE 'ns_reader', max_bytes_to_read = 6000",
    )
    # Rounded up to two significant figures: 2 x 123,456 is 246,912.
    assert got["metabase"].daily_quota_bytes == 250_000


# --- workload ----------------------------------------------------------------------------


def test_stable_hash_is_stable() -> None:
    assert stable_hash(1, "x", 2) == stable_hash(1, "x", 2)
    assert stable_hash(1, "x", 2) != stable_hash(1, "x", 3)
    assert stable_hash(1, "x", 2) == 897146772116593929, "the same value on every machine"


@pytest.mark.parametrize("every", [12, 24])
def test_dashboards_refresh_after_the_nightly_build_on_their_cadence(every: int) -> None:
    hours = workload.refresh_hours("exposure.company.sales_board", every, seed=42)
    assert hours == workload.refresh_hours("exposure.company.sales_board", every, seed=42)
    assert hours and min(hours) >= 3 and max(hours) < 24, "after the 02:00 dbt run"
    assert all(b - a == every for a, b in itertools.pairwise(hours))


def test_ad_hoc_sql_fills_in_the_namespaced_databases_and_dates() -> None:
    sql = workload.adhoc_sql(
        "SELECT * FROM {marts}.t WHERE d >= toDate('{d7}') AND id = {n}",
        T0,
        5,
        {"marts": "ns_marts", "staging": "ns_staging", "raw": "ns_raw"},
    )
    assert sql == "SELECT * FROM ns_marts.t WHERE d >= toDate('2025-12-29') AND id = 5"


def test_business_teams_query_only_the_marts() -> None:
    for team, templates in workload.ADHOC_TEMPLATES.items():
        if team != "platform":
            assert all("{raw}" not in t and "{staging}" not in t for t in templates), team
