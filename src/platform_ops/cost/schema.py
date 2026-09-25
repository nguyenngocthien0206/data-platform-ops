"""The ``ops`` tables the cost module reads and writes.

Collection (``make simulate``) fills the first group. Pricing, attribution and
the report (``make cost``) derive the second group from it, and can be rerun on
the same collected workload without simulating again.
"""

from __future__ import annotations

import duckdb

from platform_ops.common.db import OPS_SCHEMA

COLLECTION_TABLES: dict[str, str] = {
    # One row per query: dbt builds and tests, dashboard refreshes, ad hoc SQL.
    "query_log": """
        query_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        actor VARCHAR NOT NULL,
        actor_type VARCHAR NOT NULL,
        node_id VARCHAR,
        started_at TIMESTAMP NOT NULL,
        sql_text VARCHAR NOT NULL,
        replayed BOOLEAN NOT NULL,
        wallclock_ms DOUBLE,
        rows_scanned BIGINT,
        -- Bytes the vendor measured (BigQuery total_bytes_billed, Snowflake
        -- BYTES_SCANNED). NULL for the local collectors, which are estimated.
        bytes_scanned BIGINT,
        writes VARCHAR,
        select_star BOOLEAN NOT NULL,
        columns_resolved BOOLEAN NOT NULL,
        parse_error VARCHAR""",
    # The tables and columns each query read, from sql_parse.
    "query_tables": """
        query_id VARCHAR NOT NULL,
        table_name VARCHAR NOT NULL,
        columns VARCHAR[] NOT NULL,
        filter_columns VARCHAR[] NOT NULL""",
    # Uncompressed size of every column over time (ADR 0005).
    "table_sizes": """
        snapshot_at TIMESTAMP NOT NULL,
        table_name VARCHAR NOT NULL,
        column_name VARCHAR NOT NULL,
        data_type VARCHAR NOT NULL,
        row_count BIGINT NOT NULL,
        logical_bytes BIGINT NOT NULL""",
    # Whether each raw source only gained rows since the previous snapshot.
    "source_growth": """
        snapshot_at TIMESTAMP NOT NULL,
        table_name VARCHAR NOT NULL,
        row_count BIGINT NOT NULL,
        old_rows_fingerprint UBIGINT,
        append_only BOOLEAN""",
}


def reset_collection_tables(connection: duckdb.DuckDBPyConnection) -> None:
    """Drop and recreate the collection tables, so a simulation starts clean."""
    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {OPS_SCHEMA}")
    for name, ddl in COLLECTION_TABLES.items():
        connection.execute(f"CREATE OR REPLACE TABLE {OPS_SCHEMA}.{name} ({ddl})")
