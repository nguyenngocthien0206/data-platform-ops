"""Watching raw sources to see which ones only ever grow.

A model can safely become incremental only if its sources are append-only: new
rows arrive, existing rows never change. That is observed here rather than
assumed. At each snapshot a source gets a fingerprint of every row's key and
``_loaded_at``. At the next snapshot, the rows that existed last time are
fingerprinted again. If a row was updated (its ``_loaded_at`` moved forward) or
deleted, the two fingerprints disagree and the source is not append-only for
that day.
"""

from __future__ import annotations

from datetime import datetime

import duckdb

from platform_ops.common.db import OPS_SCHEMA, RAW_SCHEMA


def observe(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    key: str,
    at: datetime,
    previous: tuple[datetime, int] | None,
) -> tuple[int, int, bool | None]:
    """Row count, fingerprint of all rows, and whether only new rows arrived.

    ``previous`` is the last snapshot's time and fingerprint. The first snapshot
    of a table has nothing to compare with, so its append-only flag is None.
    """
    since = previous[0] if previous else at
    row = connection.execute(
        f"""SELECT count(*),
                   coalesce(bit_xor(hash({key}, _loaded_at)), 0),
                   coalesce(bit_xor(hash({key}, _loaded_at)) FILTER (WHERE _loaded_at <= ?), 0)
            FROM {RAW_SCHEMA}.{table}""",
        [since],
    ).fetchone()
    assert row is not None
    count, fingerprint, old_rows = int(row[0]), int(row[1]), int(row[2])
    append_only = None if previous is None else old_rows == previous[1]
    connection.execute(
        f"INSERT INTO {OPS_SCHEMA}.source_growth VALUES (?, ?, ?, ?, ?)",
        [at, f"{RAW_SCHEMA}.{table}", count, fingerprint, append_only],
    )
    return count, fingerprint, append_only


def append_only_sources(connection: duckdb.DuckDBPyConnection) -> dict[str, bool]:
    """Per raw source: True if it was append-only on every observed day."""
    rows = connection.execute(
        f"""SELECT table_name, bool_and(coalesce(append_only, true))
            FROM {OPS_SCHEMA}.source_growth GROUP BY table_name ORDER BY table_name"""
    ).fetchall()
    return {str(table): bool(flag) for table, flag in rows}
