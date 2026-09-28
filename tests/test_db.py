from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from platform_ops.common.config import Settings
from platform_ops.common.db import (
    OPS_SCHEMA,
    RAW_SCHEMA,
    connect,
    database_path,
    open_connection,
    replace_table,
    replace_table_from_arrow,
)


def _schemas(connection: object) -> set[str]:
    rows = connection.execute(  # type: ignore[attr-defined]
        "SELECT schema_name FROM information_schema.schemata"
    ).fetchall()
    return {row[0] for row in rows}


def test_connect_creates_the_file_and_the_project_schemas(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "warehouse.duckdb"
    with open_connection(path=target) as connection:
        assert {OPS_SCHEMA, RAW_SCHEMA} <= _schemas(connection)
    assert target.exists()


def test_schema_creation_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "warehouse.duckdb"
    with open_connection(path=target) as first:
        first.execute(f"CREATE TABLE {OPS_SCHEMA}.marker (id INTEGER)")
    with open_connection(path=target) as second:
        assert {OPS_SCHEMA, RAW_SCHEMA} <= _schemas(second)
        count = second.execute(f"SELECT count(*) FROM {OPS_SCHEMA}.marker").fetchone()
        assert count is not None and count[0] == 0


def test_ops_tables_survive_reconnection(tmp_path: Path) -> None:
    """Module outputs are tables on disk, not values in a Python process."""
    target = tmp_path / "warehouse.duckdb"
    with open_connection(path=target) as first:
        first.execute(f"CREATE TABLE {OPS_SCHEMA}.fact (n INTEGER)")
        first.execute(f"INSERT INTO {OPS_SCHEMA}.fact VALUES (1), (2), (3)")
    with open_connection(path=target) as second:
        total = second.execute(f"SELECT sum(n) FROM {OPS_SCHEMA}.fact").fetchone()
        assert total is not None and total[0] == 6


def test_in_memory_connection_works_without_touching_disk() -> None:
    connection = connect(path=":memory:")
    try:
        assert {OPS_SCHEMA, RAW_SCHEMA} <= _schemas(connection)
    finally:
        connection.close()


def test_database_path_comes_from_settings(settings: Settings) -> None:
    path = database_path(settings)
    assert path.is_absolute()
    assert path.name == "test.duckdb"


def test_replace_table_swaps_the_whole_table() -> None:
    connection = connect(path=":memory:")
    ddl = "k INTEGER, v VARCHAR"
    assert replace_table(connection, "t", ddl, ("k", "v"), [(1, "a"), (2, "b")]) == 2
    assert replace_table(connection, "t", ddl, ("k", "v"), [(3, "c")]) == 1
    assert connection.execute("SELECT k, v FROM ops.t").fetchall() == [(3, "c")]


def test_a_failed_replacement_leaves_the_old_table() -> None:
    connection = connect(path=":memory:")
    replace_table(connection, "t", "k INTEGER NOT NULL", ("k",), [(1,)])
    with pytest.raises(duckdb.Error):
        replace_table(connection, "t", "k INTEGER NOT NULL", ("k",), [(2,), (None,)])
    assert connection.execute("SELECT k FROM ops.t").fetchall() == [(1,)]


def test_replace_table_from_arrow() -> None:
    connection = connect(path=":memory:")
    data = pa.table({"k": [1, 2, 3]})
    assert replace_table_from_arrow(connection, "t", data) == 3
    assert connection.execute("SELECT sum(k) FROM ops.t").fetchone() == (6,)
