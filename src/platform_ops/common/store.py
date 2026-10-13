"""The toolkit's own results, in Postgres (version 2.0).

Every module writes what it found (ownership, lineage, ingestion watermarks,
and from later phases cost facts, incidents and diff results) as tables in one
schema of one database, ``platform_ops`` by default, on the same server as the
company's legacy database but separate from it. Postgres gives the writes real
transactions, the dashboards one place to read, and Phase 14's live incident
store a home that already exists.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.servers import connect_postgres


def ensure_ops_database(settings: Settings) -> None:
    """Create the ops database and schema if they are missing. Safe to repeat."""
    database = settings.company.ops_database
    with connect_postgres(settings) as connection:
        connection.autocommit = True
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", [database]
        ).fetchone()
        if exists is None:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    with connect_postgres(settings, database) as connection:
        schema = settings.company.name(settings.company.ops_schema)
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        connection.commit()


@contextmanager
def open_store(settings: Settings) -> Iterator[psycopg.Connection[Any]]:
    """A connection to the ops database, its schema in place. Commits on success."""
    ensure_ops_database(settings)
    connection = connect_postgres(settings, settings.company.ops_database)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def ops_table(settings: Settings, name: str) -> sql.Composed:
    """``"<namespace>ops".<name>`` as a safely quoted identifier."""
    return sql.SQL("{}.{}").format(
        sql.Identifier(settings.company.name(settings.company.ops_schema)), sql.Identifier(name)
    )


def replace_ops_table(
    connection: psycopg.Connection[Any],
    settings: Settings,
    name: str,
    ddl: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> int:
    """Drop and recreate ``ops.<name>`` and fill it, inside the caller's transaction.

    Readers never see a half-written table: Postgres DDL is transactional, so
    the old table stays visible until the caller commits.
    """
    table = ops_table(settings, name)
    connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))
    connection.execute(sql.SQL("CREATE TABLE {} ({})").format(table, sql.SQL(ddl)))
    if rows:
        with connection.cursor().copy(
            sql.SQL("COPY {} ({}) FROM STDIN").format(
                table, sql.SQL(", ").join(sql.Identifier(c) for c in columns)
            )
        ) as copy:
            for row in rows:
                copy.write_row(row)
    return len(rows)
