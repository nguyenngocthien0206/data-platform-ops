"""The migration's planted discrepancies, and their ground truth (version 2.0, Phase 12).

Two kinds, the same split a real parallel run sees:

- **The delivered job's defects** (``ingest.py``, ``company.delivered_job``)
  are systematic: every row the job copies goes through them, every night,
  until the fixed job ships and backfills. Their truth is computed from the
  legacy rows with the same rule the defect applies, never from the diff.
- **One-off faults** are the handful of rows deleted, invented, edited or
  reformatted in ClickHouse after the copy. One is planted on a night, after
  the ingestion and before dbt builds (so the reports see it too), and the
  team repairs it the next day, as it would after reading the morning's
  reconciliation report: invented rows are deleted, the others reloaded from
  Postgres.

Every fault touches a column no dbt test checks: the point of a parallel run
is what the existing tests let through (a padded order status, the first
version of M5, failed the ``accepted_values`` test and stopped the build).

Faults pick their rows by a stable hash of the seed among rows from the
history (older than the window start), so the overlap re-read of a later load
never repairs one by accident, and every planted discrepancy has exactly one
cause and one expected class.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import sql

from platform_ops.common.config import DiscrepancyClass, Settings
from platform_ops.simulation import raw_data
from platform_ops.simulation.ingest import raw_columns
from platform_ops.simulation.legacy_db import LOADED_AT, UPDATED_AT


@dataclass(frozen=True)
class TruthCell:
    """One cell (or, with ``column`` empty, one whole row) that should differ."""

    table: str
    key: str
    column: str
    klass: DiscrepancyClass
    cause: str  # "job_defect" or "injected"
    label: str


# -- the delivered job's defects ------------------------------------------------------


def _shifted(value: datetime, zone: str) -> bool:
    """Whether reading a UTC wall-clock time as local time in ``zone`` moves it."""
    local = value.replace(tzinfo=ZoneInfo(zone))
    return local.astimezone(UTC).replace(tzinfo=None) != value


def _through_float(value: Decimal) -> Decimal:
    """What ``toDecimal64(toFloat64(x), 2)`` leaves of ``x``.

    ClickHouse scales the float by 100 in floating point (which rounds) and
    then truncates; checked against ClickHouse on all 575,208 payments at
    scale 1.0. Truncating the float's exact binary value instead disagrees on
    245,308 of them.
    """
    return (Decimal(math.trunc(float(value) * 100)) / 100).quantize(Decimal("0.01"))


def delivered_truth(settings: Settings, connection: Any, as_of: datetime) -> list[TruthCell]:
    """Every discrepancy the delivered job puts in raw, from the legacy rows.

    Only rows the copy has read: changed at or before ``as_of``, its watermark.
    The comparison leaves later rows for the next night, so they are not
    expected tonight.
    """
    job = settings.company.delivered_job
    schema = sql.Identifier(settings.company.name(settings.company.legacy_schema))
    keys = raw_data.PRIMARY_KEYS
    read = sql.SQL("{} <= %s").format(sql.Identifier(UPDATED_AT))

    def rows(table: str, column: str) -> list[tuple[Any, Any]]:
        query = sql.SQL("SELECT {}, {} FROM {}.{} WHERE {} IS NOT NULL AND {} ORDER BY 1").format(
            sql.Identifier(keys[table]),
            sql.Identifier(column),
            schema,
            sql.Identifier(table),
            sql.Identifier(column),
            read,
        )
        return list(connection.execute(query, [as_of]).fetchall())

    truth: list[TruthCell] = []
    for key, value in rows(job.timezone_table, job.timezone_column):
        if _shifted(value, job.timezone):
            truth.append(
                TruthCell(
                    job.timezone_table,
                    str(key),
                    job.timezone_column,
                    "timezone_shift",
                    "job_defect",
                    "D1 local time read as UTC",
                )
            )
    for key, value in rows(job.float_table, job.float_column):
        if _through_float(value) != value:
            truth.append(
                TruthCell(
                    job.float_table,
                    str(key),
                    job.float_column,
                    "rounding",
                    "job_defect",
                    "D2 decimal through a float",
                )
            )
    for key, value in rows(job.trim_table, job.trim_column):
        if value != value.rstrip(" "):
            truth.append(
                TruthCell(
                    job.trim_table,
                    str(key),
                    job.trim_column,
                    "whitespace",
                    "job_defect",
                    "D3 trailing spaces trimmed",
                )
            )
    batch_key = sql.Identifier(keys[job.batch_table])
    lost = connection.execute(
        sql.SQL("SELECT {} FROM {}.{} WHERE mod({}, %s) = 0 AND {} ORDER BY 1").format(
            batch_key, schema, sql.Identifier(job.batch_table), batch_key, read
        ),
        [job.batch_rows, as_of],
    ).fetchall()
    truth += [
        TruthCell(
            job.batch_table,
            str(key),
            "",
            "missing_in_target",
            "job_defect",
            "D4 last key of every page lost",
        )
        for (key,) in lost
    ]
    return truth


# -- one-off faults ----------------------------------------------------------------------


INVENTED_KEYS = 1_000_000_000


@dataclass(frozen=True)
class OneOffFault:
    fault_id: str
    table: str
    klass: DiscrepancyClass
    column: str
    rows: int
    # ClickHouse expression for the new value; `{c}` is the column. Unused for row faults.
    change: str = ""
    label: str = ""


CATALOGUE: tuple[OneOffFault, ...] = (
    OneOffFault("M1", "orders", "extra_in_target", "", 5, label="M1 invented orders"),
    OneOffFault("M2", "customers", "missing_in_target", "", 6, label="M2 customers deleted"),
    OneOffFault(
        "M3",
        "payments",
        "value_mismatch",
        "payment_method",
        8,
        "'crypto'",
        "M3 payment method overwritten",
    ),
    OneOffFault("M4", "customers", "case_only", "email", 8, "upper({c})", "M4 emails upper-cased"),
    # On a column no dbt test checks: a padded order status would fail the
    # accepted_values test before the reconciliation ever saw it.
    OneOffFault(
        "M5",
        "products",
        "whitespace",
        "product_name",
        6,
        "concat({c}, ' ')",
        "M5 product names padded",
    ),
    # Errors that look like rounding and a time zone shift but are neither: a
    # cent added is no rounding of the amount, and two hours is not the offset
    # of a zone the company runs in. The reconciliation must not excuse them.
    OneOffFault(
        "M6", "payments", "value_mismatch", "amount", 6, "{c} + 0.01", "M6 amounts off by a cent"
    ),
    OneOffFault(
        "M7",
        "support_tickets",
        "value_mismatch",
        "created_at",
        5,
        "{c} + INTERVAL 2 HOUR",
        "M7 ticket times shifted",
    ),
)


def fault_nights(first_night: int, last_night: int) -> dict[int, OneOffFault]:
    """Which fault is planted on which night: every other night from ``first_night``.

    A fault needs the night after it for its repair, so none is planted on the
    last night of the window.
    """
    schedule: dict[int, OneOffFault] = {}
    night = first_night
    for fault in CATALOGUE:
        if night >= last_night:
            break
        schedule[night] = fault
        night += 2
    return schedule


@dataclass
class Planted:
    fault: OneOffFault
    keys: list[int]

    def truth(self) -> list[TruthCell]:
        f = self.fault
        return [
            TruthCell(f.table, str(k), f.column, f.klass, "injected", f.label)
            for k in sorted(self.keys)
        ]


def plant(settings: Settings, client: Any, fault: OneOffFault) -> Planted:
    """Apply one fault to its raw table in ClickHouse, as the admin."""
    raw = f"`{settings.company.name(settings.company.raw_database)}`.`{fault.table}`"
    key = raw_data.PRIMARY_KEYS[fault.table]
    start = settings.simulation.start.strftime("%Y-%m-%d %H:%M:%S")
    sync = "SETTINGS mutations_sync = 2"
    if fault.klass == "extra_in_target":
        # Far above the keys in use: the application's next rows take the keys
        # just above the copy's, and are in Postgres already, written after the
        # watermark, so invented rows there would pass as rows in flight.
        top = INVENTED_KEYS + int(
            client.query(f"SELECT max({key}) FROM {raw}").result_rows[0][0] or 0
        )
        keys = list(range(top + 1, top + 1 + fault.rows))
        names = [n for n, _ in raw_columns(fault.table)]
        listed = ", ".join(f"`{n}`" for n in names)
        # Copies of real rows under new keys: plausible, and invented.
        copied = ", ".join(
            f"{top} + row_number() OVER (ORDER BY `{key}`)" if n == key else f"`{n}`" for n in names
        )
        client.command(
            f"INSERT INTO {raw} ({listed}) SELECT {copied} FROM "
            f"(SELECT * FROM {raw} FINAL ORDER BY `{key}` LIMIT {fault.rows})"
        )
        return Planted(fault, keys)
    column = fault.column or key
    keys = [
        int(k)
        for (k,) in client.query(
            f"SELECT `{key}` FROM {raw} FINAL WHERE `{column}` IS NOT NULL "
            f"AND updated_at <= toDateTime64('{start}', 6, 'UTC') "
            f"ORDER BY cityHash64({settings.seed}, '{fault.fault_id}', `{key}`), `{key}` "
            f"LIMIT {fault.rows}"
        ).result_rows
    ]
    members = ", ".join(str(k) for k in keys)
    if fault.klass == "missing_in_target":
        client.command(f"ALTER TABLE {raw} DELETE WHERE `{key}` IN ({members}) {sync}")
    else:
        change = fault.change.format(c=f"`{fault.column}`")
        client.command(
            f"ALTER TABLE {raw} UPDATE `{fault.column}` = {change} "
            f"WHERE `{key}` IN ({members}) {sync}"
        )
    return Planted(fault, keys)


def repair(settings: Settings, client: Any, planted: Planted, loaded_at: datetime) -> None:
    """Undo one fault: delete invented rows, reload the others from Postgres.

    A reloaded row carries the same ``updated_at`` as the copy it replaces;
    ``ReplacingMergeTree`` keeps the last inserted of equal versions, so the
    reload wins.
    """
    company = settings.company
    raw = f"`{company.name(company.raw_database)}`.`{planted.fault.table}`"
    key = raw_data.PRIMARY_KEYS[planted.fault.table]
    members = ", ".join(str(k) for k in planted.keys)
    if planted.fault.klass == "extra_in_target":
        client.command(
            f"ALTER TABLE {raw} DELETE WHERE `{key}` IN ({members}) SETTINGS mutations_sync = 2"
        )
        return
    from platform_ops.common.servers import postgres_params

    pg = postgres_params(settings)
    names = [n for n, _ in raw_columns(planted.fault.table) if n != LOADED_AT]
    listed = ", ".join(f"`{n}`" for n in names)
    stamp = f"toDateTime64('{loaded_at:%Y-%m-%d %H:%M:%S}', 6, 'UTC')"
    client.command(
        f"INSERT INTO {raw} ({listed}, {LOADED_AT}) SELECT {listed}, {stamp} "
        f"FROM postgresql('{company.postgres_from_clickhouse}', '{pg.dbname}', "
        f"'{planted.fault.table}', '{pg.user}', '{pg.password}', "
        f"'{company.name(company.legacy_schema)}') "
        f"WHERE `{key}` IN ({members}) ORDER BY `{key}`"
    )
