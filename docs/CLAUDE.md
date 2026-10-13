# CLAUDE.md

## What this project is

`data-platform-ops` is a local-first toolkit for operating a multi-team data platform. It runs a simulated company (4 business teams plus a data platform team, around 120 dbt models) and ships three modules on top of one shared metadata layer:

1. `cost`: attributes query cost to owning teams and flags waste.
2. `incidents`: turns raw data test failures into root-cause incidents routed to owners.
3. `reconcile`: verifies a migration with a segmented checksum diff.

Version 1.x (up to 1.2.0) ran the simulated company on DuckDB. From Phase 10 the toolkit is also adopted by the teams inside the company (not open source): their dbt projects, run from Airflow (Cosmos) and Dagster (dagster-dbt), with alerts to Slack or Telegram.

Version 2.0 (Phases 11 to 15) rebuilds the simulated company to mirror the company's real stack: its reporting runs on a legacy Postgres database (hand-written SQL), and it is migrating to self-managed ClickHouse, where the analytics are one dbt project. Reconciliation signs off that migration; ownership and incidents run on the ClickHouse dbt project; cost and FinOps run on ClickHouse's query log, with BigQuery and Snowflake priced as alternative targets. One company, one dbt project. The simulated company stays the demo and the regression test.

The full specification and the phased build plan live in `docs/SPEC.md`. Read it before starting any phase.

## Hard constraints

- The demo and the test suite run on a laptop with 16 GB RAM, offline after setup, with no cloud accounts, paid services or API keys. From Phase 10 the toolkit also connects to company systems (warehouses, a shared Postgres, Slack, Telegram, Airflow, Dagster); that code sits behind the existing interfaces, is optional, is never needed by `make demo` or `make test`, and reads credentials only from the environment.
- One command brings the stack up (`make up`), one command runs the full demo (`make demo`).
- All randomness uses fixed seeds. Two runs of `make demo` on a clean checkout must produce identical reports.
- Dataset size is controlled by a single scale factor in `config/settings.yaml`. There is no fixed time budget for `make demo` (the original 10-minute limit was lifted by the owner before Phase 7); the measured duration of each step is recorded in `docs/PROGRESS.md` and stated wherever a README quotes it.
- Work one phase at a time as defined in `docs/SPEC.md`. At the end of each phase, stop, summarize what was built, show how to verify the acceptance criteria, and wait for review before continuing.

## Stack

- Python 3.11+, dependencies managed with `uv` (single `pyproject.toml`).
- From 2.0: Postgres as the simulated company's legacy database, self-managed ClickHouse as its warehouse, `dbt-clickhouse` for transformations, all in Docker Compose. Until each 2.0 phase lands, its module still runs on the 1.x stack (DuckDB with `dbt-duckdb`, Postgres and SQL Server as legacy sources, Iceberg via PyIceberg); DuckDB, SQL Server and Iceberg leave the stack in 2.0.
- BigQuery and Snowflake are priced as alternative targets only; their collectors stay contract-tested against recorded fixtures.
- From 2.0, Metabase is the simulated company's BI tool, behind a Compose profile (its application database on the same Postgres server). The toolkit's own dashboards stay in Streamlit.
- `sqlglot` for SQL parsing, `pydantic` for config and registry validation, `typer` for the CLI, `streamlit` for dashboards.
- `pytest`, `ruff` (lint and format), `mypy` on the `src/` package.
- For internal use: the company's warehouses read-only, one shared Postgres database for the toolkit's state, and thin Airflow (Cosmos) and Dagster (dagster-dbt) integrations.

Do not make the toolkit depend on an orchestrator: its code never imports Airflow or Dagster, and its image and lock file never carry them. The Makefile plus the Python CLI orchestrates `make demo`. From Phase 14, Airflow (Cosmos) and Dagster (dagster-dbt) run in their own images behind Compose profiles: to run the simulated company's dbt project the way the real company does, and to test the thin integrations end to end. Metabase (from Phase 11) is the same kind of service. None of them is ever needed by `make demo` or `make test`, and none of their own activity feeds the demo's reports.

## Code conventions

- Package layout: `src/platform_ops/{metadata,simulation,cost,incidents,reconcile,common}`.
- Every external system sits behind a small interface with a local default implementation (for example `PricingModel`, `Notifier`, `SourceConnector`). This keeps the core logic vendor-neutral so a cloud adapter can be added later without touching it.
- Type hints everywhere. Pydantic models for anything read from YAML or written as a report.
- Module outputs (facts, incidents, diff results) are stored as tables, never only in memory: in DuckDB under an `ops` schema in 1.x, in the place Phase 11 decides for 2.0, and in the shared Postgres database for company incident runs (Phase 14).
- Prefer pushing computation into the database (SQL) over pulling rows into Python, especially in `reconcile`.
- Before relying on a library or DuckDB feature (profiling settings, extension functions, PyIceberg catalog options), verify it against the installed version with a quick check or test. Do not assume API names from memory.
- Each module has unit tests for its core logic plus one integration test that runs against the simulated company.

## Documentation conventions

- `README.md` at the root: problem first, then architecture diagram (Mermaid), then quickstart, then results. Each module also has its own `README.md`.
- Architecture decisions go in `docs/adr/NNNN-title.md` using a short Context / Decision / Consequences format.
- In README and ADR prose, weave the technical decision together with its organizational effect in the same paragraph: who owns what, how a cost model changes team behavior, on-call load, who signs off a migration. Neither lens should dominate, and do not split them into separate "technical" and "business" sections.
- Never use the em dash character anywhere (code comments, docs, commit messages). Use a colon, comma, parentheses, or a new sentence instead.
- Results in READMEs must come from actual runs. Never write placeholder or invented numbers.

## Progress tracking

Maintain `docs/PROGRESS.md` as the handoff file between sessions. After every meaningful step, update it with: current phase, what is done, what is in progress, decisions made (and why), known issues, and the exact next step. At the start of every session, read `docs/PROGRESS.md` first, then `docs/SPEC.md`. Keep it short and factual.

## Git workflow

- Each phase is developed on its own branch named `phase-N-short-name` (for example `phase-1-simulated-company`).
- Commit small and often on the branch, but never merge into `main` yourself. The owner reviews and merges.
- Never commit secrets. `.env` is gitignored; provide `.env.example` instead.

## Commit hygiene

Small commits with conventional prefixes (`feat:`, `fix:`, `test:`, `docs:`, `chore:`). Do not commit generated data, DuckDB files, or Iceberg warehouse files (keep them in `.gitignore`).