# orchestrators

How a scheduler that runs dbt hands each run to the incident desk ([ADR 0020](../docs/adr/0020-orchestrators-call-the-toolkit.md)). The company the toolkit is built for runs dbt with Airflow through Cosmos and with Dagster through dagster-dbt. Phase 14b brings both up for real, in Docker: 14b-1 built what they call, 14b-2 the Airflow stack ([below](#airflow-with-cosmos)); the Dagster stack (14b-3) follows.

## The contract

After every dbt invocation, call `platform-ops incidents ingest` with the folder dbt wrote its artifacts to and the id of the scheduled run it belongs to:

```bash
platform-ops incidents ingest --target <dbt target folder> --run-id <scheduler run id>
```

Invocations that share a run id are one run: the desk regroups them on every ingest, so a run split into one invocation per model gives the same incidents as one `dbt build`, in whatever order the pieces arrive (`src/platform_ops/incidents/README.md`). Ingesting the same invocation twice changes nothing.

Two hooks do this for you, standard library only, without importing either orchestrator (`src/platform_ops/integrations/`):

| Orchestrator | Hook | Run id |
|---|---|---|
| Airflow with Cosmos | `cosmos.ingest_after_task`, as the operators' `callback` | `airflow:<dag id>:<run id>`, from the task context Cosmos passes or `AIRFLOW_CTX_*` |
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

## Airflow with Cosmos

`airflow/` runs the company's dbt project in Airflow 3.3 through Cosmos 1.15, as its own company in the `af_` namespace, so nothing it does touches `make demo`:

```bash
make airflow          # seeds af_ (scale 0.1), starts Airflow and the chat double
```

The UI is at <http://localhost:8080> (user `admin`, `AIRFLOW_ADMIN_PASSWORD` in `.env`). The DAG `company_dbt` runs at 02:00 UTC every day and is paused until you unpause it; its pages land in the chat double at <http://localhost:8099/messages>.

What is in it:

- **Its own image** (`airflow/Dockerfile`, from `apache/airflow:3.3.2-python3.11`). Cosmos is installed against Airflow's constraint file. The toolkit lives in its own venv at `/opt/toolkit`, from `uv.lock`, and that venv's dbt is the one Cosmos runs, so Airflow's environment holds no dbt and the two lock files never meet. Of the toolkit, Airflow's environment holds only the package itself, without its dependencies, so the DAG can import the standard-library-only hook.
- **The DAG** (`airflow/dags/company_dbt.py`): a Cosmos `DbtTaskGroup` rendered from a manifest parsed when the image is built, one task per model followed by its tests, a test on two models in a task of its own after both. Every task's `callback` is `ingest_after_task`; Cosmos calls it after dbt, failed or not, with the task's own `target/`. Source freshness is a task beside the models, measured on the company's clock (`platform-ops company clock`, where its latest load reached), because the company's data lives on simulated time.
- **Least privilege.** The container gets the Postgres login for its metadata database (`airflow`, created on the company's server at start) and the incident store, and the company's `dbt` user for ClickHouse, not the admin and not `.env`.
- **The schedule** is a `CronTriggerTimetable` with no grace and no `start_date`: a stack seen for the first time waits for the next 02:00 instead of running the night that just passed (what Airflow 3.3 does once a `start_date` is set), so a run someone triggers is the only one. After the stack was down, the latest night missed runs once, not every one.

Wiring your own Cosmos DAG takes the callback and one variable in the worker's environment:

```python
from platform_ops.integrations.cosmos import ingest_after_task

DbtTaskGroup(
    ...,
    render_config=RenderConfig(should_detach_multiple_parents_tests=True),
    operator_args={"callback": ingest_after_task},
)
```

```bash
PLATFORM_OPS_CLI=/opt/toolkit/bin/platform-ops   # the toolkit's own venv
```

`should_detach_multiple_parents_tests` matters on a first run: without it, a relationships test between two models runs with one of them, possibly before the other was ever built, and fails for no reason the data has.

### Tested end to end

`make docker-test-orchestrators` (and the `orchestrators` workflow, on demand) runs `scripts/check_orchestrators.py` in the toolkit container against the running stack, on the `af_` company seeded anew at scale 0.01:

1. plant F1, NULLs in `raw.orders.customer_id`;
2. trigger `company_dbt` through Airflow's REST API (v2, a JWT from `/auth/token`) and wait: the run fails on `stg_orders`'s not_null test;
3. check one incident at `model.company.stg_orders`, opened by that run, every task's invocation grouped into it, and one page in the chat double;
4. resync `raw.orders` from Postgres, the team's fix, trigger again: the run succeeds, the incident is resolved by it, and the double holds the one resolution.

Two runs from a clean stack (no containers, no `airflow` database) on the development laptop passed in 9 min 21 s and 9 min 40 s. The faulted run took 152 s and 162 s, and its 70 invocations (freshness and every task that ran; what is below `stg_orders` did not) became one incident with one page; the run after the fix, all 224 tasks, took 364 s and 374 s. The Airflow image is 5.51 GB unpacked.

It seeds `af_` anew at scale 0.01, so run `make airflow` afterwards for the scale 0.1 company.
