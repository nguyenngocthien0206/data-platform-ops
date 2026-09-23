# CLAUDE.md

## What this project is

`data-platform-ops` is a local-first toolkit for operating a multi-team data platform. It runs a simulated company (4 business teams plus a data platform team, around 120 dbt models on DuckDB) and ships three modules on top of one shared metadata layer:

1. `cost`: attributes query cost to owning teams and flags waste.
2. `incidents`: turns raw data test failures into root-cause incidents routed to owners.
3. `reconcile`: verifies a legacy-to-lakehouse migration with a segmented checksum diff.

The full specification and the phased build plan live in `docs/SPEC.md`. Read it before starting any phase.

## Hard constraints

- Everything must run on a laptop with 16 GB RAM, offline after setup. No cloud accounts, no paid services, no API keys.
- One command brings the stack up (`make up`), one command runs the full demo (`make demo`).
- All randomness uses fixed seeds. Two runs of `make demo` on a clean checkout must produce identical reports.
- Dataset size is controlled by a single scale factor in `config/settings.yaml`. Default scale must finish `make demo` in under 10 minutes.
- Work one phase at a time as defined in `docs/SPEC.md`. At the end of each phase, stop, summarize what was built, show how to verify the acceptance criteria, and wait for review before continuing.

## Stack

- Python 3.11+, dependencies managed with `uv` (single `pyproject.toml`).
- DuckDB as the warehouse, `dbt-duckdb` for transformations.
- Postgres (default) and SQL Server Developer Edition (optional Docker Compose profile) as legacy sources.
- Iceberg via PyIceberg with a SQLite catalog and a local filesystem warehouse.
- `sqlglot` for SQL parsing, `pydantic` for config and registry validation, `typer` for the CLI, `streamlit` for dashboards.
- `pytest`, `ruff` (lint and format), `mypy` on the `src/` package.

Do not add orchestrators (Airflow, Dagster, Prefect) or heavy services. The Makefile plus the Python CLI is the orchestration layer.

## Code conventions

- Package layout: `src/platform_ops/{metadata,simulation,cost,incidents,reconcile,common}`.
- Every external system sits behind a small interface with a local default implementation (for example `PricingModel`, `Notifier`, `SourceConnector`). This keeps the core logic vendor-neutral so a cloud adapter can be added later without touching it.
- Type hints everywhere. Pydantic models for anything read from YAML or written as a report.
- Module outputs (facts, incidents, diff results) are stored as tables in DuckDB under an `ops` schema, never only in memory.
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