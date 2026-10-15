# PROGRESS

Handoff file between sessions. Read this first, then `docs/SPEC.md`.

Last updated: 2026-10-15

## Current phase

Phase 15: documentation and release 2.0.0. One branch and one pull request for the whole phase (owner, 2026-10-15); the work is done in three sessions, 15a, 15b and 15c, a convention between the owner and the assistant only. 15a (the architecture diagram and the screenshot tooling) is committed on the branch; 15b (the README around 2.0 and an adoption guide per module) and 15c (the release run and 2.0.0) follow. Phases 11a to 14 are merged (PRs #15 to #23).
Branch: `phase-15` (off `main` at `3b8b38d`).

Phases 0 to 9 are released as 1.0.0, Phase 10 as 1.1.0 and the registry bootstrap as 1.2.0. Version 2.0 (Phases 11 to 15) rebuilds the simulated company on Postgres and ClickHouse.

## Done

- Phase 15a implemented and committed (branch `phase-15`):
  - **Logos from each vendor's own files, in full colour** (`docs/images/logos/`, 13 files, 66 KB): PostgreSQL, ClickHouse, dbt, Airflow, Dagster, Metabase, Streamlit, Slack, Telegram, Docker, GitHub, BigQuery, Snowflake. `LICENSES.md` gives each one's source, the date and the guidelines; nothing is modified, the two PNGs only scaled to 256 px.
  - **The architecture diagram** (`docs/images/architecture.svg`, 119 KB) drawn by `scripts/make_architecture.py` (`make docs-diagram`) in the style of the owner's template: a title, a legend, the company's flow in six numbered steps (legacy Postgres, the incremental copy, ClickHouse, dbt run nightly by Airflow or Dagster, Metabase, the teams), the toolkit's lane (reconcile, cost, incidents on the shared metadata layer, the ops store, the dashboards, pages to Slack and Telegram), and three panels underneath (the sign-off, the bill, one page per problem). Its numbers are read from the dbt project and the config; logos are embedded as data URIs so the SVG stands alone; a text that does not fit its box fails the script.
  - **Screenshots**: `scripts/screenshot_orchestrators.py` (the `docs-screens` Compose service, in the Playwright image) opens Airflow with a JWT set as its `_token` cookie, expands the task groups and photographs the grid, then Dagster's runs and its latest failed run; `scripts/curate_screens.py` picks 11 screens (4 Streamlit pages, a Metabase dashboard per team, Airflow, two of Dagster) into `docs/images/screens/`, light theme, cut to a README height, optimised PNG, under a 3 MB budget. `make docker-docs-images` runs the three after `docker-demo`, `docker-metabase-check` and `docker-test-orchestrators`.
  - `tests/test_docs_images.py`: the committed diagram equals what the script draws, its numbers come from the project, every vendored logo is listed in `LICENSES.md` and every listed one is vendored, overflowing text fails, and the curation cuts and names each screen and names the target that makes a missing one.
- Phase 15a verification: `make lint` clean; `make docker-test`: 429 passed, nothing skipped, 10 min 34 s. The diagram was rendered in Chromium as GitHub shows it (an `<img>`) and checked by eye; `make docker-docs-images` ran end to end on the Phase 14 demo state and the orchestrator stacks: 8 of 8 dashboard page loads passed, 3 orchestrator screenshots, 11 screens in 1.30 MB. The screenshots in `docs/images/screens/` are not committed in 15a: the committed set comes from the 2.0.0 release run (15c).
- Phase 14b-3 implemented and merged (PR #23):
  - **The Dagster image** (`orchestrators/dagster/Dockerfile`, `python:3.11.16-slim-bookworm`, 2.12 GB unpacked): Dagster 1.13.25 and dagster-dbt 0.29.25 from a pinned, hashed `requirements.txt` (compiled from `requirements.in` with `uv pip compile`); the toolkit in its own venv at `/opt/toolkit`, whose dbt dagster-dbt runs (`dbt_executable`); the toolkit package without dependencies in Dagster's environment, for the hook; the manifest parsed at build time.
  - **The definitions** (`orchestrators/dagster/app/company_dbt.py`): `@dbt_assets` over the manifest; source freshness on the company's clock, then one `dbt build`, both with `raise_on_error=False`, each ingested with `ingest_dbt_invocation` under `dagster:<run id>`; the run fails afterwards when dbt failed. Job `company_dbt`, schedule `company_dbt_nightly` at 02:00 UTC, stopped by default.
  - **The stack** (Compose profile `dagster`, `start.py`, `dagster.yaml`): `dagster dev` (webserver and daemon), storage in a `dagster` database on the company's Postgres, created at start; UI on 127.0.0.1 only (`DAGSTER_PORT`, default 3001), since it has no login. Same least privilege as Airflow (`dg_dbt`).
  - **`make dagster`** (seeds `dg_` at `DAGSTER_SCALE`, default 0.1); **`make docker-test-orchestrators`** runs each of `ORCHESTRATORS` (default `airflow dagster`) in turn; `scripts/check_orchestrators.py` runs one cycle for both, each orchestrator bringing its client (Airflow's REST API, Dagster's GraphQL `launchRun` and `runOrError`).
  - **The release gate**: `orchestrators.yml` runs on `workflow_call` too, one runner per orchestrator (a matrix); `release.yml`'s `publish` needs `orchestrators`. `test_release` keeps it that way.
  - Fixed from 14b-2: the Makefile's line continuations had been lost (commands joined with tabs, still working), and `.PHONY` never listed `airflow`.
  - The orchestrators README (Dagster, wiring your own `@dbt_assets`, the results of both); the incidents README ("On a real dbt project": the hooks); README status (Phase 14 done), commands, layout and a results paragraph; SPEC; `.env.example`.
- Phase 14b-3 verification: `make lint` clean; `make docker-test`: 423 passed, nothing skipped, 10 min 33 s. A manual run of `company_dbt` in Dagster on a healthy `dg_` company at scale 0.01 succeeded in 69 s, its two invocations (freshness, 6 checks; the build, 290 results) in one run. `make docker-test-orchestrators` (both) from a clean stack (no containers, no `airflow` or `dagster` database), twice: passed in 13 min 13 s and 12 min 55 s. Airflow: the check 531 s and 511 s, the faulted run 162 s and 153 s (70 invocations), the run after the fix 364 s and 353 s. Dagster: the check 161 s and 153 s, the faulted run 73 s and 63 s (2 invocations), the run after the fix 83 s both times. Every time: F1 planted 66 NULLs, one incident (INC-001 at `model.company.stg_orders`, SEV1, priya), one page, and after the resync one resolution, nothing else. `make demo` is unchanged, so the 14a runs stand.
- Phase 14b-2 implemented and merged (PR #22):
  - **The Cosmos hook fixed**: Cosmos 1.15 calls `callback(tmp_project_dir, context=context, **callback_args)`, after dbt and before it raises, so it runs on a failed task too; `ingest_after_task` now reads the task context from that keyword (14b-1 read only the top level and fell back to `AIRFLOW_CTX_*`).
  - **`platform-ops company clock`** (`simulation.ingest.company_clock`): the company's simulated time, where its latest load reached. An orchestrator's freshness is measured against it (owner's choice A), because the company's data lives on simulated time.
  - **The Airflow image** (`orchestrators/airflow/Dockerfile`, `apache/airflow:3.3.2-python3.11`, 5.51 GB unpacked): Cosmos 1.15.1 against Airflow's constraint file; the toolkit in its own venv at `/opt/toolkit` from `uv.lock`, whose dbt Cosmos runs; the toolkit package without dependencies in Airflow's environment, for the hook; the manifest parsed at build time.
  - **The DAG** `company_dbt` (`orchestrators/airflow/dags/company_dbt.py`): a `DbtTaskGroup` from the manifest, `TestBehavior.AFTER_EACH`, tests on two models detached into their own task, `ingest_after_task` on every task; a source freshness task beside it on the company's clock; 02:00 UTC, paused at creation, `max_active_runs=1`, 8 tasks at once.
  - **The stack** (Compose profile `airflow`, `start.py`): `airflow standalone`, the `airflow` metadata database created on the company's Postgres, its URL from `start.py --connection` (`SQL_ALCHEMY_CONN_CMD`), the admin password from `AIRFLOW_ADMIN_PASSWORD` (no default). The container gets the Postgres login and the company's `dbt` ClickHouse user only, not `.env`; pages go to the chat double.
  - **`make airflow`** (seeds `af_` at `AIRFLOW_SCALE`, default 0.1, starts `chat` and `airflow`), **`make docker-test-orchestrators`** (`scripts/check_orchestrators.py`), and the `orchestrators` workflow (`workflow_dispatch`).
  - `test_architecture`: `platform_ops/__init__.py` must import the standard library only too, since Airflow's environment holds the package without its dependencies.
  - `orchestrators/README.md` (the Airflow stack, wiring your own Cosmos DAG, the end-to-end check); README status, commands and layout; `.env.example`.
- Phase 14b-2 verification: `make lint` clean; `make docker-test`: 422 passed, nothing skipped, 11 min 2 s. A manual run on a healthy `af_` company at scale 0.01 succeeded in 6 min 22 s (224 tasks). `make docker-test-orchestrators` from a clean stack (no containers, no `airflow` database), twice: passed in 9 min 21 s and 9 min 40 s. Each time F1 planted 66 NULLs; the faulted run failed in 152 s and 162 s, its 70 invocations became one incident (INC-001 at `model.company.stg_orders`, SEV1, priya) with one page in the chat double; after the resync the next run succeeded in 364 s and 374 s, resolved INC-001, and sent one resolution, nothing else. `make demo`, `company`, `dbt/` and the desk are unchanged, so the 14a runs stand.
- Phase 14b-1 implemented and merged (PR #21):
  - **`PLATFORM_OPS_NAMESPACE`** replaces `company.namespace` for one run, as `PLATFORM_OPS_SCALE_FACTOR` does the scale: another company from the same config file.
  - **`platform_ops.integrations`**, standard library only: `ingest` runs `platform-ops incidents ingest` (`PLATFORM_OPS_CLI`, `PLATFORM_OPS_REGISTRY_DIR`), passes the run id and an optional detection time as naive UTC, and logs a failure or a hang, never raising it; `cosmos.ingest_after_task` (run id `airflow:<dag>:<run>` from the task context or `AIRFLOW_CTX_*`); `dagster.ingest_dbt_invocation` (run id `dagster:<run>`).
  - **The chat double** (`orchestrators/chat_double.py`, Compose profile `chat`): answers like Slack's `chat.postMessage` and Telegram's `sendMessage`, refuses a missing token or chat, keeps every message.
  - `test_architecture`: `integrations` imports nothing from the toolkit and nothing outside the standard library; no module imports Airflow, Cosmos or Dagster.
  - ADR 0020; `orchestrators/README.md`; README status and layout; `.env.example`.
- Phase 14b-1 verification: `make lint` clean; `make docker-test`: 419 passed, nothing skipped, 11 min 7 s. `make demo` is unchanged, so the 14a runs stand. The hooks were also run once against the real CLI and store with the shop project's artifacts: one incident opened, a second ingest found nothing new.
- Phase 14a implemented (branch `phase-14a-incidents`, uncommitted):
  - **Three production weeks after the parallel run** (`company.production_weeks: 3`; `simulation.weeks` 13 to 16). Every production night runs `dbt source freshness`, `run`, `snapshot` and `test` as separate invocations, the project parsed once and the parse reused; a failing check is recorded, not fatal. Each invocation's target is cleared of older result files first.
  - **Faults in operation** (`simulation/faults.py`, rewritten): eight on four days, two a day, planted in ClickHouse raw after the day's work; orphan keys replace duplicate keys. A stale feed and a changed raw table stop the loader for that table (`Ingestion.load(skip=...)`); the fix resyncs the table from Postgres (`Ingestion.resync`). Ground truth in `ops.incident_faults`.
  - **The incident store** (`incidents/store.py`): versioned migrations, one advisory lock per ingest, one open incident per root by a partial unique index.
  - **The desk** (`incidents/desk.py`): `ingest_target` reads one invocation's artifacts from any adapter, keyed by dbt's invocation id, regroups the whole run on every ingest, opens, appends, merges and resolves (a later run that covered the root and found it healthy), queues notifications and delivers them after commit. `incidents/production.py` is the company's desk: it plays the owners (ack and fix from `lifecycle`) and hands the company each fix.
  - **Notifications** (`incidents/notify.py`): Slack (`chat.postMessage`, `SLACK_BOT_TOKEN`) and Telegram (`sendMessage`, `TELEGRAM_BOT_TOKEN`) per team; local by default; the company's own pages always local. The 1.x webhook is gone.
  - **Policy** from an optional `incidents.yaml` next to the registry (`IncidentPolicy`).
  - **CLI**: `incidents ingest --target --run-id [--at] [--registry-dir]`, `incidents run` (the report from the store), `incidents summary`. Nothing is paused any more; `make demo` runs `incidents run` before `cost report`.
  - **The Incidents page is back**, reading the store; the browser check covers four pages.
  - Removed: the 1.x scenario (`incidents/scenario.py`) and its DuckDB persistence, `grouping.assign`.
  - Row-count bounds widened for 16 weeks; `support_tickets` freshness switched off (bursty at small scale).
  - Fixture artifacts from a Snowflake build and a BigQuery night of a small shop project (`tests/fixtures/dbt_artifacts`, `scripts/make_artifact_fixtures.py`).
  - ADR 0019, a later note on ADR 0008; SPEC 14a note; incidents, simulation, dashboards and root READMEs.
- Phase 14a verification:
  - `make lint` clean. `make docker-test`: 404 passed, nothing skipped, 10 min 48 s (the shared test company is now three weeks).
  - `make docker-clean docker-demo` at scale 1.0: 56 min 11 s and 57 min 17 s (45 min in Phase 13). Two clean runs: 195 of 196 relations identical, the incident store included once dbt's invocation ids are left out; `ops.company_run` differs by its random run id. `incidents.md`, both postmortems, `cost.md` and `reconciliation.md` byte-identical.
  - A first pair of clean runs left every report identical but differed by 80 bytes read in one snapshot upsert, and paged twice for F5; both fixed (Decisions above) before the pair recorded here.
  - The production weeks: 21 nights, 9 with failing checks; 8 faults, 34 failing checks, 8 incidents, 8 pages, 4 updates; every fault one incident, every incident resolved; routing right 8 of 8 (naive 3 of 8); MTTD 16.0 h for every fault but the stale feed (64.0 h); MTTR 24.0 h (SEV1), 33.6 h (SEV2), 72.0 h (SEV3). Pages before grouping: marco 28, after 2; priya 2 before, 6 after (the owner of most feeds).
  - Cost over 112 days: $1,197.24 stated and shared out exactly; 34,207 queries; BigQuery $4.08, Snowflake $363.71; the business case unchanged (payback 3.5 months).
  - History at scale 1.0 with the 16-week window: 3,507,804 rows (61 fewer than 13 weeks: the id space is fitted to the window), seeded in 34.6 s; 18,472 late payments, 801 duplicate customer emails.
  - `make docker-browser-check`: 8 of 8 page loads (four pages, light and dark), screenshots read.
- Phase 13b implemented (branch `phase-13b-dashboards`, uncommitted):
  - **The cost report stores everything it states**: `ops.cost_months`, `cost_business_case`, `cost_business_summary`, `cost_unused`, `cost_hotspots`, `cost_incremental`, `cost_quotas`, written from the same data as `reports/cost.md`.
  - **A read-only role for the dashboards** (`common/store.py`): `dashboard`, `SELECT` on the ops schema (default privileges cover the tables every run recreates), read-only sessions by default, its password from `POSTGRES_PASSWORD_DASHBOARD` with no default. `company run` checks the password before writing anything and creates the role after the ClickHouse users; `remove_company` drops it.
  - **The data layer reads Postgres** (`dashboard/queries.py`: `Store`, queries against `{ops}`, a missing table names its command, a missing password or server says what to do).
  - **Three pages**: Overview (the run, one headline per module, ownership), Cost (model switch, showback by team and month, by workload, the business case, the four recommendation tabs), Reconciliation (the verdict, the 42-night grid against the 21 pairs, one night in detail, rows moved segmented against naive). The Incidents page is removed until Phase 14.
  - `make dashboard` runs again; `scripts/check_dashboards.py` checks the three pages and waits for a page to stay rendered.
  - dashboards README rewritten; later notes on ADR 0016; SPEC 13b note; root README.
- Phase 13b verification:
  - `make lint` clean. `make docker-test`: 387 passed, 6 skipped (incidents until Phase 14), 6 min 48 s; the dashboard tests render every page on the shared test company and check the role.
  - `make docker-clean docker-demo` at scale 1.0: 44 min 51 s and 44 min 35 s. Two clean runs: 184 of 185 relations identical, the seven new cost tables and the dashboard role's 24 grants included; `ops.company_run` differs by its random run id, as in 13a. `cost.md` and `reconciliation.md` byte-identical, and `cost.md` identical to 13a's.
  - `make docker-browser-check` on the demo: 6 of 6 page loads (three pages, light and dark), and twice more from a cold dashboard; every screenshot read.
- Phase 13a implemented (branch `phase-13-cost`, uncommitted):
  - **Every query the company runs is tagged** (`common/tagging.py`) through ClickHouse's `log_comment`: run, simulated time, kind of work (dbt, ingestion, maintenance, reconcile, dashboard, ad hoc), subject and person. dbt carries it through its profile's `custom_settings` (`PLATFORM_OPS_LOG_COMMENT`), the others per query.
  - **The company's days gained** the nightly merge of raw (`OPTIMIZE TABLE ... FINAL`, `ops.raw_maintenance`), the record of in-place source changes (`ops.source_changes`), the daily workload (`simulation/workload.py`: every Metabase card on its refresh schedule as `metabase`, ad hoc SQL per person as their team's user, `FORMAT Null`, one thread) and month-end storage snapshots (`cost/storage.py`, `ops.storage_snapshots`).
  - **The cost module rebuilt on ClickHouse**: `collect.py` reads the run's tagged queries from `system.query_log` (billable only, literals redacted, numbered in a stable order); `cluster.py` shares the stated monthly cost out exactly; `pricing.py` prices the same bytes on BigQuery and Snowflake; `attribution.py` adds ingestion, maintenance and the reconciliation (migration cost); `business.py` builds the replica-to-ClickHouse business case on the reconciliation's cutover; `recommend.py` finds unused tables, hotspots (with a suggested sort key, or a skip index on a `ReplacingMergeTree`), incremental candidates and quotas; `report.py` writes `reports/cost.md`. Results in `ops.query_log`, `query_filters`, `query_costs`, `query_attribution`, `cost_showback`.
  - Settings: `cost.clickhouse` and `cost.legacy_reporting` (public list prices with sources), warehouses for the new kinds of work.
  - `make cost` runs (no longer paused) and `make demo` ends with it; `platform-ops simulation run` and `make simulate` are gone. Removed: `cost/estimate.py`, `sizes.py`, `growth.py`, `schema.py`.
  - ADR 0018, later notes on ADRs 0005 to 0007; SPEC Phase 13 updated; cost README rewritten; simulation and root READMEs updated.
- Phase 13a verification:
  - `make lint` clean. `make docker-test`: 361 passed, 31 skipped (dashboard until 13b, incidents until 14).
  - Two runs at scale 0.01 in their own namespaces: every cost ops table and `cost.md` identical; every kind of work measured the same bytes and rows.
  - `make docker-clean docker-demo` at scale 1.0: 44 min 35 s and 44 min 50 s (38 min in Phase 12; the daily dashboards and ad hoc SQL, the nightly merge and the cost report). `cost report` alone: about 13 s in the container. Two clean runs: 176 of 177 relations identical (every ClickHouse table and view, the legacy tables and reports, every ops table including the cost tables, every user's grants); the one that differs is `ops.company_run`, whose run id is random by design, since runs share ClickHouse's query log. `cost.md` and `reconciliation.md` byte-identical. A first pair of clean runs differed by a few cents, which led to the one-part-per-partition decision above.
  - At scale 1.0: 25,549 billable queries priced; the cluster's $973.02 over 91 days shared out to the cent and beyond; showback platform $529.89, sales $144.47, finance $115.15, marketing $101.06, product $82.45; BigQuery $2.97 and Snowflake $295.25 on the same workload. Ad hoc SQL $137.51 against dashboards $3.12 on the cluster ($110.14 against $167.90 on Snowflake); the nightly merge $209.44, a quarter of the compute. Business case: replica $577.50 a month against ClickHouse $320.32, overlap until the 2026-03-30 cutover $909.30, payback 3.5 months. Unused list exactly the 12 abandoned models; 3 hotspots; no incremental candidate; quotas from 12.0 MB a day (Metabase) to 850.0 MB (sales).
  - Incremental models in bytes read: the two marts read 77% less in their last build than their first; the two intermediate ones read more each build (68.0 MB to 105.2 MB for `int_order_items_enriched`), because they filter staging views over raw, sorted by key.
- Phase 12 implemented (branch `phase-12-reconcile`, uncommitted):
  - **The parallel run is the last 6 weeks of the company's window** (`company.parallel_run_weeks`). The ingestion runs as the migration team delivered it from day 0, with four defects (`company.delivered_job`, `simulation/ingest.py`): payments' `created_at` read as New York time, payments' `amount` through a float and truncated, customers' `email` right-trimmed, the last key of every 25,000-key page of web sessions lost. Seven nights into the parallel run the fixed job ships and backfills raw (`Ingestion.backfill`, keeping the load log). Seven one-off faults follow every other night (`simulation/migration.py`), planted after the ingestion and before the build, repaired the next day.
  - **Every night of the parallel run** dbt builds, and `reconcile/parallel.py` compares the 9 operational tables (Postgres `app` against ClickHouse `raw` with `FINAL`) and the 12 legacy reports (against the marts), six pairs at a time, as the `platform` user. Table differences are graded against the night's ground truth; report differences are explained through dbt lineage or flagged as logic differences. The cutover is recommended after 14 consecutive clean nights. Nights are recorded in `ops.reconcile_nights`, `_examples`, `_grades`, `_labels`, `_cutover`.
  - **The reconciliation core rebuilt for 2.0**: a ClickHouse renderer byte-identical to Postgres and Python (golden-row test with a `FixedString` column), segments by the MD5 of the canonical key so dates and composite keys segment like integers, pairs declared in `config/reconcile.yaml` with columns and types read from both catalogues (`reconcile/specs.py`).
  - `make demo` replays the parallel run; `make reconcile` (`platform-ops reconcile run`) writes `reports/reconciliation.md` from the recorded nights; `--strict` fails when the cutover is not recommended. Reconcile is no longer paused.
  - Removed: Iceberg (`pyiceberg`, `migrate.py`, `legacy.py`, the `iceberg_warehouse` path), the DuckDB connector, the 1.x run and its faults.
  - Two marts now follow their legacy reports (found while building the comparison, before any defect was planted): `marketing_fct_campaign_performance.return_on_ad_spend` divided at the dividend's scale (ClickHouse truncated where Postgres rounded) and gave 0 where the legacy report gives NULL for a campaign with no attributed order.
  - `PLATFORM_OPS_SCALE_FACTOR` overrides the scale for one run; `make docker-demo SCALE=0.1`; the release workflow's Metabase job runs the demo at 0.1 with a 90-minute timeout.
  - ADR 0017 (the parallel run and the cutover rule), a later note on ADR 0009; SPEC Phase 12 acceptance updated; reconcile and simulation READMEs and the root README updated.
- Phase 12 verification:
  - `make lint` clean. `make docker-test`: 354 passed, 37 skipped, 5 min 28 s; every skip is a paused module (cost, dashboards, incidents).
  - `make docker-clean docker-demo` at scale 1.0: 37 min 56 s and 38 min 37 s (from 4 min 35 s in Phase 11b). 44 dbt builds, 10,142,605 rows ingested (the backfill reloads everything once), 141 datasets owned, 376 lineage edges.
  - Two clean runs: every relation identical across 168 entries (the ClickHouse tables and views, the legacy tables and reports, the ops tables including the reconciliation's, and every user's grants), and `reports/reconciliation.md` byte-identical.
  - The sign-off: 42 nights from 2026-02-23 to 2026-04-05; the delivered job's 7 nights fail (476,961 timezone shifts, 26,335 truncated amounts, 351 trimmed emails, 74 lost sessions on the first night, all found); the fix on 2026-03-02; each one-off fault found on its night; cutover recommended on 2026-03-30. Over the run 3,576,917 planted discrepancies, 3,576,917 detected, 100% recall, precision and classification accuracy; no report difference without an upstream cause. M6 (six amounts off by a cent) was found but is within the rounding threshold, so its night counted as clean, and the report says so.
  - Segmented against naive: on matching pairs 11,200 rows moved instead of 378,892,260 for the tables (99.997%), 14,422 instead of 39,488,818 for the reports; on differing pairs 79.822% saved for the tables, 12.577% for the reports.
  - One night's reconciliation on the demo at scale 1.0: 102 s one pair at a time, 23 s six at a time.

- Phase 11b implemented (branch `phase-11b-metabase`, uncommitted):
  - **Users** (`simulation/access.py`): one ClickHouse user per team plus `metabase`, `dbt` and `loader`, namespaced, created again (`CREATE USER OR REPLACE`) at the start of `seed`, `build` and `company run`. `reader` profile (`readonly = 2`) for teams and Metabase, `writer` for the pipelines, both with the dbt profile's session settings. Business teams and Metabase read marts and reference; platform reads every layer; dbt reads raw and owns its databases plus `system.parts`; loader inserts into raw with `READ ON POSTGRES` and `CREATE TEMPORARY TABLE`; Metabase also reads `system.processes` behind a row policy limiting it to its own queries.
  - dbt runs as `dbt` (`common/dbt_invoke.py`) and the ingestion as `loader` (`simulation/ingest.py`). Passwords come from `CLICKHOUSE_PASSWORD_<USER>`, with no default; a missing one exits 2 naming every missing variable (`common/servers.py`).
  - **Dashboards** (`simulation/bi.py`): one per exposure in its owning team's collection, one native SQL card per model. Rules: a generated rollup is a line per period and dimension value, a daily one over its last 91 days; anything else a table sorted by its key; a `dashboard` entry in the model's `config.meta` overrides (daily sales, monthly revenue, acquisition funnel carry one).
  - **Metabase** (`simulation/metabase.py`, Compose profile `metabase`, `metabase/metabase:v0.63.19.6`, its state in a `metabase` database on Postgres, 1 GB heap): `platform-ops metabase prepare | provision | check`; provision is idempotent, updates by name and archives what no exposure asks for. `make metabase` natively; `make docker-metabase-check` adds `scripts/screenshot_metabase.py` in the Playwright image. The release workflow runs a demo and the Metabase check before publishing.
  - ADR 0015 (ClickHouse modelling and watermark ingestion, the 11a record) and ADR 0016 (users and BI); README, simulation README and `.env.example` updated.
- Phase 11b verification:
  - `make lint` clean. `make docker-test`: 340 passed, 38 skipped (all paused modules, with their phase), 2 min 48 s. Native full suite 4 min 14 s.
  - `make docker-clean docker-demo` at scale 1.0: 4 min 41 s and 4 min 32 s. The two runs are identical across 163 entries (the 155 relations of 11a plus every user's grants), and the data is identical to the 11a demo: running dbt and the ingestion as their own users changed nothing in the results.
  - `make docker-metabase-check` from a fresh Metabase, after the demo: 2 min 39 s. 12 dashboards and 69 cards in four team collections, all 69 cards run through Metabase without an error (23,349 rows), all 12 dashboards rendered in headless Chromium. Natively: Metabase healthy in 68 s, provision 21 s, check 17 s; a second provision changes nothing.
  - ClickHouse's query log shows every dbt query under `dbt` with its node comment and every load under `loader`; after the row policy, the `metabase` user leaves no errors in the log.
  - The screenshots found a chart cut off at Metabase's 2,000-row display limit (a year of daily orders by country, about 4,600 rows). Daily charts now cover 91 days (at most 910 rows), and the check fails any chart that reaches the limit.

- Phase 11a implemented (branch `phase-11-replatform`, uncommitted):
  - **Legacy Postgres** (`simulation/legacy_db.py`): the DuckDB generator stays as an in-memory scratch engine and its tables are copied into the `app` schema of the legacy database with `COPY`, one primary key per table and an `updated_at` column. Each simulated day upserts the day's new rows and the in-place changes (`INSERT ... ON CONFLICT DO UPDATE`).
  - **Legacy reports** (`simulation/legacy_reporting/*.sql`): 12 hand-written Postgres views in the `reporting` schema, one per dbt exposure, plus two shared helper views.
  - **Incremental ingestion** (`simulation/ingest.py`): `INSERT INTO raw.<t> SELECT ... FROM postgresql(...)` for rows with `updated_at` in (watermark minus 60 minutes, now]. Raw tables are `ReplacingMergeTree(updated_at) ORDER BY` the key, unpartitioned; staging reads them with `FINAL`. Every load is recorded in `ops.ingestion_loads`.
  - **dbt on ClickHouse**: `dbt-clickhouse` replaces `dbt-duckdb`. Staging is views; every other model is a `MergeTree` table sorted by its key. The four event tables (`int_order_items_enriched`, `int_sessions_enriched`, `sales_fct_order_items`, `marketing_fct_sessions`) are incremental with `insert_overwrite` on monthly partitions, rebuilding the current and previous month. The 57 generated models come from the updated `codegen.py`; the 61 hand-written ones were ported by hand. The project gained a seed (`country_codes`) and a snapshot (`snap_customers`).
  - **`platform-ops company run`** replays the window: seed and backfill, then every day the new data, ingestion at 02:00, and a dbt build every 28 days and on the last day (5 builds). `seed` and `company run` start by dropping every company database in ClickHouse.
  - **The ops store** is the Postgres database `platform_ops`, schema `ops` (`common/store.py`). `metadata check` and `metadata lineage` write there.
  - **Ownership** runs on the company project; the ClickHouse fixture project is deleted and its standalone tests run on the company project (`tests/test_ownership_standalone.py`).
  - **Paused until their phase**: reconcile (12), cost, simulation and dashboard (13), incidents (14). Their commands exit 2 naming the phase and `v1.2.0`; `platform-ops paused` and `make paused` list them; their end-to-end tests skip with the same reason; their unit tests still run.
  - **SQL Server removed**: Compose service, `pymssql`, `sqlserver.yml`, its tests, and its code in the reconciliation (connector, renderers, DDL, Windows zone names). `dbt-duckdb` removed; `dbt-clickhouse` is a main dependency. ClickHouse is no longer behind a profile; `make up` starts Postgres and ClickHouse.
  - README banner (2.0 in progress, the numbers below are 1.x, `v1.2.0` has the 1.x demo); ADR 0001 marked superseded; ADR 0014, the metadata and reconcile READMEs updated.
- Phase 11a verification:
  - `make lint` clean. `make docker-test`: 323 passed, 38 skipped, 2 min 43 s; every skip is a paused module's end-to-end test, with its phase. Native full suite 3 min 54 s.
  - `make docker-clean docker-demo` at scale 1.0: 4 min 35 s and 4 min 27 s. 3,507,865 rows of history, 1,919,938 new and 3,497 changed in Postgres over 91 days, 5,511,656 rows ingested (the overlap re-reads about 80,000), 5 dbt builds, 141 datasets owned, 376 lineage edges.
  - Two clean runs: all 155 relations identical (every ClickHouse table and view by row count and `sum(cityHash64(*))`, raw read with `FINAL`; every legacy table and report and every ops table in Postgres by row count and an MD5 of its rows).
  - On the demo's data at scale 1.0: an incremental `platform-ops build` 30 s, `--full-refresh` 26 s. Storage: raw 152 MiB, intermediate 253 MiB, marts 282 MiB, the snapshot 4.7 MiB (78,360 rows).
  - Tested at scale 0.01: every layer has its planned engine, key and partition; ClickHouse's `query_log` carries each model's node id; freshness on simulated time; each of the 12 legacy reports matches its mart in row count and every numeric total; the snapshot keeps earlier versions; an incremental build equals a full rebuild; an in-place change keeps only the latest version; the overlap catches a late commit and misses an older one.

- Phase 2 merged (PR #4). Reference numbers at scale 1.0: `make simulate` 280 to 290 s, `make cost` 20 to 24 s, byte-identical `cost.md` across two runs, unused list exactly the 12 abandoned models.
- Phase 3 implemented: fault injector with repair (`simulation/faults.py`), and `incidents/` with ingestion, grouping, severity, routing, notifier, lifecycle, the 21-day scenario, metrics and report. `platform-ops incidents run` is live; ADR 0008 and `src/platform_ops/incidents/README.md` written.
- Phase 3 reference numbers at scale 1.0: `make incidents` 2 min 22 s to 2 min 56 s over three runs, byte-identical `incidents.md` and postmortems across two runs. 8 faults, 18 failing checks, 8 incidents (exactly one per fault), 8 pages. Routing right person 8 of 8 (naive rule 3 of 8). dbt ran on 8 of 21 nights, 19 invocations.

- Phase 3 merged (PR #5).
- Phase 4 implemented: `reconcile/` (schema, canonical rules and renderers, connectors for Postgres, SQL Server and DuckDB, legacy generator, migration job with four defects, segmented diff, classification, metrics, sign-off report) and `simulation/migration_faults.py`. `platform-ops reconcile run` is live, so every Makefile target is implemented. ADR 0009 and `src/platform_ops/reconcile/README.md` written.
- Phase 4 merged (PR #6).
- Phase 5 implemented: Streamlit dashboards (`dashboards/`, data layer and charts in `src/platform_ops/dashboard/`), `platform-ops dashboard`, BigQuery and Snowflake collectors (`cost/collectors/`) with generated fixtures and contract tests, `make readme-check`, ADR 0010, the root README, a dashboards README and a collectors section in the cost README.
- Phase 5 merged (PR #7).
- Phases 6 and 7 added to `docs/SPEC.md` by the owner's decision: Phase 6 is a whole-repo review and cleanup, Phase 7 is hardening and release (1.0.0).
- Phase 6 merged (PR #8).
- Phase 7 implemented: the toolkit in a container (`Dockerfile`, `.dockerignore`), `app`, `dashboard` and `browser-check` services in Compose behind profiles, `docker-*` make targets next to the host ones (`demo` now runs `clean setup up pipeline`, and `pipeline` is every module in order), a headless Chromium check of every dashboard page (`scripts/check_dashboards.py`), `.gitattributes` with `eol=lf`, and the pandas and pyarrow caps lifted (pandas 3.0.6, pyarrow 25.0.1).
- Phase 7 acceptance, from a fresh clone of the branch plus the uncommitted Phase 7 files, with the working copy's `.env` (the Postgres volume already had its password): `make docker-build`, `make docker-demo`, `make docker-test`, `make docker-lint`, `make docker-readme-check` and `make docker-browser-check` all passed. Two clean container runs wrote byte-identical reports, identical to the native Windows run and to the Phase 6 baseline (excluding `cost_proxy_accuracy.md`). Tests in the container: 308 passed, 0 skipped (the SQL Server golden rows and e2e run there). `readme-check` 178 of 178. Browser check 8 of 8 page loads (4 pages, light and dark), screenshots reviewed.
- Phase 7 reference numbers, same laptop (16 CPUs, Docker Desktop with 14 GB), first on pandas 2.3.3 and pyarrow 21.0.0, then after the caps lift:
  - Image: first build with no cache 3 min 27 s, 1.58 GB on disk (364 MB compressed); rebuild after a code change 12 s; rebuild after a lock change 2 min 5 s.
  - `make docker-demo` from a clean state: 8 min 41 s (includes starting SQL Server). Per step, run 2: simulate 256 s, cost 16 s, incidents 189 s, reconcile 65 s (8 min 48 s). After the caps lift: 220 s, 12 s, 140 s, 56 s (7 min 16 s).
  - Native `make demo` on this branch: 8 min 2 s, and 7 min 49 s after the caps lift.
  - `make docker-test`: 7 min 48 s (pytest 7 min 40 s), 6 min 43 s after the caps lift (pytest 6 min 35 s). Native `make test` after the lift: 6 min 48 s, 305 passed and 3 skipped (no `pymssql` in the native venv).
  - `make docker-lint`: 4 min 32 s, 3 min 48 s after the lift. mypy dominates: it is built from source (`no-binary-package`) and starts without a cache in every fresh container.
  - `make docker-readme-check` 3 s; `make docker-browser-check` 30 s once both images exist (the first Playwright image pull took about 7 min).
  - Full-scale reconcile against Postgres and SQL Server in the container (`reconcile run --engine postgres --engine sqlserver`): 9 min 14 s, SQL Server about 8 of it. SQL Server as delivered: 94.984%, 99.516% and 34.836% row match, 100% recall and classification accuracy, not signed off, with the same classes and counts as Postgres.
- Phase 7 merged (PR #9).
- Phase 8 implemented:
  - mypy moved to its compiled 2.x wheels (1.19.1 to 2.3.1, which adds `ast-serialize`); the `<1.20` cap and `no-binary-package` are gone. mypy 2 reported nothing new under the strict config.
  - `.github/workflows/ci.yml` runs lint, the ownership check and the tests in the toolkit container with Postgres, on every pull request and push to `main`. The native `uv` job is gone.
  - `.github/workflows/sqlserver.yml` runs the full suite with Postgres and SQL Server, on demand, and fails on any skip.
  - `.github/workflows/release.yml` runs on a `v*` tag: it reuses `ci.yml`, checks that the tag equals the `pyproject.toml` version, pushes the amd64 image to `ghcr.io/nguyenngocthien0206/data-platform-ops` (`1.0.0`, `1.0`, `latest`), and creates a GitHub Release whose notes come from `CHANGELOG.md`.
  - Version 1.0.0: `pyproject.toml` is the only place it is written; `platform_ops.__version__` reads the installed package metadata. `CHANGELOG.md` has one 1.0.0 entry covering Phases 0 to 8. `scripts/release_notes.py` extracts a version's notes, and `tests/test_release.py` keeps the version, the package and the changelog in agreement.
  - Compose takes `TOOLKIT_IMAGE` to run a published image. The Makefile takes `DOCKER_ENGINES` (CI starts Postgres only) and has a new `docker-metadata-check` target.
- Phase 8 verification, before push: actionlint (1.7.12) found 0 errors in the three workflows. From a fresh clone of the branch plus the uncommitted files, the CI job's steps replayed locally (`docker build` instead of buildx with the gha cache) all passed. The SQL Server job's steps passed with nothing skipped. `make docker-demo` with `TOOLKIT_IMAGE` set to a GHCR-style tag of the image wrote reports byte-identical to the Phase 7 hashes. `platform-ops version` prints 1.0.0 natively and in the container.
- Phase 8 reference numbers, same laptop:
  - Cold `make lint`: native 5 min 13 s before, 1 min 2 s after (warm 4 s); `make docker-lint` 3 min 48 s in Phase 7, 62 s now.
  - CI job replay: image build 103 s (warm uv cache), lint 62 s, ownership check 13 s, tests against Postgres 6 min 37 s (309 passed, the 3 SQL Server cases skipped with their reason).
  - SQL Server job replay: 7 min 2 s, 312 passed, 0 skipped.
  - `make docker-clean docker-demo` with `TOOLKIT_IMAGE`: 7 min 8 s.
- Phase 8 merged (PR #10). CI passed on the pull request in about 6 min 20 s on `ubuntu-latest`: image build 97 s, lint 44 s, ownership check 7 s, tests against Postgres 3 min 31 s (faster than the 16-core laptop).
- Phase 9 implemented:
  - Root `README.md`: a "Run it in Docker" section (build or `TOOLKIT_IMAGE`, `make docker-demo`, the dashboards, `docker-readme-check`, `docker-test`, `docker-browser-check`, `docker-clean`), the runtime line updated to the latest runs, the ADR table extended to 0013 with a link to the index, and the repository layout updated. The sentence claiming CI runs without Docker is gone.
  - ADRs 0011 (module boundaries pinned by a test, and the review before the release), 0012 (the toolkit in a container) and 0013 (CI and release from the same container), and `docs/adr/README.md`, the index of all 13. Later notes on ADR 0007 and 0008 (the lifted budget, the designs kept) and 0010 (the caps and mypy, the container, internal use). A follow-up in `docs/review/phase-6-review.md` closes the three findings deferred to Phase 7.
  - `docs/SPEC.md`: a status line, the repository layout with the Dockerfile, CHANGELOG, workflows and review record, and the Makefile table with `pipeline`, `fmt`, `clean` and every `docker-*` target.
  - `dashboards/README.md`: the container command, the browser check, and the three presentation problems stated as known limits.
- Phase 9 acceptance, from a fresh clone of the branch plus the uncommitted files, with the working copy's `.env`: following the README's Docker section, `make docker-clean`, `docker-build` (31 s with the dependency layer cached), `docker-demo` (7 min 20 s), `docker-readme-check` (178 of 178) and `docker-browser-check` (8 of 8 page loads) passed, and the reports were byte-identical to the Phase 7 baseline. `make docker-dashboard` answered on `localhost:8501` within 6 s. All 44 relative links in the 27 Markdown files resolve (a one-off local script, not committed). All 13 ADRs are in the index.
- Phase 9 merged (PR #11).
- **1.0.0 released.** The owner pushed `v1.0.0` from `main` after the Phase 9 merge. The release workflow passed (CI rerun, tag checked against the version, image pushed, GitHub Release "data-platform-ops 1.0.0" with the changelog notes). The GHCR package is public, with tags `1.0.0`, `1.0` and `latest`. `sqlserver.yml` passed on `main`. Phase 8 and Phase 9 acceptance are closed.
- Published image check (2026-10-04): an anonymous `docker pull ghcr.io/nguyenngocthien0206/data-platform-ops:1.0.0` took 54 s (amd64, OCI labels with version 1.0.0 and the repository source); `platform-ops version` prints 1.0.0; `TOOLKIT_IMAGE=ghcr.io/nguyenngocthien0206/data-platform-ops:1.0.0 make docker-clean docker-demo` took 6 min 16 s and wrote reports byte-identical to the Phase 7 baseline.
- Phase 10 implemented:
  - `platform-ops metadata check --manifest ... --registry-dir ...` runs on any dbt project and adapter: no settings, no database, nothing written but an optional `--summary-json`. Findings name the file to change and how (a ready-to-paste rule for an unowned dataset); `--format github` adds annotations on the dataset's file or the rule's line; `--report-only` never fails; exit 1 means owners to fix, 2 means the check cannot run. New module `metadata/output.py`; `check.py` records each finding with its node, rule and fix.
  - Seeds and snapshots count as owned resources. `teams.yaml` takes an optional `telegram_chat`.
  - ClickHouse: a `clickhouse` Compose profile (`clickhouse/clickhouse-server:26.8.16.41`), a `dbt-clickhouse` extra (1.10.3) in the image, `DOCKER_ENGINES` defaulting to all three engines, CI running Postgres and ClickHouse.
  - `tests/fixtures/dbt_clickhouse` (`acme_analytics`): 15 models in a layout unlike the demo's, a local package, 4 seeds, a snapshot, 3 sources, 2 exposures and its own `ownership/` registry. `tests/test_ownership_external.py` parses it, checks every finding type, exit code and annotation, and builds it for real on ClickHouse.
  - `examples/github-actions/ownership-check.yml` for a company dbt repository, a section on it in the metadata README, ADR 0014, version 1.1.0 and its CHANGELOG section.
- Phase 10 verification, from a fresh clone of the branch plus the uncommitted files: `make docker-build` 104 s, `make docker-lint` 61 s, `make docker-metadata-check` 12 s (the demo still 9 sources, 118 models, 12 exposures, 139 rows), `make docker-test` with Postgres, ClickHouse and SQL Server 7 min 4 s, 327 passed and nothing skipped. `make docker-clean docker-demo` 6 min 58 s, reports byte-identical to the Phase 7 baseline. The example's `docker run` line, run from the built image as another user on a read-only mount of the parsed fixture: exit 0 on its registry, exit 1 with a repository-relative annotation when a rule is removed, exit 0 with `--report-only` (summary 24 of 25 owned, 0.96). actionlint found nothing in the example and the three workflows. Native `make lint` 3 s warm.
- Phase 10 merged (PR #12).
- Fix after Phase 10 (branch `fix-make-shell-on-windows`): `make` run from PowerShell or cmd failed with `! was unexpected at this time.`, because `SHELL := /usr/bin/env bash` only resolves inside Git Bash, so GNU make on Windows fell back to cmd.exe (the `bash.exe` on PATH was WSL's). On Windows the Makefile now runs recipes with Git for Windows' bash (`GIT_BASH`, default `C:/Program Files/Git/bin/bash.exe`, overridable). Verified from PowerShell (`check-env`, `help`, `make -n docker-demo`, a real `make docker-build`) and from Git Bash; Git's bash resolves Git's `find`, `grep` and `awk` and Docker Desktop's `docker`. The README states the Windows requirement.
- Phase 10 released: `v1.1.0` tagged and published (release workflow passed, image on GHCR). The Windows `make` fix merged (PR #13).
- Registry bootstrap implemented (branch `registry-bootstrap`, release 1.2.0):
  - `platform-ops metadata init-registry --manifest ... --out ownership/` writes `teams.yaml` (a commented example team) and `ownership.yaml` with one `TODO` rule per group: each source system, each folder of models split by name-token prefix until a prefix matches only that group (`fct_*`, `rpt_revenue_*`), each installed package, the seeds, the snapshots, and each exposure with its dbt owner. Every dataset matches exactly one rule and no two tie. Deterministic; never overwrites without `--force`. New module `metadata/bootstrap.py`.
  - Coverage and the summary's `owned_share` now count a dataset as owned only when its rule names a real team and member, so a starter registry reads 0%. The bundled project is unaffected (no rule problems).
  - A step-by-step rollout guide in the metadata README; the GitHub Actions example points at it and pulls 1.2.0.
  - SPEC Phase 11 rewritten for the owner's decisions (below), including the live dashboard and the changed acceptance.
- Registry bootstrap verification, from a fresh clone of the branch plus the uncommitted files: `make docker-build` 92 s, `make docker-lint` 60 s, `make docker-test` with Postgres, ClickHouse and SQL Server 7 min 10 s, 334 passed and nothing skipped. `init-registry` from the built image on the ClickHouse fixture: 25 datasets as 17 rules, and the check then reports 0 of 25 owned with 17 errors, one per rule. On the bundled project: 139 datasets as 63 rules, every dataset matched by exactly one. Filling in the TODOs makes the check pass (tested). `make docker-clean docker-demo` 7 min 2 s, reports byte-identical to the Phase 7 baseline.
- Registry bootstrap merged (PR #14) and released as 1.2.0 (`v1.2.0` tagged).
- Phase 6 implemented: broader lint rules on and clean; `# fmt: skip` noise removed; module boundaries fixed and pinned by `tests/test_architecture.py`; shared helpers in `common` (`markdown.table`, `db.replace_table`, `hashing.stable_hash`, `sandbox.write_isolated_config`); dead code removed; stale docstrings rewritten; new tests for the Slack post and the dashboard command; a shared `full_warehouse` test fixture. 22 findings recorded with decisions in `docs/review/phase-6-review.md`.
- Phase 6 acceptance: a clean `make demo` wrote byte-identical reports to the baseline (excluding `cost_proxy_accuracy.md`, non-deterministic by design); `make readme-check` 178 of 178; test suite 9 min 50 s on `main` against 6 min 24 s on the branch, same machine and session (305 passed, 3 skipped). The 5-minute target was not reached; the floor is the independent runs the determinism tests need.
- Phase 5 acceptance, from a fresh clone of the branch: `make setup && make up && make demo && make readme-check` passed in 7 min 41 s with byte-identical reports; the dashboards served every page.
- Phase 5 reference numbers: `make demo` from a clean state took 6 min 54 s and 9 min 1 s in two runs on the same laptop (slower machine state on the second: Docker Desktop just started, Smart App Control checking native modules), with identical reports. `make readme-check`: 178 numbers across the root and module READMEs, all found in the reports.
- Phase 4 reference numbers at scale 1.0: `make reconcile` about 52 s, byte-identical `reconciliation.md` across two runs. As delivered: 199,465 planted discrepancies, 100% recall, precision and classification accuracy; not signed off. Job fixed: 44 planted, all found; the diff moves 0.6% to 9.6% of the rows a naive comparison would. `make demo` from a clean state: 6 min 54 s.

## In progress

Phase 15 on branch `phase-15`: 15a committed; 15b (the README and the adoption guides) and 15c (the release run, the committed screenshots, 2.0.0) are planned, not started. The pull request opens when 15c is done.

## Decisions made

### Phase 14b (owner, at planning, 2026-10-14)

1. **Airflow 3.x**: Airflow 2 OSS reached its end of life in April 2026.
2. **Each orchestrator runs its own company in its own namespace** (`af_`, `dg_`), with its own incident store; `make demo` is never touched.
3. **A real 02:00 schedule, paused by default**; the tests trigger runs through the APIs.
4. **Three parts, one PR each** (14b-1 the foundation, 14b-2 Airflow, 14b-3 Dagster and the release gate), so every session ends in a state that can be committed.

### Phase 15 (owner, 2026-10-15)

1. **Not plug-n-play, but adoptable**: no new commands for use outside the simulation; each module's README says what runs as is, what belongs to the simulated company, and what to replace.
2. **The README speaks of version 2 only**: no DuckDB, SQL Server, 1.x results or superseded ADRs. The legacy Postgres and its reports stay, as the input of the migration.
3. **"How the decisions were made" becomes how the problems were seen, framed and solved.**
4. **A professional architecture diagram** in the style of the owner's template, white background, one version, **every logo in full colour from the vendor's own files**.
5. **Screenshots committed** in the repository.

### Phase 15a, made during implementation

- **The diagram is generated, not drawn by hand**: reviewed as code, its numbers from the project, a test that fails when it is stale.
- **Logos where the brand kit could not be fetched directly**: Slack's media kit downloads through a portal that needs a browser, and Snowflake's brand page refuses automated requests, so both come from the vendor's own site (Slack's marketing CDN, Snowflake's documentation). dbt's kit and the ASF's Airflow logos ship PNG only. ClickHouse's kit ships black and white only. All recorded in `LICENSES.md`.
- **No Python logo**: the official file carries the wordmark, and the diagram does not need it.
- **Dagster's run page, not its asset graph**: the graph of 120 assets is unreadable at README size.
- **Metabase screenshots are the first 900 px** of each dashboard: Metabase scrolls inside the page, so a full-page capture stops at the viewport.

### Phase 14b-3, made during implementation

- **Dagster 1.13.25 and dagster-dbt 0.29.25**, not 1.13.26: the latest release was six days old; this one two weeks.
- **A pinned, hashed `requirements.txt`** for Dagster's environment: Dagster publishes no constraint file, and the toolkit's `uv.lock` must not carry Dagster.
- **One `dbt build` per run, not one invocation per model**: that is how dagster-dbt runs dbt, and the desk regroups a run either way. Freshness runs first in the same asset function and never holds the build back.
- **The run fails after the ingest when dbt failed** (`raise_on_error=False`, then `Failure`): the hook always gets the artifacts, and the UI shows a faulted run red, as Airflow does.
- **`dagster dev`** in one container, like `airflow standalone`: one machine, not a production deployment; the README says so.
- **The driver named in `dagster.yaml`** (`postgresql+psycopg2`): SQLAlchemy 2.1 defaults `postgresql://` to psycopg 3, which dagster-postgres does not install.
- **No `from __future__ import annotations`** in the definitions: Dagster checks the `context` annotation as a type.
- **Dagster's UI on 127.0.0.1 only**: it has no login in open source. Airflow's has one, so it keeps its port.
- **The release runs both checks on separate runners**, in parallel with the Metabase job, so it waits for the slower one (Airflow, under 9 minutes here).

### Phase 14b-2, made during implementation

- **Freshness on the company's clock** (owner, 2026-10-15, option A): `platform-ops company clock` gives dbt's `simulated_now`; on the wall clock every source of the simulated company would be stale every night.
- **Freshness beside the models, not before them**: a stale feed turns the run red without holding the build back (the plan had it as the first task).
- **The toolkit's dbt is the one Cosmos runs** (`dbt_executable_path`): Airflow's environment holds no dbt, so the two lock files never meet.
- **The manifest is parsed at image build** (`LoadMode.DBT_MANIFEST`): no dbt runs while the DAG is parsed; Cosmos caches the partial parse after the first task, so later tasks skip the full parse (about 10 s each).
- **Tests on two models are detached** (`should_detach_multiple_parents_tests`): on the first healthy run, `relationships` from `stg_payments` to `stg_orders` ran with `stg_orders`'s tests before `stg_payments` existed and failed.
- **No `start_date`** on the schedule (`CronTriggerTimetable`, `run_immediately=timedelta(0)`, `catchup=False`): with one, Airflow 3.3 runs the night that just passed as soon as a fresh stack is unpaused, which made a clean-stack test run twice and attribute the incident to the scheduled run. Without it, a fresh stack waits for the next 02:00, and a stack that was down runs the latest night missed once.
- **The container gets only what it uses**: the Postgres login and the company's `dbt` ClickHouse user, no `.env`; nothing secret is in the DAG.
- **The whole project in the test, no selection**: one run of all 224 tasks takes about 6 minutes at scale 0.01, under the 15 minutes the plan allowed.
- **No screenshot in the README**: the repository tracks no images (screenshots go to `reports/`); the UI was checked through the API instead.

### Phase 14b-1, made during implementation

- **The hooks call the CLI, not the library** (ADR 0020): the toolkit's lock file never meets Airflow's or Dagster's constraints.
- **Run ids name their scheduler** (`airflow:<dag>:<run>`, `dagster:<run>`), so two schedulers feeding one store cannot collide.
- **A missing run id calls nothing**: an ingest under an empty run id would group unrelated invocations.

### Phase 14 (owner, at planning, 2026-10-14)

1. **Split in two**: 14a is the core; 14b is real Airflow (Cosmos) and Dagster (dagster-dbt) in Docker, their integration tests and the release gate.
2. **The incidents happen after cutover, inside the company's own timeline**: three production weeks extend the window to 16, the demo gets longer and every cost figure changes. A separate scenario (a second history of the same company) was the cheaper option, declined.
3. **A demo of about an hour is acceptable.**

### Phase 14a, made during implementation

- **Orphan keys replace duplicate keys**: the raw `ReplacingMergeTree` deduplicates keys by design, so a duplicate cannot reach staging.
- **Faults are planted in the raw copy and fixed by resyncing it from Postgres**, the source of truth; a fault that breaks the loader (a stale feed, a changed table) stops it for that table, and the watermark catches up after the fix.
- **Resolution is observed**: a later run that covered the root and found it healthy (its tests must have run, if it has any); a model that ran before its tests did is not yet healthy.
- **A run is regrouped on every ingest**, and an incident this run opened is merged into another when a later invocation finds the real root above it.
- **Each production invocation clears older result files first**: the first production night once ingested the parallel run's last `dbt build` results as its own.
- **`support_tickets` freshness is off**: at scale 0.01 a quarter of the days have no ticket, and the first test run opened an incident for a quiet day.
- **The suite's faults moved to production days 0 and 1**: on days 1 and 3, the stale feed (an error only after 48 hours) was still open when the week ended.
- **A new failure above an open incident's root appends to it.** The first full run paged twice for one fault: F5 dropped a column, the loader stopped, and two nights later the feed's freshness error (ingested before the night's run) opened a second incident before the run's model error merged it back. A stale feed whose staging model's incident is still open is the same problem, seen from the source.
- **Snapshot tables are merged before every snapshot and build.** The first pair of clean runs left every report identical but differed by 80 bytes read in one snapshot upsert: each night's snapshot appends a part, merged in the background at different moments. The same reason raw is merged every night (Phase 13).
- **The fingerprint leaves out dbt's invocation ids** in the incident tables: they are random by design, like the run id, and keying ingests by them is what makes a re-ingest change nothing.
- **Every missing password is reported at once** (the dashboards' and the ClickHouse users'), not the first one found.

### Phase 13b (owner, at planning, 2026-10-14)

1. **The Incidents page is hidden until Phase 14**, when the incident module moves to the store. The 1.x page file is deleted rather than left broken (it is in git and at `v1.2.0`).
2. **The dashboards read as their own read-only Postgres role**, with its own password and no default, like the company's ClickHouse users.
3. **The Reconciliation page is a timeline** of the 42 nights against the 21 pairs, with one night in detail.
4. **The Cost page has four views**: showback by team and month with a model switch, by workload, the business case, and what to change.

### Phase 13b, made during implementation

- **Everything the cost report states is stored**, so the pages never recompute: the business case and the recommendations were only computed at render time before.
- **Status is a fixed status colour plus a glyph** on the reconciliation grid (✕ failed, ↑ a report whose difference comes from an upstream table), never a team colour and never colour alone.
- **The browser check found three presentation bugs no assertion caught**: two dollar amounts in one caption became a LaTeX formula (dollar signs are now escaped), axis labels were cut off or every other row label dropped (wider label limits, taller grid rows), and rounded cells left dark dots where four corners met (band padding instead).
- **The browser check waits for a page to stay rendered**: Streamlit reports a page done for a moment between `app.py` and the page, and the first cold load of the overview landed on it once.

### Phase 13 (owner, at planning, 2026-10-13)

1. **The raw layer is merged (`OPTIMIZE FINAL`) every night**, after the load, so `FINAL` reads measure the same bytes on every run. Measured at 3.6 s for all nine raw tables at scale 1.0; recorded as the platform team's maintenance work and priced like any other.
2. **Public on-demand list prices with their sources** for the cluster (EC2 `m6i.2xlarge`, EBS gp3) and the legacy reporting replica (RDS `db.m6i.2xlarge` PostgreSQL, RDS gp3), us-east-1, looked up in October 2026. The sizing (one node and 500 GB each) is an assumption, printed first in the report.
3. **Quotas are suggested in the report, never applied.**
4. **Split in two**: 13a is the cost core and `reports/cost.md`; 13b brings the Streamlit dashboards back on the Postgres ops store, with the browser check.

### Phase 13, made during implementation

- **Bytes written are not collected.** `written_bytes` differed between two identical runs (how ClickHouse cuts blocks varies); `written_rows` repeats and is kept.
- **Catalogue statements and reads of system tables are collected but not billed.** dbt's count of `CREATE`, `DROP` and `RENAME` statements differs by one or two between runs, and its reads of `system.*` with them; no warehouse bills either.
- **The run id is a module value, not a `ContextVar`**: the reconciliation compares from a thread pool, and threads do not inherit a context, so its first queries went untagged.
- **Query ids are a stable numbering**, by simulated time, kind of work, subject and the query's own text and measurements: the query log's ids are random.
- **`ALTER USER ... SETTINGS` replaces a user's settings**, so the suggested per-query limit restates the reader profile. Checked by applying the suggestions to a test namespace.
- **Users that also build, load or reconcile get no quota suggestion** (`dbt`, `loader`, `platform`): a limit there could stop the pipeline.
- **Only billable statements are kept and numbered.** dbt sends one or two more or fewer catalogue statements from run to run; numbering them shifted every id after them. They are counted, not stored.
- **dbt's invocation id is taken out of table names.** dbt-clickhouse names an incremental model's staging table `<model>__dbt_new_data_<uuid>`, new every run, and it showed in the tables read and the SQL text. Two runs at scale 0.01 then left every cost ops table and `cost.md` identical.
- **dbt writes one part per partition** (`max_insert_threads = 1`, inserts squashed to one block, in the dbt profile and the `writer` settings profile). The first two clean runs at scale 1.0 differed by a few cents: ClickHouse's default of one insert thread per core wrote the four incremental models (two million rows each) into 20 to 29 parts in a different way on every run, and the same ad hoc query on `marketing_fct_sessions` read 262,491 rows in one run and 246,198 in the other. Scale 0.01 never showed it, because its tables fit in one block.
- **Redaction found a NUL-byte bug**: the comment placeholder held digits, which the number pattern then replaced, and Postgres refused the text. The placeholder now has no digits.

### Phase 12 (owner, at planning, 2026-10-14)

1. **The parallel run is inside the demo company's timeline**, not a separate scenario: one company, one history, and its cost (backfill, nightly builds, the reconciliation's queries) lands in ClickHouse's query log for Phase 13.
2. **6 weeks, cutover after 14 consecutive clean nights.**
3. **Reports reconciled every night**, so dbt builds every night of the parallel run.
4. **Report differences are graded by lineage**: explained by an upstream table that differs the same night, or flagged as a logic difference.
5. **Run time**: measured at 102 s per night one pair at a time and 23 s six at a time; with a 45 to 50 minute estimate, the owner kept 6 weeks at scale 1.0 and asked the release CI to run the demo smaller.

### Phase 12, made during implementation

- **`make demo` runs the parallel run, `make reconcile` writes the sign-off from the recorded nights** (the SPEC's acceptance said `make reconcile` runs the window; changed with the plan, since the window is part of the company's history).
- **Every defect and fault touches data no dbt test checks.** The first full run paged orders, and dbt's relationship tests caught the orphaned order lines and payments; a padded order status failed `accepted_values`. A failing build stays fatal, so the defects moved: the paging bug to web sessions (order lines could not be used either: their keys are `order_id * 10 + line` and never fall on a page boundary), the padding to product names.
- **The float defect's truth was measured against ClickHouse**: `toDecimal64(toFloat64(x), 2)` scales the float by 100 in floating point and truncates. A first model (truncating the float's exact binary value) disagreed with ClickHouse on 245,308 of 575,208 payments and showed as 71% recall; the corrected one agrees on all of them.
- **The golden-row test caught a ClickHouse trap**: `if()` reads a NULL condition as false, so a NULL boolean rendered as `false`.
- **The ops store for the reconciliation is prepared on the first parallel night**, not when the run starts, so a missing password is still reported before anything is touched.
- **Faults are repaired the next day, whether or not the diff found them**: the grading then exposes a miss instead of the simulation hiding it.

### Phase 11b (owner, at planning, 2026-10-13)

1. **The BI replay goes to Phase 13**, with the workload generator: 11b generates the card SQL from the exposures as a shared module, and Phase 13 replays exactly those queries under the `metabase` user.
2. **A separate `loader` user** for the ingestion, so its load is not counted as dbt's.
3. **One password variable per user**, no shared password.
4. **The Metabase check runs on demand and before each release**, not in pull requests.

### Phase 11b, made during implementation

- **Metabase `v0.63.19.6`**: the latest patch of a minor version out for weeks (the 0.64 line was days old). Its ClickHouse driver is bundled; the separate driver repository is archived.
- **No default passwords**: a missing variable stops the run before anything is written. `dbt parse` still works without them, because the dbt invocation falls back to the admin connection only when the dbt password is unset, and parsing never connects.
- **Grants found by running**: `system.parts` for dbt (dbt-clickhouse's `insert_overwrite`), `READ ON POSTGRES` and `CREATE TEMPORARY TABLE` for the loader, `system.processes` for Metabase's driver with a row policy (without it, Metabase could read every user's running query text).
- **Chart hints live in the model's `config.meta`**, next to the SQL its owner reviews; the rules cover everything else.
- **Card names are unique per team collection** ("Dashboard: Model"); on the dashboard each card shows its short title.
- **Metabase's own "Examples" collection and sample database are left alone**; the screenshots skip them.

### Phase 11 (owner, at planning, 2026-10-13)

1. **Split in two.** 11a: legacy Postgres and its reports, ingestion, the dbt project on ClickHouse, the ops store, ownership on the company project. 11b: Metabase from the exposures and one ClickHouse user per team.
2. **One dbt project now.** The company's project moves to ClickHouse in 11a; cost, incidents, the dashboards and reconciliation pause until Phases 12 to 14, and `make demo` says so.
3. **The ops store is Postgres**, database `platform_ops`, on the same server as the legacy database. It is also the Phase 14 store.
4. **One legacy report per dbt exposure**, about 12.

### Phase 11a, made during implementation

- **Money stays in Decimal.** A float discount rate made revenue sums depend on ClickHouse's aggregation order, so two runs differed in the last bits; the rate and resolution hours are now Decimal.
- **Averages are exact and round half away from zero**, like Postgres (`avg_half_up` macro). ClickHouse's `avg` returns a Float64 and `round` on a float rounds half to even, so `avg_order_value` drifted from the legacy reports by cents. The report-versus-mart test found it.
- **Session settings** in the dbt profile and the toolkit's client: `join_use_nulls = 1` (an outer join returns NULL, not a default) and `union_default_mode = DISTINCT`. `prefer_column_name_to_alias` was tried and dropped: it breaks dbt-clickhouse's own metadata queries. Columns an alias would shadow are qualified instead.
- **`allow_nullable_key`** only on the table folders (intermediate, marts, abandoned): the legacy columns are nullable and keys derive from them. Views reject the setting.
- **The query comment goes first** (`append: false`): a seed's `INSERT` carries its CSV rows inline, and a trailing comment was read as one more row.
- **The snapshot uses the timestamp strategy** on `updated_at`: the check strategy stamps rows with the wall clock.
- **Raw tables are not partitioned.** `ReplacingMergeTree` deduplicates only within a partition, and a changed row moves in `updated_at`.
- **Starting over drops every company database** in ClickHouse, because a snapshot survives `--full-refresh`.
- **The reconciliation library still runs** on DuckDB in its tests (self-contained, no warehouse); only its command is paused. SQL Server went from its internals too, since nothing could run it.
- **A `namespace` setting** prefixes every Postgres schema and ClickHouse database, so the tests share the servers with the demo without touching it.

### Version 2.0 (owner, 2026-10-13)

The owner asked why the repository held two dbt projects (the simulated company on DuckDB, and the Phase 10 test fixture on ClickHouse) and redirected the project around one story that mirrors the company's real stack:

1. **One simulated company, one dbt project.** The company's reporting runs on a legacy Postgres database as hand-written SQL views and tables; it migrates to self-managed ClickHouse, where the analytics are rebuilt as one dbt project. The ClickHouse fixture project is retired once the company's project replaces it in the tests.
2. **The toolkit is applied to that flow:** reconciliation signs off Postgres to ClickHouse; ownership and incidents run on the ClickHouse dbt project; cost and FinOps run on ClickHouse's query log.
3. **BigQuery and Snowflake are priced as alternative targets only**, on the same measured workload; no module is built for them, and their collectors stay contract-tested.
4. **Cost uses measured bytes and rows, modelled time.** `system.query_log` gives bytes and rows; compute time stays modelled from bytes (ADR 0005), so reports stay byte-identical. If measured bytes do not repeat exactly, ask before relaxing the rule.
5. **DuckDB stops being the warehouse; Iceberg and SQL Server are removed.** Version 1.x stays available at `v1.2.0`.
6. **Order follows the migration:** Phase 11 the company on Postgres and ClickHouse, 12 reconciliation, 13 cost and FinOps, 14 incidents on real runs (the former Phase 11, with the live store, notifiers, integrations and dashboard decided before), 15 documentation and 2.0.0. The former Phases 11 and 12 are folded into 13 and 14.
7. **Every number is regenerated** from 2.0's runs: READMEs, reports, ADR figures, and the numbers in the owner's CV.
8. **Airflow and Dagster for real, in Docker** (owner, same day). The integrations still import neither orchestrator, so the toolkit stays light. But they are also tested on real Airflow with Cosmos and real Dagster with dagster-dbt, in their own images. That runs on demand and in the release workflow before it publishes; pull requests keep the fast tests with fakes. The same images run the simulated company's dbt project on its schedule behind Compose profiles, with their UIs, so the company runs dbt the way the real one does. `make demo` stays on the Makefile. This replaces "tested with fakes only" (decision 5 below), and `docs/CLAUDE.md` was changed to allow these images.
9. **Closer to a real company** (owner asked whether the plan was realistic; all five suggestions taken):
   - ClickHouse-style modelling in Phase 11: `ReplacingMergeTree` for tables changed in place, `MergeTree` with chosen `ORDER BY` and partition keys, incremental models, not nightly full refreshes of 120 models.
   - Incremental loads from Postgres by an `updated_at` watermark through `postgresql()`, not a full copy and not CDC (too heavy for a laptop).
   - A parallel run before cutover in Phase 12: legacy and ClickHouse side by side for weeks, nightly reconciliation, cutover only after N consecutive nights within thresholds.
   - FinOps in Phase 13 splits the cluster's cost into compute (bytes and rows read) and storage (`system.parts`, by rows and uncompressed bytes), compares legacy Postgres's cost before with ClickHouse's after, and shows back per ClickHouse user and team with suggested quotas.
   - One ClickHouse user per team, plus `metabase` and `dbt`, so `query_log` records who ran what.
10. **Metabase is the company's BI, Streamlit stays the toolkit's.** Metabase (Phase 11, behind a Compose profile, its application database on the same Postgres server) shows the dbt exposures as real dashboards on ClickHouse marts, created by a script from the exposures. `make demo` replays their queries deterministically under the `metabase` user; a live Metabase's own queries never feed the reports. Superset was judged too heavy for this.

### Before Phase 11 (owner, 2026-10-13)

1. **The rollout gets a starter registry command** before Phase 11 (`init-registry`, release 1.2.0: a new command is a minor version).
2. **Dashboards for real data are part of Phase 11**: a live mode of the Streamlit app reading the shared Postgres store, starting with incidents. The demo keeps DuckDB.
3. **Slack through a bot token** (`chat.postMessage` to the team's `channel`), Telegram through a bot token to the team's `telegram_chat`.
4. **The company runs dbt with Airflow + Cosmos and Dagster + dagster-dbt.** Cosmos runs one dbt invocation per model, so ingest regroups a whole orchestrator run on every call.
5. **The integrations import neither orchestrator** and are tested with fakes plus a real end-to-end ingest; the SPEC's acceptance was changed to match.

### Phase 10 (owner, at planning)

1. **The consuming CI runs `dbt parse`** and hands the manifest to the toolkit, which needs no adapter.
2. **A report-only mode**, to measure the baseline and switch the check on without blocking merges.
3. **ClickHouse runs in PR CI**, next to Postgres.
4. **Release 1.1.0** at the end of the phase; the owner pushes the tag.

### Phase 10, made during implementation

50. **`dbt parse` with `dbt-clickhouse` runs without a server**, verified with nothing listening on the port. A parse-only profile selected with `--profile` is enough, so the example needs no secret.
51. **`--repo-root` instead of a path prefix.** Annotation paths are made relative to the repository root, which works wherever the project and the registry sit inside it; a file outside it keeps its full path.
52. **An installed package's datasets get no file annotation**, because their files are not in the repository; the finding still lists the rule to add.
53. **Exposures appear in `run_results.json` as `no-op`** in a dbt build; the integration test allows that status for them.
54. **A package macro is called with its namespace** (`acme_shared.cents_to_amount`); dbt does not resolve another package's macros unqualified.
55. **Compose defaults for ClickHouse credentials** (`:-`, not `:?`), matching the fixture profile's defaults, so an existing `.env` without the new keys keeps working.

### Before Phase 10 (owner)

1. **The toolkit stays public; company configuration lives in the company's repositories.** `ownership.yaml` and `teams.yaml` for company projects sit in each dbt repository, and CI pulls the public image and points it at them. Names, channels and chats of the company never enter this repository. Added to the SPEC's Phase 10.
2. **The company's dbt repositories run CI on GitHub Actions**, so the Phase 10 example is a GitHub Actions workflow.
3. **Company policy allows the public image in company CI** (the owner asked). The example workflow pulls `ghcr.io/nguyenngocthien0206/data-platform-ops` at a fixed version.
4. **The pilot is the data platform team.** It owns the shared layers (staging, intermediate), understands ownership best and will maintain the rules, so the first rollout meets the least friction.
5. **The pilot's dbt project runs on ClickHouse** (`dbt-clickhouse`), self-managed by the company. First chosen as BigQuery, then changed by the owner: there is no BigQuery environment to test against, while ClickHouse runs locally in Docker, so the fixture project, the local tests and the pilot use the same engine.
6. **The fixture project is built here**: a small dbt project on the ClickHouse adapter with a layout unlike the bundled one (several model folders, a package, sources and exposures), run against a ClickHouse service behind a Compose profile so its artifacts are real. No company data or manifest is needed.
7. **Self-managed ClickHouse has no vendor bill.** Cost for it (Phase 12, if ClickHouse is the first engine) means sharing the infrastructure cost by usage, so the SPEC's Phase 12 acceptance now covers that case.

### After Phase 8: internal use (owner)

1. **The toolkit goes to the teams inside the company, not open source.** Open source would mean supporting many engines and outside users. The company's stack: Snowflake, BigQuery or ClickHouse; dbt run from Airflow and from Dagster; alerts to Slack or Telegram; no migration under way.
2. **One module per phase, lowest integration cost first**: Phase 10 ownership in the company's dbt CI (no warehouse access, and the other modules route on it), Phase 11 incidents on real dbt runs (engine-agnostic, since dbt artifacts look the same on every adapter, and the pain is daily), Phase 12 cost for the one engine with the largest bill (collectors are engine-specific and need read access and a data handling review). Reconciliation waits for a real migration.
3. **Phase 9 is trimmed** to what 1.0.0 needs: a Docker section in the README, ADRs for Phases 6 to 8 with an index, notes on the ADRs whose context changed, the SPEC's Makefile table, and fixes to anything stated wrongly. The Docker-first rewrite and the docs check in CI are dropped.
4. **Shared state for company runs lives in one Postgres database**, the same whatever the warehouse, rather than in each warehouse.
5. **Airflow and Dagster integrations live in this repo as optional extras** (`platform-ops[airflow]`, `platform-ops[dagster]`), thin layers over the same CLI and Python API.

`docs/CLAUDE.md` was updated to match: the demo and tests stay offline and deterministic; code for company systems is optional, sits behind the existing interfaces and reads credentials only from the environment; no orchestrator inside the toolkit.

### Phase 8 (owner, at planning)

1. **The image is amd64 only**, like the SQL Server image. Apple Silicon runs it under emulation or builds it locally with `make docker-build`.
2. **The on-demand job runs only the SQL Server tests** (the full suite, nothing may skip), from `workflow_dispatch`. A demo and `readme-check` in CI belong to Phase 9.
3. **The release reruns the full CI first**, then publishes only if the tag equals the version and the changelog has notes for it.

Planned defaults: CI calls the same `docker-*` make targets as a laptop, with Postgres from Compose; CI writes `.env` from `.env.example` on fresh volumes; the native `uv` CI job is dropped; the browser check stays out of CI.

### Phase 8, made during implementation

45. **The version is read from the package metadata**, so `pyproject.toml` is the only place it is written, and `uv lock` records it.
46. **Release notes skip Keep a Changelog's link definitions** at the end of the file, and a missing section exits non-zero so a tag without notes never publishes.
47. **The SQL Server job runs its step with `shell: bash`**, which gives `pipefail`, so `make docker-test | tee` still fails when the tests fail; a grep on the summary fails the job on any skip.
48. **`latest` comes from `docker/metadata-action`'s default**, which adds it for stable semver tags only; the workflow lists the `{{version}}` and `{{major}}.{{minor}}` patterns.
49. **No `.mypy_cache` in CI.** With compiled wheels a cold `make docker-lint` takes about a minute, so a cache is not worth its keys.

### After Phase 7: constraints that no longer apply (owner)

Smart App Control is off and the 10-minute budget is lifted, so what was built around them was reviewed once more.

1. **mypy's cap and source build go in Phase 8** (added to the SPEC). They were the last Smart App Control workaround in the tooling, and they make `make docker-lint` take about 4 minutes, which CI would pay on every run. `python -m mypy` stays.
2. **The budget-driven designs stay as they are.** The real dbt build every 28 days with daily replay (ADR 0007) and the incident scenario that runs dbt only on nights a fault is active (ADR 0008) are documented. Their reports match across the native run, the container and the Phase 6 baseline. Changing them would redo every reference number and README figure for realism alone. The pandas and pyarrow caps were already lifted in Phase 7.

### Phase 7 (owner, at planning)

1. **The pandas and pyarrow caps are lifted only after the container is verified on the current lock**, in their own commit, and kept only if the reports stay byte-identical and everything passes. They did, in the container and natively, so the lift stays.
2. **The dashboards are checked in a real browser automatically**: Playwright and headless Chromium in their own image (`browser-check`), against the dashboards served from the toolkit container, with screenshots for a human look.

Planned defaults: work continues on `phase-7-containerized-rerun`; the code is baked into the image and generated state lives on named volumes, with only `reports/` bind-mounted; `.env` never enters the image; host targets keep working; the Docker section of the README waits for Phase 9.

### Phase 7, made during implementation

38. **Service addresses come from the environment, no code change.** The connectors already read `POSTGRES_HOST`, `POSTGRES_PORT`, `MSSQL_HOST` and `MSSQL_PORT`, and an environment variable wins over `.env`; Compose sets them to the service names and container ports. Streamlit's `localhost` binding in `.streamlit/config.toml` is overridden by `STREAMLIT_SERVER_ADDRESS=0.0.0.0` in the container.
39. **`make clean` empties the generated directories instead of removing them**, because inside the container they are volume mount points. On the host the effect is the same.
40. **The Playwright image ships the browsers but not the Python package**, so `browser-check` is built from it with `playwright` pinned to the image's version (an inline Dockerfile in Compose, context `scripts/`).
41. **The browser check waits on Streamlit's own script state** (`data-test-script-state="notRunning"` on the app root), then on every Vega chart having drawn. Watching the status widget let the first page pass before it had rendered. On a deep link such as `/cost` the frontend probes `/cost/_stcore/health` and `host-config`, gets a 404 and falls back to the root; those two probes are expected and ignored, and every other failed request fails the check.
42. **Screenshots grow the viewport to the content.** Streamlit scrolls an inner container, so a "full page" screenshot stops at the viewport.
43. **pandas 3 no longer installs `tzdata` on Linux** (only on Windows). The image has Debian's system time zone data, which `zoneinfo` reads first anyway, so nothing changed; the reconciliation's daylight saving cases still match.
44. **DuckDB's `icu`, `json` and `parquet` extensions are built into the wheel**, so nothing is downloaded at run time in the container either.

### Phases 7 to 9 (owner, after Phase 6)

1. **The project now has ten phases (0 to 9).** Phase 7 packages the toolkit in a container and reruns and verifies everything through Docker Compose; Phase 8 is CI and release; Phase 9 is all documentation. This replaces the earlier single "hardening and release" Phase 7.
2. **No time budget for `make demo` any more.** The 10-minute limit is lifted (`docs/CLAUDE.md` and the SPEC updated); measured times are recorded instead.
3. **Release in Phase 8:** CI runs checks on every pull request and push to `main`; a pushed `v*` tag builds the image, pushes it to GitHub Container Registry, and creates a GitHub Release from `CHANGELOG.md`. Version 1.0.0 and the changelog belong to Phase 8. The owner creates and pushes the tag.

### Phases 6 and 7 (owner, at planning, superseded above for Phase 7)

1. **Two more phases**: Phase 6 whole-repo review and cleanup, then Phase 7 hardening and release, so the release is cut on the cleaned code.
2. **Release**: `CHANGELOG.md`, version 1.0.0, and the tag commands; the owner creates and pushes the tag and the GitHub release.
3. **CI gets a Postgres job** (service container) in Phase 7; SQL Server stays optional and skipped in CI.
4. **Phase 6 rule: no behaviour change.** `make demo` reports must be byte-identical before and after; a bug whose fix would change a reported number is raised with the owner, not fixed silently.

Planned defaults for Phase 6: the review record is committed as `docs/review/phase-6-review.md`; the broader ruff rules stay on; `make test` target under 5 minutes, or the measured floor is reported.

### Phase 5 (owner, at planning)

1. **Dashboard tests run on a real warehouse at scale 0.01**: the ops-only data layer is tested on its own, and an AppTest smoke test renders every page against a warehouse built by all four modules (about 3 more minutes of `make test`).
2. **`make readme-check`** verifies that every number in the README "Results" sections appears in `reports/*.md`. READMEs stay hand-written; the check runs after `make demo`, not in CI.
3. **Vendor collector fixtures are generated from the simulated workload**: a deterministic script maps a sample of `ops.query_log` into BigQuery `INFORMATION_SCHEMA.JOBS` and Snowflake `QUERY_HISTORY` columns; the files are committed.

Planned defaults: no lineage graph on the Overview page; `make demo` does not run `readme-check`; `QueryRecord` gains an optional `bytes_scanned` for vendor-measured bytes; Streamlit usage stats off (offline after setup).

### Phase 5, made during implementation

30. **pandas capped `<3`, pyarrow `<22`** (asked the owner). Windows Smart App Control switched to enforcing on the development laptop and refused pandas 3.0.6 and pyarrow 25.0.1 (too new to have reputation); pandas 2.3.3 and pyarrow 21.0.0 load. Without the cap `make reconcile` and the dashboards do not run on that machine. Raise the caps once newer wheels load.
31. **`ops.query_log.bytes_scanned`** (nullable) stores vendor-measured bytes from a cloud collector; local collectors leave it NULL and `cost.md` is unchanged. Three tests that insert into `query_log` by position gained a NULL.
32. **The SQL parser ignores a comment after the final `;`.** Found by the collector contract tests: a warehouse records a dbt model's statement with dbt's query comment after the compiled SQL's semicolon, which parsed as a second statement.
33. **Fixtures carry the dbt query comment and modelled times.** The query log keeps dbt's compiled SQL from `target/run`, written before the comment is appended, so the fixture generator appends it as the warehouse would see it; elapsed times are modelled, never measured, so the fixtures are deterministic.
34. **README runtimes are marked `<!-- readme-check: runtime -->`.** Timings are printed on the console, not written to a report; every other results number must appear in the reports. Derived percentages in the READMEs were rewritten with the reports' own figures.
35. **Chart colours**: the validated reference palette, checked against Streamlit's own light and dark surfaces; three light-mode slots are below 3:1 contrast, so every chart has tooltips and its data table beside it.
37. **Streamlit always runs headless; `platform-ops dashboard` opens the browser itself** once the health check answers. Found by the fresh-clone run: on a machine that never ran Streamlit, a non-headless start stops at an interactive "Email:" prompt, so `make dashboard` hung.
36. **Hotspots can be empty at small scale** (tables under 20 MB never qualify); the Cost page says so instead of showing an empty table.

### Phase 4 (owner)

1. **PyIceberg plans the files, DuckDB reads them with `read_parquet`.** Offline; the DuckDB `iceberg` extension would need a download.
2. **`reconcile run` exits 0 whatever the verdict**; the verdict is in the report. `--strict` exits non-zero when the delivered migration is not signed off.
3. **`pymssql` as an optional extra** (`uv sync --extra sqlserver`). It carries its own driver; pyodbc needs ODBC Driver 18 on the host.
4. **Realistic time zone defect kept**: daylight saving ignored, about 65% of payments shifted by one hour.
5. **Canonical defaults**: Postgres neither trims nor folds case; SQL Server folds case, like its case-insensitive collation.
6. **Two passes** (asked during implementation): the job as delivered, then the job with its defects fixed. Systematic defects make every segment differ, so only the second pass shows what the segmented diff saves.

### Phase 4, made during implementation

24. **Legacy data has its own generator covering calendar 2025.** The Phase 1 data spans only a winter, so a daylight saving defect would never fire. Local times fall between 06:00 and midnight, avoiding the ambiguous hour when clocks go back.
25. **Hash: MD5 of the UTF-8 row string, split into two unsigned 32-bit halves, summed per segment.** Verified identical in Python, DuckDB 1.5.5, Postgres 16.15 and SQL Server 2022 CU27 on golden rows. SQL Server's legacy database uses a `_UTF8` collation so VARCHAR bytes match; style 126 drops a zero fraction, so microseconds are formatted by hand; pymssql mangles non-ASCII VARCHAR, so leaf values are fetched as NVARCHAR.
26. **Each side hashes its rows once into a temp table**, and every level groups that. The diff went from 53 s to 20 s at scale 1.0.
27. **The legacy export is read once for both passes.**
28. **Report percentages use three decimals and never round up to 100%.**
29. **Isolated test configs copy `.env`**, so the Postgres and SQL Server tests find their credentials; they skip when the engine is not reachable, so CI stays green without Docker.

### Phase 3 (owner, at planning)

1. **Run only what faults touch.** 3-week scenario, 8 faults covering all 6 types. dbt runs for real only on days a fault is active. Green days run nothing, because the baseline is all green; an e2e test proves a targeted run finds the same failures as a full one. (Selector changed during implementation, see 20.)
2. **Staging roots page the source owner**, because staging only renames and casts. Routing accuracy is reported per person and per team.
3. **`make demo` drops its redundant `seed` and `build` steps** (about 52 s); `make simulate` already does both.

Verified against dbt-core 1.12.5: `dbt build` skips everything downstream of a failed test (`Fail` is in `task/build.py` `MARK_DEPENDENT_ERRORS_STATUSES`), which would hide the alert storm. `dbt run` then `dbt test` skip only on `Error`, so the scenario uses those. Severity inputs (tier-weighted downstream consumers): customers 213, orders 189, order_items and products 160, campaigns and web_sessions 76, support_tickets 34, marketing_spend 20, payments 19.

### Phase 3, made during implementation

17. **A test that reads two models is its own subject.** A relationships test is attached to one model but depends on two. Treating it as its own node below both keeps a volume drop in orders from opening a second incident on order items.
18. **Open questions resolved with the planned defaults**: a recurrence after resolution opens a new incident linked to the old by root; appends send an update, not a page; severity thresholds stay in config.
19. **Repairs regenerate data from the seed**, no backup tables. The generator is deterministic, so "put back what the generator says" is exact; a round-trip test per fault proves every table matches a clean load.
20. **Selection is `@source:raw.<table>`, not `source:raw.<table>+`.** Every model is a table, so downstream-only runs compared fresh payments with a stale `stg_orders` and opened incidents nobody caused. `@` also rebuilds the parents of everything selected. Targeted equals full on all four fault nights.
21. **One parse, reused by every dbt invocation**, including freshness: dbt resolves `simulated_now` at run time, so freshness costs 0.6 s instead of 5.2 s.
22. **Faults land in pairs** on days 2, 5, 9 and 14, on different tables. With the freshness change this took `make incidents` from 3 min 13 s to 2 min 22 to 2 min 56 s.
23. **`dbt run` and `dbt test` are separate invocations with console logging off** in the scenario. Failing tests are expected; dbt's log file keeps the detail.

### Phase 2 (owner)

1. **Logical bytes, not stored bytes** (ADR 0005). Compressed size is not stable between identical runs.
2. **Modelled compute time**: 150 ms per query plus bytes at 200 MB/s, declared in settings and printed in the report. Wall-clock time is recorded, never priced.
3. **Real simulated dollars**, no projection factor.
4. **Products and customers change in place** during the window, so the incremental recommendation has sources to rule out.
5. **One warehouse per workload** (transform, BI, ad hoc) for compute pricing.
6. **BigQuery's 10 MB minimum per table** in scan pricing.
7. **13-week window** (not the SPEC's 6), so the 90-day unused check covers a real 90 days.
8. **Real dbt build every 28 days**, replayed daily in between. Weekly real builds measured at 8 to 9 minutes for `make simulate`, which breaks the 10-minute demo budget.

### Made during implementation

9. **History counts are independent of window length.** `raw_data.HISTORY_COUNTS` is the size by the window start; the id space is extended to cover the window. Otherwise stretching the window to 13 weeks would have shrunk the history.
10. **dbt builds no longer receive `simulated_now`.** Only source freshness uses it, and changing vars forces dbt to re-parse the whole project. With constant vars the saved parse is reused (full parse 9 s, then about 1 s).
11. **Row-count test bounds cover the whole window** (orders up to 680k at scale 1.0). The old upper bound failed mid-window as orders grew; the lower bound, which catches a volume drop, is unchanged.
12. **Payments are never loaded before their order.** Found by comparing seven daily appends with one seed at full scale: a payment stamped minutes before its order was lost when a load boundary fell between them. Tests now check the invariant and many uneven appends.
13. **Daily loads only generate ids that can land in the window**, about 0.4 s a day at scale 1.0 instead of 1.1 s.
14. **Writes batch into one transaction per build interval, and inserts use multi-row `VALUES`.** Measured on this laptop: each commit costs 0.3 to 0.9 s; for 400 rows, `executemany` 0.17 s, Arrow about 0.8 s (a fixed cost per call), multi-row `VALUES` 0.035 s.
15. **The proxy-accuracy report is a separate file.** It uses profiler row counts, which DuckDB does not guarantee to be identical between runs, so it must never affect `cost.md`.
16. **dbt threads 8** (from 4): 26.6 s against 30.0 s per `make build`.

### Earlier phases (still in force)

- Platform team owns sources, staging and intermediate; business teams own marts and exposures; most specific ownership rule wins, ties fail.
- Every model is a full-refresh table; abandoned models keep their team's default tier.
- `run_dbt` releases dbt-duckdb's cached DuckDB handle after every invocation.
- Freshness on simulated time; off for `products` and `marketing_campaigns`.
- GNU make via winget; mypy on its compiled 2.x wheels since Phase 8, still run as `python -m mypy`; CI runs lint, tests and `metadata check --parse` in the container.

## Known issues

For version 2.0:

- Every figure in the README, the module READMEs, the ADRs, the CHANGELOG's 1.x sections and the owner's CV describes 1.x on DuckDB. They stay true for `v1.2.0` and are replaced by 2.0's own results in Phase 15.
- Until each 2.0 phase lands, its module is paused: its command exits 2 and names the phase, `make demo` lists them, and its end-to-end tests skip. `readme-check` reads 1.x reports and is paused with them.
- At scale 1.0 an incremental build is not faster than a full refresh (30 s against 26 s): about 290 nodes of fixed overhead dominate. In bytes read the incremental marts save 77%, but `int_order_items_enriched` and `int_sessions_enriched` read all of raw on every build, because staging is a view over raw sorted by key. A skip index on raw's `updated_at`, or materialized staging for the event tables, would fix it; not done, so the 2.0 numbers stay comparable.
- `readme-check` on the cost README finds six numbers not in `cost.md`, all derived on purpose: the incremental models' bytes read per build (from `ops.query_log`) and the unused tables' monthly total. Phase 15, which brings `readme-check` back, decides whether the report prints them.
- The nightly merge of raw is a quarter of the cluster's compute in the cost report. It is what makes reads repeat; a real team would merge less often or deduplicate with `argMax` in staging.

Kept on purpose (design limits, documented in the ADRs):

- Replayed days price model reads on model sizes up to 4 weeks old, and dashboards read marts up to 4 weeks stale. Raw data is always current. Acceptable for quarterly cost attribution (ADR 0007).
- The bytes estimate ignores row-group pruning, so filtered queries are overestimated; ad hoc queries by more than twice what DuckDB scanned, measured per run in `reports/cost_proxy_accuracy.md`. That file is not deterministic by design (profiler row counts depend on physical row order), so it is excluded when reports are compared byte for byte.
- Filters written against a CTE or subquery column outside it are not traced to the base table for hotspot detection. The workload and dbt do not write filters that way.
- The alert storm is modest: 18 failing checks for 8 faults. The volume drop is the one fault with a real cascade (5 checks).
- Grouping is per run. Two unrelated faults failing the same downstream model in one run attach it to one of them by tie-break (ADR 0008).

The Phase 7 browser check's limits are gone with the 1.x pages (Phase 13b): the monthly chart names each month with the days the window covers in it, and the Incidents page returns in Phase 14.

From Phase 11b (presentation only):

- Every Metabase line ends with a partial period, because the window ends at 02:00 on its last day; the same limit the 1.x cost page had.
- Daily rollups chart only their last 91 days, to stay under Metabase's 2,000-row display limit; the full history is in the weekly and monthly charts.

Kept on purpose since Phases 8 and 9:

- `make docker-browser-check` and `readme-check` stay out of CI (the docs check in CI was dropped from Phase 9).

## Open questions for the owner

None open.

## Next step

1. 15b on the same branch, `phase-15`.
2. Phase 15b (planned, in the plan file): the root README around version 2.0 (the architecture diagram and its steps, the screenshots, how the problems were solved, use it on your platform), an "Adopting it" section in every module README, ADR 0010's later note, the SPEC's layout and Makefile table. Then 15c: the release run at scale 1.0, the committed screenshots, `readme-check` for 2.0, 2.0.0.
3. Pilot rollout of the ownership check, with 1.2.0 (unaffected by 2.0): follow "Rolling out on a repository" in the metadata README and record the baseline here (numbers only).
