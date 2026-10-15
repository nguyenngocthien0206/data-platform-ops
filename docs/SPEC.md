# data-platform-ops: Specification and Build Plan

This document is the source of truth for what to build. Work through the phases in order. Each phase ends with acceptance criteria; stop after each phase for review.

Status: Phases 0 to 9 make up release 1.0.0, the reference toolkit on the simulated company. Phase 10 took the ownership check to the company's dbt repositories (1.1.0, 1.2.0). Phases 11 to 15 rebuild the simulated company on Postgres and ClickHouse for version 2.0. `docs/PROGRESS.md` records where each phase stands.

## Target repository layout

The final state, version 2.0. The phases below record how it got here, so earlier ones name what was later replaced (DuckDB, Iceberg, SQL Server).

```
data-platform-ops/
├── Makefile
├── pyproject.toml
├── Dockerfile                 # the toolkit image (Phase 7)
├── docker-compose.yml         # Postgres, ClickHouse, the toolkit; behind profiles: dashboards,
│                              # browser checks, Metabase, Airflow, Dagster, the chat double
├── .env.example
├── CHANGELOG.md               # release notes, one section per version (Phase 8)
├── .github/workflows/         # CI, the orchestrator checks, the release (Phases 8 and 14)
├── config/
│   ├── settings.yaml          # scale, simulated window, the company, pricing, the stated costs
│   ├── teams.yaml             # teams, members and their Slack and Telegram addresses
│   ├── ownership.yaml         # dataset ownership registry
│   └── reconcile.yaml         # the tables and reports the parallel run compares
├── dbt/                       # the company's dbt project on ClickHouse (dbt-clickhouse)
├── src/platform_ops/
│   ├── common/                # settings, servers, the ops store, dbt invocation, clock, tags
│   ├── metadata/              # registry loader and check, lineage graph
│   ├── simulation/            # the company: legacy Postgres, ingestion, users, BI, faults
│   ├── cost/
│   ├── incidents/
│   ├── reconcile/
│   ├── integrations/          # the hooks Airflow and Dagster call (Phase 14)
│   └── dashboard/             # read-only data layer and charts for the dashboards
├── dashboards/                # streamlit app, one page per module
├── orchestrators/             # Airflow (Cosmos) and Dagster (dagster-dbt) stacks, chat double
├── .streamlit/                # streamlit config (usage stats off)
├── scripts/                   # architecture diagram, screenshots, browser checks, the
│                              # orchestrator check, vendor fixtures, README check, release notes
├── data/                      # scratch space of the data generator, gitignored
├── reports/                   # generated markdown reports and screenshots, gitignored
├── tests/
├── examples/                  # CI examples for company dbt repositories (Phase 10)
└── docs/
    ├── CLAUDE.md
    ├── PROGRESS.md
    ├── SPEC.md
    ├── images/                # the architecture diagram, vendor logos, screenshots (Phase 15)
    ├── review/                # the Phase 6 review record
    └── adr/                   # decision records, indexed in adr/README.md
```

## Makefile targets (final state)

| Target | Purpose |
|---|---|
| `setup` | `uv sync`, install dbt packages |
| `up` / `down` | start Postgres and ClickHouse; stop every service, profiles included |
| `seed` | seed the legacy Postgres, empty ClickHouse, backfill its raw layer |
| `build` | dbt build on ClickHouse as the company's `dbt` user, then refresh the lineage |
| `company` | replay the company's window: daily data, ingestion, builds, the parallel run, production |
| `reconcile` | the cutover sign-off from the parallel run's recorded nights |
| `incidents` | the incident report and postmortems from the store, graded |
| `cost` | share the cluster's bill by team, price it on BigQuery and Snowflake, write the report |
| `metabase` | start Metabase and build one dashboard per exposure |
| `dashboard` | launch streamlit |
| `paused` | confirm no 1.x module is left paused |
| `readme-check` | check every README results number against `reports/` (after `demo`) |
| `test` / `lint` / `fmt` | pytest (extra flags in `PYTEST_ARGS`), ruff, mypy; `fmt` formats and applies safe fixes |
| `clean` | remove generated data, reports and build artifacts |
| `pipeline` | `company`, the ownership check and lineage, `reconcile`, `incidents` and `cost`, in order |
| `demo` | `clean`, `setup`, `up`, then `pipeline`: everything end to end on a clean state |
| `docs-diagram` | redraw `docs/images/architecture.svg` from the project |

In the container (Phase 7 onwards), with only Docker and GNU make on the host:

| Target | Purpose |
|---|---|
| `docker-build` | build the toolkit image |
| `docker-up` | start the engines named in `DOCKER_ENGINES` (default Postgres and ClickHouse) |
| `docker-demo` | `clean` and `pipeline` inside the container (`SCALE=` for a smaller run) |
| `docker-test` / `docker-lint` | the tests (skips listed with their reason) and lint, as CI runs them |
| `docker-metadata-check` | the ownership check |
| `docker-metabase-check` | provision Metabase, run every card, open every dashboard in a browser |
| `docker-readme-check` | `readme-check` inside the container |
| `docker-dashboard` | serve the dashboards on `localhost:8501` |
| `docker-browser-check` | load every dashboard page in headless Chromium, screenshots to `reports/screenshots/` |
| `airflow` / `dagster` | seed the orchestrator's own company and start its stack (Phase 14) |
| `docker-test-orchestrators` | a fault through each orchestrator: its incident, page, fix and resolution |
| `docker-docs-images` | the docs' screenshots from the last run, into `docs/images/screens/` (Phase 15) |
| `docker-shell` / `docker-clean` | a shell in the toolkit container; remove its volumes and the reports |

`TOOLKIT_IMAGE` runs a published image instead of the local build.

---

## Phase 0: Scaffolding

Create the repo layout, `pyproject.toml` with `uv`, `ruff` and `mypy` config, `Makefile`, `.gitignore`, `docker-compose.yml`, and a `typer` CLI entry point `platform-ops` with placeholder subcommands.

Docker Compose:
- `postgres` service (default, always on).
- `sqlserver` service under a Compose profile named `sqlserver`, using the official SQL Server 2022 image with `ACCEPT_EULA` and a password from `.env`. Document in the README that this image is x86_64 only and may be slow or unstable under emulation on Apple Silicon, which is why Postgres is the default legacy source.

A `common.clock` module provides a simulated clock so that workload and incidents can be generated over weeks of simulated time in minutes of real time. Every logged event stores simulated timestamps.

**Acceptance:** `make setup && make up && make test && make lint` pass on a clean checkout. `platform-ops --help` lists the subcommands.

---

## Phase 1: Simulated company and shared metadata layer

### Raw data generator (`simulation/raw_data.py`)

Generate an e-commerce style dataset into DuckDB schema `raw` with a fixed seed and the global scale factor: customers, products, orders, order items, payments, web sessions, marketing campaigns and spend, support tickets. Include realistic mess: some duplicate customers, late-arriving payments, nullable fields, a `_loaded_at` column on each table for freshness checks.

### dbt project (`dbt/`)

Use `dbt-duckdb`. Target around 120 models total, organized as:

- `staging/`: one model per raw table.
- `intermediate/`: shared business logic.
- `marts/` split into four team folders: `sales`, `marketing`, `finance`, `product`.
- `abandoned/`: around 10 to 15 models that are built on every run but never read by anything downstream or by any exposure. These exist on purpose so the cost module has real waste to find. Give them believable names and history (for example an old campaign attribution model that was replaced).

Hand-write the core models so the business logic is meaningful. Generating repetitive variants with a script is fine, but the models must still be valid SQL with real dependencies.

Add:
- dbt tests on key models (unique, not_null, relationships, accepted_values, plus a few custom generic tests such as row count within expected range).
- Source freshness config on raw sources.
- dbt `exposures` representing around 12 dashboards owned by different teams. Exposures are how lineage reaches consumers.
- A `query-comment` config (with `append: true`) that attaches a JSON comment containing the node `unique_id` to every query dbt issues, so queries can be joined back to models.

### Ownership registry (`config/ownership.yaml`, `metadata/registry.py`)

Schema (validate with pydantic):

```yaml
teams:            # in teams.yaml
  - id: finance
    name: Finance Analytics
    channel: "#finance-data"
datasets:         # in ownership.yaml
  - match: "model.company.fct_revenue*"   # glob on dbt unique_id
    owner: alice
    team: finance
    tier: critical        # critical | important | best_effort
```

Resolution rule: most specific match wins; document the rule. Provide a `platform-ops metadata check` command that fails if any dbt model, source, or exposure has no owner. This is the "CODEOWNERS for data" idea and should run in CI.

### Lineage (`metadata/lineage.py`)

Build a directed graph (NetworkX) from `dbt/target/manifest.json` covering sources, models, tests, and exposures. Provide helpers: upstream, downstream, nearest failed ancestor, downstream consumers weighted by tier. Persist edges to `ops.lineage_edges` in DuckDB.

**Acceptance:** `make seed && make build` succeeds. `platform-ops metadata check` passes. A test proves every abandoned model has zero downstream nodes and zero exposures. Lineage helpers have unit tests on a small hand-built graph.

---

## Phase 2: Cost attribution (`cost/`)

### Design principle

Separate collection from pricing. Collection records what each query did. Pricing turns that into money through a pluggable `PricingModel` interface. Attribution and recommendations only depend on the interface, so a future adapter reading BigQuery `INFORMATION_SCHEMA.JOBS` or Snowflake `ACCOUNT_USAGE` can replace the local collector without changes to the core.

### Collection: `ops.query_log`

Two collection paths feed one table:

1. **Non-dbt workload** (dashboards, ad hoc users) runs through a `LoggedConnection` wrapper around DuckDB that records SQL text, actor (service or user), actor type, simulated start time, and wall-clock duration. Where DuckDB profiling is available in the installed version, capture its JSON output per query for rows scanned; verify the exact settings first.
2. **dbt workload** is ingested from `run_results.json` (node id, timing, status) plus the compiled SQL in `target/run/`, stamped with the simulated time of the scheduled run.

For every query, parse with `sqlglot` to extract referenced tables, referenced columns where resolvable, and the destination table if it writes.

### Normalized scan estimate

Because a local DuckDB file has no billing, estimate "bytes scanned" per query from the referenced tables and columns multiplied by their stored sizes (from DuckDB storage metadata). Document this proxy in an ADR, including its known inaccuracies (predicate pushdown, partition pruning, compression). Where profiling data exists, report how close the proxy is.

### Pricing models

- `ScanPricing`: cost proportional to bytes scanned (on-demand style).
- `ComputePricing`: cost proportional to compute time with a minimum billing increment and idle warehouse time between bursts (warehouse credit style).

Rates live in `config/settings.yaml` with a comment pointing to the public vendor price pages they were taken from. The report must be able to run under either model and show a side-by-side comparison.

### Attribution

- **Production cost** of a table: cost of the queries that write it, attributed to the table owner.
- **Consumption cost**: cost of queries that read it, attributed to the reader (dashboard owner via exposure, ad hoc user via team mapping).
- Roll up by team and by month into `ops.cost_by_team`.

### Recommendations

- **Unused tables:** written at least once in the window but never referenced by any read in the last N days (configurable 30 and 90). Use lineage so an intermediate table that feeds a used table is never flagged. Output estimated monthly saving.
- **Full-scan hotspots:** large tables repeatedly read with a filter on the same column. Suggest partitioning or clustering on that column.
- **Incremental candidates:** expensive full-refresh models whose source grows append-only.

### Workload generator (`simulation/workload.py`)

Over a configurable simulated window (default 6 weeks): dbt runs daily at 02:00 simulated time, each exposure dashboard refreshes on its own schedule, a few ad hoc users run queries of varying quality (including `SELECT *` on large tables). Fixed seed.

### Output

`ops.cost_report_*` tables plus a Markdown report written to `reports/cost.md` containing: showback by team, top 10 most expensive models, unused table list with savings, recommendations, and the scan vs compute pricing comparison.

**Acceptance:** `make simulate && make cost` produces the report deterministically. Every abandoned model from Phase 1 appears in the unused list, and no model with a live exposure downstream does. Unit tests cover SQL parsing edge cases (CTEs, subqueries, `CREATE TABLE AS`, quoted identifiers) and both pricing models.

---

## Phase 3: Incident management (`incidents/`)

### Fault injector (`simulation/faults.py`)

Injects labeled faults into raw data between dbt runs and writes ground truth to `ops.fault_ground_truth` (fault id, type, target, simulated injected_at). Fault types:

- null spike in a key column
- duplicate primary keys
- stale source (stop advancing `_loaded_at`)
- schema change (rename or drop a column)
- volume drop (large fraction of rows missing)
- invalid categorical values

### Ingestion

Read `run_results.json` and `sources.json` after each run. Normalize model errors, test failures, and freshness failures into `ops.check_events` with the node they are attached to.

### Root-cause grouping

Within a run, a failing check is a **root** if no ancestor of its attached node also failed. Every other failure attaches to its nearest failed ancestor. Each root becomes one incident; its children are listed as impact, not as separate alerts. If a root is already an open incident from a previous run, append to it instead of opening a new one.

### Severity

Score from the root dataset tier plus the tier-weighted count of downstream models and exposures. Map to SEV1, SEV2, SEV3 with thresholds in config. Document the formula in an ADR.

### Routing and lifecycle

`Notifier` interface with a default implementation that writes to `ops.notifications` and a local log, plus an optional Slack incoming webhook implementation enabled only if a URL is set in `.env`. Incidents move through `open`, `acknowledged`, `resolved`. Simulate acknowledgment and resolution times with a seeded distribution that depends on severity and on how many incidents the owner already has open.

### Metrics

Compute and store: raw alert count vs incident count, routing accuracy (incident routed to the owner of the faulted dataset per ground truth), MTTD (detected minus injected), MTTR (resolved minus detected), alerts per person per week before and after grouping. Generate a postmortem Markdown template per SEV1 incident in `reports/postmortems/`.

**Acceptance:** `make incidents` runs a multi-week scenario with injected faults. Every injected fault maps to exactly one incident. The metrics report shows raw alerts vs incidents from the actual run. Unit tests cover grouping on hand-built graphs, including diamond dependencies and a failure appearing in two consecutive runs.

---

## Phase 4: Migration reconciliation (`reconcile/`)

### Scenario

1. Generate a legacy dataset (orders, customers, payments) into Postgres, and into SQL Server when that profile is enabled. Use types that are known to cause trouble: `NUMERIC` with various scales, `TIMESTAMP` without time zone stored as local time, `TIMESTAMPTZ`, `VARCHAR` with trailing spaces, mixed case text, booleans, NULLs in key-adjacent columns.
2. A migration job copies the data into Iceberg tables (PyIceberg, SQLite catalog, local warehouse directory). The job contains realistic defects on purpose (a timezone mishandled, a decimal cast to float, trailing spaces trimmed, a filter that drops some rows).
3. A fault injector adds further labeled discrepancies to the target and records ground truth.

### Segmented diff algorithm

- Split each table by primary key range into segments.
- For each segment, compute on each side a row count and an aggregate checksum over a canonical string of each row, entirely inside each engine (Postgres or SQL Server on the source side, DuckDB reading Iceberg on the target side).
- Recurse into segments whose checksum differs until segments are small, then fetch those rows only.

The hash must be byte-identical across engines for identical canonical strings. Choose a hash available in all engines, define exactly how it is reduced to an integer and aggregated without overflow, and prove it with a cross-engine test on golden rows.

### Canonicalization layer

Per column type, a canonical rendering rule applied identically on both sides: decimals at a fixed configured scale, timestamps converted to UTC ISO format with microseconds, configurable rtrim, configurable case folding (to model SQL Server case-insensitive collation), a NULL sentinel distinct from empty string, booleans as a fixed literal. Rules are configurable per table and column.

### Root-cause classification

For each mismatched key, compare column by column and classify: `missing_in_target`, `extra_in_target`, `rounding` (difference within tolerance), `timezone_shift` (difference equal to a whole number of hours), `whitespace`, `case_only`, `value_mismatch`.

### Sign-off report

`reports/reconciliation.md`: match rate per table and per column, discrepancy counts by class, sample rows per class, and pass or fail against acceptance thresholds defined in config before the run. The report states the thresholds up front so it can serve as a migration sign-off artifact.

**Acceptance:** `make reconcile` runs end to end against Postgres by default. Detection recall and classification accuracy against ground truth are computed and reported from the actual run. A benchmark in the report shows rows transferred by the segmented diff versus a naive full comparison.

---

## Phase 5: Dashboards, documentation, polish

- Streamlit app with one page per module reading only from the `ops` schema.
- Root `README.md` following the conventions in `CLAUDE.md`: the problem, a Mermaid architecture diagram showing the shared metadata layer and three modules, quickstart, and real results from `make demo`.
- ADRs at minimum for: local-first design and the scan estimate proxy, pluggable pricing models, ownership resolution rule, incident grouping and severity, cross-engine hashing and canonicalization.
- A GitHub Actions workflow that runs lint, tests, and `platform-ops metadata check` (no Docker services needed for unit tests).
- Cloud-ready cost collectors: `BigQueryJobsCollector` and `SnowflakeQueryHistoryCollector` implementing the Phase 2 collection interface, verified only by contract tests against recorded fixture files under `tests/fixtures/` (a few hundred rows in each vendor's documented schema). No network access and no cloud account are involved.

**Acceptance:** a fresh clone followed by `make setup && make up && make demo && make dashboard` works, and every number in the README matches the generated reports.

## Phase 6: Whole-repo review and cleanup

Five phases were built one after another under a time budget. Before the release, review the whole repository and pay down what that left behind, without changing what the toolkit does.

- Enable a broader lint rule set permanently and fix what it finds. Remove formatter and lint suppressions that do nothing.
- Remove duplication and cross-module shortcuts: shared helpers (Markdown report formatting, replacing an `ops` table, writing an isolated config) live in one place, and modules do not import each other's internals.
- Review every module for correctness, dead code (checked against a coverage report), consistent naming and error messages, docstrings that no longer match the code, and missing tests. Record each finding in `docs/review/phase-6-review.md` with its location, severity and decision (fixed, accepted with a reason, or deferred to Phase 7).
- Make the test suite faster without losing coverage, by sharing expensive warehouses between integration tests where a test does not need its own.
- Bring the docs back in line with the code: `PROGRESS.md` known issues, module READMEs, ADRs.

**Acceptance:** `make demo` writes byte-identical reports before and after the phase. `make readme-check`, `make test` and `make lint` (with the broader rules) pass. `make test` runs in under 5 minutes, or the review record states the measured floor and why. Every finding in the review record has a decision.

---

## Phase 7: Containerized rerun and verification

Package the toolkit in a container and rerun everything through Docker Compose, so the project runs the same way on any machine with Docker and no longer depends on the host's Python, uv or security policies. There is no time budget for `make demo` from this phase on; the measured times are recorded instead.

- A `Dockerfile` for the toolkit (Python 3.11, dependencies installed from `uv.lock`, including the optional SQL Server driver) and an `app` service in `docker-compose.yml` next to Postgres and the SQL Server profile. Generated data, the Iceberg warehouse and reports live on volumes or bind mounts so they survive the container.
- Make targets to run the toolkit inside the container (demo, test, lint, readme-check, dashboard with its port published), next to the existing host targets.
- Rerun everything inside the container on a clean state: `make demo`, the full test suite with Postgres and SQL Server both up (so nothing skips), `make readme-check`, and the dashboards, checked in a real browser.
- Verify the results: two container runs write byte-identical reports; any difference from the native Windows run is explained or fixed; every number the READMEs quote still holds.
- Carry over the Phase 6 deferrals that belong here: `.gitattributes` with `eol=lf`, and a decision on the pandas and pyarrow caps added for Windows Smart App Control.
- Record the measured duration of every step in `PROGRESS.md`.

**Acceptance:** from a fresh clone, building the image and running the demo, the full test suite (no skips) and `readme-check` inside Docker Compose all pass. Two container runs write byte-identical reports. The dashboards served from the container have been checked in a browser. Measured times are recorded.

---

## Phase 8: CI and release

Automate the checks and publish the toolkit as 1.0.0.

- CI on every pull request and push to `main`: lint, type check, the full test suite and `metadata check`, run in the toolkit's container with a Postgres service. SQL Server runs in a separate job that can be triggered on demand, because its image is large and slow to start.
- On a pushed version tag (`v*`): build the image, push it to GitHub Container Registry, and create a GitHub Release whose notes come from `CHANGELOG.md`. Nothing is published on ordinary pushes.
- Remove the last tooling workaround left from Windows Smart App Control, now switched off: mypy's `<1.20` cap and its build from source (`no-binary-package`), so the type check runs on compiled wheels. Fix whatever a newer mypy reports. `make lint` keeps calling `python -m mypy`, which costs nothing and still suits managed machines. Measure `make docker-lint` before and after.
- `CHANGELOG.md` covering Phases 0 to 8, version 1.0.0 in `pyproject.toml` and `platform-ops version`, and the exact commands for the owner to create and push the `v1.0.0` tag. The owner creates and pushes the tag.

**Acceptance:** CI passes on a pull request. `make lint` passes on an uncapped, compiled mypy, with the lint time before and after recorded. After the owner pushes the tag, the image can be pulled from GHCR and runs the demo, and the GitHub Release exists with the changelog notes. `CHANGELOG.md`, the version and the tag agree.

---

## Phase 9: Documentation for 1.0.0

Close out 1.0.0 as the reference release: the demo, its reports and its decisions documented as they are. Trimmed by the owner after Phase 8, because the toolkit now heads for internal use; documentation for colleagues is written with each module in Phases 10 to 12.

- Root `README.md`: a short "Run it in Docker" section (build or pull the image, `make docker-demo`, the dashboards) next to the native quickstart, and every stated time taken from a real run.
- ADRs for the decisions of Phases 6 to 8 (the container, CI and release), and an index of all ADRs. Earlier ADRs are not rewritten; where a later phase changed their context (the lifted 10-minute budget, the lifted pandas and pyarrow caps in ADR 0007), a short note points to the newer decision.
- The SPEC's Makefile table lists the `pipeline` and `docker-*` targets.
- Fix what the docs state wrongly; module READMEs and the dashboards README are checked against the code and the latest reports.
- `docs/PROGRESS.md` and `docs/SPEC.md` closed out for 1.0.0: Phases 0 to 9 marked done, known issues either fixed or stated as limits.

Not in this phase: a Docker-first rewrite of the README for outside readers, and a documentation check in CI.

**Acceptance:** following the README's Docker section from a fresh clone runs the demo and serves the dashboards. `make readme-check` passes. Every relative link in the docs resolves (checked once, locally). Every ADR is listed in the index.

---

## Internal use (from Phase 10)

From here the toolkit is adopted by the teams inside the company. The owner's decisions after Phase 8:

- **Internal only, not open source.** Only the company's stack is supported: Snowflake, BigQuery or ClickHouse as the warehouse, dbt run from Airflow and from Dagster, alerts to Slack or Telegram.
- **One module per phase, lowest integration cost first:** ownership first (Phase 10). After 1.2.0 the owner reordered the rest around version 2.0 (below).
- **Shared state lives in one Postgres database** for the toolkit, the same whatever the warehouse.
- **Airflow (through Cosmos) and Dagster (through dagster-dbt) are supported through thin integrations** in this repo that import neither orchestrator. The core stays a CLI and a Python API, and the toolkit never becomes an orchestrator.

The simulated company, `make demo` and the test suite stay local, offline and deterministic. They remain the regression test for every module. Phase 10 did not change their reports; version 2.0 changes them on purpose and records a new baseline. Code that talks to company systems sits behind the existing interfaces, is never needed by `make demo` or `make test`, and reads credentials only from the environment.

Each phase starts with a pilot team chosen by the owner and a measure taken before the module is switched on, so the phase can end with a before-and-after number rather than a demo.

---

## Phase 10: Ownership in the company's dbt CI

Make every company dbt model, source and exposure have exactly one owner before it merges. This is the foundation the incidents and cost modules route on.

The toolkit repository stays public and holds only code and the demo. The company's `ownership.yaml` and `teams.yaml` live in the company's own dbt repositories, next to the project they describe; CI pulls the toolkit image and points it at them. No company configuration, names or channels ever enter this repository (owner decision before Phase 10).

- `platform-ops metadata check` runs against any dbt project, not only the bundled one: the project directory, `ownership.yaml` and `teams.yaml` are given by path or environment, and the check works from a `manifest.json` (or `--parse`) whatever the dbt adapter (Snowflake, BigQuery, ClickHouse).
- `teams.yaml` gains notification targets per team: a Slack channel and a Telegram chat, used from Phase 11.
- Output a CI reviewer can act on: every unowned or ambiguously owned node, the rule that matched, and the file to edit. Exit codes suitable for a required CI check.
- A documented CI step for the company's dbt repositories, using the published image, with a GitHub Actions workflow as the example (the company's CI).
- Tested against at least one dbt project other than the bundled one: a small fixture project on the pilot's engine, self-managed ClickHouse (`dbt-clickhouse`), with a different layout. A ClickHouse service behind a Compose profile lets the fixture project really run, so its artifacts are real and Phases 11 and 12 can reuse it.

**Acceptance:** the check passes and fails correctly on the fixture project and on the bundled one, from the published image, with no access to any warehouse. The pilot repository runs it as a CI step (the owner wires it). The bundled demo's reports are unchanged.

---

## Version 2.0: the simulated company migrates to ClickHouse (Phases 11 to 15)

Decided by the owner after 1.2.0 (2026-10-13). In 1.x the simulated company ran on DuckDB, with Postgres and SQL Server only as the sources of a migration into Iceberg, and the Phase 10 tests needed a second dbt project on ClickHouse. Version 2.0 tells one story that mirrors the company's real stack:

The company runs its reporting on a **legacy Postgres** database: operational tables, plus reporting tables and views written by hand in SQL. It is **migrating to self-managed ClickHouse**, where the analytics are rebuilt as **one dbt project**. The toolkit is applied to that flow: reconciliation signs off the migration, ownership and incidents run on the new dbt project, and cost attribution and FinOps run on ClickHouse's own query log, with **BigQuery and Snowflake priced as alternative targets**.

Decisions:

- **One company, one dbt project, on ClickHouse** (`dbt-clickhouse`). The legacy reporting on Postgres is hand-written SQL, not dbt. The Phase 10 fixture project is retired once the company's project replaces it in the tests.
- **DuckDB stops being the warehouse; Iceberg and SQL Server are removed.** DuckDB may stay as an in-process helper only where a test needs one. Version 1.x remains available at tag `v1.2.0`.
- **Cost uses ClickHouse's measured bytes and rows** from `system.query_log`; compute time stays modelled from bytes, so reports stay byte-identical across runs. If measured bytes turn out not to repeat exactly, the owner decides before the rule is relaxed.
- **BigQuery and Snowflake are priced, not built:** scan and compute pricing on the same measured workload, as candidate migration targets. Their collectors stay contract-tested only.
- **Every number is regenerated.** READMEs, reports, ADR figures and the CV numbers come from 2.0's runs; 2.0's reports are not comparable byte for byte with 1.x, and each phase records its own baseline. Determinism, the offline demo and `make demo` as one command still hold, now on Postgres and ClickHouse in Docker.
- **Order follows the migration:** the company first, then reconciliation, then cost and FinOps, then incidents (the former Phases 11 and 12 are folded into Phases 13 and 14).
- Between Phases 11 and 14, `make demo` runs the modules already moved to 2.0 and says which ones it skipped.

---

## Phase 11: The company on Postgres and ClickHouse

- **Legacy Postgres:** the raw data generator writes the operational tables (customers, products, orders, order items, payments, web sessions, campaigns and spend, support tickets) into Postgres, with the same seeded mess as 1.x. A legacy reporting layer of hand-written SQL views and tables produces the reports the business reads today.
- **Incremental loads into ClickHouse:** an ingestion job pulls new and changed rows from Postgres into ClickHouse's raw layer on the simulated clock, by an `updated_at` watermark through ClickHouse's `postgresql()` table function, the way a team without CDC feeds its new platform. Late rows and watermark edges happen for real, and the job records what each load moved.
- **The dbt project on ClickHouse, modelled the ClickHouse way:** the 1.x project (about 120 models, owners, tests, source freshness, exposures, abandoned models) ported from DuckDB SQL to ClickHouse SQL with the same structure, lineage and ownership registry, but not as nightly full refreshes. Tables whose rows change in place (customers, products) use `ReplacingMergeTree` and are read deduplicated; large event tables use `MergeTree` with chosen `ORDER BY` and partition keys and build incrementally; small marts may stay full-refresh tables. Each choice of engine and keys is written down, because cost recommendations (Phase 13) build on it.
- **Metabase as the company's BI:** behind a Compose profile, with its application database on the same Postgres server. The dbt exposures (about 12 dashboards) are created in Metabase by a script from the exposures themselves, on the marts in ClickHouse, querying as a `metabase` ClickHouse user. `make demo` replays the same dashboard queries from the seeded workload generator under that user, so the BI load in `query_log` stays deterministic; a live Metabase's own queries never feed the demo's reports.
- **One ClickHouse user per team, plus `metabase` and `dbt`**, so `query_log` records who ran what the way a real cluster does. The workload generator runs ad hoc queries as the team users.
- **The workload and fault simulations** target ClickHouse.
- **Ownership and lineage** run on the ported project; the Phase 10 fixture project and its tests move to the company's project.
- Where the toolkit's own result tables (`ops`) live in 2.0 is decided at the start of this phase.

**Acceptance:** `make demo` seeds Postgres, loads ClickHouse incrementally and builds the dbt project on it, with the engine and keys of every model recorded. `metadata check` passes on the ported project. The Metabase profile serves every exposure as a dashboard on ClickHouse. The 1.x structural invariants hold (abandoned models are dead ends, every live model reaches an exposure, one owner each). Two clean runs produce identical per-model fingerprints (row count and a content hash) in ClickHouse. The time of each step is recorded.

---

## Phase 12: Reconciliation from Postgres to ClickHouse

- A ClickHouse connector behind the existing interface, whose canonical row string and MD5 hash are byte-identical to Python's and Postgres's (ADR 0006), proven by the golden-row test extended to ClickHouse.
- ClickHouse's own traps handled by canonical rules: no enforced primary key (deduplicate before comparing), `Nullable`, `DateTime64` precision, `FixedString` padding, case-sensitive comparison.
- Tables to reconcile declared in configuration instead of hard-coded.
- Two levels of sign-off: the migrated operational tables, and the legacy reports against the dbt marts that replace them ("the new dashboards show the same revenue as the old reports").
- **A parallel run before cutover:** for a stated number of simulated weeks, legacy and ClickHouse both run, and the reconciliation runs every night. The report shows the match rate per table over the window, and the cutover is recommended only once every table has met its thresholds for N consecutive nights, the way a migration is actually signed off.
- The planted defects move into the Postgres to ClickHouse load (a time zone mishandled, a decimal cast to float, trailing spaces trimmed, rows dropped at batch boundaries), plus the labelled discrepancy injector.

**Acceptance:** `make demo` runs the parallel window as part of the company's history (decided with the owner at planning: the parallel run is the last weeks of the company's own window, not a separate scenario), and `make reconcile` writes the sign-off from its nights: the cutover recommended (or not) against thresholds fixed before the run, with recall and classification accuracy against ground truth, and the segmented diff against a naive comparison, all computed from the actual run. Reports byte-identical across two runs.

---

## Phase 13: Cost attribution and FinOps on ClickHouse

- A collector for `system.query_log`: measured bytes and rows read, the query kind, the user, and the dbt query comment for model ids. The logged-connection proxy of 1.x is no longer needed where ClickHouse measures.
- Pricing: the self-managed cluster's stated monthly cost (from configuration), split into **compute**, shared by measured bytes and rows read per query, and **storage**, shared by each owner's tables from `system.parts` (by rows and uncompressed bytes, which repeat across runs, not compressed size), alongside BigQuery scan pricing and Snowflake compute pricing on the same workload: what the same company would pay on each candidate target.
- **The migration's business case:** the legacy Postgres's stated cost before against ClickHouse's after, from the same simulated window.
- **Per-team users and quotas:** showback by ClickHouse user and team (the `metabase` user's load goes to the owners of the dashboards it serves), and suggested settings profiles or quotas for the heaviest teams, the lever a real platform team pulls.
- Attribution, showback by team and month, and recommendations on ClickHouse: unused tables, ORDER BY and partition keys for full-scan hotspots, incremental candidates.
- Query text handled as sensitive: what attribution needs is stored, literals can be redacted, raw text stays out of reports.

**Acceptance:** compute and storage shares add up to the stated infrastructure cost; the before-and-after cost of the migration comes from the same window; every abandoned model is in the unused list and no model with a live exposure is; the BigQuery and Snowflake comparison comes from the same measured workload; reports byte-identical across two runs. For the real pilot cluster, the same collector and pricing run against its query log with a read-only role, after the access and data handling review.

Decided with the owner at planning (2026-10-13): the phase is split into **13a**, the cost core and `reports/cost.md`, and **13b**, the Streamlit dashboards back on the Postgres ops store with the browser check. Prices are public on-demand list prices with their sources; the raw layer is merged every night so measured reads repeat; quotas are suggested, never applied. During 13a, compute was shared by bytes read alone (rows read are reported alongside, and two weights would need a ratio nobody could defend) and storage by uncompressed bytes ([ADR 0015](adr/0015-finops-on-a-self-managed-clickhouse.md)).

13b, decided with the owner at planning (2026-10-14): the dashboards read the Postgres ops store as their own read-only role (`POSTGRES_PASSWORD_DASHBOARD`, no default); the cost report stores everything it states (month pools, business case, recommendations) so the pages never recompute; the Reconciliation page shows the parallel run night by night; the Incidents page returns in Phase 14. **Acceptance:** every page renders on a real run in `AppTest` and in headless Chromium in both themes; the role reads every ops table and cannot write; reports and the ops store byte-identical across two runs.

---

## Phase 14: Incidents on real dbt runs

Turn failing dbt checks into one incident per root cause, routed to the right owner, instead of one alert per failure: first in the simulated company on ClickHouse, then on the company's real runs from Airflow (Cosmos, one dbt invocation per model) and Dagster (dagster-dbt).

- The simulated incident scenario runs on the ClickHouse project and stays the regression test, with its ground truth.
- `platform-ops incidents ingest` reads the artifacts of one dbt invocation (`run_results.json`, `sources.json`, `manifest.json`) from any adapter. Invocations that share an orchestrator run id form one run, regrouped on every ingest, so Cosmos's per-model invocations group like a single `dbt build`. Ingesting the same results twice changes nothing.
- Incident state in the shared Postgres store (runs, check events, incidents, notifications), safe under concurrent ingests, with its schema versioned.
- Lifecycle: open on a new root failure, append when it fails again, resolve automatically when a later run shows the root healthy (its model ran and every test on it passed, or its source is fresh again). A run that did not cover the root never resolves it.
- Severity thresholds and tier weights from an optional `incidents.yaml` next to the registry.
- Notifiers per team from `teams.yaml`: Slack through a bot token (`chat.postMessage` to the team's `channel`) and Telegram through a bot token (to the team's `telegram_chat`), sent only after the incident is committed, delivery recorded.
- Integrations that import neither orchestrator, so the toolkit's image and lock file never carry Airflow or Dagster: a callback for Cosmos's per-task hook and a helper for a dagster-dbt `@dbt_assets` function. Airflow and Dagster call them and hand over what they need (the task's project folder and run id, the dbt invocation's target path and run id).
- Airflow and Dagster for real, in Docker: an Airflow image with Cosmos and a Dagster image with dagster-dbt, each with the toolkit installed, kept apart from the toolkit's image. Both run the simulated company's dbt project on ClickHouse on its daily schedule, behind Compose profiles, with their UIs published: the company runs dbt the way the real company does. `make demo` stays on the Makefile and the simulated clock, deterministic and one command.
- Integration tests on the real orchestrators (`make docker-test-orchestrators`): break a staging test, trigger the DAG and the Dagster job, check that one incident opens in the Postgres store and is paged; repair, trigger again, check that it resolves. Run on demand (its own workflow) and by the release workflow before it publishes; pull requests run the fast tests with fakes.
- A live mode of the dashboards reading the store, starting with incidents; `platform-ops incidents summary` prints failing checks against pages per person and week.

**Acceptance:** the simulated scenario maps every injected fault to exactly one incident on ClickHouse. Fixture artifacts from Snowflake and BigQuery runs and real ClickHouse artifacts ingest into the incidents the grouping rules predict. Several Cosmos-style partial invocations of one run group like a single build. A failing build of the company project opens one incident paged to the source owner, and the next clean build resolves it, end to end on ClickHouse and Postgres. The integrations pass with fakes on every pull request and end to end on real Airflow (Cosmos) and Dagster (dagster-dbt) in Docker, on demand and before every release; the release does not publish without them. Slack and Telegram messages go through a test double. The live dashboard renders against a seeded store. The pilot records pages per person per week before and after.

Decided with the owner at planning (2026-10-14): the phase is split into **14a**, the core (the incidents in the company's own timeline, `incidents ingest`, the Postgres store, the lifecycle, `incidents.yaml`, Slack and Telegram behind test doubles, `incidents summary`, the Incidents page), and **14b**, real Airflow (Cosmos) and Dagster (dagster-dbt) in Docker with their integration tests and the release gate. The incidents happen after cutover, in three weeks of production that extend the window to 16 weeks: every night runs freshness, run, snapshot and test, faults happen in operation, and the company's desk ingests every invocation through the same code `incidents ingest` runs ([ADR 0016](adr/0016-incidents-on-the-companys-production-runs.md)). The 1.x duplicate-keys fault became orphan keys, because the raw `ReplacingMergeTree` deduplicates keys by design. 14b is done in three parts, one PR each, decided with the owner on 2026-10-14: the hooks the orchestrators call and a chat double (14b-1), Airflow 3 with Cosmos (14b-2), and Dagster with dagster-dbt with the release gate (14b-3). The orchestrators call the toolkit's CLI, not its library, each from its own image, each running its own company in its own namespace ([ADR 0017](adr/0017-orchestrators-call-the-toolkit.md)). Under an orchestrator, which runs on the wall clock, source freshness is measured on the company's own clock, where its latest load reached (`platform-ops company clock`), decided with the owner on 2026-10-15. Phase 14 was completed on 2026-10-15: a fault through either orchestrator opens one incident, pages it and resolves it after the fix, and the release waits for both checks.

---

## Phase 15: Documentation and release 2.0.0

- The root README rewritten around the migration story, with an updated architecture diagram and results from a real 2.0 run; module READMEs and the dashboards README brought in line.
- ADRs for the 2.0 decisions (the re-platform, ClickHouse hashing, cost on measured bytes, the live incident store), and a later note on every 1.x ADR the re-platform changed (0009 and 0010 at least; the three 1.x records the re-platform superseded were retired).
- The SPEC's layout and Makefile table, `docs/PROGRESS.md` closed out, `CHANGELOG.md` 2.0.0; the owner pushes the tag.

**Acceptance:** a reader goes from a fresh clone to the dashboards by following the README; `make readme-check` passes against a fresh 2.0 run; every relative link resolves; every ADR is in the index.

---

## Out of scope

Open-source distribution and outside support; warehouses beyond ClickHouse, with BigQuery and Snowflake priced but not built; Iceberg and SQL Server (removed in 2.0); replacing or running as an orchestrator (Airflow and Dagster are supported only through the thin integrations of Phase 14); authentication and multi-user deployment of the dashboards; cloud emulators; and any UI beyond the Streamlit dashboards.
