"""Contract tests for the cloud cost collectors, against recorded vendor rows.

``tests/fixtures/`` holds a few hundred rows of BigQuery ``JOBS`` and Snowflake
``QUERY_HISTORY`` in each vendor's documented columns and types, written by
``scripts/make_vendor_fixtures.py`` from the simulated company's query log.
No network, no account: these tests pin the contract a real history must meet.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from platform_ops.cost.collect import QueryCollector, QueryLog, QueryRecord
from platform_ops.cost.collectors import (
    JOBS_SCHEMA,
    QUERY_HISTORY_SCHEMA,
    BigQueryJobsCollector,
    Principals,
    SnowflakeQueryHistoryCollector,
    schema_problems,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BIGQUERY = FIXTURES / "bigquery_jobs.jsonl"
SNOWFLAKE = FIXTURES / "snowflake_query_history.jsonl"

BQ_PRINCIPALS = Principals(
    workloads={
        "dbt-runner@company-analytics.iam.gserviceaccount.com": "dbt",
        "bi-dashboards@company-analytics.iam.gserviceaccount.com": "dashboard",
    },
    actors={
        "dbt-runner@company-analytics.iam.gserviceaccount.com": "dbt",
        "bi-dashboards@company-analytics.iam.gserviceaccount.com": "bi",
    },
)
SF_PRINCIPALS = Principals(
    workloads={"DBT_RUNNER": "dbt", "BI_DASHBOARDS": "dashboard"},
    actors={"DBT_RUNNER": "dbt", "BI_DASHBOARDS": "bi"},
)


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _bigquery() -> BigQueryJobsCollector:
    return BigQueryJobsCollector.from_jsonl(BIGQUERY, BQ_PRINCIPALS, dbt_project="company")


def _snowflake() -> SnowflakeQueryHistoryCollector:
    return SnowflakeQueryHistoryCollector.from_jsonl(
        SNOWFLAKE, SF_PRINCIPALS, dbt_project="company"
    )


# -- the fixtures are what the vendors document -----------------------------------------


@pytest.mark.parametrize(
    ("path", "schema"), [(BIGQUERY, JOBS_SCHEMA), (SNOWFLAKE, QUERY_HISTORY_SCHEMA)]
)
def test_every_recorded_row_matches_the_documented_schema(path: Path, schema: object) -> None:
    rows = _rows(path)
    assert len(rows) >= 200, "a few hundred rows"
    for row in rows:
        assert schema_problems(row, schema) == [], row.get("job_id") or row.get("QUERY_ID")  # type: ignore[arg-type]


def test_schema_check_catches_missing_extra_and_mistyped_columns() -> None:
    row = dict(_rows(BIGQUERY)[0])
    row.pop("query")
    row["surprise"] = 1
    row["cache_hit"] = "false"
    row["total_slot_ms"] = True
    assert sorted(schema_problems(row, JOBS_SCHEMA)) == [
        "cache_hit is str", "missing query", "total_slot_ms is bool", "unexpected surprise",
    ]  # fmt: skip


# -- the mapping onto QueryRecord --------------------------------------------------------


@pytest.mark.parametrize("make", [_bigquery, _snowflake], ids=["bigquery", "snowflake"])
def test_collectors_implement_the_phase_2_interface(make: object) -> None:
    collector: QueryCollector = make()  # type: ignore[operator]
    records = list(collector.records())
    assert records and all(isinstance(r, QueryRecord) for r in records)
    assert len({r.query_id for r in records}) == len(records)


@pytest.mark.parametrize("make", [_bigquery, _snowflake], ids=["bigquery", "snowflake"])
def test_every_workload_is_recognised(make: object) -> None:
    records = list(make().records())  # type: ignore[operator]
    kinds = Counter(r.actor_type for r in records)
    assert set(kinds) == {"dbt", "dashboard", "adhoc"}
    for record in records:
        if record.actor_type == "dbt":
            assert record.actor == "dbt"
            assert record.node_id and record.node_id.startswith(("model.company.", "test.company."))
        elif record.actor_type == "dashboard":
            assert record.actor == "bi"
            assert record.node_id and record.node_id.startswith("exposure.company.")
        else:
            assert record.actor.islower() and "@" not in record.actor
        assert record.bytes_scanned is not None and record.bytes_scanned >= 0
        assert record.started_at.tzinfo is None, "naive UTC, like every query time"


def test_dbt_runs_group_by_invocation() -> None:
    records = [r for r in _bigquery().records() if r.actor_type == "dbt"]
    runs = Counter(r.run_id for r in records)
    assert len(runs) < len(records), "many dbt queries share one invocation"


def test_bigquery_skips_what_it_must_and_counts_it() -> None:
    collector = _bigquery()
    ids = {r.query_id for r in collector.records()}
    assert collector.skipped.reasons == Counter(
        {"failed": 1, "served from cache": 1, "not finished": 1, "script parent": 1}
    )
    assert "bigquery:bquxjob_edge_script_child" in ids, "a script's child job is kept"
    child = next(r for r in collector.records() if r.query_id.endswith("script_child"))
    assert (child.actor, child.actor_type, child.node_id) == ("maya", "adhoc", None)
    assert len(ids) + collector.skipped.total == len(_rows(BIGQUERY))


def test_snowflake_skips_failures_but_keeps_result_cache_hits() -> None:
    collector = _snowflake()
    records = {r.query_id: r for r in collector.records()}
    assert collector.skipped.reasons == Counter({"status fail": 1, "status incident": 1})
    cached = records["snowflake:01bedgeresultcache"]
    assert cached.bytes_scanned == 0 and cached.actor == "maya" and cached.node_id is None
    assert len(records) + collector.skipped.total == len(_rows(SNOWFLAKE))


def test_billed_bytes_respect_the_bigquery_minimum() -> None:
    for row in _rows(BIGQUERY):
        billed, processed = row["total_bytes_billed"], row["total_bytes_processed"]
        if row["state"] == "DONE" and not row["error_result"] and processed:
            assert billed >= 10 * 1024 * 1024 and billed >= processed  # type: ignore[operator]


# -- records flow into the existing pipeline unchanged ---------------------------------


@pytest.mark.parametrize("make", [_bigquery, _snowflake], ids=["bigquery", "snowflake"])
def test_records_go_through_the_existing_query_log(make: object) -> None:
    log = QueryLog(catalog={})
    records = list(make().records())  # type: ignore[operator]
    for record in records:
        log.add(record)
    assert log.count == len(records)
    parsed = [log.parse(r.sql_text) for r in records]
    readable = [p for p in parsed if p.ok and p.reads]
    assert len(readable) >= 0.9 * len(records), "the parser reads vendor SQL like local SQL"
    dbt_nodes = {log.parse(r.sql_text).node_id for r in records if r.actor_type == "dbt"}
    assert dbt_nodes == {r.node_id for r in records if r.actor_type == "dbt"}
