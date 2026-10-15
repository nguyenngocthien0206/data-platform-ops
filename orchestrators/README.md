# orchestrators

How a scheduler that runs dbt hands each run to the incident desk ([ADR 0020](../docs/adr/0020-orchestrators-call-the-toolkit.md)). The company the toolkit is built for runs dbt with Airflow through Cosmos and with Dagster through dagster-dbt. Phase 14b brings both up for real, in Docker: this part (14b-1) builds what they call; the Airflow stack (14b-2) and the Dagster stack (14b-3) follow.

## The contract

After every dbt invocation, call `platform-ops incidents ingest` with the folder dbt wrote its artifacts to and the id of the scheduled run it belongs to:

```bash
platform-ops incidents ingest --target <dbt target folder> --run-id <scheduler run id>
```

Invocations that share a run id are one run: the desk regroups them on every ingest, so a run split into one invocation per model gives the same incidents as one `dbt build`, in whatever order the pieces arrive (`src/platform_ops/incidents/README.md`). Ingesting the same invocation twice changes nothing.

Two hooks do this for you, standard library only, without importing either orchestrator (`src/platform_ops/integrations/`):

| Orchestrator | Hook | Run id |
|---|---|---|
| Airflow with Cosmos | `cosmos.ingest_after_task`, as the operators' `callback` | `airflow:<dag id>:<run id>`, from the task context or `AIRFLOW_CTX_*` |
| Dagster with dagster-dbt | `dagster.ingest_dbt_invocation(invocation, context)`, after the invocation finishes | `dagster:<run id>` |

```python
# Airflow, Cosmos
from platform_ops.integrations.cosmos import ingest_after_task

DbtDag(..., operator_args={"callback": ingest_after_task})

# Dagster, dagster-dbt
from platform_ops.integrations.dagster import ingest_dbt_invocation


@dbt_assets(manifest=...)
def company(context, dbt):
    invocation = dbt.cli(["build"], context=context, raise_on_error=False)
    yield from invocation.stream()
    ingest_dbt_invocation(invocation, context)
```

Both run the CLI named by `PLATFORM_OPS_CLI` (default `platform-ops`), so the toolkit can live in a virtual environment of its own next to the scheduler's. They read the usual settings from the environment: `PLATFORM_OPS_CONFIG`, the Postgres and ClickHouse passwords, and `PLATFORM_OPS_REGISTRY_DIR` for the consuming repository's ownership registry (with its optional `incidents.yaml`). `SLACK_BOT_TOKEN` and `TELEGRAM_BOT_TOKEN` page the teams in `teams.yaml`. A failed ingest is logged and never raised, so incident tracking cannot fail a pipeline run.

## Running another company beside the demo

`PLATFORM_OPS_NAMESPACE` runs another company from the same config file, in its own Postgres schemas and ClickHouse databases, with its own incident store. The orchestrator stacks use `af_` (Airflow) and `dg_` (Dagster), so nothing they do touches `make demo` or its reports:

```bash
PLATFORM_OPS_NAMESPACE=af_ PLATFORM_OPS_SCALE_FACTOR=0.1 uv run platform-ops seed
```

## The chat double

`chat_double.py` answers like Slack's `chat.postMessage` and Telegram's `sendMessage`, refuses a missing token or chat the way they do, and keeps every message (`GET /messages`, `DELETE /messages`). The orchestrator tests point `SLACK_API_URL` and `TELEGRAM_API_URL` at it to check a page without sending one:

```bash
docker compose --profile chat up -d chat      # http://localhost:8099
```
