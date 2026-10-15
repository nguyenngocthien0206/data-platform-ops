# 0017: Orchestrators call the toolkit through its CLI, each in its own image and namespace

Status: accepted
Date: 2026-10-15

## Context

Phase 14a gave the incident desk one entry point for any dbt run: `platform-ops incidents ingest --target <dir> --run-id <id>`, where invocations that share a run id are one run (ADR 0016). The company the toolkit is built for schedules dbt with Airflow, through Cosmos, which runs one dbt invocation per model, and with Dagster, through dagster-dbt. Phase 14b connects both for real, in Docker, and tests them end to end.

The obvious way is a library: import the toolkit inside the DAG or the asset function and call the desk. That ties three dependency sets together. Airflow ships constraint files that pin hundreds of packages; Dagster pins its own; the toolkit pins dbt, pandas, Streamlit and the rest through `uv.lock`. Every upgrade of any of them would have to satisfy all three, and a conflict would surface in the company's scheduler, not in the toolkit's CI.

The owner decided at planning: Airflow 3 (Airflow 2 reached its end of life in April 2026), each orchestrator running its own company in its own namespace, a real 02:00 schedule paused by default, and the work in three parts (the foundation, Airflow, Dagster) so each session ends in a state that can be committed.

## Decision

- **The contract is the CLI.** `platform_ops.integrations` holds two small hooks, standard library only:
  - `cosmos.ingest_after_task`, Cosmos's operator callback;
  - `dagster.ingest_dbt_invocation`, for a `@dbt_assets` function.
  
  Each runs `platform-ops incidents ingest` from the toolkit's own environment (`PLATFORM_OPS_CLI`). The orchestrator images install the toolkit in a virtual environment of its own, from its own lock file, beside the orchestrator's.
- **No orchestrator is ever imported.** The hooks take what the orchestrator hands them: a target folder and a run id. A test fails if any module of the toolkit imports Airflow, Cosmos or Dagster, or if the hooks import anything outside the standard library.
- **Run ids say where they come from.** `airflow:<dag id>:<run id>` comes from the task context, else from the `AIRFLOW_CTX_*` variables Airflow sets for every task. `dagster:<run id>` comes from the asset context. Every invocation of one scheduled run lands in one incident run.
- **Incident tracking never fails a pipeline.** A failed or hung ingest is logged and returned, never raised. A broken toolkit must not stop the company's data.
- **Each orchestrator runs its own company** in its own namespace (`af_`, `dg_`), set by `PLATFORM_OPS_NAMESPACE` on the same config file, with its own incident store. `make demo` and its byte-identical reports are never touched.
- **The schedule is real and paused.** Each stack carries the company's 02:00 schedule, paused by default; the tests trigger runs through each orchestrator's API.
- **Pages are checked against a double.** A small service answers like Slack's `chat.postMessage` and Telegram's `sendMessage`, refuses what they refuse, and keeps every message for the tests to read.
- **Two layers of tests.** Pull requests test the hooks against a fake CLI, the double against the toolkit's own channels, and the hooks once against the real CLI and store. The real orchestrators are tested on demand and before every release.

## Consequences

A company wires its own scheduler to the toolkit with one callback or one function call and an environment variable, and upgrades Airflow, Dagster or the toolkit independently. The cost is a process start per invocation (well under a second next to a dbt invocation), and a second virtual environment in each orchestrator image.

The hooks are deliberately thin. Everything that decides what an incident is stays in the desk, so a Cosmos run, a Dagster run and the simulated company's nights are graded by the same code, and the orchestrators only prove they hand the right folder and the right run id.
