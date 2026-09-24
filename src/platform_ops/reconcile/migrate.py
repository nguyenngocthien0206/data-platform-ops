"""The migration job: legacy tables into Iceberg, with four realistic defects.

The job exports each legacy table, transforms it in DuckDB, and writes it to an
Iceberg table (PyIceberg, SQLite catalog, local warehouse). Its defects are the
kind that survive code review because each line looks reasonable:

- D1 ``payments.paid_local`` is converted to UTC with a fixed five-hour offset.
  That is right in winter and an hour off for every payment made under
  daylight saving time.
- D2 ``orders.amount`` goes through DOUBLE. Ordinary amounts survive; the few
  very large enterprise amounts lose their last decimal.
- D3 ``customers.company_name`` is right-trimmed, dropping padding the legacy
  system kept.
- D4 orders are copied in batches of ``BATCH_ROWS`` keys, and the batch filter
  uses ``>`` where it needed ``>=``, so the first order of every batch is lost.

The job knows exactly which cells each defect touched, and records them as
ground truth. Nothing in the diff reads it.

``fixed=True`` runs the job with all four defects corrected. The report uses it
as the second pass of a real sign-off loop: the delivered job fails, the team
fixes it, and the re-run shows what is left (ADR 0009).
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.table import Table

from platform_ops.common.config import DiscrepancyClass
from platform_ops.reconcile.connectors import LegacyStore
from platform_ops.reconcile.schema import TABLES, TableSpec

BATCH_ROWS = 25_000
# What the job believes the legacy zone's offset is: the winter one.
ASSUMED_OFFSET_HOURS = 5


@dataclass(frozen=True)
class TruthCell:
    """One cell (or, with ``column`` empty, one whole row) that should differ."""

    table: str
    key: int
    column: str
    klass: DiscrepancyClass
    source: str  # "migration_defect" or "injected"
    label: str


def open_catalog(warehouse: Path) -> SqlCatalog:
    """The SQLite-backed catalog. PyIceberg on Windows needs a plain path, not a URI."""
    warehouse.mkdir(parents=True, exist_ok=True)
    return SqlCatalog(
        "reconcile",
        uri=f"sqlite:///{warehouse.as_posix()}/catalog.db",
        warehouse=warehouse.as_posix(),
    )


def reset_warehouse(warehouse: Path) -> None:
    """Every run starts from an empty Iceberg warehouse."""
    if warehouse.exists():
        shutil.rmtree(warehouse)


def namespace_for(engine: str, fixed: bool) -> str:
    return f"migrated_{engine}" + ("_fixed" if fixed else "")


def _fixed_sql(table: TableSpec, zone: str) -> str:
    """The job as it should have been: exact types, correct zone, nothing dropped."""
    columns = []
    for column in table.columns:
        if column.kind == "local_ts":
            columns.append(f"{column.name} AT TIME ZONE '{zone}' AS {column.name}")
        else:
            columns.append(column.name)
    return f"SELECT {', '.join(columns)} FROM src ORDER BY {table.key}"


def _transform_sql(table: TableSpec, zone: str) -> str:
    if table.name == "customers":
        return f"""SELECT customer_id, first_name, last_name, email,
                          rtrim(company_name) AS company_name,
                          country, is_active, credit_limit,
                          created_local AT TIME ZONE '{zone}' AS created_local, updated_at
                   FROM src ORDER BY customer_id"""
    if table.name == "orders":
        return f"""SELECT order_id, customer_id, status, channel, currency,
                          CAST(amount AS DOUBLE) AS amount, fx_rate, is_gift,
                          ordered_local AT TIME ZONE '{zone}' AS ordered_local, created_at
                   FROM src
                   WHERE (order_id - (SELECT min(order_id) FROM src)) % {BATCH_ROWS} <> 0
                   ORDER BY order_id"""
    return f"""SELECT payment_id, order_id, method, status, amount, fee, is_refund,
                      (paid_local + INTERVAL {ASSUMED_OFFSET_HOURS} HOUR) AT TIME ZONE 'UTC'
                          AS paid_local,
                      settled_at
               FROM src ORDER BY payment_id"""


def _truth_sql(table: TableSpec, zone: str) -> list[tuple[str, str, DiscrepancyClass, str]]:
    """(label, SQL selecting affected keys, class, column) for each defect on ``table``."""
    if table.name == "customers":
        return [("D3", "SELECT customer_id FROM src WHERE company_name <> rtrim(company_name)",
                 "whitespace", "company_name")]  # fmt: skip
    if table.name == "orders":
        kept = f"(order_id - (SELECT min(order_id) FROM src)) % {BATCH_ROWS} <> 0"
        return [
            ("D4", f"SELECT order_id FROM src WHERE NOT ({kept})", "missing_in_target", ""),
            ("D2", f"""SELECT order_id FROM src WHERE {kept}
                       AND CAST(CAST(amount AS DOUBLE) AS DECIMAL(38, 6)) <> amount""",
             "rounding", "amount"),
        ]  # fmt: skip
    return [("D1", f"""SELECT payment_id FROM src
                       WHERE (paid_local + INTERVAL {ASSUMED_OFFSET_HOURS} HOUR) AT TIME ZONE 'UTC'
                             <> paid_local AT TIME ZONE '{zone}'""",
             "timezone_shift", "paid_local")]  # fmt: skip


@dataclass(frozen=True)
class MigrationResult:
    tables: dict[str, Table]
    truth: list[TruthCell]
    rows_read: dict[str, int]
    rows_written: dict[str, int]


def migrate(
    store: LegacyStore,
    catalog: SqlCatalog,
    engine: str,
    zone: str,
    work_dir: Path,
    tables: Sequence[TableSpec] = TABLES,
    *,
    fixed: bool = False,
) -> MigrationResult:
    namespace = namespace_for(engine, fixed)
    catalog.create_namespace_if_not_exists(namespace)
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    written: dict[str, Table] = {}
    truth: list[TruthCell] = []
    rows_read: dict[str, int] = {}
    rows_written: dict[str, int] = {}
    try:
        for spec in tables:
            exported = store.export(spec, work_dir)
            rows_read[spec.name] = exported.num_rows
            con.register("src", exported)
            transform = _fixed_sql(spec, zone) if fixed else _transform_sql(spec, zone)
            migrated: pa.Table = con.execute(transform).to_arrow_table()
            for label, sql, klass, column in [] if fixed else _truth_sql(spec, zone):
                for (key,) in con.execute(sql + " ORDER BY 1").fetchall():
                    truth.append(TruthCell(spec.name, int(key), column, klass,
                                           "migration_defect", label))  # fmt: skip
            con.unregister("src")
            identifier = f"{namespace}.{spec.name}"
            if catalog.table_exists(identifier):
                catalog.drop_table(identifier)
            iceberg = catalog.create_table(identifier, schema=migrated.schema)
            iceberg.append(migrated)
            written[spec.name] = catalog.load_table(identifier)
            rows_written[spec.name] = migrated.num_rows
    finally:
        con.close()
    return MigrationResult(written, truth, rows_read, rows_written)


def data_files(table: Table) -> list[str]:
    """The Parquet files a scan of ``table`` reads, as local paths.

    PyIceberg plans the scan; DuckDB then reads the files itself, so the
    checksums are computed inside DuckDB. PyIceberg's deletes are copy-on-write,
    so a planned file never carries delete files; that is checked, not assumed.
    """
    files = []
    for task in table.scan().plan_files():
        if task.delete_files:
            raise RuntimeError(f"{table.name()} has delete files, which this reader does not apply")
        path = task.file.file_path
        files.append(path.removeprefix("file://"))
    return sorted(files)
