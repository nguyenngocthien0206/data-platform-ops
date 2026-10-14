"""Incremental ingestion from the legacy Postgres into ClickHouse (version 2.0).

The company has no change-data-capture stream. Like many teams in that spot, it
pulls new and changed rows on a schedule, by a watermark on ``updated_at``:
every load reads the rows whose ``updated_at`` falls in ``(watermark - overlap,
until]`` through ClickHouse's ``postgresql()`` table function, which runs on the
ClickHouse server and reads Postgres directly.

The overlap re-reads a margin before the watermark, so a row that committed
late with an older ``updated_at`` is not lost. Reading a row twice is harmless
because every raw table is a ``ReplacingMergeTree`` versioned by
``updated_at`` and ordered by the primary key: the latest version of a row
wins, whether it changed in place or was simply read again. Readers that need
one row per key read with ``FINAL``.

Raw tables are not partitioned. ``ReplacingMergeTree`` only deduplicates within
a partition, and a row that changes moves in ``updated_at``, so partitioning by
it would keep both versions forever.

Each load is recorded in the ops store (``ingestion_loads``), with its range
and the rows it moved; the latest ``loaded_until`` per table is the watermark.

Until the migration team ships its fix (Phase 12), the job runs as it was
first delivered (``delivered = True``), with four defects a real copy job
ships with: a UTC timestamp read as New York time, a decimal passed through a
float and truncated on the way back, trailing spaces trimmed, and the last key
of every page of a paged table lost (``company.delivered_job``). Each is a
change to one column's expression or the load's filter, nothing else.

The loads run as the company's ``loader`` user (``simulation.access``), which
may only insert into raw and read Postgres, so ClickHouse logs the ingestion
under its own name. Creating the raw tables is the platform's setup, done by
the admin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.servers import clickhouse_client, postgres_params
from platform_ops.common.store import open_store, ops_table
from platform_ops.common.tagging import Tag
from platform_ops.simulation import raw_data
from platform_ops.simulation.legacy_db import LOADED_AT, UPDATED_AT

_TYPES = {
    "BIGINT": "Int64",
    "INTEGER": "Int32",
    "VARCHAR": "String",
    "TIMESTAMP": "DateTime64(6, 'UTC')",
    "DATE": "Date32",
    "BOOLEAN": "Bool",
}

LOADS_DDL = """
    table_name text NOT NULL,
    loaded_from timestamp,
    loaded_until timestamp NOT NULL,
    rows_read bigint NOT NULL,
    loaded_at timestamp NOT NULL"""


def clickhouse_type(duckdb_type: str) -> str:
    decimal = re.fullmatch(r"DECIMAL\((\d+),\s*(\d+)\)", duckdb_type)
    if decimal:
        return f"Decimal({decimal[1]}, {decimal[2]})"
    return _TYPES[duckdb_type]


def raw_columns(table: str) -> list[tuple[str, str]]:
    """(name, ClickHouse type) of a raw table: Postgres's columns, then ``_loaded_at``.

    The key and ``updated_at`` are never NULL; every other column may be, as in
    Postgres. ``_loaded_at`` is when the row reached ClickHouse, the column
    dbt's source freshness watches.
    """
    key = raw_data.PRIMARY_KEYS[table]
    columns: list[tuple[str, str]] = []
    for name, data_type in raw_data._ddl_columns(table):
        ch = clickhouse_type(data_type)
        if name == LOADED_AT:
            columns.append((UPDATED_AT, ch))
        elif name == key:
            columns.append((name, ch))
        else:
            columns.append((name, f"Nullable({ch})"))
    columns.append((LOADED_AT, clickhouse_type("TIMESTAMP")))
    return columns


def _ts(value: datetime) -> str:
    return f"toDateTime64('{value:%Y-%m-%d %H:%M:%S}', 6, 'UTC')"


@dataclass
class Ingestion:
    """Recreates the raw layer and pulls from Postgres into it."""

    settings: Settings
    # The job as the migration team first delivered it, defects included.
    delivered: bool = False

    def __post_init__(self) -> None:
        self.client: Any = clickhouse_client(self.settings)
        self.database = self.settings.company.name(self.settings.company.raw_database)
        self.overlap = timedelta(minutes=self.settings.company.ingest_overlap_minutes)

    def close(self) -> None:
        self.client.close()

    def recreate(self) -> None:
        """Drop and recreate every raw table, empty, and forget every watermark."""
        self._empty_raw()
        with open_store(self.settings) as store:
            store.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(self._loads()))
            store.execute(sql.SQL("CREATE TABLE {} ({})").format(self._loads(), sql.SQL(LOADS_DDL)))

    def backfill(self, until: datetime) -> dict[str, int]:
        """Empty raw and reload every table in full, keeping the record of earlier loads.

        What a migration team does when it ships a fixed job: the old copy is
        not patched, it is replaced.
        """
        self._empty_raw()
        return self.load(until, full=True)

    def _empty_raw(self) -> None:
        self.client.command(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")
        for table in raw_data.TABLES:
            columns = ", ".join(f"`{name}` {data_type}" for name, data_type in raw_columns(table))
            key = raw_data.PRIMARY_KEYS[table]
            self.client.command(
                f"CREATE OR REPLACE TABLE `{self.database}`.`{table}` ({columns}) "
                f"ENGINE = ReplacingMergeTree({UPDATED_AT}) ORDER BY {key}"
            )

    def _loads(self) -> sql.Composed:
        return ops_table(self.settings, "ingestion_loads")

    def watermarks(self) -> dict[str, datetime]:
        with open_store(self.settings) as store:
            rows = store.execute(
                sql.SQL("SELECT table_name, max(loaded_until) FROM {} GROUP BY table_name").format(
                    self._loads()
                )
            ).fetchall()
        return {str(name): until for name, until in rows}

    def load(self, until: datetime, *, full: bool = False) -> dict[str, int]:
        """Pull every table up to ``until`` from its watermark, minus the overlap, as ``loader``.

        ``full`` ignores the watermarks and reads everything up to ``until``.
        """
        marks = {} if full else self.watermarks()
        loader = clickhouse_client(self.settings, "loader")
        try:
            moved, records = self._pull(loader, marks, until)
        finally:
            loader.close()
        self._record(records)
        return moved

    def _pull(
        self, loader: Any, marks: dict[str, datetime], until: datetime
    ) -> tuple[dict[str, int], list[tuple[Any, ...]]]:
        pg = postgres_params(self.settings)
        schema = self.settings.company.name(self.settings.company.legacy_schema)
        source_host = self.settings.company.postgres_from_clickhouse
        moved: dict[str, int] = {}
        records: list[tuple[Any, ...]] = []
        for table in raw_data.TABLES:
            mark = marks.get(table)
            since = None if mark is None else mark - self.overlap
            names = [name for name, _ in raw_columns(table) if name != LOADED_AT]
            selected = ", ".join(f"`{n}`" for n in names)
            read = ", ".join(self._expression(table, n) for n in names)
            where = f"{UPDATED_AT} <= {_ts(until)}"
            if since is not None:
                where = f"{UPDATED_AT} > {_ts(since)} AND {where}"
            where += self._dropped(table)
            tag = Tag(until, "ingestion", subject=f"source.company.raw.{table}")
            summary = loader.command(
                f"INSERT INTO `{self.database}`.`{table}` ({selected}, {LOADED_AT}) "
                f"SELECT {read}, {_ts(until)} "
                f"FROM postgresql('{source_host}', '{pg.dbname}', '{table}', "
                f"'{pg.user}', '{pg.password}', '{schema}') "
                f"WHERE {where} ORDER BY {raw_data.PRIMARY_KEYS[table]}",
                settings=tag.settings(),
            )
            rows = int(getattr(summary, "written_rows", 0) or 0)
            moved[table] = rows
            records.append((table, since, until, rows, until))
        return moved, records

    def _expression(self, table: str, column: str) -> str:
        """How the job reads one column: as it is, or with a delivered defect."""
        plain = f"`{column}`"
        if not self.delivered:
            return plain
        job = self.settings.company.delivered_job
        if (table, column) == (job.timezone_table, job.timezone_column):
            # The wall-clock text of a UTC instant, parsed as local time.
            return f"toDateTime64(toString({plain}), 6, '{job.timezone}')"
        if (table, column) == (job.float_table, job.float_column):
            return f"toDecimal64(toFloat64({plain}), 2)"
        if (table, column) == (job.trim_table, job.trim_column):
            return f"trimRight({plain})"
        return plain

    def _dropped(self, table: str) -> str:
        """The delivered job's paging bug: the last key of every page is never loaded."""
        job = self.settings.company.delivered_job
        if not self.delivered or table != job.batch_table:
            return ""
        key = raw_data.PRIMARY_KEYS[table]
        return f" AND {key} % {job.batch_rows} != 0"

    def _record(self, records: list[tuple[Any, ...]]) -> None:
        with (
            open_store(self.settings) as store,
            store.cursor().copy(
                sql.SQL(
                    "COPY {} (table_name, loaded_from, loaded_until, rows_read, loaded_at) "
                    "FROM STDIN"
                ).format(self._loads())
            ) as copy,
        ):
            for record in records:
                copy.write_row(record)
