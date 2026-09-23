"""Uncompressed ("logical") column sizes, the basis of the bytes-scanned estimate.

DuckDB's compressed size is not stable: two runs over identical data can pick
different codecs for the same column. So the estimate uses the size the data
would have uncompressed, which depends only on its content (ADR 0005):

- fixed-width types count their width for every non-null value;
- strings and blobs count their actual length in bytes;
- anything else (lists, structs) counts the length of its text form.

This is also roughly how BigQuery bills: by the logical size of the columns a
query references, regardless of how the data is stored.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

import duckdb

from platform_ops.common.db import OPS_SCHEMA, insert_rows, transaction

FIXED_WIDTH: dict[str, int] = {
    "BOOLEAN": 1,
    "TINYINT": 1,
    "UTINYINT": 1,
    "SMALLINT": 2,
    "USMALLINT": 2,
    "INTEGER": 4,
    "UINTEGER": 4,
    "BIGINT": 8,
    "UBIGINT": 8,
    "HUGEINT": 16,
    "UHUGEINT": 16,
    "FLOAT": 4,
    "DOUBLE": 8,
    "DATE": 4,
    "TIME": 8,
    "TIMESTAMP": 8,
    "TIMESTAMP WITH TIME ZONE": 8,
    "INTERVAL": 16,
    "UUID": 16,
}
_DECIMAL = re.compile(r"DECIMAL\((\d+),\s*\d+\)")


def width_of(data_type: str) -> int | None:
    """Bytes per value for a fixed-width type, or None for variable width."""
    upper = data_type.upper()
    if upper in FIXED_WIDTH:
        return FIXED_WIDTH[upper]
    match = _DECIMAL.fullmatch(upper)
    if match:
        precision = int(match.group(1))
        return 2 if precision <= 4 else 4 if precision <= 9 else 8 if precision <= 18 else 16
    return None


def _size_expression(column: str, data_type: str) -> str:
    # DuckDB: strlen() is the byte length of a VARCHAR (length() counts
    # characters); octet_length() only accepts BLOB.
    quoted = f'"{column}"'
    width = width_of(data_type)
    upper = data_type.upper()
    if width is not None:
        return f"count({quoted}) * {width}"
    if upper == "VARCHAR":
        return f"coalesce(sum(strlen({quoted})), 0)"
    if upper == "BLOB":
        return f"coalesce(sum(octet_length({quoted})), 0)"
    return f"coalesce(sum(strlen(CAST({quoted} AS VARCHAR))), 0)"


def columns_of(connection: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    schema, _, name = table.partition(".")
    rows = connection.execute(
        """SELECT column_name, data_type FROM duckdb_columns()
           WHERE schema_name = ? AND table_name = ? ORDER BY column_index""",
        [schema, name],
    ).fetchall()
    return [(str(column), str(data_type)) for column, data_type in rows]


def measure(
    connection: duckdb.DuckDBPyConnection, table: str, where: str | None = None
) -> tuple[int, dict[str, tuple[str, int]]]:
    """Row count and ``{column: (type, logical bytes)}``, optionally for a subset of rows."""
    columns = columns_of(connection, table)
    expressions = ", ".join(_size_expression(c, t) for c, t in columns)
    clause = f" WHERE {where}" if where else ""
    row = connection.execute(f"SELECT count(*), {expressions} FROM {table}{clause}").fetchone()
    assert row is not None
    sizes = {c: (t, int(b)) for (c, t), b in zip(columns, row[1:], strict=True)}
    return int(row[0]), sizes


def record_snapshot(
    connection: duckdb.DuckDBPyConnection,
    snapshot_at: datetime,
    measured: Iterable[tuple[str, int, dict[str, tuple[str, int]]]],
) -> int:
    """Append one snapshot of ``(table, row_count, sizes)`` to ``ops.table_sizes``."""
    rows = [
        (snapshot_at, table, column, data_type, row_count, logical_bytes)
        for table, row_count, sizes in measured
        for column, (data_type, logical_bytes) in sizes.items()
    ]
    columns = (
        "snapshot_at",
        "table_name",
        "column_name",
        "data_type",
        "row_count",
        "logical_bytes",
    )
    with transaction(connection):
        insert_rows(connection, f"{OPS_SCHEMA}.table_sizes", columns, rows)
    return len(rows)


def latest(
    connection: duckdb.DuckDBPyConnection, table: str
) -> tuple[int, dict[str, tuple[str, int]]] | None:
    """The most recent snapshot of ``table``, used to add a day's growth to it."""
    rows = connection.execute(
        f"""SELECT column_name, data_type, row_count, logical_bytes
            FROM {OPS_SCHEMA}.table_sizes
            WHERE table_name = ?
              AND snapshot_at = (SELECT max(snapshot_at) FROM {OPS_SCHEMA}.table_sizes
                                 WHERE table_name = ?)""",
        [table, table],
    ).fetchall()
    if not rows:
        return None
    return int(rows[0][2]), {str(c): (str(t), int(b)) for c, t, _, b in rows}


def add(
    base: tuple[int, dict[str, tuple[str, int]]], delta: tuple[int, dict[str, tuple[str, int]]]
) -> tuple[int, dict[str, tuple[str, int]]]:
    """Sizes after appending ``delta`` rows to a table measured as ``base``.

    Exact for append-only tables, because a logical size is a plain sum over rows.
    """
    base_rows, base_sizes = base
    delta_rows, delta_sizes = delta
    merged = {
        column: (data_type, logical_bytes + delta_sizes.get(column, (data_type, 0))[1])
        for column, (data_type, logical_bytes) in base_sizes.items()
    }
    return base_rows + delta_rows, merged
