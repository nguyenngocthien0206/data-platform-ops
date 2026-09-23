"""DuckDB connections.

Module outputs (cost facts, incidents, diff results) are tables in the ``ops``
schema, never values that live only in a Python process. Routing every
connection through here means the schema exists before anything tries to write
to it, and there is one place to change if the warehouse ever moves.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
