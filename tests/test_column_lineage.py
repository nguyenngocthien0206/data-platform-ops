"""Column lineage from compiled SQL, and report differences explained through it.

A small project in the shape of the company's: customers folded by email into
one canonical account, orders joined to it with a left join, and a revenue
mart that filters on status and groups by month. No server is needed: the
catalogue is given, and the SQL is only parsed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from platform_ops.metadata.column_lineage import ColumnLineage, LineageUnavailable
from platform_ops.reconcile.diff import CellDiff, DuplicateKey, TableDiff
from platform_ops.reconcile.explain import explain
from platform_ops.reconcile.schema import Column, TableSpec

MODELS = {
    "stg_customers": """
        select customer_id, lower(trim(email)) as email, country, signed_up_at
        from `raw`.`customers`""",
    "stg_orders": """
        select * from `raw`.`orders`""",
    "int_customer_id_map": """
        select
            customer_id,
            first_value(customer_id) over (
                partition by email order by signed_up_at, customer_id
            ) as canonical_customer_id
        from `staging`.`stg_customers`""",
    "int_orders_enriched": """
        with priced as (
            select o.order_id as order_id, o.status as status, o.ordered_at as ordered_at,
                   o.amount as amount, m.canonical_customer_id as canonical_customer_id
            from `staging`.`stg_orders` o
            left join `intermediate`.`int_customer_id_map` m on m.customer_id = o.customer_id
        )
        select p.*, c.country as country
        from priced p
        left join `staging`.`stg_customers` c on c.customer_id = p.canonical_customer_id""",
    "revenue_monthly": """
        select toStartOfMonth(ordered_at) as revenue_month,
               count(*) as orders,
               sum(amount) as revenue
        from `intermediate`.`int_orders_enriched`
        where status in ('completed', 'shipped')
        group by 1""",
    "customers_by_country": """
        select country, count(distinct canonical_customer_id) as customers
        from `intermediate`.`int_orders_enriched`
        group by country""",
    "keys_bare_union": """
        select order_id from `staging`.`stg_orders`
        union
        select customer_id from `staging`.`stg_customers`""",
}
LAYER = {
    "stg_customers": "staging",
    "stg_orders": "staging",
    "int_customer_id_map": "intermediate",
    "int_orders_enriched": "intermediate",
}
CATALOGUE = {
    "raw": {
        "customers": {
            "customer_id": "Int64",
            "email": "String",
            "country": "String",
            "signed_up_at": "DateTime64(6)",
        },
        "orders": {
            "order_id": "Int64",
            "customer_id": "Int64",
            "status": "String",
            "ordered_at": "DateTime64(6)",
            "amount": "Decimal(12, 2)",
        },
    },
    "staging": {
        "stg_customers": {
            "customer_id": "Int64",
            "email": "String",
            "country": "String",
            "signed_up_at": "DateTime64(6)",
        },
        "stg_orders": {
            "order_id": "Int64",
            "customer_id": "Int64",
            "status": "String",
            "ordered_at": "DateTime64(6)",
            "amount": "Decimal(12, 2)",
        },
    },
    "intermediate": {
        "int_customer_id_map": {"customer_id": "Int64", "canonical_customer_id": "Int64"},
        "int_orders_enriched": {
            "order_id": "Int64",
            "status": "String",
            "ordered_at": "DateTime64(6)",
            "amount": "Decimal(12, 2)",
            "canonical_customer_id": "Int64",
            "country": "String",
        },
    },
}


@pytest.fixture
def traced(tmp_path: Path) -> ColumnLineage:
    nodes: dict[str, Any] = {}
    for name, sql in MODELS.items():
        path = f"models/{name}.sql"
        (tmp_path / "models").mkdir(exist_ok=True)
        (tmp_path / path).write_text(sql, encoding="utf-8")
        nodes[f"model.shop.{name}"] = {
            "resource_type": "model",
            "name": name,
            "alias": name,
            "schema": LAYER.get(name, "marts"),
            "original_file_path": path,
        }
    sources = {
        f"source.shop.raw.{t}": {"schema": "raw", "identifier": t} for t in ("customers", "orders")
    }
    manifest = {"nodes": nodes, "sources": sources}
    return ColumnLineage(manifest, tmp_path, CATALOGUE, raw_source="source.shop.raw.")


def test_an_amount_depends_on_its_filter_and_grouping_not_on_email(traced: ColumnLineage) -> None:
    revenue = traced.model("model.shop.revenue_monthly")
    assert revenue["revenue"] == {
        ("orders", "amount"),
        ("orders", "status"),
        ("orders", "ordered_at"),
    }
    # A count reads no column, only the rows the filter and the grouping leave.
    assert revenue["orders"] == {("orders", "status"), ("orders", "ordered_at")}
    assert all(("customers", "email") not in deps for deps in revenue.values())


def test_a_left_join_reaches_only_the_columns_from_its_joined_side(traced: ColumnLineage) -> None:
    enriched = traced.model("model.shop.int_orders_enriched")
    assert ("customers", "email") in enriched["country"]
    assert ("customers", "email") in enriched["canonical_customer_id"]
    assert enriched["amount"] == {("orders", "amount")}
    by_country = traced.model("model.shop.customers_by_country")
    assert ("customers", "email") in by_country["customers"]


def test_sql_sqlglot_cannot_read_is_reported_not_guessed(traced: ColumnLineage) -> None:
    with pytest.raises(LineageUnavailable, match="keys_bare_union"):
        traced.model("model.shop.keys_bare_union")


# -- explaining a report ------------------------------------------------------------------

REVENUE = TableSpec(
    "revenue_monthly",
    "report",
    ("revenue_month",),
    (Column("revenue_month", "date"), Column("orders", "int"), Column("revenue", "decimal", 2)),
    source='"reporting"."revenue_monthly"',
    target="`marts`.`revenue_monthly`",
    upstream=("customers", "orders"),
)


def _revenue_diff(column: str = "revenue") -> TableDiff:
    return TableDiff("revenue_monthly", cells=[CellDiff("2026-01-01", column, "10.00", "9.00")])


def test_trimmed_emails_do_not_explain_a_revenue_difference(traced: ColumnLineage) -> None:
    lineage = traced.model("model.shop.revenue_monthly")
    why = explain(REVENUE, _revenue_diff(), lineage, {("customers", "email")}, set())
    assert not why.explained and why.causes == () and why.traced


def test_a_wrong_amount_upstream_explains_it(traced: ColumnLineage) -> None:
    lineage = traced.model("model.shop.revenue_monthly")
    why = explain(REVENUE, _revenue_diff(), lineage, {("orders", "amount")}, set())
    assert why.explained and why.causes == ("orders.amount",)


def test_missing_rows_explain_every_column_built_on_the_table(traced: ColumnLineage) -> None:
    lineage = traced.model("model.shop.revenue_monthly")
    why = explain(REVENUE, _revenue_diff("orders"), lineage, set(), {"orders"})
    assert why.explained and why.causes == ("orders (rows)",)
    # Missing customers reach nothing in the revenue report.
    assert not explain(REVENUE, _revenue_diff(), lineage, set(), {"customers"}).explained


def test_a_report_row_one_side_has_needs_its_key_explained(traced: ColumnLineage) -> None:
    lineage = traced.model("model.shop.revenue_monthly")
    extra_month = TableDiff("revenue_monthly", extra=["2026-02-01"])
    assert explain(REVENUE, extra_month, lineage, {("orders", "ordered_at")}, set()).explained
    assert not explain(REVENUE, extra_month, lineage, {("orders", "amount")}, set()).explained
    doubled = TableDiff("revenue_monthly", duplicates=[DuplicateKey("2026-01-01", 1, 2)])
    assert explain(REVENUE, doubled, lineage, set(), {"orders"}).explained


def test_every_differing_column_needs_its_own_cause(traced: ColumnLineage) -> None:
    lineage = traced.model("model.shop.revenue_monthly")
    both = TableDiff(
        "revenue_monthly",
        cells=[
            CellDiff("2026-01-01", "revenue", "10.00", "9.00"),
            CellDiff("2026-01-01", "orders", "3", "4"),
        ],
    )
    # A wrong amount explains the revenue, not one more order in the month.
    assert not explain(REVENUE, both, lineage, {("orders", "amount")}, set()).explained


def test_an_untraced_report_falls_back_to_its_tables() -> None:
    why = explain(REVENUE, _revenue_diff(), None, {("customers", "email")}, set())
    assert why.explained and why.causes == ("customers",) and not why.traced
