"""The company's nightly dbt run, scheduled by Airflow through Cosmos (Phase 14b-2).

What a company running dbt in Airflow typically has, wired to the incident desk:

- **the models**, one Cosmos task per node, each model followed by its tests
  (``TestBehavior.AFTER_EACH``), so a failing test stops only what is below it;
  a test on two models runs once both are built.
  Every task hands its dbt invocation to the desk through the toolkit's hook,
  ``ingest_after_task``, which Cosmos calls whether dbt failed or not;
- **source freshness**, a task of its own beside the models: a stale feed turns
  the run red without holding the build back. It ingests into the same run.

All of one DAG run's invocations share its run id, ``airflow:company_dbt:<run
id>``, so the desk regroups them like a single ``dbt build`` (ADR 0016, 0017).

The DAG runs at 02:00 UTC every day and is paused when Airflow first sees it.

Two things are specific to the simulated company and would go in a real one:
the ``simulated_now`` var, because the company's data lives on its own clock
(``platform-ops company clock``), and the ``namespace`` var, because this
company runs in the ``af_`` namespace beside the demo's.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from airflow.exceptions import AirflowException
from airflow.sdk import dag, task
from airflow.timetables.trigger import CronTriggerTimetable
from cosmos import DbtTaskGroup, ExecutionConfig, ProfileConfig, ProjectConfig, RenderConfig
from cosmos.constants import ExecutionMode, InvocationMode, LoadMode, TestBehavior

from platform_ops.integrations import ingest
from platform_ops.integrations.cosmos import ingest_after_task, run_id_from

PROJECT = Path(os.environ.get("COMPANY_DBT_PROJECT", "/opt/platform-ops/dbt"))
MANIFEST = Path(os.environ.get("COMPANY_DBT_MANIFEST", "/opt/cosmos/target/manifest.json"))
DBT = os.environ.get("COMPANY_DBT_EXECUTABLE", "/opt/toolkit/bin/dbt")
TOOLKIT = os.environ.get("PLATFORM_OPS_CLI", "/opt/toolkit/bin/platform-ops")

# The company this stack runs, set on the container (docker-compose.yml).
DBT_VARS = {
    "namespace": os.environ.get("PLATFORM_OPS_NAMESPACE", "af_"),
    "scale_factor": float(os.environ.get("PLATFORM_OPS_SCALE_FACTOR", "1.0")),
}
# As many dbt processes at once as the profile has threads.
CONCURRENCY = 8
# 02:00 UTC every day. A stack seen for the first time waits for the next 02:00
# (no grace, and no start_date: Airflow 3.3 runs the night that just passed at
# once when a start_date is set), so a run someone triggers is the only one.
# After a stack was down, the latest night missed runs once, not every one.
NIGHTLY = CronTriggerTimetable("0 2 * * *", timezone="UTC", run_immediately=timedelta(0))


@dag(
    dag_id="company_dbt",
    schedule=NIGHTLY,
    catchup=False,
    max_active_runs=1,
    max_active_tasks=CONCURRENCY,
    is_paused_upon_creation=True,
    tags=["dbt", "company", "platform-ops"],
    doc_md=__doc__,
)
def company_dbt() -> None:
    @task
    def source_freshness(**context: Any) -> None:
        """dbt source freshness on the company's clock, ingested into this run."""
        clock = subprocess.run(
            [TOOLKIT, "company", "clock"], capture_output=True, text=True, check=True
        ).stdout.strip()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "target"
            done = subprocess.run(
                [
                    DBT,
                    "source",
                    "freshness",
                    "--project-dir",
                    str(PROJECT),
                    "--profiles-dir",
                    str(PROJECT),
                    "--target-path",
                    str(target),
                    "--log-path",
                    str(Path(folder) / "logs"),
                    "--vars",
                    json.dumps({**DBT_VARS, "simulated_now": clock}),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            print(done.stdout[-5000:], done.stderr[-5000:])
            ingest(target, run_id_from(context))
        if done.returncode != 0:
            raise AirflowException(f"dbt source freshness failed (exit {done.returncode})")

    source_freshness()

    DbtTaskGroup(
        group_id="models",
        project_config=ProjectConfig(
            dbt_project_path=PROJECT,
            manifest_path=MANIFEST,
            project_name="company",
            install_dbt_deps=False,
            dbt_vars=DBT_VARS,
        ),
        profile_config=ProfileConfig(
            profile_name="company",
            target_name="local",
            profiles_yml_filepath=PROJECT / "profiles.yml",
        ),
        execution_config=ExecutionConfig(
            execution_mode=ExecutionMode.LOCAL,
            invocation_mode=InvocationMode.SUBPROCESS,
            dbt_executable_path=DBT,
        ),
        render_config=RenderConfig(
            load_method=LoadMode.DBT_MANIFEST,
            test_behavior=TestBehavior.AFTER_EACH,
            # A test on two models (a relationships test) is a task of its own
            # after both, not part of one model's tests, where it would read a
            # model the run has not built yet.
            should_detach_multiple_parents_tests=True,
        ),
        operator_args={"callback": ingest_after_task, "install_deps": False},
    )


company_dbt()
