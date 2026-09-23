"""DuckDB connections.

Module outputs (cost facts, incidents, diff results) are tables in the ``ops``
schema, never values that live only in a Python process. Routing every
connection through here means the schema exists before anything tries to write
to it, and there is one place to change if the warehouse ever moves.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

from platform_ops.common.config import Settings

# Schemas the toolkit owns. `raw` holds generated source data (Phase 1), `ops`
# holds everything the three modules produce.
OPS_SCHEMA = "ops"
RAW_SCHEMA = "raw"


def database_path(settings: Settings) -> Path:
    """Absolute path of the DuckDB file described by the settings."""
    return settings.resolve(settings.paths.duckdb)


def connect(
    settings: Settings | None = None,
    *,
    path: Path | str | None = None,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Open a connection with the project schemas in place.

    Pass ``path=":memory:"`` for tests that do not need a file on disk.
    """
    if path is None:
        if settings is None:
            raise ValueError("pass either settings or an explicit path")
        target = database_path(settings)
    else:
        target = Path(path) if path != ":memory:" else Path(":memory:")

    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect(str(target), read_only=read_only)
    if not read_only:
        ensure_schemas(connection)
    return connection


def ensure_schemas(connection: duckdb.DuckDBPyConnection) -> None:
    """Create the project schemas if they are missing. Safe to call repeatedly."""
    for schema in (RAW_SCHEMA, OPS_SCHEMA):
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


_INSERT_CHUNK_ROWS = 1000


def insert_rows(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> int:
    """Bulk-insert Python rows as multi-row ``VALUES`` statements.

    Measured on DuckDB 1.5.5 (400 rows): ``executemany`` 0.17 s, because it runs
    one statement per row; the Arrow path about 0.8 s, almost all of it a fixed
    cost per call; one parameterised multi-row ``VALUES`` statement 0.035 s.
    Rows go in chunks so a statement never carries an unbounded parameter list.
    """
    column_list = ", ".join(columns)
    width = len(columns)
    for start in range(0, len(rows), _INSERT_CHUNK_ROWS):
        chunk = rows[start : start + _INSERT_CHUNK_ROWS]
        placeholders = ", ".join(["(" + ", ".join(["?"] * width) + ")"] * len(chunk))
        params = [value for row in chunk for value in row]
        connection.execute(f"INSERT INTO {table} ({column_list}) VALUES {placeholders}", params)
    return len(rows)


# Connections that currently have a transaction open through this module.
# DuckDB aborts the outer transaction if begin() is called inside it, so nesting
# is detected here instead of by trying.
_OPEN_TRANSACTIONS: set[int] = set()


def begin(connection: duckdb.DuckDBPyConnection) -> None:
    """Open a transaction that the caller will end with :func:`commit`."""
    connection.begin()
    _OPEN_TRANSACTIONS.add(id(connection))


def commit(connection: duckdb.DuckDBPyConnection) -> None:
    _OPEN_TRANSACTIONS.discard(id(connection))
    connection.commit()


def rollback(connection: duckdb.DuckDBPyConnection) -> None:
    _OPEN_TRANSACTIONS.discard(id(connection))
    connection.rollback()


def in_transaction(connection: duckdb.DuckDBPyConnection) -> bool:
    return id(connection) in _OPEN_TRANSACTIONS


@contextmanager
def transaction(connection: duckdb.DuckDBPyConnection) -> Iterator[None]:
    """Run a block in one transaction: commit on success, roll back on error.

    Multi-statement writes to ``ops`` tables go through here, so readers see
    either the old table or the new one, never something in between. Inside an
    outer transaction the block simply joins it, and the outer one decides.

    Commits are also the expensive part of a write: each one forces DuckDB's
    write-ahead log to disk, which took 0.3 to 0.9 s per commit on the
    development laptop. Batching many writes into one transaction is the main
    lever on simulation speed.
    """
    if in_transaction(connection):
        yield
        return
    begin(connection)
    try:
        yield
    except BaseException:
        rollback(connection)
        raise
    commit(connection)


@contextmanager
def open_connection(
    settings: Settings | None = None,
    *,
    path: Path | str | None = None,
    read_only: bool = False,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Context-managed version of :func:`connect`."""
    connection = connect(settings, path=path, read_only=read_only)
    try:
        yield connection
    finally:
        connection.close()
