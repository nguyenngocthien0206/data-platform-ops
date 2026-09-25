"""Cloud-ready cost collectors (SPEC Phase 5).

Each implements :class:`platform_ops.cost.collect.QueryCollector` over a
vendor's query history, so pricing, attribution and recommendations run on a
real warehouse's history without changing. They are exercised only by contract
tests against recorded rows in ``tests/fixtures/``: no network, no account.
"""

from platform_ops.cost.collectors._common import Principals, Skipped, schema_problems
from platform_ops.cost.collectors.bigquery import JOBS_SCHEMA, BigQueryJobsCollector
from platform_ops.cost.collectors.snowflake import (
    QUERY_HISTORY_SCHEMA,
    SnowflakeQueryHistoryCollector,
)

__all__ = [
    "JOBS_SCHEMA",
    "QUERY_HISTORY_SCHEMA",
    "BigQueryJobsCollector",
    "Principals",
    "Skipped",
    "SnowflakeQueryHistoryCollector",
    "schema_problems",
]
