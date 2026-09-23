from __future__ import annotations

from pathlib import Path

from platform_ops.common.config import Settings
from platform_ops.common.db import (
    OPS_SCHEMA,
    RAW_SCHEMA,
    connect,
    database_path,
    open_connection,
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
