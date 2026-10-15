"""A callback for Cosmos (astronomer-cosmos), which runs dbt one task per model in Airflow.

Give it to Cosmos as the operators' ``callback``. Cosmos calls it after each
task's dbt invocation with the project folder the task ran in; the invocation's
artifacts are in its ``target/``. The run id is Airflow's DAG run, from the
task context when Cosmos passes it, else from the ``AIRFLOW_CTX_*`` variables
Airflow sets for every running task, so every task of one DAG run lands in one
incident run and is regrouped like a single build::

    from platform_ops.integrations.cosmos import ingest_after_task

    DbtDag(..., operator_args={"callback": ingest_after_task})

No import of Airflow or Cosmos here: Airflow calls this and hands it what it needs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from platform_ops.integrations import IngestOutcome, ingest


def run_id_from(context: dict[str, Any], environ: dict[str, str] | None = None) -> str:
    """``airflow:<dag id>:<run id>``, from the task context or Airflow's task environment."""
    env = os.environ if environ is None else environ
    dag = context.get("dag")
    dag_run = context.get("dag_run")
    dag_id = getattr(dag, "dag_id", None) or env.get("AIRFLOW_CTX_DAG_ID", "")
    run_id = (
        context.get("run_id")
        or getattr(dag_run, "run_id", None)
        or env.get("AIRFLOW_CTX_DAG_RUN_ID", "")
    )
    if not run_id:
        raise ValueError("no Airflow run id: neither the task context nor AIRFLOW_CTX_* has one")
    return f"airflow:{dag_id}:{run_id}" if dag_id else f"airflow:{run_id}"


def ingest_after_task(project_dir: str | Path, **context: Any) -> IngestOutcome:
    """Hand one task's dbt invocation to the incident desk. Never raises."""
    try:
        run_id = run_id_from(context)
    except ValueError as error:
        return IngestOutcome(False, (), str(error))
    return ingest(Path(project_dir) / "target", run_id)
