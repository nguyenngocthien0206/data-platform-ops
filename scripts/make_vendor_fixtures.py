"""Write the recorded vendor query histories the collector contract tests read.

Usage (from the repo root)::

    uv run python scripts/make_vendor_fixtures.py

It simulates one week of the company at scale 0.01 in a temporary directory,
prices it, and takes a sample of the real query log: dbt builds and tests,
dashboard refreshes and ad hoc SQL, with their SQL text, dbt query comments,
times and estimated bytes. Each sampled query is written as a BigQuery
``INFORMATION_SCHEMA.JOBS`` row and as a Snowflake ``QUERY_HISTORY`` row, with
the column names and types of the vendors' documentation. A few edge rows are
added by hand: a failed job, a cache hit, a script parent and its child, an
unfinished job, and SQL without any comment or tag.

The values are simulated, the shapes are the vendors'. The output is
deterministic, so rerunning it only changes the files if the simulation does.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import yaml
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
PROJECT = "company-analytics"
SAMPLE = {"dbt": 150, "dashboard": 100, "adhoc": 50}
DBT_PRINCIPAL = {
    "bigquery": f"dbt-runner@{PROJECT}.iam.gserviceaccount.com",
    "snowflake": "DBT_RUNNER",
}
BI_PRINCIPAL = {"bigquery": f"bi-dashboards@{PROJECT}.iam.gserviceaccount.com",
                "snowflake": "BI_DASHBOARDS"}  # fmt: skip
ROLES = {"dbt": "TRANSFORMER", "dashboard": "REPORTER", "adhoc": "ANALYST"}


def _digest(*parts: object) -> str:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


def _number(*parts: object, modulo: int) -> int:
    return int(_digest(*parts)[:12], 16) % modulo


def _simulate(root: Path) -> tuple[Path, dict[str, str]]:
    """One week at scale 0.01, simulated and priced, in ``root``."""
    from platform_ops.cli import app
    from platform_ops.common.config import CONFIG_PATH_ENV_VAR

    config = root / "config"
    config.mkdir(parents=True)
    raw = yaml.safe_load((REPO / "config" / "settings.yaml").read_text(encoding="utf-8"))
    raw["scale_factor"] = 0.01
    raw["simulation"]["weeks"] = 1
    raw["paths"] = {"duckdb": str(root / "warehouse.duckdb"), "reports": str(root / "reports"),
                    "iceberg_warehouse": str(root / "iceberg"), "dbt_project": str(REPO / "dbt"),
                    "dbt_target": str(root / "target")}  # fmt: skip
    (config / "settings.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    for name in ("teams.yaml", "ownership.yaml"):
        shutil.copyfile(REPO / "config" / name, config / name)
    runner = CliRunner()
    env = {CONFIG_PATH_ENV_VAR: str(config / "settings.yaml")}
    for command in (["simulation", "run"], ["cost", "report"]):
        result = runner.invoke(app, command, env=env)
        if result.exit_code != 0:
            sys.exit(f"{' '.join(command)} failed:\n{result.output}")
    return root / "warehouse.duckdb", raw["pricing"]["compute"]["warehouses"]


def _sample(warehouse: Path) -> list[dict[str, Any]]:
    connection = duckdb.connect(str(warehouse), read_only=True)
    try:
        rows = connection.execute(
            """SELECT l.query_id, l.run_id, l.actor, l.actor_type, l.node_id, l.started_at,
                      l.sql_text, l.wallclock_ms, l.writes, e.bytes_scanned, e.modeled_ms,
                      list(t.table_name ORDER BY t.table_name)
                          FILTER (WHERE t.table_name IS NOT NULL)
               FROM ops.query_log l
               JOIN ops.query_estimates e USING (query_id)
               LEFT JOIN ops.query_tables t USING (query_id)
               GROUP BY ALL"""
        ).fetchall()
    finally:
        connection.close()
    names = ["query_id", "run_id", "actor", "actor_type", "node_id", "started_at", "sql_text",
             "wallclock_ms", "writes", "bytes", "modeled_ms", "tables"]  # fmt: skip
    queries = [dict(zip(names, row, strict=True)) for row in rows]
    chosen: list[dict[str, Any]] = []
    for actor_type, n in SAMPLE.items():
        pool = sorted((q for q in queries if q["actor_type"] == actor_type),
                      key=lambda q: _digest("sample", q["query_id"]))  # fmt: skip
        chosen += pool[:n]
    return sorted(chosen, key=lambda q: (q["started_at"], q["query_id"]))


def _elapsed_ms(query: dict[str, Any]) -> int:
    # The modelled time, never the measured one: wall-clock time differs on every
    # run, and the fixtures must not change unless the simulation does.
    return max(1, int(query["modeled_ms"]))


def _executed_sql(query: dict[str, Any]) -> str:
    """The SQL as the warehouse received it.

    The query log keeps dbt's compiled SQL from ``target/run``, which dbt writes
    before it adds the query comment. The warehouse sees the statement with the
    comment appended (``query-comment`` with ``append: true`` in
    ``dbt_project.yml``), and that is what a vendor's history records.
    """
    sql = str(query["sql_text"])
    if query["actor_type"] == "dbt" and query["node_id"]:
        comment = json.dumps({"app": "platform-ops", "unique_id": query["node_id"]})
        sql = f"{sql}\n/* {comment} */"
    return sql


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _table_ref(name: str) -> dict[str, str]:
    dataset, _, table = name.partition(".")
    return {"project_id": PROJECT, "dataset_id": dataset, "table_id": table}


def _exposure_name(node_id: str | None) -> str | None:
    return node_id.split(".")[-1] if node_id and node_id.startswith("exposure.") else None


def bigquery_row(query: dict[str, Any]) -> dict[str, Any]:
    kind = query["actor_type"]
    started = query["started_at"]
    ended = started + timedelta(milliseconds=_elapsed_ms(query))
    principal = {"dbt": DBT_PRINCIPAL["bigquery"], "dashboard": BI_PRINCIPAL["bigquery"]}.get(
        kind, f"{query['actor']}@company.example"
    )
    labels = []
    if kind == "dbt":
        labels.append({"key": "dbt_invocation_id", "value": _digest("run", query["run_id"])[:32]})
    exposure = _exposure_name(query["node_id"])
    if exposure:
        labels.append({"key": "dashboard", "value": exposure})
    billed = max(query["bytes"], 10 * 1024 * 1024) if query["bytes"] else 0
    return {
        "job_id": f"bquxjob_{_digest('job', query['query_id'])[:16]}",
        "creation_time": _iso(started),
        "start_time": _iso(started),
        "end_time": _iso(ended),
        "project_id": PROJECT,
        "user_email": principal,
        "job_type": "QUERY",
        "statement_type": "CREATE_TABLE_AS_SELECT" if query["writes"] else "SELECT",
        "query": _executed_sql(query),
        "state": "DONE",
        "total_bytes_processed": int(query["bytes"]),
        "total_bytes_billed": int(billed),
        "total_slot_ms": int(query["modeled_ms"]) * 2,
        "cache_hit": False,
        "destination_table": _table_ref(query["writes"]) if query["writes"] else None,
        "referenced_tables": [_table_ref(t) for t in query["tables"] or []],
        "labels": labels,
        "error_result": None,
        "parent_job_id": None,
        "reservation_id": None,
    }


def snowflake_row(query: dict[str, Any], warehouses: dict[str, str]) -> dict[str, Any]:
    kind = query["actor_type"]
    started = query["started_at"]
    elapsed = _elapsed_ms(query)
    principal = {"dbt": DBT_PRINCIPAL["snowflake"], "dashboard": BI_PRINCIPAL["snowflake"]}.get(
        kind, str(query["actor"]).upper()
    )
    tag: dict[str, str] = {}
    if kind == "dbt":
        tag["dbt_invocation_id"] = _digest("run", query["run_id"])[:32]
    exposure = _exposure_name(query["node_id"])
    if exposure:
        tag["dashboard"] = exposure
    schema = query["writes"].split(".")[0].upper() if query["writes"] else None
    return {
        "QUERY_ID": "01b" + _digest("sf", query["query_id"])[:33],
        "QUERY_TEXT": _executed_sql(query),
        "DATABASE_NAME": "ANALYTICS",
        "SCHEMA_NAME": schema,
        "QUERY_TYPE": "CREATE_TABLE_AS_SELECT" if query["writes"] else "SELECT",
        "SESSION_ID": 1_000_000 + _number("session", query["run_id"], modulo=900_000),
        "USER_NAME": principal,
        "ROLE_NAME": ROLES[kind],
        "WAREHOUSE_NAME": warehouses[kind].upper(),
        "WAREHOUSE_SIZE": "X-Small",
        "QUERY_TAG": json.dumps(tag, sort_keys=True) if tag else "",
        "EXECUTION_STATUS": "success",
        "ERROR_CODE": None,
        "START_TIME": started.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00",
        "END_TIME": (started + timedelta(milliseconds=elapsed)).strftime("%Y-%m-%dT%H:%M:%S.%f")
        + "+00:00",
        "TOTAL_ELAPSED_TIME": elapsed,
        "BYTES_SCANNED": int(query["bytes"]),
        "ROWS_PRODUCED": None,
        "CREDITS_USED_CLOUD_SERVICES": round(elapsed / 3_600_000 * 0.1, 9),
        "EXECUTION_TIME": max(0, elapsed - 20),
        "PERCENTAGE_SCANNED_FROM_CACHE": 0.0,
    }


def bigquery_edges(base: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows the collector must skip or handle specially, derived from a real row."""
    failed = {**base, "job_id": "bquxjob_edge_failed", "total_bytes_billed": 0,
              "error_result": {"reason": "invalidQuery", "location": "query",
                               "message": "Unrecognized name: ordered_on"}}  # fmt: skip
    cached = {**base, "job_id": "bquxjob_edge_cached", "cache_hit": True,
              "total_bytes_processed": 0, "total_bytes_billed": 0}  # fmt: skip
    running = {**base, "job_id": "bquxjob_edge_running", "state": "RUNNING", "end_time": None,
               "total_bytes_billed": None}  # fmt: skip
    script = {**base, "job_id": "bquxjob_edge_script", "statement_type": "SCRIPT",
              "query": "DECLARE d DATE DEFAULT CURRENT_DATE(); SELECT d;",
              "referenced_tables": [], "labels": []}  # fmt: skip
    child = {**base, "job_id": "bquxjob_edge_script_child",
             "parent_job_id": "bquxjob_edge_script", "query": "SELECT d",
             "statement_type": "SELECT", "referenced_tables": [], "labels": [],
             "user_email": "maya@company.example", "destination_table": None}  # fmt: skip
    return [failed, cached, running, script, child]


def snowflake_edges(base: dict[str, Any]) -> list[dict[str, Any]]:
    failed = {**base, "QUERY_ID": "01bedgefailed", "EXECUTION_STATUS": "fail",
              "ERROR_CODE": 904, "BYTES_SCANNED": 0}  # fmt: skip
    incident = {**base, "QUERY_ID": "01bedgeincident", "EXECUTION_STATUS": "incident",
                "ERROR_CODE": 300005}  # fmt: skip
    cached = {**base, "QUERY_ID": "01bedgeresultcache", "BYTES_SCANNED": 0,
              "EXECUTION_TIME": 0, "PERCENTAGE_SCANNED_FROM_CACHE": 1.0,
              "USER_NAME": "MAYA", "ROLE_NAME": "ANALYST", "QUERY_TAG": "",
              "QUERY_TEXT": "select count(*) from marts.sales_fct_orders"}  # fmt: skip
    return [failed, incident, cached]


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                    encoding="utf-8", newline="\n")  # fmt: skip


def main() -> None:
    # dbt keeps its log file open on Windows, so a failed cleanup is not an error.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        warehouse, warehouses = _simulate(Path(tmp))
        queries = _sample(warehouse)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    bigquery = [bigquery_row(q) for q in queries]
    snowflake = [snowflake_row(q, warehouses) for q in queries]
    first_dbt = next(q for q in queries if q["actor_type"] == "dbt")
    bigquery += bigquery_edges(bigquery_row(first_dbt))
    snowflake += snowflake_edges(snowflake_row(first_dbt, warehouses))
    _write(FIXTURES / "bigquery_jobs.jsonl", bigquery)
    _write(FIXTURES / "snowflake_query_history.jsonl", snowflake)
    print(f"wrote {len(bigquery)} BigQuery and {len(snowflake)} Snowflake rows to {FIXTURES}")


if __name__ == "__main__":
    main()
