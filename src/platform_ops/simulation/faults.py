"""Labelled faults injected into raw data, with ground truth and repair.

Each fault is aimed at a check that already exists on the staging model reading
the faulted source, so it has one clear root cause. Injection records ground
truth in ``ops.fault_ground_truth``; the incident module never reads it, only
the metrics that grade the incident module do.

Repair relies on the generator being deterministic: whatever a fault changed,
removed or broke, the correct content can be regenerated from the seed. So every
repair is a variation of "put back what the generator says should be there",
and no backup copies are kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import duckdb

from platform_ops.common.db import OPS_SCHEMA, RAW_SCHEMA
from platform_ops.simulation import raw_data
from platform_ops.simulation.raw_data import RawDataPlan

FaultType = Literal[
    "null_spike",
    "invalid_category",
    "duplicate_keys",
    "stale_source",
    "schema_drop",
    "schema_rename",
    "volume_drop",
]


@dataclass(frozen=True)
class Fault:
    fault_id: str
    fault_type: FaultType
    table: str
    day: int
    column: str | None = None
    bad_value: str | None = None
    expected_check: str = ""

    @property
    def target(self) -> str:
        return f"{RAW_SCHEMA}.{self.table}"


# The scenario's faults, one or more of every type in the SPEC. They land in
# pairs on different tables, so one targeted run covers two faults: dbt time,
# not data volume, is what the demo budget is spent on (ADR 0008). Two faults
# never target the same table while both could be active; the scenario checks.
CATALOGUE: tuple[Fault, ...] = (
    Fault("F1", "null_spike", "orders", 2, "customer_id",
          expected_check="not_null on stg_orders.customer_id"),
    Fault("F2", "invalid_category", "support_tickets", 2, "priority", "p0",
          expected_check="accepted_values on stg_support_tickets.priority"),
    Fault("F3", "duplicate_keys", "payments", 5,
          expected_check="unique on stg_payments.payment_id"),
    Fault("F4", "stale_source", "web_sessions", 5,
          expected_check="source freshness on raw.web_sessions"),
    Fault("F5", "schema_drop", "marketing_spend", 9, "clicks",
          expected_check="model error on stg_marketing_spend"),
    Fault("F6", "volume_drop", "orders", 9,
          expected_check="row_count_in_range on stg_orders"),
    Fault("F7", "schema_rename", "marketing_campaigns", 14, "channel",
          expected_check="model error on stg_marketing_campaigns"),
    Fault("F8", "invalid_category", "payments", 14, "status", "chargeback",
          expected_check="accepted_values on stg_payments.payment_status"),
)  # fmt: skip

GROUND_TRUTH_DDL = """
    fault_id VARCHAR PRIMARY KEY,
    fault_type VARCHAR NOT NULL,
    target VARCHAR NOT NULL,
    column_name VARCHAR,
    expected_check VARCHAR NOT NULL,
    injected_at TIMESTAMP NOT NULL,
    rows_affected BIGINT NOT NULL,
    repaired_at TIMESTAMP"""

RENAMED_SUFFIX = "_v2"


def reset_ground_truth(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        f"CREATE OR REPLACE TABLE {OPS_SCHEMA}.fault_ground_truth ({GROUND_TRUTH_DDL})"
    )


def _key(fault: Fault) -> str:
    return raw_data.PRIMARY_KEYS[fault.table]


def _affected(connection: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def inject(connection: duckdb.DuckDBPyConnection, fault: Fault, at: datetime, seed: int) -> int:
    """Break the raw data and record ground truth. Returns rows affected."""
    table = f"{RAW_SCHEMA}.{fault.table}"
    key = _key(fault)
    # Recent rows, plus always the newest one, so tiny test scales still break.
    recent = (
        f"(_loaded_at > TIMESTAMP '{at - timedelta(days=3):%Y-%m-%d %H:%M:%S}' "
        f"AND hash({seed}, '{fault.fault_id}', {key}) % 2 = 0) "
        f"OR {key} = (SELECT max({key}) FROM {table})"
    )
    rows = 0
    if fault.fault_type == "null_spike":
        rows = _affected(connection, f"SELECT count(*) FROM {table} WHERE {recent}")
        connection.execute(f"UPDATE {table} SET {fault.column} = NULL WHERE {recent}")
    elif fault.fault_type == "invalid_category":
        rows = _affected(connection, f"SELECT count(*) FROM {table} WHERE {recent}")
        connection.execute(
            f"UPDATE {table} SET {fault.column} = '{fault.bad_value}' WHERE {recent}"
        )
    elif fault.fault_type == "duplicate_keys":
        rows = _affected(connection, f"SELECT count(*) FROM {table} WHERE {recent}")
        connection.execute(f"INSERT INTO {table} SELECT * FROM {table} WHERE {recent}")
    elif fault.fault_type == "volume_drop":
        drop = f"hash({seed}, '{fault.fault_id}', {key}) % 10 < 6"
        rows = _affected(connection, f"SELECT count(*) FROM {table} WHERE {drop}")
        connection.execute(f"DELETE FROM {table} WHERE {drop}")
    elif fault.fault_type == "schema_drop":
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {fault.column}")
    elif fault.fault_type == "schema_rename":
        connection.execute(
            f"ALTER TABLE {table} RENAME COLUMN {fault.column} TO {fault.column}{RENAMED_SUFFIX}"
        )
    # A stale source changes nothing now: the scenario stops loading it.
    connection.execute(
        f"INSERT INTO {OPS_SCHEMA}.fault_ground_truth VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        [
            fault.fault_id,
            fault.fault_type,
            fault.target,
            fault.column,
            fault.expected_check,
            at,
            rows,
        ],
    )
    return rows


def _restore_column(
    connection: duckdb.DuckDBPyConnection, plan: RawDataPlan, fault: Fault, until: datetime
) -> None:
    table = f"{RAW_SCHEMA}.{fault.table}"
    key = _key(fault)
    column = fault.column
    connection.execute(
        f"""UPDATE {table} AS t SET {column} = g.{column}
            FROM ({raw_data.generated_rows(plan, fault.table, until)}) AS g
            WHERE t.{key} = g.{key} AND t.{column} IS DISTINCT FROM g.{column}"""
    )


def _restore_missing_rows(
    connection: duckdb.DuckDBPyConnection, plan: RawDataPlan, table_name: str, until: datetime
) -> None:
    table = f"{RAW_SCHEMA}.{table_name}"
    key = raw_data.PRIMARY_KEYS[table_name]
    columns = ", ".join(raw_data.generated_columns(table_name))
    connection.execute(
        f"""INSERT INTO {table} ({columns})
            SELECT {columns} FROM ({raw_data.generated_rows(plan, table_name, until)}) g
            WHERE g.{key} NOT IN (SELECT {key} FROM {table})
            ORDER BY g.{key}"""
    )


def repair(
    connection: duckdb.DuckDBPyConnection,
    plan: RawDataPlan,
    fault: Fault,
    at: datetime,
    loaded_until: datetime,
    stalled_since: datetime | None = None,
) -> None:
    """Put the raw data back as the generator says it should be, and record when."""
    table = f"{RAW_SCHEMA}.{fault.table}"
    key = _key(fault)
    if fault.fault_type in ("null_spike", "invalid_category"):
        _restore_column(connection, plan, fault, loaded_until)
    elif fault.fault_type == "duplicate_keys":
        connection.execute(
            f"CREATE OR REPLACE TABLE {table} AS SELECT DISTINCT * FROM {table} ORDER BY {key}"
        )
    elif fault.fault_type == "volume_drop":
        _restore_missing_rows(connection, plan, fault.table, loaded_until)
        # Children generated while the parents were missing lost rows too.
        for child in ("order_items", "payments"):
            _restore_missing_rows(connection, plan, child, loaded_until)
    elif fault.fault_type == "schema_drop":
        column_type = raw_data.column_type(fault.table, str(fault.column))
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {fault.column} {column_type}")
        _restore_column(connection, plan, fault, loaded_until)
    elif fault.fault_type == "schema_rename":
        connection.execute(
            f"ALTER TABLE {table} RENAME COLUMN {fault.column}{RENAMED_SUFFIX} TO {fault.column}"
        )
        _restore_column(connection, plan, fault, loaded_until)
    elif fault.fault_type == "stale_source":
        # Backfill everything the source should have delivered while it was stuck.
        raw_data.load_window(
            connection, plan, after=stalled_since, until=loaded_until, tables=[fault.table]
        )
    connection.execute(
        f"UPDATE {OPS_SCHEMA}.fault_ground_truth SET repaired_at = ? WHERE fault_id = ?",
        [at, fault.fault_id],
    )
