# orchestrators

How a scheduler that runs dbt hands each run to the incident desk ([ADR 0020](../docs/adr/0020-orchestrators-call-the-toolkit.md)). The company the toolkit is built for runs dbt with Airflow through Cosmos and with Dagster through dagster-dbt. Phase 14b brings both up for real, in Docker, each on a company of its own: [Airflow with Cosmos](#airflow-with-cosmos) and [Dagster with dagster-dbt](#dagster-with-dagster-dbt), tested [end to end](#tested-end-to-end) before every release.

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

## Dagster with dagster-dbt

`dagster/` runs the same project in Dagster 1.13 through dagster-dbt 0.29, as its own company in the `dg_` namespace:

```bash
make dagster          # seeds dg_ (scale 0.1), starts Dagster and the chat double
```

The UI is at <http://localhost:3001>. Dagster's open source UI has no login, so the port is published on this machine only. The job `company_dbt` runs at 02:00 UTC every day once its schedule is started; it is stopped until then, and Dagster never runs the nights it missed.

What is in it:

- **Its own image** (`dagster/Dockerfile`, from the toolkit's `python:3.11.16-slim-bookworm`). Dagster's environment is installed from `dagster/requirements.txt`, pinned and hashed (compiled from `requirements.in`). dagster-dbt brings a dbt-core of its own, which reads manifests and never runs; the dbt it runs is the toolkit venv's (`dbt_executable`), as with Cosmos. The toolkit package goes into Dagster's environment without its dependencies, for the hook.
- **The definitions** (`dagster/app/company_dbt.py`): `@dbt_assets` over a manifest parsed when the image is built. The asset function runs source freshness on the company's clock, then one `dbt build`, both with `raise_on_error=False` so the hook always gets the artifacts, hands each to `ingest_dbt_invocation`, and fails the run afterwards when dbt failed, so the UI shows it red.
- **The instance** (`dagster/app/dagster.yaml`): runs, events and schedules in a `dagster` database on the company's Postgres, created at start (`dagster/start.py`); the driver is named (`postgresql+psycopg2`) because SQLAlchemy 2.1 defaults to psycopg 3. `dagster dev` runs the webserver and the daemon in one container, the way `airflow standalone` does: fine for one machine. A real deployment runs them as separate services.
- **The same least privilege** as Airflow: the Postgres login and the company's `dbt` user (`dg_dbt`), no `.env`.

Wiring your own `@dbt_assets` takes one call after each invocation:

```python
from platform_ops.integrations.dagster import ingest_dbt_invocation


@dbt_assets(manifest=...)
def project(context, dbt):
    build = dbt.cli(["build"], context=context, raise_on_error=False)
    yield from build.stream()
    ingest_dbt_invocation(build, context)
```

Keep `from __future__ import annotations` out of that module: Dagster reads the `context` annotation as a type.

## Tested end to end

`make docker-test-orchestrators` runs `scripts/check_orchestrators.py` in the toolkit container against each running stack in turn (`ORCHESTRATORS=airflow` or `dagster` for one), on its company seeded anew at scale 0.01. The `orchestrators` workflow runs it on demand, one runner per orchestrator, and the release waits for it. The cycle is the same for both:

1. plant F1, NULLs in `raw.orders.customer_id`;
2. start `company_dbt` through the orchestrator's API (Airflow's REST API v2 with a JWT from `/auth/token`; Dagster's GraphQL `launchRun`) and wait: the run fails on `stg_orders`'s not_null test;
3. check one incident at `model.company.stg_orders`, opened by that run, every invocation of the run grouped into it, and one page in the chat double;
4. resync `raw.orders` from Postgres, the team's fix, run again: the run succeeds, the incident is resolved by it, and the double holds the one resolution.

Two runs from a clean stack (no containers, no `airflow` or `dagster` database) on the development laptop:

| | Run 1 | Run 2 |
|---|---|---|
| Both checks, one after the other | 13 min 13 s | 12 min 55 s |
| Airflow: the check | 531 s | 511 s |
| Airflow: the faulted run (70 invocations: freshness and every task that ran) | 162 s | 153 s |
| Airflow: the run after the fix (224 tasks) | 364 s | 353 s |
| Dagster: the check | 161 s | 153 s |
| Dagster: the faulted run (2 invocations) | 73 s | 63 s |
| Dagster: the run after the fix | 83 s | 83 s |

Each time F1 put 66 NULLs in `raw.orders`, and each run gave one incident (INC-001 at `stg_orders`, SEV1, paged to priya, who owns the feed), one page and, after the fix, one resolution.

Airflow runs one dbt invocation per Cosmos task, so its runs take minutes and hand the desk dozens of invocations; Dagster runs one build, so it hands the desk two (freshness and the build). The desk gives the same answer from both: one incident, one page, one resolution. The Airflow image is 5.51 GB unpacked, the Dagster image 2.12 GB.

It seeds `af_` and `dg_` anew at scale 0.01, so run `make airflow` or `make dagster` afterwards for the scale 0.1 companies.
