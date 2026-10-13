"""Dashboards from dbt exposures: the card rules, on a hand-built manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_ops.metadata.registry import DatasetRule, Registry, Team
from platform_ops.simulation.bi import (
    CHART_ROWS,
    RECENT_DAYS,
    TABLE_ROWS,
    Column,
    ModelShape,
    card_for,
    dashboards,
    exposed_models,
)

ROLLUP = (
    Column("period_start", "Date"),
    Column("channel", "Nullable(String)"),
    Column("orders", "UInt64"),
    Column("net_revenue", "Nullable(Decimal(38, 2))"),
)


def test_column_types_are_read_through_nullable() -> None:
    assert Column("d", "Nullable(Date32)").is_time
    assert Column("t", "DateTime64(6, 'UTC')").is_time
    assert Column("n", "Nullable(Decimal(18, 2))").is_number
    assert Column("u", "UInt64").is_number
    assert not Column("s", "LowCardinality(String)").is_number


def test_a_monthly_rollup_is_one_line_per_dimension_value() -> None:
    card = card_for(ModelShape("sales_rpt_orders_monthly_by_channel", "marts", ROLLUP))
    assert card.display == "line"
    assert card.dimensions == ("period_start", "channel") and card.metrics == ("orders",)
    assert card.sql == (
        "SELECT `period_start`, `channel`, sum(`orders`) AS `orders` "
        "FROM `marts`.`sales_rpt_orders_monthly_by_channel` "
        "GROUP BY `period_start`, `channel` ORDER BY `period_start`, `channel`"
    )


def test_a_daily_rollup_shows_its_recent_days_so_metabase_never_cuts_it_off() -> None:
    card = card_for(ModelShape("sales_rpt_orders_daily_by_channel", "marts", ROLLUP))
    assert (
        "WHERE `period_start` > (SELECT max(`period_start`) "
        f"FROM `marts`.`sales_rpt_orders_daily_by_channel`) - INTERVAL {RECENT_DAYS} DAY"
    ) in card.sql
    # Ten countries for every recent day still fit under Metabase's chart limit.
    assert RECENT_DAYS * 10 < CHART_ROWS


def test_a_hint_in_the_model_wins() -> None:
    columns = (Column("order_date", "Date"), Column("orders", "UInt64"))
    hint = {"display": "bar", "x": "order_date", "y": ["orders"]}
    card = card_for(ModelShape("sales_fct_daily_sales", "marts", columns, "order_date", hint))
    assert (card.display, card.dimensions, card.metrics) == ("bar", ("order_date",), ("orders",))
    assert card.sql.startswith("SELECT `order_date`, sum(`orders`) AS `orders`")


def test_a_hint_naming_an_unknown_column_fails_loudly() -> None:
    hint = {"x": "day", "y": ["orders"]}
    with pytest.raises(ValueError, match="unknown columns"):
        card_for(ModelShape("m", "marts", ROLLUP, "", hint))


def test_anything_else_is_a_table_sorted_by_its_key() -> None:
    columns = (Column("customer_id", "Int64"), Column("email", "String"))
    card = card_for(ModelShape("sales_dim_customers", "t_marts", columns, "customer_id"))
    assert card.display == "table" and card.metrics == ()
    assert card.sql == (
        f"SELECT * FROM `t_marts`.`sales_dim_customers` ORDER BY customer_id LIMIT {TABLE_ROWS}"
    )


def _manifest(path: Path) -> Path:
    def model(name: str, meta: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "unique_id": f"model.company.{name}",
            "resource_type": "model",
            "name": name,
            "config": {"meta": meta or {}},
        }

    hint = {"display": "line", "x": "period_start", "y": ["net_revenue"]}
    nodes = {
        "model.company.finance_rpt_revenue_monthly_by_channel": model(
            "finance_rpt_revenue_monthly_by_channel"
        ),
        "model.company.finance_fct_revenue": model("finance_fct_revenue", {"dashboard": hint}),
        "model.company.stg_orders": model("stg_orders"),
    }
    exposure = {
        "unique_id": "exposure.company.finance_revenue_dashboard",
        "resource_type": "exposure",
        "name": "finance_revenue_dashboard",
        "label": "Revenue Dashboard",
        "description": "Recognized revenue by month.",
        "owner": {"name": "alice"},
        "depends_on": {
            "nodes": [
                "model.company.finance_rpt_revenue_monthly_by_channel",
                "model.company.finance_fct_revenue",
            ]
        },
    }
    path.write_text(
        json.dumps({"nodes": nodes, "sources": {}, "exposures": {exposure["unique_id"]: exposure}}),
        encoding="utf-8",
    )
    return path


def test_one_dashboard_per_exposure_in_its_owners_team(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "manifest.json")
    registry = Registry(
        teams={"finance": Team(id="finance", name="Finance", channel="#f", members=["alice"])},
        rules=(
            DatasetRule.model_validate(
                {
                    "match": "exposure.company.finance_*",
                    "owner": "alice",
                    "team": "finance",
                    "tier": "critical",
                }
            ),
        ),
    )
    assert exposed_models(manifest) == [
        "finance_fct_revenue",
        "finance_rpt_revenue_monthly_by_channel",
    ]
    shapes = {name: ModelShape(name, "marts", ROLLUP) for name in exposed_models(manifest)}
    (board,) = dashboards(manifest, registry, shapes)
    assert (board.name, board.team, board.owner) == ("Revenue Dashboard", "finance", "alice")
    assert board.description == "Recognized revenue by month."
    by_model = {card.model: card for card in board.cards}
    assert set(by_model) == {"finance_fct_revenue", "finance_rpt_revenue_monthly_by_channel"}
    # The hint in the model's meta is applied; the rollup follows the default rule.
    assert by_model["finance_fct_revenue"].metrics == ("net_revenue",)
    assert by_model["finance_rpt_revenue_monthly_by_channel"].metrics == ("orders",)
