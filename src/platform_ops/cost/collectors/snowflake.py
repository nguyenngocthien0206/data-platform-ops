"""Snowflake query history, from ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY``.

Column names and types follow Snowflake's documentation of the view
(docs.snowflake.com/en/sql-reference/account-usage/query_history, checked
2026-09-25). Snowflake returns column names in upper case; a recorded row is
``SELECT <these columns>`` as JSON, with TIMESTAMP_LTZ values in ISO 8601 with
their offset. ``TOTAL_ELAPSED_TIME`` is in milliseconds.

Mapping onto :class:`QueryRecord`:

- ``USER_NAME`` is the principal: a dbt or BI service user, or a person;
- ``node_id`` comes from the JSON comment dbt appends to every query, or for a
  dashboard query from ``QUERY_TAG`` (``{"dashboard": "<exposure name>"}``);
- ``run_id`` is ``dbt_invocation_id`` from ``QUERY_TAG`` when present, else
  the query id;
- ``bytes_scanned`` is ``BYTES_SCANNED``.

Skipped, and counted: queries whose ``EXECUTION_STATUS`` is not ``success``
(``fail`` or ``incident``). A query answered from the result cache scans zero
bytes and is kept: it still ran on the account and cost cloud-services time.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from platform_ops.cost.collect import QueryRecord
from platform_ops.cost.collectors._common import (
    NULLABLE,
    Principals,
    Schema,
    Skipped,
    read_jsonl,
    utc_naive,
)
from platform_ops.cost.sql_parse import node_id_from_comment

QUERY_HISTORY_SCHEMA: Schema = {
    "QUERY_ID": (str,),
    "QUERY_TEXT": (str,),
    "DATABASE_NAME": (str, NULLABLE),
    "SCHEMA_NAME": (str, NULLABLE),
    "QUERY_TYPE": (str,),
    "SESSION_ID": (int,),
    "USER_NAME": (str,),
    "ROLE_NAME": (str, NULLABLE),
    "WAREHOUSE_NAME": (str, NULLABLE),
    "WAREHOUSE_SIZE": (str, NULLABLE),
    "QUERY_TAG": (str,),
    "EXECUTION_STATUS": (str,),
    "ERROR_CODE": (int, NULLABLE),
    "START_TIME": (str,),
    "END_TIME": (str,),
    "TOTAL_ELAPSED_TIME": (int,),
    "BYTES_SCANNED": (int,),
    "ROWS_PRODUCED": (int, NULLABLE),
    "CREDITS_USED_CLOUD_SERVICES": (float, int),
    "EXECUTION_TIME": (int,),
    "PERCENTAGE_SCANNED_FROM_CACHE": (float, int),
}


def _tag(row: Mapping[str, Any]) -> dict[str, str]:
    """``QUERY_TAG`` as a dict when it holds JSON, as dbt and BI tools usually set it."""
    try:
        value = json.loads(row.get("QUERY_TAG") or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


class SnowflakeQueryHistoryCollector:
    """A :class:`~platform_ops.cost.collect.QueryCollector` over recorded ``QUERY_HISTORY`` rows."""

    def __init__(
        self, rows: Iterable[Mapping[str, Any]], principals: Principals, dbt_project: str
    ) -> None:
        self.rows = list(rows)
        self.principals = principals
        self.dbt_project = dbt_project
        self.skipped = Skipped()

    @classmethod
    def from_jsonl(
        cls, path: Path, principals: Principals, dbt_project: str
    ) -> SnowflakeQueryHistoryCollector:
        return cls(read_jsonl(path), principals, dbt_project)

    def records(self) -> Iterator[QueryRecord]:
        self.skipped = Skipped()
        for row in self.rows:
            status = str(row["EXECUTION_STATUS"]).lower()
            if status != "success":
                self.skipped.add(f"status {status}")
                continue
            tag = _tag(row)
            principal = str(row["USER_NAME"])
            node_id = node_id_from_comment(row["QUERY_TEXT"])
            if node_id is None and "dashboard" in tag:
                node_id = f"exposure.{self.dbt_project}.{tag['dashboard']}"
            yield QueryRecord(
                query_id=f"snowflake:{row['QUERY_ID']}",
                run_id=tag.get("dbt_invocation_id", str(row["QUERY_ID"])),
                actor=self.principals.actor(principal),
                actor_type=self.principals.actor_type(principal),
                node_id=node_id,
                started_at=utc_naive(row["START_TIME"]),
                sql_text=str(row["QUERY_TEXT"]),
                wallclock_ms=float(row["TOTAL_ELAPSED_TIME"]),
                bytes_scanned=int(row["BYTES_SCANNED"]),
            )
