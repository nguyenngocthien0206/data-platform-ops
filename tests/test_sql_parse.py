"""SQL parsing edge cases (a SPEC Phase 2 acceptance item)."""

from __future__ import annotations

import pytest

from platform_ops.cost.sql_parse import (
    ParsedQuery,
    node_id_from_comment,
    parse_query,
)

CATALOG = {
    "marts.orders": {
        "order_id": "BIGINT",
        "customer_id": "BIGINT",
        "order_date": "DATE",
        "channel": "VARCHAR",
        "net_amount": "DECIMAL(12,2)",
    },
    "marts.customers": {"customer_id": "BIGINT", "country": "VARCHAR", "segment": "VARCHAR"},
    "staging.events": {"event_id": "BIGINT", "Payload": "VARCHAR", "event_date": "DATE"},
}


def cols(parsed: ParsedQuery, table: str) -> set[str]:
    return set(parsed.reads[table].columns)


def filters(parsed: ParsedQuery, table: str) -> set[str]:
    return set(parsed.reads[table].filter_columns)


def test_simple_select_reads_only_the_named_columns() -> None:
    parsed = parse_query("select order_id, net_amount from marts.orders", CATALOG)
    assert parsed.ok and parsed.writes is None
    assert cols(parsed, "marts.orders") == {"order_id", "net_amount"}


def test_select_star_expands_to_every_column() -> None:
    parsed = parse_query("select * from marts.orders limit 10", CATALOG)
    assert parsed.select_star
    assert cols(parsed, "marts.orders") == set(CATALOG["marts.orders"])


def test_cte_columns_trace_back_to_the_base_table() -> None:
    sql = """
        with recent as (
            select order_id, net_amount from marts.orders where order_date >= '2025-12-01'
        ),
        orders as (select * from recent)          -- a CTE that shadows a table-like name
        select order_id, sum(net_amount) from orders group by 1
    """
    parsed = parse_query(sql, CATALOG)
    assert set(parsed.reads) == {"marts.orders"}, "CTE names are not tables"
    assert cols(parsed, "marts.orders") == {"order_id", "net_amount", "order_date"}
    assert filters(parsed, "marts.orders") == {"order_date"}


def test_nested_subqueries_and_joins() -> None:
    sql = """
        select c.country, x.total
        from (
            select customer_id, sum(net_amount) as total
            from (select * from marts.orders where channel = 'web') o
            group by customer_id
        ) x
        join marts.customers c on c.customer_id = x.customer_id
    """
    parsed = parse_query(sql, CATALOG)
    assert set(parsed.reads) == {"marts.orders", "marts.customers"}
    # SELECT * inside the subquery reads every column of the base table.
    assert cols(parsed, "marts.orders") == set(CATALOG["marts.orders"])
    assert filters(parsed, "marts.orders") == {"channel"}
    assert cols(parsed, "marts.customers") == {"country", "customer_id"}


def test_create_table_as_writes_the_target_and_reads_the_source() -> None:
    sql = (
        'create table "warehouse"."marts"."daily__dbt_tmp" as '
        "(select order_date, count(*) from marts.orders group by 1)"
    )
    parsed = parse_query(sql, CATALOG)
    assert parsed.writes == "marts.daily", "dbt's temporary suffix is stripped"
    assert set(parsed.reads) == {"marts.orders"}
    assert cols(parsed, "marts.orders") == {"order_date"}


def test_a_query_comment_after_the_final_semicolon_is_not_a_second_statement() -> None:
    # What a warehouse's query history records for a dbt model: the compiled SQL,
    # which ends with `;`, then dbt's appended query comment.
    sql = (
        'create table "warehouse"."marts"."daily__dbt_tmp" as '
        "(select order_date from marts.orders);\n"
        '/* {"app": "platform-ops", "unique_id": "model.company.daily"} */'
    )
    parsed = parse_query(sql, CATALOG)
    assert parsed.ok, parsed.error
    assert parsed.writes == "marts.daily"
    assert parsed.node_id == "model.company.daily"
    assert not parse_query("select 1; select 2", CATALOG).ok, "two real statements still fail"


def test_insert_select_writes_the_target() -> None:
    parsed = parse_query("insert into marts.customers select * from marts.customers", CATALOG)
    assert parsed.writes == "marts.customers"


def test_quoted_identifiers_and_three_part_names() -> None:
    sql = (
        'select "event_id", "Payload" from "warehouse"."staging"."events" '
        'where "event_date" > current_date - 7'
    )
    parsed = parse_query(sql, CATALOG)
    assert set(parsed.reads) == {"staging.events"}
    assert cols(parsed, "staging.events") == {"event_id", "payload", "event_date"}
    assert filters(parsed, "staging.events") == {"event_date"}


def test_count_star_still_registers_the_table() -> None:
    parsed = parse_query("select count(*) from marts.orders", CATALOG)
    assert set(parsed.reads) == {"marts.orders"}
    assert cols(parsed, "marts.orders") == set()


def test_union_reads_both_sides() -> None:
    sql = "select customer_id from marts.orders union all select customer_id from marts.customers"
    parsed = parse_query(sql, CATALOG)
    assert set(parsed.reads) == {"marts.orders", "marts.customers"}


def test_duckdb_specific_syntax_parses() -> None:
    sql = """
        select channel,
               count(*) filter (where net_amount > 100) as big,
               net_amount::double as amount
        from marts.orders
        where order_date >= date '2025-01-01'
        group by all
    """
    parsed = parse_query(sql, CATALOG)
    assert parsed.ok
    assert filters(parsed, "marts.orders") == {"order_date"}


def test_unknown_table_is_still_listed() -> None:
    parsed = parse_query("select a, b from marts.not_in_catalog", CATALOG)
    assert parsed.ok
    assert "marts.not_in_catalog" in parsed.reads


def test_broken_sql_reports_an_error_instead_of_raising() -> None:
    parsed = parse_query("select from where", CATALOG)
    assert not parsed.ok
    assert parsed.error


def test_multiple_statements_are_rejected() -> None:
    parsed = parse_query("select 1; select 2", CATALOG)
    assert not parsed.ok


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            'select 1 /* {"app": "platform-ops", "unique_id": "model.company.sales_fct_orders"} */',
            "model.company.sales_fct_orders",
        ),
        ('select 1 /* {"app": "platform-ops", "unique_id": null} */', None),
        ("select 1 /* not json */", None),
        ("select 1", None),
    ],
)
def test_node_id_from_the_dbt_query_comment(sql: str, expected: str | None) -> None:
    assert node_id_from_comment(sql) == expected
    assert parse_query(sql, CATALOG).node_id == expected
