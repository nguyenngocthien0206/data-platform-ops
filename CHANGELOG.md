# Changelog

All notable changes to data-platform-ops. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The GitHub Release for each version uses its section below as the notes.

## [Unreleased]

### Fixed

- **A duplicated row no longer passes the reconciliation.** A differing segment's rows were kept one per key, so a row that appeared twice on one side, the classic join that doubles a report's line, compared as equal. Every row is kept now, a key that appears more than once on either side is reported as `duplicate_key`, and its rows count as unmatched.
- **A pair whose row counts do not add up fails.** The verdict now checks that the difference in row counts is exactly what the missing, extra and duplicated rows explain.
- **The reconciliation compares the legacy side as of the copy.** It compared the live legacy tables, so on a system that keeps taking writes every row written since the copy showed as missing, every night. Each table is now compared up to the copy's watermark on its `changed_at` column, a row changed in place after it counts as in flight instead of as a difference, and the legacy reports are frozen in one snapshot just before the copy reads (`reconcile.asof.freeze_reports`) and compared frozen. A pair whose legacy side is more than `max_watermark_lag_hours` behind fails.
- **The nightly record keeps the `duplicate_key` count**, which the dashboard shows with the missing and extra rows.
- **A report difference is explained only through the columns it reads.** It counted as explained when any upstream table differed, so trimmed emails explained a revenue report that never reads an email. Each mart column is now traced to the raw columns it is computed from (`metadata.column_lineage`, from dbt's compiled SQL with sqlglot, filters, joins and groupings included), and a differing report column needs one of them, or a table it reads with rows missing or extra, to differ the same night. A mart that cannot be traced falls back to its tables, and the sign-off names it.
- **A cause label needs the mechanism it names to reproduce the value.** Any difference within 0.01 was rounding and any whole number of hours a time zone shift, so a cent added on purpose passed under the rounding allowance. Rounding now means one side is the other rounded or truncated at the column's scale, directly or through a float, and a time zone shift that one side is the other read as local time in one of `reconcile.time_zones`; anything else is a value mismatch.
- **The pairs are read again every night.** They were loaded on the first night and kept for the whole parallel run, so a column added mid-run was missed, and a column missing on one side stopped the run. A pair whose columns no longer line up now fails its night with the reason (`schema: ...`), and the others are compared as usual (`specs.load_pairs`).
- **An incident resolves when its fix lands, not at the next night.** The desk waited for the next scheduled run to see the root healthy, so no incident closed in less than 24 hours. Each fix now starts a rerun of the incident's root and everything below it (`rerun-<incident>`), which the desk ingests like any run: the mean time to resolve fell from 36.0 hours to 22.3, and the dashboards are right again as soon as the fix lands.
- **A stopped feed is caught the first night.** The freshness rule allows two days, so the stopped web sessions feed was found after 64 hours. A volume test (`recent_volume`) on every continuous feed fails the first night it delivers less than half its usual rows, on the data's own clock, and the sources' tests run before the models.
- **A test on a source is about that source**, for routing and grouping (dbt attaches no node to it), and a source is healthy only when it is fresh and its tests pass.
- **A failure a rerun finds below another open incident's root that it did not check joins that incident**, instead of opening a new one and paging again.

### Changed

- **`reconcile.thresholds.max_discrepancies` needs a `duplicate_key` entry.** The thresholds still name every class, so a settings file from 2.0.0 fails to load until the line is added (`duplicate_key: 0` in the shipped config). The sign-off's criteria table gains a line for it.
- **`reconcile.thresholds.max_watermark_lag_hours` is required** (26 in the shipped config), and each table pair in `reconcile.yaml` names its `changed_at` column to be compared as of a watermark. The sign-off's night table gains a column, the rows written after the copy.
- **The simulated company keeps writing after the copy** during the parallel run (`company.writes_after_ingest_minutes`, 60): the next hour of new rows reaches Postgres before the comparison. The run ends with the same data, since those rows arrive the next day either way. M1's invented orders take keys far above the ones in use, so they are never mistaken for rows in flight.
- **`reconcile.time_zones`** lists the zones the legacy applications run in (`[America/New_York]` in the shipped config). Without it no difference is labelled a time zone shift.
- **M6 and M7 are value mismatches in the ground truth:** a cent added and a two-hour shift are real errors that look like rounding and a time zone shift. The M6 night is no longer clean; the cutover night does not move.
- **`int_campaign_performance_daily` spells out `union distinct`**, which ClickHouse already read a bare `union` as, so sqlglot can trace it.
- **A production night runs five invocations:** source freshness, the sources' tests, run, snapshot and the other tests. `CompanyDesk` returns, with each fix, the dbt invocations to rerun (`Fix.commands`), and `company run` reports how many reruns it ran.
- **The incident and cost numbers moved:** 39 failing checks for the same 8 incidents and pages; the reruns and the volume tests add 968 priced queries to dbt's share of the bill, which stays $1,197.24.

## [2.0.0] - 2026-10-15

The simulated company now looks like the one the toolkit is meant for: its reporting runs on a legacy Postgres, it migrates to a self-managed ClickHouse with its analytics rebuilt as one dbt project, and every module works on that migration. The reconciliation signs it off over a six-week parallel run, the cost module shares the cluster's bill out by team, and the incident desk runs on the company's production nights and on real Airflow and Dagster runs.

### Added

- **The company on Postgres and ClickHouse.** A legacy Postgres with a year of history and 12 hand-written SQL reports; an incremental copy into ClickHouse by an `updated_at` watermark into `ReplacingMergeTree` raw tables; the dbt project rebuilt for ClickHouse (118 models, a seed, a snapshot, 12 exposures), modelled per layer. The window is 16 weeks: seven before the migration, a six-week parallel run, three weeks of production.
- **A ClickHouse user per team and service**, with grants per layer and no admin reads, and **Metabase dashboards generated from the dbt exposures** (`make metabase`, `make docker-metabase-check`, which runs every card and opens every dashboard in a browser).
- **The parallel run and the cutover sign-off** (`reconcile`): the 9 operational tables and the 12 reports compared with ClickHouse every night by a segmented checksum diff, report differences explained through dbt's lineage, equality rules and thresholds the data owner signs, and a cutover recommended after 14 clean nights in a row.
- **FinOps on a self-managed cluster** (`cost`): every query tagged with what it was for, the stated monthly cost shared out exactly by bytes read and stored, production charged to the owner and reads to the reader, the migration's business case and payback, BigQuery and Snowflake priced on the same measured workload, unused tables, hotspots and suggested quotas.
- **Incidents on the company's production runs** (`incidents`): faults in operation, every dbt invocation ingested into a Postgres store any run can feed (`platform-ops incidents ingest --target --run-id`), one incident per root regrouped across a run's invocations, routing to the feed's owner, resolution by a later healthy run, Slack and Telegram per team, and `incidents summary`.
- **Real orchestrators**: Airflow 3 with Cosmos and Dagster with dagster-dbt, each in its own image and on its own company (`make airflow`, `make dagster`), calling the toolkit through standard-library hooks (`platform_ops.integrations`); `make docker-test-orchestrators` puts a fault through each, and the release waits for both.
- **The Streamlit dashboards on the ops store**, read through a read-only role of their own.
- **Documentation**: the README around the migration, with an architecture diagram generated from the project (`make docs-diagram`) and screenshots from the release run (`make docker-docs-images`); how each problem was solved; an "Adopting it" section in every module README; a test that every relative link in the docs resolves.

### Changed

- **Cost is measured, not estimated**: bytes and rows come from ClickHouse's query log; the dollar figures of 1.x do not carry over.
- **dbt builds for real every night** of the parallel run and production, instead of a build every four weeks with replayed days in between.
- **`readme-check`** checks the 2.0 results; a line whose numbers come from the console or the database is marked `<!-- readme-check: console -->`.
- **The architecture decision records** were renumbered 0001 to 0017 after three 1.x records were retired (old-to-new numbers in `docs/PROGRESS.md`).

### Removed

- The DuckDB warehouse and `dbt-duckdb` (DuckDB only generates the company's data now), the Iceberg lakehouse and `pyiceberg`, and SQL Server as a legacy source (`MSSQL_*` in `.env`).
- `platform-ops simulation run` and `make simulate`: `make demo` runs the company (`platform-ops company run`).
- The Slack webhook notifier: Slack and Telegram now use their bots' tokens.

### Upgrading from 1.x

- Copy the new variables from `.env.example`: a password for each company ClickHouse user (`CLICKHOUSE_PASSWORD_<USER>`), the dashboards' role (`POSTGRES_PASSWORD_DASHBOARD`), Metabase's admin, and `AIRFLOW_ADMIN_PASSWORD` for the Airflow stack. None has a default; a missing one stops the run and names the variable.
- `make docker-clean`, then `make docker-demo`: the 1.x volumes hold nothing 2.0 reads.
- The ownership check is unchanged: a consuming repository moves to the 2.0.0 image by changing its tag.

## [1.2.0] - 2026-10-13

A starter registry, so rolling the ownership check out on a large dbt project starts from a short to-do list instead of a blank file.

### Added

- **`platform-ops metadata init-registry`** writes `teams.yaml` and `ownership.yaml` from a manifest, with one `TODO` rule per group of datasets: each source system, each folder of models (split by name prefix where a folder mixes them), each installed package, the seeds, the snapshots, and each exposure with the owner its dbt definition names. Every dataset matches exactly one rule and no two rules tie. On the bundled project, 139 datasets become 63 rules. An existing registry is only overwritten with `--force`.
- A step-by-step rollout guide in the metadata README, from the starter registry to a required check.

### Changed

- Coverage, and the summary's `owned_share`, count a dataset as owned only when its rule names a real team and one of its members, so a starter registry reads 0% and the number rises as the TODOs close.
- The GitHub Actions example pulls the 1.2.0 image.

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
- ADR 0011: the ownership gate runs in the consuming repository, on its manifest.

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
- **Architecture decision records**, ten of them (three were retired in 2.0 and the rest renumbered).

### Changed

- A whole-repo review before the release: broader lint rules, shared helpers for report tables, `ops` tables and isolated configs, module boundaries pinned by a test, dead code removed, and a shared test warehouse. Reports stayed byte-identical.
- The 10-minute budget for `make demo` was lifted; measured times are recorded instead.
- The pandas and pyarrow caps added for Windows Smart App Control were lifted (pandas 3, pyarrow 25), and mypy moved to its compiled 2.x wheels. Reports stayed byte-identical.

[2.0.0]: https://github.com/nguyenngocthien0206/data-platform-ops/releases/tag/v2.0.0
[1.2.0]: https://github.com/nguyenngocthien0206/data-platform-ops/releases/tag/v1.2.0
[1.1.0]: https://github.com/nguyenngocthien0206/data-platform-ops/releases/tag/v1.1.0
[1.0.0]: https://github.com/nguyenngocthien0206/data-platform-ops/releases/tag/v1.0.0
