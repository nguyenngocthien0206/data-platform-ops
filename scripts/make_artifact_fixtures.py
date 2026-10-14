"""Write the dbt artifact fixtures the incident desk's tests ingest.

Usage (from the repo root)::

    uv run python scripts/make_artifact_fixtures.py

A small shop project (two sources, three models, three tests, a dashboard), its
ownership registry, and the artifacts two runs of it left on other warehouses:
a ``dbt build`` on Snowflake where a staging test fails, and a night on
BigQuery where a source went stale and a relationships test failed below it.
The files follow dbt's documented artifact schemas (manifest v12, run results
v6, sources v3) with only the fields a reader of them needs; the values are
made up, the shapes are dbt's. The output is deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "dbt_artifacts"
PROJECT = "shop"


def _node(
    unique_id: str,
    resource_type: str,
    name: str,
    *,
    depends_on: tuple[str, ...] = (),
    schema: str = "analytics",
    path: str = "",
    attached: str | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "unique_id": unique_id,
        "resource_type": resource_type,
        "name": name,
        "package_name": PROJECT,
        "schema": schema,
        "original_file_path": path,
        "depends_on": {"nodes": list(depends_on), "macros": []},
        "config": {"enabled": True},
    }
    if resource_type == "model":
        node["alias"] = name
        node["config"]["materialized"] = "table"
    if resource_type == "test":
        node["attached_node"] = attached
        node["path"] = f"{name}.sql"
    return node


SOURCES = {
    f"source.{PROJECT}.raw.{name}": {
        "unique_id": f"source.{PROJECT}.raw.{name}",
        "resource_type": "source",
        "name": name,
        "source_name": "raw",
        "identifier": name,
        "package_name": PROJECT,
        "schema": "raw",
        "original_file_path": "models/sources.yml",
        "loaded_at_field": "_loaded_at",
    }
    for name in ("orders", "payments")
}
STG_ORDERS = f"model.{PROJECT}.stg_orders"
STG_PAYMENTS = f"model.{PROJECT}.stg_payments"
FCT = f"model.{PROJECT}.fct_revenue"
NOT_NULL = f"test.{PROJECT}.not_null_stg_orders_customer_id.a1"
RELATIONSHIPS = f"test.{PROJECT}.relationships_stg_payments_order_id__order_id__ref_stg_orders_.b2"
UNIQUE = f"test.{PROJECT}.unique_fct_revenue_order_id.c3"
NODES = {
    STG_ORDERS: _node(
        STG_ORDERS, "model", "stg_orders", depends_on=(f"source.{PROJECT}.raw.orders",),
        schema="staging", path="models/staging/stg_orders.sql",
    ),
    STG_PAYMENTS: _node(
        STG_PAYMENTS, "model", "stg_payments", depends_on=(f"source.{PROJECT}.raw.payments",),
        schema="staging", path="models/staging/stg_payments.sql",
    ),
    FCT: _node(
        FCT, "model", "fct_revenue", depends_on=(STG_ORDERS, STG_PAYMENTS),
        schema="marts", path="models/marts/fct_revenue.sql",
    ),
    NOT_NULL: _node(
        NOT_NULL, "test", "not_null_stg_orders_customer_id", depends_on=(STG_ORDERS,),
        attached=STG_ORDERS, path="models/staging/_staging.yml",
    ),
    RELATIONSHIPS: _node(
        RELATIONSHIPS, "test", "relationships_stg_payments_order_id__order_id__ref_stg_orders_",
        depends_on=(STG_ORDERS, STG_PAYMENTS), attached=STG_PAYMENTS,
        path="models/staging/_staging.yml",
    ),
    UNIQUE: _node(
        UNIQUE, "test", "unique_fct_revenue_order_id", depends_on=(FCT,), attached=FCT,
        path="models/marts/_marts.yml",
    ),
}  # fmt: skip
EXPOSURES = {
    f"exposure.{PROJECT}.revenue_board": {
        "unique_id": f"exposure.{PROJECT}.revenue_board",
        "resource_type": "exposure",
        "name": "revenue_board",
        "package_name": PROJECT,
        "original_file_path": "models/exposures.yml",
        "depends_on": {"nodes": [FCT], "macros": []},
        "owner": {"name": "fiona"},
        "maturity": "high",
    }
}

TEAMS = """teams:
  - id: data
    name: Data platform
    channel: "#data-platform"
    members: [dana]
  - id: finance
    name: Finance
    channel: "#finance-data"
    members: [fiona]
    telegram_chat: "-1001"
"""
OWNERSHIP = """datasets:
  - match: "source.shop.*"
    owner: dana
    team: data
    tier: important
  - match: "model.shop.stg_*"
    owner: dana
    team: data
    tier: important
  - match: "model.shop.fct_*"
    owner: fiona
    team: finance
    tier: critical
  - match: "exposure.shop.*"
    owner: fiona
    team: finance
    tier: critical
"""


def _result(unique_id: str, status: str, message: str | None = None) -> dict[str, Any]:
    return {
        "unique_id": unique_id,
        "status": status,
        "message": message,
        "failures": 3 if status == "fail" else 0,
        "thread_id": "Thread-1",
        "execution_time": 0.5,
        "timing": [],
        "adapter_response": {},
    }


def _run_results(
    adapter: str, invocation: str, at: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v6.json",
            "dbt_version": "1.9.4",
            "generated_at": at,
            "invocation_id": invocation,
            "adapter_type": adapter,
        },
        "results": results,
        "elapsed_time": 12.5,
        "args": {"which": "build"},
    }


def _sources(
    adapter: str, invocation: str, at: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/sources/v3.json",
            "dbt_version": "1.9.4",
            "generated_at": at,
            "invocation_id": invocation,
            "adapter_type": adapter,
        },
        "results": results,
        "elapsed_time": 1.5,
    }


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> None:
    _write(
        OUT / "manifest.json",
        {
            "metadata": {
                "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                "project_name": PROJECT,
            },
            "nodes": NODES,
            "sources": SOURCES,
            "exposures": EXPOSURES,
        },
    )
    (OUT / "registry").mkdir(parents=True, exist_ok=True)
    (OUT / "registry" / "teams.yaml").write_text(TEAMS, encoding="utf-8", newline="\n")
    (OUT / "registry" / "ownership.yaml").write_text(OWNERSHIP, encoding="utf-8", newline="\n")
    # A dbt build on Snowflake: a staging test fails, everything else passes.
    _write(
        OUT / "snowflake" / "run_results.json",
        _run_results(
            "snowflake",
            "9b2f0c1e-5d1a-4c55-9c8e-0a1b2c3d4e01",
            "2026-03-02T02:14:09.120934Z",
            [
                _result(STG_ORDERS, "success"),
                _result(STG_PAYMENTS, "success"),
                _result(NOT_NULL, "fail", "Got 3 results, configured to fail if != 0"),
                _result(RELATIONSHIPS, "pass"),
                _result(FCT, "success"),
                _result(UNIQUE, "pass"),
            ],
        ),
    )
    # A night on BigQuery: payments went stale, and the relationships test below it failed.
    _write(
        OUT / "bigquery" / "sources.json",
        _sources(
            "bigquery",
            "4c7d2e90-1b3a-4f6e-8d2c-7e6f5a4b3c02",
            "2026-03-03T02:01:40.553112Z",
            [
                {"unique_id": f"source.{PROJECT}.raw.orders", "status": "pass",
                 "max_loaded_at": "2026-03-03T01:00:00Z", "age": 3700.0},
                {"unique_id": f"source.{PROJECT}.raw.payments", "status": "error",
                 "max_loaded_at": "2026-02-28T01:00:00Z", "age": 263000.0},
            ],
        ),
    )  # fmt: skip
    _write(
        OUT / "bigquery" / "run_results.json",
        _run_results(
            "bigquery",
            "4c7d2e90-1b3a-4f6e-8d2c-7e6f5a4b3c03",
            "2026-03-03T02:09:12.002311Z",
            [
                _result(STG_ORDERS, "success"),
                _result(STG_PAYMENTS, "success"),
                _result(NOT_NULL, "pass"),
                _result(RELATIONSHIPS, "fail", "Got 41 results, configured to fail if != 0"),
                _result(FCT, "success"),
                _result(UNIQUE, "pass"),
            ],
        ),
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
