from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from platform_ops.common.config import Settings, load_settings
from platform_ops.common.db import connect
from platform_ops.simulation import raw_data
from platform_ops.simulation.raw_data import TABLES, RawDataPlan

PRIMARY_KEYS = {
    "customers": "customer_id",
    "products": "product_id",
    "orders": "order_id",
    "order_items": "order_item_id",
    "payments": "payment_id",
    "web_sessions": "session_id",
    "marketing_campaigns": "campaign_id",
    "marketing_spend": "spend_id",
    "support_tickets": "ticket_id",
}


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def small() -> Settings:
    base = load_settings(REPO_ROOT / "config" / "settings.yaml")
    return base.model_copy(update={"scale_factor": 0.01})


@pytest.fixture(scope="module")
def seeded(small: Settings) -> duckdb.DuckDBPyConnection:
    connection = connect(path=":memory:")
    raw_data.seed(connection, small)
    return connection


def _rows(connection: duckdb.DuckDBPyConnection, table: str) -> list[tuple[Any, ...]]:
    return connection.execute(
        f"SELECT * FROM raw.{table} ORDER BY {PRIMARY_KEYS[table]}"
    ).fetchall()


def _scalar(connection: duckdb.DuckDBPyConnection, sql: str) -> Any:
    row = connection.execute(sql).fetchone()
    assert row is not None
    return row[0]


def test_same_seed_produces_identical_content(
    small: Settings, seeded: duckdb.DuckDBPyConnection
) -> None:
    again = connect(path=":memory:")
    raw_data.seed(again, small)
    for table in TABLES:
        assert _rows(seeded, table) == _rows(again, table), table


def test_different_seed_produces_different_data(
    small: Settings, seeded: duckdb.DuckDBPyConnection
) -> None:
    other = connect(path=":memory:")
    raw_data.seed(other, small.model_copy(update={"seed": small.seed + 1}))
    assert _rows(seeded, "orders") != _rows(other, "orders")


def test_scale_factor_scales_row_counts(small: Settings) -> None:
    single = connect(path=":memory:")
    double = connect(path=":memory:")
    raw_data.seed(single, small)
    raw_data.seed(double, small.model_copy(update={"scale_factor": 0.02}))
    ratio = _scalar(double, "SELECT count(*) FROM raw.orders") / _scalar(
        single, "SELECT count(*) FROM raw.orders"
    )
    assert 1.9 < ratio < 2.1


def test_every_table_is_populated_and_has_loaded_at(
    small: Settings, seeded: duckdb.DuckDBPyConnection
) -> None:
    cutoff = small.simulation.start
    for table in TABLES:
        assert _scalar(seeded, f"SELECT count(*) FROM raw.{table}") > 0, table
        latest = _scalar(seeded, f"SELECT max(_loaded_at) FROM raw.{table}")
        assert latest <= cutoff, f"{table} has rows loaded after the window start"


def test_primary_keys_are_unique(seeded: duckdb.DuckDBPyConnection) -> None:
    for table, key in PRIMARY_KEYS.items():
        dupes = _scalar(seeded, f"SELECT count(*) - count(DISTINCT {key}) FROM raw.{table}")
        assert dupes == 0, table


def test_duplicate_customers_exist_after_email_folding(
    seeded: duckdb.DuckDBPyConnection,
) -> None:
    folded = _scalar(
        seeded,
        """SELECT count(*) FROM (
               SELECT lower(trim(email)) FROM raw.customers GROUP BY 1 HAVING count(*) > 1)""",
    )
    raw = _scalar(
        seeded,
        "SELECT count(*) FROM (SELECT email FROM raw.customers GROUP BY 1 HAVING count(*) > 1)",
    )
    assert folded > 0
    assert raw == 0, "duplicates must only collide after case and whitespace folding"


def test_some_payments_arrive_late(seeded: duckdb.DuckDBPyConnection) -> None:
    share = _scalar(
        seeded,
        """SELECT count(*) FILTER (WHERE _loaded_at > created_at + INTERVAL 1 DAY)
                  / count(*)::DOUBLE FROM raw.payments""",
    )
    assert 0.02 < share < 0.08


def test_nullable_fields_contain_nulls(seeded: duckdb.DuckDBPyConnection) -> None:
    for table, column in [
        ("customers", "phone"),
        ("customers", "referral_source"),
        ("web_sessions", "customer_id"),
        ("orders", "discount_code"),
        ("support_tickets", "order_id"),
    ]:
        nulls = _scalar(seeded, f"SELECT count(*) FILTER (WHERE {column} IS NULL) FROM raw.{table}")
        assert nulls > 0, f"{table}.{column}"


def test_children_reference_existing_parents(seeded: duckdb.DuckDBPyConnection) -> None:
    for child, parent, key in [
        ("order_items", "orders", "order_id"),
        ("payments", "orders", "order_id"),
        ("order_items", "products", "product_id"),
        ("marketing_spend", "marketing_campaigns", "campaign_id"),
    ]:
        orphans = _scalar(
            seeded, f"SELECT count(*) FROM raw.{child} ANTI JOIN raw.{parent} USING ({key})"
        )
        assert orphans == 0, f"{child}.{key} -> {parent}"


def test_appending_a_day_equals_seeding_through_that_day(small: Settings) -> None:
    """The Phase 2 daily append is the same function over a one-day window."""
    day_after = small.simulation.start + timedelta(days=1)
    plan = RawDataPlan.from_settings(small)

    appended = connect(path=":memory:")
    raw_data.seed(appended, small)
    added = raw_data.load_window(appended, plan, after=small.simulation.start, until=day_after)

    direct = connect(path=":memory:")
    raw_data.create_raw_tables(direct)
    raw_data.load_window(direct, plan, after=None, until=day_after)

    assert added["orders"] > 0, "one simulated day should bring new orders"
    for table in TABLES:
        assert _rows(appended, table) == _rows(direct, table), table
