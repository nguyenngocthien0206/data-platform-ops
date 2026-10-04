# data-platform-ops: Specification and Build Plan

This document is the source of truth for what to build. Work through the phases in order. Each phase ends with acceptance criteria; stop after each phase for review.

Status: Phases 0 to 9 make up release 1.0.0, the reference toolkit on the simulated company. Phases 10 to 12 plan its adoption inside the company. `docs/PROGRESS.md` records where each phase stands.

## Target repository layout

```
data-platform-ops/
├── Makefile
├── pyproject.toml
├── Dockerfile                 # the toolkit image (Phase 7)
├── docker-compose.yml         # legacy databases, the toolkit, dashboards, browser check
├── .env.example
├── CHANGELOG.md               # release notes, one section per version (Phase 8)
├── .github/workflows/         # CI, the on-demand SQL Server job, the release (Phase 8)
├── config/
│   ├── settings.yaml          # scale factor, paths, simulated time window, pricing rates
│   ├── teams.yaml             # teams and their notification channels
│   └── ownership.yaml         # dataset ownership registry
├── dbt/                       # simulated company dbt project (dbt-duckdb)
├── src/platform_ops/
│   ├── common/                # config loading, DuckDB connection, simulated clock, logging
│   ├── metadata/              # registry loader and validation, lineage graph
│   ├── simulation/            # raw data generator, workload generator, fault injectors
│   ├── cost/
│   ├── incidents/
│   ├── reconcile/
│   └── dashboard/             # read-only data layer and charts for the dashboards
├── dashboards/                # streamlit app, one page per module
├── .streamlit/                # streamlit config (usage stats off)
├── scripts/                   # vendor fixtures, README check, dashboard browser check, release notes
├── data/                      # generated DuckDB warehouse file, gitignored
├── warehouse/                 # local Iceberg warehouse, gitignored
├── reports/                   # generated markdown reports, gitignored
├── tests/
├── examples/                  # CI examples for company dbt repositories (Phase 10)
└── docs/
    ├── CLAUDE.md
    ├── PROGRESS.md
    ├── SPEC.md
    ├── review/                # the Phase 6 review record
    └── adr/                   # decision records, indexed in adr/README.md
```

## Makefile targets (final state)

| Target | Purpose |
|---|---|
| `setup` | `uv sync`, install dbt packages |
| `up` / `down` | start and stop Docker services |
| `seed` | generate raw data for the simulated company |
| `build` | run dbt build |
| `simulate` | run the workload generator over the simulated time window |
| `cost` | collect, price, attribute, and write the cost report |
| `incidents` | inject faults, run dbt, detect and group incidents, write metrics |
| `reconcile` | run the migration scenario and the diff, write the sign-off report |
| `dashboard` | launch streamlit |
| `readme-check` | check every README results number against `reports/` (after `demo`) |
| `test` / `lint` / `fmt` | pytest (extra flags in `PYTEST_ARGS`), ruff, mypy; `fmt` formats and applies safe fixes |
| `clean` | remove generated data, reports and build artifacts |
| `pipeline` | `simulate`, `cost`, `incidents` and `reconcile` in order, on the current state |
| `demo` | `clean`, `setup`, `up`, then `pipeline`: everything end to end on a clean state |

In the container (Phase 7 onwards), with only Docker and GNU make on the host:

| Target | Purpose |
|---|---|
| `docker-build` | build the toolkit image |
| `docker-up` | start the legacy engines named in `DOCKER_ENGINES` (default Postgres and SQL Server) |
| `docker-demo` | `clean` and `pipeline` inside the container |
| `docker-test` / `docker-lint` | the tests (skips listed with their reason) and lint, as CI runs them |
| `docker-metadata-check` | the ownership check |
| `docker-readme-check` | `readme-check` inside the container |
| `docker-dashboard` | serve the dashboards on `localhost:8501` |
| `docker-browser-check` | load every dashboard page in headless Chromium, screenshots to `reports/screenshots/` |
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
- ADRs for the decisions of Phases 6 to 8 (the container, CI and release), and an index of all ADRs. Earlier ADRs are not rewritten; where a later phase changed their context (the lifted 10-minute budget, the lifted pandas and pyarrow caps in ADR 0010), a short note points to the newer decision.
- The SPEC's Makefile table lists the `pipeline` and `docker-*` targets.
- Fix what the docs state wrongly; module READMEs and the dashboards README are checked against the code and the latest reports.
- `docs/PROGRESS.md` and `docs/SPEC.md` closed out for 1.0.0: Phases 0 to 9 marked done, known issues either fixed or stated as limits.

Not in this phase: a Docker-first rewrite of the README for outside readers, and a documentation check in CI.

**Acceptance:** following the README's Docker section from a fresh clone runs the demo and serves the dashboards. `make readme-check` passes. Every relative link in the docs resolves (checked once, locally). Every ADR is listed in the index.

---

## Internal use (Phases 10 to 12)

From here the toolkit is adopted by the teams inside the company. The owner's decisions after Phase 8:

- **Internal only, not open source.** Only the company's stack is supported: Snowflake, BigQuery or ClickHouse as the warehouse, dbt run from Airflow and from Dagster, alerts to Slack or Telegram.
- **One module per phase, lowest integration cost first:** ownership, then incidents, then cost for one engine. Reconciliation waits until the company has a migration.
- **Shared state lives in one Postgres database** for the toolkit, the same whatever the warehouse.
- **Airflow and Dagster are supported through thin, optional integrations** in this repo (`platform-ops[airflow]`, `platform-ops[dagster]`). The core stays a CLI and a Python API, and the toolkit never becomes an orchestrator.

The simulated company, `make demo` and the test suite stay local, offline and deterministic. They remain the regression test for every module, and nothing in Phases 10 to 12 may change their reports. Code that talks to company systems sits behind the existing interfaces, is never needed by `make demo` or `make test`, and reads credentials only from the environment.

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

## Phase 11: Incidents on real dbt runs

Turn the company's failing dbt checks into one incident per root cause, routed to the right owner, instead of one alert per failure.

- `platform-ops incidents ingest` reads the artifacts of one dbt invocation (`run_results.json`, `sources.json`, `manifest.json`) from any adapter, and runs the existing ingestion, grouping, severity and routing.
- Incident state in the shared Postgres database (open incidents, appends, pages sent), safe when several Airflow or Dagster workers ingest at the same time, with its schema versioned.
- Lifecycle for real runs: open on a new root failure, append when it fails again, resolve automatically when a later run passes the root's checks. Acknowledgement stays optional.
- Notifiers: the existing Slack webhook, and Telegram (bot token and chat from the environment), chosen per team from `teams.yaml`.
- Integrations as optional extras: for Airflow, a callback or operator to add after the dbt task; for Dagster, a sensor or hook after the dbt assets run. Both call the same ingest.
- The simulated 21-day scenario keeps running through the same code path and stays the regression test, with its ground truth.

**Acceptance:** fixture artifacts from Snowflake, BigQuery and ClickHouse dbt runs ingest into the same incidents the grouping rules predict. An example Airflow DAG and an example Dagster job ingest a failing run end to end against a local Postgres. Slack and Telegram messages are rendered and sent through a test double. `make demo` reports are byte-identical. The pilot team runs it; the pages per person per week before and after are recorded.

---

## Phase 12: Cost for the first engine

Attribute one warehouse's real query cost to the owning teams. The engine (Snowflake, BigQuery or ClickHouse) is chosen at planning, by where the largest bill is.

- A collector for that engine reading its query history with a read-only role (the Phase 5 collector for BigQuery or Snowflake, verified against a real account; or a new one for ClickHouse's `system.query_log`), with a pricing model that matches how the company is billed. For self-managed ClickHouse, which has no vendor bill, the pricing model shares the stated infrastructure cost of the cluster by usage.
- Query text handled as sensitive: the toolkit stores what attribution needs, can redact literals, and keeps raw text out of reports.
- Attribution and showback through the existing core, with results in the shared Postgres database, run on a schedule from Airflow or Dagster.
- The access and data handling are reviewed with whoever owns security or data governance before the first run against production.

**Acceptance:** a showback for the pilot's warehouse over a real window, reconciled against the vendor's own bill for the same window within a stated tolerance, or, for self-managed ClickHouse, adding up to the infrastructure cost the owner states for that window. Contract tests for the collector, and for ClickHouse an integration test against the local ClickHouse service. `make demo` reports are byte-identical.

---

## Out of scope

Open-source distribution and outside support; warehouses beyond Snowflake, BigQuery and ClickHouse; replacing or running as an orchestrator (Airflow and Dagster are supported only through the thin integrations of Phase 11); authentication and multi-user deployment of the dashboards; cloud emulators; and any UI beyond the Streamlit dashboards. Migration reconciliation for company systems waits for a real migration.
