"""The ops store in Postgres (version 2.0): created on demand, replaced atomically.

Runs in its own namespace on the Postgres server (``make up``); skips with the
reason when Postgres does not answer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from psycopg import sql

from platform_ops.common.config import Settings, load_settings
from platform_ops.common.servers import connect_postgres
from platform_ops.common.store import open_store, ops_table, replace_ops_table

pytestmark = pytest.mark.integration

DDL = "name text NOT NULL, value bigint"


@pytest.fixture
def settings(tmp_path: Path, isolated_config: Callable[..., Path]) -> Iterator[Settings]:
    loaded = load_settings(isolated_config(tmp_path, 0.01, namespace="t_store_"))
    try:
        connect_postgres(loaded).close()
    except Exception as error:
        pytest.skip(f"Postgres not reachable ({type(error).__name__}); run `make up`")
    yield loaded
    with connect_postgres(loaded, loaded.company.ops_database) as connection:
        schema = loaded.company.name(loaded.company.ops_schema)
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
        connection.commit()


def _rows(settings: Settings) -> list[tuple[str, int]]:
    with open_store(settings) as connection:
        return [
            (str(name), int(value))
            for name, value in connection.execute(
                sql.SQL("SELECT name, value FROM {} ORDER BY name").format(
                    ops_table(settings, "example")
                )
            ).fetchall()
        ]


def test_the_store_is_created_in_its_own_database_and_schema(settings: Settings) -> None:
    with open_store(settings) as connection:
        database, schema = connection.execute(
            "SELECT current_database(), "
            "%s IN (SELECT schema_name FROM information_schema.schemata)",
            [settings.company.name(settings.company.ops_schema)],
        ).fetchone() or (None, None)
    assert (database, schema) == ("platform_ops", True)


def test_a_table_is_replaced_whole(settings: Settings) -> None:
    columns = ["name", "value"]
    with open_store(settings) as connection:
        written = replace_ops_table(connection, settings, "example", DDL, columns, [("a", 1)])
    assert written == 1
    with open_store(settings) as connection:
        replace_ops_table(connection, settings, "example", DDL, columns, [("b", 2), ("c", 3)])
    assert _rows(settings) == [("b", 2), ("c", 3)]


def test_a_failed_write_leaves_the_previous_table(settings: Settings) -> None:
    columns = ["name", "value"]
    with open_store(settings) as connection:
        replace_ops_table(connection, settings, "example", DDL, columns, [("kept", 1)])
    with pytest.raises(RuntimeError), open_store(settings) as connection:
        replace_ops_table(connection, settings, "example", DDL, columns, [("lost", 2)])
        raise RuntimeError("the writer failed after replacing the table")
    assert _rows(settings) == [("kept", 1)]
