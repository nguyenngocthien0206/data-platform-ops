"""Bytes scanned and compute time for every collected query (ADR 0005).

Bytes: the logical size of every column a query read, taken from the size
snapshot that was current when the query ran. The whole column counts, whatever
the filter, the way BigQuery bills: predicate pushdown and row-group pruning
make the real scan smaller, which is the known overestimate the accuracy report
measures. A query whose columns could not be resolved is billed for every column
of the tables it read.

Compute time: a fixed per-query overhead plus bytes divided by a configured
throughput. It is a model, not a measurement, because measured wall-clock time
changes on every run and priced numbers must not.

Everything here is SQL over ``ops`` tables, and all arithmetic is on integers,
so the results are exact and identical on every run.
"""

from __future__ import annotations

import duckdb

from platform_ops.common.config import Settings
from platform_ops.common.db import OPS_SCHEMA


def estimate(connection: duckdb.DuckDBPyConnection, settings: Settings) -> int:
    """Write ``ops.query_table_bytes`` and ``ops.query_estimates``; return the query count."""
    overhead = settings.cost.per_query_overhead_ms
    throughput = settings.cost.throughput_bytes_per_second
    connection.execute(
        f"""
        CREATE OR REPLACE TABLE {OPS_SCHEMA}.query_table_bytes AS
        WITH used AS (
            SELECT qt.query_id, qt.table_name, qt.columns, ql.started_at, ql.columns_resolved
            FROM {OPS_SCHEMA}.query_tables qt
            JOIN {OPS_SCHEMA}.query_log ql USING (query_id)
        ),
        known_columns AS (
            SELECT DISTINCT table_name, column_name FROM {OPS_SCHEMA}.table_sizes
        ),
        billed_columns AS (
            SELECT query_id, table_name, started_at, unnest(columns) AS column_name
            FROM used WHERE columns_resolved
            UNION ALL
            SELECT u.query_id, u.table_name, u.started_at, k.column_name
            FROM used u JOIN known_columns k USING (table_name)
            WHERE NOT u.columns_resolved
        ),
        sized AS (
            SELECT b.query_id, b.table_name, s.logical_bytes
            FROM billed_columns b
            ASOF LEFT JOIN {OPS_SCHEMA}.table_sizes s
              ON s.table_name = b.table_name
             AND s.column_name = b.column_name
             AND b.started_at >= s.snapshot_at
        ),
        per_table AS (
            SELECT query_id, table_name, sum(coalesce(logical_bytes, 0))::BIGINT AS bytes
            FROM sized GROUP BY query_id, table_name
        )
        -- Tables read without any column (count(*)) still appear, at zero bytes.
        SELECT u.query_id, u.table_name, coalesce(p.bytes, 0)::BIGINT AS bytes
        FROM used u
        LEFT JOIN per_table p USING (query_id, table_name)
        ORDER BY u.query_id, u.table_name
        """
    )
    connection.execute(
        f"""
        CREATE OR REPLACE TABLE {OPS_SCHEMA}.query_estimates AS
        SELECT
            ql.query_id,
            ql.actor_type,
            ql.started_at,
            coalesce(sum(qtb.bytes), 0)::BIGINT AS bytes_scanned,
            count(qtb.table_name)::INTEGER AS tables_referenced,
            ({overhead} + (coalesce(sum(qtb.bytes), 0) * 1000) // {throughput})::BIGINT
                AS modeled_ms
        FROM {OPS_SCHEMA}.query_log ql
        LEFT JOIN {OPS_SCHEMA}.query_table_bytes qtb USING (query_id)
        GROUP BY ql.query_id, ql.actor_type, ql.started_at
        ORDER BY ql.query_id
        """
    )
    row = connection.execute(f"SELECT count(*) FROM {OPS_SCHEMA}.query_estimates").fetchone()
    return int(row[0]) if row else 0
