# data-platform-ops: Specification and Build Plan

This document is the source of truth for what to build. Work through the phases in order. Each phase ends with acceptance criteria; stop after each phase for review.

## Target repository layout

```
data-platform-ops/
├── CLAUDE.md
├── Makefile
├── pyproject.toml
├── docker-compose.yml
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
│   └── reconcile/
├── dashboards/                # streamlit app, one page per module
├── tests/
└── docs/
    ├── SPEC.md
    └── adr/
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
| `test` / `lint` | pytest, ruff, mypy |
| `demo` | everything above end to end on a clean state |

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

## Out of scope

Authentication, multi-user deployment, cloud emulators, and running against real cloud accounts (the collector interfaces must be ready for it, but everything runs on local Docker), orchestration frameworks, and any UI beyond the Streamlit dashboards.