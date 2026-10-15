"""The company's nightly dbt run, scheduled by Dagster through dagster-dbt (Phase 14b-3).

What a company running dbt in Dagster typically has, wired to the incident desk:

- **the models as assets** (``@dbt_assets`` over the project's manifest), built
  by one ``dbt build``: each model, then its tests, a failing test stopping what
  is below it;
- **source freshness** first, in the same run, which never holds the build back.

Both invocations are handed to the desk through the toolkit's hook,
``ingest_dbt_invocation``, under the run's id, ``dagster:<run id>``, so the desk
groups them as one run (ADR 0016, 0017). dbt runs with ``raise_on_error=False``
so the hook always gets the artifacts; the run is failed afterwards when dbt
failed, so it shows as failed in the UI.

The job runs at 02:00 UTC every day once its schedule is started; it is stopped
until then. Dagster never runs the nights it missed.

Two things are specific to the simulated company and would go in a real one:
the ``simulated_now`` var, because the company's data lives on its own clock
(``platform-ops company clock``), and the ``namespace`` var, because this
company runs in the ``dg_`` namespace beside the demo's.
"""

# No `from __future__ import annotations` here: Dagster reads the asset
# function's `context` annotation as a real type, not as a string.
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dagster import (
    AssetExecutionContext,
    AssetSelection,
    DefaultScheduleStatus,
    Definitions,
    Failure,
    ScheduleDefinition,
    define_asset_job,
)
from dagster_dbt import DbtCliResource, dbt_assets

from platform_ops.integrations.dagster import ingest_dbt_invocation

PROJECT = Path(os.environ.get("COMPANY_DBT_PROJECT", "/opt/platform-ops/dbt"))
MANIFEST = Path(os.environ.get("COMPANY_DBT_MANIFEST", "/opt/dagster/manifest/manifest.json"))
DBT = os.environ.get("COMPANY_DBT_EXECUTABLE", "/opt/toolkit/bin/dbt")
TOOLKIT = os.environ.get("PLATFORM_OPS_CLI", "/opt/toolkit/bin/platform-ops")

# The company this stack runs, set on the container (docker-compose.yml).
DBT_VARS = {
    "namespace": os.environ.get("PLATFORM_OPS_NAMESPACE", "dg_"),
    "scale_factor": float(os.environ.get("PLATFORM_OPS_SCALE_FACTOR", "1.0")),
}


def _vars(**extra: Any) -> list[str]:
    return ["--vars", json.dumps({**DBT_VARS, **extra})]


@dbt_assets(manifest=MANIFEST)
def company(context: AssetExecutionContext, dbt: DbtCliResource) -> Iterator[Any]:
    clock = subprocess.run(
        [TOOLKIT, "company", "clock"], capture_output=True, text=True, check=True
    ).stdout.strip()
    freshness = dbt.cli(
        ["source", "freshness", *_vars(simulated_now=clock)], raise_on_error=False
    ).wait()
    ingest_dbt_invocation(freshness, context)

    build = dbt.cli(["build", *_vars()], context=context, raise_on_error=False)
    yield from build.stream()
    ingest_dbt_invocation(build, context)

    failed = [
        name
        for name, invocation in (("source freshness", freshness), ("build", build))
        if not invocation.is_successful()
    ]
    if failed:
        raise Failure(description=f"dbt {' and '.join(failed)} failed; see the incident desk")


company_dbt = define_asset_job("company_dbt", selection=AssetSelection.assets(company))

nightly = ScheduleDefinition(
    name="company_dbt_nightly",
    job=company_dbt,
    cron_schedule="0 2 * * *",
    execution_timezone="UTC",
    default_status=DefaultScheduleStatus.STOPPED,
)

defs = Definitions(
    assets=[company],
    jobs=[company_dbt],
    schedules=[nightly],
    resources={
        "dbt": DbtCliResource(
            project_dir=os.fspath(PROJECT),
            profiles_dir=os.fspath(PROJECT),
            dbt_executable=DBT,
        )
    },
)
