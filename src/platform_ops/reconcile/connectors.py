"""One small interface over every engine the reconciliation talks to.

A :class:`SourceConnector` answers the only three questions the segmented diff
asks: what key range does this table cover, what are the row counts and hash
sums per segment, and what are the canonical values of the rows in these few
segments. Every answer is computed inside the engine; only summaries and the
rows of differing leaf segments cross the wire (CLAUDE.md: push computation
into the database).

The legacy side also has to be written and read in bulk, for the generator and
for the migration job. That is :class:`LegacyStore`. Postgres, SQL Server and
DuckDB are legacy stores; the Iceberg target is read through DuckDB.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import duckdb
import pyarrow as pa

from platform_ops.reconcile.canonical import RenderedTable
from platform_ops.reconcile.schema import Dialect, TableSpec, arrow_schema

LEGACY_SCHEMA = "legacy"


@dataclass(frozen=True)
class Summary:
    rows: int
    h1: int
    h2: int


class SourceConnector(Protocol):
    engine: str
    dialect: Dialect
    # The zone a legacy ``local_ts`` value was recorded in on this side, or
    # ``None`` when this side stores those columns as UTC instants.
    local_zone: str | None

    def key_range(self, table: TableSpec) -> tuple[int, int] | None: ...

    def summarize(
        self,
        table: TableSpec,
        rendered: RenderedTable,
        lo: int,
        width: int,
        parent_width: int | None,
        parents: Sequence[int],
    ) -> dict[int, Summary]: ...

    def fetch(
        self, table: TableSpec, rendered: RenderedTable, lo: int, width: int, buckets: Sequence[int]
    ) -> dict[int, tuple[str, ...]]: ...


class LegacyStore(Protocol):
    def recreate(self, tables: dict[str, pa.Table], specs: Sequence[TableSpec]) -> None: ...

    def export(self, table: TableSpec, work_dir: Path) -> pa.Table: ...


class _SqlConnector:
    """Shared SQL for the three engines; subclasses supply the dialect bits."""

    engine: str
    dialect: Dialect
    local_zone: str | None
    # Table name to the rendered row its temp hash table was built from.
    _prepared: dict[str, str]

    def relation(self, table: TableSpec) -> str:
        raise NotImplementedError

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        raise NotImplementedError

    def _bucket(self, key: str, lo: int, width: int) -> str:
        return f"(({key} - {lo}) / {width})"

    def _members(self, expr: str, values: Sequence[int]) -> tuple[str, list[Any]]:
        raise NotImplementedError

    def _fetch_value(self, expr: str) -> str:
        return expr

    def _create_hash_table(self, name: str, source_sql: str) -> None:
        self._rows(f"CREATE TEMP TABLE {name} AS {source_sql}")

    def _drop_hash_table(self, name: str) -> None:
        self._rows(f"DROP TABLE IF EXISTS {name}")

    def _hash_table_name(self, table: TableSpec) -> str:
        return f"rowhash_{table.name}"

    def _hashes(self, table: TableSpec, rendered: RenderedTable) -> str:
        """A temp table of (key, h1, h2), computed once per table and rendering.

        Rendering a row canonically is the expensive part (time zone
        conversion, decimal casts, escaping). A dense table is summarised at
        every level over nearly every row, so each row is hashed once, inside
        the engine, and every level groups the small key-and-hash table.
        """
        name = self._hash_table_name(table)
        if self._prepared.get(table.name) != rendered.row:
            self._drop_hash_table(name)
            self._create_hash_table(
                name,
                f"SELECT {table.key}, {rendered.h1} AS h1, {rendered.h2} AS h2 "
                f"FROM {self.relation(table)}",
            )
            self._prepared[table.name] = rendered.row
        return name

    def key_range(self, table: TableSpec) -> tuple[int, int] | None:
        rows = self._rows(f"SELECT MIN({table.key}), MAX({table.key}) FROM {self.relation(table)}")
        low, high = rows[0]
        return None if low is None else (int(low), int(high))

    def summarize(
        self,
        table: TableSpec,
        rendered: RenderedTable,
        lo: int,
        width: int,
        parent_width: int | None,
        parents: Sequence[int],
    ) -> dict[int, Summary]:
        bucket = self._bucket(table.key, lo, width)
        where, params = "", list[Any]()
        if parent_width is not None:
            condition, params = self._members(self._bucket(table.key, lo, parent_width), parents)
            where = f"WHERE {condition}"
        hashes = self._hashes(table, rendered)
        sql = f"""SELECT {bucket} AS bucket, COUNT(*), SUM(h1), SUM(h2)
                  FROM {hashes} {where}
                  GROUP BY {bucket}"""
        return {
            int(b): Summary(int(n), int(s1), int(s2)) for b, n, s1, s2 in self._rows(sql, params)
        }

    def fetch(
        self, table: TableSpec, rendered: RenderedTable, lo: int, width: int, buckets: Sequence[int]
    ) -> dict[int, tuple[str, ...]]:
        condition, params = self._members(self._bucket(table.key, lo, width), buckets)
        values = ", ".join(self._fetch_value(v) for v in rendered.values)
        sql = f"SELECT {table.key}, {values} FROM {self.relation(table)} WHERE {condition}"
        return {int(row[0]): tuple(str(v) for v in row[1:]) for row in self._rows(sql, params)}


# -- DuckDB --------------------------------------------------------------------------


class DuckDBConnector(_SqlConnector):
    """DuckDB tables or views. Used for the Iceberg target and as a legacy store in tests."""

    dialect: Dialect = "duckdb"

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        *,
        engine: str = "duckdb",
        local_zone: str | None = None,
        relations: dict[str, str] | None = None,
        schema: str = LEGACY_SCHEMA,
    ) -> None:
        self.connection = connection
        self._prepared = {}
        self.schema = schema
        self.engine = engine
        self.local_zone = local_zone
        self.relations = relations or {}
        # Canonical timestamps are rendered in the session zone (verified on 1.5.5).
        connection.execute("SET TimeZone = 'UTC'")

    def relation(self, table: TableSpec) -> str:
        return self.relations.get(table.name, f"{self.schema}.{table.name}")

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return self.connection.execute(sql, list(params)).fetchall()

    def _bucket(self, key: str, lo: int, width: int) -> str:
        return f"(({key} - {lo}) // {width})"

    def _members(self, expr: str, values: Sequence[int]) -> tuple[str, list[Any]]:
        return f"{expr} IN (SELECT UNNEST(?::BIGINT[]))", [list(values)]

    def recreate(self, tables: dict[str, pa.Table], specs: Sequence[TableSpec]) -> None:
        self._prepared = {}
        self.connection.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
        self.connection.execute(f"CREATE SCHEMA {self.schema}")
        for spec in specs:
            name = f"{self.schema}.{spec.name}"
            self.connection.execute(spec.ddl("duckdb", name))
            self.connection.register("_incoming", tables[spec.name])
            self.connection.execute(f"INSERT INTO {name} SELECT * FROM _incoming")
            self.connection.unregister("_incoming")

    def export(self, table: TableSpec, work_dir: Path) -> pa.Table:
        columns = ", ".join(c.name for c in table.columns)
        return self.connection.execute(
            f"SELECT {columns} FROM {self.relation(table)} ORDER BY {table.key}"
        ).to_arrow_table()


# -- Postgres ------------------------------------------------------------------------


@dataclass(frozen=True)
class PostgresParams:
    host: str
    port: int
    user: str
    password: str
    dbname: str


class PostgresConnector(_SqlConnector):
    dialect: Dialect = "postgres"

    def __init__(
        self, params: PostgresParams, *, local_zone: str | None, schema: str = LEGACY_SCHEMA
    ) -> None:
        import psycopg

        self.engine = "postgres"
        self._prepared = {}
        self.schema = schema
        self.local_zone = local_zone
        self.connection = psycopg.connect(
            host=params.host, port=params.port, user=params.user, password=params.password,
            dbname=params.dbname, autocommit=True,
        )  # fmt: skip
        self.connection.execute("SET TimeZone = 'UTC'")

    def close(self) -> None:
        self.connection.close()

    def relation(self, table: TableSpec) -> str:
        return f"{self.schema}.{table.name}"

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        cursor = self.connection.execute(sql, list(params))
        return cursor.fetchall() if cursor.description else []

    def _members(self, expr: str, values: Sequence[int]) -> tuple[str, list[Any]]:
        # A hashed semi-join. `= ANY(array)` scans the array for every row.
        return f"{expr} IN (SELECT unnest(%s::bigint[]))", [list(values)]

    def recreate(self, tables: dict[str, pa.Table], specs: Sequence[TableSpec]) -> None:
        self._prepared = {}
        self.connection.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
        self.connection.execute(f"CREATE SCHEMA {self.schema}")
        with tempfile.TemporaryDirectory() as tmp:
            staging = duckdb.connect()
            staging.execute("SET TimeZone = 'UTC'")
            for spec in specs:
                self.connection.execute(spec.ddl("postgres", self.relation(spec)))
                path = (Path(tmp) / f"{spec.name}.csv").as_posix()
                staging.register("_incoming", tables[spec.name])
                # Every non-NULL value is quoted, so '' and NULL stay different.
                staging.execute(
                    f"COPY _incoming TO '{path}' (FORMAT csv, HEADER false, FORCE_QUOTE *)"
                )
                staging.unregister("_incoming")
                copy_sql = f"COPY {self.relation(spec)} FROM STDIN (FORMAT csv)"
                with self.connection.cursor().copy(copy_sql) as copy, open(path, "rb") as source:
                    while chunk := source.read(1 << 20):
                        copy.write(chunk)
            staging.close()
        self.connection.execute("ANALYZE")

    def export(self, table: TableSpec, work_dir: Path) -> pa.Table:
        """The table as the migration job sees it: a CSV export, parsed by DuckDB."""
        columns = ", ".join(c.name for c in table.columns)
        path = work_dir / f"export_{table.name}.csv"
        sql = (
            f"COPY (SELECT {columns} FROM {self.relation(table)} ORDER BY {table.key}) "
            "TO STDOUT (FORMAT csv, FORCE_QUOTE *)"
        )
        with self.connection.cursor().copy(sql) as copy, open(path, "wb") as target:
            for chunk in copy:
                target.write(chunk)
        return read_export_csv(path, table)


def read_export_csv(path: Path, table: TableSpec) -> pa.Table:
    types = {c.name: c.ddl_type("duckdb") for c in table.columns}
    reader = duckdb.connect()
    reader.execute("SET TimeZone = 'UTC'")
    try:
        return reader.execute(
            f"SELECT * FROM read_csv('{path.as_posix()}', header = false, nullstr = '', "
            f"allow_quoted_nulls = false, columns = {types!r})"
        ).to_arrow_table()
    finally:
        reader.close()


# -- SQL Server ----------------------------------------------------------------------


@dataclass(frozen=True)
class SqlServerParams:
    host: str
    port: int
    user: str
    password: str
    database: str


class SqlServerConnector(_SqlConnector):
    dialect: Dialect = "sqlserver"
    insert_batch_rows = 1000  # SQL Server's limit for one VALUES list

    def __init__(
        self, params: SqlServerParams, *, local_zone: str | None, zone_names: dict[str, str]
    ) -> None:
        import pymssql

        self.engine = "sqlserver"
        self._prepared = {}
        self.local_zone = local_zone
        self.zone_names = zone_names
        self.params = params
        self.connection = pymssql.connect(
            server=params.host, port=str(params.port), user=params.user,
            password=params.password, autocommit=True,
        )  # fmt: skip
        self._use_database(create=False)

    def _use_database(self, *, create: bool) -> None:
        from platform_ops.reconcile.schema import SQLSERVER_COLLATION

        cursor = self.connection.cursor()
        database = self.params.database
        if create:
            cursor.execute("USE master")
            cursor.execute(
                f"IF DB_ID('{database}') IS NOT NULL BEGIN "
                f"ALTER DATABASE [{database}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                f"DROP DATABASE [{database}]; END"
            )
            # A UTF-8 default collation makes every VARCHAR, literals included,
            # hash as UTF-8 bytes like the other engines.
            cursor.execute(f"CREATE DATABASE [{database}] COLLATE {SQLSERVER_COLLATION}")
        # USE is resolved when the batch compiles, even inside IF, so check first.
        cursor.execute(f"SELECT DB_ID('{database}')")
        row = cursor.fetchone()
        if row is not None and row[0] is not None:
            cursor.execute(f"USE [{database}]")

    def close(self) -> None:
        self.connection.close()

    def relation(self, table: TableSpec) -> str:
        return f"dbo.{table.name}"

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        cursor = self.connection.cursor()
        cursor.execute(sql, tuple(params) if params else None)
        rows: list[tuple[Any, ...]] = cursor.fetchall()
        return rows

    def _members(self, expr: str, values: Sequence[int]) -> tuple[str, list[Any]]:
        return (
            f"{expr} IN (SELECT CONVERT(BIGINT, value) FROM OPENJSON(%s))",
            [json.dumps(list(values))],
        )

    def _hash_table_name(self, table: TableSpec) -> str:
        return f"#rowhash_{table.name}"

    def _create_hash_table(self, name: str, source_sql: str) -> None:
        select, _, rest = source_sql.partition(" FROM ")
        self.connection.cursor().execute(f"{select} INTO {name} FROM {rest}")

    def _drop_hash_table(self, name: str) -> None:
        self.connection.cursor().execute(
            f"IF OBJECT_ID('tempdb..{name}') IS NOT NULL DROP TABLE {name}"
        )

    def _fetch_value(self, expr: str) -> str:
        # pymssql decodes VARCHAR in the wrong code page; NVARCHAR arrives intact.
        return f"CONVERT(NVARCHAR(MAX), {expr})"

    def recreate(self, tables: dict[str, pa.Table], specs: Sequence[TableSpec]) -> None:
        self._prepared = {}
        self._use_database(create=True)
        cursor = self.connection.cursor()
        for spec in specs:
            cursor.execute(spec.ddl("sqlserver", self.relation(spec)))
            rows = tables[spec.name].to_pylist()
            names = ", ".join(c.name for c in spec.columns)
            for start in range(0, len(rows), self.insert_batch_rows):
                chunk = rows[start : start + self.insert_batch_rows]
                values = ",\n".join(
                    "(" + ", ".join(_mssql_literal(row[c.name], c.kind) for c in spec.columns) + ")"
                    for row in chunk
                )
                cursor.execute(f"INSERT INTO {self.relation(spec)} ({names}) VALUES {values}")

    def export(self, table: TableSpec, work_dir: Path) -> pa.Table:
        selected = []
        for c in table.columns:
            if c.kind == "text":
                selected.append(f"CONVERT(NVARCHAR(MAX), {c.name})")
            elif c.kind == "utc_ts":
                selected.append(f"CONVERT(DATETIME2(6), SWITCHOFFSET({c.name}, '+00:00'))")
            elif c.kind == "decimal":
                # As text, so no float ever sits between the engine and Arrow.
                selected.append(f"CONVERT(VARCHAR(50), {c.name})")
            else:
                selected.append(c.name)
        rows = self._rows(
            f"SELECT {', '.join(selected)} FROM {self.relation(table)} ORDER BY {table.key}"
        )
        return _arrow_from_rows(table, rows)


def _mssql_literal(value: Any, kind: str) -> str:
    if value is None:
        return "NULL"
    if kind == "bool":
        return "1" if value else "0"
    if kind in ("int", "decimal"):
        return str(value)
    if kind == "local_ts":
        return f"'{value:%Y-%m-%d %H:%M:%S.%f}'"
    if kind == "utc_ts":
        return f"'{value:%Y-%m-%d %H:%M:%S.%f}+00:00'"
    return "N'" + str(value).replace("'", "''") + "'"


def _arrow_from_rows(table: TableSpec, rows: list[tuple[Any, ...]]) -> pa.Table:
    """Rows from a DB-API cursor into Arrow, typed like the other exports."""
    from datetime import UTC
    from decimal import Decimal

    schema = arrow_schema(table)
    arrays = []
    for i, column in enumerate(table.columns):
        values = [row[i] for row in rows]
        if column.kind == "decimal":
            values = [None if v is None else Decimal(v) for v in values]
        elif column.kind == "bool":
            values = [None if v is None else bool(v) for v in values]
        elif column.kind == "utc_ts":
            values = [None if v is None else v.replace(tzinfo=UTC) for v in values]
        arrays.append(pa.array(values, type=schema.field(i).type))
    return pa.Table.from_arrays(arrays, schema=schema)


# -- building connectors from settings -------------------------------------------------


def postgres_params(env_file: Path) -> PostgresParams:
    """Connection details from the environment or ``.env``, as Docker Compose uses them."""
    from platform_ops.common.env import env_value

    return PostgresParams(
        host=env_value("POSTGRES_HOST", env_file, "localhost") or "localhost",
        port=int(env_value("POSTGRES_PORT", env_file, "5432") or 5432),
        user=env_value("POSTGRES_USER", env_file, "platform_ops") or "platform_ops",
        password=env_value("POSTGRES_PASSWORD", env_file, "") or "",
        dbname=env_value("POSTGRES_DB", env_file, "legacy") or "legacy",
    )


def sqlserver_params(env_file: Path, database: str = "legacy") -> SqlServerParams:
    from platform_ops.common.env import env_value

    return SqlServerParams(
        host=env_value("MSSQL_HOST", env_file, "localhost") or "localhost",
        port=int(env_value("MSSQL_PORT", env_file, "1433") or 1433),
        user="sa",
        password=env_value("MSSQL_SA_PASSWORD", env_file, "") or "",
        database=database,
    )
