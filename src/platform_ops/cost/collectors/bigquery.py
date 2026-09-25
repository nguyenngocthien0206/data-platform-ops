"""BigQuery query history, from ``INFORMATION_SCHEMA.JOBS``.

Column names and types follow Google's documentation of the ``JOBS`` view
(docs.cloud.google.com/bigquery/docs/information-schema-jobs, checked
2026-09-25). Only the columns this collector reads are declared; a recorded
row is the JSON a client library returns for ``SELECT <these columns>``, with
TIMESTAMP values in ISO 8601.

Mapping onto :class:`QueryRecord`:

- ``user_email`` is the principal: a dbt or BI service account, or a person;
- ``node_id`` comes from the JSON comment dbt appends to every query
  (``query-comment`` in ``dbt_project.yml``), or for a dashboard query from its
  ``dashboard`` job label, naming the exposure;
- ``run_id`` is dbt's ``dbt_invocation_id`` label when present, else the job id;
- ``bytes_scanned`` is ``total_bytes_billed``, what on-demand pricing charges.

Skipped, and counted: jobs that are not queries, not ``DONE``, failed
(``error_result`` set), served from cache (``cache_hit``, nothing billed), and
``SCRIPT`` parents, whose child jobs carry the actual statements.
"""

from __future__ import annotations

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

JOBS_SCHEMA: Schema = {
    "job_id": (str,),
    "creation_time": (str,),
    "start_time": (str, NULLABLE),
    "end_time": (str, NULLABLE),
    "project_id": (str,),
    "user_email": (str,),
    "job_type": (str,),
    "statement_type": (str, NULLABLE),
    "query": (str,),
    "state": (str,),
    "total_bytes_processed": (int, NULLABLE),
    "total_bytes_billed": (int, NULLABLE),
    "total_slot_ms": (int, NULLABLE),
    "cache_hit": (bool, NULLABLE),
    "destination_table": (dict, NULLABLE),  # RECORD: project_id, dataset_id, table_id
    "referenced_tables": (list,),  # REPEATED RECORD: project_id, dataset_id, table_id
    "labels": (list,),  # REPEATED RECORD: key, value
    "error_result": (dict, NULLABLE),  # RECORD: reason, location, message
    "parent_job_id": (str, NULLABLE),
    "reservation_id": (str, NULLABLE),
}


def _labels(row: Mapping[str, Any]) -> dict[str, str]:
    return {str(label["key"]): str(label["value"]) for label in row.get("labels") or []}


class BigQueryJobsCollector:
    """A :class:`~platform_ops.cost.collect.QueryCollector` over recorded ``JOBS`` rows."""

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
    ) -> BigQueryJobsCollector:
        return cls(read_jsonl(path), principals, dbt_project)

    def _skip_reason(self, row: Mapping[str, Any]) -> str | None:
        if row["job_type"] != "QUERY":
            return "not a query"
        if row["state"] != "DONE":
            return "not finished"
        if row.get("error_result"):
            return "failed"
        if row.get("cache_hit"):
            return "served from cache"
        if row.get("statement_type") == "SCRIPT":
            return "script parent"
        return None

    def records(self) -> Iterator[QueryRecord]:
        self.skipped = Skipped()
        for row in self.rows:
            reason = self._skip_reason(row)
            if reason is not None:
                self.skipped.add(reason)
                continue
            labels = _labels(row)
            principal = str(row["user_email"])
            node_id = node_id_from_comment(row["query"])
            if node_id is None and "dashboard" in labels:
                node_id = f"exposure.{self.dbt_project}.{labels['dashboard']}"
            started = utc_naive(row["start_time"] or row["creation_time"])
            wallclock_ms = None
            if row.get("start_time") and row.get("end_time"):
                ended = utc_naive(row["end_time"])
                wallclock_ms = (ended - started).total_seconds() * 1000
            yield QueryRecord(
                query_id=f"bigquery:{row['job_id']}",
                run_id=labels.get("dbt_invocation_id", str(row["job_id"])),
                actor=self.principals.actor(principal),
                actor_type=self.principals.actor_type(principal),
                node_id=node_id,
                started_at=started,
                sql_text=str(row["query"]),
                wallclock_ms=wallclock_ms,
                bytes_scanned=row.get("total_bytes_billed"),
            )
