"""A helper for dagster-dbt, which runs dbt from a Dagster ``@dbt_assets`` function.

Call it once the invocation has finished, with the invocation and the asset
context. The run id is Dagster's, so every invocation of one Dagster run lands
in one incident run::

    from platform_ops.integrations.dagster import ingest_dbt_invocation

    @dbt_assets(manifest=...)
    def company(context, dbt):
        invocation = dbt.cli(["build"], context=context, raise_on_error=False)
        yield from invocation.stream()
        ingest_dbt_invocation(invocation, context)

No import of Dagster here: the asset function calls this and hands it what it needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from platform_ops.integrations import IngestOutcome, ingest


def ingest_dbt_invocation(invocation: Any, context: Any) -> IngestOutcome:
    """Hand one ``DbtCliInvocation`` to the incident desk, as part of the context's run."""
    run_id = getattr(context, "run_id", "")
    target = getattr(invocation, "target_path", None)
    if not run_id or target is None:
        return IngestOutcome(False, (), "a dagster-dbt invocation and its context are needed")
    return ingest(Path(target), f"dagster:{run_id}")
