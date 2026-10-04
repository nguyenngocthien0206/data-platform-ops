# Changelog

All notable changes to data-platform-ops. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The GitHub Release for each version uses its section below as the notes.

## [1.1.0] - 2026-10-04

The ownership check leaves the demo: it now guards any company dbt project from that repository's own CI, starting with the data platform team's project on self-managed ClickHouse.

### Added

- **The ownership check on any dbt project.** `platform-ops metadata check --manifest <manifest.json> --registry-dir <folder>` checks a manifest from any adapter against a registry kept in the consuming repository. It loads no settings, opens no database and writes nothing but an optional summary, so the published image can run it in a company CI with no credential.
- **Output a reviewer can act on.** Every finding names the file to change and how, with a ready-to-paste rule for an unowned dataset. `--format github` turns findings into annotations on the pull request, on the dataset's file or on the rule's line in `ownership.yaml`.
- **A gentle rollout.** `--report-only` reports every problem without failing, and `--summary-json` records coverage per resource type, so a team measures its baseline before making the check required.
- **Exit codes for a required check:** 1 for owners to fix, 2 when the check cannot run.
- **A GitHub Actions example** for a company dbt repository (`examples/github-actions/ownership-check.yml`): `dbt parse` with a parse-only profile, then the check from the public image.
- **ClickHouse.** A `clickhouse` Compose profile, a `dbt-clickhouse` extra, and a fixture dbt project on ClickHouse (`tests/fixtures/dbt_clickhouse`) that the tests parse and build for real. CI runs the tests with Postgres and ClickHouse.
- **`telegram_chat` per team** in `teams.yaml`, optional, next to the Slack `channel`.
- ADR 0014: the ownership gate runs in the consuming repository, on its manifest.

### Changed

- Seeds and snapshots need an owner too, alongside sources, models and exposures. The bundled project has none, so its results are unchanged.
- A rule that matches nothing is now reported as "matches no dataset".

## [1.0.0] - 2026-09-29

The first release: a local-first toolkit for operating a multi-team data platform, with cost attribution, incident management and migration reconciliation over one shared metadata layer. Everything runs offline on a laptop, natively or in Docker Compose, and every run at a given scale writes byte-identical reports.

### Added

- **Simulated company.** A deterministic generator for an e-commerce dataset (customers, products, orders, order items, payments, web sessions, marketing campaigns and spend, support tickets) with realistic mess, and a dbt-duckdb project of about 120 models across staging, intermediate, four team mart folders and 12 abandoned models, with tests, source freshness on simulated time, and 12 dashboards as exposures.
- **Shared metadata layer.** An ownership registry with a most-specific-match rule and `platform-ops metadata check` (CODEOWNERS for data), and a lineage graph from the dbt manifest with upstream, downstream, nearest failed ancestor and tier-weighted consumers.
- **Cost attribution** (`platform-ops cost report`). A query log fed by dbt run results and a logged connection for dashboards and ad hoc users, SQL parsing with sqlglot, a scan estimate from logical bytes, scan and compute pricing side by side, production and consumption cost by team and month, and recommendations for unused tables, full-scan hotspots and incremental candidates. Over a 91-day window the reference run prices 35,813 queries at $296.02 under compute pricing and $3.35 under scan pricing, and finds exactly the 12 abandoned models.
- **Cloud-ready collectors** for BigQuery `INFORMATION_SCHEMA.JOBS` and Snowflake `QUERY_HISTORY`, verified by contract tests against recorded fixtures, with no network access.
- **Incident management** (`platform-ops incidents run`). A labelled fault injector (null spikes, duplicate keys, stale sources, schema changes, volume drops, invalid categories), ingestion of dbt results and freshness, root-cause grouping on lineage, severity from tier and downstream impact, routing to the owner of the faulted source, a notifier (local log, optional Slack webhook), a simulated lifecycle, metrics and postmortems. The reference scenario turns 18 failing checks from 8 faults into 8 incidents, each paging the right person.
- **Migration reconciliation** (`platform-ops reconcile run`). A legacy dataset in Postgres (and SQL Server behind a Compose profile), a migration job into Iceberg with four planted defects, a segmented checksum diff whose row hash is byte-identical across Python, DuckDB, Postgres and SQL Server, per-column canonical rules, root-cause classification, and a sign-off report against thresholds fixed before the run. The reference run finds all 199,465 planted discrepancies with 100% recall, precision and classification accuracy.
- **Dashboards** (`platform-ops dashboard`). A Streamlit app with one page per module, read-only over the `ops` schema, in light and dark themes.
- **Container.** A `Dockerfile` and Compose services for the toolkit, the dashboards and a headless Chromium check of every dashboard page, with `docker-*` make targets that run the demo, the tests, lint and the README check inside the container. `TOOLKIT_IMAGE` runs a published image instead of the local build.
- **CI and release.** Lint, type check, tests and the ownership check on every pull request and push to `main`, in the container with Postgres; an on-demand job that runs the tests against SQL Server too; and, on a version tag, the image published to GitHub Container Registry and a GitHub Release with these notes.
- **Checks on the documentation.** `make readme-check` confirms that every number in the READMEs' results sections appears in the generated reports.
- **Architecture decision records** 0001 to 0010.

### Changed

- A whole-repo review before the release: broader lint rules, shared helpers for report tables, `ops` tables and isolated configs, module boundaries pinned by a test, dead code removed, and a shared test warehouse. Reports stayed byte-identical.
- The 10-minute budget for `make demo` was lifted; measured times are recorded instead.
- The pandas and pyarrow caps added for Windows Smart App Control were lifted (pandas 3, pyarrow 25), and mypy moved to its compiled 2.x wheels. Reports stayed byte-identical.

[1.1.0]: https://github.com/nguyenngocthien0206/data-platform-ops/releases/tag/v1.1.0
[1.0.0]: https://github.com/nguyenngocthien0206/data-platform-ops/releases/tag/v1.0.0
