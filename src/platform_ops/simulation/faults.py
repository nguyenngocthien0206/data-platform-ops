"""Faults in operation, after cutover: what breaks raw data on ClickHouse, and the fix.

During the production weeks (Phase 14) the company runs on ClickHouse and
things go wrong the way they do in production: a loader writes NULLs, an
upstream system starts sending a status nobody expected, a feed stops, a
column disappears. Each fault is aimed at a check that already exists on the
staging model reading the faulted source, so it has one clear root cause.

Faults are planted in ClickHouse's raw layer, as the admin, after the day's
work and before the next night's load and merge. Rows are picked by a stable
hash of the seed, so two runs plant the same fault in the same rows. Each one
is recorded in ``ops.incident_faults``, the ground truth only the grading reads:
the incident desk finds incidents from what dbt reports, as a real team would.

The fix is what a platform team does once an owner has found the cause: the
source of truth is the company's Postgres, so the faulted raw table is
resynced from it in full (``Ingestion.resync``), and the loader resumes if it
had stopped. ``ReplacingMergeTree`` deduplicates raw by key, so the duplicate
keys of 1.x cannot happen here; payments that point at no order (orphan keys)
take their place, a failure a migration to an engine without enforced foreign
keys makes common.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from platform_ops.common.config import Settings
from platform_ops.common.store import open_store, ops_table, replace_ops_table
from platform_ops.simulation import raw_data

FaultType = Literal[
    "null_spike",
    "invalid_category",
    "orphan_keys",
    "stale_source",
    "schema_drop",
    "schema_rename",
    "volume_drop",
    "loader_nulls",
]

# Faults that stop the loader for their table until the fix: a stale feed, and
# a raw table the loader can no longer write because its columns changed.
STOPS_LOADER: frozenset[FaultType] = frozenset({"stale_source", "schema_drop", "schema_rename"})
# Faults the loader carries into every load until the fix: it writes NULL into
# the column, the way an upstream format change breaks one field and no other.
LOADS_NULLS: frozenset[FaultType] = frozenset({"loader_nulls"})
RENAMED_SUFFIX = "_renamed"
# One row in this many is hit by a null spike, a bad category or an orphan copy.
SHARE = 50
# A volume drop removes this many rows in ten.
DROPPED_TENTHS = 6
# Orphans point at order ids above every real one.
ORPHAN_OFFSET = 10**12


@dataclass(frozen=True)
class Fault:
    fault_id: str
    fault_type: FaultType
    table: str
    column: str | None = None
    bad_value: str | None = None
    expected_check: str = ""

    @property
    def stops_loader(self) -> bool:
        return self.fault_type in STOPS_LOADER

    @property
    def loads_nulls(self) -> bool:
        return self.fault_type in LOADS_NULLS


# Two faults a day, on different tables, in this order over incidents.fault_days.
# fmt: off
CATALOGUE: tuple[Fault, ...] = (
    Fault("F1", "null_spike", "orders", "customer_id",
          expected_check="not_null on stg_orders.customer_id"),
    Fault("F2", "invalid_category", "support_tickets", "priority", "p0",
          expected_check="accepted_values on stg_support_tickets.priority"),
    Fault("F3", "orphan_keys", "payments",
          expected_check="relationships from stg_payments.order_id to stg_orders"),
    Fault("F4", "stale_source", "web_sessions",
          expected_check="source freshness on raw.web_sessions"),
    Fault("F5", "schema_drop", "marketing_spend", "clicks",
          expected_check="model error on stg_marketing_spend"),
    Fault("F6", "volume_drop", "orders",
          expected_check="row_count_in_range on stg_orders"),
    Fault("F7", "schema_rename", "marketing_campaigns", "channel",
          expected_check="model error on stg_marketing_campaigns"),
    Fault("F8", "invalid_category", "payments", "status", "chargeback",
          expected_check="accepted_values on stg_payments.payment_status"),
    # A column no dbt test checks: only the column profile on the source sees it.
    Fault("F9", "loader_nulls", "web_sessions", "device",
          expected_check="column_profile on raw.web_sessions (device NULLs)"),
)
# fmt: on


def schedule(fault_days: list[int], production_days: int) -> dict[int, tuple[Fault, ...]]:
    """Production day -> the faults that happen on it: two per listed day, in order
    (the last day has one when the catalogue runs out).

    A day too late to be caught and fixed inside the window is dropped.
    """
    out: dict[int, tuple[Fault, ...]] = {}
    for n, day in enumerate(fault_days):
        pair = CATALOGUE[2 * n : 2 * n + 2]
        if pair and day < production_days - 1:
            out[day] = pair
    return out


GROUND_TRUTH_DDL = """
    fault_id text PRIMARY KEY, fault_type text NOT NULL, table_name text NOT NULL,
    column_name text, expected_check text NOT NULL, injected_at timestamp NOT NULL,
    repaired_at timestamp, rows_affected bigint NOT NULL"""
GROUND_TRUTH_COLUMNS = [
    "fault_id",
    "fault_type",
    "table_name",
    "column_name",
    "expected_check",
    "injected_at",
    "repaired_at",
    "rows_affected",
]


def reset_ground_truth(settings: Settings) -> None:
    with open_store(settings) as store:
        replace_ops_table(
            store, settings, "incident_faults", GROUND_TRUTH_DDL, GROUND_TRUTH_COLUMNS, []
        )


def _raw(settings: Settings, table: str) -> str:
    return f"`{settings.company.name(settings.company.raw_database)}`.`{table}`"


def _count(client: Any, query: str) -> int:
    return int(client.query(query).result_rows[0][0] or 0)


def plant(settings: Settings, client: Any, fault: Fault, at: datetime) -> int:
    """Break the raw table, record the ground truth, and return the rows affected.

    Only rows from the history before the window are picked: no load re-reads
    them (the overlap only reaches back an hour), so the fault stays until fixed.
    """
    raw = _raw(settings, fault.table)
    key = raw_data.PRIMARY_KEYS[fault.table]
    start = settings.simulation.start.strftime("%Y-%m-%d %H:%M:%S")
    old = f"updated_at <= toDateTime64('{start}', 6, 'UTC')"
    picked = f"{old} AND cityHash64({settings.seed}, '{fault.fault_id}', `{key}`) % {SHARE} = 0"
    sync = "SETTINGS mutations_sync = 2"
    rows = 0
    if fault.fault_type == "null_spike":
        rows = _count(client, f"SELECT count() FROM {raw} FINAL WHERE {picked}")
        client.command(f"ALTER TABLE {raw} UPDATE `{fault.column}` = NULL WHERE {picked} {sync}")
    elif fault.fault_type == "invalid_category":
        rows = _count(client, f"SELECT count() FROM {raw} FINAL WHERE {picked}")
        client.command(
            f"ALTER TABLE {raw} UPDATE `{fault.column}` = '{fault.bad_value}' WHERE {picked} {sync}"
        )
    elif fault.fault_type == "orphan_keys":
        top = _count(client, f"SELECT max(`{key}`) FROM {raw}")
        names = _columns(client, settings, fault.table)
        values = ", ".join(
            f"{top} + row_number() OVER (ORDER BY `{key}`)"
            if n == key
            else (f"`order_id` + {ORPHAN_OFFSET}" if n == "order_id" else f"`{n}`")
            for n in names
        )
        listed = ", ".join(f"`{n}`" for n in names)
        rows = _count(client, f"SELECT count() FROM {raw} FINAL WHERE {picked}")
        client.command(
            f"INSERT INTO {raw} ({listed}) SELECT {values} FROM "
            f"(SELECT * FROM {raw} FINAL WHERE {picked} ORDER BY `{key}`)"
        )
    elif fault.fault_type == "volume_drop":
        dropped = (
            f"cityHash64({settings.seed}, '{fault.fault_id}', `{key}`) % 10 < {DROPPED_TENTHS}"
        )
        rows = _count(client, f"SELECT count() FROM {raw} FINAL WHERE {dropped}")
        client.command(f"ALTER TABLE {raw} DELETE WHERE {dropped} {sync}")
    elif fault.fault_type == "schema_drop":
        client.command(f"ALTER TABLE {raw} DROP COLUMN `{fault.column}`")
    elif fault.fault_type == "schema_rename":
        client.command(
            f"ALTER TABLE {raw} RENAME COLUMN `{fault.column}` TO `{fault.column}{RENAMED_SUFFIX}`"
        )
    # A stale source changes nothing now: the loader stops reading it. A loader
    # writing NULLs changes nothing now either: the next load carries it.
    with (
        open_store(settings) as store,
        store.cursor().copy(_copy(settings, "incident_faults", GROUND_TRUTH_COLUMNS)) as copy,
    ):
        copy.write_row(
            (
                fault.fault_id,
                fault.fault_type,
                fault.table,
                fault.column,
                fault.expected_check,
                at,
                None,
                rows,
            )
        )
    return rows


def repaired(settings: Settings, fault: Fault, at: datetime) -> None:
    """Record when the fix for ``fault`` landed (the resync is the ingestion's job)."""
    from psycopg import sql

    with open_store(settings) as store:
        store.execute(
            sql.SQL("UPDATE {} SET repaired_at = %s WHERE fault_id = %s").format(
                ops_table(settings, "incident_faults")
            ),
            [at, fault.fault_id],
        )


def _columns(client: Any, settings: Settings, table: str) -> list[str]:
    rows = client.query(
        "SELECT name FROM system.columns WHERE database = %(db)s AND table = %(t)s "
        "ORDER BY position",
        parameters={"db": settings.company.name(settings.company.raw_database), "t": table},
    ).result_rows
    return [str(name) for (name,) in rows]


def _copy(settings: Settings, table: str, columns: list[str]) -> Any:
    from psycopg import sql

    return sql.SQL("COPY {} ({}) FROM STDIN").format(
        ops_table(settings, table), sql.SQL(", ").join(map(sql.Identifier, columns))
    )
